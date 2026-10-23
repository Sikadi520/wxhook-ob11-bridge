# -*- coding: utf-8 -*-
"""
把 wxhook 自己的日志归档到项目目录。

## 为什么需要(2026-10-23 的教训)

wxhook 的 DLL 把日志写到 `<wxhook>\\tools\\logs\\daily_YYYY-MM-DD.txt`,**不轮转、
不清理**。而桥接自己的 `logs/bridge.log` 是 `RotatingFileHandler(5MB, backupCount=3)`。

问题不在轮转,在于**位置**:那次项目目录被整体覆盖/重新解压,`logs/` 跟着没了,
15:41 那轮最关键的成功日志整段消失。最后能还原时间线,全靠 wxhook 自己那份
`daily_*.txt` —— 它藏在 uv 缓存里,反而活下来了。

所以启动时顺手把 wxhook 的日志复制一份进项目 `logs/`,两份互不依赖:
项目目录没了还有缓存里的原件,缓存被清了还有项目里这份。
"""

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("wxhook_logs")

PREFIX = "wxhook-"


def wxhook_log_dir(wxhook_root: Path):
    """`<wxhook>\\tools\\logs`;不存在返回 None。"""
    if not wxhook_root:
        return None
    d = Path(wxhook_root) / "tools" / "logs"
    return d if d.is_dir() else None


def archive(wxhook_root: Path, dest_dir: Path, keep_days: int = 30) -> list:
    """
    把 wxhook 的 daily_*.txt 复制到 dest_dir,重命名为
    `wxhook-YYYY-MM-DD.txt`。返回本次复制/更新的文件名列表。

    规则:
      * 目标已存在且大小相同 → 跳过(不刷 mtime,免得每次都算"新");
      * 源文件正在被 DLL 写入 → 复制可能失败,失败就跳过,不重试不报错;
      * 超过 keep_days 的归档删掉(默认 30 天)。
    """
    src = wxhook_log_dir(wxhook_root)
    if not src:
        return []
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("建不了归档目录:%s", dest_dir)
        return []

    copied = []
    try:
        sources = sorted(src.glob("daily_*.txt"))
    except OSError:
        return []

    for f in sources:
        date = f.stem[len("daily_"):]
        target = dest_dir / ("%s%s.txt" % (PREFIX, date))
        try:
            if target.exists() and target.stat().st_size == f.stat().st_size:
                continue
            shutil.copyfile(f, target)
            copied.append(target.name)
        except OSError as exc:
            # DLL 正在写这个文件时常见,忽略即可
            log.debug("归档 %s 失败(可能正在写入):%s", f.name, exc)
            continue

    if copied:
        log.info("已归档 wxhook 日志 %d 份 -> %s", len(copied), dest_dir)
    _prune(dest_dir, keep_days)
    return copied


def _prune(dest_dir: Path, keep_days: int):
    """删掉过期归档;keep_days<=0 表示不清理。"""
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        for f in dest_dir.glob(PREFIX + "*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return
    if removed:
        log.info("清理过期 wxhook 日志归档 %d 份(保留 %d 天)", removed, keep_days)
