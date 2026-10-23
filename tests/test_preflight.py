# -*- coding: utf-8 -*-
"""
启动自检的离线测试(不碰网络、不碰微信、不执行任何 exe)。

对应 2026-10-23 排障报告里的两条 P0:

  P0-1  AstrBot 3.x 是 11229、4.x 默认 6199,写死哪个都会让另一拨人连不上。
        所以 ``resolve_ob_url`` 必须:显式写了端口 → 照用;没写 → 探测。
  P0-2  faker.exe 被 Defender 删掉后,微信根本起不来,而报错是含糊的
        WinError 225 / 2。``inspect_tools`` 必须能把"文件不在"和
        "文件在但读不出来(被实时保护拦)"分开报。
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import preflight  # noqa: E402

PORT_11229 = 11229
PORT_6199 = 6199


def only(*open_ports):
    """造一个 port_in_use:只有列出的端口算"在监听"。

    注意:resolve_ob_url 用【位置参数】调 probe(port, host),所以这里
    第一个形参必须就叫 port —— 用一个不同名的形参会导致 "port" 成了 host。
    """
    open_set = set(open_ports)

    def probe(port, host="127.0.0.1", timeout=0.25):
        return int(port) in open_set

    return probe


class ResolveObUrlTest(unittest.TestCase):

    def test_explicit_port_is_never_overridden(self):
        url = "ws://127.0.0.1:11229/ws"
        got, note = preflight.resolve_ob_url(url, only(PORT_6199))
        self.assertEqual(got, url, "用户明确写了端口就必须照用")
        self.assertIn("11229", note)

    def test_explicit_port_not_listening_still_kept(self):
        """端口写错了也不该偷偷换 —— 那会让日志和实际连接对不上。"""
        url = "ws://127.0.0.1:9999/ws"
        got, note = preflight.resolve_ob_url(url, only(PORT_6199))
        self.assertEqual(got, url)
        self.assertIn("9999", note)

    def test_no_port_picks_6199_when_only_astrbot4_is_up(self):
        """报告 P0-1 的核心:没写端口时,4.x 的 6199 必须被自动选中。"""
        got, note = preflight.resolve_ob_url("ws://127.0.0.1/ws", only(PORT_6199))
        self.assertEqual(got, "ws://127.0.0.1:6199/ws")
        self.assertIn("6199", note)

    def test_no_port_picks_11229_when_only_astrbot3_is_up(self):
        got, _ = preflight.resolve_ob_url("ws://127.0.0.1/ws", only(PORT_11229))
        self.assertEqual(got, "ws://127.0.0.1:11229/ws")

    def test_both_up_prefers_11229_for_backward_compat(self):
        got, note = preflight.resolve_ob_url("ws://127.0.0.1/ws",
                                             only(PORT_11229, PORT_6199))
        self.assertEqual(got, "ws://127.0.0.1:11229/ws")
        self.assertIn("6199", note, "另一个端口也要提一句,便于排障")

    def test_nothing_listening_keeps_original_and_explains(self):
        got, note = preflight.resolve_ob_url("ws://127.0.0.1/ws", only())
        self.assertEqual(got, "ws://127.0.0.1/ws")
        self.assertIn("11229", note)
        self.assertIn("6199", note)

    def test_localhost_and_path_are_preserved(self):
        got, _ = preflight.resolve_ob_url("ws://localhost/ws", only(PORT_6199))
        self.assertEqual(got, "ws://localhost:6199/ws")

    def test_empty_url_is_not_rewritten(self):
        got, note = preflight.resolve_ob_url("", only(PORT_6199))
        self.assertEqual(got, "")
        self.assertIn("astrbot_ob_url", note)

    def test_real_socket_probe_finds_a_listening_port(self):
        """真开一个 socket,确认探测函数本身是对的(不走 mock)。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(preflight.port_in_use(port))
            # 显式写了端口且确实在监听 → 原样返回(这里用一个随机端口的 URL)
            url = "ws://127.0.0.1:%d/ws" % port
            got, note = preflight.resolve_ob_url(url, preflight.port_in_use)
            self.assertEqual(got, url)
            self.assertIn(str(port), note)
        finally:
            srv.close()
        self.assertFalse(preflight.port_in_use(port))


