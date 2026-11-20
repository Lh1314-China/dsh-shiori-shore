"""HTTP 传输层：统一请求头、WBI 签名、风控退避、错误归一。

三条纪律：

1. **零第三方依赖**。只用标准库（urllib / hashlib / json / time / urllib.parse），
   不用 requests、不用 aiohttp、不用任何 B 站 SDK。
2. **所有出站请求都必须经过 `Client._request`**。这样审计只看一个地方，
   也保证没有任何模块能偷偷绕开 cookie 与签名。
3. **绝不写 stdout**。MCP 用 stdout 走 JSON-RPC，多打印一个字符就会污染协议帧。
   需要日志一律走 `logging`（由 server 配置到 stderr）。

风控约定（实测有效的组合）：

- 固定正常 UA + `Referer: https://www.bilibili.com/`
- HTTP 412 退避：1s → 3s → 9s，最多 3 次，仍失败就抛错，不无限重试
- 写操作一律带 `csrf=<bili_jct>`，缺 jct 直接拒绝，不发出半截请求
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

log = logging.getLogger("shore.client")

# ----------------------------------------------------------------------
# Windows + 校园网 / 家庭宽带常见问题：IPv6 黑洞
#
# 实测（这台机器，2026-11-20）：
#   getaddrinfo 返回 4 个 IPv6 地址（2409:8c50:...）
#   socket.create_connection 逐个尝试，IPv6 全部不可达，
#   每个都挂到系统级超时，单次连接 80 秒才回退到 IPv4。
#   一次工具调用 160 秒，看起来像「接口很慢」，其实是地址族选错。
#
# 这里默认强制 IPv4。需要 IPv6 的环境（或未来 IPv6 真的通了），
# 设 SHORE_IPV6=1 关掉本补丁即可，不写死任何本机地址。
# ----------------------------------------------------------------------
if os.environ.get("SHORE_IPV6", "") != "1":
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):  # type: ignore[no-redef]
        if family in (0, socket.AF_UNSPEC):
            family = socket.AF_INET
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = _ipv4_only

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HOME_REFERER = "https://www.bilibili.com/"
API_ROOT = "https://api.bilibili.com"
LIVE_ROOT = "https://api.live.bilibili.com"

# WBI 混排表。这是 B 站前端公开的固定置换表（64 个位置），
# 不是从任何第三方库里抄来的实现——它是协议常量。
_MIXIN_ORDER = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# WBI 签名前必须剔除的字符（协议规定，出现在参数值里会算错签名）
_WBI_STRIP = "!'()*"


class ShoreError(Exception):
    """业务或传输错误。`code` 是 B 站返回码，`http` 是 HTTP 状态码。"""

    def __init__(self, message: str, *, code: int | None = None, http: int | None = None):
        super().__init__(message)
        self.code = code
        self.http = http

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"error": str(self)}
        if self.code is not None:
            out["code"] = self.code
        if self.http is not None:
            out["http"] = self.http
        return out


class RiskControlError(ShoreError):
    """风控类错误（412 / -352 / -412 / 频繁）。调用方应停止而不是重试。"""


@dataclass
class Credential:
    """登录态。四个字段都是 cookie 里直接取的名字。"""

    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""
    buvid3: str = ""

    @property
    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.sessdata:
            out["SESSDATA"] = self.sessdata
        if self.bili_jct:
            out["bili_jct"] = self.bili_jct
        if self.dedeuserid:
            out["DedeUserID"] = self.dedeuserid
        if self.buvid3:
            out["buvid3"] = self.buvid3
        return out

    @property
    def logged_in(self) -> bool:
        """有 SESSDATA 就算「有登录态」；写操作还要求 bili_jct。"""
        return bool(self.sessdata)

    @property
    def can_write(self) -> bool:
        return bool(self.sessdata and self.bili_jct)

    def uid_int(self) -> int:
        try:
            return int(self.dedeuserid or 0)
        except (TypeError, ValueError):
            return 0

    def require_write(self) -> None:
        if not self.can_write:
            raise ShoreError("缺少写操作凭证（SESSDATA 或 bili_jct 为空），已拒绝发送")


@dataclass
class Client:
    """一个带登录态的 HTTP 客户端。

    实例应当长生命周期复用：WBI 的 mixin key 按 1 小时缓存，
    每次工具调用都新建实例会让签名接口慢一倍。
    """

    cred: Credential
    timeout: float = 15.0
    retries: int = 3
    _mixin_key: str = field(default="", init=False, repr=False)
    _mixin_at: float = field(default=0.0, init=False, repr=False)
    _opener: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """构建 opener。

        默认**不用系统代理**。实测这台机器开着 `ProxyEnable=1` 指向本地 12450 端口，
        但那个代理并不转发 B 站，urllib 会一路等到超时（单请求 160 秒）。
        国内直连 513ms 就回来了，所以默认直连是正确取舍。

        需要代理的环境（公司网 / 海外）设 `SHORE_PROXY=http://host:port` 即可覆盖，
        不把本机地址写进代码。
        """
        proxy = os.environ.get("SHORE_PROXY", "").strip()
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            log.info("使用显式代理：%s", proxy)
        else:
            handler = urllib.request.ProxyHandler({})
            log.debug("未配置 SHORE_PROXY，走直连")
        self._opener = urllib.request.build_opener(handler)

    # ------------------------------------------------------------------
    # WBI
    # ------------------------------------------------------------------
    def _fetch_mixin_key(self) -> str:
        """从 nav 接口取 img/sub 两个 key，按混排表拼成 32 位 mixin key。"""
        if self._mixin_key and (time.time() - self._mixin_at) < 3600:
            return self._mixin_key

        raw = self.get_json(f"{API_ROOT}/x/web-interface/nav", {}, check_code=False)
        data = raw.get("data") or {}
        wbi = data.get("wbi_img") or {}
        img_url = str(wbi.get("img_url") or "")
        sub_url = str(wbi.get("sub_url") or "")
        if not img_url or not sub_url:
            raise ShoreError("无法获取 wbi_img（登录态失效或接口变更）")

        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        merged = img_key + sub_key
        if len(merged) < 64:
            raise ShoreError(f"wbi key 长度异常：{len(merged)}")

        self._mixin_key = "".join(merged[i] for i in _MIXIN_ORDER)[:32]
        self._mixin_at = time.time()
        return self._mixin_key

    def _sign(self, params: Mapping[str, Any]) -> dict[str, str]:
        """给参数加 wts + w_rid。返回新的参数字典（不改原对象）。

        `w_webid` 是 WBI 新版强校验的设备指纹：space 系列接口
        （如 `/x/space/wbi/acc/info`）缺了它，就算 w_rid 完全正确也直接 -352。
        实测（2026-11-20）用 `buvid3` 当 w_webid 即可通过。
        """
        mixin = self._fetch_mixin_key()
        cleaned: dict[str, str] = {}
        for k, v in params.items():
            if v is None:
                continue
            s = str(v)
            cleaned[k] = "".join(ch for ch in s if ch not in _WBI_STRIP)

        if "w_webid" not in cleaned and getattr(self.cred, "buvid3", ""):
            cleaned["w_webid"] = self.cred.buvid3

        cleaned["wts"] = str(int(time.time()))
        ordered = urllib.parse.urlencode(sorted(cleaned.items()))
        cleaned["w_rid"] = hashlib.md5((ordered + mixin).encode("utf-8")).hexdigest()
        return cleaned

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------
    def _headers(self, referer: str | None) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": referer or HOME_REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "close",
        }

    def _headers_with_cookies(self, referer: str | None) -> dict[str, str]:
        """请求头 + 登录 cookie。

        补充说明：原实现只带 UA/Referer，从不发 Cookie，
        所有登录态接口都会被判"未登录"（nav 永远 -101）。
        """
        headers = self._headers(referer)
        cookies = self.cred.cookies
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return headers

    def _request(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        *,
        referer: str | None = None,
        wbi: bool = False,
    ) -> dict:
        """发一次请求，返回**完整**响应体（含 code / data / message）。"""
        params = dict(params or {})
        if wbi:
            params = self._sign(params)

        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)

        body: bytes | None = None
        if data is not None:
            body = urllib.parse.urlencode({k: v for k, v in data.items() if v is not None}).encode("utf-8")

        last_error: Exception | None = None

        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=body, headers=self._headers_with_cookies(referer), method=method)
            if body is not None:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")

            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                text = raw.decode("utf-8", "replace")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    raise ShoreError(f"响应不是 JSON（前 80 字：{text[:80]!r}）")

            except urllib.error.HTTPError as e:
                if e.code == 412:
                    wait = 1 * (3 ** attempt)
                    log.warning("命中 412 风控，%s 秒后重试（第 %s/%s 次）", wait, attempt + 1, self.retries)
                    time.sleep(wait)
                    last_error = RiskControlError("HTTP 412 风控拦截", http=412)
                    continue
                raise ShoreError(f"HTTP {e.code}", http=e.code)

            except urllib.error.URLError as e:
                raise ShoreError(f"网络不可达：{e.reason}")

            except TimeoutError:
                raise ShoreError(f"请求超时（{self.timeout}s）")

        raise last_error or ShoreError("请求失败（已重试）")

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------
    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        referer: str | None = None,
        wbi: bool = False,
        check_code: bool = True,
    ) -> dict:
        body = self._request("GET", url, params, None, referer=referer, wbi=wbi)
        if check_code:
            code = int(body.get("code") or 0)
            if code != 0:
                msg = str(body.get("message") or body.get("msg") or f"code={code}")
                if code in (412, -412, -352, -351):
                    raise RiskControlError(msg, code=code)
                raise ShoreError(msg, code=code)
        return body

    def get_data(self, url: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """只要 data 字段。绝大多数接口用这个。"""
        body = self.get_json(url, params, check_code=True, **kwargs)
        return body.get("data")

    def post_json(
        self,
        url: str,
        data: Mapping[str, Any] | None = None,
        *,
        referer: str | None = None,
        wbi: bool = False,
        check_code: bool = True,
    ) -> dict:
        body = self._request("POST", url, None, data, referer=referer, wbi=wbi)
        if check_code:
            code = int(body.get("code") or 0)
            if code != 0:
                msg = str(body.get("message") or body.get("msg") or f"code={code}")
                if code in (412, -412, -352, -351):
                    raise RiskControlError(msg, code=code)
                raise ShoreError(msg, code=code)
        return body

    def post_write(self, url: str, data: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """写操作统一入口：先确认有 jct，再自动补 csrf。"""
        self.cred.require_write()
        payload = dict(data or {})
        payload.setdefault("csrf", self.cred.bili_jct)
        payload.setdefault("csrf_token", self.cred.bili_jct)
        body = self.post_json(url, payload, **kwargs)
        return body.get("data")

    def get_bytes(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        referer: str | None = None,
    ) -> bytes:
        """拿原始字节响应（弹幕段是压缩二进制，不能先解码成 str）。"""
        params = dict(params or {})
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, headers=self._headers_with_cookies(referer), method="GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise ShoreError(f"HTTP {e.code}", http=e.code)
        except urllib.error.URLError as e:
            raise ShoreError(f"网络不可达：{e.reason}")

    def get_text(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        referer: str | None = None,
    ) -> str:
        """拿纯文本 / XML 响应（弹幕列表这类不是 JSON 的接口）。

        有些节点即使不要求压缩也会回 deflate（zlib 头 0x78 0x9c），这里顺手解掉。
        """
        params = dict(params or {})
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, headers=self._headers_with_cookies(referer), method="GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ShoreError(f"HTTP {e.code}", http=e.code)
        except urllib.error.URLError as e:
            raise ShoreError(f"网络不可达：{e.reason}")

        if raw[:2] == b"\x78\x9c":
            import zlib

            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                pass

        return raw.decode("utf-8", "replace")

    def get_live(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        room_id: int = 0,
        check_code: bool = False,
    ) -> dict:
        """直播域接口。Referer 要指到具体房间，否则部分接口返回空。

        默认**不抛业务异常**：直播接口返回码很杂（-352 / 房间不存在 / 未开播），
        由调用方决定哪种算失败。要直接拿 data 的调用方自己判 code。
        """
        referer = f"https://live.bilibili.com/{room_id}" if room_id else "https://live.bilibili.com/"
        return self.get_json(LIVE_ROOT + path, params, referer=referer, check_code=check_code)
