"""MCP 服务端：注册 52 个工具，名字与参数严格对齐 `test/schemas.json`。

三条纪律：

1. **工具名必须是 `bili_*`**。host 侧会把前缀换成 `shore_`，这里不能预先加前缀。
2. **只往 stdout 写 JSON-RPC**。所有日志走 stderr（FastMCP 自己保证协议帧，
   我们只保证自己不 print）。
3. **任何工具都不能把异常漏出去**。统一在 `_emit` 里转成 `{"ok": false, "error": ...}`，
   否则一次异常会打断整条 MCP 连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import auth, ledger, listen, tools_inbox
from .client import Client, Credential, ShoreError
from . import tools_auth, tools_dm, tools_frames, tools_live, tools_local, tools_user, tools_video

# ----------------------------------------------------------------------
# 运行环境
# ----------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("BILI_DUTY_HOME") or (Path.home() / ".dsh" / "shiori-shore"))
PYTHON_DIR = Path(__file__).resolve().parent.parent
LEDGER_ON = os.environ.get("SHORE_LEDGER", "1") != "0"

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("shore.server")

@asynccontextmanager
async def _lifespan(_app: Any):
    """进程活多久，监听就活多久。

    这就是「只在模型醒着时监听」的实现方式：轮询任务挂在 MCP 进程里，
    会话一关，进程一退，监听自动消失——不需要谁记得去关。
    """
    task: asyncio.Task | None = None
    try:
        task = asyncio.create_task(listen.poller_loop(lambda: _CLI, DATA_DIR))
        log.info("消息监听已挂载（interval=%ss）", listen.load_config(DATA_DIR).get("interval"))
    except Exception:  # noqa: BLE001 —— 监听起不来不能影响工具本身
        log.exception("监听器启动失败")

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("监听器停止时出错")


mcp = FastMCP("shore", lifespan=_lifespan)

_CRED, _CRED_PATH = auth.load(DATA_DIR, PYTHON_DIR)
_CLI = Client(_CRED)
_LAST_QR_KEY = ""


def _refresh_credential() -> None:
    """扫码登录或运行期换号后，重新装配一次客户端。"""
    global _CRED, _CLI
    cred, _ = auth.load(DATA_DIR, PYTHON_DIR)
    if cred.logged_in:
        _CRED = cred
        _CLI = Client(cred)


async def _emit(name: str, coro: Any, args: dict[str, Any] | None = None) -> str:
    """统一出口：包 ok 字段、记账本、把异常压成 JSON。"""
    start = time.perf_counter()
    ok = True
    payload: dict[str, Any]

    try:
        data = await coro
        payload = {"ok": True}
        if isinstance(data, dict):
            payload.update(data)
        else:
            payload["data"] = data
    except ShoreError as e:
        ok = False
        payload = {"ok": False, **e.as_dict()}
    except Exception as e:  # noqa: BLE001 —— 绝不能让异常穿过 MCP 边界
        ok = False
        payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        log.exception("工具 %s 失败", name)
    finally:
        if LEDGER_ON:
            ledger.record(
                DATA_DIR,
                name,
                args or {},
                ok=ok,
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )

    return json.dumps(payload, ensure_ascii=False, default=str)


# ======================================================================
# 登录态
# ======================================================================
@mcp.tool()
async def bili_login() -> str:
    """生成B站登录二维码，返回二维码内容（可选 base64 图片）供用户扫码。

    用户扫码后，调用 bili_login_check 检查登录状态。
    """
    global _LAST_QR_KEY
    try:
        data = await tools_auth.login(_CLI, DATA_DIR)
        _LAST_QR_KEY = str(data.get("qrcode_key") or "")
        return await _emit("bili_login", _as_coro(data), {})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@mcp.tool()
async def bili_login_check() -> str:
    """检查B站扫码登录状态。

    返回 status：waiting（未扫）/ scanning（已扫待确认）/ done（成功）/ expired（过期）。
    成功时会自动把凭证写入 dataDir。
    """
    global _LAST_QR_KEY
    if not _LAST_QR_KEY:
        return json.dumps(
            {"ok": False, "error": "还没有发起登录。请先调用 bili_login 拿二维码"},
            ensure_ascii=False,
        )
    result = await tools_auth.login_check(_CLI, _LAST_QR_KEY, DATA_DIR)
    if result.get("status") == "done" and result.get("saved_to"):
        _refresh_credential()
    return json.dumps({"ok": True, **result}, ensure_ascii=False)


@mcp.tool()
async def bili_check_credential() -> str:
    """检查当前B站登录凭证是否有效。"""
    return await _emit("bili_check_credential", tools_auth.check_credential(_CLI), {})


# ======================================================================
# 视频：读
# ======================================================================
@mcp.tool()
async def bili_search(keyword: str, num: int = 10, order: str = "") -> str:
    """搜索B站视频。"""
    return await _emit("bili_search", tools_video.search(_CLI, keyword, num, order), {"keyword": keyword})


@mcp.tool()
async def bili_comments(bvid: str, num: int = 20, order: str = "hot") -> str:
    """获取B站视频的评论（可按热度或按时间）。"""
    return await _emit("bili_comments", tools_video.comments(_CLI, bvid, num, order), {"bvid": bvid})


@mcp.tool()
async def bili_circle_scan(bvid: str, num: int = 30) -> str:
    """圈地自萌：只扫描视频 UP 主本人发出的一级邀请评论。不会发任何评论。"""
    return await _emit("bili_circle_scan", tools_video.circle_scan(_CLI, bvid, num), {"bvid": bvid})


@mcp.tool()
async def bili_circle_reply(bvid: str, rpid: int, text: str) -> str:
    """圈地自萌专用：只回复 UP 主邀请评论的楼中楼（硬闸）。"""
    return await _emit("bili_circle_reply", tools_video.circle_reply(_CLI, bvid, rpid, text), {"bvid": bvid})


@mcp.tool()
async def bili_subtitle(bvid: str) -> str:
    """获取B站视频的AI字幕（语音转文字）。"""
    return await _emit("bili_subtitle", tools_video.subtitle(_CLI, bvid), {"bvid": bvid})


@mcp.tool()
async def bili_danmaku(bvid: str, num: int = 100) -> str:
    """获取B站视频的弹幕。"""
    return await _emit("bili_danmaku", tools_video.danmaku(_CLI, bvid, num), {"bvid": bvid})


@mcp.tool()
async def bili_video_info(bvid: str) -> str:
    """获取B站视频的详细信息。"""
    return await _emit("bili_video_info", tools_video.video_info(_CLI, bvid), {"bvid": bvid})


@mcp.tool()
async def bili_frames(bvid: str, at: float = 0, count: int = 1, width: int = 960) -> str:
    """取B站视频的真实画面（jpg 落盘，返回本地路径）。

    拿到返回里的 files[].path 后，用 read_image 打开就能看见画面。

    Args:
        bvid: 视频 BV 号
        at: 起始秒数，默认 0
        count: 取几帧（1–5，一秒一帧），默认 1
        width: 输出宽度（320–1920），默认 960
    """
    return await _emit(
        "bili_frames",
        tools_frames.frames(_CLI, DATA_DIR, bvid, at, count, width),
        {"bvid": bvid, "at": at, "count": count},
    )


@mcp.tool()
async def bili_crawl(
    keyword: str,
    max_videos: int = 3,
    comments_per_video: int = 5,
    get_subtitles: bool = False,
) -> str:
    """批量采集：搜索B站视频并抓取每个视频的评论和字幕。"""
    return await _emit(
        "bili_crawl",
        tools_video.crawl(_CLI, keyword, max_videos, comments_per_video, get_subtitles),
        {"keyword": keyword},
    )


# ======================================================================
# 视频：写
# ======================================================================
@mcp.tool()
async def bili_like(bvid: str, like: bool = True) -> str:
    """给视频点赞 / 取消点赞（用登录的那个号）。"""
    return await _emit("bili_like", tools_user.like(_CLI, bvid, like), {"bvid": bvid})


@mcp.tool()
async def bili_fav(bvid: str, fav: bool = True, media_id: int = 0) -> str:
    """收藏 / 取消收藏视频；不给 media_id 就进默认收藏夹。"""
    return await _emit("bili_fav", tools_user.fav(_CLI, bvid, fav, media_id), {"bvid": bvid})


@mcp.tool()
async def bili_reply(bvid: str, text: str, rpid: int = 0, root: int = 0) -> str:
    """在B站视频下发表评论或回复某条评论。"""
    return await _emit("bili_reply", tools_user.reply(_CLI, bvid, text, rpid, root), {"bvid": bvid})


@mcp.tool()
async def bili_send_dynamic(
    text: str,
    images: list[str] | None = None,
    topic_id: int = 0,
    schedule_time: int = 0,
) -> str:
    """发布B站图文动态（本版只支持纯文本；带图与定时明确报错）。"""
    return await _emit(
        "bili_send_dynamic",
        tools_user.send_dynamic(_CLI, text, images, topic_id, schedule_time),
        {"len": len(text or "")},
    )


@mcp.tool()
async def bili_send_opus(
    title: str,
    content: str,
    images: list[str] | None = None,
    category_id: int = 0,
) -> str:
    """发布B站图文（Opus，新版专栏）——P2，本版明确不实现。"""
    return await _emit("bili_send_opus", tools_user.send_opus(_CLI, title, content, images, category_id), {})


@mcp.tool()
async def bili_upload_video(
    video_path: str,
    title: str,
    desc: str = "",
    tid: int = 0,
    tags: str = "",
    cover_path: str = "",
    source: str = "",
    dynamic_text: str = "",
    no_reprint: bool = False,
) -> str:
    """上传视频到B站——P2，本版明确不实现。"""
    return await _emit("bili_upload_video", tools_user.upload_video(_CLI), {"title": title})


@mcp.tool()
async def bili_upload_video_multi(
    video_paths: list[str],
    page_titles: list[str],
    title: str,
    desc: str = "",
    tid: int = 0,
    tags: str = "",
    cover_path: str = "",
    source: str = "",
) -> str:
    """上传多P视频到B站——P2，本版明确不实现。"""
    return await _emit("bili_upload_video_multi", tools_user.upload_video_multi(_CLI), {"title": title})


# ======================================================================
# 榜单与分区
# ======================================================================
@mcp.tool()
async def bili_video_zones() -> str:
    """获取B站视频常用分区ID列表，供上传视频时选择tid参数。"""
    return await _emit("bili_video_zones", tools_video.video_zones(), {})


@mcp.tool()
async def bili_hot_videos(pn: int = 1, ps: int = 20) -> str:
    """获取B站当前热门视频列表。"""
    return await _emit("bili_hot_videos", tools_video.hot_videos(_CLI, pn, ps), {})


@mcp.tool()
async def bili_hot_buzzwords(page_num: int = 1, page_size: int = 20, source: str = "hot") -> str:
    """获取B站热搜词/热门关键词。source=hot 走实时热搜，lexicon 走旧热词图鉴。"""
    return await _emit(
        "bili_hot_buzzwords",
        tools_video.hot_buzzwords(_CLI, page_num, page_size, source),
        {},
    )


@mcp.tool()
async def bili_weekly_hot(week: int = 0, num: int = 12, offset: int = 0, order: str = "") -> str:
    """获取B站每周必看视频推荐。week=0 取期数目录（可分页），week>0 取该期视频。"""
    return await _emit("bili_weekly_hot", tools_video.weekly_hot(_CLI, week, num, offset, order), {})


@mcp.tool()
async def bili_rank(category: str = "all", day: str = "") -> str:
    """获取B站各分区排行榜。"""
    return await _emit("bili_rank", tools_video.rank(_CLI, category, day), {})


# ======================================================================
# 用户与收藏
# ======================================================================
@mcp.tool()
async def bili_user_info(uid: int) -> str:
    """获取B站用户的详细信息。"""
    return await _emit("bili_user_info", tools_user.user_info(_CLI, uid), {"uid": uid})


@mcp.tool()
async def bili_user_videos(
    uid: int,
    pn: int = 1,
    ps: int = 30,
    order: str = "pubdate",
    keyword: str = "",
) -> str:
    """获取B站用户的投稿视频列表。"""
    return await _emit("bili_user_videos", tools_user.user_videos(_CLI, uid, pn, ps, order, keyword), {"uid": uid})


@mcp.tool()
async def bili_favorite_lists(uid: int = 0) -> str:
    """获取用户的收藏夹列表（uid=0 表示当前登录账号）。"""
    return await _emit("bili_favorite_lists", tools_user.favorite_lists(_CLI, uid), {})


@mcp.tool()
async def bili_favorite_content(media_id: int, page: int = 1, keyword: str = "") -> str:
    """获取收藏夹内的视频列表。"""
    return await _emit("bili_favorite_content", tools_user.favorite_content(_CLI, media_id, page, keyword), {"media_id": media_id})


@mcp.tool()
async def bili_feed(num: int = 12) -> str:
    """首页推荐流（登录态）。拿不到就退回热门，source 会说明走的是哪条路。"""
    return await _emit("bili_feed", tools_user.feed(_CLI, num), {})


@mcp.tool()
async def bili_dynamic_feed(num: int = 15) -> str:
    """关注动态：我关注的人最近发了什么。"""
    return await _emit("bili_dynamic_feed", tools_user.dynamic_feed(_CLI, num), {})


# ======================================================================
# 私信与通知
# ======================================================================
@mcp.tool()
async def bili_send_message(receiver_uid: int, text: str) -> str:
    """给B站用户发送私信。"""
    return await _emit("bili_send_message", tools_dm.send_message(_CLI, receiver_uid, text), {"receiver_uid": receiver_uid})


@mcp.tool()
async def bili_unread_messages() -> str:
    """获取B站未读消息数（私信、@、回复、点赞等）。"""
    return await _emit("bili_unread_messages", tools_dm.unread_messages(_CLI), {})


@mcp.tool()
async def bili_dm_inbox(uid: int = 0, limit: int = 30) -> str:
    """读「指定用户 ↔ 本号」的私信内容（不给 uid 就读最新会话）。"""
    return await _emit("bili_dm_inbox", tools_dm.dm_inbox(_CLI, uid or None, limit), {})


@mcp.tool()
async def bili_dm_threads(num: int = 20) -> str:
    """私信会话列表：谁最近来找过我（含未读数、最后一句）。"""
    return await _emit("bili_dm_threads", tools_dm.dm_threads(_CLI, num), {})


@mcp.tool()
async def bili_received_replies() -> str:
    """获取最近收到的评论回复通知。"""
    return await _emit("bili_received_replies", tools_dm.received_replies(_CLI), {})


@mcp.tool()
async def bili_received_at_and_likes() -> str:
    """获取最近收到的@提及和点赞通知。"""
    return await _emit("bili_received_at_and_likes", tools_dm.received_at_and_likes(_CLI), {})


@mcp.tool()
async def bili_guard_scan(
    bvids: list[str] | None = None,
    since_at_id: int = 0,
    since_at_time: int = 0,
    since_ctimes: dict | None = None,
    max_pages: int = 2,
) -> str:
    """值班扫描：一次拿回「谁 @ 了我」+「我最近视频下的新评论」。"""
    return await _emit(
        "bili_guard_scan",
        tools_dm.guard_scan(_CLI, bvids, since_at_id, since_at_time, since_ctimes, max_pages),
        {},
    )


# ======================================================================
# 直播
# ======================================================================
@mcp.tool()
async def bili_live_room(room_id: int) -> str:
    """看一个直播间：在不在播、标题、主播、分区、人气。"""
    return await _emit("bili_live_room", tools_live.live_room(_CLI, room_id), {"room_id": room_id})


@mcp.tool()
async def bili_live_hot(num: int = 30, parent_area_id: int = 0) -> str:
    """直播热门：现在正在播、有人在看的房间。"""
    return await _emit("bili_live_hot", tools_live.live_hot(_CLI, num, parent_area_id), {})


@mcp.tool()
async def bili_live_chat(room_id: int, num: int = 20) -> str:
    """看一个直播间最近的弹幕（读，不发）。"""
    return await _emit("bili_live_chat", tools_live.live_chat(_CLI, room_id, num), {"room_id": room_id})


@mcp.tool()
async def bili_live_listen(room_id: int, seconds: int = 20) -> str:
    """在直播间蹲一会儿，只收「这期间的新弹幕」。"""
    return await _emit("bili_live_listen", tools_live.live_listen(_CLI, room_id, seconds), {"room_id": room_id})


@mcp.tool()
async def bili_live_send_danmaku(room_id: int, text: str, reply_mid: int = 0) -> str:
    """去直播间发一条弹幕（普通用户单条上限 20 字）。"""
    return await _emit(
        "bili_live_send_danmaku",
        tools_live.live_send_danmaku(_CLI, room_id, text, reply_mid),
        {"room_id": room_id},
    )


# ======================================================================
# 表态
# ======================================================================
@mcp.tool()
async def bili_coin(bvid: str, num: int = 1, also_like: bool = False) -> str:
    """给视频投币（每支最多 2 枚；硬币是账号自己的，不是钱）。"""
    return await _emit("bili_coin", tools_user.coin(_CLI, bvid, num, also_like), {"bvid": bvid})


@mcp.tool()
async def bili_follow(uid: int, action: str = "follow") -> str:
    """关注 / 取关 / 拉黑 / 取消拉黑。"""
    return await _emit("bili_follow", tools_user.follow(_CLI, uid, action), {"uid": uid})


# ======================================================================
# 本地值班（只写 dataDir）
# ======================================================================
@mcp.tool()
async def bili_report_save(markdown: str) -> str:
    """把这一轮的 Markdown 报告整份写到 dataDir/out/<今天>.md（覆盖写）。"""
    return await _emit("bili_report_save", tools_local.report_save(DATA_DIR, markdown), {})


@mcp.tool()
async def bili_seen_read(limit_chars: int = 2000) -> str:
    """读判重表 seen.md（返回尾部若干字符，够判重就行）。"""
    return await _emit("bili_seen_read", tools_local.seen_read(DATA_DIR, limit_chars), {})


@mcp.tool()
async def bili_seen_append(lines: list[str]) -> str:
    """往判重表 seen.md 追加记录（一行一条，别重复追加同一行）。"""
    return await _emit("bili_seen_append", tools_local.seen_append(DATA_DIR, lines), {})


@mcp.tool()
async def bili_archive_append(lines: list[str]) -> str:
    """往归档 out/<YYYY-MM>.md 追加记录。"""
    return await _emit("bili_archive_append", tools_local.archive_append(DATA_DIR, lines), {})


@mcp.tool()
async def bili_memory_append(what: str, why: str = "", bvid: str = "") -> str:
    """往记忆 memory.jsonl 追加一条（值得跨天记住的观察，不是流水账）。"""
    return await _emit("bili_memory_append", tools_local.memory_append(DATA_DIR, what, why, bvid), {})


@mcp.tool()
async def bili_diary_write(markdown: str) -> str:
    """把今天的日记整份写到 diary/<今天>.md（覆盖写，一天一篇）。"""
    return await _emit("bili_diary_write", tools_local.diary_write(DATA_DIR, markdown), {})


@mcp.tool()
async def bili_say(text: str) -> str:
    """留一句「鱼说的话」（一句话，≤50 字，覆盖上一句）。"""
    return await _emit("bili_say", tools_local.say(DATA_DIR, text), {})


@mcp.tool()
async def bili_setting_get() -> str:
    """看现在这几个设置是什么值（改设置前先看一眼，别凭记忆）。"""
    return await _emit("bili_setting_get", tools_local.setting_get(DATA_DIR), {})


@mcp.tool()
async def bili_setting_set(key: str, value: str, confirm: bool = False) -> str:
    """改一个设置。会改自动行为的几个键必须传 confirm=true。"""
    return await _emit("bili_setting_set", tools_local.setting_set(DATA_DIR, key, value, confirm), {"key": key})


# ======================================================================
# 监听 / 收件箱（0.2 扩展，不属于 52 条契约）
# ======================================================================
@mcp.tool()
async def bili_listen_set(
    enabled: bool | None = None,
    types: str = "",
    interval: int = 0,
    auto_reply: bool | None = None,
    min_gap: int = 0,
    max_per_hour: int = 0,
    max_reply_chars: int = 0,
    quiet_hours: str | None = None,
    pause: bool | None = None,
) -> str:
    """改监听配置。这是「权限按钮」的开关本体。

    - enabled：是否轮询（默认开）
    - types：逗号分隔，dm,at,reply（默认三个都听）
    - interval：轮询秒数，30~600，默认 75
    - auto_reply：是否允许自动回复（默认开）
    - min_gap / max_per_hour：两条自动回复的最小间隔、每小时上限
    - max_reply_chars：自动回复长度上限
    - quiet_hours：安静时段，如 "23:00-07:00"，空串=不限
    - pause：true=立刻停发（监听继续，只是不发），false=恢复
    """
    patch: dict[str, Any] = {}
    if enabled is not None:
        patch["enabled"] = enabled
    if types:
        patch["types"] = [t.strip() for t in str(types).split(",") if t.strip()]
    if interval:
        patch["interval"] = interval
    if auto_reply is not None:
        patch["auto_reply"] = auto_reply
    if min_gap:
        patch["min_gap"] = min_gap
    if max_per_hour:
        patch["max_per_hour"] = max_per_hour
    if max_reply_chars:
        patch["max_reply_chars"] = max_reply_chars
    if quiet_hours is not None:
        patch["quiet_hours"] = quiet_hours

    cfg = listen.save_config(DATA_DIR, patch)
    if pause is not None:
        listen.set_paused(DATA_DIR, bool(pause))

    return json.dumps(
        {"ok": True, "config": cfg, "paused": listen.is_paused(DATA_DIR)},
        ensure_ascii=False,
    )


@mcp.tool()
async def bili_listen_status() -> str:
    """看监听跑到哪一步：是否活着、心跳多久前、队列里积了多少、配置是什么。"""
    return json.dumps({"ok": True, **listen.status(DATA_DIR)}, ensure_ascii=False)


@mcp.tool()
async def bili_inbox(limit: int = 20, type: str = "all", keyword: str = "", unread_only: bool = False) -> str:
    """统一收件箱：私信 + AT + 回复揉成一条时间线，按时间倒序。

    type 取 all / dm / at / reply；keyword 只在本地文本上过滤。
    """
    return await _emit(
        "bili_inbox",
        tools_inbox.inbox(_CLI, DATA_DIR, limit, type, keyword, unread_only),
        {"limit": limit, "type": type},
    )


@mcp.tool()
async def bili_inbox_pending(limit: int = 20, mark: bool = True) -> str:
    """取监听队列里还没投递的新消息。定时唤醒的那一轮先调这个。

    mark=true 表示这次算投递过了，游标推进；想留着重读就传 false。
    """
    return await _emit("bili_inbox_pending", tools_inbox.pending(DATA_DIR, limit, mark), {"limit": limit})


@mcp.tool()
async def bili_inbox_ack(keys: list[str], state: str = "read") -> str:
    """把某几条标记成已读/已忽略，下次收件箱里会带上 seen=true。"""
    return await _emit("bili_inbox_ack", tools_inbox.ack(DATA_DIR, keys, state), {"n": len(keys or [])})


@mcp.tool()
async def bili_dm_autoreply(uid: int, text: str) -> str:
    """自动回复专用发送口：带闸门（暂停 / 安静时段 / 黑名单 / 频率 / 去重）。

    手动回私信请用 bili_send_message，那个不受这些限制。
    """
    return await _emit("bili_dm_autoreply", tools_inbox.autoreply(_CLI, DATA_DIR, uid, text), {"uid": uid})


# ======================================================================
# 启动
# ======================================================================
def _real_image() -> str:
    """进程真实映像路径（venv 的 python.exe 可能只是启动壳）。"""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.QueryFullProcessImageNameW
        fn.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if fn(k32.GetCurrentProcess(), 0, buf, ctypes.byref(size)) and buf.value:
            return buf.value
    except Exception:
        pass
    return getattr(sys, "_base_executable", "") or sys.executable


def _record_identity() -> None:
    """把解释器身份写进 python-pid.log，方便 host 侧对账。异常一律吞掉。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "python-pid.log"
        try:
            if path.exists() and path.stat().st_size > 200_000:
                path.replace(DATA_DIR / "python-pid.log.1")
        except OSError:
            pass

        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "python": sys.version.split()[0],
            "sys_executable": sys.executable,
            "real_image": _real_image(),
            "cwd": os.getcwd(),
            "server": str(Path(__file__).resolve()),
            "cred_file": str(_CRED_PATH) if _CRED_PATH else "",
            "data_dir": str(DATA_DIR),
            "logged_in": _CRED.logged_in,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _as_coro(data: Any) -> Any:
    """把一个现成的 dict 包成可 await 的对象，复用 _emit 的出口逻辑。"""
    return data


def main() -> None:
    _record_identity()
    log.info(
        "汐岸启动：dataDir=%s，凭证=%s，登录=%s，账本=%s",
        DATA_DIR,
        _CRED_PATH or "未找到",
        _CRED.logged_in,
        LEDGER_ON,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
