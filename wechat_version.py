# -*- coding: utf-8 -*-
"""
微信版本与「自动升级」检测。

## 为什么必须单独成文件

wxhook 0.0.10 的 `core.py` 把支持版本写死成 `3.9.5.81`。微信一旦在后台把
自己升到 3.9.12.x(2026-10-23 实测:同一天两次),`start-wechat.exe` 注入失败
只回一个 `1,unknown error`,wxhook 原样抛出,用户看到的只有:

    Exception: unknown error

版本对不对是**整个项目能用不能用**的前提,却从来没人检查过
(`SUPPORTED_WECHAT_VERSION` 定义了但零引用)。这个文件补上这件事。

## 关键实测结论(2026-10-23)

1. **升级不需要用户确认、不需要重启电脑**:客户端运行中就能暂存 + 顶替 +
   自我重启。重装 3.9.5.81 而不做封锁,大约只撑 5–12 分钟。
2. **"防升级三步"(取消勾选 / 改名根目录 WeChatUpdate.exe / NeedUpdateType=0)
   全部无效或路径错**:
   - `NeedUpdateType` 升级前本来就是 0,它是**状态字段不是开关**;
   - 根目录没有 `WeChatUpdate.exe`,真实文件在 `[版本号]\\` 子目录里,
     真正被执行的是 XPlugin 和 `C:\\ProgramData\\Tencent\\WeChat` 下的副本。
3. **安装路径不固定**:本仓库维护者机器上是 `D:\\WeChat`,报告机上是
   `C:\\WeChat`。所有路径必须从注册表 `InstallPath` 推导,不能写死。

## Windows 上的两个实现陷阱(都踩过)

* **方括号路径必须按字面量处理**:`[3.9.5.81]` 在 PowerShell 通配符语法里
  是字符类,`Get-ChildItem 'C:\\WeChat\\[3.9.5.81]'` 会**静默返回 0 条**;
  必须用 `-LiteralPath`。Python 的 `os.scandir` / `Path.iterdir` 天然按字面
  处理,所以这里用 Python 实现而不是调 PowerShell。
* **32/64 位注册表视图**:微信是 32 位程序,`HKCU\\Software\\Tencent\\WeChat`
  在 64 位进程里读可能落到 WOW64 重定向。这里通过 winreg 的
  `KEY_WOW64_32KEY` 与默认视图各读一次,谁有结果用谁。
"""

import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("wechat_version")

# wxhook 0.0.10 写死的版本。preflight.SUPPORTED_WECHAT_VERSION 与本值必须一致,
# 单测会校验,避免两处漂移。
SUPPORTED_VERSION = "3.9.5.81"

# 注册表里版本号的编码:0xAABBCCDD 是原始数值,但微信实际写的是
#   0x63 09 05 51  ->  3.9.5.81
# 也就是每个字节按十六进制读出后,**第一个字节减去 0x60**(0x63→3)。
# 实测:
#   1661535569 = 0x63090551 -> 3.9.5.81
#   1661537335 = 0x63090C37 -> 3.9.12.55
#   1661537331 = 0x63090C33 -> 3.9.12.51(faker 的伪装版本,见下)
_VERSION_FIRST_BYTE_OFFSET = 0x60


