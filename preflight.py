# -*- coding: utf-8 -*-
"""
启动前自检 —— 把"能提前发现的问题"全部挪到启动最前面。

为什么单独一个文件:这里面的每条检查都对应一次真实踩过的坑,而它们
**都不该等到微信已经起来了才发现**:

    坑                          检查
    ------------------------    --------------------------------------
    AstrBot 端口默认值变了      端口探测(6199 / 11229 / 自定义)
    忘了用管理员跑              IsUserAnAdmin() 直接判定
    faker.exe 被杀软删了        是否存在 / 是否可读 / SHA256 / 排除状态
    上次的微信还在跑            WeChat.exe 进程 + wxhook.json 里残留的 pid
    门禁一直提示"版本过低"      伪装和补丁是不是同时开了(options.py)
    wxhook 端口被上次的占用     wxhook.json / config.ini 的端口现状

本模块**只依赖标准库**:它要在 webhook / websockets / pymem 安装之前就能跑,
所以不能 import 它们。同样地,它不 import wxhook —— 只按路径去看文件。
"""

import hashlib
import json
import logging
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("preflight")

# AstrBot 的 aiocqhttp 适配器端口。**按顺序**尝试,第一个在监听的就用它。
#   11229 = AstrBot 3.x 的默认值(老文档/老教程里到处都是)
#   6199  = AstrBot 4.x 的默认值(2026-10 实测 4.29.1)
# 顺序上仍然先试 11229:老用户现有部署不会因为我们改默认值而改变行为,
# 而新用户(没配端口)会自动落到 6199 上。
DEFAULT_PORTS = (11229, 6199)

# 只认 3.9.5.81:wxhook 0.0.10 在源码里把它写死了(core.py 的 self.version)。
# 4.x 的 Weixin.exe 走的是另一套适配层,本仓库没验证过。
SUPPORTED_WECHAT_VERSION = "3.9.5.81"

# wxhook 自带的三个文件。少任何一个都起不来,faker.exe 还最容易被杀软删。
WXHOOK_TOOLS = ("faker.exe", "start-wechat.exe", "wxhook.dll")


# --------------------------------------------------------------------------
# 端口
# --------------------------------------------------------------------------

def port_in_use(port, host="127.0.0.1", timeout=0.25) -> bool:
    """有东西在 listen 就算 True(连上就立刻断开,不影响对方)。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _candidate_ports(url: str):
    """
    把一个 ws:// 地址拆成 (host, [(port, 来源说明), ...])。

    没指定端口时按 DEFAULT_PORTS 顺序试;指定了就只试那一个 —— 用户明确写了
    端口却探测别的端口,会让日志和实际连接对不上,排障时更糊涂。
    """
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or "127.0.0.1"
    except ValueError:
        return "127.0.0.1", []
    if port:
        return host, [(port, "配置里指定的端口")]
    # 写成显式循环:别用推导式里的变量名,那个 p 会遮蔽后面的 probe 参数
    candidates = []
    for candidate in DEFAULT_PORTS:
        why = "AstrBot 3.x 默认" if candidate == 11229 else "AstrBot 4.x 默认"
        candidates.append((candidate, why))
    return host, candidates


def port_open(port, host="127.0.0.1", probe=None) -> bool:
    """
    探测一个端口是否在监听。

    ``probe`` 是关键字参数而不是位置参数 —— 这样两种写法都能用:
      port_open(p, probe=port_in_use)          # 真探测
      port_open(p, probe=lambda p, **k: ...)   # 单测桩(只要形参叫 port 就行)
    第三方的 mock / partial 经常按位置传参,位置传错的坑(把 host 当 port)
    在这个项目里真的踩过一次。
    """
    fn = probe or port_in_use
    try:
        return bool(fn(port, host=host))
    except TypeError:
        # 桩只接受一个参数时兜底
        return bool(fn(port))


def resolve_ob_url(url: str, probe=None):
    """
    决定最终要连的地址。

    返回 ``(可用地址, 说明)``:

    - 配置里写了端口,而且那个端口在监听  -> 原样返回,直连
    - 配置里没写端口,探测到 6199 在监听  -> 返回带 6199 的地址(这是本报告的核心修复)
    - 两个端口都在监听                    -> 先试 11229(再说 6199)
    - 哪个都没在监听                      -> 原样返回,由 ob11 报可读的错误
    """
    if not url:
        return url, "配置里没有 astrbot_ob_url"

    host, candidates = _candidate_ports(url)
    if not candidates:
        return url, "地址解析失败"

    live = [(candidate, why) for candidate, why in candidates
            if port_open(candidate, host, probe)]
    if not live:
        tried = " / ".join(str(p) for p, _ in candidates)
        return url, ("%s 上没有任何端口在监听(试过 %s)" % (host, tried))

    port, why = live[0]
    if parts_has_port(url):
        return url, "端口 %d 在监听(%s)" % (port, why)

    resolved = _with_port(url, port)
    note = "自动选中端口 %d(%s)" % (port, why)
    if len(live) > 1:
        note += ";另外 %s 也在监听" % ", ".join(str(p) for p, _ in live[1:])
    return resolved, note


def parts_has_port(url: str) -> bool:
    try:
        return bool(urlsplit(url).port)
    except ValueError:
        return True        # 解析不了就当用户写了,别擅自改

def _with_port(url: str, port: int) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname or "127.0.0.1"
    if ":" in netloc and not netloc.startswith("["):
        netloc = "[%s]" % netloc                      # IPv6
    return parts._replace(netloc="%s:%d" % (netloc, port)).geturl()


# --------------------------------------------------------------------------
# 权限
# --------------------------------------------------------------------------

def is_admin() -> bool:
    """
    当前进程是不是管理员。

    注入 DLL、改微信进程内存都要提权。以前只在报错里含糊地提一句,用户
    往往要等到"版本过低"或者"命中 0 处"才回头怀疑权限。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------
