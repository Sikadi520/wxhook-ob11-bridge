# -*- coding: utf-8 -*-
"""
DACL 读取（防升级封锁检测）的离线测试。

2026-10-23 写这个检测时踩了两个常量错误，都表现为“明明封了却报未封”：

1. **ACL 头部偏移**：用 ``ctypes.Structure`` 描述 ACL 时，``c_ushort`` 会做
   2 字节对齐，``AceCount`` 落到 offset **6** 而不是 4，整体 sizeof 变 8。
   实测真实 7 个 ACE 读成 6 个，恰好把 DENY 那条漏掉。
2. **ACE 类型**：``ACCESS_DENIED_ACE_TYPE`` 是 **1**，不是 3（3 是 AUDIT）。

这里锁住偏移与常量，保证以后改代码不会又写回错的值。
"""
import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wechat_version as wv  # noqa: E402

IS_WINDOWS = os.name == "nt"


class AclLayoutTest(unittest.TestCase):
    """ACL 头部的字节布局。"""

    def test_ace_count_lives_at_offset_4(self):
        """ACL 头: rev(1) sbz1(1) size(2) count(2) sbz2(2),ACE 从 offset 8 开始。"""
        buf = ctypes.create_string_buffer(64)
        buf[0] = b"\x02"
        ctypes.cast(ctypes.byref(buf, 2), ctypes.POINTER(ctypes.c_ushort))[0] = 64
        ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(ctypes.c_ushort))[0] = 7
        base = ctypes.addressof(buf)
        self.assertEqual(
            ctypes.cast(base + 4, ctypes.POINTER(ctypes.c_ushort))[0], 7)

    def test_ctypes_structure_matches_raw_offsets(self):
        """
        把事实固定下来:结构体写法与原始偏移在本平台上一致。

        （一开始怀疑 ctypes 对齐把 AceCount 推到 offset 6 导致漏读 DENY,
        实测证明结构体偏移是对的。真正原因是探测时 DENY 还没落盘。
        写成测试,避免下次又去怀疑错的地方。）
        """
        class ACL(ctypes.Structure):
            _fields_ = [("AclRevision", ctypes.c_ubyte),
                        ("Sbz1", ctypes.c_ubyte),
                        ("AclSize", ctypes.c_ushort),
                        ("AceCount", ctypes.c_ushort),
                        ("Sbz2", ctypes.c_ushort)]

        self.assertEqual(getattr(ACL, "AceCount").offset, 4)
        self.assertEqual(ctypes.sizeof(ACL), 8)


class DenyAceConstantTest(unittest.TestCase):
    """ACE 类型常量必须和 WinNT.h 一致。"""

    def test_denied_type_is_1_not_3(self):
        source = (ROOT / "wechat_version.py").read_text(encoding="utf-8")
        self.assertIn("ACCESS_DENIED_ACE_TYPE = 1", source,
                      "ACCESS_DENIED_ACE_TYPE 是 1；3 是 SYSTEM_AUDIT")
        self.assertNotIn("DENY_ACCESS = 3", source,
                         "旧写法把 3 当成 DENY，已证明是错的")

    def test_acl_header_size_constant(self):
        source = (ROOT / "wechat_version.py").read_text(encoding="utf-8")
        self.assertIn("ACL_HEADER_SIZE = 8", source)
        self.assertNotIn("ctypes.sizeof(ACL)", source,
                         "不能用 sizeof(结构体)，字段偏移已被对齐破坏")


@unittest.skipUnless(IS_WINDOWS, "DACL 只在 Windows 上有")
class RealDaclTest(unittest.TestCase):
    """在真实目录上跑一遍（不需要管理员：DENY 加在自己的 temp 目录上）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.added = False

    def tearDown(self):
        if self.added:
            import subprocess
            subprocess.run(["icacls", str(self.path), "/remove:d",
                            os.environ.get("USERNAME", "")],
                           capture_output=True, text=True)

    def test_plain_directory_has_no_deny(self):
        self.assertFalse(wv._has_deny_ace(self.path))

    @unittest.skipUnless(IS_WINDOWS, "icacls 只在 Windows 上有")
    def test_deny_ace_is_detected(self):
        import subprocess
        user = os.environ.get("USERNAME", "")
        r = subprocess.run(["icacls", str(self.path), "/deny",
                            "%s:(WD,AD,DC)" % user],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("当前环境无法修改 ACL: %s" % (r.stdout or r.stderr))
        self.added = True
        self.assertTrue(wv._has_deny_ace(self.path),
                        "有 DENY 的目录必须被识别出来")

    def test_missing_path_returns_false(self):
        self.assertFalse(wv._has_deny_ace(self.path / "nope"))


class CheckBlockadeTest(unittest.TestCase):

    def test_result_shape(self):
        rep = wv.check_blockade(r"D:\WeChat")
        self.assertIn("ok", rep)
        self.assertIn("denied", rep)
        self.assertEqual(len(rep["targets"]), 3)
        keys = [t["key"] for t in rep["targets"]]
        self.assertEqual(keys, ["install", "programdata", "xplugin"])

    def test_missing_install_dir_marks_not_denied(self):
        rep = wv.check_blockade(
            str(Path(tempfile.gettempdir()) / "definitely_not_here"))
        install = next(t for t in rep["targets"] if t["key"] == "install")
        self.assertFalse(install["exists"])
        self.assertFalse(install["denied"])
        self.assertFalse(rep["ok"])

    def test_no_targets_means_not_ok(self):
        """什么都不存在时不能报“已封锁”。"""
        rep = wv.check_blockade("")
        if not any(t["exists"] for t in rep["targets"]):
            self.assertFalse(rep["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
