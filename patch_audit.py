"""Narrow crash-site exclusion and per-site auditing for the existing patch.

No process is opened here. The caller supplies the exact wxhook-owned process.
The rest of the candidate scoring/selection remains the same as the old patch.
"""
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


def file_sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_patch(pm, cfg, log, audit_dir, query_memory, hash_file=file_sha256):
    """Return audit path/counts. Raise on unsafe setup or audit persistence failure.

Never changes page protections. A failed/mismatched pre-read is not overwritten.
Only three-byte values are logged, not surrounding memory or message contents.
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

        record("start", schema=1, pid=pm.process_id,
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
                    # Persist intent before writing; a partial run remains diagnosable.
                    record("write_intent", **site)
                    try:
                        pm.write_bytes(addr, REPLACEMENT, 3)
                    except Exception as exc:
                        status = "write_failed"
                        site["write_error"] = f"{type(exc).__name__}: {exc}"
                    else:
                        try:
                            after = pm.read_bytes(addr, 3)
                            site["after"] = after.hex()
                            status = "verified" if after == REPLACEMENT else "verify_mismatch"
                        except Exception as exc:
                            status = "verify_failed"
                            site["verify_error"] = f"{type(exc).__name__}: {exc}"
                counts[status] += 1
                record("site_result", status=status, **site)
                if status not in ("verified", "not_selected", "skipped_crash_site"):
                    log.warning("补丁候选 @0x%X %s: %s", addr, status,
                                site.get("write_error") or site.get("read_error") or
                                site.get("verify_error") or site.get("page_error") or "字节校验不一致")
            record("summary", candidates=len(candidates), counts=dict(counts))
        except Exception as exc:
            record("aborted", error=f"{type(exc).__name__}: {exc}", counts=dict(counts))
            raise
    log.info("x64->x32 补丁: 回读确认 %d 处，可疑位置跳过 %d 处；详情 %s",
             counts.get("verified", 0), counts.get("skipped_crash_site", 0), path)
    return path, dict(counts)
