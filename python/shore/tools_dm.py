"""私信域工具：会话列表 / 会话内容 / 发送 / 未读 / 收到的回复 / @与点赞 / 守护扫描。

私信走的是 vc 域（api.vc.bilibili.com），鉴权仍用同一套 cookie。
发送接口的参数是 `msg[xxx]` 这种嵌套键名，不能写成嵌套 dict。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .client import API_ROOT, Client, ShoreError

VC = "https://api.vc.bilibili.com"
_DEV_ID = str(uuid.uuid4()).upper()


def _self_uid(cli: Client) -> int:
    uid = cli.cred.uid_int()
    if not uid:
        raise ShoreError("未登录：缺少 DedeUserID，无法判断会话归属")
    return uid


async def dm_threads(cli: Client, num: int = 20) -> dict:
    take = max(1, min(int(num or 20), 60))
    body = cli.get_json(
        f"{VC}/session_svr/v1/session_svr/get_sessions",
        {"session_type": 4, "group_id": 0, "size": take},
        referer="https://message.bilibili.com/",
    )
    data = body.get("data") or {}
    self_uid = cli.cred.uid_int()

    rows = []
    for it in (data.get("session_list") or [])[:take]:
        last = it.get("last_msg") or {}
        raw = last.get("content") or "{}"
        text = ""
        if isinstance(raw, str):
            try:
                text = str((json.loads(raw) or {}).get("content") or "")
            except (json.JSONDecodeError, AttributeError, TypeError):
                text = ""
        elif isinstance(raw, dict):
            text = str(raw.get("content") or "")

        rows.append(
            {
                "talker_uid": int(it.get("talker_id") or 0),
                "unread": int(it.get("unread_count") or 0),
                "is_follow": bool(it.get("is_follow")),
                "last_text": text[:160],
                "last_from_me": int(last.get("sender_uid") or 0) == self_uid,
                "last_ts": int(it.get("session_ts") or 0),
            }
        )

    return {"count": len(rows), "threads": rows}


async def dm_inbox(cli: Client, uid: int | None = None, limit: int = 30) -> dict:
    """读「指定用户 ↔ 本号」的最近消息。不给 uid 就读最新一个会话。"""
    self_uid = _self_uid(cli)
    take = max(1, min(int(limit or 30), 200))

    target = int(uid or 0)
    if not target:
        threads = await dm_threads(cli, 1)
        if not threads["threads"]:
            return {"uid": 0, "count": 0, "messages": [], "note": "没有任何私信会话"}
        target = int(threads["threads"][0]["talker_uid"])

    body = cli.get_json(
        f"{VC}/svr_sync/v1/svr_sync/fetch_session_msgs",
        {"talker_id": target, "session_type": 1, "size": take},
        referer="https://message.bilibili.com/",
    )
    data = body.get("data") or {}

    rows = []
    for m in (data.get("messages") or []):
        raw = m.get("content") or "{}"
        text = ""
        if isinstance(raw, str):
            try:
                text = str((json.loads(raw) or {}).get("content") or "")
            except (json.JSONDecodeError, AttributeError, TypeError):
                text = raw
        elif isinstance(raw, dict):
            text = str(raw.get("content") or "")

        sender = int(m.get("sender_uid") or 0)
        rows.append(
            {
                "sender_uid": sender,
                "receiver_uid": int(m.get("receiver_uid") or 0),
                "text": text,
                "ts": int(m.get("timestamp") or 0),
                "from_me": sender == self_uid,
            }
        )

    rows.sort(key=lambda x: x["ts"])
    return {"uid": target, "self_uid": self_uid, "count": len(rows), "messages": rows}


async def send_message(cli: Client, receiver_uid: int, text: str) -> dict:
    body = str(text or "").strip()
    if not body:
        raise ShoreError("私信内容不能为空")

    self_uid = _self_uid(cli)
    payload = {
        "msg[sender_uid]": self_uid,
        "msg[receiver_id]": int(receiver_uid),
        "msg[receiver_type]": 1,
        "msg[msg_type]": 1,
        "msg[msg_status]": 0,
        "msg[content]": json.dumps({"content": body}, ensure_ascii=False),
        "msg[dev_id]": _DEV_ID,
        "msg[new_face_version]": 0,
        "from_firework": 0,
        "build": 0,
        "mobi_app": "web",
    }
    data = cli.post_write(f"{VC}/web_im/v1/web_im/send_msg", payload, referer="https://message.bilibili.com/")
    return {"receiver_uid": int(receiver_uid), "text": body[:120], "result": data}


async def unread_messages(cli: Client) -> dict:
    data = cli.get_data(f"{API_ROOT}/x/msgfeed/unread", {}, referer="https://message.bilibili.com/") or {}
    return {
        "reply": data.get("reply") or 0,
        "at": data.get("at") or 0,
        "like": data.get("like") or 0,
        "system": data.get("sys") or 0,
        "dm": data.get("dm") or 0,
        "raw": data,
    }


async def received_replies(cli: Client, pn: int = 1, ps: int = 20) -> dict:
    data = cli.get_data(
        f"{API_ROOT}/x/msgfeed/reply",
        {"pn": max(1, int(pn or 1)), "ps": max(1, min(int(ps or 20), 50))},
        referer="https://message.bilibili.com/",
    ) or {}
    items = []
    for it in (data.get("items") or []):
        item = it.get("item") or {}
        user = it.get("user") or {}
        items.append(
            {
                "id": it.get("id"),
                "uid": int(user.get("mid") or 0),
                "uname": user.get("nickname") or "",
                "title": item.get("title") or "",
                "text": (item.get("source_content") or item.get("content") or "")[:200],
                "bvid": item.get("uri") or "",
                "ts": it.get("reply_time") or it.get("time") or 0,
            }
        )
    return {"count": len(items), "items": items}


async def received_at_and_likes(cli: Client) -> dict:
    at = cli.get_data(f"{API_ROOT}/x/msgfeed/at", {"pn": 1, "ps": 20}, referer="https://message.bilibili.com/") or {}
    likes = cli.get_data(f"{API_ROOT}/x/msgfeed/like", {"pn": 1, "ps": 20}, referer="https://message.bilibili.com/") or {}

    at_items = []
    for it in (at.get("items") or []):
        item = it.get("item") or {}
        user = it.get("user") or {}
        at_items.append(
            {
                "id": it.get("id"),
                "uid": int(user.get("mid") or 0),
                "uname": user.get("nickname") or "",
                "text": (item.get("source_content") or item.get("content") or "")[:200],
                "bvid": item.get("uri") or "",
                "ts": it.get("at_time") or it.get("time") or 0,
            }
        )

    like_items = [
        {
            "id": it.get("id"),
            "uid": int((it.get("user") or {}).get("mid") or 0),
            "uname": (it.get("user") or {}).get("nickname") or "",
            "title": (it.get("item") or {}).get("title") or "",
            "ts": it.get("like_time") or it.get("time") or 0,
        }
        for it in (likes.get("items") or [])
    ]

    return {"at": at_items, "likes": like_items, "at_count": len(at_items), "like_count": len(like_items)}


async def guard_scan(
    cli: Client,
    bvids: list[str] | None = None,
    since_at_id: int = 0,
    since_at_time: int = 0,
    since_ctimes: dict[str, int] | None = None,
    max_pages: int = 2,
) -> dict:
    """一次拿回「谁 @ 了我」+「我指定视频下的新评论」。

    与 `received_at_and_likes` 的区别：那个只给通知列表；这个还去视频下面翻评论，
    并且按时间戳做增量过滤，适合值班轮反复调用。
    """
    news = await received_at_and_likes(cli)
    at_rows = news["at"]
    if since_at_id:
        at_rows = [x for x in at_rows if int(x.get("id") or 0) > int(since_at_id)]
    if since_at_time:
        at_rows = [x for x in at_rows if int(x.get("ts") or 0) >= int(since_at_time)]

    marks = since_ctimes or {}
    fresh_comments: list[dict] = []

    for bv in (bvids or [])[:10]:
        try:
            aid = None
            from .tools_video import _aid_of, norm_bvid

            aid = await _aid_of(cli, norm_bvid(bv))
        except ShoreError:
            continue

        floor = int(marks.get(bv) or 0)
        for page in range(1, max(1, int(max_pages or 2)) + 1):
            try:
                data = cli.get_data(
                    f"{API_ROOT}/x/v2/reply",
                    {"type": 1, "oid": aid, "pn": page, "ps": 20, "sort": 0},
                    referer=f"https://www.bilibili.com/video/{bv}",
                ) or {}
            except ShoreError:
                break

            replies = data.get("replies") or []
            if not replies:
                break

            hit_old = False
            for r in replies:
                ctime = int(r.get("ctime") or 0)
                if floor and ctime < floor:
                    hit_old = True
                    continue
                member = r.get("member") or {}
                fresh_comments.append(
                    {
                        "bvid": bv,
                        "rpid": r.get("rpid"),
                        "uid": int(member.get("mid") or 0),
                        "uname": member.get("uname") or "",
                        "text": str((r.get("content") or {}).get("message") or "")[:200],
                        "ctime": ctime,
                    }
                )
            if hit_old:
                break

    return {
        "at": at_rows,
        "at_count": len(at_rows),
        "comments": fresh_comments,
        "comment_count": len(fresh_comments),
        "checked_bvids": list((bvids or [])[:10]),
    }