class ToolsInspectionTest(unittest.TestCase):
    """P0-2:faker.exe 被杀软拦截。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wx = Path(self.tmp.name) / "wxhook"
        self.tools = self.wx / "tools"
        self.tools.mkdir(parents=True)
        (self.wx / "core.py").write_text("", encoding="utf-8")

    def _put(self, name, data=b"MZ"):
        p = self.tools / name
        p.write_bytes(data)
        return p

    def test_all_present_and_hashed(self):
        for name in preflight.WXHOOK_TOOLS:
            self._put(name)
        out = preflight.inspect_tools(self.wx)
        self.assertTrue(out["ok"])
        self.assertEqual(out["problems"], [])
        for name in preflight.WXHOOK_TOOLS:
            item = out["files"][name]
            self.assertTrue(item["exists"])
            self.assertEqual(len(item["sha256"]), 64)

    def test_missing_faker_is_reported_with_reason(self):
        self._put("start-wechat.exe")
        self._put("wxhook.dll")
        out = preflight.inspect_tools(self.wx)
        self.assertFalse(out["ok"])
        self.assertTrue(any("faker.exe" in p and "不存在" in p for p in out["problems"]))

    def test_unreadable_faker_is_distinguished_from_missing(self):
        """
        被 Defender 实时保护拦住的现场:文件还在,连 get_file_hash 都会失败。
        这两种要分开报 —— 处理办法不同(重装 vs 加排除)。
        """
        self._put("start-wechat.exe")
        self._put("wxhook.dll")
        faker = self._put("faker.exe")
        with patch.object(preflight, "file_sha256",
                          side_effect=PermissionError("virus detected")):
            out = preflight.inspect_tools(self.wx)
        self.assertFalse(out["ok"])
        item = out["files"]["faker.exe"]
        self.assertTrue(item["exists"])
        self.assertFalse(item.get("sha256"))
        self.assertTrue(any("读不出来" in p for p in out["problems"]))
        self.assertTrue(faker.exists())

    def test_defender_hint_mentions_both_exclusions(self):
        self.assertIn("Add-MpPreference", preflight.DEFENDER_HINT)
        self.assertIn("faker.exe", preflight.DEFENDER_HINT)
        self.assertIn("start-wechat.exe", preflight.DEFENDER_HINT)


class PortStateTest(unittest.TestCase):
    """wxhook.json 的端口账本。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wx = Path(self.tmp.name) / "wxhook"
        (self.wx / "tools").mkdir(parents=True)
        (self.wx / "core.py").write_text("", encoding="utf-8")

    def test_absent_state_file(self):
        info = preflight.read_port_state(self.wx)
        self.assertFalse(info["exists"])

    def test_reads_next_ports_and_stale_pids(self):
        (self.wx / "tools" / "wxhook.json").write_text(json.dumps({
            "increase_remote_port": 19001,
            "wechat": [{"pid": 424242, "remote_port": 19001, "server_port": 18999}],
        }), encoding="utf-8")
        with patch.object(preflight, "running_wechat_pids", return_value=[]):
            info = preflight.read_port_state(self.wx)
        self.assertTrue(info["exists"])
        self.assertEqual(info["next_remote_port"], 19002)
        self.assertEqual(info["next_server_port"], 18998)
        self.assertEqual(info["stale_pids"], [424242])

    def test_reset_removes_state_file(self):
        state = self.wx / "tools" / "wxhook.json"
        state.write_text("{}", encoding="utf-8")
        notes = preflight.reset_port_state(self.wx)
        self.assertFalse(state.exists())
        self.assertTrue(any("删除" in n for n in notes))
        # 再删一次不应该抛
        preflight.reset_port_state(self.wx)


