r"""
微信(小号) <-> wxhook <-> OneBot v11 <-> AstrBot

启动前:
  1. AstrBot 已经启动(它的 aiocqhttp 适配器在监听;4.x 默认 6199,3.x 是 11229)
  2. 微信 3.9.5.81 已安装,且【没有】在运行(本脚本会自己启动它)
  3. 用【管理员】PowerShell 运行(要注入 DLL + 改微信进程内存)

运行:
  uv run --with wxhook --with pymem --with websockets python main.py

启动时会依次做这些事(都有日志):
  - 自检:是不是管理员 / AstrBot 端口到底在哪个 / faker.exe 还在不在
  - 配置归一化:faked_version 和 patch_x64_to_x32 必须同时开,只开一半会自动补齐
  - 清理残留:上次的 WeChat.exe、占用 18999 的桥接进程、wxhook 端口状态文件

停止:
  建议顺序:先 Ctrl+C,再【手动退出微信】,然后才能重新启动。
  忘了退出微信也没关系 —— 下次启动本脚本会自动清掉。
  注意:本脚本的 Ctrl+C 处理不会调用 wxhook 的 exit(),所以微信一般
  会留在后台继续运行,而不是被终止。
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def _cleanup_leftovers(log):
    """
    清掉上次运行留下的残留,否则下次启动会撞端口。

    根因:`uv run --with wxhook` 每次都会新建一个临时环境,因此每次都是一份
    全新的 wxhook.json —— 端口永远从 19001 开始试。如果上次的微信进程还活着
    (注入的 DLL 占着 19001),新实例就会报 WinError 10043(地址已被占用),
    接收服务器绑不上,消息全部丢失。
    """
    # 1) 结束残留的微信进程(只针对 3.9.x 的 WeChat.exe,不动 4.x 的 Weixin.exe)
    try:
        r = subprocess.run(["taskkill", "/F", "/IM", "WeChat.exe"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            log.warning("已结束残留的 WeChat.exe 进程(上次运行留下的)")
    except Exception:
        pass

    # 3) 释放接收端口 18999。
    #    上一个残留的桥接进程会占着它 —— 新版启动时 serve_forever 绑不上,
    #    报 WinError 10043,结果是【一条消息都收不到】,而发送侧看起来一切正常。
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        pids = set()
        for line in out.splitlines():
            if ":18999" in line and "LISTENING" in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        me = str(os.getpid())
        for pid in pids:
            if pid == me:
                continue
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True)
            log.warning("已结束占用 18999 的残留桥接进程 pid=%s", pid)
    except Exception:
        pass

    # 4) 清掉所有 wxhook 端口状态文件,让端口分配从 19001 重新开始。
    #    注意:不限于 uv 缓存目录 —— 用 .venv / 本地 site-packages 装的时候
    #    状态文件在别的地方,以前只扫 uv 缓存会漏(表现为"端口莫名其妙变 19002")。
    #    不存在的目录不会 delete,所以这里安全。
    try:
        from preflight import find_wxhook_dirs
        dirs = find_wxhook_dirs()
        if not dirs:
            log.info("未找到 wxhook 包,跳过端口状态清理(首次启动会由 uv 安装)")
        for d in dirs:
            state = d / "tools" / "wxhook.json"
            if not state.exists():
                continue
            state.unlink()
            log.info("已重置端口状态文件: %s", state)
    except Exception:
        log.exception("清理 wxhook 端口状态失败(忽略,继续启动)")


def _add_file_log():
    """
    再写一份日志到 bridge/logs/bridge.log。

    为什么需要:控制台是【管理员】窗口,普通权限的工具读不到它的缓冲区
    (AttachConsole 报拒绝访问),出问题只能靠截图,连 msgId 都常被截断。
    落一份文件后,排查时直接读文件即可。
    """
    try:
        from logging.handlers import RotatingFileHandler

        log_dir = BASE / "logs"
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "bridge.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(name)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)
        return log_dir / "bridge.log"
    except Exception:
        logging.getLogger("main").exception("挂文件日志失败(忽略,继续用控制台)")
        return None


def _doctor(cfg: dict = None) -> int:
    """
    `python main.py --doctor`:一次把“能不能跑”的现场全打印出来。

    为什么单独一个入口:排查时最想要的是“一眼看完全部事实”,而不是
    启动、等 30 秒、看一句 unknown error。这个模式**什么都不改、什么都不起**,
    没有 config.json 也能跑。

    返回 0 = 全绿,1 = 有阻塞项(可以拿去给 start.bat 判断)。
    """
    import preflight
    from options import normalize
    import wechat_version as wv

    cfg = dict(cfg or {})
    problems, warnings = [], []

    print("=" * 70)
    print("  wxhook-ob11-bridge 自检 (--doctor)")
    print("=" * 70)

    # 1) 微信版本 —— 2026-10-23 就是因为没人查这一项而卡了很久
    info = wv.installed_wechat_version()
    traces = wv.update_traces(Path(info["install_dir"]) if info["install_dir"] else None)
    ver = info.get("version") or ""
    ok = ver == wv.SUPPORTED_VERSION
    print("\n[微信版本]")
    print("  已安装     : %s (来源: %s)" % (ver or "判定不了", info.get("source") or "-"))
    print("  需要       : %s" % wv.SUPPORTED_VERSION)
    print("  安装目录   : %s" % (info.get("install_dir") or "(读不到)"))
    print("  版本子目录 : %s" % (", ".join(d["name"] for d in info["dirs"]) or "(无)"))
    print("  注册表原值 : %s -> %s" % (info.get("reg_version"), info.get("reg_decoded") or "-"))
    print("  FileVersion: %s" % (info.get("exe_version") or "-"))
    if not ok:
        problems.append("微信版本不对:%s(需要 %s)" % (ver or "判定不了", wv.SUPPORTED_VERSION))

    if traces.get("tmp_dirs") or traces.get("patch_dir") or \
            (traces.get("need_update_type") not in (None, 0)):
        print("  待升级痕迹 :")
        if traces.get("tmp_dirs"):
            print("    暂存目录 : %s" % ", ".join(traces["tmp_dirs"]))
        if traces.get("patch_dir"):
            print("    patch    : %s" % traces["patch_dir"])
        print("    NeedUpdateType: %s" % traces.get("need_update_type"))
        warnings.append("微信可能正在升级;升级后 wxhook 就注入不了了")

    # 2) 管理员
    print("\n[权限]")
    admin = preflight.is_admin()
    print("  管理员     : %s" % ("是" if admin else "**否**"))
    if not admin:
        problems.append("当前不是管理员:注入 DLL 必须提权")

    # 3) wxhook 三件套
    print("\n[wxhook 依赖]")
    dirs = preflight.find_wxhook_dirs()
    if not dirs:
        print("  没找到 wxhook 包")
        problems.append("没找到 wxhook 包:先跑一次 uv run --with wxhook python -c \"import wxhook\"")
    else:
        best = dirs[0]
        for d in dirs:
            if (d / "tools" / "faker.exe").is_file():
                best = d
                break
        print("  位置       : %s" % best)
        tools = preflight.inspect_tools(best)
        for name, item in tools["files"].items():
            state = "OK" if item.get("sha256") else ("缺失" if not item.get("exists")
                                                    else "**读不出来**")
            print("  %-16s %-10s %s" % (name, state,
                                        (item.get("sha256") or "")[:16]))
        if not tools["ok"]:
            problems.append("wxhook 的文件不完整:" + ";".join(tools["problems"]))

    # 4) 微信进程与残留
    pids = preflight.running_wechat_pids()
    print("\n[进程]")
    print("  WeChat.exe : %s" % (pids or "没在运行"))
    if pids:
        warnings.append("微信正在运行,启动前会被结束再接管")

    # 5) AstrBot 端口
    print("\n[AstrBot]")
    raw = cfg.get("astrbot_ob_url") or "ws://127.0.0.1/ws"
    resolved, note = preflight.resolve_ob_url(raw)
    print("  配置       : %s" % raw)
    print("  实际使用   : %s" % resolved)
    print("  说明       : %s" % note)
    if "没有" in (note or ""):
        problems.append("连不上 AstrBot:%s" % note)

    # 6) 门禁组合
    print("\n[登录门禁]")
    opt = normalize(cfg if cfg else {"faked_version": "", "patch_x64_to_x32": False})
    print("  伪装版本   : %s" % (opt["faked_version"] or "(未开启)"))
    print("  x64→x32    : %s" % opt["patch_x64_to_x32"])
    if not (opt["faked_version"] and opt["patch_x64_to_x32"]):
        warnings.append("伪装与补丁没同时开,大概率提示主客户端版本过低")

    # 汇总
    print("\n" + "=" * 70)
    for w in warnings:
        print("  [警告] %s" % w)
    for p in problems:
        print("  [阻塞] %s" % p)
    if problems:
        print("\n结论:有 %d 个阻塞项,先处理完再启动。" % len(problems))
        if not ok:
            print()
            print(wv.recover_hint(info.get("install_dir") or ""))
        return 1
    print("  结论:全部通过,可以启动。")
    print("=" * 70)
    return 0


def main():
    if "--doctor" in sys.argv or "-d" in sys.argv:
        # 没有 config.json 也要能跑 —— 排查时往往就是在“刚装完”那一刻
        cfg = {}
        cfg_path = BASE / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                print("config.json 解析失败(忽略,只做环境检查):%s" % exc)
        logging.basicConfig(level=logging.WARNING,
                            format="%(levelname)-7s | %(message)s")
        return _doctor(cfg)

    cfg_path = BASE / "config.json"
    if not cfg_path.exists():
        sys.exit(
            "\n缺少配置文件:{}\n"
            "请先复制一份模板再启动:\n"
            "    copy config.example.json config.json\n".format(cfg_path)
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        sys.exit("\n配置文件解析失败:%s\n  文件:%s\n  常见原因:少逗号、多了注释、"
                 "用了中文引号。可以拿 config.example.json 对照修正。\n"
                 % (exc, cfg_path))

    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")
    log_file = _add_file_log()

    # ---------- 1) 配置归一化:伪装 + 补丁必须同时开 ----------
    from options import normalize
    opt = normalize(cfg, log)

    log.info("=" * 66)
    log.info("微信(小号)  <->  wxhook  <->  OneBot v11  <->  AstrBot")
    log.info("AstrBot 地址   : %s", cfg.get("astrbot_ob_url"))
    log.info("版本伪装为     : %s", cfg.get("faked_version") or "(未开启)")
    log.info("x64->x32 补丁  : %s(上限 %s 处)",
             cfg.get("patch_x64_to_x32"), cfg.get("patch_max_sites"))
    log.info("白名单         : %s", cfg.get("allowed_wxids") or "未启用(所有人可用)")
    if log_file:
        log.info("日志文件       : %s", log_file)
    log.info("=" * 66)

    for msg in opt["adjusted"]:
        log.warning("[配置自动修正] %s", msg)
    for msg in opt["notes"]:
        log.info("[配置] %s", msg)
    if opt["adjusted"]:
        log.info("[配置] 要还原成你自己写的值,把这一段写回 config.json:")
        log.info("[配置]   %s", opt["revert"])

    # ---------- 2) 启动前自检 ----------
    from preflight import log_report, run as preflight_run
    report = preflight_run(cfg, log)
    log_report(report, log)
    if report.get("ob_url"):
        # 用探测到的实际地址(配置没写端口时,这里会把 6199 补上)
        cfg["astrbot_ob_url"] = report["ob_url"]

    # ---------- 3) wxhook(loguru)日志也进 bridge.log ----------
    from wechat import attach_wxhook_logger
    attach_wxhook_logger(log_file)

    # ---------- 4) 把 wxhook 自己的不轮转日志复制一份进来 ----------
    # 2026-10-23:项目目录被整体覆盖后 logs/ 没了,而那天的成功日志只存在
    # wxhook 的 tools\logs\daily_*.txt 里。两份互相兵底。
    try:
        import wxhook_logs
        from preflight import find_wxhook_dirs
        wx_dirs = find_wxhook_dirs(limit=1)
        if wx_dirs and log_file:
            wxhook_logs.archive(wx_dirs[0], log_file.parent)
    except Exception:
        log.exception("归档 wxhook 日志失败(忽略)")

    _cleanup_leftovers(log)

    log.info("")
    log.info("wxhook 即将启动微信,请确认微信当前没有在运行...")
    if opt["patch_x64_to_x32"]:
        log.info("登录门禁:扫码 → 先不确认 → 再扫一次 → 点确认(两段式)")
    log.info("")

    from bridge import Bridge

    b = Bridge(cfg)
    b.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.warning("")
        log.warning("收到 Ctrl+C。若要重新启动,建议先手动退出微信,")
        log.warning("否则残留的注入进程会占住 19001(不过下次启动会自动清理)。")


if __name__ == "__main__":
    sys.exit(main())
