// 只验证 MCP 层：起服务、列工具、数 58 个。
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { McpClient } from '../lib/mcp.mjs'

const here = fileURLToPath(new URL('..', import.meta.url))
const py = process.env.SHORE_PYTHON || process.env.SHORE_PYTHON || 'python'
const pythonDir = join(here, 'python')

const cli = new McpClient({
  command: py,
  args: [join(pythonDir, 'shore_server.py')],
  cwd: pythonDir,
  env: {
    BILI_DUTY_HOME: join(here, 'tmp-list-tools'),
    BILI_MCP_TOOLS: '',
    SHORE_LEDGER: '0',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
  },
  startupTimeoutMs: 60000,
})

await cli.start()
const tools = await cli.listTools()
const names = tools.map((t) => t.name)
console.log(`tools=${tools.length}`)
console.log('new:', names.filter((n) => /listen|inbox|autoreply/.test(n)).join(', '))
console.log('first:', names.slice(0, 5).join(', '))
process.exit(0)
