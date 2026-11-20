/**
 * test/shape-check.mjs —— 0.1.1 输出形状验收（只读接口）
 *   node test/shape-check.mjs
 */
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { McpClient } from '../lib/mcp.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const py = process.env.SHORE_PYTHON || process.env.SHORE_PYTHON || 'python'
const c = new McpClient({
  command: py,
  args: [join(root, 'python', 'shore_server.py')],
  cwd: join(root, 'python'),
  env: { ...process.env, BILI_DUTY_HOME: join(root, 'test', 'tmp-shape'), BILI_MCP_TOOLS: '', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
  log: () => {},
  startupTimeoutMs: 60000,
})

let bad = 0
const check = (name, ok, detail) => {
  console.log(`${ok ? '✓' : '✗'} ${name}${detail ? ' — ' + detail : ''}`)
  if (!ok) bad++
}

await c.start()

// 1) weekly_hot(week=0)
const w = await c.call('bili_weekly_hot', {})
const wCount = Array.isArray(w?.items) ? w.items.length : -1
check('weekly_hot 默认 ≤12 期', wCount > 0 && wCount <= 12, `count=${wCount} total=${w?.total}`)
check('weekly_hot 有一期名字', Boolean(w?.items?.[0]?.name), JSON.stringify(w?.items?.[0] ?? null))
check('weekly_hot 有分页标记', typeof w?.has_more === 'boolean' && 'next_offset' in (w || {}), `has_more=${w?.has_more} next=${w?.next_offset}`)

// 2) hot_buzzwords
const b = await c.call('bili_hot_buzzwords', { page_size: 5 })
check('hot_buzzwords 默认走实时热搜', b?.source === 'hot', `source=${b?.source}`)
check('hot_buzzwords 有词', Array.isArray(b?.items) && b.items.length > 0, JSON.stringify(b?.items?.[0] ?? null))
check('hot_buzzwords 带时间', typeof b?.fetched_at === 'string' && b.fetched_at.length > 10, b?.fetched_at)
const legacy = await c.call('bili_hot_buzzwords', { page_size: 3, source: 'lexicon' })
check('hot_buzzwords 保留旧图鉴', legacy?.source === 'lexicon' && Array.isArray(legacy?.items), `count=${legacy?.count}`)

// 3) search
const s = await c.call('bili_search', { keyword: '番剧', num: 10 })
const vids = Array.isArray(s?.videos) ? s.videos : []
check('search 返回视频', vids.length > 0, `count=${vids.length} skipped=${s?.skipped} dropped=${s?.dropped}`)
check('search 每条都有 bvid', vids.every((v) => typeof v.bvid === 'string' && /^BV[0-9A-Za-z]{10}$/.test(v.bvid)), vids.map((v) => v.bvid).join(','))
check('search 有计数', typeof s?.skipped === 'number' && typeof s?.dropped === 'number', `skipped=${s?.skipped} dropped=${s?.dropped}`)

c.stop()
console.log(bad ? `\nSHAPE FAILED (${bad})` : '\nSHAPE OK')
process.exit(bad ? 1 : 0)
