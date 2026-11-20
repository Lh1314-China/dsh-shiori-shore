"""视频域工具：信息 / 评论 / 弹幕 / 字幕 / 搜索 / 热门 / 热搜 / 每周必看 / 排行 / 圈地 / 批量采集。

全部是纯逻辑函数，不依赖 MCP 框架——由 server.py 统一包装成工具。
每个函数要么返回 dict（成功），要么抛 ShoreError（失败）。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import API_ROOT, Client, ShoreError
from .models import comment_row, video_row

CST = timezone(timedelta(hours=8))
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_BV_ANY = re.compile(r"BV[0-9A-Za-z]{10}")


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def norm_bvid(bvid: str) -> str:
    b = str(bvid or "").strip()
    if not _BV_RE.match(b):
        raise ShoreError(f"bvid 不合法：{bvid!r}")
    return b


async def _view(cli: Client, bvid: str) -> dict:
    bv = norm_bvid(bvid)
    data = cli.get_data(f"{API_ROOT}/x/web-interface/view", {"bvid": bv}, referer=f"https://www.bilibili.com/video/{bv}")
    if not isinstance(data, dict) or not data.get("aid"):
        raise ShoreError(f"取不到视频信息：{bv}")
    return data


async def _aid_of(cli: Client, bvid: str) -> int:
    return int((await _view(cli, bvid)).get("aid") or 0)


async def _cid_of(cli: Client, bvid: str) -> int:
    data = await _view(cli, bvid)
    cid = int(data.get("cid") or 0)
    if not cid:
        pages = data.get("pages") or []
        if pages:
            cid = int(pages[0].get("cid") or 0)
    if not cid:
        raise ShoreError(f"取不到 cid：{bvid}")
    return cid


# ----------------------------------------------------------------------
# 极简 protobuf 读取器（只服务弹幕段，不引第三方库）
# ----------------------------------------------------------------------
def _pb_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            break
    return result, pos


def _pb_fields(buf: bytes):
    """逐个产出 (field_no, wire_type, value)。长度域给 bytes，其余给 int。"""
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _pb_varint(buf, pos)
        if tag == 0:
            break
        fno, wire = tag >> 3, tag & 7
        if wire == 0:
            val, pos = _pb_varint(buf, pos)
            yield fno, wire, val
        elif wire == 1:
            yield fno, wire, buf[pos : pos + 8]
            pos += 8
        elif wire == 2:
            ln, pos = _pb_varint(buf, pos)
            yield fno, wire, buf[pos : pos + ln]
            pos += ln
        elif wire == 5:
            yield fno, wire, buf[pos : pos + 4]
            pos += 4
        else:
            break


def _pb_elem(chunk: bytes) -> dict:
    out: dict[str, Any] = {}
    for fno, wire, val in _pb_fields(chunk):
        if wire == 2 and fno == 7:
            out["text"] = val.decode("utf-8", "replace")
        elif wire == 0:
            if fno == 2:
                out["progress"] = int(val)
            elif fno == 3:
                out["mode"] = int(val)
            elif fno == 5:
                out["color"] = int(val)
    return out


# ----------------------------------------------------------------------
# 信息
# ----------------------------------------------------------------------
async def video_info(cli: Client, bvid: str) -> dict:
    data = await _view(cli, bvid)
    pages = data.get("pages") or []
    owner = data.get("owner") or {}
    return {
        "bvid": data.get("bvid") or norm_bvid(bvid),
        "aid": data.get("aid") or 0,
        "title": data.get("title") or "",
        "desc": (data.get("desc") or "")[:500],
        "up": owner.get("name") or "",
        "uid": owner.get("mid") or 0,
        "duration": data.get("duration") or 0,
        "pubdate": data.get("pubdate") or 0,
        "pic": data.get("pic") or "",
        "stat": data.get("stat") or {},
        "pages": [
            {
                "cid": p.get("cid"),
                "page": p.get("page"),
                "part": p.get("part"),
                "duration": p.get("duration"),
            }
            for p in pages
        ],
        "url": f"https://www.bilibili.com/video/{data.get('bvid') or bvid}",
    }


async def comments(cli: Client, bvid: str, num: int = 20, order: str = "hot") -> dict:
    bv = norm_bvid(bvid)
    aid = await _aid_of(cli, bv)
    hot = str(order).lower() in ("hot", "热度", "2", "")
    sort = 2 if hot else 0
    ps = max(1, min(int(num or 20), 50))
    data = cli.get_data(
        f"{API_ROOT}/x/v2/reply",
        {"type": 1, "oid": aid, "pn": 1, "ps": ps, "sort": sort},
        referer=f"https://www.bilibili.com/video/{bv}",
    ) or {}
    rows = [comment_row(x) for x in (data.get("replies") or [])]
    return {
        "bvid": bv,
        "aid": aid,
        "count": len(rows),
        "order": "hot" if hot else "time",
        "total": (data.get("page") or {}).get("count") or 0,
        "comments": rows,
    }


async def danmaku(cli: Client, bvid: str, num: int = 100) -> dict:
    """视频弹幕。

    接口实测（2026-11-20）：`x/v1/dm/list.so` 返回的是 **raw deflate 压缩的 XML**
    （没有 zlib 头，`zlib.decompress(raw, -15)` 才解得开）。
    解压后是 `<d p="时间,模式,字号,颜色,...">正文</d>` 这种结构。

    代码里同时留了一条 protobuf 兜底：万一以后接口换成二进制段，
    也能靠 `_pb_fields` 读出来，不至于整个工具直接空掉。
    """
    import zlib

    bv = norm_bvid(bvid)
    cid = await _cid_of(cli, bv)
    raw = cli.get_bytes(
        f"{API_ROOT}/x/v1/dm/list.so",
        {"oid": cid},
        referer=f"https://www.bilibili.com/video/{bv}",
    )

    # 1) zlib 头（0x78）
    if raw[:1] == b"\x78":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    else:
        # 2) 无头 raw deflate
        try:
            raw = zlib.decompress(raw, -15)
        except zlib.error:
            pass

    limit = max(1, min(int(num or 100), 1000))
    items: list[dict] = []

    # A. XML 路线（当前线上格式）
    text = raw.decode("utf-8", "replace")
    if "<d " in text or "<d p=" in text:
        for m in re.finditer(r'<d p="([^"]*)">([^<]*)</d>', text):
            p = (m.group(1) or "").split(",")
            try:
                t = float(p[0]) if p and p[0] else 0.0
            except ValueError:
                t = 0.0
            items.append(
                {
                    "time": round(t, 2),
                    "mode": int(p[1]) if len(p) > 1 and p[1].isdigit() else 1,
                    "color": int(p[3]) if len(p) > 3 and p[3].isdigit() else 0xFFFFFF,
                    "text": m.group(2) or "",
                }
            )
            if len(items) >= limit:
                break

    # B. protobuf 兜底
    if not items:
        for fno, wire, value in _pb_fields(raw):
            if fno != 1 or wire != 2:
                continue
            elem = _pb_elem(value)
            if elem.get("text"):
                items.append(
                    {
                        "time": round(elem.get("progress", 0) / 1000.0, 2),
                        "mode": elem.get("mode", 1),
                        "color": elem.get("color", 0xFFFFFF),
                        "text": elem["text"],
                    }
                )
            if len(items) >= limit:
                break

    return {"bvid": bv, "cid": cid, "count": len(items), "danmaku": items}


async def subtitle(cli: Client, bvid: str) -> dict:
    bv = norm_bvid(bvid)
    cid = await _cid_of(cli, bv)
    data = cli.get_data(f"{API_ROOT}/x/player/v2", {"bvid": bv, "cid": cid}) or {}
    subs = ((data.get("subtitle") or {}).get("subtitles") or [])
    out: list[dict] = []
    for s in subs[:5]:
        url = str(s.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            continue
        try:
            body = cli.get_data(url) or {}
        except ShoreError:
            body = {}
        lines = [
            {"from": x.get("from"), "to": x.get("to"), "text": x.get("content")}
            for x in (body.get("body") or [])
        ]
        out.append(
            {
                "lan": s.get("lan"),
                "lan_doc": s.get("lan_doc"),
                "count": len(lines),
                "lines": lines,
            }
        )
    return {"bvid": bv, "cid": cid, "count": len(out), "subtitles": out}


# ----------------------------------------------------------------------
# 搜索 / 榜单
# ----------------------------------------------------------------------
async def search(cli: Client, keyword: str, num: int = 10, order: str = "") -> dict:
    kw = str(keyword or "").strip()
    if not kw:
        raise ShoreError("keyword 不能为空")
    ps = max(1, min(int(num or 10), 50))
    data = cli.get_data(
        f"{API_ROOT}/x/web-interface/search/type",
        {"search_type": "video", "keyword": kw, "page": 1, "page_size": ps, "order": order or ""},
        referer="https://search.bilibili.com/",
    ) or {}

    videos: list[dict] = []
    skipped = 0
    dropped = 0
    for it in (data.get("result") or []):
        kind = str(it.get("type") or "video")
        if kind and kind != "video":
            skipped += 1
            continue

        bv = str(it.get("bvid") or "")
        if not _BV_RE.match(bv):
            m = _BV_ANY.search(str(it.get("arcurl") or ""))
            bv = m.group(0) if m else ""
        if not _BV_RE.match(bv):
            dropped += 1
            continue

        row = video_row(it)
        row["bvid"] = bv
        row["type"] = kind
        videos.append(row)

    return {
        "keyword": kw,
        "count": len(videos),
        "skipped": skipped,
        "dropped": dropped,
        "videos": videos,
    }


async def hot_videos(cli: Client, pn: int = 1, ps: int = 20) -> dict:
    data = cli.get_data(
        f"{API_ROOT}/x/web-interface/popular",
        {"pn": max(1, int(pn or 1)), "ps": max(1, min(int(ps or 20), 50))},
    ) or {}
    items = [video_row(x) for x in (data.get("list") or [])]
    return {"count": len(items), "no_more": bool(data.get("no_more")), "items": items}


async def hot_buzzwords(cli: Client, page_num: int = 1, page_size: int = 20, source: str = "hot") -> dict:
    """实时热搜（默认）与旧热词图鉴（备查）。

    两条路的 `source` 必须如实回显，调用方靠它判断拿到的是哪一种。
    """
    size = max(1, min(int(page_size or 20), 50))
    kind = "lexicon" if str(source).lower() == "lexicon" else "hot"

    data = cli.get_data(
        f"{API_ROOT}/x/web-interface/search/square",
        {"limit": size},
        referer="https://www.bilibili.com/",
    ) or {}
    trending = (data.get("trending") or {}).get("list") or []
    if not trending:
        raise ShoreError("热搜接口没有返回条目（可能被风控或接口变更）")

    items = []
    for x in trending[:size]:
        items.append(
            {
                "keyword": x.get("keyword") or "",
                "name": x.get("show_name") or x.get("keyword") or "",
                "heat": x.get("heat_score"),
                "icon": x.get("icon"),
            }
        )
    return {
        "source": kind,
        "fetched_at": now_iso(),
        "page_num": max(1, int(page_num or 1)),
        "count": len(items),
        "total": len(items),
        "items": items,
    }


async def weekly_hot(cli: Client, week: int = 0, num: int = 12, offset: int = 0, order: str = "") -> dict:
    """week=0 返回期数目录（分页），week>0 返回该期视频。"""
    if int(week or 0) == 0:
        data = cli.get_data(f"{API_ROOT}/x/web-interface/popular/series/list") or {}
        all_items = data.get("list") or []
        total = len(all_items)

        off = max(0, int(offset or 0))
        take = max(1, min(int(num or 12), 100))
        window = all_items[off : off + take]
        if str(order).lower() in ("asc", "old"):
            window = list(reversed(window))

        items = [
            {
                "number": x.get("number"),
                "name": x.get("subject") or x.get("name") or f"第 {x.get('number')} 期",
                "status": x.get("status"),
            }
            for x in window
        ]
        has_more = (off + take) < total
        return {
            "total": total,
            "offset": off,
            "count": len(items),
            "has_more": has_more,
            "next_offset": (off + take) if has_more else None,
            "items": items,
        }

    data = cli.get_data(f"{API_ROOT}/x/web-interface/popular/series/one", {"number": int(week)}) or {}
    items = [video_row(x) for x in (data.get("list") or [])]
    return {"week": int(week), "count": len(items), "items": items}


_RANK_RID = {
    "all": 0,
    "full": 0,
    "original": 0,
    "rookie": 0,
    "douga": 1,
    "anime": 1,
    "guochuang": 168,
    "music": 3,
    "dance": 129,
    "game": 4,
    "knowledge": 36,
    "tech": 188,
    "sports": 234,
    "life": 160,
    "food": 211,
    "animal": 217,
    "fashion": 155,
    "ent": 5,
    "cinephile": 181,
    "documentary": 177,
}


async def rank(cli: Client, category: str = "all", day: str = "") -> dict:
    rid = _RANK_RID.get(str(category or "all").lower(), 0)
    params: dict[str, Any] = {"rid": rid}
    if str(day).lower() in ("origin", "original", "原创"):
        params["type"] = "origin"
    data = cli.get_data(f"{API_ROOT}/x/web-interface/ranking/v2", params) or {}
    items = [video_row(x) for x in (data.get("list") or [])]
    return {"category": category or "all", "rid": rid, "count": len(items), "items": items}


_ZONES = [
    (1, "动画"), (13, "番剧"), (167, "国创"), (3, "音乐"), (129, "舞蹈"),
    (4, "游戏"), (36, "知识"), (188, "科技"), (234, "运动"), (160, "生活"),
    (211, "美食"), (217, "动物圈"), (155, "时尚"), (5, "娱乐"), (181, "影视"),
    (177, "纪录片"), (23, "电影"), (11, "电视剧"), (119, "鬼畜"), (138, "搞笑"),
]


async def video_zones() -> dict:
    return {"count": len(_ZONES), "zones": [{"tid": t, "name": n} for t, n in _ZONES]}


# ----------------------------------------------------------------------
# 圈地自萌（只回复 UP 主本人的邀请评论）
# ----------------------------------------------------------------------
_INVITE_HINTS = ("来", "邀请", "欢迎", "一起", "看看", "聊聊", "留言", "回复", "坐坐", "喝茶")


async def _up_uid(cli: Client, bvid: str) -> int:
    info = await _view(cli, bvid)
    return int((info.get("owner") or {}).get("mid") or 0)


async def circle_scan(cli: Client, bvid: str, num: int = 30) -> dict:
    bv = norm_bvid(bvid)
    aid = await _aid_of(cli, bv)
    up = await _up_uid(cli, bv)

    data = cli.get_data(
        f"{API_ROOT}/x/v2/reply",
        {"type": 1, "oid": aid, "pn": 1, "ps": max(1, min(int(num or 30), 50)), "sort": 2},
        referer=f"https://www.bilibili.com/video/{bv}",
    ) or {}

    invites = []
    for r in (data.get("replies") or []):
        member = r.get("member") or {}
        if int(member.get("mid") or 0) != up:
            continue
        text = str((r.get("content") or {}).get("message") or "").strip()
        if not text:
            continue
        if not any(w in text for w in _INVITE_HINTS):
            continue
        invites.append(
            {
                "rpid": r.get("rpid"),
                "text": text[:300],
                "like": r.get("like") or 0,
                "rcount": r.get("rcount") or 0,
                "ctime": r.get("ctime") or 0,
            }
        )

    return {
        "bvid": bv,
        "aid": aid,
        "up_uid": up,
        "count": len(invites),
        "invites": invites,
        "note": "启发式筛选：UP 主本人 + 一级评论 + 含邀请词。转发前请人工确认。",
    }


async def circle_reply(cli: Client, bvid: str, rpid: int, text: str) -> dict:
    """硬闸：rpid 必须是该视频 UP 主本人发出的一级评论，且正文含邀请。"""
    bv = norm_bvid(bvid)
    aid = await _aid_of(cli, bv)
    up = await _up_uid(cli, bv)

    target = cli.get_data(
        f"{API_ROOT}/x/v2/reply/reply",
        {"type": 1, "oid": aid, "root": int(rpid), "pn": 1, "ps": 1},
        referer=f"https://www.bilibili.com/video/{bv}",
    ) or {}

    root_info = target.get("root") or {}
    member = root_info.get("member") or {}
    if int(member.get("mid") or 0) != up:
        raise ShoreError("拒绝：rpid 不是该视频 UP 主本人的一级评论")
    root_text = str((root_info.get("content") or {}).get("message") or "").strip()
    if not any(w in root_text for w in _INVITE_HINTS):
        raise ShoreError("拒绝：该评论正文不含邀请语义")

    body = cli.post_write(
        f"{API_ROOT}/x/v2/reply/add",
        {"oid": aid, "type": 1, "message": str(text or "").strip(), "root": int(rpid), "parent": int(rpid)},
        referer=f"https://www.bilibili.com/video/{bv}",
    )
    return {"bvid": bv, "aid": aid, "rpid": int(rpid), "text": text, "result": body}


# ----------------------------------------------------------------------
# 批量采集
# ----------------------------------------------------------------------
async def crawl(
    cli: Client,
    keyword: str,
    max_videos: int = 3,
    comments_per_video: int = 5,
    get_subtitles: bool = False,
) -> dict:
    take = max(1, min(int(max_videos or 3), 20))
    got = await search(cli, keyword, num=take)
    out: list[dict] = []

    for v in got["videos"][:take]:
        item: dict[str, Any] = {
            "bvid": v["bvid"],
            "title": v["title"],
            "up": v["up"],
            "play": v["play"],
        }
        try:
            c = await comments(cli, v["bvid"], num=comments_per_video)
            item["comments"] = c["comments"]
        except ShoreError as e:
            item["comments_error"] = str(e)

        if get_subtitles:
            try:
                s = await subtitle(cli, v["bvid"])
                item["subtitles"] = s["subtitles"]
            except ShoreError as e:
                item["subtitle_error"] = str(e)

        out.append(item)
        await asyncio.sleep(0.6)  # 连续打接口容易 412，给一点间隔

    return {"keyword": keyword, "count": len(out), "videos": out}
