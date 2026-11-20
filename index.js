/**
 * dsh-shiori-shore —— 汐岸 · 给 DSH 的 B 站手（host 半端）
 * ============================================================================
 * 做什么：
 *   插件内部起一份 Python MCP（`python/shore_server.py`，它 import `shore.server`），
 *   把 MCP `tools/list` 出来的 58 个工具**动态注册**成 DSH 工具（默认前缀 `shore_`）。
 *
 * 三条硬要求（设计约束）：
 *   ① 独立：不 import 鱼塘、不写死 `bili-duty` 路径；凭证/数据默认 `~/.dsh/shiori-shore`。
 *   ② 无闸：不要求理由、不限额、不设收件人白名单、不强制 @ 谁。
 *      —— 本文件里没有任何额度/理由/白名单检查；`BILI_MCP_TOOLS` 显式清空，防止父进程的环境裁剪工具。
 *   ③ 58 个工具：从 MCP `tools/list` 动态注册，DSH 名 = `shore_` + 原工具名去掉 `bili_`。
 *
 * 只做底座，不搬鱼塘的值班/守护业务逻辑（那是 v0.2）。
 * ============================================================================
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// ⭐ 宿主那两样（schemastery / defineTool）从宿主树里取；拿不到就降级，绝不让整条插件不激活。
import { loadHostDeps } from './lib/hostdeps.mjs'
import { McpClient } from './lib/mcp.mjs'
import { paramsOf } from './lib/schema.mjs'
import { resolvePython } from './lib/python.mjs'

const hostDeps = await loadHostDeps()
const z = hostDeps.z
const defineTool = hostDeps.defineTool

export const name = 'shiori-shore'
export const inject = ['tools']

/**
 * ⚠️ 2026-11-18：全部去掉 `.extra('volatile', true)`。
 *
 * 原因：schemastery 3.19.0 的 extra() 有回归——克隆后 meta 与 type 看着都对，
 * 但**求值直接返回 {}**，导致 Config 解析出来每个字段都变成对象，
 * 插件读 cfg.pythonPath 拿到 {} → String({}) → "[object Object]"
 * → spawn "[object Object]" ENOENT → 60 秒 initialize 超时 → 一直没工具。
 *
 * 实测（同一份 3.19.0）：
 *   z.object({p: z.string().default('x')})({})                            → {"p":"x"}  好
 *   z.object({p: z.string().default('x').extra('volatile',true)})({})     → {"p":{}}   坏
 *   z.object({p: z.string().default('x').volatile()})({})                 → {"p":{}}   坏
 *   .description() 不触发，只有 .extra() 触发。
 *
 * 代价：settings.update() 只认 volatile 字段 ⇒ 设置页这几个字段不显示/不可写。
 *       要改值，直接改 profile 的 cordis.patch.yml 里 shiori-shore 那段。
 * ⚠️ schema 里不写 `.max()` —— 校验不过会让整条 entry 不激活（鱼塘踩过的老坑）。
 */
export const Config = !z ? undefined : z.object({
  /** 插件总开关 */
  enabled: z.boolean().default(true),
  /** Python 解释器。默认 `python`；拿不到依赖时会自动退到 `py -3` / 鱼塘 venv */
  pythonPath: z.string().default('python'),
  /** 数据目录（凭证、账本）。留空 = `~/.dsh/shiori-shore` */
  dataDir: z.string().default(''),
  /** DSH 工具名前缀。默认 `shore_`（`bili_search` → `shore_search`） */
  toolPrefix: z.string().default('shore_'),
  /** 只注册这些工具（MCP 原名或 DSH 名都认）。留空 = 58 个全放 */
  toolsAllow: z.array(z.string()).default([]),
  /** MCP initialize 超时（毫秒） */
  startupTimeoutMs: z.natural().default(60000),
  /** 账本：每次工具调用记一笔到 `<dataDir>/ledger/*.jsonl` */
  ledger: z.boolean().default(true),
  /** 唤醒：有新私信/AT/回复时，把目标会话叫醒（默认关；要同时填 wakeSessionId） */
  wakeEnabled: z.boolean().default(false),
  /** 被叫醒的会话 id（完整 `session-...`；留空 = 不唤醒） */
  wakeSessionId: z.string().default(''),
  /** 唤醒轮询间隔（秒，20–300，默认 45；只读本地队列，不打 B 站） */
  wakeIntervalSec: z.natural().default(45),
})

