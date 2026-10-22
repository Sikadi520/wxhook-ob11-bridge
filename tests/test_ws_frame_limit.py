"""单帧上限回归测试(离线:不连 AstrBot、不碰微信)。

背景(线上日志):
    WARNING 连接断开失败(sent 1009 (message too big) frame with 1312263 bytes
            exceeds limit of 1048576 bytes, no close frame received),5 秒后重连

AstrBot 的 aiocqhttp 适配器会把图片段统一转成 base64:// 内嵌,所以"发一张
高清原图"这条 send_private_msg 的 JSON 轻松超过 websockets 库的默认
max_size = 1 MiB。超限时不是截断,而是关掉整条连接 —— 图丢了、连接也断了。

本测试本地起一个假 AstrBot(websockets 服务端),按线上同尺寸
(1312263 字节,与日志一致)发一条内嵌 base64 图片的 send_private_msg:

  - 用默认 1 MiB 连接  → 必须复现 1009 断开(证明这就是线上那个报错的原因)
  - 用 ob11 现在的上限  → 必须完整收到并落地,连接保持存活(证明修复有效)

运行(与本项目其它测试一致,用 uv 带上依赖):
    uv run --with websockets python -m unittest discover -s tests -v
"""
import asyncio
import base64
import json
import logging
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402
import ob11  # noqa: E402

LOG = logging.getLogger("ws-frame-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

# 与线上日志里的字节数完全一致,便于对照
BIG_FRAME_BYTES = 1312263
DEFAULT_WS_MAX_SIZE = 2 ** 20  # websockets 库的默认值


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_big_send_call(target_bytes: int = BIG_FRAME_BYTES) -> str:
    """造一条与线上同尺寸的 send_private_msg(图片以 base64 内嵌)。"""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 900_000
    b64 = base64.b64encode(png).decode("ascii")
    msg = {
        "action": "send_private_msg",
        "params": {
            "user_id": 1756005117,
            "message": [{"type": "image", "data": {"file": "base64://" + b64}}],
        },
        "echo": "frame-limit-test",
    }
    raw = json.dumps(msg, ensure_ascii=False)
    pad = target_bytes - len(raw.encode("utf-8"))
    if pad < 0:
        raise AssertionError("构造的消息已经超过目标尺寸,请调小图片字节数")
    # base64 串末尾补 'A':不改语义,正好把整条消息顶到目标字节数
    msg["params"]["message"][0]["data"]["file"] = "base64://" + b64 + "A" * pad
    raw = json.dumps(msg, ensure_ascii=False)
    assert len(raw.encode("utf-8")) == target_bytes
    return raw


class FakeAstrBot:
    """只做一件事:每来一个连接,就发一条超限的 send_private_msg。"""

    def __init__(self):
        self.port = _free_port()
        self.raw = build_big_send_call()
        self._loop = None
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.connections = 0

    @property
    def url(self) -> str:
        return "ws://127.0.0.1:%d/ws" % self.port

    async def _handler(self, ws):
        self.connections += 1
        await ws.send(self.raw)
        # 留一点时间让对方处理(它会回一条 echo 响应)
        await asyncio.sleep(2.0)

    def start(self):
        self._thread.start()
        for _ in range(100):
            if self._server is not None:
                return
            threading.Event().wait(0.05)
        raise RuntimeError("假 AstrBot 启动超时")

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def _boot():
            # 服务端侧不设限:AstrBot 发出这个消息时本来就不受 max_size 约束,
            # max_size 限制的只是【接收方】。
            self._server = await websockets.serve(
                self._handler, "127.0.0.1", self.port, max_size=None)

        loop.run_until_complete(_boot())
        loop.run_forever()

    def stop(self):
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


class FrameLimitTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = FakeAstrBot()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_default_limit_reproduces_1009(self):
        """默认 1 MiB → 复现线上那句 1009,证明根因是接收方的 max_size。"""
        async def _recv():
            async with websockets.connect(self.server.url,
                                          max_size=DEFAULT_WS_MAX_SIZE) as ws:
                async for _ in ws:
                    pass

        with self.assertRaises(websockets.exceptions.ConnectionClosed) as cm:
            asyncio.run(_recv())
        text = str(cm.exception)
        self.assertIn("1009", text, text)
        self.assertIn("exceeds limit of %d bytes" % DEFAULT_WS_MAX_SIZE, text, text)
        # 字节数只在旧实现(websockets <= 13)的措辞里出现,不作为硬断言
        LOG.info("默认上限下的报错: %s", text)

    def test_raised_limit_delivers_and_stays_connected(self):
        """用 ob11 现在的上限 → 同一帧能收到,并且连接不再被掐断。"""
        got = {}
        done = threading.Event()

        def on_send_image(ob_id, raw):
            got["ob_id"] = ob_id
            got["payload_len"] = len(raw)
            got["prefix"] = raw[:9]
            done.set()

        client = ob11.OneBotClient(
            self.server.url, 10000,
            on_send_text=lambda *_a: None,
            on_send_image=on_send_image,
            max_size=ob11.DEFAULT_MAX_SIZE,
        )
        client.start()
        self.assertTrue(client.ready.wait(10), "客户端未能连上假 AstrBot")

        self.assertTrue(done.wait(15), "超限的那条消息没有被处理")
        self.assertEqual(got["prefix"], "base64://")
        # 落地的载荷 = 整条帧去掉 JSON 外壳,量级必须在 1 MiB 以上
        self.assertGreater(got["payload_len"], DEFAULT_WS_MAX_SIZE)
        # 关键:处理完这条大帧之后连接仍然活着(旧代码此时已经断了)
        self.assertIsNotNone(client.ws, "连接被掐断了")
        self.assertTrue(client.ready.is_set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
