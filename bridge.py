"""
桥接胶水:微信 <-> AstrBot

职责:
  - wxid 与 OneBot 整数 ID 的双向映射(用 crc32,跨重启稳定)
  - 白名单过滤(默认不过滤,配置 allowed_wxids 后只放行名单内的人)
  - 群聊 @ 判定
  - 多条消息缓冲合并(buffer_seconds),避免连续消息触发多轮回复
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from mentions import split_mention
from ob11 import (DEFAULT_MAX_SIZE, OneBotClient, compose_message,
                  make_message_event, stable_id)
from preflight import reset_port_state, resolve_ob_url
from wechat import WeChatSide

log = logging.getLogger("bridge")

# id <-> wxid 映射的落盘位置。
# 为什么必须落盘:映射是"收到消息时"才建立的,重启后如果还没收到任何消息,
# AstrBot 发来的回复就找不到 wxid,表现为 "还没有映射到 wxid,无法发送"。
MAP_PATH = Path(__file__).resolve().parent / "wxid_map.json"

# 一个缓冲里最多带几张图。微信连着甩十张图时不至于把一条事件撑爆
# (图片是 base64 内嵌的,一张就可能 1~3 MB)。
MAX_IMAGES_PER_PUSH = 4


def _guess_image_suffix(data: bytes) -> str:
    """按文件头猜后缀。微信对图片格式比较敏感,尽量给对。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


