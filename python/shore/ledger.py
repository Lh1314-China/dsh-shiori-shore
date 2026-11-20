"""调用账本：一行一条，按月分文件。

定位：**留痕，不是限流**。写不进去也不能让工具调用失败——
账本是观测手段，不是业务链路。所以所有异常都吞掉，只写日志。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("shore.ledger")


def _digest(args: dict[str, Any], limit: int = 16) -> str:
    """参数摘要。不落原始参数，避免把私信正文、cookie 之类写进账本。"""
    try:
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(args)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:limit]


def record(
    data_dir: Path | None,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    ok: bool = True,
    elapsed_ms: int = 0,
    note: str = "",
) -> None:
    """追加一条记录。任何异常都不外抛。"""
    if data_dir is None:
        return

    try:
        stamp = time.localtime()
        month = time.strftime("%Y-%m", stamp)
        folder = Path(data_dir) / "ledger"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{month}.jsonl"

        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", stamp),
            "tool": tool,
            "args_digest": _digest(args or {}),
            "outcome": "ok" if ok else "error",
            "elapsed_ms": int(elapsed_ms),
        }
        if note:
            line["note"] = note[:200]

        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()

        # 账本不是日志，别让它无限长：单文件超过 8MB 就滚一个
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                path.replace(path.with_suffix(".jsonl.1"))
        except OSError:
            pass

    except Exception as e:  # noqa: BLE001 —— 账本绝不能让业务失败
        log.warning("写账本失败：%s", e)


class Timer:
    """给工具调用计时的小上下文管理器。"""

    __slots__ = ("start", "elapsed_ms")

    def __init__(self) -> None:
        self.start = 0.0
        self.elapsed_ms = 0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)
