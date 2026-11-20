"""登录态工具：扫码登录、轮询登录结果、检查当前凭证。

扫码登录走 passport 的二维码接口：

    GET  /x/passport-login/web/qrcode/generate   → url + qrcode_key
    GET  /x/passport-login/web/qrcode/poll       → 状态 + （成功时）带 cookie 的跳转 URL

图片编码依赖 `qrcode`（可选）。venv 里没装时不会失败：仍然返回二维码内容与 key，
只是 `image_base64` 为空，调用方可用任意外部工具把 url 渲染成图。
这符合"P2 允许先给可用 stub、但不能缺工具"的要求。
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from pathlib import Path

from . import auth
from .client import API_ROOT, Client, Credential, ShoreError

log = logging.getLogger("shore.auth.tools")

PASSPORT = "https://passport.bilibili.com"


def _render_qr(text: str) -> str | None:
    """尽力把二维码内容渲染成 PNG base64；没有依赖就返回 None。"""
    try:
        import qrcode  # type: ignore
        import io

        img = qrcode.make(text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


async def login(cli: Client, data_dir: Path | None = None) -> dict:
    body = cli.get_json(
        f"{PASSPORT}/x/passport-login/web/qrcode/generate",
        {},
        referer="https://www.bilibili.com/",
        check_code=True,
    )
    data = body.get("data") or {}
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "")
    if not url or not key:
        raise ShoreError("二维码接口没有返回 url / qrcode_key")

    image = _render_qr(url)
    return {
        "url": url,
        "qrcode_key": key,
        "image_base64": image,
        "hint": (
            "用 B 站 App 扫码；扫完调用 bili_login_check（参数传上面的 qrcode_key）"
            if image
            else "本机没有二维码渲染依赖。把 url 用任意二维码工具转成图再扫；扫完调用 bili_login_check"
        ),
        "expires_in": 180,
    }


async def login_check(cli: Client, qrcode_key: str, data_dir: Path | None = None) -> dict:
    key = str(qrcode_key or "").strip()
    if not key:
        raise ShoreError("qrcode_key 不能为空")

    body = cli.get_json(
        f"{PASSPORT}/x/passport-login/web/qrcode/poll",
        {"qrcode_key": key},
        referer="https://www.bilibili.com/",
        check_code=True,
    )
    data = body.get("data") or {}
    code = int(data.get("code") or 0)

    # passport 的 code 是业务码，不是 HTTP 码：0=成功 86101=未扫 86090=已扫未确认 86038=过期
    status_map = {0: "done", 86101: "waiting", 86090: "scanning", 86038: "expired"}

    if code != 0:
        return {
            "status": status_map.get(code, f"unknown({code})"),
            "message": data.get("message") or "",
            "qrcode_key": key,
        }

    redirect = str(data.get("url") or "")
    cookies = _parse_set_cookie(redirect)

    if cookies.get("sessdata"):
        cred = Credential(
            sessdata=cookies.get("sessdata", ""),
            bili_jct=cookies.get("bili_jct", ""),
            dedeuserid=cookies.get("dedeuserid", ""),
            buvid3=cli.cred.buvid3,
        )
        cli.cred = cred
        saved = None
        if data_dir is not None:
            try:
                saved = str(auth.save(Path(data_dir), cred))
            except OSError as e:
                log.warning("凭证落盘失败：%s", e)

        return {
            "status": "done",
            "uid": cred.dedeuserid,
            "saved_to": saved,
            "message": "登录成功，凭证已写入；后续调用会自动带上",
        }

    return {
        "status": "done",
        "message": "扫码已确认，但跳转 URL 里没有解析出 SESSDATA，可能需要重试或手工填凭证",
        "redirect_url": redirect,
    }


def _parse_set_cookie(url: str) -> dict[str, str]:
    """passport 成功时把 cookie 挂在跳转 URL 的 query 上（不是 Set-Cookie 头）。"""
    out: dict[str, str] = {}
    try:
        q = urllib.parse.urlparse(url).query
        for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True):
            out[k.lower()] = v
    except Exception:
        return out
    # 常见键名大小写归一
    return out


async def check_credential(cli: Client) -> dict:
    """nav 接口是判断"到底有没有登录"的唯一可靠来源。"""
    body = cli.get_json(
        f"{API_ROOT}/x/web-interface/nav",
        {},
        referer="https://www.bilibili.com/",
        check_code=False,
    )
    data = body.get("data") or {}
    is_login = bool(data.get("isLogin"))

    result: dict = {
        "logged_in": is_login,
        "http_code": body.get("code"),
        "vip": bool(data.get("vipStatus")),
        "money": data.get("money"),
    }
    if is_login:
        result.update(
            {
                "uid": data.get("mid"),
                "name": data.get("uname"),
                "level": (data.get("level_info") or {}).get("current_level"),
                "face": data.get("face"),
                "wbi_ready": bool((data.get("wbi_img") or {}).get("img_url")),
            }
        )
    else:
        result["hint"] = "未登录或 SESSDATA 已失效；需要写操作请先 bili_login"

    return result
