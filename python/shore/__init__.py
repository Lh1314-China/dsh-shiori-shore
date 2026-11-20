"""汐岸（shore）· 自研 B 站工具层。

这个包与插件 host 半端（index.js / lib/*.mjs）解耦：
host 只负责把本包注册成 MCP stdio 服务端，其余行为全部在这里实现。

模块分工：
    client    HTTP 传输、cookie、WBI 签名、风控退避
    auth      凭证定位与解析
    ledger    调用账本
    models    接口返回 → 工具返回形状
    tools_*   各领域工具实现
    server    MCP 注册入口（52 个工具名与契约对齐）
"""

__all__ = ["client", "auth", "ledger", "models", "server"]
