"""凭证定位与解析。

查找顺序（与 v0.1.1 完全一致，不改变用户已有的登录行为）：

1. 环境变量 `BILI_CRED_FILE` 指定的文件
2. `<dataDir>/bili_credential.json`
3. 插件 `python/` 目录下的 `bili_credential.json`
4. 鱼塘家目录 `~/.dsh/bili-duty/bili_credential.json`

文件内容兼容三种写法，因为历史上三种都出现过：

- 对象：`{"sessdata": "...", "bili_jct": "...", "dedeuserid": "...", "buvid3": "..."}`
  （键名大小写都认：`SESSDATA` / `sessdata` / `SessData`）
- cookie 字符串：`"SESSDATA=xxx; bili_jct=yyy; DedeUserID=zzz"`
- 浏览器导出的列表：`[{"name": "SESSDATA", "value": "xxx"}, ...]`
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .client import Credential

log = logging.getLogger("shore.auth")

SESS_KEYS = ("sessdata", "sess_data")
JCT_KEYS = ("bili_jct", "csrf", "csrf_token")
UID_KEYS = ("dedeuserid", "dede_user_id", "uid", "mid")
BUVID_KEYS = ("buvid3", "buvid_3")


def candidate_paths(data_dir: Path | None, plugin_dir: Path | None) -> list[Path]:
    """按优先级列出候选文件（不检查存在性）。"""
    out: list[Path] = []

    env = os.environ.get("BILI_CRED_FILE", "").strip()
    if env:
        out.append(Path(env).expanduser())

    if data_dir:
        out.append(Path(data_dir).expanduser() / "bili_credential.json")

    if plugin_dir:
        out.append(Path(plugin_dir).expanduser() / "bili_credential.json")

    out.append(Path.home() / ".dsh" / "bili-duty" / "bili_credential.json")
    return out


def _pick(d: dict, keys: tuple[str, ...]) -> str:
    lower = {str(k).lower().replace("-", "_"): v for k, v in d.items()}
    for k in keys:
        if k in lower and lower[k]:
            return str(lower[k]).strip()
    return ""


def _from_mapping(d: dict) -> Credential:
    return Credential(
        sessdata=_pick(d, SESS_KEYS),
        bili_jct=_pick(d, JCT_KEYS),
        dedeuserid=_pick(d, UID_KEYS),
        buvid3=_pick(d, BUVID_KEYS),
    )


def _from_cookie_string(s: str) -> Credential:
    parts: dict[str, str] = {}
    for chunk in s.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        parts[k.strip()] = v.strip()
    return _from_mapping(parts)


def parse(raw: str) -> Credential:
    """把文件原文解析成凭证。解析不出任何登录字段时返回空凭证（不抛错）。"""
    text = raw.strip()
    if not text:
        return Credential()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return _from_cookie_string(text)

    if isinstance(obj, dict):
        # 有些导出格式把 cookie 塞在子字段里
        for key in ("cookie", "cookies", "credential"):
            inner = obj.get(key)
            if isinstance(inner, str):
                got = _from_cookie_string(inner)
                if got.sessdata:
                    return got
            if isinstance(inner, list):
                obj = inner
                break
        if isinstance(obj, dict):
            return _from_mapping(obj)

    if isinstance(obj, list):
        parts: dict[str, str] = {}
        for item in obj:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name:
                    parts[name] = value
        return _from_mapping(parts)

    return Credential()


def load(data_dir: Path | None = None, plugin_dir: Path | None = None) -> tuple[Credential, Path | None]:
    """返回 (凭证, 命中的文件路径)。没找到文件时凭证为空、路径为 None。"""
    for path in candidate_paths(data_dir, plugin_dir):
        try:
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            log.warning("读凭证文件失败 %s：%s", path, e)
            continue

        cred = parse(raw)
        if cred.logged_in:
            log.info("凭证来自 %s", path)
            return cred, path
        log.warning("凭证文件存在但没有可用的 SESSDATA：%s", path)

    log.warning("没有找到可用凭证，将以未登录状态启动")
    return Credential(), None


def save(data_dir: Path, cred: Credential) -> Path:
    """把凭证落盘到 dataDir（供扫码登录后写回）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "bili_credential.json"
    payload = {
        "sessdata": cred.sessdata,
        "bili_jct": cred.bili_jct,
        "dedeuserid": cred.dedeuserid,
        "buvid3": cred.buvid3,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
