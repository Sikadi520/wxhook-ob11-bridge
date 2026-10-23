# -*- coding: utf-8 -*-
"""
微信版本检测与「防升级」的离线测试。

背景(2026-10-23):微信在后台把自己从 3.9.5.81 升到 3.9.12.55(同一天两次),
wxhook 注入失败只回 `1,unknown error`,桥接抛 `Exception: unknown error`。
版本是整个项目能不能用的前提,却从来没被检查过。

这里锁住四件事:

1. 注册表版本号解码:`0x63090551` -> `3.9.5.81`(必须和真实 FileVersion 一致);
2. **方括号版本目录要按字面量读** —— PowerShell/glob 的通配符会把 `[3.9.5.81]`
   当成字符类,静默返回 0 条。我在这台机器上真的踩过。
3. 「正在升级」的痕迹能被识别(暂存目录 / patch 工作区 / NeedUpdateType / 异版本目录);
4. start-wechat.exe 的失败文案能分出 `unknown error` / `port occupied` / `dll not found`。

全部离线:不读真实注册表、不起微信、不联网。
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import preflight  # noqa: E402
import wechat_version as wv  # noqa: E402


class VersionCodecTest(unittest.TestCase):
    """注册表 DWORD <-> 版本字符串。实测样本来自报告与真机。"""

    def test_known_samples(self):
        # 真机 HKCU\Software\Tencent\WeChat\Version
        self.assertEqual(wv.decode_version(1661535569), "3.9.5.81")
        # 报告里升级后的值
        self.assertEqual(wv.decode_version(1661537335), "3.9.12.55")
        # CrashVersion = faker 的伪装版本 3.9.12.51
        self.assertEqual(wv.decode_version(1661537331), "3.9.12.51")

    def test_hex_literals_match(self):
        self.assertEqual(wv.decode_version(0x63090551), "3.9.5.81")
        self.assertEqual(wv.decode_version(0x63090C37), "3.9.12.55")

    def test_roundtrip(self):
        for text in ("3.9.5.81", "3.9.12.55", "4.1.15.56"):
            self.assertEqual(wv.decode_version(wv.encode_version(text)), text)

    def test_garbage_returns_empty_not_a_guess(self):
        """解不出来必须返回空串 —— 报个假版本比没检测更糟。"""
        for bad in (None, "", 0, -1, "abc", 0x01020304):
            self.assertEqual(wv.decode_version(bad), "", repr(bad))

    def test_supported_version_matches_preflight(self):
        """两处常量不能漂移,否则自检和 wxhook 的认知会不一致。"""
        self.assertEqual(wv.SUPPORTED_VERSION, preflight.SUPPORTED_WECHAT_VERSION)


class VersionDirTest(unittest.TestCase):
    """`[3.9.5.81]` 这种方括号目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_literal_reading_of_bracket_dirs(self):
        """
        核心回归:用 os.scandir 按字面量读,方括号不会当通配符。

        顺带证明"裸通配符会漏"确实会发生 —— 这正是 PowerShell 里
        `Get-ChildItem 'D:\\WeChat\\[3.9.5.81]'` 静默返回 0 条的原因。
        """
        (self.root / "[3.9.5.81]").mkdir()
        (self.root / "[3.9.5.81]" / "WeChatWin.dll").write_bytes(b"x" * 10)

        found = wv.find_version_dirs(self.root)
        self.assertEqual([d["name"] for d in found], ["[3.9.5.81]"])
        self.assertFalse(found[0]["tmp"])

        # 裸方括号被当成字符类,匹配不到
        self.assertEqual(list(self.root.glob("[3.9.5.81]")), [])
        # 显式转义后才中,说明问题只在裸写法
        self.assertEqual(len(list(self.root.glob("[[]3.9.5.81[]]"))), 1)

    def test_tmp_staging_dir_detected(self):
        (self.root / "[3.9.5.81]").mkdir()
        (self.root / "[3.9.12.55]_tmp").mkdir()
        found = wv.find_version_dirs(self.root)
        tmp_dirs = [d for d in found if d["tmp"]]
        self.assertEqual([d["name"] for d in tmp_dirs], ["[3.9.12.55]_tmp"])
        self.assertEqual(tmp_dirs[0]["version"], "3.9.12.55")

    def test_non_version_dirs_ignored(self):
        (self.root / "wechat file").mkdir()
        (self.root / "[3.9.5.81]").mkdir()
        found = wv.find_version_dirs(self.root)
        self.assertEqual([d["name"] for d in found], ["[3.9.5.81]"])

    def test_missing_dir_is_safe(self):
        self.assertEqual(wv.find_version_dirs(self.root / "nope"), [])
        self.assertEqual(wv.find_version_dirs(None), [])


class InstalledVersionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _patch_reg(self, version=None, install=None, need_update=None):
        def fake_read(name):
            if name == "Version":
                return version
            if name == "NeedUpdateType":
                return need_update
            return None

        def fake_read_str(name):
            return install if name == "InstallPath" else ""

        return patch.multiple(wv, read_registry=fake_read,
                              read_registry_str=fake_read_str)

    def test_registry_wins_and_reports_source(self):
        (self.root / "[3.9.5.81]").mkdir()
        with self._patch_reg(version=1661535569, install=str(self.root)):
            info = wv.installed_wechat_version(self.root)
        self.assertEqual(info["version"], "3.9.5.81")
        self.assertEqual(info["source"], "registry")
        self.assertTrue(wv.version_ok(info))

    def test_upgraded_version_is_not_ok(self):
        """报告的核心场景:注册表已经变成 3.9.12.55。"""
        (self.root / "[3.9.12.55]").mkdir()
        with self._patch_reg(version=1661537335, install=str(self.root)):
            info = wv.installed_wechat_version(self.root)
        self.assertEqual(info["version"], "3.9.12.55")
        self.assertFalse(wv.version_ok(info))

    def test_falls_back_to_file_version_when_registry_missing(self):
        # _file_version_windows 只有在真的找到 WeChat.exe 时才被调用,
        # 所以这里得先把那个文件造出来 —— 否则测的是"没有 exe 可读",
        # 根本走不到回退分支。
        (self.root / "WeChat.exe").write_bytes(b"MZ")
        with self._patch_reg(version=None, install=str(self.root)), \
             patch.object(wv, "_file_version_windows", return_value="3.9.5.81"):
            info = wv.installed_wechat_version(self.root)
        self.assertEqual(info["version"], "3.9.5.81")
        self.assertEqual(info["source"], "fileversion")

    def test_falls_back_to_dir_name_last(self):
        (self.root / "[3.9.5.81]").mkdir()
        with self._patch_reg(version=None, install=str(self.root)), \
             patch.object(wv, "_file_version_windows", return_value=""):
            info = wv.installed_wechat_version(self.root)
        self.assertEqual(info["version"], "3.9.5.81")
        self.assertEqual(info["source"], "dirname")

    def test_nothing_readable_returns_empty(self):
        with self._patch_reg(version=None, install=str(self.root)), \
             patch.object(wv, "_file_version_windows", return_value=""):
            info = wv.installed_wechat_version(self.root)
        self.assertEqual(info["version"], "")
        self.assertFalse(wv.version_ok(info), "判定不了也不能算 OK")

    def test_dirname_fallback_ignores_tmp_dirs(self):
        """只剩 _tmp 时不能把它的版本号当成已安装版本。"""
        (self.root / "[3.9.12.55]_tmp").mkdir()
        with self._patch_reg(version=None, install=str(self.root)), \
             patch.object(wv, "_file_version_windows", return_value=""):
            info = wv.installed_wechat_version(self.root)
        self.assertEqual(info["version"], "")


class UpdateTracesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_clean_machine_has_no_traces(self):
        (self.root / "[3.9.5.81]").mkdir()
        with patch.object(wv, "read_registry", return_value=0):
            traces = wv.update_traces(self.root, appdata=self.root / "appdata")
        self.assertEqual(traces["tmp_dirs"], [])
        self.assertEqual(traces["suspicious_version_dirs"], [])
        self.assertEqual(traces["patch_dir"], "")

    def test_tmp_dir_and_suspicious_version_reported(self):
        (self.root / "[3.9.5.81]").mkdir()
        (self.root / "[3.9.12.55]_tmp").mkdir()
        (self.root / "[3.9.12.55]").mkdir()
        with patch.object(wv, "read_registry", return_value=200):
            traces = wv.update_traces(self.root, appdata=self.root / "appdata")
        self.assertEqual(traces["tmp_dirs"], ["[3.9.12.55]_tmp"])
        self.assertIn("[3.9.12.55]", traces["suspicious_version_dirs"])
        self.assertNotIn("[3.9.5.81]", traces["suspicious_version_dirs"])
        self.assertEqual(traces["need_update_type"], 200)

    def test_plugin_dir_detected(self):
        appdata = self.root / "appdata"
        plugin = appdata / "Tencent" / "WeChat" / "XPlugin" / "Plugins" / "WeChatUpdate"
        plugin.mkdir(parents=True)
        (self.root / "[3.9.5.81]").mkdir()
        with patch.object(wv, "read_registry", return_value=0):
            traces = wv.update_traces(self.root, appdata=appdata)
        self.assertEqual(traces["plugin_dir"], str(plugin))


