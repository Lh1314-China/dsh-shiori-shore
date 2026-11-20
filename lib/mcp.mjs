/**
 * lib/mcp.mjs —— 一个极小的 MCP stdio 客户端（只做我们需要的三件事）
 * ============================================================================
 * 为什么不用 `@deepseek-ai/dsh-mcp-client`：那是个"把 MCP 挂成 dsh 工具"的完整插件，
 * 而我们要的是**插件内部自己起这个 Python MCP**（用户选了 A 方案：装完就能用，
 * Python 路径和登录都在设置页里，不用去改 profile 的 patch）。
 *
 * 协议就三句话：`initialize` → `notifications/initialized` → `tools/call`，
 * stdio 上是**一行一个 JSON-RPC**。
 * ============================================================================
 */
import { spawn } from 'node:child_process'
import { appendFileSync, readFileSync } from 'node:fs'

// 报给对端的身份：版本号**从 package.json 读**，别手写（手写的那份永远追不上发版）
const PKG = (() => { try { return JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) } catch { return {} } })()

export class McpClient {
  constructor({ command, args = [], cwd, env, log = () => {}, startupTimeoutMs = 60000, stderrFile = '' }) {
    this.cfg = { command, args, cwd, env, log, startupTimeoutMs }
    this.proc = null
    this.buf = ''
    this.nextId = 1
    this.pending = new Map()
    this.stderr = ''
    this.stderrFile = stderrFile
    this.stderrRetryAt = 0 // 下一次允许重试落盘的时间戳；0 = 正常
    this.stderrSkipped = 0 // 失败窗口内跳过的段数
    this.starting = null
    this.lastPid = null
    this.spawnAt = null
  }

  get running() { return Boolean(this.proc) }

  /** spawn 出来那个进程的 pid（可能是 venv 的启动壳，真解释器见 <dataDir>/python-pid.log） */
  get pid() { return this.lastPid ?? this.proc?.pid ?? null }

