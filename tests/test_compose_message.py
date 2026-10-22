# -*- coding: utf-8 -*-
"""
OneBot 消息段组装(文本 + 图片)的回归测试。

锁定两条关键规矩:
  1. at 段必须排第一位 —— AstrBot 只把"第一个 at 且是自己"当唤醒信号;
  2. 图片必须 base64:// 内嵌 —— 两个进程不共享临时目录,给路径没用。

跑法:
    uv run --with websockets python -m unittest discover -s tests -p "test_compose_*.py" -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ob11 import compose_message, make_message_event  # noqa: E402

IMG = "iVBORw0KGgoAAAANSUhEUg=="


class ComposeMessageTest(unittest.TestCase):

    def test_private_text(self):
        self.assertEqual(compose_message("你好", []),
                         [{"type": "text", "data": {"text": "你好"}}])

    def test_private_image_only(self):
        segs = compose_message("", [IMG])
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["type"], "image")
        self.assertEqual(segs[0]["data"]["file"], "base64://" + IMG)

    def test_group_at_first_then_text(self):
        segs = compose_message("签到", [], mention_self=True)
        self.assertEqual(segs[0], {"type": "at", "data": {"qq": "10000"}})
        self.assertEqual(segs[1], {"type": "text", "data": {"text": "签到"}})

    def test_group_image_keeps_at_first(self):
        """群里发图:at 必须在图片前面,否则 AstrBot 认为没被唤醒。"""
        segs = compose_message("", [IMG], mention_self=True)
        self.assertEqual(segs[0]["type"], "at")
        self.assertEqual(segs[1]["type"], "image")

    def test_text_then_images(self):
        segs = compose_message("看图", [IMG, IMG], mention_self=True)
        self.assertEqual([s["type"] for s in segs], ["at", "text", "image", "image"])

    def test_empty_image_payload_is_skipped(self):
        segs = compose_message("", ["", None])
        self.assertEqual(segs, [])

    def test_at_qq_is_string(self):
        """AstrBot 用 str() 比较 self_id,这里必须是字符串。"""
        segs = compose_message("x", None, mention_self=True, self_id=12345)
        self.assertEqual(segs[0]["data"]["qq"], "12345")

    def test_image_only_event_has_empty_raw_message(self):
        segs = compose_message("", [IMG])
        ev = make_message_event("private", 1, segs)
        self.assertEqual(ev["raw_message"], "")
        self.assertEqual(ev["message"][0]["type"], "image")

    def test_group_image_event_shape(self):
        segs = compose_message("", [IMG], mention_self=True)
        ev = make_message_event("group", 1, segs, group_id=939182512)
        self.assertEqual(ev["message_type"], "group")
        self.assertEqual(ev["group_id"], 939182512)
        self.assertEqual([s["type"] for s in ev["message"]], ["at", "image"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