# wxhook 包与 faker.exe
# --------------------------------------------------------------------------

def _uv_archive_roots():
    """uv 的解包缓存目录(每装一次就多一个 archive-v0/<hash>)。"""
    roots = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        roots.append(Path(la) / "uv" / "cache" / "archive-v0")
    roots.append(Path.home() / ".cache" / "uv" / "archive-v0")
    return roots


def find_wxhook_dirs(limit=40):
    """找出所有装着 wxhook 的 site-packages 目录(最新的排在前面)。"""
    found = []
    try:
        import wxhook  # noqa: F401  —— 只是想知道它装在哪
        found.append(Path(sys.modules["wxhook"].__file__).resolve().parent)
    except Exception:
        pass

    for root in _uv_archive_roots():
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("wxhook"):
                if (p / "core.py").is_file() and p not in found:
                    found.append(p)
        except OSError:
            continue

    # 只保留当前环境真正能被 import 到的那份。
    # 为什么:uv 缓存里常存着好几份 wxhook(不同安装时的解包目录),
    # 挨个猜测"哪份在用"会盯错文件 —— 比如盯着一个残留副本报"faker 不存在",
    # 而实际加载的那份好好的。本模块只依赖标准库,所以用 importlib 的
    # find_spec(不执行 wxhook 自己的任何代码)来问一个权威答案。
    if not found:
        try:
            import importlib.util
            spec = importlib.util.find_spec("wxhook")
            if spec and spec.origin:
                found.append(Path(spec.origin).resolve().parent)
        except Exception:
            pass

    # 有 tools 的优先,其次按修改时间倒序
    found.sort(key=lambda p: ((p / "tools").is_dir(), _mtime(p)), reverse=True)
    return found[:limit]


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def file_sha256(path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def inspect_tools(wxhook_dir: Path) -> dict:
    """
    检查 tools/ 下的三个文件。

    只读 checksum:**绝不执行 faker.exe**,也不会去"修"它 —— 这里只负责说清楚
    它到底在不在、读不读得到、长什么样,让用户自己决定加排除还是重装。
    """
    tools = Path(wxhook_dir) / "tools"
    out = {"dir": str(tools), "files": {}, "ok": True, "problems": []}
    for name in WXHOOK_TOOLS:
        path = tools / name
        item = {"path": str(path), "exists": path.exists()}
        if item["exists"]:
            try:
                stat = path.stat()
                item["size"] = stat.st_size
                item["sha256"] = file_sha256(path)
            except OSError as exc:
                # Defender 查杀后留下的就是这种情况:文件在、连读都被拦
                item["readable"] = False
                item["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["files"][name] = item

    if not out["files"].get("faker.exe", {}).get("exists"):
        out["ok"] = False
        out["problems"].append("faker.exe 不存在(极可能被 Windows Defender 查杀)")
    elif not out["files"]["faker.exe"].get("sha256"):
        out["ok"] = False
        out["problems"].append("faker.exe 存在但读不出来(被实时保护拦了)")

    for name in ("start-wechat.exe", "wxhook.dll"):
        if not out["files"].get(name, {}).get("exists"):
            out["ok"] = False
            out["problems"].append("%s 不存在" % name)
    return out


DEFENDER_HINT = (
    "处理办法(管理员 PowerShell),然后重装依赖 uv cache clean wxhook 后重跑:\n"
    '    Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\\uv\\cache"\n'
    '    Add-MpPreference -ExclusionProcess "faker.exe"\n'
    '    Add-MpPreference -ExclusionProcess "start-wechat.exe"'
)


# --------------------------------------------------------------------------
# 微信进程 / wxhook 端口状态
# --------------------------------------------------------------------------

def running_wechat_pids():
    """
    正在跑的 WeChat.exe(3.9.x)。

    故意不区分"是谁启动的":本脚本启动前要求微信没在运行,跑了就清掉。
    4.x 的 Weixin.exe 不在此列 —— 那是另一套进程,桥接不碰。
    """
    if os.name != "nt":
        return []
    try:
        import subprocess
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WeChat.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    pids = []
    for line in r.stdout.splitlines():
        m = re.match(r'\s*"([^"]+)","(\d+)"', line)
        if m and m.group(1).lower() == "wechat.exe":
            pids.append(int(m.group(2)))
    return pids


def read_port_state(wxhook_dir: Path) -> dict:
    """
    读 wxhook 自己的端口状态文件。

    tools/wxhook.json 是它分配端口的账本:
        increase_remote_port = 上次用掉的 remote 端口(下一个是 +1)
        wechat[]             = [{pid, remote_port, server_port}]
        server_port          = 19000 - (remote_port - 19000),即 18999 / 18998 ...

    这些端口是**全局的**:不管是不是本进程占的,只要上一个进程还活着,
    新实例就会撞上 WinError 10043(地址被占用),而表现是"一条消息都收不到"。
    """
    info = {"path": str(Path(wxhook_dir) / "tools" / "wxhook.json"),
            "exists": False, "next_remote_port": None, "next_server_port": None,
            "entries": [], "stale_pids": []}
    path = Path(wxhook_dir) / "tools" / "wxhook.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return info
    except Exception as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
        return info

    info["exists"] = True
    try:
        nxt = int(data.get("increase_remote_port", 19000)) + 1
    except (TypeError, ValueError):
        nxt = 19001
    info["next_remote_port"] = nxt
    info["next_server_port"] = 19000 - (nxt - 19000)
    info["entries"] = list(data.get("wechat") or [])

    live = set(running_wechat_pids())
    for item in info["entries"]:
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid not in live:
            info["stale_pids"].append(pid)
    return info


def reset_port_state(wxhook_dir: Path, kill_pids=()) -> list:
    """
    删掉 wxhook.json(必要时先杀掉残留的微信进程),让端口从 19001 重新分配。

    返回做了什么事的说明列表,方便写日志。
    """
    from pathlib import Path as _P
    done = []
    path = _P(wxhook_dir) / "tools" / "wxhook.json"
    for pid in kill_pids:
        done.append("结束残留微信进程 pid=%s" % pid)
    try:
        path.unlink()
        done.append("已删除端口状态文件 %s" % path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        done.append("删除端口状态文件失败:%s" % exc)
    return done


# --------------------------------------------------------------------------
# Defender 排除状态(只读)
# --------------------------------------------------------------------------

def defender_status():
    """
    读一下 Defender 有没有把 uv 缓存 / faker.exe 排除掉。

    只在 PowerShell 可用时查;查不到就返回 None(不影响启动)。
    """
    if os.name != "nt":
        return None
    try:
        import subprocess
        script = (
            "$p = Get-MpPreference; "
            "@{paths=$p.ExclusionPath; procs=$p.ExclusionProcess} | ConvertTo-Json -Compress"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        return {"paths": data.get("paths") or [], "procs": data.get("procs") or []}
    except Exception:
        return None


def has_defender_exclusion(status, wxhook_dir) -> bool:
    """uv 缓存目录或 faker.exe / start-wechat.exe 至少有一项在排除列表里。"""
    if not status:
        return False
    paths = [str(p).lower() for p in status.get("paths") or []]
    procs = [str(p).lower() for p in status.get("procs") or []]
    if any("uv" in p and "cache" in p for p in paths):
        return True
    tools = str(Path(wxhook_dir) / "tools").lower()
    if any(tools.startswith(p.rstrip("\\")) for p in paths if p.strip()):
        return True
    return any(n in procs for n in ("faker.exe", "start-wechat.exe"))


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def run(cfg: dict, log_=None, probe=None) -> dict:
    """
    跑一遍完整自检,返回一份可以直接打印/落日志的报告。

    不修改任何配置,不启动任何东西 —— 只读 + 探测。

    ``probe`` 默认在调用时解析 port_in_use,而不是写成默认参数值:
    默认参数在函数定义时就绑定函数对象了,以后无论怎么改(或单测里
    patch)module 上的 port_in_use,这里拿到的还是旧那个 —— 这是个
    真实的坑,排障时会表现为"探测结果和配置对不上"。
    """
    lg = log_ or log
    # 同样别写成默认参数:port_in_use 可能被替换(单测 / 定制探测)
    probe = probe or globals()["port_in_use"]
    report = {"ok": True, "problems": [], "warnings": [], "admin": is_admin()}

    # 1) 权限
    if not report["admin"]:
        report["warnings"].append(
            "当前不是管理员:注入 DLL 会失败。请右键 start.bat → 以管理员身份运行")

    # 2) AstrBot 端口
    raw_url = cfg.get("astrbot_ob_url") or ""
    resolved, note = resolve_ob_url(raw_url, probe)
    report["ob_url_raw"] = raw_url
    report["ob_url"] = resolved
    report["ob_note"] = note
    if resolved == raw_url and "没有" in note:
        report["problems"].append(
            "连不上 AstrBot:%s。检查 AstrBot 是否在运行、aiocqhttp 适配器端口是多少"
            "(WebUI → 平台 → aiocqhttp),必要时直接改 astrbot_ob_url" % note)
    else:
        lg.info("AstrBot 地址:%s(%s)", resolved, note)

    # 3) wxhook 包与 faker.exe
    dirs = find_wxhook_dirs()
    report["wxhook_dirs"] = [str(d) for d in dirs]
    if not dirs:
        report["problems"].append(
            "没找到 wxhook 包:先跑一次 uv run --with wxhook python -c \"import wxhook\"")
    else:
        # 优先选含 tools 且 faker.exe 在的那个
        best = dirs[0]
        for d in dirs:
            if (d / "tools" / "faker.exe").is_file():
                best = d
                break
        report["wxhook_dir"] = str(best)
        tools = inspect_tools(best)
        report["tools"] = tools
        for name, item in tools["files"].items():
            if item.get("sha256"):
                lg.info("  %-16s %8d 字节  sha256=%s", name, item.get("size", 0),
                        item["sha256"][:16] + "…")
        if not tools["ok"]:
            report["problems"].append(
                "wxhook 的文件不完整:%s\n%s" % (";".join(tools["problems"]), DEFENDER_HINT))
            # 试着给出可以直接复制的排除路径
            lg.warning("可排除的路径:%s", best)

        # 4) 端口状态
        state = read_port_state(best)
        report["port_state"] = state
        if state.get("exists"):
            lg.info("wxhook 端口状态:下一轮端口 remote=%s / server=%s,历史条目 %d 条",
                    state.get("next_remote_port"), state.get("next_server_port"),
                    len(state.get("entries") or []))
            # “端口被占着但没人在听”= 上一个实例死在了半路。
            # 真实现场（2026-10-23）wxhook 报的是 Exception: port occupied:19001,
            # 而且是在 bot.run() 之前抛的 —— 光重启不够,得把真相提前抬出来。
            for entry in state.get("entries") or []:
                port = entry.get("remote_port") or entry.get("server_port")
                if port and not port_open(port, probe=probe):
                    report["warnings"].append(
                        "wxhook 上次用过的端口 %s 现在没人在听(上个进程异常退出);"
                        "启动时会重置端口状态并从 19001 重新分配" % port)
                    break
            if state.get("stale_pids"):
                report["warnings"].append(
                    "wxhook.json 里登记着已经退出的微信进程 pid=%s;启动时会重置端口状态"
                    % state["stale_pids"])

    # 5) 微信进程
    pids = running_wechat_pids()
    report["wechat_pids"] = pids
    # 同一个事只报一次:“微信在跑”既是“启动前要清”又是“端口被占”的因。
    # 以前会打两三条意思差不多的警告,反而把真正的问题(比如端口填错)淹了。
    if pids and not report["problems"]:
        report["warnings"].append(
            "微信已经在运行(pid=%s)。桥接启动时会先结束它再接管(不会丢会话)" % pids)
    elif pids:
        lg.info("微信已经在运行(pid=%s),启动时会先结束它再接管", pids)

    return report


def log_report(report: dict, lg=None):
    """把自检结果打进日志,该醒目的醒目。"""
    lg = lg or log
    for msg in report.get("warnings") or []:
        lg.warning("[自检] %s", msg)
    for msg in report.get("problems") or []:
        lg.error("[自检] %s", msg)