  /** stderr 落盘：一路追加到文件，一路在内存留尾巴。写不进去就算了，绝不影响协议。 */
  #saveStderr(chunk) {
    this.stderr = (this.stderr + chunk).slice(-4000)
    if (!this.stderrFile) return
    const now = Date.now()
    if (this.stderrRetryAt > now) {
      // 失败窗口内不刷屏，但要数着丢了多少段，恢复时一并报出来
      this.stderrSkipped += 1
      return
    }
    try {
      appendFileSync(this.stderrFile, chunk, 'utf8')
      if (this.stderrRetryAt) {
        this.cfg.log(`stderr 落盘恢复（窗口内跳过了 ${this.stderrSkipped} 段）`)
        this.stderrSkipped = 0
        this.stderrRetryAt = 0
      }
    } catch (e) {
      // 失败不永久关闭：60s 后放行一次重试，避免一次抖动就丢掉后面的 stderr
      this.stderrRetryAt = now + 60_000
      this.cfg.log(`stderr 落盘失败（${this.stderrFile}）：${e?.code ? e.code + ' / ' : ''}${e?.message ?? e}；60s 后重试，期间只保留内存尾巴`)
    }
  }

  /** 把多行 stderr 压成一行，免得 plugin.log 一条事件被拆成几十行 */
  #oneLine(text, max = 2000) {
    const flat = String(text || '').replace(/\r/g, '').replace(/\n+/g, ' ⏎ ').trim()
    return flat.length > max ? '…' + flat.slice(-max) : flat
  }

  async start() {
    if (this.proc) return
    if (this.starting) return this.starting
    this.starting = (async () => {
      const { command, args, cwd, env, log } = this.cfg
      this.stderr = ''
      this.spawnAt = Date.now()
      this.proc = spawn(command, args, {
        cwd,
        env: { ...process.env, ...(env || {}) },
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      })
      this.lastPid = this.proc.pid ?? null
      log(`MCP 子进程已启动：pid=${this.lastPid ?? '未知'}，版本=${PKG.version || '未知'}，命令=${command}${args.length ? ' ' + args.join(' ') : ''}`)
      this.proc.stdout.setEncoding('utf8')
      this.proc.stdout.on('data', (chunk) => this.#onData(chunk))
      this.proc.stderr.setEncoding('utf8')
      this.proc.stderr.on('data', (c) => this.#saveStderr(c))
      this.proc.on('error', (e) => {
        log(`MCP 子进程启动失败：pid=${this.lastPid ?? '未知'}，版本=${PKG.version || '未知'}，错误=${e?.code ? e.code + ' / ' : ''}${e?.message ?? e}`)
        if (this.stderr) log(`MCP stderr（启动失败前 ${this.stderr.length} 字符）：${this.#oneLine(this.stderr)}`)
      })
      this.proc.on('exit', (code, signal) => {
        const pid = this.lastPid ?? '未知'
        const ran = this.spawnAt ? Math.round((Date.now() - this.spawnAt) / 1000) : '?'
        log(`MCP 子进程退出：pid=${pid}，版本=${PKG.version || '未知'}，code=${code}，signal=${signal ?? '无'}，运行=${ran}s${this.stderrFile ? '，stderr 落盘=' + this.stderrFile : ''}`)
        if (this.stderr) log(`MCP stderr 尾部（共 ${this.stderr.length} 字符）：${this.#oneLine(this.stderr)}`)
        for (const [, p] of this.pending) p.reject(new Error(`MCP 子进程退出（code=${code}）${this.stderr ? '：' + this.stderr.slice(-300) : ''}`))
        this.pending.clear()
        this.proc = null
      })
      await this.#request('initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: PKG.name || 'dsh-shiori-shore', version: PKG.version || '0.0.0' },
      }, this.cfg.startupTimeoutMs)
      this.#notify('notifications/initialized', {})
      log(`MCP 已就绪：pid=${this.lastPid ?? '未知'}，版本=${PKG.version || '未知'}，耗时=${this.spawnAt ? Date.now() - this.spawnAt : '?'}ms`)
    })()
    try { await this.starting } finally { this.starting = null }
  }

  stop() {
    try { this.proc?.kill() } catch { /* 已经没了 */ }
    this.proc = null
  }

  /** 取 MCP 的工具清单（tools/list） */
  async listTools({ timeoutMs = 60000 } = {}) {
    await this.start()
    const result = await this.#request('tools/list', {}, timeoutMs)
    return Array.isArray(result?.tools) ? result.tools : []
  }

  /** 调一个 MCP 工具：能解析成 JSON 就返回对象，否则返回字符串 */
  async call(name, args = {}, { timeoutMs = 180000 } = {}) {
    await this.start()
    const result = await this.#request('tools/call', { name, arguments: args }, timeoutMs)
    const text = (result?.content ?? []).map((c) => c?.text ?? '').join('\n').trim()
    if (result?.isError) throw new Error(text || `MCP 工具 ${name} 报错`)
    try { return text ? JSON.parse(text) : {} } catch { return text }
  }

  #onData(chunk) {
    this.buf += chunk
    let idx
    while ((idx = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, idx).trim()
      this.buf = this.buf.slice(idx + 1)
      if (!line) continue
      let msg
      try { msg = JSON.parse(line) } catch { continue }   // 非 JSON 行（有些库会往 stdout 打日志）忽略
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) p.reject(new Error(msg.error?.message ? String(msg.error.message) : JSON.stringify(msg.error)))
        else p.resolve(msg.result)
      }
    }
  }

  #send(obj) {
    if (!this.proc?.stdin?.writable) throw new Error('MCP 子进程不在运行')
    this.proc.stdin.write(JSON.stringify(obj) + '\n')
  }

  #notify(method, params) { this.#send({ jsonrpc: '2.0', method, params }) }

  #request(method, params, timeoutMs = 60000) {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        // 超时也留痕：否则「MCP 层卡住」和「业务接口慢」在日志里长得一样。
        // 这一条是「超时事实」的唯一完整记录；上层只记工具名，不再重复耗时/pid。
        this.cfg.log(`MCP ${method} 超时：耗时=${timeoutMs}ms，pid=${this.lastPid ?? '未知'}，版本=${PKG.version || '未知'}${this.stderr ? '，stderr 尾部=' + this.#oneLine(this.stderr, 200) : ''}`)
        const err = new Error(`MCP ${method} 超时（${timeoutMs}ms）${this.stderr ? '：' + this.stderr.slice(-200) : ''}`)
        err.code = 'MCP_TIMEOUT'
        err.mcpLogged = true // 告诉上层：这条已经记过，别把同一事实再列一遍
        reject(err)
      }, timeoutMs)
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v) },
        reject: (e) => { clearTimeout(timer); reject(e) },
      })
      try { this.#send({ jsonrpc: '2.0', id, method, params }) } catch (e) {
        clearTimeout(timer); this.pending.delete(id); reject(e)
      }
    })
  }
}
