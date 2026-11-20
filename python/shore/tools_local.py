"""本地值班工具：报告 / 判重表 / 归档 / 记忆 / 日记 / 一句话 / 设置。

共同约束：

- 只写 dataDir（以及其下的 out/、diary/、ledger/），不写别的地方
- 写失败必须显式报错，不静默吞掉（值班轮靠这些文件交接，静默失败比报错更危险）
- 全部 UTF-8，中文路径可写
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .client import ShoreError

CST = timezone(timedelta(hours=8))


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _month() -> str:
    return datetime.now(CST).strftime("%Y-%m")


def _stamp() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise ShoreError(f"写文件失败：{path}（{e}）")


def _append(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
    except OSError as e:
        raise ShoreError(f"追加文件失败：{path}（{e}）")


# ----------------------------------------------------------------------
# 报告 / 日记 / 一句话
# ----------------------------------------------------------------------
async def report_save(data_dir: Path, markdown: str) -> dict:
    body = str(markdown or "")
    if not body.strip():
        raise ShoreError("报告内容为空")

    path = Path(data_dir) / "out" / f"{_today()}.md"
    _write(path, body)
    return {"path": str(path), "bytes": len(body.encode("utf-8")), "date": _today()}


async def diary_write(data_dir: Path, markdown: str) -> dict:
    body = str(markdown or "")
    if not body.strip():
        raise ShoreError("日记内容为空")

    path = Path(data_dir) / "diary" / f"{_today()}.md"
    _write(path, body)
    return {"path": str(path), "bytes": len(body.encode("utf-8")), "date": _today()}


async def say(data_dir: Path, text: str) -> dict:
    body = str(text or "").strip()
    if not body:
        raise ShoreError("内容不能为空")
    if len(body) > 50:
        raise ShoreError(f"超过了 50 字上限（当前 {len(body)} 字）")

    path = Path(data_dir) / "say.txt"
    _write(path, body)
    return {"path": str(path), "text": body}


# ----------------------------------------------------------------------
# 判重表 / 归档 / 记忆
# ----------------------------------------------------------------------
async def seen_read(data_dir: Path, limit_chars: int = 2000) -> dict:
    path = Path(data_dir) / "seen.md"
    if not path.is_file():
        return {"path": str(path), "exists": False, "count": 0, "text": ""}

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ShoreError(f"读判重表失败：{e}")

    take = max(100, min(int(limit_chars or 2000), 20000))
    tail = raw[-take:]
    return {"path": str(path), "exists": True, "chars": len(raw), "count": raw.count("\n"), "text": tail}


async def seen_append(data_dir: Path, lines: list[str]) -> dict:
    rows = [str(x).strip() for x in (lines or []) if str(x).strip()]
    if not rows:
        raise ShoreError("没有可追加的行")

    path = Path(data_dir) / "seen.md"
    _append(path, "".join(x + "\n" for x in rows))
    return {"path": str(path), "appended": len(rows)}


async def archive_append(data_dir: Path, lines: list[str]) -> dict:
    rows = [str(x).strip() for x in (lines or []) if str(x).strip()]
    if not rows:
        raise ShoreError("没有可追加的行")

    path = Path(data_dir) / "out" / f"{_month()}.md"
    _append(path, "".join(f"- {x}\n" for x in rows))
    return {"path": str(path), "appended": len(rows), "month": _month()}


async def memory_append(data_dir: Path, what: str, why: str = "", bvid: str = "") -> dict:
    body = str(what or "").strip()
    if not body:
        raise ShoreError("what 不能为空")

    rec: dict[str, Any] = {"ts": _stamp(), "what": body}
    if why:
        rec["why"] = str(why).strip()
    if bvid:
        rec["bvid"] = str(bvid).strip()

    path = Path(data_dir) / "memory.jsonl"
    _append(path, json.dumps(rec, ensure_ascii=False) + "\n")
    return {"path": str(path), "record": rec}


# ----------------------------------------------------------------------
# 设置
# ----------------------------------------------------------------------
_DEFAULTS: dict[str, Any] = {
    "autoRun": False,
    "autoComment": False,
    "replyToComments": False,
    "autoLike": False,
    "autoFollow": False,
    "pollIntervalMinutes": 30,
    "quietHours": "23:00-07:00",
}

# 会「自动对外说话 / 自动跑」的键：改它们必须 confirm=true
_SENSITIVE = {"autoRun", "autoComment", "replyToComments", "autoLike", "autoFollow"}
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "settings.json"


def _load_settings(data_dir: Path) -> dict:
    path = _settings_path(data_dir)
    if not path.is_file():
        return dict(_DEFAULTS)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    if isinstance(obj, dict):
        merged.update(obj)
    return merged


def _coerce(value: str, old: Any) -> Any:
    if isinstance(old, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")
    if isinstance(old, int) and not isinstance(old, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ShoreError(f"该设置需要整数，收到：{value!r}")
    return value


async def setting_get(data_dir: Path) -> dict:
    settings = _load_settings(data_dir)
    return {
        "path": str(_settings_path(data_dir)),
        "settings": settings,
        "keys": sorted(settings.keys()),
        "sensitive": sorted(_SENSITIVE),
    }


async def setting_set(data_dir: Path, key: str, value: str, confirm: bool = False) -> dict:
    name = str(key or "").strip()
    if not _KEY_RE.match(name):
        raise ShoreError(f"非法的设置名：{name!r}")

    settings = _load_settings(data_dir)
    if name not in settings and name not in _DEFAULTS:
        raise ShoreError(f"不在白名单里：{name}。可用：{sorted(_DEFAULTS)}")

    if name in _SENSITIVE and not confirm:
        raise ShoreError(f"「{name}」会改变自动行为，必须显式传 confirm=true 才写")

    old = settings.get(name)
    new_value = _coerce(value, old)

    settings[name] = new_value
    path = _settings_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        raise ShoreError(f"写设置失败：{e}")

    return {"path": str(path), "key": name, "before": old, "after": new_value, "confirmed": bool(confirm)}
