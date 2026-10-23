# -*- coding: utf-8 -*-
"""
wxhook 日志归档的离线测试。

2026-10-23:项目目录被整体覆盖后 `logs/bridge.log` 整段消失,那轮成功日志
只剩 wxhook 自己那份 `tools\\logs\\daily_*.txt`(不轮转)。所以启动时顺手
复制一份进项目 `logs/`,两份互相兜底。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wxhook_logs  # noqa: E402


class ArchiveTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.wx = self.base / "wxhook"
        self.src = self.wx / "tools" / "logs"
        self.src.mkdir(parents=True)
        self.dest = self.base / "project" / "logs"

    def _put(self, date, text):
        (self.src / ("daily_%s.txt" % date)).write_text(text, encoding="utf-8")

    def test_copies_and_renames(self):
        self._put("2026-10-23", "hook sync 0\n")
        copied = wxhook_logs.archive(self.wx, self.dest)
        self.assertEqual(copied, ["wxhook-2026-10-23.txt"])
        target = self.dest / "wxhook-2026-10-23.txt"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "hook sync 0\n")

    def test_same_size_is_skipped(self):
        self._put("2026-10-23", "abc")
        wxhook_logs.archive(self.wx, self.dest)
        again = wxhook_logs.archive(self.wx, self.dest)
        self.assertEqual(again, [], "大小一致就不该重复复制")

    def test_grown_file_is_recopied(self):
        self._put("2026-10-23", "abc")
        wxhook_logs.archive(self.wx, self.dest)
        self._put("2026-10-23", "abcdef")      # DLL 又写了几行
        again = wxhook_logs.archive(self.wx, self.dest)
        self.assertEqual(again, ["wxhook-2026-10-23.txt"])
        self.assertEqual((self.dest / "wxhook-2026-10-23.txt")
                         .read_text(encoding="utf-8"), "abcdef")

    def test_missing_source_dir_is_safe(self):
        self.assertEqual(wxhook_logs.archive(self.base / "nope", self.dest), [])
        self.assertFalse(self.dest.exists())

    def test_no_wxhook_root(self):
        self.assertEqual(wxhook_logs.archive(None, self.dest), [])

    def test_old_archives_pruned(self):
        self._put("2026-10-23", "new")
        wxhook_logs.archive(self.wx, self.dest, keep_days=0)
        stale = self.dest / "wxhook-2020-01-01.txt"
        stale.write_text("old", encoding="utf-8")
        old_time = time.time() - 60 * 86400
        import os
        os.utime(stale, (old_time, old_time))

        wxhook_logs.archive(self.wx, self.dest, keep_days=30)
        self.assertFalse(stale.exists(), "超过 30 天的归档要清掉")
        self.assertTrue((self.dest / "wxhook-2026-10-23.txt").exists())

    def test_unreadable_source_is_skipped_not_fatal(self):
        """DLL 正在写目标文件时 copyfile 会失败 —— 不能把整轮归档搞挂。"""
        self._put("2026-10-23", "abc")
        with patch.object(wxhook_logs.shutil, "copyfile",
                          side_effect=PermissionError("locked")):
            copied = wxhook_logs.archive(self.wx, self.dest)
        self.assertEqual(copied, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