def decode_version(value) -> str:
    """
    把注册表 DWORD 解成 "3.9.5.81"。

    解不出来就返回空串 —— **绝不猜**。猜错会让自检报一个假版本,
    比没有检测更糟。
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    a = (value >> 24) & 0xFF
    b = (value >> 16) & 0xFF
    c = (value >> 8) & 0xFF
    d = value & 0xFF
    major = a - _VERSION_FIRST_BYTE_OFFSET
    if major < 0:
        return ""
    return "%d.%d.%d.%d" % (major, b, c, d)


def encode_version(text: str) -> int:
    """把 "3.9.5.81" 编回注册表 DWORD(单测与自查命令用)。"""
    parts = [int(p) for p in str(text).strip().split(".")]
    if len(parts) != 4:
        raise ValueError("版本号要四段: %r" % (text,))
    a, b, c, d = parts
    return (((a + _VERSION_FIRST_BYTE_OFFSET) & 0xFF) << 24 |
            (b & 0xFF) << 16 | (c & 0xFF) << 8 | (d & 0xFF))


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

REG_SUBKEY = r"Software\Tencent\WeChat"


def _open_key(view: str):
    """开 HKCU\\...\\WeChat,按指定的注册表视图。失败抛 OSError。"""
    import winreg
    access = winreg.KEY_READ
    if view == "32" and hasattr(winreg, "KEY_WOW64_32KEY"):
        access |= winreg.KEY_WOW64_32KEY
    elif view == "64" and hasattr(winreg, "KEY_WOW64_64KEY"):
        access |= winreg.KEY_WOW64_64KEY
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, access)


def read_registry(value_name: str):
    """
    读 HKCU 下的一个值(DWORD 或字符串)。

    HKCU 里没有微信的键(比如装到别的用户)时返回 None,不抛异常 ——
    自检里"读不到"是一种正常状态,由调用方决定怎么说。
    """
    try:
        import winreg
    except ImportError:
        return None
    for view in ("32", "64"):
        try:
            with _open_key(view) as key:
                return winreg.QueryValueEx(key, value_name)[0]
        except (OSError, ImportError, AttributeError):
            continue
    return None


def read_registry_str(value_name: str) -> str:
    """读 HKCU 下的一个字符串值(InstallPath 用)。"""
    value = read_registry(value_name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


# --------------------------------------------------------------------------
# 文件版本 / 安装布局
# --------------------------------------------------------------------------

def _file_version_windows(path: Path) -> str:
    """
    读 exe 的 FileVersion(注册表失效时的兜底)。

    用 PowerShell 是为了不引入 pywin32 依赖;单测会把这一层替换掉。
    注意 **-LiteralPath**:版本子目录带方括号,通配符写法会静默返回空。
    """
    if os.name != "nt":
        return ""
    try:
        script = ("(Get-Item -LiteralPath '%s').VersionInfo.FileVersion" % str(path))
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script],
                           capture_output=True, text=True, timeout=15)
        text = (r.stdout or "").strip()
        m = re.match(r"(\d+\.\d+\.\d+\.\d+)", text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def find_version_dirs(install_dir: Path):
    """
    列出 `[x.y.z]` 版本子目录与 `[x.y.z]_tmp` 暂存目录。

    两种名字都要认:
        [3.9.5.81]          已生效的版本目录
        [3.9.12.55]_tmp     暂存目录（**不以下方括号结尾**）

    一开始只判断“以 [ 开头且以 ] 结尾”,结果 _tmp 目录全部漏掉 ——
    而它恰好是“升级正在进行”最重要的信号。单测里被抓到了。

    **必须用字面量方式列目录**(Python 的 os.scandir 天然如此):
    方括号在通配符语法里是字符类,写成 glob 会静默返回空。
    """
    found = []
    if not install_dir or not Path(install_dir).is_dir():
        return found
    try:
        with os.scandir(str(install_dir)) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir:
                    continue
                name = entry.name
                if not name.startswith("["):
                    continue
                # [x.y.z]  或  [x.y.z]_tmp
                inner = name[1:]
                is_tmp = inner.endswith("]_tmp")
                if is_tmp:
                    inner = inner[:-len("]_tmp")]
                elif inner.endswith("]"):
                    inner = inner[:-1]
                else:
                    continue          # [ 开头但既不是版本目录也不是暂存目录
                if not inner:
                    continue
                found.append({"name": name, "version": inner, "tmp": is_tmp,
                              "path": entry.path})
    except OSError:
        pass
    found.sort(key=lambda d: (d["tmp"], _vkey(d["version"])))
    return found


def _vkey(text):
    try:
        return tuple(int(p) for p in str(text).split("."))
    except (TypeError, ValueError):
        return (0,)


def installed_wechat_version(install_dir: Path = None) -> dict:
    """
    判定"这台机器上装的微信到底是哪个版本"。

    返回 dict:
        version       : "3.9.5.81" / "3.9.12.55" / "" (判定不了)
        source        : registry / fileversion / dirname / ""
        install_dir   : 安装根目录(可能为空)
        dirs          : 版本子目录(含 _tmp)
        reg_version   : 注册表原始 DWORD
        exe_version   : FileVersion(如果读了)

    判定顺序:注册表 > WeChat.exe 的 FileVersion > 唯一的版本子目录名。
    三者不一致时**以能反映"实际会被拉起的是哪个"为准**:注册表为准,
    但把不一致本身也报出来 —— 自相矛盾往往正是"升级正在进行"的信号。
    """
    reg_raw = read_registry("Version")
    reg_version = decode_version(reg_raw) if reg_raw is not None else ""

    if install_dir is None:
        install_dir = Path(read_registry_str("InstallPath") or "")
    install_dir = Path(install_dir) if install_dir else None

    dirs = find_version_dirs(install_dir)

    exe_version = ""
    exe_path = None
    if install_dir:
        for name in ("WeChat.exe", "Weixin.exe"):
            candidate = install_dir / name
            if candidate.is_file():
                exe_path = candidate
                break
    if exe_path is not None:
        exe_version = _file_version_windows(exe_path)
    if not exe_version and dirs:
        # 版本子目录更可靠:根目录的 WeChat.exe 是个启动壳,子目录里才是本体
        real = [d for d in dirs if not d["tmp"]]
        if real:
            innermost = Path(real[-1]["path"]) / "WeChat.exe"
            if innermost.is_file():
                exe_version = _file_version_windows(innermost)

    version = reg_version or exe_version
    source = "registry" if reg_version else ("fileversion" if exe_version else "")
    if not version:
        real = [d for d in dirs if not d["tmp"]]
        if real:
            version = real[-1].get("version", "")
            source = "dirname" if version else ""

    return {
        "version": version,
        "source": source,
        "install_dir": str(install_dir) if install_dir else "",
        "dirs": dirs,
        "reg_version": reg_raw,
        "reg_decoded": reg_version,
        "exe_version": exe_version,
        "exe_path": str(exe_path) if exe_path else "",
    }


# --------------------------------------------------------------------------
# 「正在准备升级」的痕迹
# --------------------------------------------------------------------------

def update_traces(install_dir: Path = None, appdata: Path = None) -> dict:
    """
    找"微信正在升级 / 刚升完"的证据。

    这些痕迹来自 2026-10-23 两轮实测:

        [3.9.12.55]_tmp          暂存目录,顶替前出现
        patch\\[版本]            差分包工作区(C:\\ProgramData\\Tencent\\WeChat)
        NeedUpdateType != 0      状态字段,升级准备期间变成 200,平时是 0
        XPlugin\\...\\WeChatUpdate  插件热更新的组件目录

    只读,不改任何东西。
    """
    traces = {"tmp_dirs": [], "patch_dir": "", "need_update_type": None,
              "plugin_dir": "", "suspicious_version_dirs": []}

    dirs = find_version_dirs(install_dir)
    traces["tmp_dirs"] = [d["name"] for d in dirs if d["tmp"]]
    traces["suspicious_version_dirs"] = [
        d["name"] for d in dirs if not d["tmp"] and d["version"] != SUPPORTED_VERSION]

    patch = Path(r"C:\ProgramData\Tencent\WeChat\patch")
    if patch.is_dir():
        try:
            entries = [p.name for p in patch.iterdir()]
        except OSError:
            entries = []
        if entries:
            traces["patch_dir"] = str(patch)

    nut = read_registry("NeedUpdateType")
    try:
        traces["need_update_type"] = int(nut) if nut is not None else None
    except (TypeError, ValueError):
        traces["need_update_type"] = None

    if appdata is None:
        appdata = Path(os.environ.get("APPDATA") or "")
    plugin = Path(appdata) / "Tencent" / "WeChat" / "XPlugin" / "Plugins" / "WeChatUpdate"
    if plugin.is_dir():
        traces["plugin_dir"] = str(plugin)

    return traces


def version_ok(info: dict) -> bool:
    """装的版本是不是 wxhook 能用的那个。判定不了(空串)也算不 OK。"""
    return info.get("version") == SUPPORTED_VERSION


def doctor_report() -> dict:
    """
    `--doctor` 用的一份汇总。全是只读检查。

    刻意不复用 preflight.run():那条路会去探测端口、扫 uv 缓存,慢且和
    "这台机器的微信到底能不能用"是两件事。doctor 要能在一秒内跑完。
    """
    info = installed_wechat_version()
    traces = update_traces(Path(info["install_dir"]) if info["install_dir"] else None)
    return {"version": info, "traces": traces, "ok": version_ok(info),
            "supported": SUPPORTED_VERSION}


# --------------------------------------------------------------------------
# 恢复指引
# --------------------------------------------------------------------------

RECOVER_STEPS = """\
微信版本不对,wxhook 0.0.10 只认 {supported}。按顺序做:

  1) 先**完全退出微信**(托盘也要退)
  2) 卸掉当前版本,重装 {supported}:
       https://github.com/tom-snow/wechat-windows-versions/releases/download/v3.9.5.81/WeChatSetup-3.9.5.81.exe
  3) 装完先做一次封锁(**重装只能撑 5–12 分钟,不封锁等于白装**):
       管理员 PowerShell,把下面三处全部拒绝写入/删除:

       $deny = "$env:USERNAME:(WD,AD,DC)"
       icacls 'C:\\WeChat'                           /deny $deny /T /C
       icacls 'C:\\ProgramData\\Tencent\\WeChat'     /deny $deny /T /C
       icacls "$env:APPDATA\\Tencent\\WeChat\\XPlugin" /deny $deny /T /C

       ↑ 路径以你的实际安装目录为准(见下面"安装目录")。
       ↑ DC(删除子项)必须一起拒,只挡写不挡删照样会被换掉。
  4) 重新跑 start.bat

