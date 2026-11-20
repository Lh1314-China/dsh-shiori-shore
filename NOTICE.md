# NOTICE

本包以 **MIT** 许可发布，完整文本见 [LICENSE](./LICENSE)。

## 第三方 MIT 部分（宿主侧，仅两个文件）

- `lib/hostdeps.mjs`：与 `dsh-bili-pond@1.3.14` 的同名文件**逐字节相同**
- `lib/mcp.mjs`：基于 `dsh-bili-pond` 的同名文件改写（加了 `listTools()`、
  `startupTimeoutMs`、默认 clientInfo 名）

版权与许可：

```text
MIT License
Copyright (c) 2026 YOYOLnFu and dsh-bili-duty contributors
```

`dsh-bili-pond` 自身衍生自 `dsh-bili-scroll`（同为 MIT，Copyright (c) 2026
dsh-bili-duty contributors）。完整许可文本见 [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md)。

## 本项目原创部分

- `index.js`、`lib/schema.mjs`、`lib/python.mjs`，以及 `lib/mcp.mjs` 的扩展
- `python/` 下的全部模块（HTTP 用标准库，WBI 签名、cookie、412 退避、弹幕解压均为自写；
  依赖只有 `mcp`，**不依赖** `bilibili-api-python`，也**不包含**任何第三方代码）
- `test/`

## 历史

v0.1.x 的 Python 侧曾派生自第三方 MIT 项目，并依赖 GPL 的 `bilibili-api-python`。
从 0.2.0 起 Python 侧已全部重写；本仓库不包含、也不再分发 v0.1.x。
