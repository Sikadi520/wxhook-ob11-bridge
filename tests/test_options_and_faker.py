# -*- coding: utf-8 -*-
"""
"伪装 + 补丁必须同时开"的离线测试。

报告 P0-3 的四组对照实验里有三组失败,而且其中两组就是"只开一半":
    faked_version 开、patch 关  → 版本过低
    faked_version 空、patch 开  → 版本过低
    faked_version 开、patch 开  → 登录成功

所以 ``options.normalize`` 的职责很明确:用户只开一半时把它补齐,并把
"我替你改了什么、怎么改回去"写进日志。但是 —— 显式写 false 的、或者
打开逃生门 require_patch_and_faker=false 的,不能强改。

同一文件里还覆盖 wechat.py 的 faker 日志修复(P1-2):
过去 fake_wechat_version 的成败只写到 wxhook 自己的 stdout,不进
logs/bridge.log,于是排障时分不清"没执行 / 执行失败 / 成功但门禁仍拦"。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from options import DEFAULT_FAKED_VERSION, normalize  # noqa: E402


class NormalizeTest(unittest.TestCase):

    def test_only_faker_enabled_gets_patch_turned_on(self):
        """报告 P0-3 里 15:31 那组失败实验的直接重现。"""
        cfg = {"faked_version": "3.9.12.51", "patch_x64_to_x32": False}
        info = normalize(cfg)
        self.assertTrue(cfg["patch_x64_to_x32"])
        self.assertEqual(cfg["faked_version"], "3.9.12.51")
        self.assertTrue(any("必须同时开" in a for a in info["adjusted"]))

    def test_missing_faked_version_is_filled_in(self):
        cfg = {"faked_version": "", "patch_x64_to_x32": True}
        info = normalize(cfg)
        self.assertEqual(cfg["faked_version"], DEFAULT_FAKED_VERSION)
        self.assertTrue(any("faked_version 为空" in a for a in info["adjusted"]))

    def test_both_already_on_changes_nothing(self):
        cfg = {"faked_version": "3.9.12.51", "patch_x64_to_x32": True}
        info = normalize(cfg)
        self.assertEqual(info["adjusted"], [])
        self.assertFalse(info["changed"])
        self.assertTrue(any("已同时开启" in n for n in info["notes"]))

    def test_escape_hatch_respects_explicit_false(self):
        cfg = {"faked_version": "", "patch_x64_to_x32": False,
               "require_patch_and_faker": False}
        info = normalize(cfg)
        self.assertFalse(cfg["patch_x64_to_x32"])
        self.assertEqual(cfg["faked_version"], "")
        self.assertEqual(info["adjusted"], [])

    def test_custom_faked_version_is_kept(self):
        cfg = {"faked_version": "4.1.15.56", "patch_x64_to_x32": False}
        normalize(cfg)
        self.assertEqual(cfg["faked_version"], "4.1.15.56")

    def test_zero_sites_is_raised(self):
        """patch_max_sites=0 等于只打 1 处,和没打一样。"""
        cfg = {"faked_version": "3.9.12.51", "patch_x64_to_x32": True,
               "patch_max_sites": 0}
        info = normalize(cfg)
        self.assertEqual(cfg["patch_max_sites"], 999)
        self.assertTrue(any("patch_max_sites" in a for a in info["adjusted"]))

    def test_string_true_is_understood(self):
        cfg = {"faked_version": "3.9.12.51", "patch_x64_to_x32": "true"}
        info = normalize(cfg)
        self.assertTrue(info["patch_x64_to_x32"])
        self.assertEqual(info["adjusted"], [])

    def test_revert_hint_is_a_pasteable_snippet(self):
        cfg = {"faked_version": "", "patch_x64_to_x32": False}
        info = normalize(cfg)
        self.assertIn("require_patch_and_faker", info["revert"])
        self.assertIn("patch_x64_to_x32", info["revert"])

    def test_half_satisfied_is_called_out(self):
        cfg = {"faked_version": "", "patch_x64_to_x32": False,
               "require_patch_and_faker": False}
        info = normalize(cfg)
        self.assertTrue(any("门禁条件不成立" in n for n in info["notes"]))


class FakerLoggingTest(unittest.TestCase):
    """P1-2:faker 返回码必须进 bridge.log。"""

    def setUp(self):
        wxhook = types.ModuleType("wxhook")
        wxhook.Bot = Mock()
        wxhook.events = types.SimpleNamespace(TEXT_MESSAGE=1, IMAGE_MESSAGE=3,
                                              XML_MESSAGE=49)
        utils = types.ModuleType("wxhook.utils")
        self.fake_wechat_version = Mock(return_value=0)
        utils.fake_wechat_version = self.fake_wechat_version
        wxhook.utils = utils
        self.modules = patch.dict(sys.modules, {"wxhook": wxhook,
                                                "wxhook.utils": utils})
        self.modules.start()
        self.addCleanup(self.modules.stop)

        spec = importlib.util.spec_from_file_location("wechat_faker_under_test",
                                                     ROOT / "wechat.py")
        self.wechat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wechat)

    def _bot(self, pid=999):
        return types.SimpleNamespace(version="3.9.5.81",
                                     process=types.SimpleNamespace(pid=pid))

    def test_success_is_recorded(self):
        side = self.wechat.WeChatSide({"faked_version": "3.9.12.51"}, lambda e: None)
        bot = self._bot()
        with patch.object(self.wechat.log, "info") as info:
            side._apply_faked_version(bot)
        self.fake_wechat_version.assert_called_once_with(999, "3.9.5.81", "3.9.12.51")
        self.assertTrue(side.faker_ok)
        calls = [(c.args[0], c.args[1:]) for c in info.call_args_list]
        # 返回码已格式化进文案里;参数里是目标版本
        self.assertTrue(any(str(call[0]).startswith("faker 返回 0") for call in calls),
                        info.call_args_list)
        self.assertTrue(any("3.9.12.51" in call[1] for call in calls),
                        "成功日志里要能看到目标版本,否则不知进没进版本")

    def test_failure_is_recorded_and_marks_flag(self):
        self.fake_wechat_version.return_value = -1
        side = self.wechat.WeChatSide({"faked_version": "3.9.12.51"}, lambda e: None)
        with patch.object(self.wechat.log, "error") as error, \
             patch.object(self.wechat.log, "info"):
            side._apply_faked_version(self._bot())
        self.assertFalse(side.faker_ok)
        # logging 是延迟格式化的:看模板里有没有「faker 返回」,以及码 -1 在不在参数里
        found = False
        for call in error.call_args_list:
            args = call.args
            if args and isinstance(args[0], str) and "faker 返回" in args[0] \
                    and -1 in args[1:]:
                found = True
        self.assertTrue(found, error.call_args_list)

    def test_no_faked_version_skips_call(self):
        side = self.wechat.WeChatSide({"faked_version": ""}, lambda e: None)
        side._apply_faked_version(self._bot())
        self.fake_wechat_version.assert_not_called()
        self.assertIsNone(side.faker_ok)

    def test_missing_pid_does_not_raise(self):
        side = self.wechat.WeChatSide({"faked_version": "3.9.12.51"}, lambda e: None)
        bot = types.SimpleNamespace(version="3.9.5.81", process=None)
        side._apply_faked_version(bot)
        self.assertFalse(side.faker_ok)
        self.fake_wechat_version.assert_not_called()

    def test_bot_constructor_does_not_receive_faked_version(self):
        """
        关键回归:如果把 faked_version 交给 Bot(),wxhook 会自己调 faker
        并且只写 stdout —— 我们要的恰恰是"自己调、自己记"。
        """
        captured = {}

        def construct(**kwargs):
            captured.update(kwargs)
            kwargs["on_start"](self._bot())
            return Mock()

        side = self.wechat.WeChatSide({"faked_version": "3.9.12.51",
                                       "patch_x64_to_x32": False}, lambda e: None)
        with patch.object(self.wechat, "Bot", side_effect=construct), \
             patch.object(side, "_start_watchdog"):
            side._run()
        self.assertIsNone(captured.get("faked_version"))
        self.fake_wechat_version.assert_called_once()

    def test_gate_summary_includes_patch_counts(self):
        side = self.wechat.WeChatSide({"faked_version": "3.9.12.51"}, lambda e: None)
        side.faker_ok = True
        side.patch_counts = {"verified": 37, "skipped_crash_site": 1,
                             "write_failed_other": 0}
        with patch.object(self.wechat.log, "info") as info:
            side._log_gate_summary()
        text = str(info.call_args_list)
        self.assertIn("37", text)
        self.assertIn("3.9.12.51", text)


class StartFailureTest(unittest.TestCase):
    """P0-2:WinError 225 / 2 必须给出可操作提示。"""

    def setUp(self):
        wxhook = types.ModuleType("wxhook")
        wxhook.Bot = Mock()
        wxhook.events = types.SimpleNamespace(TEXT_MESSAGE=1, IMAGE_MESSAGE=3,
                                              XML_MESSAGE=49)
        utils = types.ModuleType("wxhook.utils")
        utils.fake_wechat_version = Mock(return_value=0)
        wxhook.utils = utils
        self.modules = patch.dict(sys.modules, {"wxhook": wxhook,
                                                "wxhook.utils": utils})
        self.modules.start()
        self.addCleanup(self.modules.stop)

        # preflight 在 _report_start_failure 里才 import;预先塞进去,
        # 好让“不是管理员”这个分支在测试里可控
        preflight = types.ModuleType("preflight")
        preflight.is_admin = lambda: True
        self.preflight_patch = patch.dict(sys.modules, {"preflight": preflight})
        self.preflight_patch.start()
        self.addCleanup(self.preflight_patch.stop)

        spec = importlib.util.spec_from_file_location("wechat_fail_under_test",
                                                     ROOT / "wechat.py")
        self.wechat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wechat)

    def test_winerror_225_prints_exclusion_steps(self):
        side = self.wechat.WeChatSide({}, lambda e: None)
        exc = OSError(225, "无法成功完成操作,因为文件包含病毒或潜在的垃圾软件")
        with patch.object(self.wechat.log, "error") as error:
            side._report_start_failure(exc)
        text = str(error.call_args_list)
        self.assertIn("Add-MpPreference", text)
        self.assertIn("faker.exe", text)
        self.assertIn("uv cache clean wxhook", text)

    def test_winerror_2_is_treated_as_quarantined(self):
        side = self.wechat.WeChatSide({}, lambda e: None)
        exc = FileNotFoundError(2, "系统找不到指定的文件。")
        with patch.object(self.wechat.log, "error") as error:
            side._report_start_failure(exc)
        self.assertIn("Defender", str(error.call_args_list))

    def test_winerror_10043_mentions_leftover_process(self):
        side = self.wechat.WeChatSide({}, lambda e: None)
        exc = OSError(10043, "用了与要求不符的协议")
        with patch.object(self.wechat.log, "error") as error:
            side._report_start_failure(exc)
        self.assertIn("10043", str(error.call_args_list))

    def test_winerror_extraction_from_chained_exceptions(self):
        inner = OSError(225, "virus")
        outer = RuntimeError("wxhook failed")
        outer.__cause__ = inner
        self.assertEqual(self.wechat._winerror_of(outer), 225)

    def test_winerror_extraction_does_not_confuse_2_and_225(self):
        self.assertEqual(
            self.wechat._winerror_of(OSError("[WinError 2] 系统找不到指定的文件。")), 2)
        self.assertEqual(
            self.wechat._winerror_of(OSError("[WinError 225] 包含病毒")), 225)


if __name__ == "__main__":
    unittest.main(verbosity=2)