class ReportTest(unittest.TestCase):
    """run() 的整体行为:只读、不写配置、问题分级别。"""

    def test_report_flags_missing_astrbot_and_keeps_cfg_untouched(self):
        cfg = {"astrbot_ob_url": "ws://127.0.0.1/ws"}
        with patch.object(preflight, "port_in_use", only()), \
             patch.object(preflight, "find_wxhook_dirs", return_value=[]), \
             patch.object(preflight, "running_wechat_pids", return_value=[]), \
             patch.object(preflight, "is_admin", return_value=True):
            report = preflight.run(cfg)
        self.assertFalse(report["problems"] == [])
        self.assertTrue(any("AstrBot" in p for p in report["problems"]))
        self.assertEqual(cfg, {"astrbot_ob_url": "ws://127.0.0.1/ws"})

    def test_report_notes_running_wechat_as_warning_not_error(self):
        """
        "微信在跑" 是提醒,不是错误。

        这条以前只在开发机上过 —— 因为它依赖跑测试的那台机器恰好装好了
        wxhook、而且恰好是 Windows。CI 的 Linux runner 两样都没有:

        * ``find_wxhook_dirs`` 返回空 -> run() 多报一条真问题
          「没找到 wxhook 包」;而 run() 的规则是「已经有问题就不再加
          这条提醒」(否则真问题会被重复警告淹了),于是警告被压掉;
        * ``_check_wechat_version`` 读不到注册表 -> 又多一条问题。

        结果这条测试变成在测 "runner 上装了什么",而不是 "微信在跑会怎样"。
        把这两处外部检查钉住,断言才只关于行为本身。
        """
        wxhook_dir = Path(tempfile.mkdtemp(prefix="wxhook-dirs-"))
        self.addCleanup(shutil.rmtree, wxhook_dir, ignore_errors=True)
        (wxhook_dir / "tools").mkdir()

        with patch.object(preflight, "port_in_use", only(PORT_6199)), \
             patch.object(preflight, "running_wechat_pids", return_value=[1234]), \
             patch.object(preflight, "is_admin", return_value=True), \
             patch.object(preflight, "find_wxhook_dirs", return_value=[wxhook_dir]), \
             patch.object(preflight, "inspect_tools",
                          return_value={"ok": True, "problems": [], "files": {}}), \
             patch.object(preflight, "_check_wechat_version",
                          return_value={"ok": True, "version": "3.9.5.81"}):
            report = preflight.run({"astrbot_ob_url": "ws://127.0.0.1/ws"})
        self.assertEqual(report["ob_url"], "ws://127.0.0.1:6199/ws")
        self.assertEqual(report["problems"], [], report)
        self.assertTrue(any("微信已经在运行" in w for w in report["warnings"]), report)

    def test_running_wechat_is_quiet_when_a_real_problem_exists(self):
        """真正的问题(连不上 AstrBot)不能被两三条重复警告淹了。"""
        with patch.object(preflight, "port_in_use", only()), \
             patch.object(preflight, "find_wxhook_dirs", return_value=[]), \
             patch.object(preflight, "running_wechat_pids", return_value=[1234]), \
             patch.object(preflight, "is_admin", return_value=True):
            report = preflight.run({"astrbot_ob_url": "ws://127.0.0.1/ws"})
        self.assertFalse(any("微信已经在运行" in w for w in report["warnings"]))
        self.assertTrue(report["problems"])

    def test_non_admin_is_a_warning(self):
        with patch.object(preflight, "port_in_use", only(PORT_6199)), \
             patch.object(preflight, "find_wxhook_dirs", return_value=[]), \
             patch.object(preflight, "running_wechat_pids", return_value=[]), \
             patch.object(preflight, "is_admin", return_value=False):
            report = preflight.run({"astrbot_ob_url": "ws://127.0.0.1/ws"})
        self.assertTrue(any("管理员" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
