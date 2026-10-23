# -*- coding: utf-8 -*-
"""Narrow crash-site exclusion and per-site auditing for the existing patch.

No process is opened here. The caller supplies the exact wxhook-owned process.
The rest of the candidate scoring/selection remains the same as the old patch.

## 页保护(VirtualProtectEx)—— 2026-10-23 排障报告 P1-1

旧实现写死"Never changes page protections",于是只读页一律写不进去:

    candidates=52  verified=30  write_failed=20     <- 全部 GetLastError 998
                                                   (ERROR_NOACCESS = 只读页)

能过门禁的机器是"恰好门禁依赖的那处可写",换台机器内存布局一变就可能变成
"日志显示补丁跑完了、实际没打上"。所以现在按以下顺序写入:

    1. VirtualProtectEx(local, page, PAGE_READWRITE)
    2. 写 3 字节
    3. 回读校验
    4. finally: VirtualProtectEx(原保护) 恢复,并把恢复结果写进审计

审计里逐站点记录 ``protect_before`` / ``protect_restored``,``write_failed``
再按原因分类(reprotect_failed / restore_failed / other),便于判断
"这轮到底有没有打全"。

排除逻辑不变:``wcprobe.dll+0x26A6DD`` 仍然跳过 —— 那是崩溃可疑点。
"""
import ctypes
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from uuid import uuid4

EXCLUDED_MODULE = "wcprobe.dll"
EXCLUDED_RVA = 0x26A6DD
# Verified local wcprobe.dll, FileVersion 2.0.1.4, from WeChat 3.9.5.81.
EXCLUDED_SHA256 = "17c1edd5393a07fa06219471203822114751d762e930acc14be2129245614576"
ORIGINAL = b"x64"
REPLACEMENT = b"x32"

# Win32 页保护常量
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_WRITECOPY = 0x80
WRITABLE_PROTECTS = (PAGE_READWRITE, PAGE_EXECUTE_READWRITE,
                     PAGE_WRITECOPY, PAGE_EXECUTE_WRITECOPY)
ERROR_NOACCESS = 998

PROTECT_NAMES = {
    0x01: "NOACCESS", 0x02: "READONLY", 0x04: "READWRITE", 0x08: "WRITECOPY",
    0x10: "EXECUTE", 0x20: "EXECUTE_READ", 0x40: "EXECUTE_READWRITE",
    0x80: "EXECUTE_WRITECOPY", 0x100: "GUARD", 0x200: "NOCACHE",
}


def protect_name(value):
    """把页保护数值翻译成人能看懂的常量名(GUARD 等是叠加位,单独列)。"""
    if not value:
        return None
    flags = [PROTECT_NAMES.get(value & 0xFF)] + \
            [name for bit, name in ((0x100, "GUARD"), (0x200, "NOCACHE")) if value & bit]
    return "|".join(n for n in flags if n) or hex(value)


def file_sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# --------------------------------------------------------------------------
# 页保护
# --------------------------------------------------------------------------

class _Kernel32:
    """延迟拿 kernel32.VirtualProtectEx;拿不到就返回 None,由调用方降级。"""

    _fn = None
    _tried = False

    @classmethod
    def virtual_protect_ex(cls):
        if not cls._tried:
            cls._tried = True
            try:
                fn = ctypes.windll.kernel32.VirtualProtectEx     # noqa: SLF001
                fn.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
                fn.restype = ctypes.c_int
                cls._fn = fn
            except Exception:
                cls._fn = None
        return cls._fn


def set_page_protection(process_handle, address, size, protect, protect_fn=None):
    """
    改一段页的保护,返回旧保护值。

    ``protect_fn`` 是可注入的钩子(单测用),签名:
        (handle, address, size, new_protect) -> old_protect
    """
    fn = protect_fn or _Kernel32.virtual_protect_ex()
    if fn is None:
        raise OSError("VirtualProtectEx 不可用(当前不是 Windows?)")
    if protect_fn is not None:
        # 单测钩子:直接收普通整数,不用自己拼 ctypes 参数
        old = fn(int(process_handle), int(address), int(size), int(protect))
        return int(old or 0)
    old = ctypes.c_uint32(0)
    ok = fn(ctypes.c_void_p(int(process_handle)), ctypes.c_void_p(int(address)),
            ctypes.c_size_t(int(size)), ctypes.c_uint32(int(protect)),
            ctypes.byref(old))
    if not ok:
        raise OSError(ctypes.get_last_error() or 0, "VirtualProtectEx failed")
    return int(old.value)


