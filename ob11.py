"""
OneBot v11 客户端

负责:
  - 以 WebSocket 客户端身份连到 AstrBot 的 aiocqhttp 服务端
  - push_event(): 把微信消息包装成 OneBot 事件推给 AstrBot
  - 接收 AstrBot 的 API 调用(send_msg / send_private_msg / send_group_msg)

事件形状沿用你原 bridge 里验证过能用的那一套,不重新发明。
"""

import asyncio
import json
import logging
import re
import threading
import time
import zlib

import websockets

log = logging.getLogger("ob11")

# websockets 库默认 max_size = 1 MiB(1048576 字节)。这个默认值对我们的用途
# 太小了:AstrBot 的 aiocqhttp 适配器会把图片段统一转成 base64:// 内嵌,
# 一张 1 MB 的原图 base64 后约 1.33 MB,整条 send_private_msg 的 JSON 也就
# 超过 1 MiB。超限时 websockets 不会截断,而是【发 1009 关掉整条连接】,
# 于是这张图丢了、连接也断了,日志表现为:
#     sent 1009 (message too big) frame with 1312263 bytes exceeds limit of 1048576 bytes
# 注意:max_size 只限制【收到】的消息;发出去的不受它约束。
DEFAULT_MAX_SIZE = 64 * 1024 * 1024


def stable_id(text: str) -> int:
    """
    把 wxid 映射成稳定的整数 ID(OneBot 用整数 user_id)。

    注意:不能用 hash() —— Python 3 的字符串 hash 每个进程都不同,
    会导致 AstrBot 每次重启看到的 user_id 都变,对话连续性直接断掉。
    crc32 是确定的,同一个 wxid 永远得到同一个 ID。
    """
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def make_message_event(message_type: str, user_id: int, message: list,
                       group_id: int = 0, group_name: str = "",
                       nickname: str = "") -> dict:
    """构造 OneBot v11 消息事件"""
    event = {
        "time": int(time.time()),
        "self_id": 10000,
        "post_type": "message",
        "message": message,
        "raw_message": "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if isinstance(seg, dict) and seg.get("type") == "text"
        ),
        "sender": {"user_id": user_id, "nickname": nickname or str(user_id)},
    }
    if message_type == "group":
        event["message_type"] = "group"
        event["group_id"] = group_id
        event["user_id"] = user_id
        event["group_name"] = group_name or str(group_id)
    else:
        event["message_type"] = "private"
        event["user_id"] = user_id
    return event


