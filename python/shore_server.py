#!/usr/bin/env python3
"""汐岸 MCP 入口（host 半端按这个路径 spawn）。

真正的实现在 `shore/server.py`。这里只做三件事：

1. 把 `python/` 放进 sys.path（因为 host 可能用绝对路径调用本文件）
2. 钉死 UTF-8（Windows 默认 GBK 会让中文参数走形）
3. 调 `shore.server.main()`

不在这里 print 任何东西——stdout 是 MCP 的协议通道。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("BILI_DUTY_HOME", str(_HERE))


def _run() -> None:
    from shore.server import main

    main()


if __name__ == "__main__":
    _run()
