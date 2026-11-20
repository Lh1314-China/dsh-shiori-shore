"""直播域工具：房间信息 / 热门 / 弹幕读取 / 蹲守增量 / 发弹幕。

弹幕走 `xlive/web-room/v1/dM/gethistory`（返回 JSON，比老的口令式接口稳），
发送走 `msg/send`，需要 csrf=bili_jct。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .client import Client, LIVE_ROOT, ShoreError
from .models import live_msg_row


async def live_room(cli: Client, room_id: int) -> dict:
    rid = int(room_id)
    body = cli.get_live("/room/v1/Room/get_info", {"room_id": rid}, room_id=rid)
    if int(body.get("code") or 0) != 0:
        raise ShoreError(body.get("message") or f"code={body.get('code')}", code=body.get("code"))
    data = body.get("data") or {}
    if not data:
        raise ShoreError(f"直播间不存在或接口无数据：{rid}")

    # 主播信息在另一个接口（get_info 只给房间侧）
    anchor: dict[str, Any] = {}
    try:
        info = cli.get_live("/live_user/v1/Master/info", {"uid": data.get("uid") or 0}).get("data") or {}
        anchor = info.get("info") or {}
    except ShoreError:
        anchor = {}

    return {
        "room_id": int(data.get("room_id") or rid),
        "short_id": int(data.get("short_id") or 0),
        "title": data.get("title") or "",
        "live_status": int(data.get("live_status") or 0),
        "area": data.get("area_name") or "",
        "parent_area": data.get("parent_area_name") or "",
        "online": data.get("online") or 0,
        "anchor_uid": int(data.get("uid") or anchor.get("uid") or 0),
        "anchor_uname": anchor.get("uname") or "",
        "link": f"https://live.bilibili.com/{int(data.get('room_id') or rid)}",
        "note": "live_status: 0=未开播 1=直播中 2=轮播",
    }


async def live_hot(cli: Client, num: int = 30, parent_area_id: int = 0) -> dict:
    """直播热门。

    实测（2026-11-20）：`second/getList` 与 `second/getListByArea` 对本机出口 IP
    返回 -352（风控），而直播首页聚合接口 `index/getList` 正常 code=0。
    所以这里走首页接口，把 `recommend_room_list` + 各分区模块的 `list` 合并去重。
    """
    take = max(1, min(int(num or 30), 100))
    pid = int(parent_area_id or 0)

    body = cli.get_live("/xlive/web-interface/v1/index/getList", {"platform": "web"})
    if int(body.get("code") or 0) != 0:
        raise ShoreError(body.get("message") or f"code={body.get('code')}", code=body.get("code"))
    data = body.get("data") or {}

    candidates: list[dict] = list(data.get("recommend_room_list") or [])
    for mod in data.get("room_list") or []:
        if isinstance(mod, dict):
            candidates.extend(mod.get("list") or [])

    seen: set[int] = set()
    rooms: list[dict] = []
    for it in candidates:
        if not isinstance(it, dict):
            continue
        rid = int(it.get("roomid") or it.get("room_id") or 0)
        if not rid or rid in seen:
            continue
        if pid and int(it.get("area_v2_parent_id") or 0) != pid:
            continue
        seen.add(rid)
        rooms.append(
            {
                "room_id": rid,
                "uid": int(it.get("uid") or it.get("up_id") or 0),
                "uname": it.get("uname") or "",
                "title": it.get("title") or "",
                "area": it.get("area_v2_name") or "",
                "parent_area": it.get("area_v2_parent_name") or "",
                "online": it.get("online") or 0,
                "link": f"https://live.bilibili.com/{rid}",
            }
        )
        if len(rooms) >= take:
            break

    if not rooms:
        raise ShoreError("直播热门没有返回房间（可能被风控或分区过滤过严）")

    return {"count": len(rooms), "rooms": rooms, "parent_area_id": pid}


async def live_chat(cli: Client, room_id: int, num: int = 20) -> dict:
    rid = int(room_id)
    body = cli.get_live(
        "/xlive/web-room/v1/dM/gethistory",
        {"roomid": rid, "room_type": 0},
        room_id=rid,
    )
    if int(body.get("code") or 0) != 0:
        raise ShoreError(body.get("message") or f"code={body.get('code')}", code=body.get("code"))
    data = body.get("data") or {}

    raw = data.get("room") or []
    msgs = [live_msg_row(x) for x in raw if str(x.get("text") or "").strip()]
    take = max(1, min(int(num or 20), 200))
    tail = msgs[-take:]

    return {
        "room_id": rid,
        "count": len(tail),
        "messages": tail,
        "note": "" if tail else "暂时没有可读弹幕（可能未开播或弹幕池为空）",
    }


async def live_listen(cli: Client, room_id: int, seconds: int = 20) -> dict:
    """蹲一会儿，只回这段时间里的新弹幕。先 listen 一次拿基线，再 listen 拿增量。"""
    rid = int(room_id)
    span = max(5, min(int(seconds or 20), 55))
    seen: set[tuple] = set()
    fresh: list[dict] = []
    deadline = time.time() + span

    while time.time() < deadline:
        try:
            chunk = await live_chat(cli, rid, num=200)
        except ShoreError:
            break
        for m in chunk["messages"]:
            key = (m["uid"], m["text"], m["time"])
            if key in seen:
                continue
            seen.add(key)
            fresh.append(m)
        await asyncio.sleep(2.5)

    return {"room_id": rid, "seconds": span, "count": len(fresh), "new": fresh[-60:]}


async def live_send_danmaku(cli: Client, room_id: int, text: str, reply_mid: int = 0) -> dict:
    rid = int(room_id)
    body = str(text or "").strip()
    if not body:
        raise ShoreError("弹幕不能为空")
    if len(body) > 20:
        raise ShoreError(f"弹幕 {len(body)} 字，超过普通用户 20 字上限")

    payload: dict[str, Any] = {
        "color": 16777215,
        "fontsize": 25,
        "mode": 1,
        "msg": body,
        "rnd": int(time.time()),
        "roomid": rid,
    }
    if reply_mid:
        payload["reply_mid"] = int(reply_mid)

    data = cli.post_write(
        LIVE_ROOT + "/msg/send",
        payload,
        referer=f"https://live.bilibili.com/{rid}",
    )
    return {"room_id": rid, "text": body, "result": data}