const pluginDir = dirname(fileURLToPath(import.meta.url))

/** 版本号以 package.json 为准（日志里要能一眼看出这轮跑的是哪个版本） */
const PKG = (() => { try { return JSON.parse(readFileSync(join(pluginDir, 'package.json'), 'utf8')) } catch { return {} } })()

/** 读 <dataDir>/python-pid.log 的最后一条：Python 自己报的「真解释器」身份 */
const lastPythonIdentity = (path) => {
  try {
    const lines = readFileSync(path, 'utf8').trim().split('\n')
    return JSON.parse(lines[lines.length - 1])
  } catch { return null }
}

/** 错误压成一行：日志一条事件别被多行堆栈打断 */
const errLine = (e, max = 240) => {
  const s = `${e?.name && e.name !== 'Error' ? e.name + ' / ' : ''}${e?.message ?? e}`.replace(/\s+/g, ' ').trim()
  return s.length > max ? s.slice(0, max) + '…' : s
}

/** 这个 pid 现在还活着吗（python-pid.log 是追加的，最后一条可能是上一轮的死进程） */
const pidAlive = (pid) => {
  const n = Number(pid)
  if (!Number.isInteger(n) || n <= 0) return false
  try { process.kill(n, 0); return true } catch (e) { return e?.code === 'EPERM' }
}

/** 日志同时落到 <dataDir>/plugin.log —— 宿主控制台看不到时还能查 */
let LOG_FILE = null
let LOG_WRITE_BROKEN = false
const log = (msg) => {
  const line = `[${new Date().toISOString()}] ${msg}`
  try { console.log(`[dsh-shiori-shore] ${msg}`) } catch { /* 没有 console 就算了 */ }
  if (!LOG_FILE) return
  try {
    appendFileSync(LOG_FILE, line + '\n', 'utf8')
    if (LOG_WRITE_BROKEN) {
      // 恢复也要说一声，否则中间那段空白看起来像没记
      LOG_WRITE_BROKEN = false
      try { console.error('[dsh-shiori-shore] 写账恢复') } catch { /* console 也没了 */ }
    }
  } catch (e) {
    // 记账自身失败不能再静默；文件写不进，只能打 stdout 留一个标记（只报一次，防刷屏）
    if (!LOG_WRITE_BROKEN) {
      LOG_WRITE_BROKEN = true
      try {
        console.error(`[dsh-shiori-shore] 写账失败（${e?.code ? e.code + ' / ' : ''}${e?.message ?? e}），后续只在恢复时报一次；本行内容：${msg}`)
      } catch { /* console 也没了 */ }
    }
  }
}

