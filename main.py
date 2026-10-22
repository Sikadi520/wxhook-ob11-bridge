r"""
微信(小号) <-> wxhook <-> OneBot v11 <-> AstrBot

启动前:
  1. AstrBot 已经启动(它的 python 后端应监听 11229,即 aiocqhttp 适配器)
  2. 微信 3.9.5.81 已安装,且【没有】在运行(本脚本会自己启动它)
  3. 用【管理员】PowerShell 运行(要注入 DLL + 改微信进程内存)

运行:
  uv run --with wxhook --with pymem --with websockets python main.py

本脚本启动时会自动清理残留(不做就会撞端口):
  - 上次遗留的 WeChat.exe 进程(它注入的 DLL 占着 19001)
  - 所有 wxhook.json 端口状态文件

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

    # 4) 清掉所有 wxhook 端口状态文件,让端口分配从 19001 重新开始
    try:
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "cache" / "archive-v0"
        for p in base.rglob("wxhook.json"):
            try:
                p.unlink()
                log.info("已重置端口状态文件: %s", p)
            except Exception:
                pass
    except Exception:
        pass


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


def main():
    cfg_path = BASE / "config.json"
    if not cfg_path.exists():
        sys.exit(
            "\n缺少配置文件:{}\n"
            "请先复制一份模板再启动:\n"
            "    copy config.example.json config.json\n".format(cfg_path)
        )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")
    log_file = _add_file_log()

    log.info("=" * 66)
    log.info("微信(小号)  <->  wxhook  <->  OneBot v11  <->  AstrBot")
    log.info("AstrBot 地址   : %s", cfg.get("astrbot_ob_url"))
    log.info("版本伪装为     : %s", cfg.get("faked_version"))
    log.info("x64->x32 补丁  : %s", cfg.get("patch_x64_to_x32"))
    log.info("白名单         : %s", cfg.get("allowed_wxids") or "未启用(所有人可用)")
    if log_file:
        log.info("日志文件       : %s", log_file)
    log.info("=" * 66)

    _cleanup_leftovers(log)

    log.info("")
    log.info("wxhook 即将启动微信,请确认微信当前没有在运行...")
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
