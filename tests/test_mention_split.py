# -*- coding: utf-8 -*-
"""
微信群 @ 前缀解析的回归测试(离线,不需要 wxhook / 微信 / AstrBot)。

对应故障:群里 `/签到` 不执行、私聊正常。根因是微信用 U+2005 分隔昵称和
正文,桥接只按 ASCII 空格切,正文里留着 "@小助手\u2005",AstrBot 因此看不到
以 "/" 开头的指令。

跑法:
    python -m unittest discover -s tests -p "test_mention_split.py" -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mentions import split_mention  # noqa: E402

# 线上实测到的原始文本(wxhook 事件里的 content,原样照抄)
REAL_GROUP_MSG = "@\u5c0f\u52a9\u624b\u2005\u6211\u7684\u63d2\u4ef6\u529f\u80fd\u5462?\u5728\u7fa4\u91cc\u4f60\u6743\u9650\u6bd4\u6211\u8fd8\u5927\uff1f"


class SplitMentionTest(unittest.TestCase):

    def test_u2005_separator_is_stripped(self):
        """核心回归:U+2005 分隔符必须被识别,正文要干净。"""
        mentioned, rest = split_mention("@\u5c0f\u52a9\u624b\u2005\u7b7e\u5230")
        self.assertTrue(mentioned)
        self.assertEqual(rest, "\u7b7e\u5230")

    def test_command_slash_ends_up_at_position_zero(self):
        """真正要命的那条:剥完之后 "/" 必须在第 0 位,否则指令永远匹配不上。"""
        mentioned, rest = split_mention("@\u5c0f\u52a9\u624b\u2005/\u7b7e\u5230")
        self.assertTrue(mentioned)
        self.assertEqual(rest, "/\u7b7e\u5230")
        self.assertTrue(rest.startswith("/"))

    def test_real_group_message(self):
        mentioned, rest = split_mention(REAL_GROUP_MSG)
        self.assertTrue(mentioned)
        self.assertNotIn("@\u5c0f\u52a9\u624b", rest)
        self.assertTrue(rest.startswith("\u6211\u7684\u63d2\u4ef6"))

    def test_ascii_space_still_works(self):
        """旧行为不能坏:普通空格分隔仍然要能切。"""
        mentioned, rest = split_mention("@\u5c0f\u52a9\u624b \u7b7e\u5230")
        self.assertTrue(mentioned)
        self.assertEqual(rest, "\u7b7e\u5230")

    def test_fullwidth_mention_and_ideographic_space(self):
        mentioned, rest = split_mention("\uff20\u5c0f\u52a9\u624b\u3000\u7b7e\u5230")
        self.assertTrue(mentioned)
        self.assertEqual(rest, "\u7b7e\u5230")

    def test_plain_text_untouched(self):
        for text in ["\u7b7e\u5230", "/\u7b7e\u5230", ""]:
            self.assertEqual(split_mention(text), (False, text))

    def test_lone_at_is_not_a_mention(self):
        self.assertEqual(split_mention("@"), (False, "@"))
        self.assertEqual(split_mention("@ \u7b7e\u5230"), (False, "@ \u7b7e\u5230"))

    def test_name_only_keeps_original_text(self):
        """整条只 @ 了一下、没正文时保留原文,避免推空事件。"""
        mentioned, rest = split_mention("@\u5c0f\u52a9\u624b")
        self.assertTrue(mentioned)
        self.assertEqual(rest, "@\u5c0f\u52a9\u624b")

    def test_bot_names_mismatch_is_not_a_mention(self):
        mentioned, rest = split_mention(
            "@\u5f20\u4e09\u2005\u4f60\u597d", bot_names=["\u5c0f\u52a9\u624b"])
        self.assertFalse(mentioned)
        self.assertEqual(rest, "\u4f60\u597d")

    def test_bot_names_match_is_a_mention(self):
        mentioned, rest = split_mention(
            "@\u5c0f\u52a9\u624b\u2005\u4f60\u597d", bot_names=["\u5c0f\u52a9\u624b"])
        self.assertTrue(mentioned)
        self.assertEqual(rest, "\u4f60\u597d")

    def test_leading_whitespace_and_multiline(self):
        mentioned, rest = split_mention("  @\u5c0f\u52a9\u624b\u2005\u7b2c\u4e00\u884c\n\u7b2c\u4e8c\u884c")
        self.assertTrue(mentioned)
        self.assertEqual(rest, "\u7b2c\u4e00\u884c\n\u7b2c\u4e8c\u884c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
