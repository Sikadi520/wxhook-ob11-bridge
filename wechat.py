# -*- coding: utf-8 -*-
"""
微信侧:wxhook 的生命周期与收发

wxhook 0.0.10 只支持微信 3.9.5.81(源码里写死)。
Bot(...) 构造时会自动:启动微信 -> 注入 wxhook.dll -> 伪装版本号。

本模块额外自动补上你那一步手动的 wechat_patcher.py(x64 -> x32),
这样启动就只剩一条命令,不用再开两个窗口。

## 2026-10-23 排障报告带来的三处改动

1. **faker 结果进日志**(报告 P1-2)
   wxhook 用的是自己的 loguru logger,只写 stdout,不进 logs/bridge.log。
   于是"伪装没执行 / 执行了但失败 / 成功但门禁仍拦"三者从文件日志里分不清。
   现在:构造 Bot 时**不再**把 faked_version 交给它,而是在 on_start 里显式
   调 ``fake_wechat_version`` 并记录返回码;同时把 wxhook 的 loguru 接到
   bridge.log 上,SUCCESS/ERROR 原文也一起落盘。

2. **杀软拦截给出可操作提示**(报告 P0-2)
   faker.exe 被 Defender 查杀后,报错是 WinError 225(文件包含病毒)或
   WinError 2(找不到文件),堆栈埋在 wxhook 内部。现在单独识别,直接打印
   排除命令、恢复办法。

3. **补丁在 on_start 里打,时序不变**(报告 P0-3)
   start-wechat.exe 是"挂起 -> 注入 -> 恢复",恢复后客户端马上做版本校验。
   on_start 是 wxhook 在恢复之后、握手之前回调的同步钩子,补丁在这里完成,
   仍然是"客户端开始说话之前"。唯一改动是:回读处数为 0 时明确告警 ——
   那基本一定过不了门禁,而不是"补丁跑完了"。
"""

import base64
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

from wxhook import Bot, events

log = logging.getLogger("wechat")

# "微信已登录"的就绪标记。外部程序(比如 AstrBot 的启动致辞插件)可以轮询它,
# 避免在微信还没登录时发消息 —— 那种情况 wxhook 照样返回 code=1,消息却丢了。
READY_FILE = Path(__file__).resolve().parent / "ready.json"

# 微信图片落盘时可能是加密的 .dat(前 4 字节是这个魔数),需要 DLL 解码一次
_DAT_MAGIC = (b"\x07\x08\x56\x31", b"\x07\x08\x56\x32")

# 合法图片的文件头。用来挡掉"分块落盘的半截文件"——大图下载时目录里会先出现
# <msgId>.dat<hex> 这种 512KB 分块,它不是图片,发给模型会 400。
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",            # JPEG
    b"\x89PNG\r\n\x1a\n",       # PNG
    b"GIF87a", b"GIF89a",       # GIF
    b"RIFF",                    # WEBP(RIFF....WEBP)
)

# WinError:杀软 / 文件缺失。P0-2 里这两个错误是同一件事的两种表现
WINERROR_BLOCKED = 225          # ERROR_VIRUS_INFECTED:包含病毒或潜在的垃圾软件
WINERROR_NOT_FOUND = 2          # ERROR_FILE_NOT_FOUND:faker.exe 已经没了

ANTIVIRUS_HINT = """
╔══════════════════════════════════════════════════════════════════════╗
║  微信没能启动:faker.exe 被 Windows Defender 拦截或已删除              ║
╚══════════════════════════════════════════════════════════════════════╝
这是 wxhook 自带的"版本伪装"工具,拿不到它,微信进程根本起不来。
请用【管理员】PowerShell 依次执行:

    Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\\uv\\cache"
    Add-MpPreference -ExclusionProcess "faker.exe"
    Add-MpPreference -ExclusionProcess "start-wechat.exe"

然后让它把被删的文件补回来(任选其一):

    uv cache clean wxhook          # 清掉缓存,下次启动会重新下载
    # 或者从别的机器/别的缓存副本拷回 wxhook/tools/faker.exe

重新启动 start.bat 即可。如果装了第三方杀软(360/火绒等),也要加同样的排除。
"""