def native_protect(handle, address, size, new_protect):
    """真实实现;kernel32 或 VirtualProtectEx 不可用时直接抛,由上层降级。"""
    fn = _Kernel32.virtual_protect_ex()
    if fn is None:
        raise OSError("VirtualProtectEx 不可用(当前不是 Windows?)")
    old = ctypes.c_uint32(0)
    ok = fn(ctypes.c_void_p(int(handle)), ctypes.c_void_p(int(address)),
            ctypes.c_size_t(int(size)), ctypes.c_uint32(int(new_protect)),
            ctypes.byref(old))
    if not ok:
        raise OSError(ctypes.get_last_error() or 0, "VirtualProtectEx failed")
    return int(old.value)


def _page_span(address, page):
    """覆盖 address 的那一整页(跨页的 3 字节也要一起放开)。"""
    base = int(getattr(page, "BaseAddress", 0) or 0)
    size = int(getattr(page, "RegionSize", 0) or 0)
    if not base or not size:
        return int(address) & ~0xFFF, 0x1000
    if base <= address < base + size:
        return base, size
    return int(address) & ~0xFFF, 0x1000


def run_patch(pm, cfg, log, audit_dir, query_memory, hash_file=file_sha256,
              protect_fn=None):
    """Return audit path/counts. Raise on unsafe setup or audit persistence failure.

    Writes are now wrapped in VirtualProtectEx and always restored; the before /
    after protection is recorded per site. A failed/mismatched pre-read is still
    never overwritten. Only three-byte values are logged, not surrounding memory
    or message contents.
    """
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = audit_dir / f"patch-{stamp}-pid{pm.process_id}-{uuid4().hex[:8]}.jsonl"
    counts = Counter()
    log.info("补丁逐项审计日志: %s", path)
    with path.open("x", encoding="utf-8") as stream:
        def record(event, **data):
            stream.write(json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(),
                                     "event": event, **data}, ensure_ascii=False) + "\n")
            stream.flush()

        record("start", schema=2, pid=pm.process_id,
               strategy="virtual_protect_ex",
               exclusion={"module": EXCLUDED_MODULE, "rva": hex(EXCLUDED_RVA),
                          "sha256": EXCLUDED_SHA256})
        try:
            modules = []
            for mod in pm.list_modules():
                filename = mod.filename
                name = PureWindowsPath(filename).name.lower()
                if not name:
                    raise RuntimeError("无法识别已加载模块，停止本轮写入")
                item = {"base": int(mod.lpBaseOfDll), "size": int(mod.SizeOfImage),
                        "name": name}
                if name == EXCLUDED_MODULE:
                    digest = hash_file(filename).lower()
                    record("module_identity", module=name, base=hex(item["base"]), sha256=digest)
                    if digest != EXCLUDED_SHA256:
                        raise RuntimeError("wcprobe.dll 哈希与已分析版本不符，不能盲用排除偏移；本轮未写入")
                modules.append(item)

            addresses = list(pm.pattern_scan_all(ORIGINAL, return_multiple=True) or [])
            candidates = []
            for addr in addresses:
                # Preserve the original score and stable order, including score=0.
                try:
                    raw = pm.read_bytes(addr - 64, 160)
                except Exception:
                    raw = b""
                text = "".join(chr(v) if 32 <= v < 127 else "." for v in raw)
                score = 2 * bool(re.search(r"\d+\.\d+\.\d+", text))
                score += 2 * ("wechat" in text.lower())
                score += text.count(".") < 60
                candidates.append((score, addr))
            candidates.sort(key=lambda item: -item[0])
            limit = int(cfg.get("patch_max_sites", 60) or 60)
            selected_count = min(len(candidates), limit if limit > 0 else 1)
            record("scan", candidates=len(candidates), selected=selected_count, patch_max_sites=limit)

            for index, (score, addr) in enumerate(candidates):
                mod = next((m for m in modules if m["base"] <= addr < m["base"] + m["size"]), None)
                rva = addr - mod["base"] if mod else None
                site = {"address": hex(addr), "score": score,
                        "module": mod["name"] if mod else None,
                        "rva": hex(rva) if rva is not None else None,
                        "expected": ORIGINAL.hex(), "target": REPLACEMENT.hex()}
                page = None
                try:
                    page = query_memory(pm.process_handle, addr)
                    site["page"] = {key: int(getattr(page, key) or 0) for key in
                                    ("BaseAddress", "AllocationBase", "RegionSize", "State", "Protect", "Type")}
                except Exception as exc:
                    site["page_error"] = f"{type(exc).__name__}: {exc}"
                before = None
                try:
                    before = pm.read_bytes(addr, 3)
                    site["before"] = before.hex()
                except Exception as exc:
                    site["read_error"] = f"{type(exc).__name__}: {exc}"

                if mod and mod["name"] == EXCLUDED_MODULE and rva == EXCLUDED_RVA:
                    status = "skipped_crash_site"
                    log.warning("保留补丁，仅跳过崩溃可疑候选: %s+0x%X @0x%X", mod["name"], rva, addr)
                elif index >= selected_count:
                    status = "not_selected"
                elif before is None:
                    status = "read_failed"
                elif before != ORIGINAL:
                    status = "skipped_changed_bytes"
                elif "page_error" in site:
                    status = "page_query_failed"
                else:
                    status, site = _write_site(pm, addr, site, page, protect_fn, record)
                counts[status] += 1
                record("site_result", status=status, **site)
                if status not in ("verified", "not_selected", "skipped_crash_site"):
                    log.warning("补丁候选 @0x%X %s: %s", addr, status, _why(site))
            record("summary", candidates=len(candidates), counts=dict(counts))
        except Exception as exc:
            record("aborted", error=f"{type(exc).__name__}: {exc}", counts=dict(counts))
            raise
    log.info("x64->x32 补丁: 回读确认 %d 处，可疑位置跳过 %d 处，写入失败 %d 处；详情 %s",
             counts.get("verified", 0), counts.get("skipped_crash_site", 0),
             _write_failures(counts), path)
    return path, dict(counts)


