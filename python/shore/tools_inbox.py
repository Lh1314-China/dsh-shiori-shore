"""收件箱：把私信 / AT / 回复揉成一条时间线，外加自动回复的闸门。

分工：
- 本模块只做「读」和「按规则发送」，不自己做判断内容；
- 回什么由模型决定，这里只保证不刷屏、不重复、不在暂停时发。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from . import listen, tools_dm
from .client import Client, ShoreError

CST = timezone(timedelta(hours=8))


def _stamp(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return ""


def _in_quiet(cfg: dict, now: datetime | None = None) -> bool:
    spec = str(cfg.get("quiet_hours") or "").strip()
    if not spec or "-" not in spec:
        return False
    try:
        a, b = spec.split("-", 1)
        h1, m1 = (int(x) for x in a.strip().split(":"))
        h2, m2 = (int(x) for x in b.strip().split(":"))
    except (ValueError, TypeError):
        return False

    cur = now or datetime.now(CST)
    t = cur.hour * 60 + cur.minute
    s = h1 * 60 + m1
    e = h2 * 60 + m2
    if s == e:
        return False
    if s < e:
        return s <= t < e
    return t >= s or t < e


async def inbox(
    cli: Client,
    data_dir,
    limit: int = 20,
    kind: str = "all",
    keyword: str = "",
    unread_only: bool = False,
) -> dict[str, Any]:
    """统一收件箱：私信会话 + AT + 回复，按时间倒序。

    `kind` 取 all / dm / at / reply。`keyword` 只在本地文本上过滤，不打远程接口。
    """
    take = max(1, min(int(limit or 20), 100))
    want = str(kind or "all").lower()
    if want not in ("all", "dm", "at", "reply"):
        raise ShoreError(f"kind 只能是 all / dm / at / reply，收到 {kind!r}")

    cur = listen.load_cursors(data_dir)
    seen = listen.load_seen(data_dir)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    if want in ("all", "dm"):
        try:
            threads = await tools_dm.dm_threads(cli, 40)
            for th in threads.get("threads") or []:
                key = f"dm:{th['talker_uid']}:{th['last_ts']}"
                rows.append(
                    {
                        "type": "dm",
                        "key": key,
                        "uid": int(th.get("talker_uid") or 0),
                        "uname": "",
                        "text": str(th.get("last_text") or ""),
                        "ts": int(th.get("last_ts") or 0),
                        "when": _stamp(int(th.get("last_ts") or 0)),
                        "unread": int(th.get("unread") or 0) > 0,
                        "unread_count": int(th.get("unread") or 0),
                        "from_me": bool(th.get("last_from_me")),
                        "seen": key in seen,
                    }
                )
        except ShoreError as e:
            warnings.append(f"dm: {e}")

    if want in ("all", "at"):
        try:
            news = await tools_dm.received_at_and_likes(cli)
            floor_at = int(cur.get("at") or 0)
            for x in news.get("at") or []:
                iid = int(x.get("id") or 0)
                key = f"at:{iid}"
                rows.append(
                    {
                        "type": "at",
                        "key": key,
                        "uid": int(x.get("uid") or 0),
                        "uname": x.get("uname") or "",
                        "text": str(x.get("text") or ""),
                        "ts": int(x.get("ts") or 0),
                        "when": _stamp(int(x.get("ts") or 0)),
                        "bvid": x.get("bvid") or "",
                        "unread": iid > floor_at,
                        "seen": key in seen,
                    }
                )
        except ShoreError as e:
            warnings.append(f"at: {e}")

    if want in ("all", "reply"):
        try:
            data = await tools_dm.received_replies(cli, 1, 30)
            floor_rp = int(cur.get("reply") or 0)
            for x in data.get("items") or []:
                iid = int(x.get("id") or 0)
                key = f"reply:{iid}"
                rows.append(
                    {
                        "type": "reply",
                        "key": key,
                        "uid": int(x.get("uid") or 0),
                        "uname": x.get("uname") or "",
                        "text": str(x.get("text") or ""),
                        "ts": int(x.get("ts") or 0),
                        "when": _stamp(int(x.get("ts") or 0)),
                        "bvid": x.get("bvid") or "",
                        "title": x.get("title") or "",
                        "unread": iid > floor_rp,
                        "seen": key in seen,
                    }
                )
        except ShoreError as e:
            warnings.append(f"reply: {e}")

    kw = str(keyword or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in f"{r.get('text','')} {r.get('uname','')} {r.get('title','')}".lower()]
    if unread_only:
        rows = [r for r in rows if r.get("unread")]

    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    rows = rows[:take]

    return {
        "count": len(rows),
        "items": rows,
        "unread": sum(1 for r in rows if r.get("unread")),
        "warnings": warnings,
        "note": "私信会话不附带昵称（会话接口不返回），要看昵称请用 bili_user_info 按 uid 查",
    }


async def pending(data_dir, limit: int = 20, mark: bool = True) -> dict[str, Any]:
    """取监听队列里还没投递的事件。mark=True 表示这次已经投递出去了，游标推进。"""
    res = listen.read_pending(data_dir, limit, mark)
    return {
        "count": res["count"],
        "items": res["items"],
        "delivered": res["delivered"],
        "pending_after": res["pending_after"],
    }


async def ack(data_dir, keys: list[str], state: str = "read") -> dict[str, Any]:
    st = str(state or "read").strip().lower()
    if st not in ("read", "ignored"):
        raise ShoreError("state 只能是 read 或 ignored")
    rows = [str(k).strip() for k in (keys or []) if str(k).strip()]
    if not rows:
        raise ShoreError("keys 为空")

    seen = listen.load_seen(data_dir)
    before = len(seen)
    seen.update(rows)
    listen.save_seen(data_dir, seen)
    return {"added": len(seen) - before, "total": len(seen), "state": st}


# ----------------------------------------------------------------------
# 自动回复闸门
# ----------------------------------------------------------------------
def _sent_path(data_dir):
    return listen._dir(data_dir) / "sent.json"


def _load_sent(data_dir) -> dict[str, Any]:
    raw = listen._read_json(_sent_path(data_dir), {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("times", [])
    raw.setdefault("keys", {})
    return raw


async def autoreply(cli: Client, data_dir, uid: int, text: str) -> dict[str, Any]:
    """带闸门的自动回复。手发请走 bili_send_message，这条只给自动流程用。"""
    cfg = listen.load_config(data_dir)
    target = int(uid)
    body = str(text or "").strip()

    if not cfg.get("auto_reply"):
        raise ShoreError("自动回复未开启")
    if listen.is_paused(data_dir):
        raise ShoreError("监听处于暂停状态（listen/pause.flag 存在），不发")
    if target in set(cfg.get("reply_blacklist") or []):
        raise ShoreError(f"{target} 在自动回复黑名单里")
    if not body:
        raise ShoreError("回复内容为空")
    if len(body) > int(cfg.get("max_reply_chars") or 300):
        raise ShoreError(f"超过自动回复长度上限（{cfg.get('max_reply_chars')} 字）")
    if _in_quiet(cfg):
        raise ShoreError(f"处于安静时段（{cfg.get('quiet_hours')}），不发")

    now = int(time.time())
    rec = _load_sent(data_dir)
    times = [int(t) for t in (rec.get("times") or []) if str(t).lstrip("-").isdigit()]
    times = [t for t in times if now - t < 3600]
    if len(times) >= int(cfg.get("max_per_hour") or 20):
        raise ShoreError(f"本小时已发 {len(times)} 条，达到上限 {cfg.get('max_per_hour')}")
    if times and now - max(times) < int(cfg.get("min_gap") or 8):
        raise ShoreError(f"距上一条不足 {cfg.get('min_gap')} 秒，稍等")

    key = f"{target}:{body[:80]}"
    last_same = int((rec.get("keys") or {}).get(key) or 0)
    if last_same and now - last_same < 1800:
        raise ShoreError("同一对象、同样内容 30 分钟内已发过，判为重复")

    result = await tools_dm.send_message(cli, target, body)

    times.append(now)
    rec["times"] = times[-200:]
    keys = rec.get("keys") or {}
    keys[key] = now
    if len(keys) > 300:
        keys = dict(sorted(keys.items(), key=lambda kv: kv[1])[-300:])
    rec["keys"] = keys
    listen._write_json(_sent_path(data_dir), rec)

    return {
        "sent": True,
        "uid": target,
        "text": body[:120],
        "hour_count": len(times),
        "gap_ok": True,
        "result": result.get("result") if isinstance(result, dict) else None,
    }
