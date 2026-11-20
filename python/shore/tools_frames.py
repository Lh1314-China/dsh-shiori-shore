"""B站视频取帧：playurl 拿 DASH 直链 → ffmpeg 抽帧 → 本地 jpg。

返回的是**路径**，不是图片字节：调用方拿到 path 后用 read_image 打开即可。
这样不用改 MCP 传输层（工具返回值仍是一段 JSON 文本），也不需要宿主侧图片通道。

安全边界：
- 只读 B 站公开/已登录可看的视频；充电/付费/地区限制的视频会在 playurl 或 ffmpeg 处失败，报错不崩
- ffmpeg 只写 <dataDir>/frames/，不碰其它目录；最多保留 200 个文件
- 一次最多 5 帧；每帧 60 秒超时
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

from .client import API_ROOT, Client, ShoreError

REFERER = "https://www.bilibili.com/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAX_FRAMES = 5
FRAME_TIMEOUT = 60
KEEP_FILES = 200


def find_ffmpeg() -> str:
    """SHORE_FFMPEG 优先；其次 PATH；最后两个常见安装位置。"""
    env = os.environ.get("SHORE_FFMPEG", "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in (
        os.environ.get("SHORE_FFMPEG", "ffmpeg"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ):
        if Path(p).is_file():
            return p
    raise ShoreError("找不到 ffmpeg：设 SHORE_FFMPEG 环境变量，或把 ffmpeg 放进 PATH")


def _pick_stream(dash: dict) -> dict:
    vids = [v for v in (dash.get("video") or []) if isinstance(v, dict)]
    if not vids:
        raise ShoreError("没有可用视频流（可能是充电/付费/地区限制视频）")

    def score(v: dict) -> tuple[int, int]:
        try:
            q = int(v.get("id") or 0)
        except (TypeError, ValueError):
            q = 0
        try:
            bw = int(v.get("bandwidth") or 0)
        except (TypeError, ValueError):
            bw = 0
        # 离 720p 最近优先；同距离选带宽低的（更快）
        return (abs(q - 64), bw)

    return sorted(vids, key=score)[0]


def _url_of(v: dict) -> str:
    u = v.get("baseUrl") or v.get("base_url")
    if u:
        return str(u)
    backups = v.get("backupUrl") or v.get("backup_url") or []
    if backups:
        return str(backups[0])
    raise ShoreError("视频流没有可用 URL")


def _ffmpeg_cmd(ff: str, url: str, t: float, width: int, out: Path) -> list[str]:
    return [
        ff,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{t:.3f}",
        "-headers", f"Referer: {REFERER}\r\nUser-Agent: {UA}",
        "-user_agent", UA,
        "-i", url,
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "3",
        "-y",
        str(out),
    ]


def _prune(dir_: Path, keep: int = KEEP_FILES, protected: set[Path] | None = None) -> None:
    protected = protected or set()
    try:
        items = sorted(dir_.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    if len(items) <= keep:
        return
    for p in items[: len(items) - keep]:
        if p in protected:
            continue
        try:
            p.unlink()
        except OSError:
            pass


async def frames(
    cli: Client,
    data_dir: Path,
    bvid: str,
    at: float = 0.0,
    count: int = 1,
    width: int = 960,
    cid: int | None = None,
) -> dict:
    """取 1–5 帧，返回落盘路径。"""
    bv = (bvid or "").strip()
    if not bv:
        raise ShoreError("bvid 不能为空")
    count = max(1, min(int(count or 1), MAX_FRAMES))
    at = max(0.0, float(at or 0))
    width = max(320, min(int(width or 960), 1920))

    view = cli.get_data(f"{API_ROOT}/x/web-interface/view", {"bvid": bv}) or {}
    cid_int = int(cid or view.get("cid") or 0)
    if not cid_int:
        raise ShoreError("拿不到 cid（视频可能不存在或已下架）")
    title = str(view.get("title") or "")
    duration = int(view.get("duration") or 0)

    play = cli.get_data(
        f"{API_ROOT}/x/player/playurl",
        {"bvid": bv, "cid": cid_int, "fnval": 4048, "fourk": 1},
    ) or {}
    stream = _pick_stream(play.get("dash") or {})
    url = _url_of(stream)

    ff = find_ffmpeg()
    out_dir = Path(data_dir) / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    errors: list[str] = []
    for i in range(count):
        t = at + i  # 一秒一帧，简单可预期
        out = out_dir / f"{bv}-{int(t)}s-{int(time.time())}-{i}.jpg"
        cmd = _ffmpeg_cmd(ff, url, t, width, out)
        try:
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=FRAME_TIMEOUT)
        except subprocess.TimeoutExpired:
            errors.append(f"{t:.0f}s: ffmpeg 超时（{FRAME_TIMEOUT}s）")
            continue
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size <= 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            errors.append(f"{t:.0f}s: ffmpeg 失败 {tail[-1][:120] if tail else ''}".strip())
            continue
        files.append({"path": str(out), "at": t, "bytes": out.stat().st_size})

    _prune(out_dir, KEEP_FILES, {Path(f["path"]) for f in files})

    if not files:
        raise ShoreError("一帧都没取到：" + ("；".join(errors) if errors else "未知原因"))

    return {
        "bvid": bv,
        "cid": cid_int,
        "title": title,
        "duration": duration,
        "at": at,
        "files": files,
        "errors": errors,
        "ffmpeg": ff,
        "note": "用 read_image 打开 files[].path 就能看见画面",
    }