/** 宿主只吃「无损 JSON」：函数 / 类实例 / 循环引用都过不去 */
const safe = (v) => {
  if (v === undefined || v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
  try { return JSON.parse(JSON.stringify(v)) } catch { return String(v) }
}

const textOutput = {
  schema: { type: 'object', properties: {}, additionalProperties: true },
  render: (_args, value) => [{
    type: 'text',
    text: typeof value === 'string' ? value : JSON.stringify(value, null, 2),
  }],
}

/** MCP inputSchema 先转成宿主要的「属性表」（anyOf→oneOf、required 提到属性上，见 lib/schema.mjs） */

// Python 解析（默认 `python` / `py -3` / 鱼塘 venv / `python3` 自动探测）在 lib/python.mjs

const dataDirOf = (cfg) => {
  const raw = String(cfg?.dataDir || '').trim()
  if (!raw) return join(homedir(), '.dsh', 'shiori-shore')
  return isAbsolute(raw) ? raw : resolve(raw)
}

/** 注册一个工具：defineTool 优先，失败退 ctx.tools.register，两种都失败只记日志、不崩 */
const registerTool = (ctx, spec) => {
  try {
    if (typeof defineTool === 'function') {
      ctx.tools.register(defineTool(spec))
      return true
    }
  } catch (e) {
    log(`defineTool 注册 ${spec.name} 失败：${e?.message ?? e}`)
  }
  try {
    ctx.tools.register(spec)
    return true
  } catch (e) {
    log(`ctx.tools.register 注册 ${spec.name} 失败：${e?.message ?? e}`)
    return false
  }
}

/**
 * 新消息唤醒：每 N 秒读一次**本地** inbox_pending（不打 B 站），
 * 有事件才往目标会话投一条 user 消息；没有就什么都不做（零打扰）。
 *
 * 三条护栏：
 *   · 目标会话正在跑 → 这轮跳过，事件留在队列里（绝不「标记已读却没送到」）
 *   · 一次最多带 3 条摘要，正文 ≤600 字
 *   · 没有 sessionController / 没有 wakeSessionId → 直接不挂，记一行日志
 */
const startWaker = (ctx, client, cfg) => {
  const sid = String(cfg.wakeSessionId || '').trim()
  if (!sid) {
    log('唤醒未开：没填 wakeSessionId')
    return null
  }
  const every = Math.min(300, Math.max(20, Number(cfg.wakeIntervalSec) || 45)) * 1000
  let inFlight = false
  let stopped = false
  let warnedNoSc = false
  let fails = 0

  const tick = async () => {
    if (inFlight || stopped) return
    inFlight = true
    try {
      const sc = ctx.get('sessionController')
      if (!sc || typeof sc.prompt !== 'function') {
        if (!warnedNoSc) {
          warnedNoSc = true
          log('唤醒等待中：sessionController 还没就绪（下一轮再看）')
        }
        return
      }

      // 目标在跑就先不动：事件必须留在队列里等它闲下来
      try {
        const st = await sc.state({ sessionId: sid }, new AbortController().signal)
        if (st && st.running) return
      } catch {
        // 读不到状态也继续，prompt 失败会在下面留痕
      }

      const raw = await client.call('bili_inbox_pending', { limit: 20, mark: true }, { timeoutMs: 20000 })
      let res = null
      try {
        res = typeof raw === 'string' ? JSON.parse(raw) : raw
      } catch {
        return
      }
      if (!res || !Number(res.count)) return

      const items = Array.isArray(res.items) ? res.items : []
      const lines = items.slice(0, 3).map((it) =>
        `· ${String(it.type || '消息')}：${String(it.uname || it.uid || '').slice(0, 20)} ${String(it.text || '').replace(/\s+/g, ' ').slice(0, 60)}`,
      )
      const text = `汐岸：有 ${res.count} 条新消息（私信 / AT / 回复）。\n${lines.join('\n')}\n\n用 shore_inbox 看详情，要回就回；处理完用 shore_inbox_ack 标记。`

      await sc.prompt(
        {
          requestId: `shore-wake-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 5)}`,
          sessionId: sid,
          mode: 'queue',
          content: [{ type: 'text', text: text.slice(0, 600) }],
        },
        new AbortController().signal,
      )
      log(`唤醒：已投递 ${res.count} 条新消息到 ${sid}`)
      fails = 0
    } catch (e) {
      fails++
      log(`唤醒这一轮失败（第 ${fails} 次，下一轮再试）：${errLine(e)}`)
      if (fails >= 5) {
        stopped = true
        log('唤醒连续失败 5 次，已停用（修好配置/会话 id 后重启插件）')
      }
    } finally {
      inFlight = false
    }
  }

  const timer = setInterval(() => { void tick() }, every)
  if (typeof timer.unref === 'function') timer.unref()
  log(`唤醒已挂上：每 ${Math.round(every / 1000)}s 读一次本地 inbox_pending，目标 ${sid}`)
  return () => {
    stopped = true
    try { clearInterval(timer) } catch { /* 已经没了 */ }
  }
}

export async function apply(ctx, config) {
  const cfg = { ...(config || {}) }
  if (cfg.enabled === false) {
    log('enabled=false，跳过')
    return
  }

  const dataDir = dataDirOf(cfg)
  try { mkdirSync(dataDir, { recursive: true }) } catch (e) { log(`数据目录建不了（${dataDir}）：${e?.message ?? e}`) }
  LOG_FILE = join(dataDir, 'plugin.log')

  const pythonDir = join(pluginDir, 'python')
  const serverPath = join(pythonDir, 'shore_server.py')
  if (!existsSync(serverPath)) {
    log(`找不到 ${serverPath} —— 插件继续加载，但没有工具`)
    return
  }

  const python = resolvePython(cfg)
  const stderrFile = join(dataDir, 'mcp-stderr.log')
  log(`版本=${PKG.version || '未知'}，Python=${python.command}${python.args.length ? ' ' + python.args.join(' ') : ''}，dataDir=${dataDir}，stderr落盘=${stderrFile}`)

  const client = new McpClient({
    command: python.command,
    args: [...python.args, serverPath],
    cwd: pythonDir, // 让 `import mcp_core` 稳定；args 里同时用绝对路径兜底
    env: {
      // 汐岸的家：凭证优先 <dataDir>/bili_credential.json，账本落 <dataDir>/ledger
      BILI_DUTY_HOME: dataDir,
      // ⭐ 显式清空白名单：父进程若带着 BILI_MCP_TOOLS 会裁工具，这里必须置空 = 58 个全放
      BILI_MCP_TOOLS: '',
      SHORE_LEDGER: cfg.ledger === false ? '0' : '1',
      // Python 侧的中文 / emoji 走管道必须钉死 UTF-8（Windows 默认 GBK 会炸）
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    },
    log,
    startupTimeoutMs: Math.max(5000, Number(cfg.startupTimeoutMs) || 60000),
    stderrFile,
  })

  let registered = 0
  try {
    await client.start()
    const tools = await client.listTools()
    const prefix = String(cfg.toolPrefix ?? 'shore_')
    const allow = Array.isArray(cfg.toolsAllow) ? cfg.toolsAllow.map(String) : []

    for (const t of tools) {
      const mcpName = String(t?.name || '')
      if (!mcpName) continue
      const dshName = prefix + (mcpName.startsWith('bili_') ? mcpName.slice('bili_'.length) : mcpName)
      if (allow.length && !allow.includes(mcpName) && !allow.includes(dshName)) continue

      const description = String(t?.description || '').trim()
      const spec = {
        name: dshName,
        description: `${description}\n\n汐岸 · 无闸`.trim(),
        parameters: paramsOf(t?.inputSchema),
        output: textOutput,
        async execute(args) {
          try {
            return safe(await client.call(mcpName, args || {}))
          } catch (e) {
            // 失败也要留痕：请求可能根本没到 Python（子进程死 / 管道断 / 超时），
            // 那种情况 Python 账本里一行都没有，只剩这一条。
            // 纪律：同一事实只在一处记完整——超时的耗时/pid 归 mcp.mjs，这里不重复列。
            const detail = e?.mcpLogged ? '超时（细节见上一条 MCP 超时记录）' : errLine(e)
            log(`工具调用失败：${dshName}，pid=${client.pid ?? '未知'}，版本=${PKG.version || '未知'}，错误=${detail}`)
            throw e
          }
        },
      }
      if (registerTool(ctx, spec)) registered++
    }
    const pidFile = join(dataDir, 'python-pid.log')
    const pyId = lastPythonIdentity(pidFile)
    log(`已注册 ${registered}/${tools.length} 个工具（前缀 ${prefix}，pid=${client.pid ?? '未知'}，版本=${PKG.version || '未知'}）`)
    // 两个路径字段刻意分开：sys_executable 可能被 __PYVENV_LAUNCHER__ 改写成 venv 壳，
    // real_image 才是 OS 眼里真正在跑的映像（旧记录只有 executable，留作回退）
    if (pyId) log(`解释器：pid=${pyId.pid ?? '?'}，python=${pyId.python ?? '?'}，sys_executable=${pyId.sys_executable ?? pyId.executable ?? '?'}，real_image=${pyId.real_image ?? '?'}，ppid=${pyId.ppid ?? '?'}，存活=${pidAlive(pyId.pid) ? '是' : '否（记录是上一轮的）'}`)
    else log(`解释器身份未回报（缺 ${pidFile}）`)
    if (registered === 0) log('⚠️ 注册了 0 个工具：查 Python 环境 / MCP 握手输出')
  } catch (e) {
    // 🚨 MCP 起不来也不能让整条插件不激活：记日志、注册 0 个工具，DSH 照常跑
    const pyId = lastPythonIdentity(join(dataDir, 'python-pid.log'))
    log(`MCP 启动失败（插件继续加载，只是没有工具）：pid=${client.pid ?? '未知'}，版本=${PKG.version || '未知'}，错误=${e?.message ?? e}`)
    if (pyId) log(`解释器：pid=${pyId.pid ?? '?'}，python=${pyId.python ?? '?'}，real_image=${pyId.real_image ?? '?'}，存活=${pidAlive(pyId.pid) ? '是' : '否'}`)
    try { client.stop() } catch { /* 已经没了 */ }
  }

  // ── 新消息唤醒：只在「有事件」时叫醒，没事就静默 ─────────────────────
  if (cfg.wakeEnabled) {
    try {
      ctx.effect(() => {
        const stop = startWaker(ctx, client, cfg)
        return () => { try { stop?.() } catch { /* 收摊 */ } }
      }, 'dsh-shiori-shore: 新消息唤醒')
    } catch (e) {
      log(`唤醒没挂上：${errLine(e)}`)
    }
  } else {
    log('唤醒未开（wakeEnabled=false）')
  }

  // ── 心跳：宿主硬崩/断电时这里会停，最后一条心跳的时间就是断点 ──────────
  // 进程内的 exit 只能覆盖「进程还活着」的退出（kill / 重启），覆盖不了硬崩；
  // 这行是那个缺口的显式说明，别让人把「日志断掉」误读成采集坏了。
  log('记账边界：宿主硬崩/断电不会留下「退出」行，以最后一条「心跳」为断点。')
  const beatStart = Date.now()
  let beat = null
  try {
    beat = setInterval(() => {
      log(`心跳：版本=${PKG.version || '未知'}，pid=${client.pid ?? '未知'}，运行=${Math.round((Date.now() - beatStart) / 1000)}s`)
    }, 10 * 60 * 1000)
    if (typeof beat.unref === 'function') beat.unref() // 不拖住宿主退出
    ctx.effect(() => () => { try { clearInterval(beat) } catch { /* 已经没了 */ } }, 'dsh-shiori-shore: 心跳')
  } catch (e) {
    // effect 注册失败时定时器已经起来了，必须自己收，不然会一直空转
    if (beat) {
      try { clearInterval(beat) } catch { /* 已经没了 */ }
      beat = null
    }
    log(`心跳定时器没挂上（已清理）：${errLine(e)}`)
  }

  try {
    ctx.effect(() => () => { try { client.stop() } catch { /* 收摊 */ } }, 'dsh-shiori-shore: 收 MCP 子进程')
  } catch (e) {
    log(`dispose 钩子没挂上：${e?.message ?? e}`)
  }
}