关于网上流传的"防升级三步"——**全都不要用了**:
    x 取消「有更新时自动升级微信」 只是客户端偏好,拦不住热更新链路
    x 改名安装目录里的 WeChatUpdate.exe  根目录没有这个文件;真实文件在
       [版本号]\\ 子目录,而且真正执行的是 XPlugin 与 ProgramData 下的副本
    x reg add ... NeedUpdateType /d 0   它是状态字段,升级前本来就是 0
"""


def recover_hint(install_dir: str = "", supported: str = SUPPORTED_VERSION) -> str:
    text = RECOVER_STEPS.replace("{supported}", supported)
    if install_dir:
        text += "\n你的安装目录: %s\n" % install_dir
    return text


if __name__ == "__main__":  # pragma: no cover - 手工自查用
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rep = doctor_report()
    v = rep["version"]
    print("支持版本   :", rep["supported"])
    print("已安装版本 :", v["version"] or "(判定不了)", "来源:", v["source"] or "-")
    print("安装目录   :", v["install_dir"] or "(读不到)")
    print("版本子目录 :", ", ".join(d["name"] for d in v["dirs"]) or "(无)")
    print("注册表原值 :", v["reg_version"], "->", v["reg_decoded"] or "-")
    print("FileVersion:", v["exe_version"] or "-")
    t = rep["traces"]
    print("待升级痕迹 :")
    print("  暂存目录   :", t["tmp_dirs"] or "-")
    print("  可疑版本   :", t["suspicious_version_dirs"] or "-")
    print("  patch 工作区:", t["patch_dir"] or "-")
    print("  NeedUpdateType:", t["need_update_type"])
    print("  插件目录   :", t["plugin_dir"] or "-")
    print()
    print("判定:", "OK" if rep["ok"] else "版本不对")
    if not rep["ok"]:
        print()
        print(recover_hint(v["install_dir"]))
