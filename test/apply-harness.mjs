import { apply } from '../index.js'

const registered = []
const ctx = {
  tools: { register: (t) => { registered.push(t); return () => {} } },
  effect: (f) => { try { const d = f(); if (typeof d === 'function') d() } catch { /* ignore */ } },
}

await apply(ctx, {
  enabled: true,
  pythonPath: process.env.SHORE_PYTHON || 'python',
  dataDir: process.env.SHORE_TEST_DIR || 'tmp-shore-apply',
  ledger: false,
})

console.log('REGISTERED', registered.length)
console.log('FIRST', registered.slice(0, 3).map((t) => t?.name))
