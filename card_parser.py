# -*- coding: utf-8 -*-
"""QQ JSON 卡片解析 — 直接移植 NachoBot card_handler.py (2026-08-22)
=====================================================================
原版: NachoBot-Napcat-Adapter/src/recv_handler/card_handler.py
改动: 去掉 ncnk_message.Seg 依赖和图片加载 (返回纯文本), 逻辑原样保留
用法: parse_json_card(raw_message) -> str (卡片文本)
=====================================================================
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Mapping

from qq_emoji_list import qq_face


def parse_json_card(raw_message: Mapping[str, Any]) -> str:
    """把 OneBot ``json`` 消息段转换为核心可读取的文本。"""
    segment_data = raw_message.get("data", {})
    if not isinstance(segment_data, Mapping):
        return "[json]"

    raw_card = segment_data.get("data")
    if isinstance(raw_card, Mapping):
        parsed_card: Any = dict(raw_card)
    else:
        json_data = str(raw_card or "").strip()
        if not json_data:
            return "[json]"
        try:
            parsed_card = json.loads(json_data)
        except (TypeError, ValueError):
            return "[json]"

    if not isinstance(parsed_card, Mapping):
        return "[json]"

    app_name = str(parsed_card.get("app") or "").strip()
    meta = parsed_card.get("meta", {})
    if not isinstance(meta, Mapping):
        meta = {}

    if app_name == "com.tencent.mannounce":
        return _build_announcement_text(meta)

    if app_name in {"com.tencent.music.lua", "com.tencent.structmsg"}:
        return _build_music_text(meta) or "[音乐分享]"

    if app_name == "com.tencent.miniapp_01":
        return _build_miniapp_text(meta)

    if app_name == "com.tencent.giftmall.giftark":
        return _build_gift_text(meta)

    if app_name == "com.tencent.contact.lua":
        return _build_contact_text(meta, "推荐联系人")

    if app_name == "com.tencent.troopsharecard":
        return _build_contact_text(meta, "推荐群聊")

    if app_name == "com.tencent.tuwen.lua":
        return _build_news_text(meta, "图文分享")

    if app_name == "com.tencent.feed.lua":
        return _build_feed_text(meta)

    if app_name == "com.tencent.template.qqfavorite.share":
        return _build_favorite_text(meta)

    if app_name == "com.tencent.miniapp.lua":
        return _build_simple_title_text(meta, "miniapp", "QQ空间")

    if app_name == "com.tencent.forum":
        return _build_forum_text(meta)

    if app_name == "com.tencent.map":
        return _build_location_text(meta)

    if app_name == "com.tencent.together":
        return _build_together_text(meta)

    prompt = str(parsed_card.get("prompt") or meta.get("prompt") or "").strip()
    fallback_text = prompt or app_name or "json"
    return f"[json:{fallback_text}]"


def _build_announcement_text(meta: Mapping[str, Any]) -> str:
    announcement = meta.get("mannounce", {})
    if not isinstance(announcement, Mapping):
        announcement = {}
    title = str(announcement.get("title") or "").strip()
    content = str(announcement.get("text") or "").strip()
    if announcement.get("encode") == 1:
        title = _safe_base64_decode(title)
        content = _safe_base64_decode(content)
    if title and content:
        return f"[{title}]：{content}"
    if title:
        return f"[{title}]"
    return content or "[群公告]"


def _build_music_text(meta: Mapping[str, Any]) -> str:
    music = meta.get("music", {})
    if not isinstance(music, Mapping):
        return ""
    title = str(music.get("title") or "").strip()
    singer = str(music.get("desc") or music.get("singer") or "").strip()
    tag = str(music.get("tag") or "音乐分享").strip() or "音乐分享"
    parts = [f"[{tag}]"]
    if title:
        parts.append(title)
    if singer:
        parts.append(f"- {singer}")
    return " ".join(parts).strip() or "[音乐分享]"


def _build_miniapp_text(meta: Mapping[str, Any]) -> str:
    detail = meta.get("detail_1", {})
    if not isinstance(detail, Mapping):
        return "[小程序]"
    title = str(detail.get("title") or "").strip()
    description = str(detail.get("desc") or "").strip()
    if title and description:
        return f"[小程序] {title}：{description}"
    return f"[小程序] {title or description}".strip()


def _build_gift_text(meta: Mapping[str, Any]) -> str:
    gift = meta.get("giftark", {})
    if not isinstance(gift, Mapping):
        return "[赠送礼物]"
    gift_name = str(gift.get("title") or "礼物").strip() or "礼物"
    description = str(gift.get("desc") or "").strip()
    suffix = f" {description}" if description else ""
    return f"[赠送礼物: {gift_name}]{suffix}"


def _build_contact_text(meta: Mapping[str, Any], default_tag: str) -> str:
    contact = meta.get("contact", {})
    if not isinstance(contact, Mapping):
        return f"[{default_tag}]"
    name = str(contact.get("nickname") or "未知对象").strip() or "未知对象"
    tag = str(contact.get("tag") or default_tag).strip() or default_tag
    return f"[{tag}] {name}"


def _build_news_text(meta: Mapping[str, Any], default_tag: str) -> str:
    news = meta.get("news", {})
    if not isinstance(news, Mapping):
        return f"[{default_tag}]"
    title = str(news.get("title") or "未知标题").strip() or "未知标题"
    description = str(news.get("desc") or "").replace("[图片]", "").strip()
    tag = str(news.get("tag") or default_tag).strip() or default_tag
    if tag in title:
        title = _trim_card_title(title.replace(tag, "", 1))
    if description:
        return f"[{tag}] {title}：{description}"
    return f"[{tag}] {title}".strip()


def _build_feed_text(meta: Mapping[str, Any]) -> str:
    feed = meta.get("feed", {})
    if not isinstance(feed, Mapping):
        return "[群相册]"
    title = str(feed.get("title") or "群相册").strip() or "群相册"
    tag = str(feed.get("tagName") or "群相册").strip() or "群相册"
    description = str(feed.get("forwardMessage") or "").strip()
    if tag in title:
        title = _trim_card_title(title.replace(tag, "", 1))
    if description:
        return f"[{tag}] {title}：{description}"
    return f"[{tag}] {title}".strip()


def _build_favorite_text(meta: Mapping[str, Any]) -> str:
    news = meta.get("news", {})
    if not isinstance(news, Mapping):
        return "[QQ收藏]"
    description = str(news.get("desc") or "").replace("[图片]", "").strip()
    tag = str(news.get("tag") or "QQ收藏").strip() or "QQ收藏"
    return f"[{tag}] {description}".strip()


def _build_simple_title_text(meta: Mapping[str, Any], key: str, default_tag: str) -> str:
    payload = meta.get(key, {})
    if not isinstance(payload, Mapping):
        return f"[{default_tag}]"
    title = str(payload.get("title") or "未知标题").strip() or "未知标题"
    tag = str(payload.get("tag") or default_tag).strip() or default_tag
    return f"[{tag}] {title}".strip()


def _build_forum_text(meta: Mapping[str, Any]) -> str:
    detail = meta.get("detail", {})
    if not isinstance(detail, Mapping):
        return "[频道帖子]"
    feed = detail.get("feed", {})
    poster = detail.get("poster", {})
    channel_info = detail.get("channel_info", {})
    if not all(isinstance(item, Mapping) for item in (feed, poster, channel_info)):
        return "[频道帖子]"
    guild_name = str(channel_info.get("guild_name") or "").strip()
    nickname = str(poster.get("nick") or "QQ用户").strip() or "QQ用户"
    title = _extract_forum_title(feed)
    face_content = _extract_forum_face_text(feed)
    prefix = f"[频道帖子] [{guild_name}]" if guild_name else "[频道帖子]"
    return f"{prefix}{nickname}:{title}{face_content}"


def _extract_forum_title(feed: Mapping[str, Any]) -> str:
    title_payload = feed.get("title", {})
    if not isinstance(title_payload, Mapping):
        return "帖子"
    contents = title_payload.get("contents", [])
    if not isinstance(contents, list) or not contents or not isinstance(contents[0], Mapping):
        return "帖子"
    text_content = contents[0].get("text_content", {})
    if not isinstance(text_content, Mapping):
        return "帖子"
    return str(text_content.get("text") or "帖子").strip() or "帖子"


def _extract_forum_face_text(feed: Mapping[str, Any]) -> str:
    contents_payload = feed.get("contents", {})
    if not isinstance(contents_payload, Mapping):
        return ""
    contents = contents_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    face_parts: list[str] = []
    for item in contents:
        if not isinstance(item, Mapping):
            continue
        emoji_content = item.get("emoji_content", {})
        if not isinstance(emoji_content, Mapping):
            continue
        emoji_id = str(emoji_content.get("id") or "").strip()
        if emoji_id in qq_face:
            face_parts.append(qq_face[emoji_id])
    return "".join(face_parts)


def _build_location_text(meta: Mapping[str, Any]) -> str:
    location = meta.get("Location.Search", {})
    if not isinstance(location, Mapping):
        return "[位置]"
    name = str(location.get("name") or "未知地点").strip() or "未知地点"
    address = str(location.get("address") or "").strip()
    if address:
        return f"[位置] {address} · {name}"
    return f"[位置] {name}"


def _build_together_text(meta: Mapping[str, Any]) -> str:
    invite = meta.get("invite", {})
    if not isinstance(invite, Mapping):
        return "[一起听歌]"
    title = str(invite.get("title") or "一起听歌").strip() or "一起听歌"
    summary = str(invite.get("summary") or "").strip()
    return f"[{title}] {summary}".strip()


def _trim_card_title(title: str) -> str:
    return re.sub(r"^[：:\s\-—]+|[：:\s\-—]+$", "", str(title or "").strip())


def _safe_base64_decode(encoded_text: str) -> str:
    normalized_text = str(encoded_text or "").strip()
    if not normalized_text:
        return ""
    try:
        return base64.b64decode(normalized_text).decode("utf-8", errors="ignore")
    except Exception:
        return normalized_text
