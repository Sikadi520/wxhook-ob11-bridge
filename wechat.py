"""
微信侧:wxhook 的生命周期与收发

wxhook 0.0.10 只支持微信 3.9.5.81(源码里写死)。
Bot(...) 构造时会自动:启动微信 -> 注入 wxhook.dll -> 伪装版本号。

本模块额外自动补上你那一步手动的 wechat_patcher.py(x64 -> x32),
这样启动就只剩一条命令,不用再开两个窗口。
"""

import base64
import json
import logging
import os
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
                if self.cfg.get("patch_x64_to_x32", True):
                    # Bot's constructor invokes this callback before self.bot is assigned.
                    self._patch_x64_to_x32(bot)

            def on_login(bot, event):
                try:
                    info = bot.get_self_info()
                    self.self_wxid = getattr(info, "wxid", "") or self.self_wxid
                    log.info("已登录: %s (%s)",
                             getattr(info, "name", "?"), self.self_wxid)
                    self._write_ready_file(info)
                except Exception:
                    log.exception("获取自身账号信息失败")

            faked = self.cfg.get("faked_version") or None
            log.info("构造 wxhook Bot(版本伪装 -> %s)", faked)
            self.bot = Bot(faked_version=faked, on_start=on_start, on_login=on_login)
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
        except Exception:
            log.exception("微信侧启动失败(微信是否已安装 3.9.5.81?是否已在运行?)")
            self.ready.clear()

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
            from patch_audit import run_patch

            # Only patch the process this Bot owns, not an arbitrary WeChat.exe.
            pm = pymem.Pymem(bot.process.pid)
            run_patch(pm, self.cfg, log,
                      Path(__file__).resolve().parent / "patch_audit",
                      pymem.memory.virtual_query)
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
