# -*- coding: utf-8 -*-
"""
桥接"微信图片 -> OneBot 事件"这段胶水的离线测试。

真 wxhook 会把微信进程拉起来并注入 DLL,单测里绝不能碰,所以先往
sys.modules 里塞一个假的 wxhook,再 import bridge。

覆盖:
  - 私聊图片 -> 单个 image 段
  - 群图片   -> at 段在第一位(否则 AstrBot 不认唤醒)
  - 同一缓冲里 文本+图片 -> at / text / image 顺序
  - 没有 base64Img 的图片事件 -> 不推空事件
  - 白名单外 -> 丢弃

跑法:
    uv run --with websockets python -m unittest discover -s tests -p "test_bridge_image*.py" -v
"""
import base64
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- 假 wxhook(必须在 import bridge 之前装好)----
_fake = types.ModuleType("wxhook")
_fake.events = types.SimpleNamespace(TEXT_MESSAGE=1, IMAGE_MESSAGE=3)


class _NeverBot:  # 单测里不应该真的构造 Bot
    def __init__(self, *a, **kw):
        raise RuntimeError("单测里不该构造真的 wxhook.Bot")


_fake.Bot = _NeverBot
sys.modules.setdefault("wxhook", _fake)

import bridge as bridge_mod  # noqa: E402

IMG = "iVBORw0KGgoAAAANSUhEUg=="
CFG = {
    "astrbot_ob_url": "ws://127.0.0.1:11229/ws",
    "self_wxid": "",
    "allowed_wxids": [],
    "bot_nicknames": [],
    "group_reply_mode": "mention",
    "buffer_seconds": 2,
    "send_interval": 0,
}


class FakeEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeOneBot:
    def __init__(self):
        self.sent = []

    def push_event(self, ev):
        self.sent.append(ev)
        return True