def _winerror_of(exc):
    """
    从异常链里挖出 WinError。

    三层兵底,按可靠程度递减:
      1. OSError.winerror(最准)—— 包括链上被包住的那些异常
      2. 消息里的 "WinError NNN"(pymem / 第三方库会把它拼进 str)
      3. OSError.errno —— **注意**:
         ``OSError(225, "包含病毒")`` 这种两参数构造,码落在 errno 里,
         winerror 是 None;而真正从 Windows API 抛出来的 OSError
         是两个都有。所以不能只看 winerror,否则手动构造/被包装过的
         异常(排障工具、单测、第三方封装层)就认不出来了。

    注意不能用 "WinError 2" 这种子串匹配:它是 "WinError 225" 的前缀。
    """

    def code_of(obj):
        for attr in ("winerror", "errno"):
            value = getattr(obj, attr, None)
            if value:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = code_of(cur)
        if code:
            return code
        found = re.findall(r"WinError (\d+)", str(cur))
        if found:
            return int(found[-1])
        cur = cur.__cause__ or cur.__context__
    return None


def _is_blocked_by_av(exc):
    """faker.exe 被查杀 / 被拦 / 直接不见。"""
    code = _winerror_of(exc)
    if code in (WINERROR_BLOCKED, WINERROR_NOT_FOUND):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("virus", "病毒", "垃圾软件",
                                   "faker.exe", "系统找不到指定的文件"))