class RecoverHintTest(unittest.TestCase):
    """恢复指引必须包含有效手段,并明确点出那三步是无效的。"""

    def test_mentions_acl_blockade_and_deny_flag(self):
        text = wv.recover_hint(r"D:\WeChat")
        self.assertIn("icacls", text)
        self.assertIn("(WD,AD,DC)", text)
        self.assertIn(r"D:\WeChat", text)

    def test_warns_against_the_three_useless_steps(self):
        text = wv.recover_hint()
        for needle in ("NeedUpdateType", "WeChatUpdate.exe", "有更新时自动升级"):
            self.assertIn(needle, text, "必须在文档里明确说这条无效")

    def test_supported_version_is_substituted(self):
        self.assertIn("3.9.5.81", wv.recover_hint())
        self.assertNotIn("{supported}", wv.recover_hint())


def _load_wechat():
    wxhook = types.ModuleType("wxhook")
    wxhook.Bot = object
    wxhook.events = types.SimpleNamespace()
    utils = types.ModuleType("wxhook.utils")
    utils.fake_wechat_version = lambda *a: 0
    wxhook.utils = utils
    patcher = patch.dict(sys.modules, {"wxhook": wxhook, "wxhook.utils": utils,
                                       "preflight": preflight})
    patcher.start()
    spec = importlib.util.spec_from_file_location("wechat_ver_under_test",
                                                  ROOT / "wechat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, patcher


class StartErrorClassifyTest(unittest.TestCase):
    """start-wechat.exe 的失败文案分类(报告第 4 节 P1)。"""

    def setUp(self):
        self.wechat, self._patcher = _load_wechat()
        self.addCleanup(self._patcher.stop)

    def test_unknown_error_is_its_own_bucket(self):
        kind, text = self.wechat._classify_start_error(Exception("unknown error"))
        self.assertEqual(kind, "unknown_client")
        self.assertEqual(text, "unknown error")

    def test_port_occupied(self):
        self.assertEqual(
            self.wechat._classify_start_error(Exception("port occupied:19001"))[0],
            "port_occupied")

    def test_dll_missing(self):
        self.assertEqual(
            self.wechat._classify_start_error(Exception("dll not found"))[0],
            "dll_missing")

    def test_unknown_text_is_not_misread(self):
        self.assertEqual(
            self.wechat._classify_start_error(Exception("something else"))[0],
            "unknown")

    def test_empty(self):
        self.assertEqual(self.wechat._classify_start_error(Exception(""))[0], "empty")


class StartFailureVersionReportTest(unittest.TestCase):
    """版本不对时,_report_start_failure 要点名版本并给出恢复路径。"""

    def setUp(self):
        self.wechat, self._patcher = _load_wechat()
        self.addCleanup(self._patcher.stop)
        self.side = self.wechat.WeChatSide({}, lambda e: None)

    def test_upgraded_client_points_at_version(self):
        info = {"version": "3.9.12.55", "source": "registry",
                "install_dir": r"D:\WeChat",
                "dirs": [{"name": "[3.9.12.55]", "version": "3.9.12.55",
                          "tmp": False}],
                "ok": False}
        with patch.object(self.side, "_describe_installed_version",
                          return_value=info), \
             patch.object(self.wechat, "log") as lg, \
             patch.object(wv, "update_traces", return_value={
                 "tmp_dirs": [], "patch_dir": "", "need_update_type": 0,
                 "plugin_dir": "", "suspicious_version_dirs": []}):
            self.side._report_start_failure(Exception("unknown error"))
        text = str(lg.error.call_args_list)
        self.assertIn("3.9.12.55", text)
        self.assertIn("D:\\\\WeChat", text)
        self.assertIn("icacls", text, "必须直接给封锁命令")

    def test_unknown_error_after_version_ok_lists_remaining_causes(self):
        info = {"version": "3.9.5.81", "source": "registry",
                "install_dir": r"D:\WeChat", "dirs": [], "ok": True}
        with patch.object(self.side, "_describe_installed_version",
                          return_value=info), \
             patch.object(self.wechat, "log") as lg:
            self.side._report_start_failure(Exception("unknown error"))
        text = str(lg.error.call_args_list)
        self.assertIn("仍在运行", text)
        self.assertIn("安装目录", text)

    def test_port_occupied_text_gives_actionable_hint(self):
        with patch.object(self.side, "_describe_installed_version",
                          return_value={"version": "3.9.5.81", "ok": True,
                                        "install_dir": "", "dirs": []}), \
             patch.object(self.wechat, "log") as lg:
            self.side._report_start_failure(Exception("port occupied:19001"))
        self.assertIn("完全退出微信", str(lg.error.call_args_list))


if __name__ == "__main__":
    unittest.main(verbosity=2)
