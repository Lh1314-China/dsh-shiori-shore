#!/usr/bin/env node
/**
 * test/smoke.mjs —— 不依赖 DSH 的冒烟测试
 * ============================================================================
 * 跑法：
 *   node test/smoke.mjs
 *   或指定解释器：SHORE_PYTHON=C:\...\python.exe node test/smoke.mjs
 *
 * 做什么：
 *   1. 起 `python/shore_server.py`（stdio MCP）
 *   2. initialize + tools/list → 期望 **52** 个工具
 *   3. 调 `bili_check_credential` → 期望 logged_in=true
 *   4. 全过才 exit 0
 * ============================================================================
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { McpClient } from '../lib/mcp.mjs'

const pluginRoot = fileURLToPath(new URL('..', import.meta.url))
const pythonDir = join(pluginRoot, 'python')
const serverPath = join(pythonDir, 'shore_server.py')
const EXPECT_TOOLS = 52
// 冒烟只验证「登录态有效」，不绑定任何具体账号

const probe = (command, args) => {
  try {
    const r = spawnSync(command, [...args, '-c', 'import bilibili_api, mcp'], {
      timeout: 20000,
      windowsHide: true,
      stdio: 'ignore',
    })
    return r.status === 0
  } catch { return false }
}

const pickPython = () => {
  const explicit = String(process.env.SHORE_PYTHON || '').trim()
  if (explicit) return { command: explicit, args: [] }
  const pondVenv = [
    join(homedir(), '.dsh', 'bili-duty', '.venv', 'Scripts', 'python.exe'),
    join(homedir(), '.dsh', 'bili-duty', '.venv', 'bin', 'python'),
  ]
  const candidates = [
    { command: 'python', args: [] },
    { command: 'py', args: ['-3'] },
    ...pondVenv.map((p) => ({ command: p, args: [] })),
    { command: 'python3', args: [] },
  ]
  for (const c of candidates) {
    const isPath = c.command.includes('/') || c.command.includes('\\')
    if (isPath && !existsSync(c.command)) continue
    if (probe(c.command, c.args)) return c
  }
  return { command: 'python', args: [] }
}

const python = pickPython()
const dataDir = mkdtempSync(join(tmpdir(), 'shore-smoke-'))

const client = new McpClient({
  command: python.command,
  args: [...python.args, serverPath],
  cwd: pythonDir,
  env: {
    ...process.env,
    BILI_DUTY_HOME: dataDir,
    BILI_MCP_TOOLS: '',
    SHORE_LEDGER: '0',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
  },
  log: (m) => console.log(`  ${m}`),
  startupTimeoutMs: 60000,
})

let failed = false
try {
  console.log(`Python：${python.command}${python.args.length ? ' ' + python.args.join(' ') : ''}`)
  console.log(`数据目录（临时）：${dataDir}`)

  await client.start()
  const tools = await client.listTools()
  const names = tools.map((t) => t?.name).filter(Boolean)
  console.log(`\n工具数：${names.length}（期望 ${EXPECT_TOOLS}）`)
  console.log('前 5 个：' + names.slice(0, 5).join(', '))
  if (names.length !== EXPECT_TOOLS) {
    console.error(`❌ 工具数不对：${names.length} ≠ ${EXPECT_TOOLS}`)
    failed = true
  }

  const cred = await client.call('bili_check_credential', {})
  console.log('\nbili_check_credential：')
  console.log(typeof cred === 'string' ? cred : JSON.stringify(cred, null, 2))
  const loggedIn = cred?.logged_in === true
  if (!loggedIn) {
    console.error(`❌ 凭证未登录：logged_in=${loggedIn}`)
    failed = true
  }
} catch (e) {
  console.error(`❌ 冒烟失败：${e?.stack ?? e?.message ?? e}`)
  failed = true
} finally {
  try { client.stop() } catch { /* 已经没了 */ }
  try { rmSync(dataDir, { recursive: true, force: true }) } catch { /* Windows 文件占用时会留一个临时目录，不影响结果 */ }
}

if (failed) {
  console.error('\nSMOKE FAILED')
  process.exit(1)
}
console.log('\nSMOKE OK')
process.exit(0)
