import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { paramsOf } from '../lib/schema.mjs'

const here = fileURLToPath(new URL('..', import.meta.url))
const tools = JSON.parse(readFileSync(new URL('./schemas.json', import.meta.url), 'utf8'))
const { defineTool } = await import('file:///D:/Deepseek%20Harness/resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js')

const output = {
  schema: { type: 'object', properties: {}, additionalProperties: true },
  render: () => [],
}

let ok = 0
const fails = []
for (const t of tools) {
  try {
    defineTool({
      name: 'shore_' + String(t.name).replace(/^bili_/, ''),
      description: String(t.description || '') || 'x',
      parameters: paramsOf(t.inputSchema),
      output,
      async execute() { return 1 },
    })
    ok++
  } catch (e) {
    fails.push(`${t.name}: ${e?.message || e}`)
  }
}
console.log(`ok=${ok} fail=${fails.length}`)
for (const f of fails.slice(0, 10)) console.log(' -', f)
process.exit(fails.length ? 1 : 0)