class FakeDllBot:
    """假 wxhook.Bot,只实现 download_attachment / decode_image。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def download_attachment(self, msg_id):
        self.calls.append(("download_attachment", msg_id))
        return types.SimpleNamespace(code=200, msg="ok", data=self.payload)

    def decode_image(self, path, store_dir):
        self.calls.append(("decode_image", path))
        return types.SimpleNamespace(code=200, msg="ok", data={})


class BridgeImageFlowTest(unittest.TestCase):

    def setUp(self):
        # wxid_map.json 是运行时的真实文件,测试里别写它
        self._orig_save = bridge_mod.Bridge._save_map
        bridge_mod.Bridge._save_map = lambda self: None
        self.b = bridge_mod.Bridge(dict(CFG))
        self.b.ob = FakeOneBot()

    def tearDown(self):
        bridge_mod.Bridge._save_map = self._orig_save

    def _feed_and_emit(self, ev):
        self.b._on_wechat_image(ev)
        buf = self.b.buffers.get(ev.fromUser)
        if buf:
            self.b._emit(buf)
            self.b.buffers.pop(ev.fromUser, None)
        return self.b.ob.sent

    def test_private_image(self):
        sent = self._feed_and_emit(
            FakeEvent(fromUser="wxid_test_img_a", base64Img=IMG,
                      content=None, msgId=1))
        self.assertEqual(len(sent), 1)
        ev = sent[0]
        self.assertEqual(ev["message_type"], "private")
        self.assertEqual(len(ev["message"]), 1)
        self.assertEqual(ev["message"][0]["type"], "image")
        self.assertEqual(ev["message"][0]["data"]["file"], "base64://" + IMG)

    def test_group_image_puts_at_first(self):
        room = "999001@chatroom"
        sent = self._feed_and_emit(
            FakeEvent(fromUser=room, base64Img=IMG,
                      content="wxid_sender_x:\n", msgId=2))
        self.assertEqual(len(sent), 1)
        ev = sent[0]
        self.assertEqual(ev["message_type"], "group")
        self.assertEqual(ev["group_id"], zlib.crc32(room.encode()) & 0x7FFFFFFF)
        self.assertEqual(ev["message"][0]["type"], "at")
        self.assertEqual(ev["message"][0]["data"]["qq"], "10000")
        self.assertEqual(ev["message"][1]["type"], "image")

    def test_text_plus_image_order(self):
        room = "999002@chatroom"
        self.b._on_wechat_message(FakeEvent(fromUser=room, content="@" + "\u5c0f\u52a9\u624b\u2005\u770b\u8fd9\u5f20", msgId=3))
        self.b._on_wechat_image(FakeEvent(fromUser=room, base64Img=IMG, content=None, msgId=4))
        buf = self.b.buffers[room]
        self.b._emit(buf)
        ev = self.b.ob.sent[0]
        self.assertEqual([s["type"] for s in ev["message"]], ["at", "text", "image"])
        self.assertEqual(ev["message"][1]["data"]["text"], "\u770b\u8fd9\u5f20")

    def test_image_without_payload_is_dropped(self):
        sent = self._feed_and_emit(
            FakeEvent(fromUser="wxid_test_img_b", base64Img=None,
                      content="some-xml", msgId=5))
        self.assertEqual(sent, [])

    def test_whitelist_blocks_image(self):
        self.b.cfg["allowed_wxids"] = ["wxid_only_me"]
        sent = self._feed_and_emit(
            FakeEvent(fromUser="wxid_test_img_c", base64Img=IMG,
                      content=None, msgId=6))
        self.assertEqual(sent, [])

    # ---- 群里没有 base64Img 时的兜底链路 ----

    def test_group_image_falls_back_to_download_attachment(self):
        raw = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        path = os.path.join(tempfile.mkdtemp(prefix="wxtest_"), "g.png")
        with open(path, "wb") as f:
            f.write(raw)

        dll = FakeDllBot({"filePath": path})
        self.b.wx.bot = dll
        sent = self._feed_and_emit(
            FakeEvent(fromUser="999003@chatroom", base64Img=None,
                      content=None, msgId=7, type=3))

        self.assertEqual(len(sent), 1)
        msg = sent[0]["message"]
        self.assertEqual([s["type"] for s in msg], ["at", "image"])
        self.assertEqual(msg[1]["data"]["file"],
                         "base64://" + base64.b64encode(raw).decode("ascii"))
        self.assertEqual(dll.calls[0], ("download_attachment", 7))

    def test_download_attachment_without_path_is_dropped(self):
        self.b.wx.bot = FakeDllBot({"whatever": 1})
        sent = self._feed_and_emit(
            FakeEvent(fromUser="999004@chatroom", base64Img=None,
                      content=None, msgId=8))
        self.assertEqual(sent, [])

    def test_pick_path_variants(self):
        pick = bridge_mod.WeChatSide._pick_path
        self.assertEqual(pick("C:\\a.png"), "C:\\a.png")
        self.assertEqual(pick({"filePath": "C:\\b.png"}), "C:\\b.png")
        self.assertEqual(pick({"imagePath": "C:\\c.png"}), "C:\\c.png")
        self.assertEqual(pick({"data": {"path": "C:\\d.png"}}), "C:\\d.png")
        self.assertEqual(pick({}), "")
        self.assertEqual(pick(None), "")
        self.assertEqual(pick({"whatever": 1}), "")

    # ---- 放歌:record 段落成文件(wxhook 没有 send_voice) ----

    def test_audio_suffix_detection(self):
        guess = bridge_mod.Bridge._guess_audio_suffix
        self.assertEqual(guess(b"....ftypM4A \x00"), ".m4a")
        self.assertEqual(guess(b"ID3\x04\x00\x00"), ".mp3")
        self.assertEqual(guess(b"\xff\xfb\x90\x00"), ".mp3")
        self.assertEqual(guess(b"#!AMR\n\x00"), ".amr")
        self.assertEqual(guess(b"RIFF\x00\x00\x00\x00WAVEfmt "), ".wav")
        self.assertEqual(guess(b"OggS\x00\x02\x00\x00"), ".ogg")
        self.assertEqual(guess(b"fLaC\x00\x00"), ".flac")
        self.assertEqual(guess(b"\x00\x00\x00\x00garbage"), ".m4a")

    def test_materialize_record_base64(self):
        raw = b"....ftypM4A " + bytes(32)
        ref = "base64://" + base64.b64encode(raw).decode("ascii")
        path = bridge_mod.Bridge._materialize_file(ref)
        try:
            self.assertTrue(path.endswith(".m4a"), path)
            self.assertEqual(Path(path).read_bytes(), raw)
        finally:
            os.remove(path)

    def test_send_file_uses_wxhook_send_file(self):
        calls = []

        class FakeWx:
            def send_file(self, wxid, path):
                calls.append((wxid, Path(path).read_bytes(), path))

        self.b._register("wxid_test_audio")     # 建立 id -> wxid 映射
        ob_id = self.b.wxid_to_id["wxid_test_audio"]
        self.b.wx = FakeWx()
        raw = b"....ftypM4A " + bytes(16)
        self.b._send_file(ob_id,
                          "base64://" + base64.b64encode(raw).decode("ascii"),
                          None, "record")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "wxid_test_audio")
        self.assertEqual(calls[0][1], raw)

        # 关键回归:发完不能立刻删(2026-10-18 09:15 的"发送中断")
        self.assertTrue(os.path.exists(calls[0][2]), "临时文件被提前删掉了")

        # 清理是有计划的,不是不管
        self.b._cleanup_later(calls[0][2], delay=0.05)
        time.sleep(0.3)
        self.assertFalse(os.path.exists(calls[0][2]), "延迟清理没生效")


    # ---- 竞态:DLL 先返回、后落盘(2026-10-18 08:08 实测) ----

    def test_group_image_survives_async_write_race(self):
        """download_attachment 返回时文件还没写出来,必须等一下才拿到图。"""
        raw = b"\xff\xd8\xff\xe0race-bytes"
        d = tempfile.mkdtemp(prefix="wxtest_race_")
        msg_id = 4597000000000000123
        target = os.path.join(d, f"{msg_id}.dat")

        class RacingBot(FakeDllBot):
            IMAGE_SAVE_PATH = d
            DATA_SAVE_PATH = d

            def download_attachment(self, mid):
                # 复刻真 DLL:先返回 success(data=None),稍后才写盘
                threading.Thread(target=self._late_write, daemon=True).start()
                return types.SimpleNamespace(code=0, msg="success", data=None)

            def _late_write(self):
                time.sleep(0.3)
                with open(target, "wb") as f:
                    f.write(raw)

        self.b.wx.bot = RacingBot({})
        self.b.wx.image_wait_seconds = 3

        sent = self._feed_and_emit(
            FakeEvent(fromUser="999005@chatroom", base64Img=None,
                      content=None, msgId=msg_id))

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"][1]["data"]["file"],
                         "base64://" + base64.b64encode(raw).decode("ascii"))

    def test_wait_times_out_when_nothing_lands(self):
        d = tempfile.mkdtemp(prefix="wxtest_none_")
        dll = FakeDllBot({})
        dll.IMAGE_SAVE_PATH = d
        self.b.wx.bot = dll
        t0 = time.time()
        got = self.b.wx._wait_for_downloaded_image(888, timeout=0.3)
        self.assertEqual(got, "")
        self.assertLess(time.time() - t0, 2.0)

    # ---- 大图分块落盘(2026-10-18 08:53 实测) ----

    def test_chunk_file_is_skipped_until_real_image_appears(self):
        """
        DLL 下大图时会先落一个 <msgId>.dat<hex> 的 512KB 分块(不是图片)。
        必须跳过它,等真正的 <msgId>.dat 出现 —— 否则模型会 400
        "You have uploaded an unsupported image"。
        """
        d = tempfile.mkdtemp(prefix="wxtest_chunk_")
        msg = 5200000000000001
        chunk = os.path.join(d, f"{msg}.datdeadbeefcafe")
        real = os.path.join(d, f"{msg}.dat")
        with open(chunk, "wb") as f:
            f.write(b"\x00" * 4096)                 # 分块文件:不是合法图片

        def _writer():
            time.sleep(0.3)
            with open(real, "wb") as f:
                f.write(b"\xff\xd8\xff\xe1" + b"real-image" * 20)

        threading.Thread(target=_writer, daemon=True).start()

        dll = FakeDllBot({})
        dll.IMAGE_SAVE_PATH = d
        self.b.wx.bot = dll

        got = self.b.wx._wait_for_downloaded_image(msg, timeout=3)
        self.assertEqual(got, real)

    def test_invalid_bytes_at_exact_name_are_rejected(self):
        d = tempfile.mkdtemp(prefix="wxtest_bad_")
        msg = 5200000000000002
        with open(os.path.join(d, f"{msg}.dat"), "wb") as f:
            f.write(b"\x00" * 1024)                 # 空壳,不是图片

        dll = FakeDllBot({})
        dll.IMAGE_SAVE_PATH = d
        self.b.wx.bot = dll

        got = self.b.wx._wait_for_downloaded_image(msg, timeout=0.4)
        self.assertEqual(got, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
