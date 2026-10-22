# -*- coding: utf-8 -*-
"""
OneBot 段分发测试:record / file / video 必须交给"发文件"回调。

背景:听歌插件发的是 `record`(语音)段,而 wxhook 0.0.10 没有 send_voice,
所以桥接把它退化成"发一个音频文件"。这条链路以前是直接丢的:
`[API] 暂不支持的段类型: record`。

跑法:
    uv run --with websockets python -m unittest discover -s tests -p "test_ob11_*.py" -v
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ob11 import OneBotClient  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make_client(**kwargs):
    client = OneBotClient("ws://127.0.0.1:1/ws", 10000, **kwargs)
    client.ws = FakeWS()
    return client


class SegmentDispatchTest(unittest.TestCase):

    def test_record_goes_to_send_file(self):
        """放歌走的就是这条:record -> on_send_file。"""
        got = {}

        def on_file(target, raw, name, seg_type):
            got.update(target=target, raw=raw, name=name, seg_type=seg_type)

        client = make_client(on_send_file=on_file)
        asyncio.run(client._handle({
            "action": "send_group_msg",
            "params": {"group_id": 939182512,
                       "message": [{"type": "record",
                                    "data": {"file": "base64://AAAA"}}]},
            "echo": "e1",
        }))

        self.assertEqual(got["seg_type"], "record")
        self.assertEqual(got["target"], 939182512)
        self.assertEqual(got["raw"], "base64://AAAA")
        # echo 必须先回,否则 AstrBot 会超时
        self.assertEqual(len(client.ws.sent), 1)
        self.assertEqual(json.loads(client.ws.sent[0])["status"], "ok")
        self.assertEqual(json.loads(client.ws.sent[0])["echo"], "e1")

    def test_file_and_video_segments(self):
        for seg_type in ("file", "video"):
            seen = []
            client = make_client(
                on_send_file=lambda t, r, n, k: seen.append((t, r, n, k)))
            asyncio.run(client._handle({
                "action": "send_private_msg",
                "params": {"user_id": 1756005117,
                           "message": [{"type": seg_type,
                                        "data": {"file": "base64://BBBB",
                                                 "name": "song.m4a"}}]},
                "echo": "e",
            }))
            self.assertEqual(seen[0][3], seg_type)
            self.assertEqual(seen[0][2], "song.m4a")

    def test_text_and_image_still_work(self):
        texts, images = [], []
        client = make_client(on_send_text=lambda t, s: texts.append(s),
                             on_send_image=lambda t, p: images.append(p))
        asyncio.run(client._handle({
            "action": "send_private_msg",
            "params": {"user_id": 1, "message": [
                {"type": "text", "data": {"text": "你好"}},
                {"type": "image", "data": {"file": "base64://CCCC"}},
            ]},
            "echo": "e",
        }))
        self.assertEqual(texts, ["你好"])
        self.assertEqual(images, ["base64://CCCC"])

    def test_unknown_segment_does_not_crash(self):
        client = make_client()
        asyncio.run(client._handle({
            "action": "send_private_msg",
            "params": {"user_id": 1,
                       "message": [{"type": "poke", "data": {}}]},
            "echo": "e",
        }))
        # 未知段:不回调,但 echo 仍要回
        self.assertEqual(len(client.ws.sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
