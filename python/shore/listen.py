"""B站消息监听：轮询 → 事件队列 → 唤醒。

为什么只有轮询：B站对个人账号没有任何消息推送通道（私信 / AT / 回复都只有拉取接口），
WebSocket 是未公开的私有协议，不作为主线。

「只在模型醒着时监听」是**结构性保证**，不靠开关自觉：
轮询任务跑在本 MCP 进程里，进程跟着会话生命周期生死——会话关了，轮询就没了。

节奏：默认 75 秒一问（下限 60，连续空闲退避到 120）。未登录时退到 600 秒并只记一次日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import tools_dm
from .client import Client, ShoreError

log = logging.getLogger("shore.listen")

DEFAULT_CONFIG: dict[str, Any] = {
    # 默认第三档：开着，且允许自动回复（默认值）
    "enabled": True,
    "types": ["dm", "at", "reply"],
    "interval": 75,
    "min_interval": 60,
    "max_interval": 120,
    "auto_reply": True,
    "min_gap": 8,
    "max_per_hour": 20,
    "max_reply_chars": 300,
    "quiet_hours": "",  # 空 = 不限制；"23:00-07:00" 可跨午夜
    "reply_blacklist": [],
}

# 进程内运行状态（供 bili_listen_status 读，不落盘）
_STATE: dict[str, Any] = {
    "started_at": 0,
    "poll_count": 0,
    "last_poll": 0,
    "last_ok": 0,
    "last_error": "",
    "last_new": 0,
    "next_in": 0,
    "covering": False,
}


# ----------------------------------------------------------------------
# 文件读写
# ----------------------------------------------------------------------
def _dir(data_dir: Path) -> Path:
    p = Path(data_dir) / "listen"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return default


def _write_json(path: Path, obj: Any) -> None:
    """原子写：先写 .tmp 再 replace，断电也不会留半截文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("写 %s 失败", path)


def config_path(d) -> Path:
    return _dir(d) / "config.json"


def load_config(data_dir) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    raw = _read_json(config_path(data_dir), {})
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULT_CONFIG:
                cfg[k] = v
    # 兜底：类型只认白名单
    cfg["types"] = [t for t in (cfg.get("types") or []) if t in ("dm", "at", "reply")]
    try:
        cfg["interval"] = max(30, min(int(cfg["interval"]), 600))
    except (TypeError, ValueError):
        cfg["interval"] = DEFAULT_CONFIG["interval"]
    return cfg


