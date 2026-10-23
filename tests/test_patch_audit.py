"""Offline tests: no real process access, injection, network, or chat messages."""
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import patch_audit as audit

LOG = logging.getLogger("patch-audit-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class FakeMemory:
    def __init__(self, base=0x7FFF0A130000, name="wcprobe.dll"):
        self.process_id = 12345
        self.process_handle = 88
        self.base = base
        self.target = base + audit.EXCLUDED_RVA
        self.other = base + 0x1000
        self.addresses = [self.target, self.other]
        self.data = {address: b"x64" for address in self.addresses}
        self.writes = []
        self.modules = [types.SimpleNamespace(filename="D:\\WeChat\\" + name,
                        lpBaseOfDll=base, SizeOfImage=0x600000)]
        self.fail_write = False
        self.fail_read = False
        self.fail_verify = False
        self.ignore_write = False

    def list_modules(self):
        return iter(self.modules)

    def pattern_scan_all(self, pattern, return_multiple):
        assert pattern == b"x64" and return_multiple is True
        return self.addresses

    def read_bytes(self, addr, size):
        if size == 160:
            return b"PRIVATE_CONTEXT_WECHAT_3.9.5.81"  # must not enter audit log
        if self.fail_read or (self.fail_verify and self.writes):
            raise OSError("mock read failure")
        return self.data[addr]

    def write_bytes(self, addr, value, size):
        assert value == b"x32" and size == 3
        if self.fail_write:
            raise OSError("mock write denied")
        self.writes.append(addr)
        if not self.ignore_write:
            self.data[addr] = value


def query_page(handle, addr):
    return types.SimpleNamespace(BaseAddress=addr & ~0xFFF, AllocationBase=0,
                                 RegionSize=4096, State=0x1000, Protect=0x40, Type=0x1000000)


def readonly_page(handle, addr):
    """报告 P1-1 里的情况:目标页是 PAGE_READONLY,直接写会得到 998。"""
    return types.SimpleNamespace(BaseAddress=addr & ~0xFFF, AllocationBase=0,
                                 RegionSize=4096, State=0x1000, Protect=0x02,
                                 Type=0x1000000)


class FakeProtector:
    """记录 VirtualProtectEx 调用,并让写入按"当前页保护"决定成败。"""

    def __init__(self, fail_reprotect=False, fail_restore=False):
        self.calls = []
        self.fail_reprotect = fail_reprotect
        self.fail_restore = fail_restore
        self.protect = 0x02          # 初始只读

    def __call__(self, handle, address, size, new_protect):
        self.calls.append((address, size, new_protect))
        if self.fail_reprotect:
            raise OSError(5, "Access is denied")
        if self.fail_restore and new_protect != audit.PAGE_READWRITE:
            raise OSError(5, "restore denied")
        old, self.protect = self.protect, new_protect
        return old


class PatchAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_patch(self, mem, cfg=None, **kwargs):
        path, counts = audit.run_patch(mem, cfg or {"patch_max_sites": 999}, LOG,
            self.tmp.name, kwargs.pop("query_memory", query_page),
            hash_file=kwargs.pop("hash_file", lambda _: audit.EXCLUDED_SHA256), **kwargs)
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return path, counts, events
    def test_exact_site_excluded_and_other_patch_still_written(self):
        mem = FakeMemory()
        _, counts, events = self.run_patch(mem)
        self.assertEqual(mem.writes, [mem.other])
        self.assertEqual(mem.data[mem.target], b"x64")
        self.assertEqual(counts, {"skipped_crash_site": 1, "verified": 1})
        verified = next(e for e in events if e.get("status") == "verified")
        self.assertEqual(verified["before"], "783634")
        self.assertEqual(verified["after"], "783332")
        self.assertIn("Protect", verified["page"])
        self.assertLess(next(i for i,e in enumerate(events) if e["event"] == "write_intent"),
                        events.index(verified))

    def test_exclusion_survives_aslr(self):
        mem = FakeMemory(base=0x7FFAC04E0000)
        self.run_patch(mem)
        self.assertEqual(mem.writes, [mem.other])

    def test_same_offset_in_other_module_not_excluded(self):
        mem = FakeMemory(name="other.dll")
        self.run_patch(mem)
        self.assertEqual(mem.writes, mem.addresses)

    def test_wrong_hash_aborts_before_any_write(self):
        mem = FakeMemory()
        with self.assertRaisesRegex(RuntimeError, "哈希"):
            self.run_patch(mem, hash_file=lambda _: "0" * 64)
        self.assertEqual(mem.writes, [])
        records = next(Path(self.tmp.name).glob("*.jsonl")).read_text(encoding="utf-8")
        self.assertIn('"event": "aborted"', records)

    def test_hash_read_failure_aborts_before_any_write(self):
        mem = FakeMemory()
        with self.assertRaises(OSError):
            self.run_patch(mem, hash_file=Mock(side_effect=OSError("cannot hash")))
        self.assertEqual(mem.writes, [])

    def test_write_failure_logged_not_counted_success(self):
        mem = FakeMemory()
        mem.fail_write = True
        _, counts, events = self.run_patch(mem)
        # 报告 P1-1:write_failed 现在按原因分类。page 保护是 0x40(READWRITE),
        # 不需要 VirtualProtectEx,所以这里是 write_failed_other
        self.assertEqual(counts["write_failed_other"], 1)
        self.assertNotIn("verified", counts)
        self.assertTrue(any(e.get("write_error") for e in events))

    def test_readback_mismatch_not_counted_success(self):
        mem = FakeMemory()
        mem.ignore_write = True
        _, counts, _ = self.run_patch(mem)
        self.assertEqual(counts["verify_mismatch"], 1)
        self.assertNotIn("verified", counts)

    def test_changed_bytes_not_overwritten(self):
        mem = FakeMemory()
        mem.data[mem.other] = b"abc"
        _, counts, _ = self.run_patch(mem)
        self.assertEqual(counts["skipped_changed_bytes"], 1)
        self.assertEqual(mem.writes, [])

    def test_read_failure_does_not_write(self):
        mem = FakeMemory()
        mem.fail_read = True
        _, counts, _ = self.run_patch(mem)
        self.assertEqual(counts["read_failed"], 1)
        self.assertEqual(mem.writes, [])

    def test_verify_read_failure_recorded(self):
        mem = FakeMemory()
        mem.fail_verify = True
        _, counts, _ = self.run_patch(mem)
        self.assertEqual(counts["verify_failed"], 1)

    def test_page_query_failure_does_not_write(self):
        mem = FakeMemory()
        _, counts, _ = self.run_patch(mem, query_memory=Mock(side_effect=OSError("query failed")))
        self.assertEqual(mem.writes, [])
        self.assertEqual(counts["page_query_failed"], 1)

    def test_context_not_logged_and_logs_are_unique(self):
        first, _, _ = self.run_patch(FakeMemory())
        second, _, _ = self.run_patch(FakeMemory())
        self.assertNotEqual(first, second)
        self.assertNotIn("PRIVATE_CONTEXT", first.read_text(encoding="utf-8"))

    def test_exclusion_does_not_backfill_beyond_original_limit(self):
        mem = FakeMemory()
        _, counts, _ = self.run_patch(mem, {"patch_max_sites": 1})
        self.assertEqual(mem.writes, [])
        self.assertEqual(counts["not_selected"], 1)

    def test_no_candidates(self):
        mem = FakeMemory()
        mem.addresses = None
        _, counts, events = self.run_patch(mem)
        self.assertEqual(counts, {})
        self.assertEqual(events[-1]["candidates"], 0)

    def test_audit_cannot_open_means_no_write(self):
        mem = FakeMemory()
        blocked = Path(self.tmp.name) / "file-not-directory"
        blocked.write_text("occupied", encoding="utf-8")
        with self.assertRaises(OSError):
            audit.run_patch(mem, {}, LOG, blocked, query_page)
        self.assertEqual(mem.writes, [])


class PageProtectionTests(unittest.TestCase):
    """报告 P1-1:只读页写不进去(GetLastError 998),必须临时放开再恢复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_patch(self, mem, protector):
        path, counts = audit.run_patch(
            mem, {"patch_max_sites": 999}, LOG, self.tmp.name, readonly_page,
            hash_file=lambda _: audit.EXCLUDED_SHA256, protect_fn=protector)
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return counts, events

    def test_readonly_page_is_unlocked_then_restored(self):
        mem = FakeMemory()
        protector = FakeProtector()
        counts, events = self.run_patch(mem, protector)

        # 除 crash site 外还是打上了
        self.assertEqual(mem.writes, [mem.other])
        self.assertEqual(counts.get("verified"), 1)
        # 一开一收:先 READWRITE,再恢复成原来的 READONLY
        self.assertEqual([c[2] for c in protector.calls],
                         [audit.PAGE_READWRITE, 0x02])
        self.assertEqual(protector.protect, 0x02)

        verified = next(e for e in events if e.get("status") == "verified")
        self.assertEqual(verified["protect_before"], 0x02)
        self.assertEqual(verified["protect_before_name"], "READONLY")
        self.assertTrue(verified["page_unlocked"])
        self.assertTrue(verified["protect_restored"])

    def test_writable_page_needs_no_virtual_protect(self):
        mem = FakeMemory()
        protector = FakeProtector()
        path, counts = audit.run_patch(
            mem, {"patch_max_sites": 999}, LOG, self.tmp.name, query_page,
            hash_file=lambda _: audit.EXCLUDED_SHA256, protect_fn=protector)
        self.assertEqual(protector.calls, [])
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        verified = next(e for e in events if e.get("status") == "verified")
        self.assertFalse(verified["page_unlocked"])

    def test_reprotect_failure_is_its_own_bucket(self):
        mem = FakeMemory()
        counts, events = self.run_patch(mem, FakeProtector(fail_reprotect=True))
        self.assertEqual(counts.get("write_failed_reprotect"), 1)
        self.assertEqual(mem.writes, [])
        intent = [e for e in events if e.get("event") == "protect_intent"]
        self.assertEqual(len(intent), 1)
        self.assertEqual(intent[0]["before_name"], "READONLY")

    def test_restore_failure_is_recorded(self):
        mem = FakeMemory()
        counts, events = self.run_patch(mem, FakeProtector(fail_restore=True))
        # 写入本身成功,但页面没恢复 —— 必须能从审计里看出来
        self.assertEqual(counts.get("verified"), 1)
        verified = next(e for e in events if e.get("status") == "verified")
        self.assertFalse(verified["protect_restored"])
        self.assertIn("restore_error", verified)

    def test_noaccess_998_maps_to_protect_bucket(self):
        """pymem 真实的 998 报文要能认出来。"""
        exc = OSError("Could not write memory at: 0x1234 - GetLastError: 998")
        self.assertTrue(audit._is_noaccess(exc))
        self.assertFalse(audit._is_noaccess(OSError("some other failure")))


class WeChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        wxhook = types.ModuleType("wxhook")
        wxhook.Bot = Mock()
        wxhook.events = types.SimpleNamespace(TEXT_MESSAGE=1)
        self.modules = patch.dict(sys.modules, {"wxhook": wxhook})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("wechat_under_test", ROOT / "wechat.py")
        self.wechat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wechat)
        self.wechat.log = LOG

    def test_constructor_callback_uses_bot_argument_before_assignment(self):
        side = self.wechat.WeChatSide({"patch_x64_to_x32": True}, lambda e: None)
        fake = Mock()
        def construct(**kwargs):
            self.assertIsNone(side.bot)
            kwargs["on_start"](fake)
            return fake
        with patch.object(self.wechat, "Bot", side_effect=construct), \
             patch.object(side, "_patch_x64_to_x32") as apply, \
             patch.object(side, "_start_watchdog"):
            side._run()
        apply.assert_called_once_with(fake)

    def test_disabled_setting_still_respected(self):
        side = self.wechat.WeChatSide({"patch_x64_to_x32": False}, lambda e: None)
        fake = Mock()
        def construct(**kwargs):
            kwargs["on_start"](fake)
            return fake
        with patch.object(self.wechat, "Bot", side_effect=construct), \
             patch.object(side, "_patch_x64_to_x32") as apply, \
             patch.object(side, "_start_watchdog"):
            side._run()
        apply.assert_not_called()

    def check_handle(self, fail=False):
        pymem = types.ModuleType("pymem")
        memory = types.ModuleType("pymem.memory")
        memory.virtual_query = Mock()
        pymem.memory = memory
        handle = Mock()
        pymem.Pymem = Mock(return_value=handle)
        side = self.wechat.WeChatSide({"patch_x64_to_x32": True}, lambda e: None)
        bot = types.SimpleNamespace(process=types.SimpleNamespace(pid=4321))
        with patch.dict(sys.modules, {"pymem": pymem, "pymem.memory": memory}), \
             patch.object(audit, "run_patch", side_effect=RuntimeError("mock failure") if fail else None) as run:
            side._patch_x64_to_x32(bot)
        pymem.Pymem.assert_called_once_with(4321)
        handle.close_process.assert_called_once_with()
        run.assert_called_once()

    def test_exact_process_id_and_handle_cleanup(self):
        self.check_handle()

    def test_handle_closed_even_if_patch_fails(self):
        self.check_handle(fail=True)


if __name__ == "__main__":
    unittest.main()
