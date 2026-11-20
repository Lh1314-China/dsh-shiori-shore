"""用户域工具：用户信息 / 投稿 / 收藏夹 / 收藏内容 / 首页推荐 / 关注动态 / 互动写操作 / 发布。

写操作统一走 `client.post_write`：先校验 jct，再自动补 csrf，避免半截请求。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .client import API_ROOT, Client, ShoreError
from .models import dyn_row, user_row, video_row
from .tools_video import _aid_of, norm_bvid, now_iso

# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------
async def user_info(cli: Client, uid: int) -> dict:
    """用户信息。

    两条路：
      1. `x/space/wbi/acc/info`（WBI）—— 新版风控很严，本机出口 IP 常直接 -352；
      2. `x/web-interface/card`（名片接口）—— 不需要签名，实测稳定。

    主路失败就降级，两条都拿不到才报错。降级时把名片字段拍平成 user_row 需要的形状。
    """
    mid = int(uid)
    ref = f"https://space.bilibili.com/{mid}"
    flat: dict | None = None

    try:
        data = cli.get_data(
            f"{API_ROOT}/x/space/wbi/acc/info",
            {"mid": mid},
            wbi=True,
            referer=ref,
        ) or {}
        if data.get("mid"):
            flat = data
    except ShoreError:
        flat = None

    if flat is None:
        body = cli.get_json(
            f"{API_ROOT}/x/web-interface/card",
            {"mid": mid},
            referer=ref,
            check_code=False,
        )
        if int(body.get("code") or 0) == 0:
            payload = body.get("data") or {}
            card = payload.get("card") or {}
            if card.get("mid"):
                flat = {
                    "mid": card.get("mid"),
                    "name": card.get("name"),
                    "sign": card.get("sign"),
                    "level": (card.get("level_info") or {}).get("current_level"),
                    "face": card.get("face"),
                    "stat": {
                        "follower": payload.get("fans"),
                        "following": card.get("attention"),
                        "archive_count": payload.get("archive_count"),
                        "like_num": payload.get("like_num"),
                    },
                }

    if flat is None:
        raise ShoreError(f"用户不存在或不可见：{mid}")

    row = user_row(flat)
    row["name"] = row.get("name") or ""

    # 关注关系（可选，失败不影响主体）
    try:
        rel = cli.get_data(
            f"{API_ROOT}/x/relation",
            {"fid": mid},
            referer=f"https://space.bilibili.com/{mid}",
        ) or {}
        row["relation"] = {
            "following": bool(rel.get("attribute") in (2, 6)),
            "followed_by": bool(rel.get("attribute") in (6,)),
            "attribute": rel.get("attribute"),
        }
    except ShoreError:
        pass

    return row


async def user_videos(
    cli: Client,
    uid: int,
    pn: int = 1,
    ps: int = 30,
    order: str = "pubdate",
    keyword: str = "",
) -> dict:
    mid = int(uid)
    data = cli.get_data(
        f"{API_ROOT}/x/space/wbi/arc/search",
        {
            "mid": mid,
            "pn": max(1, int(pn or 1)),
            "ps": max(1, min(int(ps or 30), 50)),
            "order": order or "pubdate",
            "keyword": keyword or "",
        },
        wbi=True,
        referer=f"https://space.bilibili.com/{mid}/video",
    ) or {}

    vlist = (data.get("list") or {}).get("vlist") or []
    items = [
        {
            "bvid": x.get("bvid") or "",
            "title": x.get("title") or "",
            "play": x.get("play") or 0,
            "danmaku": x.get("video_review") or 0,
            "created": x.get("created") or 0,
            "length": x.get("length") or "",
            "desc": (x.get("description") or "")[:200],
            "url": f"https://www.bilibili.com/video/{x.get('bvid')}" if x.get("bvid") else "",
        }
        for x in vlist
    ]
    page = data.get("page") or {}
    return {
        "uid": mid,
        "count": len(items),
        "total": page.get("count") or 0,
        "pn": page.get("pn") or pn,
        "ps": page.get("ps") or ps,
        "items": items,
    }


async def favorite_lists(cli: Client, uid: int = 0) -> dict:
    """uid=0 时用当前登录账号。"""
    mid = int(uid or cli.cred.uid_int())
    if not mid:
        raise ShoreError("未登录且没有指定 uid，读不到收藏夹")
    data = cli.get_data(
        f"{API_ROOT}/x/v3/fav/folder/created/list-all",
        {"up_mid": mid},
        referer=f"https://space.bilibili.com/{mid}/favlist",
    ) or {}
    rows = [
        {
            "id": x.get("id"),
            "fid": x.get("fid"),
            "name": x.get("title") or "",
            "count": x.get("media_count") or 0,
            "public": bool(x.get("attr") == 0),
        }
        for x in (data.get("list") or [])
    ]
    return {"uid": mid, "count": len(rows), "lists": rows}


async def favorite_content(cli: Client, media_id: int, page: int = 1, keyword: str = "") -> dict:
    data = cli.get_data(
        f"{API_ROOT}/x/v3/fav/resource/list",
        {
            "media_id": int(media_id),
            "pn": max(1, int(page or 1)),
            "ps": 20,
            "keyword": keyword or "",
            "platform": "web",
        },
    ) or {}
    medias = data.get("medias") or []
    items = []
    for m in medias:
        items.append(
            {
                "bvid": m.get("bvid") or "",
                "title": m.get("title") or "",
                "up": (m.get("upper") or {}).get("name") or "",
                "uid": (m.get("upper") or {}).get("mid") or 0,
                "duration": m.get("duration") or 0,
                "intro": (m.get("intro") or "")[:200],
                "url": f"https://www.bilibili.com/video/{m.get('bvid')}" if m.get("bvid") else "",
            }
        )
    info = data.get("info") or {}
    return {
        "media_id": int(media_id),
        "title": info.get("title") or "",
        "count": len(items),
        "total": info.get("media_count") or 0,
        "has_more": bool(data.get("has_more")),
        "items": items,
    }


async def feed(cli: Client, num: int = 12) -> dict:
    """首页推荐。拿不到就退回热门，返回里 source 说明走的是哪条路。"""
    take = max(1, min(int(num or 12), 30))
    note = ""
    try:
        data = cli.get_data(
            f"{API_ROOT}/x/web-interface/wbi/index/top/feed/rcmd",
            {
                "web_location": 1430650,
                "y_num": 4,
                "fresh_type": 3,
                "fresh_idx": 1,
                "fresh_idx_1h": 1,
                "brush": 0,
                "homepage_ver": 1,
                "feed_version": "V8",
                "ps": take,
                "screen": "1920x1080",
                "seo_info": "",
            },
            wbi=True,
            referer="https://www.bilibili.com/",
        ) or {}
        items = data.get("item") or []
        if items:
            return {
                "source": "rcmd",
                "count": min(len(items), take),
                "items": [video_row(x) for x in items[:take]],
            }
        note = "推荐流返回空"
    except ShoreError as e:
        note = f"推荐流失败：{e}"

    hot = cli.get_data(f"{API_ROOT}/x/web-interface/popular", {"pn": 1, "ps": take}) or {}
    items = [video_row(x) for x in (hot.get("list") or [])]
    return {"source": "hot", "note": note, "count": len(items), "items": items}


async def dynamic_feed(cli: Client, num: int = 15) -> dict:
    take = max(1, min(int(num or 15), 50))
    try:
        data = cli.get_data(
            f"{API_ROOT}/x/polymer/web-dynamic/v1/feed/all",
            {"type": "all", "page": 1, "offset": "", "timezone_offset": -480},
            referer="https://www.bilibili.com/",
        ) or {}
        items = data.get("items") or []
        return {
            "source": "follow",
            "count": min(len(items), take),
            "items": [dyn_row(x) for x in items[:take]],
        }
    except ShoreError as e:
        return {"source": "none", "count": 0, "items": [], "note": f"动态流不可用：{e}"}


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------
async def like(cli: Client, bvid: str, like_: bool = True) -> dict:
    bv = norm_bvid(bvid)
    data = cli.post_write(
        f"{API_ROOT}/x/web-interface/archive/like",
        {"bvid": bv, "like": 1 if like_ else 0},
        referer=f"https://www.bilibili.com/video/{bv}",
    )
    return {"bvid": bv, "action": "like" if like_ else "unlike", "result": data}


async def coin(cli: Client, bvid: str, num: int = 1, also_like: bool = False) -> dict:
    bv = norm_bvid(bvid)
    n = max(1, min(int(num or 1), 2))
    data = cli.post_write(
        f"{API_ROOT}/x/web-interface/coin/add",
        {"bvid": bv, "multiply": n, "select_like": 1 if also_like else 0},
        referer=f"https://www.bilibili.com/video/{bv}",
    )
    return {"bvid": bv, "coins": n, "also_like": bool(also_like), "result": data}


async def fav(cli: Client, bvid: str, fav_: bool = True, media_id: int = 0) -> dict:
    bv = norm_bvid(bvid)
    aid = await _aid_of(cli, bv)

    target = int(media_id or 0)
    if not target:
        mine = await favorite_lists(cli, 0)
        if not mine["lists"]:
            raise ShoreError("当前账号没有默认收藏夹，请显式传 media_id")
        target = int(mine["lists"][0]["id"])

    key = "add_media_ids" if fav_ else "del_media_ids"
    data = cli.post_write(
        f"{API_ROOT}/x/v3/fav/resource/deal",
        {"rid": aid, "type": 2, key: str(target)},
        referer=f"https://www.bilibili.com/video/{bv}",
    )
    return {"bvid": bv, "media_id": target, "action": "fav" if fav_ else "unfav", "result": data}


async def reply(cli: Client, bvid: str, text: str, rpid: int = 0, root: int = 0) -> dict:
    bv = norm_bvid(bvid)
    body = str(text or "").strip()
    if not body:
        raise ShoreError("评论内容不能为空")

    aid = await _aid_of(cli, bv)
    payload: dict[str, Any] = {"oid": aid, "type": 1, "message": body}

    parent = int(root or rpid or 0)
    if parent:
        payload["root"] = parent
        payload["parent"] = int(rpid or parent)

    data = cli.post_write(
        f"{API_ROOT}/x/v2/reply/add",
        payload,
        referer=f"https://www.bilibili.com/video/{bv}",
    )
    return {"bvid": bv, "aid": aid, "text": body, "root": payload.get("root"), "result": data}


_RELATION_ACT = {"follow": 1, "unfollow": 2, "block": 5, "unblock": 6}


async def follow(cli: Client, uid: int, action: str = "follow") -> dict:
    act = _RELATION_ACT.get(str(action or "follow").lower())
    if act is None:
        raise ShoreError(f"action 只能是 {list(_RELATION_ACT)}")

    mid = int(uid)
    data = cli.post_write(
        f"{API_ROOT}/x/relation/modify",
        {"fid": mid, "act": act, "re_src": 14},
        referer=f"https://space.bilibili.com/{mid}",
    )
    return {"uid": mid, "action": action, "result": data}


async def send_dynamic(
    cli: Client,
    text: str,
    images: list[str] | None = None,
    topic_id: int = 0,
    schedule_time: int = 0,
) -> dict:
    """纯文本动态。带图 / 定时这两条先明确拒绝，不假装成功。"""
    body = str(text or "").strip()
    if not body:
        raise ShoreError("动态正文不能为空")
    if images:
        raise ShoreError("图片动态尚未实现（需要先走图片上传接口），本次未发送任何内容")
    if schedule_time:
        raise ShoreError("定时发布尚未实现，本次未发送任何内容")

    payload: dict[str, Any] = {
        "dyn": json.dumps(
            {
                "text": body,
                "type": 4,
                "topic": {"id": int(topic_id or 0)} if topic_id else None,
            },
            ensure_ascii=False,
        ),
        "dyn_type": 4,
    }
    data = cli.post_write(
        f"{API_ROOT}/x/dynamic/feed/create/dyn",
        payload,
        referer="https://www.bilibili.com/",
    )
    return {"text": body[:120], "result": data}


async def send_opus(cli: Client, title: str, content: str, images: list[str] | None = None, category_id: int = 0) -> dict:
    raise ShoreError("Opus 图文发布属于 P2：接口结构与图片上传链路尚未收敛，本版明确不实现（未发送任何内容）")


async def upload_video(cli: Client, **kwargs: Any) -> dict:
    raise ShoreError("视频上传属于 P2：需要分片上传 + 投稿接口 + 实名校验，本版明确不实现（未上传任何内容）")


async def upload_video_multi(cli: Client, **kwargs: Any) -> dict:
    raise ShoreError("多 P 上传属于 P2：同上，本版明确不实现（未上传任何内容）")
