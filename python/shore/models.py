"""接口返回 → 工具返回形状。

这一层只做一件事：把 B 站各接口千奇百怪的字段名，归一成调用方（模型 / 测试）
认得的固定形状。字段名属于对外契约，不能随便改——`test/shape-check.mjs`
和 `live-probe.py` 都按这些名字读。
"""

from __future__ import annotations

from typing import Any


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _s(v: Any, default: str = "") -> str:
    return str(v) if v is not None else default


def video_row(it: dict) -> dict:
    """视频卡片。搜索、热门、排行、推荐、收藏、投稿全都用这一个形状。"""
    owner = it.get("owner") or {}
    stat = it.get("stat") or {}
    bvid = _s(it.get("bvid"))
    return {
        "bvid": bvid,
        "title": _s(it.get("title")),
        "up": _s(owner.get("name") or it.get("uname") or it.get("author")),
        "uid": _i(owner.get("mid") or it.get("uid")),
        "play": _i(stat.get("view") or it.get("play")),
        "danmaku": _i(stat.get("danmaku") or it.get("video_review")),
        "duration": _i(it.get("duration")),
        "desc": _s(it.get("desc"))[:200],
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else _s(it.get("arcurl")),
    }


def dyn_row(it: dict) -> dict:
    """关注动态条目。视频 / 图文 / 转发都要能落成这个形状。"""
    mods = it.get("modules") or {}
    author = mods.get("module_author") or {}
    dyn = mods.get("module_dynamic") or {}
    major = dyn.get("major") or {}

    text = ""
    bvid = ""

    opus = major.get("opus") or {}
    if opus:
        text = _s((opus.get("summary") or {}).get("text")).strip()

    archive = major.get("archive") or {}
    if archive:
        bvid = _s(archive.get("bvid"))
        text = text or f"{_s(archive.get('title'))} {_s(archive.get('desc'))}".strip()

    draw = major.get("draw") or {}
    if draw and not text:
        text = " ".join(
            _s(x.get("description") or x.get("alt")) for x in (draw.get("items") or [])
        ).strip()

    article = major.get("article") or {}
    if article and not text:
        text = f"{_s(article.get('title'))} {_s(article.get('desc'))}".strip()

    if not text:
        text = _s((dyn.get("desc") or {}).get("text")).strip()

    jump = _s((it.get("basic") or {}).get("jump_url"))
    return {
        "id": _s(it.get("id_str")),
        "type": _s(major.get("type") or it.get("type")),
        "author": _s(author.get("name")),
        "uid": _i(author.get("mid")),
        "time": _s(author.get("pub_time")),
        "text": text[:300],
        "bvid": bvid,
        "url": ("https:" + jump) if jump and not jump.startswith("http") else jump,
    }


def live_msg_row(it: dict) -> dict:
    """直播间弹幕一条。"""
    return {
        "uid": _i(it.get("uid")),
        "uname": _s(it.get("nickname") or it.get("uname")),
        "text": _s(it.get("text")).strip(),
        "time": _s(it.get("timeline")),
        "admin": bool(it.get("isadmin")),
        "vip": bool(it.get("vip") or it.get("svip")),
    }


def dm_thread_row(it: dict) -> dict:
    """私信会话列表一行。最后一句要从嵌套 JSON 里解出来。"""
    import json

    last = it.get("last_msg") or {}
    raw = last.get("content") or "{}"
    text = ""
    if isinstance(raw, str):
        try:
            text = _s((json.loads(raw) or {}).get("content"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            text = ""
    elif isinstance(raw, dict):
        text = _s(raw.get("content"))

    return {
        "talker_uid": _i(it.get("talker_id")),
        "unread": _i(it.get("unread_count")),
        "is_follow": bool(it.get("is_follow")),
        "last_text": text[:160],
        "last_from_me": _i(last.get("sender_uid")) == _i(it.get("self_uid")),
        "last_ts": _i(it.get("session_ts")),
    }


def dm_message_row(it: dict) -> dict:
    """一封私信。content 可能是 JSON 字符串，要拆。"""
    import json

    raw = it.get("content") or "{}"
    text = ""
    if isinstance(raw, str):
        try:
            text = _s((json.loads(raw) or {}).get("content"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            text = raw
    elif isinstance(raw, dict):
        text = _s(raw.get("content"))

    return {
        "sender_uid": _i(it.get("sender_uid")),
        "receiver_uid": _i(it.get("receiver_uid")),
        "text": text,
        "ts": _i(it.get("timestamp")),
        "from_me": False,  # 由调用方按当前 uid 填充
    }


def comment_row(it: dict, *, with_replies: bool = False) -> dict:
    """评论一条。"""
    member = it.get("member") or {}
    content = it.get("content") or {}
    out = {
        "rpid": _i(it.get("rpid")),
        "uid": _i(member.get("mid")),
        "uname": _s(member.get("uname")),
        "text": _s(content.get("message")).strip(),
        "like": _i(it.get("like")),
        "ctime": _i(it.get("ctime")),
        "rcount": _i(it.get("rcount")),
    }
    if with_replies:
        out["replies"] = [comment_row(x) for x in (it.get("replies") or [])][:5]
    return out


def user_row(it: dict) -> dict:
    """用户信息。"""
    stat = it.get("stat") or {}
    level = it.get("level")
    if isinstance(level, dict):
        level = level.get("current_level")
    return {
        "uid": _i(it.get("mid")),
        "name": _s(it.get("name") or it.get("uname")),
        "sign": _s(it.get("sign")),
        "level": _i(level),
        "fans": _i(stat.get("follower") or it.get("fans")),
        "follow": _i(stat.get("following") or it.get("attention")),
        "videos": _i(stat.get("archive_count") or it.get("video")),
        "likes": _i(stat.get("like_num")),
        "avatar": _s(it.get("face")),
        "url": f"https://space.bilibili.com/{_i(it.get('mid'))}" if it.get("mid") else "",
    }
