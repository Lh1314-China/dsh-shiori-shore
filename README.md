# 汐岸 · dsh-shiori-shore

给 DeepSeek Harness 的 **B 站手**：内置一个 Python MCP 子进程，把 B 站的常用操作
注册成 `shore_*` 工具（**59 个**：52 个基础 + 6 个监听 + 1 个取帧）。

Python 侧**原创实现**：HTTP 用标准库，WBI 签名、cookie、412 退避、弹幕解压都是自写的；
运行时依赖只有 `mcp`，**不依赖** `bilibili-api-python`（GPL），也不包含第三方代码。

> 设计取向：**无信任闸**——不要求理由、不限制额度、不限制收件人、不强制 @ 谁。
> 它是一双手，不是审批流。代价是账号权限很高，见文末「已知限制」。

## 前提

| 需要 | 说明 |
|---|---|
| Node **≥ 22** | 插件宿主侧 |
| DSH **≥ 0.2.1-rc.2** | 见 `dsh.compatibility` |
| Python **≥ 3.10** + `mcp` | `pip install -r python/requirements.txt` |
| `ffmpeg`（可选） | 只有 `shore_frames` 取视频画面时需要（`SHORE_FFMPEG` 或 PATH） |

Python 解释器怎么找：`pythonPath` 显式配了就用它；留默认 `python` 时依次探测
`SHORE_PYTHON` → `python` → `py -3` → `python3`。找不到能 `import mcp` 的解释器时，
插件照常加载，只是没有工具（日志里写明原因）。

## 安装

```powershell
# npm
dsh plugin --profile desktop add dsh-shiori-shore

# 或直接装 GitHub 仓库
dsh plugin --profile desktop add github:Lh1314-China/dsh-shiori-shore
```

装完**重启 DSH**（app 只在启动时读 profile，热加载不生效）。
工具出现后先调一次 `shore_check_credential` 验登录。

## 配置

设置页可改，或在 profile 的 `cordis.patch.yml` 里覆盖：

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `pythonPath` | `python` | Python 解释器；空/默认则自动探测 |
| `dataDir` | `''` | 数据目录；空 = `~/.dsh/shiori-shore` |
| `toolPrefix` | `shore_` | 工具名前缀 |
| `toolsAllow` | `[]` | 只放开部分工具；空 = 全部 |
| `startupTimeoutMs` | `60000` | MCP 子进程启动超时 |
| `ledger` | `true` | 本地账本（工具调用流水） |
| `wakeEnabled` | `false` | 新消息唤醒：有私信/AT/回复才叫醒模型 |
| `wakeSessionId` | `''` | 唤醒投递到哪个会话 |
| `wakeIntervalSec` | `45` | 轮询间隔 |

## 凭证与数据

- 登录：`shore_login` 出二维码 → `shore_login_check` 确认。凭证写到
  `<dataDir>/bili_credential.json`（你自己的账号，**不随包分发、不上传**）。
- 数据目录默认 `~/.dsh/shiori-shore`：账本、本地凭证副本、取帧缓存都在这里。
- 删掉这个目录只是丢账本和本地副本，不影响 DSH 启动。

## 工具

- **52 个基础工具**：视频/用户/评论/私信/动态/收藏/关注/投币等（见 `test/schemas.json`）
- **6 个监听工具**：`shore_inbox_pending` 等，只读队列、不自动发
- **1 个取帧工具**：`shore_frames` —— 取 B 站视频真实画面（需要 ffmpeg）

⚠️ 无闸意味着这些工具能对你的账号做真实操作；装之前想清楚谁能使唤它。

## 冒烟测试

```powershell
$env:SHORE_PYTHON = "<你的 venv>\Scripts\python.exe"
node test/smoke.mjs
```

期望：工具数 52，`shore_check_credential` 返回 `logged_in: true`，最后一行 `SMOKE OK`。

## 回滚

1. `dsh plugin --profile desktop remove dsh-shiori-shore`（或从 `dsh.profile.bundles` 删掉）。
2. 重启 DSH，`shore_*` 工具消失。
3. 数据目录可保留；删了只丢账本与本地凭证副本。

## 已知限制

- **只在启动时读 profile**：装完或改配置必须重启 DSH。
- **登录二维码**会往 `python/` 写 `qrcode_login.png`；插件目录只读时会失败——
  把 `dataDir` 指到可写目录，或先把凭证放进 `<dataDir>/bili_credential.json`。
- **平台风控不归我们管**：-352 / -412 / 禁言 / 弹幕 20 字 / 投稿实名 / 每日投币关注上限，
  都是 B 站侧限制，撞上就退，别硬刚。
- **`BILI_MCP_TOOLS` 会被插件清空**：父进程带这个白名单会裁工具；插件置空 = 全放。
- **无闸不等于无边界**：改密码、换绑、实名、注销、大额消费、删视频这类不可回滚操作，
  建议不要交给自动流程。

## 来源与许可

- 本包 **MIT**，见 [LICENSE](./LICENSE)。
- 宿主侧两个文件来自 MIT 项目：`lib/hostdeps.mjs` 与 `lib/mcp.mjs`
  （`dsh-bili-pond` / `dsh-bili-scroll`，Copyright (c) 2026 YOYOLnFu and
  dsh-bili-duty contributors）。署名与完整许可见 [NOTICE.md](./NOTICE.md) 与
  [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md)。
- `python/` 侧为原创实现，不含第三方代码，也不依赖 GPL 的 `bilibili-api-python`。
