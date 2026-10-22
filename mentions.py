# -*- coding: utf-8 -*-
"""
微信群消息里 @ 前缀的解析。

为什么单独成一个文件:bridge.py 一被 import 就会连带 import wechat.py,而
wechat.py 会 `from wxhook import Bot`,将一个纯字符串问题绑死在"必须能注入
微信"这套重环境上。拆出来之后可以脱离 wxhook 单测。

## 背景(这是一个真实踩过的坑)

微信在群里 @ 人时,昵称和正文之间的分隔符**不是普通空格**,而是
U+2005(FOUR-PER-EM SPACE)。wxhook 把这段文本原样交给我们:

    "@小助手\\u2005签到"

旧实现只按 ASCII 空格切(`text.partition(" ")`),切不动 →
`rest` 退化成整串原文 → 推给 AstrBot 的正文仍然是 "@小助手\\u2005签到"。

而 AstrBot 判断"这是不是一条指令"看的是 message_str 是否以 wake_prefix
("/")开头 —— 开头是 "@" 就永远不匹配。结果:

    群里发 /签到  → 指令没匹配上 → 落到大模型 → 它回一句"没这功能"
    私聊发 /签到  → 正文干净      → 指令正常执行

这就是"插件装了、私聊能用、群里用不了"的根因。
"""

MENTION_HEADS = "@＠"

# 昵称与正文之间可能出现的分隔符。U+2005 是实测到的,其余是同类空白兜底。
MENTION_SEPS = (
    " \t\n\r\v\f"
    "\u00a0"          # NBSP
    "\u2002\u2003"    # EN SPACE / EM SPACE
    "\u2004\u2005\u2006"  # 1/3 EM / 1/4 EM / 1/6 EM SPACE
    "\u2009\u202f"    # THIN SPACE / NARROW NBSP
    "\u3000"          # 全角空格
)


def split_mention(text: str, bot_names=None):
    """拆开 "@昵称<分隔符>正文"。

    返回 ``(是否在叫机器人, 正文)``。

    - 没配 ``bot_names``:任何开头的 @ 都当成在叫机器人(保持旧行为)。
    - 配了 ``bot_names``:只有昵称命中才返回 True;叫的是别人时返回 False,
      但仍然会去掉 @ 前缀(让正文干净),由调用方决定要不要推给 AstrBot。
    """
    if not text:
        return False, text

    t = text.lstrip()
    if not t or t[0] not in MENTION_HEADS:
        return False, text

    body = t[1:]
    cut = len(body)
    for i, ch in enumerate(body):
        if ch in MENTION_SEPS:
            cut = i
            break
    if cut == 0:                     # 只有一个 "@",不算叫人
        return False, text

    head = body[:cut]
    rest = body[cut:].lstrip(MENTION_SEPS)
    # 整条消息就是 "@小助手" 时,正文为空 —— 保留原文,避免推空事件
    result = rest or text

    names = [n for n in (bot_names or []) if n]
    if names and not any(n in head for n in names):
        return False, result
    return True, result