class Bridge:

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.id_to_wxid = {}
        self.wxid_to_id = {}
        self.buffers = {}          # 会话 key -> {"texts": [...], "images": [...]}
        self._load_map()

        # 单帧上限。AstrBot 发来的图片是 base64 内嵌的,高清原图很容易超过
        # websockets 的默认 1 MiB,会直接掐断连接(见 ob11.py 的说明)。
        # config.json 里填 0 或 null 表示不限制。
        max_size = cfg.get("ws_max_size", DEFAULT_MAX_SIZE)
        if max_size is not None and int(max_size) <= 0:
            max_size = None

        self.ob = OneBotClient(
            cfg["astrbot_ob_url"],
            10000,
            on_send_text=self._send_text,
            on_send_image=self._send_image,
            on_send_file=self._send_file,
            max_size=max_size,
            # 配置里没写端口时,每次(重)连都先探测 AstrBot 到底在 6199 还是 11229。
            # 报告 P0-1:默认值 11229 是 AstrBot 3.x 的事,4.x 默认 6199,
            # 写死哪个都会让另一拨人连不上。
            url_resolver=resolve_ob_url,
        )
        self.wx = WeChatSide(cfg, self._on_wechat_message,
                             on_image=self._on_wechat_image,
                             on_xml=self._on_wechat_xml)

    def start(self):
        self._sweep_temp_files()      # 清掉上次遗留的落地文件
        self._reset_wxhook_ports()
        self.ob.start()
        self.wx.start()
        threading.Thread(target=self._flush_loop, daemon=True,
                         name="flush").start()

    def _reset_wxhook_ports(self):
        """
        把 wxhook 的端口账本清掉,让端口从 19001 重新分配。

        为什么必须在 bridge 里做(而不是只在 main 里):
        wxhook 的 tools/wxhook.json 会记下上一次用的 remote/server 端口,
        而且它的 "clean" 只保留 pid 还活着的条目。上一个进程如果卡在
        半死状态,新实例会拿到 remote=19002/server=18998,而 DLL 里
        config.ini 的 port 固定是 19001 —— 两边对不上就变成
        WinError 10043 / 一条消息都收不到。
        """
        try:
            from preflight import find_wxhook_dirs
            for d in find_wxhook_dirs(limit=3):
                state = d / "tools" / "wxhook.json"
                if not state.exists():
                    continue
                for note in reset_port_state(d):
                    log.info("%s", note)
                break
        except Exception:
            log.exception("重置 wxhook 端口状态失败(忽略,继续启动)")

    # ---------- ID 映射 ----------

    def _load_map(self):
        """启动时把上次的映射读回来,这样没收到新消息也能回复老会话。"""
        try:
            if MAP_PATH.exists():
                data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
                for k, v in (data or {}).items():
                    try:
                        oid = int(k)
                    except (TypeError, ValueError):
                        continue
                    self.id_to_wxid[oid] = v
                    self.wxid_to_id[v] = oid
                log.info("已载入 %d 条 wxid 映射", len(self.id_to_wxid))
        except Exception:
            log.exception("载入 wxid 映射失败(忽略,继续启动)")

    def _save_map(self):
        try:
            MAP_PATH.write_text(
                json.dumps(self.id_to_wxid, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            log.exception("保存 wxid 映射失败")

    def _register(self, wxid: str) -> int:
        with self.lock:
            oid = self.wxid_to_id.get(wxid)
            if oid is None:
                oid = stable_id(wxid)
                self.wxid_to_id[wxid] = oid
                self.id_to_wxid[oid] = wxid
                log.info("注册会话: %s <-> OneBot id %d", wxid, oid)
                self._save_map()
            return oid

    # ---------- 微信 -> AstrBot ----------

    @staticmethod
    def _split_group_sender(from_user: str, content: str):
        """群消息形如 "wxid_xxx:\\n实际内容",把真正的发送者拆出来。"""
        if not from_user.endswith("@chatroom"):
            return from_user, content
        head, sep, tail = content.partition(":")
        if sep and head and ("wxid" in head or head.startswith("wx")):
            return head.strip(), tail.lstrip("\n")
        return from_user, content

    def _passes_whitelist(self, sender: str, from_user: str) -> bool:
        allowed = self.cfg.get("allowed_wxids") or []
        if not allowed:
            return True
        if sender in allowed or from_user in allowed:
            return True
        log.warning("白名单之外,已忽略 —— 发消息者 wxid = %s", sender)
        log.warning("  如果这就是你自己,请把它原样填进 config.json 的 allowed_wxids")
        return False

    def _buffer_add(self, key: str, sender: str, is_group: bool,
                    text: str = None, image: str = None, atusers=None):
        """往缓冲里塞一段文本或一张图;flush 线程到点再统一推给 AstrBot。"""
        with self.lock:
            buf = self.buffers.get(key)
            if buf is None:
                buf = {"texts": [], "images": [], "sender": sender,
                       "group": is_group, "target": key, "last": 0.0,
                       "atusers": set(), "has_image": False}
                self.buffers[key] = buf
            if text:
                buf["texts"].append(text)
            if image:
                if len(buf["images"]) >= MAX_IMAGES_PER_PUSH:
                    log.warning("单个缓冲里图片已达上限 %d 张,多余的丢弃",
                                MAX_IMAGES_PER_PUSH)
                else:
                    buf["images"].append(image)
                    buf["has_image"] = True
            if atusers:
                buf.setdefault("atusers", set()).update(atusers)
            buf["last"] = time.time()

    def _on_wechat_message(self, event):
        from_user = (getattr(event, "fromUser", None) or "").strip()
        content = getattr(event, "content", None) or ""
        if not from_user or not content.strip():
            return
        if self.wx.self_wxid and from_user == self.wx.self_wxid:
            return

        is_group = from_user.endswith("@chatroom")
        sender, text = self._split_group_sender(from_user, content)

        if not self._passes_whitelist(sender, from_user):
            return

        # 群消息全部推给 AstrBot(供群聊 ICL 记录"群友在聊什么"),
        # 但**只有被 @ 时才是"唤醒"** —— 是否唤醒由 _emit 用 atuserlist 判定,
        # 没有 at 段 AstrBot 就不会回复。于是"看得见、不插嘴"。
        log.info("收到消息 [%s] 来自 %s: %s",
                 "群" if is_group else "私聊", sender, text[:80])
        self._buffer_add(from_user, sender, is_group, text=text,
                         atusers=self._mentioned_wxids(event))

    def _on_wechat_image(self, event):
        """
        微信图片 -> OneBot 的 image 段。

        取图交给 `WeChatSide.image_base64()`:私聊事件自带 base64Img,
        群里的事件**通常没有**(2026-10-18 实测),得走 download_attachment
        兜底。图片进 AstrBot 之后能不能"看懂",取决于那边配的视觉/转述模型。
        """
        from_user = (getattr(event, "fromUser", None) or "").strip()
        if not from_user:
            return
        if self.wx.self_wxid and from_user == self.wx.self_wxid:
            return

        b64 = self.wx.image_base64(event)
        if not b64:
            log.warning("这张图取不到内容,已丢弃:msgId=%s type=%s 概要=%r",
                        getattr(event, "msgId", None),
                        getattr(event, "type", None),
                        getattr(event, "displayFullContent", None))
            return

        content = getattr(event, "content", None)
        if not isinstance(content, str):
            content = ""
        is_group = from_user.endswith("@chatroom")
        sender, _ = self._split_group_sender(from_user, content)

        if not self._passes_whitelist(sender, from_user):
            return

        log.info("收到图片 [%s] 来自 %s: base64 %d 字节",
                 "群" if is_group else "私聊", sender, len(b64))
        self._buffer_add(from_user, sender, is_group, image=b64,
                         atusers=self._mentioned_wxids(event))

    @staticmethod
    def _flatten_quote(content):
        """
        把微信「引用回复」(type=49 的 appmsg,子类型 57)拍平成一串文本。

        引用消息的 XML 形状大致是:

            <appmsg><title>用户新输入</title><type>57</type>
              <refermsg><displayname>张三</displayname>
                        <content>被引用的原文</content></refermsg>
            </appmsg>

        拍平成「[引用 张三:原文]」+ 换行 + 新文本。这样大模型能**同时**看到
        被引用的那句话和用户这次的需求;而且不依赖 AstrBot 对 OneBot reply 段的
        渲染 —— 正好对上人格文件里「[引用 某群友:原文]」的预设格式。
        """
        if not content:
            return None

        def grab(tag):
            m = re.search(
                r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag),
                content, re.S)
            return m.group(1).strip() if m else ""

        title = grab("title")
        name = grab("displayname")
        quoted = grab("content")
        if not title and not quoted:
            return None

        if quoted:
            quoted = " ".join(quoted.split())
            if len(quoted) > 300:
                quoted = quoted[:300] + "..."
            prefix = "[引用 %s:%s]" % (name or "某人", quoted)
            return (prefix + "\n" + title).strip() if title else prefix

        # 引用的不是文字(图片/小程序/文件等):至少保留"引用了谁"
        if name:
            return "[引用 %s 的消息]\n%s" % (name, title or "")
        return title or None

    def _on_wechat_xml(self, event):
        """
        type=49 的 app 消息入口。

        引用回复在这里被拍平成文本,再走和普通文本完全一样的流程
        (白名单 -> mention 过滤 -> 缓冲 -> 推送),所以 @ 判定、缓冲合并
        这些行为都自动一致。
        """
        from_user = (getattr(event, "fromUser", None) or "").strip()
        content = getattr(event, "content", None)
        if not from_user or not isinstance(content, str) or not content.strip():
            return
        if self.wx.self_wxid and from_user == self.wx.self_wxid:
            return

        is_group = from_user.endswith("@chatroom")
        sender, text = self._split_group_sender(from_user, content)

        flat = self._flatten_quote(text)
        if flat is None:
            # 不是引用,而是别的 app 消息(链接/小程序/转账/文件...)。
            # 现在不解析,但记一条,方便以后按需扩展。
            log.info("收到 XML 消息(type=49),暂不解析:来自 %s 概要=%.160r",
                     sender, text)
            return

        if not self._passes_whitelist(sender, from_user):
            return

        # 同普通群消息:全部放行给 ICL 记录,是否唤醒由 _emit 的 atuserlist 判定。
        log.info("收到引用消息 [%s] 来自 %s: %s",
                 "群" if is_group else "私聊", sender, flat[:110])
        self._buffer_add(from_user, sender, is_group, text=flat,
                         atusers=self._mentioned_wxids(event))

    def _flush_loop(self):
        gap = float(self.cfg.get("buffer_seconds", 2) or 2)
        # 含图片的缓冲单独用更长的等待时长:让用户把图发完,机器人再一次性讲解。
        # 文字消息仍用 gap,保证聊天响应快。
        image_gap = float(self.cfg.get("image_buffer_seconds", 10) or 10)
        while True:
            # 检查间隔必须明显小于缓冲时长,否则实际延迟会额外多出最多一个间隔
            time.sleep(0.25)
            now = time.time()
            ready = []
            with self.lock:
                for key, buf in list(self.buffers.items()):
                    wait = image_gap if buf.get("has_image") else gap
                    if now - buf["last"] >= wait:
                        ready.append(buf)
                        del self.buffers[key]
            for buf in ready:
                try:
                    self._emit(buf)
                except Exception:
                    log.exception("推送缓冲消息失败")

    def _split_mention(self, text: str):
        """
        微信群里的 @ 是【纯文本】,而 AstrBot 在群里要求消息里必须有【真正的
        at 段】才算"被唤醒",否则它收到群消息后静默忽略 —— 表现是"私聊能回、
        群里不回"。这里把开头的 @某某 拆出来,由调用方转成 at 段。

        坑(2026-10-18 实测):微信 @"昵称" 与正文之间的分隔符是 U+2005
        四分之一 em 空格,**不是 ASCII 空格**。旧实现只按 " " 切,切不动,
        于是正文里一直带着 "@小助手\\u2005",导致 AstrBot 永远看不到以 "/"
        开头的指令 —— 群里 /签到、/搜图 全部落到大模型;私聊没 @ 前缀,
        所以"私聊能用、群里用不了"。解析逻辑见 mentions.py。
        """
        return split_mention(text, self.cfg.get("bot_nicknames"))

    @staticmethod
    def _mentioned_wxids(event):
        """
        从微信原始事件里取出"这条消息 @ 了谁"。

        权威来源:事件 ``signature`` 里的 ``msgsource.atuserlist``,逗号分隔的 wxid 列表。

        为什么必须用它:微信的 ``@昵称`` 只是正文里的一段纯文本,光看文本无法区分
        "@我" 和 "@别人" —— 旧逻辑只判断"开头有 @",于是群里 @ 别人时也会伪造一个
        指向机器人的 at 段,AstrBot 见状就插嘴,表现就是"@谁它都应"。
        """
        sig = getattr(event, "signature", None)
        if not sig:
            return []
        try:
            data = json.loads(sig) if isinstance(sig, str) else sig
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        src = data.get("msgsource") or {}
        if not isinstance(src, dict):
            return []
        raw = src.get("atuserlist") or ""
        return [u.strip() for u in str(raw).replace(";", ",").split(",") if u.strip()]

    def _self_wxid(self):
        return (getattr(self.wx, "self_wxid", "") or
                self.cfg.get("self_wxid") or "")

    def _emit(self, buf: dict):
        text = "\n".join(t for t in buf["texts"] if t).strip()
        images = buf.get("images") or []
        if not text and not images:
            return
        user_id = self._register(buf["sender"])
        if buf["group"]:
            group_id = self._register(buf["target"])
            mentioned, rest = self._split_mention(text)

            # 用 atuserlist 做权威判定,覆盖"看文本猜"的结果。
            at_users = buf.get("atusers") or set()
            me = self._self_wxid()
            if at_users:
                real = (me in at_users) if me else mentioned
                if real != mentioned:
                    log.info("群 @ 判定修正:文本判定=%s / atuserlist=%s → 实际=%s",
                             mentioned, sorted(at_users), real)
                if not real:
                    rest = text      # 没叫我,保留整段(连同 @别人 那部分)
                mentioned = real
            else:
                # atuserlist 缺失(极少见):退回「@ 后面那个名字」的判定。
                # 名字来自 config 的 bot_nicknames(解析在 mentions.py),
                # 所以 @别人 时 mentioned 本来就是 False。
                if mentioned:
                    log.info("群 @ 判定(无 atuserlist):按名字判定为 @我")

            # 群图片要唤醒(用户希望它讲解图片),但有两个前提:
            #   1) 已由 flush 循环等够 image_buffer_seconds,图基本发完了;
            #   2) 显式开关 group_image_reply 打开,避免它在群里随便插嘴。
            # 注意:被 @ 过就不用等图,已由 mentioned 判定处理。
            if (images and not mentioned
                    and self.cfg.get("group_image_reply", True)):
                mentioned = True
                note_img = "(图)"
            else:
                note_img = ""
            segs = compose_message(rest, images, mention_self=mentioned)
            ev = make_message_event("group", user_id, segs, group_id=group_id)
            note = ("群(含@)" if mentioned else "群") + note_img
        else:
            ev = make_message_event("private", user_id,
                                    compose_message(text, images))
            note = "私聊"
        ok = self.ob.push_event(ev)
        log.info("推送事件 -> AstrBot (%s): %s, 文本 %d 字 / 图片 %d 张",
                 "成功" if ok else "失败", note, len(text), len(images))

    # ---------- AstrBot -> 微信 ----------

    def _resolve(self, ob_id):
        try:
            ob_id = int(ob_id)
        except (TypeError, ValueError):
            return None
        with self.lock:
            return self.id_to_wxid.get(ob_id)

    def _send_text(self, ob_id, text: str):
        wxid = self._resolve(ob_id)
        if not wxid:
            log.warning("OneBot id %s 还没有映射到 wxid,无法发送", ob_id)
            return
        time.sleep(float(self.cfg.get("send_interval", 1) or 0))
        self.wx.send_text(wxid, text)

    def _send_image(self, ob_id, raw: str):
        wxid = self._resolve(ob_id)
        if not wxid:
            log.warning("OneBot id %s 还没有映射到 wxid,无法发图片", ob_id)
            return
        try:
            local = self._materialize_image(raw)
        except Exception:
            log.exception("图片落地成文件失败:%s", str(raw)[:80])
            return
        if not local:
            return
        try:
            if self._prefer_emotion(local):
                resp = self.wx.send_emotion(wxid, local)
                if getattr(resp, "code", None) == 1:
                    return
                # 表情通道失败(比如超过微信表情大小上限)就退回图片,别把图弄丢
                log.warning("表情发送失败(code=%s),退回图片发送: %s",
                            getattr(resp, "code", None), local)
            self.wx.send_image(wxid, local)
        finally:
            # 别立刻删!wxhook 是异步上传,删早了会变成"发送中断"
            if local != raw:
                self._cleanup_later(local)

    def _prefer_emotion(self, path: str) -> bool:
        """
        GIF 走表情通道。

        图片通道(sendImagesMsg)会把动图压成静态首帧 —— 2026-10-21 实测,
        同一个 73 帧的 gif 发出去在微信里不动,改走 sendCustomEmotion 就动了。
        超过微信表情大小上限的 gif 仍然走图片(会是静态的,但至少能发出去)。
        """
        if not self.cfg.get("gif_as_emotion", True):
            return False
        if not path.lower().endswith(".gif"):
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        limit_mb = float(self.cfg.get("gif_emotion_max_mb", 5) or 0)
        if limit_mb <= 0:
            return True
        return size <= limit_mb * 1024 * 1024

    @staticmethod
    def _safe_remove(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    def _cleanup_later(self, path: str, delay: float = None):
        """
        延迟删除临时文件。

        **不能发完立刻删**:wxhook 的 sendFileMsg / sendImagesMsg 都是把上传任务
        丢给 DLL 就返回 code=1,真正上传还在后台跑。2026-10-18 09:15 就栽在这:
        16.8MB 的 wav 一删源文件,微信那边立刻显示"发送中断"。
        """
        if delay is None:
            delay = float(self.cfg.get("send_file_keep_seconds", 1800) or 1800)
        timer = threading.Timer(max(0.0, float(delay)), Bridge._safe_remove,
                                args=(path,))
        timer.daemon = True
        timer.start()
        return timer

    def _sweep_temp_files(self, max_age_seconds: float = 6 * 3600):
        """启动时清掉上次遗留的落地文件(超过 6 小时没删的)。"""
        import tempfile
        try:
            tmp = Path(tempfile.gettempdir())
            now = time.time()
            removed = 0
            for pat in ("wxhook_img_*", "wxhook_file_*"):
                for p in tmp.glob(pat):
                    try:
                        if now - p.stat().st_mtime > max_age_seconds:
                            p.unlink()
                            removed += 1
                    except OSError:
                        pass
            if removed:
                log.info("清理上次遗留的落地文件 %d 个", removed)
        except Exception:
            log.exception("清理临时文件失败(忽略)")

    @staticmethod
    def _guess_audio_suffix(data: bytes) -> str:
        """按文件头猜音频后缀。微信对后缀比较敏感,给错可能播不了。"""
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return ".m4a"                       # MP4/M4A(fMP4 容器)
        if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            return ".mp3"
        if data[:5] == b"#!AMR":
            return ".amr"
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return ".wav"
        if data[:4] == b"OggS":
            return ".ogg"
        if data[:4] == b"fLaC":
            return ".flac"
        return ".m4a"

    def _send_file(self, ob_id, raw: str, name: str = None, seg_type: str = "file"):
        """
        发文件 —— 主要是给"放歌"用的。

        wxhook 0.0.10 **没有 send_voice**,发不了微信语音气泡;所以 AstrBot 的
        `record` 段(听歌插件就发这个)只能退化成"发一个音频文件"。微信里点开
        照样能播,只是长成文件卡片、不是语音条。

        同时顺手支持 `file` / `video` 段。
        """
        wxid = self._resolve(ob_id)
        if not wxid:
            log.warning("OneBot id %s 还没有映射到 wxid,无法发%s", ob_id, seg_type)
            return
        try:
            local = self._materialize_file(raw, name)
        except Exception:
            log.exception("文件落地失败(%s):%s", seg_type, str(raw)[:80])
            return
        if not local:
            return
        try:
            size = os.path.getsize(local)
        except OSError:
            size = -1
        log.info("发送文件(%s) -> %s : %s(%s 字节)",
                 seg_type, wxid, local, size)
        try:
            self.wx.send_file(wxid, local)
        finally:
            # 同样不能立刻删 —— 上传还在后台跑
            if local != raw:
                self._cleanup_later(local)

    @staticmethod
    def _materialize_file(raw: str, name: str = None):
        """
        把 AstrBot 发来的 record/file 段落地成本地文件。

        和图片一样:AstrBot 的 aiocqhttp 适配器会把 Record 段转成 base64
        (aiocqhttp_message_event.py::_from_segment_to_dict 对 Image|Record 统一处理),
        而 wxhook 的 send_file 只吃本地路径。
        """
        import base64 as _base64
        import tempfile
        import urllib.request

        if not raw:
            return None
        raw = raw.strip()

        suffix = ""
        if name:
            suffix = os.path.splitext(name)[1][:8]

        if raw.startswith("base64://"):
            data = _base64.b64decode(raw[len("base64://"):])
            if not suffix:
                suffix = Bridge._guess_audio_suffix(data)
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="wxhook_file_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            log.info("文件来自 base64:已落地 %s(%d 字节,%s)", path, len(data), suffix)
            return path

        if raw.startswith("file://"):
            path = urllib.request.url2pathname(raw[len("file://"):])
            if len(path) > 2 and path[0] in "\\/" and path[2] == ":":
                path = path[1:]
            return path if os.path.exists(path) else None

        if raw.startswith(("http://", "https://")):
            fd, path = tempfile.mkstemp(suffix=suffix or ".m4a",
                                        prefix="wxhook_file_")
            with urllib.request.urlopen(raw, timeout=60) as resp, os.fdopen(fd, "wb") as f:
                f.write(resp.read())
            log.info("文件来自 URL:已下载 %s", path)
            return path

        if os.path.exists(raw):
            return raw

        log.warning("文件不存在,无法发送:%s", raw[:120])
        return None

    @staticmethod
    def _materialize_image(raw: str):
        """
        把 AstrBot 发来的图片"落地"成本地文件。

        为什么需要这一步:AstrBot 的 aiocqhttp 适配器会把 Image 段统一转成 base64
        (见 aiocqhttp_message_event.py::_from_segment_to_dict),
        而 wxhook 的 send_image(wxid, path) 只接受【本地文件路径】。

        兼容四种形式:
            base64://xxxx        图片内容直接内嵌
            file:///C:/a.png     本地文件 URI
            http(s)://...        网络图片
            裸本地路径            直接可用
        """
        import base64 as _base64
        import tempfile
        import urllib.request

        if not raw:
            return None
        raw = raw.strip()

        if raw.startswith("base64://"):
            data = _base64.b64decode(raw[len("base64://"):])
            suffix = _guess_image_suffix(data)
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="wxhook_img_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            log.info("图片来自 base64:已落地 %s(%d 字节,%s)", path, len(data), suffix)
            return path

        if raw.startswith("file://"):
            path = urllib.request.url2pathname(raw[len("file://"):])
            if len(path) > 2 and path[0] in "\\/" and path[2] == ":":
                path = path[1:]          # \C:\a.png -> C:\a.png
            if os.path.exists(path):
                log.info("图片来自 file:// URI:%s", path)
                return path
            log.warning("file:// 指向的文件不存在:%s", path)
            return None

        if raw.startswith(("http://", "https://")):
            fd, path = tempfile.mkstemp(suffix=".png", prefix="wxhook_img_")
            with urllib.request.urlopen(raw, timeout=30) as resp, os.fdopen(fd, "wb") as f:
                f.write(resp.read())
            log.info("图片来自 URL:已下载 %s", path)
            return path

        if os.path.exists(raw):
            return raw

        log.warning("图片不存在或格式不认识,无法发送:%s", raw[:120])
        return None
