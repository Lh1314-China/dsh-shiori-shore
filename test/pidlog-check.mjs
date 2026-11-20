// 自测：lib/mcp.mjs 的记账（pid / 版本 / 退出 stderr / 失败分支）
// 跑法：node test/pidlog-check.mjs
// 只起临时的 node 子进程，不碰正在跑的 shore 进程。
import { McpClient } from '../lib/mcp.mjs'
import { mkdtempSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const logs = []
const log = (m) => logs.push(m)
let failures = 0
const assert = (cond, msg) => {
  if (!cond) { failures++; console.error('FAIL:', msg) } else { console.log('ok  :', msg) }
}

const dir = mkdtempSync(join(tmpdir(), 'shore-pidlog-'))
const stderrFile = join(dir, 'mcp-stderr.log')

// ── 用例 A：子进程写 stderr 后以 code=3 退出 ──────────────────────────
const a = new McpClient({
  command: process.execPath,
  args: ['-e', 'process.stderr.write("boom-line1\\nboom-line2\\n"); setTimeout(() => process.exit(3), 60)'],
  log, startupTimeoutMs: 3000, stderrFile,
})
let aErr = null
try { await a.start() } catch (e) { aErr = e }
const aText = logs.join('\n')
assert(a.lastPid > 0, `A 记录到 pid（${a.lastPid}）`)
assert(/已启动：pid=\d+，版本=[^，]+/.test(aText), 'A 启动日志含 pid + 版本')
assert(aErr && /MCP 子进程退出（code=3）/.test(aErr.message), 'A initialize 因退出被拒（失败分支）')
assert(/退出：pid=\d+，版本=[^，]+，code=3，signal=无，运行=\d+s/.test(aText), 'A 退出日志含 pid/版本/code/signal/运行秒数')
assert(aText.includes('boom-line1 ⏎ boom-line2'), 'A 退出日志带 stderr 尾部（压成一行）')
assert(existsSync(stderrFile), 'A stderr 落盘文件已创建')
const stderrDisk = readFileSync(stderrFile, 'utf8')
assert(stderrDisk.includes('boom-line1') && stderrDisk.includes('boom-line2'), 'A stderr 文件内容完整')

// ── 用例 B：可执行文件不存在（spawn 失败，无 pid） ─────────────────────
const before = logs.length
const b = new McpClient({
  command: 'definitely-not-a-real-exe-12345.exe', args: [], log, startupTimeoutMs: 1200,
  stderrFile: join(dir, 'never.log'),
})
const t0 = Date.now()
let bErr = null
try { await b.start() } catch (e) { bErr = e }
const bMs = Date.now() - t0
const bText = logs.slice(before).join('\n')
assert(bErr !== null, `B 启动失败会抛错（${bMs}ms）`)
assert(bMs < 5000, 'B 失败分支没有拖满 60s 默认超时')
assert(/启动失败：pid=未知，版本=[^，]+，错误=ENOENT/.test(bText), 'B 日志含 启动失败 / pid / 版本 / ENOENT')
assert(!existsSync(join(dir, 'never.log')), 'B 没有凭空造 stderr 文件')

// ── 用例 C：正常握手的假 MCP（验证成功分支的字段） ─────────────────────
const stub = `let buf='';process.stdin.on('data',d=>{buf+=d;let i;while((i=buf.indexOf('\\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+1);if(!line.trim())continue;const m=JSON.parse(line);if(m.method==='initialize'){console.log(JSON.stringify({jsonrpc:'2.0',id:m.id,result:{protocolVersion:'2024-11-05',capabilities:{},serverInfo:{name:'stub',version:'9.9.9'}}}))}else if(m.method==='tools/list'){console.log(JSON.stringify({jsonrpc:'2.0',id:m.id,result:{tools:[{name:'bili_ping',description:'p',inputSchema:{type:'object',properties:{}}}]}}))}}})`
const beforeC = logs.length
const c = new McpClient({ command: process.execPath, args: ['-e', stub], log, startupTimeoutMs: 3000 })
await c.start()
const tools = await c.listTools({ timeoutMs: 3000 })
const cText = logs.slice(beforeC).join('\n')
assert(/MCP 已就绪：pid=\d+，版本=[^，]+，耗时=\d+ms/.test(cText), 'C 就绪日志含 pid/版本/耗时')
assert(tools.length === 1 && tools[0].name === 'bili_ping', 'C tools/list 正常返回')
c.stop()

// ── 用例 D：initialize 超时 → 必须记一条带耗时的日志 ─────────────────
const beforeD = logs.length
const d = new McpClient({ command: process.execPath, args: ['-e', 'process.stdin.resume()'], log, startupTimeoutMs: 400 })
let dErr = null
try { await d.start() } catch (e) { dErr = e }
const dText = logs.slice(beforeD).join('\n')
assert(dErr && /MCP initialize 超时（400ms）/.test(dErr.message), 'D 超时会抛错')
assert(/MCP initialize 超时：耗时=400ms，pid=\d+，版本=/.test(dText), 'D 超时日志含 方法/耗时/pid/版本')
assert(dErr?.code === 'MCP_TIMEOUT' && dErr?.mcpLogged === true, 'D 带 mcpLogged 标记（上层不再重复记）')
d.stop()

// ── 用例 E：stderr 落盘失败 → 必须留降级标记 ──────────────────────────
const beforeE = logs.length
const e = new McpClient({
  command: process.execPath,
  // 两段 stderr：第二段落在 60s 重试窗口内，应被跳过而不是再报一次
  args: ['-e', 'process.stderr.write("boom-x\\n"); setTimeout(() => process.stderr.write("boom-y\\n"), 30); setTimeout(() => process.exit(4), 90)'],
  log, startupTimeoutMs: 3000,
  stderrFile: join(dir, 'no-such-dir', 'x.log'), // 目录不存在 → appendFileSync 必炸
})
try { await e.start() } catch { /* 预期退出 */ }
const eText = logs.slice(beforeE).join('\n')
assert(/stderr 落盘失败（.*no-such-dir/.test(eText), 'E 落盘失败有降级标记')
assert((eText.match(/stderr 落盘失败/g) || []).length === 1, 'E 窗口内只报一次，不刷屏（会到期重试）')
assert(/60s 后重试/.test(eText), 'E 标明会重试，不是永久关闭')

console.log(`\nfailures=${failures}`)
process.exit(failures ? 1 : 0)