def save_config(data_dir, patch: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config(data_dir)
    for k, v in (patch or {}).items():
        if k not in DEFAULT_CONFIG or v is None:
            continue
        if k == "types":
            cfg["types"] = [t for t in v if t in ("dm", "at", "reply")]
        elif k in ("enabled", "auto_reply"):
            cfg[k] = bool(v)
        elif k in ("interval", "min_gap", "max_per_hour", "max_reply_chars", "min_interval", "max_interval"):
            try:
                cfg[k] = int(v)
            except (TypeError, ValueError):
                continue
        elif k == "quiet_hours":
            cfg[k] = str(v or "").strip()
        elif k == "reply_blacklist":
            cfg[k] = [int(x) for x in (v or []) if str(x).strip().lstrip("-").isdigit()]
    cfg["interval"] = max(30, min(int(cfg["interval"]), 600))
    cfg["min_interval"] = max(30, min(int(cfg["min_interval"]), cfg["interval"]))
    cfg["max_interval"] = max(cfg["interval"], min(int(cfg["max_interval"]), 600))
    _write_json(config_path(data_dir), cfg)
    return cfg


# ----------------------------------------------------------------------
# 心跳 / 暂停
# ----------------------------------------------------------------------
def touch_heartbeat(data_dir, **extra: Any) -> None:
    rec = {"ts": int(time.time()), "pid": __import__("os").getpid(), **extra}
    _write_json(_dir(data_dir) / "heartbeat.json", rec)


def heartbeat(data_dir) -> dict[str, Any]:
    rec = _read_json(_dir(data_dir) / "heartbeat.json", {})
    if not isinstance(rec, dict):
        return {"ts": 0, "age": None}
    ts = int(rec.get("ts") or 0)
    return {**rec, "age": (int(time.time()) - ts) if ts else None}


def is_paused(data_dir) -> bool:
    return (_dir(data_dir) / "pause.flag").is_file()


def set_paused(data_dir, paused: bool) -> bool:
    f = _dir(data_dir) / "pause.flag"
    if paused:
        try:
            f.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
    else:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    return is_paused(data_dir)


# ----------------------------------------------------------------------
# 游标 / 已读
# ----------------------------------------------------------------------
def load_cursors(data_dir) -> dict[str, Any]:
    raw = _read_json(_dir(data_dir) / "cursors.json", {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("dm", {})
    raw.setdefault("at", 0)
    raw.setdefault("reply", 0)
    # boot：每个信源各自建过基线没有。用凭证字段判断「登没登录」不可靠,
    # 所以基线必须跟「这一轮真的成功拿到数据」绑在一起。
    raw.setdefault("boot", {})
    return raw


def save_cursors(data_dir, cur: dict[str, Any]) -> None:
    _write_json(_dir(data_dir) / "cursors.json", cur)


def load_seen(data_dir) -> set[str]:
    raw = _read_json(_dir(data_dir) / "seen.json", {})
    keys = raw.get("keys") if isinstance(raw, dict) else None
    return set(keys or [])


def save_seen(data_dir, keys: set[str]) -> None:
    keep = list(keys)[-3000:]
    _write_json(_dir(data_dir) / "seen.json", {"keys": keep})


# ----------------------------------------------------------------------
# 事件队列
# ----------------------------------------------------------------------
def _queue_path(data_dir) -> Path:
    return _dir(data_dir) / "events.jsonl"


def _queue_state(data_dir) -> dict[str, int]:
    raw = _read_json(_dir(data_dir) / "queue.json", {})
    if not isinstance(raw, dict):
        raw = {}
    return {"seq": int(raw.get("seq") or 0), "delivered": int(raw.get("delivered") or 0)}


def append_events(data_dir, events: list[dict[str, Any]]) -> int:
    """把新事件追加进队列，返回本条事件的 seq（没有事件返回 0）。"""
    if not events:
        return 0
    st = _queue_state(data_dir)
    seq = st["seq"]
    path = _queue_path(data_dir)
    try:
        # 太胖就轮转：把旧队列挪走，seq 保持单调（游标不会倒退）
        if path.is_file() and path.stat().st_size > 8 * 1024 * 1024:
            path.replace(_dir(data_dir) / f"events-{int(time.time())}.jsonl")
    except OSError:
        pass

    last = seq
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for ev in events:
                seq += 1
                ev["seq"] = seq
                fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                last = seq
    except OSError:
        log.exception("写事件队列失败")
        return 0

    st["seq"] = last
    _write_json(_dir(data_dir) / "queue.json", st)
    return last


def read_pending(data_dir, limit: int = 20, mark: bool = True) -> dict[str, Any]:
    """取未投递的事件（seq > delivered）。mark=True 则推进游标。"""
    st = _queue_state(data_dir)
    path = _queue_path(data_dir)
    if not path.is_file():
        return {"count": 0, "items": [], "pending_after": 0, "delivered": st["delivered"]}

    items: list[dict[str, Any]] = []
    max_seq = st["delivered"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                s = int(ev.get("seq") or 0)
                if s <= st["delivered"]:
                    continue
                items.append(ev)
                max_seq = max(max_seq, s)
                if len(items) >= max(1, min(int(limit or 20), 200)):
                    break
    except OSError:
        log.exception("读事件队列失败")

    if mark and items:
        st["delivered"] = max_seq
        _write_json(_dir(data_dir) / "queue.json", st)

    return {
        "count": len(items),
        "items": items,
        "delivered": st["delivered"],
        "pending_after": max(0, st["seq"] - st["delivered"]),
    }


# ----------------------------------------------------------------------
# 轮询
# ----------------------------------------------------------------------
async def _poll_dm(cli: Client, data_dir, cur: dict, events: list[dict], *, bootstrap: bool) -> None:
    threads = await tools_dm.dm_threads(cli, 30)
    for th in threads.get("threads") or []:
        uid = int(th.get("talker_uid") or 0)
        if not uid or int(th.get("unread") or 0) <= 0:
            continue
        try:
            box = await tools_dm.dm_inbox(cli, uid, 20)
        except ShoreError as e:
            log.warning("拉会话 %s 失败：%s", uid, e)
            continue

        msgs = box.get("messages") or []
        if not msgs:
            continue

        floor = int(float((cur.get("dm") or {}).get(str(uid), 0) or 0))
        newest = max(int(m.get("ts") or 0) for m in msgs)

        if not bootstrap:
            for m in msgs:
                if m.get("from_me"):
                    continue
                ts = int(m.get("ts") or 0)
                if ts <= floor:
                    continue
                events.append(
                    {
                        "type": "dm",
                        "key": f"dm:{uid}:{ts}",
                        "ts": ts,
                        "uid": uid,
                        "uname": "",
                        "text": str(m.get("text") or "")[:300],
                        "session_uid": uid,
                        "detected_at": int(time.time()),
                    }
                )

        cur.setdefault("dm", {})[str(uid)] = max(floor, newest)


async def _poll_at(cli: Client, data_dir, cur: dict, events: list[dict], *, bootstrap: bool) -> None:
    news = await tools_dm.received_at_and_likes(cli)
    rows = news.get("at") or []
    floor = int(cur.get("at") or 0)
    top = floor
    for x in rows:
        iid = int(x.get("id") or 0)
        top = max(top, iid)
        if bootstrap or iid <= floor:
            continue
        events.append(
            {
                "type": "at",
                "key": f"at:{iid}",
                "ts": int(x.get("ts") or 0),
                "uid": int(x.get("uid") or 0),
                "uname": x.get("uname") or "",
                "text": str(x.get("text") or "")[:300],
                "bvid": x.get("bvid") or "",
                "detected_at": int(time.time()),
            }
        )
    cur["at"] = top


async def _poll_reply(cli: Client, data_dir, cur: dict, events: list[dict], *, bootstrap: bool) -> None:
    data = await tools_dm.received_replies(cli, 1, 30)
    rows = data.get("items") or []
    floor = int(cur.get("reply") or 0)
    top = floor
    for x in rows:
        iid = int(x.get("id") or 0)
        top = max(top, iid)
        if bootstrap or iid <= floor:
            continue
        events.append(
            {
                "type": "reply",
                "key": f"reply:{iid}",
                "ts": int(x.get("ts") or 0),
                "uid": int(x.get("uid") or 0),
                "uname": x.get("uname") or "",
                "text": str(x.get("text") or "")[:300],
                "bvid": x.get("bvid") or "",
                "detected_at": int(time.time()),
            }
        )
    cur["reply"] = top


async def poll_once(cli: Client, data_dir, *, social: bool = True) -> dict[str, Any]:
    """跑一轮。**按信源**建基线：某类第一次真正成功拉到数据时不产出事件，
    只把游标推到当前位置，避免把历史消息当成新消息炸一遍。

    基线不能靠「cursors.json 存不存在」判断——凭证文件字段齐全但 SESSDATA 过期时，
    logged_in 仍是 True，第一轮会全错，基线就白建了。
    """
    cfg = load_config(data_dir)
    cur = load_cursors(data_dir)
    boot: dict[str, Any] = cur.setdefault("boot", {})
    events: list[dict] = []
    errors: list[str] = []
    booted_now: list[str] = []

    if not cli.cred.logged_in:
        return {"ok": False, "reason": "not_logged_in", "new": 0, "bootstrap": True, "types": []}

    for kind, fn in (("dm", _poll_dm), ("at", _poll_at), ("reply", _poll_reply)):
        if kind not in (cfg.get("types") or []):
            continue
        if kind != "dm" and not social:
            continue

        is_baseline = not bool(boot.get(kind))
        try:
            await fn(cli, data_dir, cur, events, bootstrap=is_baseline)
            boot[kind] = True
            if is_baseline:
                booted_now.append(kind)
        except ShoreError as e:
            errors.append(f"{kind}: {e}")
        except Exception as e:  # noqa: BLE001 —— 单类失败不能打断整轮
            log.exception("%s 轮询异常", kind)
            errors.append(f"{kind}: {type(e).__name__}: {e}")
        await asyncio.sleep(0)

    save_cursors(data_dir, cur)
    append_events(data_dir, events)

    # SESSDATA 过期时各接口都会回 -101 / 账号未登录：这是「没登录」而不是「轮询失败」，
    # 必须让上层降到 600 秒，否则就是 75 秒一次白打。
    not_login = not events and errors and all(("未登录" in e or "(-101)" in e or "-101" in e) for e in errors)

    return {
        "ok": not errors,
        "reason": "not_logged_in" if not_login else "; ".join(errors)[:300],
        "new": len(events),
        "bootstrap_created": booted_now,
        "types": list(cfg.get("types") or []),
    }


async def poller_loop(cli_getter: Callable[[], Client], data_dir) -> None:
    """常驻轮询。由 FastMCP 的 lifespan 启动，随进程退出而结束。"""
    _STATE["started_at"] = int(time.time())
    _STATE["covering"] = True
    log.info("监听器启动：目录=%s", _dir(data_dir))

    idle = 0
    not_login_logged = False
    first_round_done = False

    try:
        await asyncio.sleep(4)  # 让 MCP 先把 stdio 握手走完
        while True:
            cfg = load_config(data_dir)
            if not cfg.get("enabled"):
                _STATE["next_in"] = 30
                await asyncio.sleep(30)
                continue

            social = (_STATE["poll_count"] % 2 == 0) or not first_round_done
            try:
                res = await poll_once(cli_getter(), data_dir, social=social)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "reason": f"{type(e).__name__}: {e}", "new": 0}
                log.exception("轮询整体失败")

            _STATE["poll_count"] += 1
            _STATE["last_poll"] = int(time.time())
            _STATE["last_new"] = int(res.get("new") or 0)
            first_round_done = True

            if res.get("ok"):
                _STATE["last_ok"] = _STATE["last_poll"]
                _STATE["last_error"] = ""
            else:
                _STATE["last_error"] = str(res.get("reason") or "")[:300]

            if res.get("reason") == "not_logged_in":
                if not not_login_logged:
                    log.warning("未登录，监听降速到 600 秒一次；恢复登录后会自己加速")
                    not_login_logged = True
                interval = 600
            else:
                not_login_logged = False
                base = int(cfg.get("interval") or 75)
                idle = 0 if res.get("new") else idle + 1
                interval = base if idle < 6 else min(int(cfg.get("max_interval") or 120), base + 15 * (idle // 6))

            touch_heartbeat(
                data_dir,
                interval=interval,
                poll_count=_STATE["poll_count"],
                last_new=_STATE["last_new"],
                covering=True,
            )
            _STATE["next_in"] = interval
            await asyncio.sleep(max(20, interval))
    except asyncio.CancelledError:
        _STATE["covering"] = False
        log.info("监听器停止")
        raise


# ----------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------
def status(data_dir) -> dict[str, Any]:
    cfg = load_config(data_dir)
    hb = heartbeat(data_dir)
    q = _queue_state(data_dir)
    return {
        "config": cfg,
        "paused": is_paused(data_dir),
        "heartbeat": hb,
        "heartbeat_age_s": hb.get("age"),
        "queue": {"total": q["seq"], "delivered": q["delivered"], "pending": max(0, q["seq"] - q["delivered"])},
        "runtime": dict(_STATE),
        "alive": bool(hb.get("age") is not None and hb["age"] <= max(180, int(cfg.get("interval") or 75) * 3)),
    }