def compose_message(text: str, images=None, mention_self: bool = False,
                   self_id: int = 10000) -> list:
    """组装 OneBot v11 的 message 段列表:at(可选) → 文本(可选) → 图片。

    两条规矩都来自 AstrBot 的实际实现:

    1. **at 段必须排第一**。AstrBot 的唤醒判断只认"第一个 at 段是自己"
       (aiocqhttp_platform_adapter.py:384),而且第一个 at 段不会进 message_str,
       不会污染指令匹配。放后面等于没 @。
    2. **图片用 base64:// 内嵌**。两个进程不共享临时目录,给路径没用。
       图片大小由 WebSocket 的单帧上限兜着(ob11.py 的 DEFAULT_MAX_SIZE),
       微信图片 base64 后常在 1~3 MB,所以那个 64 MiB 的上限是必须的。
    """
    segs = []
    if mention_self:
        segs.append({"type": "at", "data": {"qq": str(self_id)}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    for b64 in (images or []):
        if b64:
            segs.append({"type": "image", "data": {"file": "base64://" + b64}})
    return segs


class OneBotClient:
    """到 AstrBot 的 OneBot v11 WebSocket 客户端。"""

    def __init__(self, url: str, self_id: int,
                 on_send_text=None, on_send_image=None,
                 on_send_file=None,
                 max_size: int = DEFAULT_MAX_SIZE):
        self.url = url
        self.self_id = self_id
        self.on_send_text = on_send_text
        self.on_send_image = on_send_image
        # record / file / video 段走这里(放歌的音频就是 record)
        self.on_send_file = on_send_file
        self.max_size = max_size
        self.ws = None
        self.loop = None
        self.ready = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="ob11")
        self._thread.start()

    def _run(self):
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self):
        while True:
            try:
                log.info("正在连接 AstrBot: %s (单帧上限 %s 字节)",
                         self.url, self.max_size if self.max_size else "无限制")
                async with websockets.connect(
                    self.url,
                    # 不显式抬高的话,默认 1 MiB 会在收到 base64 图片时把连接掐断
                    max_size=self.max_size,
                    additional_headers={
                        "X-Self-ID": str(self.self_id),
                        "X-Client-Role": "Universal",
                        "User-Agent": "OneBot/11",
                    },
                ) as ws:
                    self.ws = ws
                    self.ready.set()
                    log.info("已连接到 AstrBot")

                    async def _keepalive():
                        while True:
                            await asyncio.sleep(15)
                            try:
                                await ws.ping()
                            except Exception:
                                break

                    ka = asyncio.create_task(_keepalive())
                    try:
                        async for raw in ws:
                            try:
                                await self._handle(json.loads(raw))
                            except Exception:
                                log.exception("处理 AstrBot API 失败")
                    finally:
                        ka.cancel()
            except Exception as e:
                log.warning("连接断开或失败(%s),5 秒后重连", e)

            self.ws = None
            self.ready.clear()
            await asyncio.sleep(5)

    def push_event(self, event: dict) -> bool:
        if not self.ws or not self.loop:
            log.warning("WebSocket 尚未就绪,事件丢弃")
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.ws.send(json.dumps(event, ensure_ascii=False)), self.loop)
            fut.result(timeout=5)
            return True
        except Exception as e:
            log.warning("推送事件失败: %s", e)
            return False

    async def _handle(self, data: dict):
        action = data.get("action", "")
        params = data.get("params", {}) or {}
        echo = data.get("echo", "")

        # 把 AstrBot 发来的每个 API 调用记下来。
        # 没有这行日志,发送环节出问题就完全无从下手(之前就是吃了这个亏)。
        try:
            _preview = json.dumps(params, ensure_ascii=False)
        except Exception:
            _preview = repr(params)
        log.info("[API] %s  echo=%s  params=%s", action, echo, _preview[:400])

        # 先回响应,否则 AstrBot 侧会超时
        resp = {"status": "ok", "retcode": 0, "data": {}}
        if echo:
            resp["echo"] = echo
        if self.ws:
            try:
                await self.ws.send(json.dumps(resp, ensure_ascii=False))
            except Exception:
                pass

        if action not in ("send_msg", "send_private_msg", "send_group_msg"):
            return

        target = params.get("user_id") or params.get("group_id")
        message = params.get("message")
        sent_any = False

        # AstrBot 可能发"段列表",也可能发 CQ 码字符串 —— 两种都要吃
        if isinstance(message, str):
            text = re.sub(r"\[CQ:[^\]]*\]", "", message).strip()
            if text and self.on_send_text:
                log.info("[API] message 是字符串,CQ 码已剥离,纯文本 %d 字", len(text))
                await asyncio.to_thread(self.on_send_text, target, text)
                sent_any = True
        else:
            for seg in message or []:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                seg_data = seg.get("data", {}) or {}
                if seg_type == "text" and seg_data.get("text"):
                    if self.on_send_text:
                        await asyncio.to_thread(self.on_send_text, target, seg_data["text"])
                        sent_any = True
                elif seg_type == "image":
                    path = seg_data.get("file") or seg_data.get("path")
                    if path and self.on_send_image:
                        await asyncio.to_thread(self.on_send_image, target, path)
                        sent_any = True
                elif seg_type in ("record", "file", "video"):
                    # 放歌(is_plugin listen_music)来的就是 record 段。
                    # wxhook 0.0.10 没有 send_voice,发不了语音气泡,只能当文件发
                    # —— 微信里点开一样能播,只是长成文件卡片。
                    path = (seg_data.get("file") or seg_data.get("path")
                            or seg_data.get("url"))
                    if path and self.on_send_file:
                        await asyncio.to_thread(
                            self.on_send_file, target, path,
                            seg_data.get("name") or seg_data.get("filename"),
                            seg_type)
                        sent_any = True
                else:
                    log.info("[API] 暂不支持的段类型: %s", seg_type)

        if not sent_any:
            log.warning("[API] 这条 send 调用没解析出任何可发送内容,原始 message=%r", message)