def _write_failures(counts):
    return sum(v for k, v in counts.items() if k.startswith("write_failed"))


def _why(site):
    return (site.get("write_error") or site.get("restore_error") or
            site.get("read_error") or site.get("verify_error") or
            site.get("page_error") or site.get("protect_error") or
            "字节校验不一致")


def _write_site(pm, addr, site, page, protect_fn, record):
    """
    改页保护 -> 写 -> 回读 -> 恢复保护。返回 (status, site)。

    恢复放在 finally 里:即使写入或回读抛异常,页面也不会停在我们改过的
    可写状态上 —— 那会扩大后续内存被误改的窗口。
    """
    old_protect = int(getattr(page, "Protect", 0) or 0)
    base, size = _page_span(addr, page)
    site["protect_before"] = old_protect
    site["protect_before_name"] = protect_name(old_protect)
    site["protect_span"] = [hex(base), hex(size)]

    changed = old_protect and (old_protect & 0xFF) not in WRITABLE_PROTECTS
    saved = None
    if changed:
        record("protect_intent", address=hex(addr), page=hex(base), size=hex(size),
               before=old_protect, before_name=protect_name(old_protect),
               target=PAGE_READWRITE)
        try:
            saved = set_page_protection(pm.process_handle, base, size,
                                        PAGE_READWRITE, protect_fn)
            site["page_unlocked"] = True
        except Exception as exc:
            site["protect_error"] = f"{type(exc).__name__}: {exc}"
            site["protect_restored"] = None
            return "write_failed_reprotect", site
    else:
        site["page_unlocked"] = False

    try:
        # Persist intent before writing; a partial run remains diagnosable.
        record("write_intent", **site)
        try:
            pm.write_bytes(addr, REPLACEMENT, 3)
        except Exception as exc:
            site["write_error"] = f"{type(exc).__name__}: {exc}"
            status = ("write_failed_protect" if _is_noaccess(exc)
                      else "write_failed_other")
            return status, site
        try:
            after = pm.read_bytes(addr, 3)
            site["after"] = after.hex()
            return ("verified" if after == REPLACEMENT else "verify_mismatch"), site
        except Exception as exc:
            site["verify_error"] = f"{type(exc).__name__}: {exc}"
            return "verify_failed", site
    finally:
        if saved is not None:
            try:
                set_page_protection(pm.process_handle, base, size, saved, protect_fn)
                site["protect_restored"] = True
            except Exception as exc:
                site["protect_restored"] = False
                site["restore_error"] = f"{type(exc).__name__}: {exc}"


def _is_noaccess(exc):
    """
    判断写入异常是不是"页只读"。

    pymem 抛的是 ``Could not write memory at: ... - GetLastError: 998``,
    Windows 的 998 = ERROR_NOACCESS。按数字和关键字双保险,避免措辞变了就认不出。
    """
    text = str(exc)
    return str(ERROR_NOACCESS) in text or "NOACCESS" in text.upper() or \
        "access violation" in text.lower() or "拒绝访问" in text
