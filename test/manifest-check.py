#!/usr/bin/env python3
"""52 个工具名 / 参数 / 必填项 与 schemas.json 的对齐检查。

用法：
    <venv>\\Scripts\\python.exe test\\manifest-check.py

判据：
- 工具名必须一一对应（52/52）
- 参数名集合必须一致
- 必填项集合必须一致

允许的「契约外新增可选参数」：
    bili_hot_buzzwords  + source
    bili_weekly_hot     + num / offset / order
这两组是 0.1.1 README 里明确记录过的形状修正（实时热搜切换、期数分页），
属于新增可选参数，不破坏既有调用，所以列为已知差异而不是失败。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE.parent / "python"
sys.path.insert(0, str(PY))

KNOWN_EXTRA = {
    "bili_hot_buzzwords": {"source"},
    "bili_weekly_hot": {"num", "offset", "order"},
}

# 0.2 的扩展工具：不在这 52 条契约里，是新增能力（消息监听 / 收件箱）。
# 它们不改变任何既有工具的名字、参数或返回形状，所以单列出来放行。
EXTENSION_TOOLS = {
    "bili_listen_set",
    "bili_listen_status",
    "bili_inbox",
    "bili_inbox_pending",
    "bili_inbox_ack",
    "bili_dm_autoreply",
}


def main() -> int:
    spec_path = HERE / "schemas.json"
    if not spec_path.is_file():
        print(f"找不到契约文件：{spec_path}")
        return 2

    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    from shore.server import mcp

    async def collect():
        return await mcp.list_tools()

    tools = asyncio.run(collect())
    by_name = {t.name: t for t in tools}

    problems: list[str] = []
    known: list[str] = []
    ok = 0

    for item in spec:
        name = str(item.get("name") or "")
        tool = by_name.get(name)
        if tool is None:
            problems.append(f"{name}: 工具缺失")
            continue

        want = item.get("inputSchema") or {}
        got = tool.inputSchema or {}
        want_props = set((want.get("properties") or {}).keys())
        got_props = set((got.get("properties") or {}).keys())
        want_req = set(want.get("required") or [])
        got_req = set(got.get("required") or [])

        extra = got_props - want_props
        missing = want_props - got_props

        allowed = KNOWN_EXTRA.get(name, set())
        if missing:
            problems.append(f"{name}: 缺参数 {sorted(missing)}")
        elif extra and extra != allowed:
            problems.append(f"{name}: 多出参数 {sorted(extra)}（不在已知白名单里）")
        elif extra:
            known.append(f"{name}: 新增可选参数 {sorted(extra)}（0.1.1 已记录，放行）")

        if want_req != got_req:
            problems.append(f"{name}: 必填项不一致 want={sorted(want_req)} got={sorted(got_req)}")
        else:
            ok += 1

    total = len(spec)
    spec_names = {str(i.get("name") or "") for i in spec}
    extra_tools = sorted(set(by_name) - spec_names)
    unexpected = [n for n in extra_tools if n not in EXTENSION_TOOLS]
    missing_ext = sorted(EXTENSION_TOOLS - set(by_name))

    print(f"契约条目：{total}    实际工具：{len(tools)}")
    print(f"对齐通过：{ok}/{total}")
    for line in known:
        print("  · " + line)
    if extra_tools:
        print("  + 扩展工具：" + ", ".join(extra_tools))
    if missing_ext:
        problems.append(f"扩展工具缺失：{missing_ext}")
    if unexpected:
        problems.append(f"出现未登记的扩展工具：{unexpected}")

    if problems:
        print("\n失败项：")
        for p in problems:
            print("  ✗ " + p)
        print(f"\nMANIFEST FAILED ({len(problems)})")
        return 1

    print("\nMANIFEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
