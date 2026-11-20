#!/usr/bin/env python3
"""10 条只读现场探针。用真实账号打真实接口，逐条打印 ok / 耗时 / 关键字段。

用法：
    <venv>\\Scripts\\python.exe test\\live-probe.py

只读，不发任何弹幕 / 评论 / 私信。
凭证来源与运行期一致：BILI_CRED_FILE → dataDir → 插件 python/ → ~/.dsh/bili-duty。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = ROOT / "python"
sys.path.insert(0, str(PY))

DATA_DIR = Path(os.environ.get("BILI_DUTY_HOME") or (Path.home() / ".dsh" / "shiori-shore"))

from shore import auth, tools_dm, tools_live, tools_user, tools_video  # noqa: E402
from shore.client import Client, ShoreError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str) -> None:
    RESULTS.append((label, ok, detail))
    print(f"{'✓' if ok else '✗'} {label} — {detail}")


async def timed(label: str, coro, judge):
    t0 = time.perf_counter()
    try:
        data = await coro
    except ShoreError as e:
        record(label, False, f"ShoreError code={e.code} http={e.http} {e}")
        return
    except Exception as e:  # noqa: BLE001
        record(label, False, f"{type(e).__name__}: {e}")
        return
    ms = int((time.perf_counter() - t0) * 1000)
    ok, detail = judge(data)
    record(label, ok, f"{ms}ms {detail}")


async def main() -> int:
    cred, cred_path = auth.load(DATA_DIR, PY)
    print(f"凭证：{cred_path or '未找到'}   登录={cred.logged_in}")
    if not cred.logged_in:
        print("没有可用凭证，探针无法继续")
        return 2

    cli = Client(cred, timeout=20)

    # 1 视频信息
    await timed(
        "1 video_info",
        tools_video.video_info(cli, "BV1ACxS67EbZ"),
        lambda d: (bool(d.get("title")), f"title={d.get('title')}"),
    )

    # 2 评论
    await timed(
        "2 comments(5)",
        tools_video.comments(cli, "BV1ACxS67EbZ", 5),
        lambda d: (d.get("count") == 5 or len(d.get("comments") or []) >= 1, f"count={d.get('count')}"),
    )

    # 3 弹幕
    await timed(
        "3 danmaku(10)",
        tools_video.danmaku(cli, "BV1ACxS67EbZ", 10),
        lambda d: (bool(d.get("danmaku")), f"count={d.get('count')} first={(d.get('danmaku') or [{}])[0].get('text','')[:20]}"),
    )

    # 4 搜索
    await timed(
        "4 search(2)",
        tools_video.search(cli, "DeepSeek Harness", 2),
        lambda d: (
            bool(d.get("videos")) and all(str(v.get("bvid") or "").startswith("BV") for v in d["videos"]),
            f"count={d.get('count')} bvids={[v.get('bvid') for v in (d.get('videos') or [])]}",
        ),
    )

    # 5 每周必看
    await timed(
        "5 weekly_hot",
        tools_video.weekly_hot(cli, 0, 12, 0, ""),
        lambda d: (d.get("count") == 12 and d.get("has_more") is True, f"count={d.get('count')} total={d.get('total')} has_more={d.get('has_more')}"),
    )

    # 6 实时热搜
    await timed(
        "6 hot_buzzwords",
        tools_video.hot_buzzwords(cli, 1, 10, "hot"),
        lambda d: (d.get("source") == "hot" and isinstance(d.get("fetched_at"), str), f"source={d.get('source')} fetched_at={d.get('fetched_at')} first={(d.get('items') or [{}])[0].get('name','')}"),
    )

    # 7 用户信息
    await timed(
        "7 user_info",
        tools_user.user_info(cli, 1378940616),
        lambda d: (bool(d.get("name")), f"name={d.get('name')} fans={d.get('fans')}"),
    )

    # 8 直播热门 + 房间
    await timed(
        "8a live_hot(3)",
        tools_live.live_hot(cli, 3),
        lambda d: (len(d.get("rooms") or []) >= 1, f"count={d.get('count')}"),
    )
    await timed(
        "8b live_room",
        tools_live.live_room(cli, 1735752300),
        lambda d: (d.get("live_status") is not None, f"live_status={d.get('live_status')} title={d.get('title')}"),
    )

    # 9 直播弹幕
    await timed(
        "9 live_chat",
        tools_live.live_chat(cli, 1735752300, 3),
        lambda d: (True, f"count={d.get('count')} note={d.get('note') or ''}"),
    )

    # 10 私信
    await timed(
        "10a dm_threads(3)",
        tools_dm.dm_threads(cli, 3),
        lambda d: (True, f"count={d.get('count')}"),
    )
    await timed(
        "10b dm_inbox",
        tools_dm.dm_inbox(cli, 1378940616, 20),
        lambda d: (
            any("我也在" in (m.get("text") or "") for m in (d.get("messages") or [])),
            f"count={d.get('count')} hit={'我也在' in json.dumps(d, ensure_ascii=False)}",
        ),
    )

    bad = [x for x in RESULTS if not x[1]]
    print(f"\n合计 {len(RESULTS)} 条，失败 {len(bad)} 条")
    print("PROBE OK" if not bad else "PROBE FAILED")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
