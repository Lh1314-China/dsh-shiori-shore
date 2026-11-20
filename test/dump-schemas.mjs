import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { McpClient } from '../lib/mcp.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const py = process.env.SHORE_PYTHON || 'python'
const c = new McpClient({
  command: py,
  args: [join(root, 'python', 'shore_server.py')],
  cwd: join(root, 'python'),
  env: { ...process.env, BILI_DUTY_HOME: join(root, 'test', 'tmp-schemas'), BILI_MCP_TOOLS: '', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
  log: () => {},
  startupTimeoutMs: 60000,
})
await c.start()
const tools = await c.listTools()
c.stop()
writeFileSync(join(root, 'test', 'schemas.json'), JSON.stringify(tools, null, 2))
console.log('tools', tools.length)

const keys = new Set()
const walk = (o) => {
  if (!o || typeof o !== 'object') return
  if (Array.isArray(o)) { o.forEach(walk); return }
  for (const k of Object.keys(o)) { keys.add(k); walk(o[k]) }
}
walk(tools.map((t) => t.inputSchema))
console.log('schema keys:', [...keys].sort().join(', '))