class WeChatSide:

    def __init__(self, cfg: dict, on_message, on_image=None, on_xml=None):
        self.cfg = cfg
        self.on_message = on_message          # 回调:收到文本消息 -> event
        self.on_image = on_image              # 回调:收到图片消息 -> event
        self.on_xml = on_xml                  # 回调:收到 XML/引用消息(type=49) -> event
        self.bot = None
        self.ready = threading.Event()
        self.self_wxid = cfg.get("self_wxid") or ""
        # DLL 是先返回、后落盘;等多久由这里控制(大图分块落盘会慢一点,8s 够用)
        self.image_wait_seconds = float(cfg.get("image_wait_seconds", 8) or 8)
        # 供日志和排障使用:本轮伪装 / 补丁到底发生了什么
        self.faker_ok = None                  # None=没做,True/False=成功与否
        self.patch_counts = None              # 补丁 summary(dict)

    # ---------- 启动 ----------

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="wxhook")
        t.start()

    def _run(self):
        self._clear_ready_file()      # 重启先撤掉标记,别让外部以为已经能发
        try:
            def on_start(bot):
                log.info("微信已启动,wxhook.dll 已注入")
                self.ready.set()
                # 伪装和补丁是门禁的两个必要条件,都在这里同步做完。
                # 顺序:先伪装版本(改 6 处版本整数),再打 x64->x32 补丁。
                self._apply_faked_version(bot)
                if self.cfg.get("patch_x64_to_x32", True):
                    # Bot's constructor invokes this callback before self.bot is assigned.
                    self._patch_x64_to_x32(bot)

            def on_login(bot, event):
                try:
                    info = bot.get_self_info()
                    self.self_wxid = getattr(info, "wxid", "") or self.self_wxid
                    log.info("已登录: %s (%s)",
                             getattr(info, "name", "?"), self.self_wxid)
                    self._log_gate_summary()
                    self._write_ready_file(info)
                except Exception:
                    log.exception("获取自身账号信息失败")

            # 注意:faked_version 故意不传给 Bot —— 传进去的话 wxhook 只把结果
            # 打到自己的 stdout(loguru),bridge.log 里看不到(报告 P1-2)。
            # 这里改成 on_start 里显式调用,拿到返回码自己记。
            faked = self.cfg.get("faked_version") or None
            log.info("构造 wxhook Bot(版本伪装 -> %s,由桥接显式调用 faker)", faked)
            self.bot = Bot(faked_version=None, on_start=on_start, on_login=on_login)
            self._start_watchdog()

            @self.bot.handle(events.TEXT_MESSAGE)
            def _on_text(bot, event):
                try:
                    self.on_message(event)
                except Exception:
                    log.exception("处理微信消息失败")

            # 图片消息:以前只订阅 TEXT_MESSAGE,微信里发的图压根没进桥接,
            # 表现就是"AstrBot 配好了视觉模型也永远收不到图"。
            @self.bot.handle(events.IMAGE_MESSAGE)
            def _on_image(bot, event):
                try:
                    if self.on_image:
                        self.on_image(event)
                    else:
                        log.info("收到图片消息,但未注册图片处理函数,已忽略")
                except Exception:
                    log.exception("处理微信图片消息失败")

            # 引用回复(type=49 appmsg):以前没订阅,所以"引用某人 + @小助手 问一句"
            # 里被引用的那句话完全到不了 AstrBot —— 而群里只放行含"小助手"的消息,
            # 别人说的原话压根不在上下文里。引用是唯一的补充途径。
            @self.bot.handle(events.XML_MESSAGE)
            def _on_xml(bot, event):
                try:
                    if self.on_xml:
                        self.on_xml(event)
                    else:
                        log.info("收到 XML 消息(type=49),但未注册处理函数,已忽略")
                except Exception:
                    log.exception("处理微信 XML/引用消息失败")

            log.info("进入微信消息监听循环")
            self.bot.run()
        except Exception as exc:
            self._report_start_failure(exc)
            self.ready.clear()

    # ---------- 启动失败的可读报错 ----------

    def _report_start_failure(self, exc):
        """
        把"看不出所以然"的启动异常翻译成下一步该做什么。

        报告 P0-2 的现场:
            OSError: [WinError 225] 无法成功完成操作,因为文件包含病毒…
            FileNotFoundError: [WinError 2] 系统找不到指定的文件。
        旧代码只打一句"微信是否已安装 3.9.5.81?",用户只能去翻堆栈。
        """
        code = _winerror_of(exc)
        if _is_blocked_by_av(exc):
            log.error(ANTIVIRUS_HINT)
            log.error("原始错误: %s: %s", type(exc).__name__, exc)
            return
        if code == 10043:
            log.error("微信侧监听端口被占用(WinError 10043)。")
            log.error("常见原因:上次的微信/注入进程还没退出。先手动退出微信再重启;")
            log.error("本脚本下次启动会自动清理 wxhook 端口状态文件。")
            log.error("原始错误: %s: %s", type(exc).__name__, exc)
            return

        # 管理员权限单独说清楚 —— 权限不够时这里报的往往是含糊的拒绝访问
        try:
            from preflight import is_admin
        except Exception:
            is_admin = lambda: True   # noqa: E731
        elevated = is_admin()
        if not elevated:
            # 自检已经报过“不是管理员”,这里只补一句怎么处理,不重复同一个警告
            log.error("注入 DLL 必须提权:请右键 start.bat → 以管理员身份运行。")

        log.exception("微信侧启动失败:请确认微信 3.9.5.81 已安装、且启动前没有在运行"
                      if elevated else "微信侧启动失败(当前进程未提权)")
        if code:
            log.error("WinError %s —— 具体见上面的堆栈和 logs/bridge.log", code)

    # ---------- 版本伪装(显式调用 + 落日志) ----------

    def _apply_faked_version(self, bot):
        """
        调用 wxhook 的 faker,并把结果写进 bridge.log。

        为什么不直接用 Bot(faked_version=...):
          wxhook 内部只把成败打到它自己的 logger(stdout),文件日志里没有,
          排障时分不清"没执行 / 执行失败 / 成功但门禁仍拦"(报告 P1-2)。

        为什么不复用 bot.version:
          Bot 构造时 self.version 已经定死,这里从 bot 上取,取不到再退回
          3.9.5.81,保证老版本 wxhook(字段名不同)也不会直接崩。
        """
        faked = self.cfg.get("faked_version") or None
        if not faked:
            log.info("未配置 faked_version,跳过版本伪装(门禁大概率过不了)")
            self.faker_ok = None
            return

        old = str(getattr(bot, "version", None) or "3.9.5.81")
        try:
            from wxhook.utils import fake_wechat_version
        except Exception:
            log.warning("这个 wxhook 版本没有 fake_wechat_version,退回 Bot(faked_version=…)"
                        " 的默认路径,结果不会进 bridge.log")
            self.faker_ok = None
            try:
                bot.faked_version = faked
            except Exception:
                pass
            return

        pid = None
        try:
            pid = int(bot.process.pid)
        except Exception:
            log.exception("取不到微信 pid,无法调用 faker")
            self.faker_ok = False
            return

        log.info("调用 faker:pid=%s %s -> %s", pid, old, faked)
        try:
            code = fake_wechat_version(pid, old, faked)
        except Exception as exc:
            self.faker_ok = False
            log.error("faker 调用异常:%s: %s", type(exc).__name__, exc)
            if _is_blocked_by_av(exc):
                log.error(ANTIVIRUS_HINT)
            return

        self.faker_ok = (code == 0)
        if code == 0:
            log.info("faker 返回 0(成功),目标版本 %s", faked)
            log.info("  微信版本已从 %s 改为 %s", old, faked)
            log.info("  注意:faker 只改版本整数的 1 处(x64->x32 补丁负责其余位置),")
            log.info("  所以它必须和补丁一起用才过得了门禁。")
        else:
            log.error("faker 返回 %s(失败):版本伪装没有生效,门禁一定过不去", code)
            log.error("  常见原因:微信进程已退出 / 权限不足 / 目标版本号不被支持")

    def _log_gate_summary(self):
        """登录成功后把门禁三要素打一遍,以后对照日志就能判断成败原因。"""
        faked = self.cfg.get("faked_version") or "(未开启)"
        counts = self.patch_counts
        if counts is None:
            patch = "未执行"
        else:
            patch = "回读确认 %d 处(可疑跳过 %d、写失败 %d)" % (
                counts.get("verified", 0),
                counts.get("skipped_crash_site", 0),
                sum(v for k, v in counts.items() if k.startswith("write_failed")))
        log.info("登录门禁要素:伪装=%s %s | 补丁=%s | 微信自身版本=3.9.5.81",
                 faked, "(成功)" if self.faker_ok else "(失败/未知)", patch)

    # ---------- 就绪标记(给外部程序判断"能不能发了") ----------

    def _clear_ready_file(self):
        try:
            READY_FILE.unlink()
            log.info("已撤掉就绪标记")
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("删除就绪标记失败:%s", READY_FILE)

    def _write_ready_file(self, info):
        """
        微信真正登录后才写。为什么要单独一个文件:
        未登录时 wxhook 的 sendTextMsg 照样返回 code=1,消息却凭空消失 ——
        外部(比如启动致辞插件)只看返回值会误判成"发成功了"。
        """
        try:
            READY_FILE.write_text(json.dumps({
                "ready": True,
                "wxid": self.self_wxid,
                "name": str(getattr(info, "name", "") or ""),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "faked_version": self.cfg.get("faked_version") or "",
                "faker_ok": self.faker_ok,
                "patch": self.patch_counts or {},
            }, ensure_ascii=False), encoding="utf-8")
            log.info("就绪标记已写:%s", READY_FILE)
        except OSError:
            log.exception("写就绪标记失败:%s", READY_FILE)

    # ---------- x64 -> x32 补丁(自动化原 wechat_patcher.py) ----------

    def _patch_x64_to_x32(self, bot):
        try:
            import pymem
            import pymem.memory
        except ImportError:
            log.warning("未安装 pymem,x64->x32 补丁已跳过(请用 --with pymem 运行)")
            return
        pm = None
        try:
            from patch_audit import run_patch, native_protect

            # Only patch the process this Bot owns, not an arbitrary WeChat.exe.
            pm = pymem.Pymem(bot.process.pid)
            path, counts = run_patch(pm, self.cfg, log,
                                     Path(__file__).resolve().parent / "patch_audit",
                                     pymem.memory.virtual_query,
                                     protect_fn=native_protect)
            self.patch_counts = counts
            if not counts.get("verified"):
                log.error("补丁回读确认 0 处 —— 几乎一定过不了门禁。")
                log.error("  查 %s:是不是没提权、微信版本不是 3.9.5.81、"
                          "或者 patch_max_sites 设得太小", path)
            elif counts.get("skipped_crash_site"):
                log.info("  已按崩溃可疑点跳过 %d 处(wcprobe.dll+0x26A6DD)",
                         counts["skipped_crash_site"])
        except Exception:
            log.exception("补丁本轮执行未完成，请检查审计日志和具体异常；未修改配置开关")
        finally:
            if pm is not None:
                try:
                    pm.close_process()  # Close our handle only; do not terminate WeChat.
                except Exception:
                    log.exception("关闭补丁进程句柄失败")

    # ---------- 收图 ----------

    @staticmethod
    def _pick_path(data):
        """从 DLL 返回的 data 里猜出一个本地文件路径。

        wxhook 没有文档说明 downloadAttach 的返回结构,各版本字段名不统一,
        所以这里按常见键名找一遍,再往嵌套字典里找一层。
        """
        if isinstance(data, str) and data:
            return data
        if isinstance(data, dict):
            for k in ("path", "filePath", "file_path", "imagePath", "image_path",
                      "savePath", "save_path", "file", "location", "url"):
                v = data.get(k)
                if isinstance(v, str) and v:
                    return v
            for v in data.values():
                if isinstance(v, dict):
                    p = WeChatSide._pick_path(v)
                    if p:
                        return p
        return ""

    def image_base64(self, event) -> str:
        """
        取一张图片的 base64。

        两条路:
          1. 事件里带 base64Img —— 私聊一般都有,直接用;
          2. **群里的事件通常没有 base64Img**(实测),只能让 DLL 把附件落地再读:
             download_attachment(msgId) -> 路径 -> 若是加密 .dat 再 decode_image。

        拿不到就返回 "",由调用方决定丢弃并告警。
        """
        b64 = (getattr(event, "base64Img", None) or "").strip()
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        if b64:
            return b64

        msg_id = getattr(event, "msgId", None)
        if not msg_id:
            log.warning("图片事件既无 base64Img 也无 msgId,无法取值")
            return ""
        if not getattr(self, "bot", None):
            log.warning("微信侧尚未就绪,无法下载图片(msgId=%s)", msg_id)
            return ""

        try:
            resp = self.bot.download_attachment(int(msg_id))
        except Exception:
            log.exception("download_attachment 调用失败(msgId=%s)", msg_id)
            return ""

        log.info("download_attachment(msgId=%s) -> code=%s msg=%s data=%r",
                 msg_id, getattr(resp, "code", None), getattr(resp, "msg", None),
                 getattr(resp, "data", None))

        # 实测(2026-10-18):这个接口【不在返回值里给路径】,而是把文件写到
        #   <dataSavePath>\wxhelper\image\<msgId>.dat        (原图,明文 JPEG)
        #   <dataSavePath>\wxhelper\image\<msgId>_t.dat      (缩略图)
        # 而且它是"先返回、后写盘" —— 实测返回和落盘在同一秒,直接查必然查空,
        # 所以这里要轮询等一下。
        path = self._pick_path(getattr(resp, "data", None))
        if path and os.path.exists(path) and not self._looks_like_image(path):
            log.warning("DLL 给的路径不是合法图片,改去落盘目录找:%s", path)
            path = ""
        if not path or not os.path.exists(path):
            path = self._wait_for_downloaded_image(msg_id)
        if not path or not os.path.exists(path):
            log.warning("等了 %ss 仍没等到落盘文件(msgId=%s),目录=%r",
                        self.image_wait_seconds, msg_id, self._image_save_dir())
            return ""

        try:
            raw = Path(path).read_bytes()
        except Exception:
            log.exception("读取图片文件失败:%s", path)
            return ""

        # 落到 <msgId>.dat 的文件通常已经是明文(实测 JPEG:FF D8 FF E0),
        # 只有旧版本/特殊情况才是微信加密格式(07 08 56 31/32),那种才需要解码。
        if raw[:4] in _DAT_MAGIC:
            out_dir = tempfile.mkdtemp(prefix="wxhook_dec_")
            try:
                dec = self.bot.decode_image(str(path), out_dir)
                dec_path = self._pick_path(getattr(dec, "data", None))
                if not dec_path:
                    # decodeImage 同样可能只往 storeDir 里写文件
                    written = sorted(Path(out_dir).glob("*"),
                                     key=lambda p: p.stat().st_mtime, reverse=True)
                    dec_path = str(written[0]) if written else ""
                log.info("加密 .dat 已解码 -> %r(code=%s)",
                         dec_path, getattr(dec, "code", None))
                if dec_path and os.path.exists(dec_path):
                    raw = Path(dec_path).read_bytes()
                else:
                    log.warning("decode_image 没产出文件,%s", path)
                    return ""
            except Exception:
                log.exception("decode_image 失败:%s", path)
                return ""

        log.info("图片取到:%s(%d 字节)", path, len(raw))
        return base64.b64encode(raw).decode("ascii")

    def _image_save_dir(self):
        """wxhook 落盘的图片目录:IMAGE_SAVE_PATH,拿不到就用 dataSavePath 拼。"""
        d = getattr(self.bot, "IMAGE_SAVE_PATH", None)
        if d and os.path.isdir(d):
            return d
        base = getattr(self.bot, "DATA_SAVE_PATH", None)
        if base:
            cand = os.path.join(base, "wxhelper", "image")
            if os.path.isdir(cand):
                return cand
        return None

    def _wait_for_downloaded_image(self, msg_id, timeout=None):
        """
        轮询等 DLL 把图片落盘,并且**确认它是一张合法的图片**。

        两个坑都是实测踩出来的:
          1. `download_attachment` 是**先返回、后写盘**的(返回和落地常在同一秒),
             返回后立刻查目录必然查空;
          2. **大图会被分块落盘** —— 目录里会先出现
             `<msgId>.dat<十六进制串>` 这种 512KB 的**分块文件**,它不是合法图片。
             2026-10-18 08:53 就栽在这上面:把分块当原图发出去,
             模型直接 400 "You have uploaded an unsupported image"。

        所以这里只认精确文件名,并且要校验文件头确实是图片才返回。
        """
        if timeout is None:
            timeout = self.image_wait_seconds
        d = self._image_save_dir()
        if not d:                            # 连目录都不知道,别白等
            return ""
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            p = self._find_downloaded_image(msg_id)
            if p and self._looks_like_image(p):
                try:
                    size1 = os.path.getsize(p)
                    if size1 > 0:
                        time.sleep(0.1)      # 等它写稳
                        if os.path.getsize(p) == size1:
                            return p
                except OSError:
                    pass
            if time.time() >= deadline:
                break
            time.sleep(0.1)

        # 超时了:退而求其次用缩略图(能看,只是糊)
        thumb = os.path.join(d, f"{msg_id}_t.dat")
        if os.path.exists(thumb) and self._looks_like_image(thumb):
            log.warning("等不到原图,退回缩略图 %s", os.path.basename(thumb))
            return thumb
        log.warning("等了 %ss 也没有等到一张合法图片(msgId=%s)", timeout, msg_id)
        return ""

    @staticmethod
    def _looks_like_image(path) -> bool:
        """看文件头判断是不是图片(或微信加密格式)。分块/半截文件会被挡掉。"""
        try:
            with open(path, "rb") as f:
                head = f.read(12)
        except OSError:
            return False
        if not head:
            return False
        if head[:4] in _DAT_MAGIC:          # 微信加密 .dat,后面 decode
            return True
        return any(head.startswith(m) for m in _IMAGE_MAGIC)

    def _find_downloaded_image(self, msg_id):
        """
        在 wxhelper/image 下按 msgId 找刚下载出来的文件。

        **只认精确文件名** —— 不能用 `<msgId>*` 宽松匹配:大图分块落盘时,
        目录里会有 `<msgId>.dat<hex>` 这种分块文件,宽松匹配会优先抓到它。
        """
        d = self._image_save_dir()
        if not d:
            return ""
        for name in (f"{msg_id}.dat", f"{msg_id}.jpg", f"{msg_id}.png",
                     f"{msg_id}.gif", f"{msg_id}.webp"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        return ""

    # ---------- 发送 ----------

    def send_text(self, wxid: str, text: str):
        if not self.bot:
            raise RuntimeError("微信侧尚未就绪")
        r = self.bot.send_text(wxid, text)
        log.info("发送文本 -> %s : code=%s msg=%s",
                 wxid, getattr(r, "code", None), getattr(r, "msg", None))
        return r

    def send_image(self, wxid: str, path: str):
        if not self.bot:
            raise RuntimeError("微信侧尚未就绪")
        r = self.bot.send_image(wxid, path)
        log.info("发送图片 -> %s : code=%s", wxid, getattr(r, "code", None))
        return r

    def send_emotion(self, wxid: str, path: str):
        """
        发表情(贴纸)。GIF 只有走这条路才会动。

        2026-10-21 实测:同一个 gif 走 send_image(图片消息)发出去是静态首帧,
        走 send_emotion(sendCustomEmotion)才是会动的小表情。
        """
        if not self.bot:
            raise RuntimeError("微信侧尚未就绪")
        r = self.bot.send_emotion(wxid, path)
        log.info("发送表情 -> %s : code=%s", wxid, getattr(r, "code", None))
        return r

    def send_file(self, wxid: str, path: str):
        """
        发文件。放歌的音频只能走这条路 —— wxhook 0.0.10 没有 send_voice,
        发不出微信语音气泡(语音还要 AMR/SILK 编码),只能当文件发。
        """
        if not self.bot:
            raise RuntimeError("微信侧尚未就绪")
        r = self.bot.send_file(wxid, path)
        log.info("发送文件 -> %s : code=%s msg=%s",
                 wxid, getattr(r, "code", None), getattr(r, "msg", None))
        return r

    # ---------- 看门狗:微信崩了必须看得见 ----------

    def _start_watchdog(self):
        threading.Thread(target=self._watchdog, daemon=True,
                         name="wx-watchdog").start()

    def _watchdog(self):
        """
        盯着微信进程,它一消失就在窗口里打出醒目提示。

        为什么需要:进程没了以后,桥接剩下的表现只是
        "ConnectionRefusedError [WinError 10061]" 这种看不懂的报错,
        很容易被误判成网络问题 —— 或者像之前那样被误判成权限问题。
        """
        was_alive = True
        while True:
            time.sleep(5)
            alive = True
            try:
                proc = getattr(self.bot, "process", None)
                if proc is not None:
                    alive = bool(proc.is_running())
            except Exception:
                alive = False

            if was_alive and not alive:
                log.error("")
                log.error("=" * 70)
                self.ready.clear()
                self._clear_ready_file()   # 微信没了,就绪标记必须撤掉
                log.error("  ⚠️  微信进程已退出—— 机器人已离线!")
                log.error("")
                try:
                    age = time.time() - self.bot.process.create_time()
                    log.error("  pid=%s，自进程创建约 %.1f 秒", self.bot.process.pid, age)
                except Exception:
                    pass
                log.error("  退出原因尚未确定，请结合 Windows 崩溃事件、转储及 patch_audit 日志。")
                log.error("  可疑位置是否成功排除，请查看本轮 patch_audit 的 skipped_crash_site 记录。")
                log.error("  请保存本轮日志；是否崩溃及根因不能仅由进程消失判断。")
                log.error("=" * 70)
                log.error("")
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetConsoleTitleW(
                        "微信进程已退出 --- 请检查崩溃事件和补丁日志")
                except Exception:
                    pass
            was_alive = alive


# ---------- 文件小工具 ----------

def attach_wxhook_logger(log_file, level="DEBUG"):
    """
    把 wxhook 的 loguru(SUCCESS/ERROR 原文)接到 bridge.log。

    报告 P1-2 的根因就在这:wxhook 用的是 loguru,和桥接的 logging 是两套;
    它的输出只写 stdout,于是"wechat version faked: ..."这句最关键的证据
    永远进不了文件日志。

    这里给 loguru 挂一个额外 sink 直接写文件:不经过 logging,所以不会
    影响 wxhook 自己的输出格式,也不会和根 logger 的 handler 重复。

    失败(没有 loguru / 权限问题)只告警,绝不影响启动。
    """
    if not log_file:
        return False
    try:
        from loguru import logger as _loguru
    except Exception:
        log.info("没有找到 loguru(可能是 wxhook 换了实现),wxhook 日志仍只写控制台")
        return False
    try:
        _loguru.add(str(log_file), level=level, enqueue=False,
                    encoding="utf-8", backtrace=False, diagnose=False,
                    # 注意:loguru 的 format 里 {…} 都是格式化字段。
                    # 想输出字面量的 WXHOOK 标记就得把它写死成普通文本,
                    # 别用 "{'WXHOOK':<8}" —— 那会被当成字段去查字典,
                    # 结果每个日志调用都抛 KeyError(填行日志里就能看到)。
                    format="{time:YYYY-MM-DD HH:mm:ss}  WXHOOK   | "
                           "{level} | {message}")
        return True
    except Exception:
        log.exception("挂载 wxhook(loguru)日志失败(忽略)")
        return False
