/**
 * lib/python.mjs —— 选一个能跑汐岸的 Python
 * ============================================================================
 * 优先级：
 *   1. Config 里显式配了非 `python` 的 `pythonPath` ⇒ 完全按用户配的走
 *   2. 环境变量 `SHORE_PYTHON`
 *   3. 默认 `python` ⇒ 依次探测 `python` / `py -3` / 鱼塘 venv / `python3`
 *
 * 探测判据不是"命令存在"，而是真的能 `import bilibili_api, mcp`——
 * 本机就踩过：`python` 是某个沙箱的 3.13，命令在、依赖不在。
 * ============================================================================
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const probePython = (command, args) => {
  try {
    const r = spawnSync(command, [...args, '-c', 'import bilibili_api, mcp'], {
      timeout: 10000,
      windowsHide: true,
      stdio: 'ignore',
    })
    return r.status === 0
  } catch { return false }
}

/**
 * @param {{pythonPath?: string}} cfg
 * @returns {{command: string, args: string[]}}
 */
export const resolvePython = (cfg) => {
  const wanted = String(cfg?.pythonPath || 'python').trim() || 'python'
  if (wanted !== 'python') return { command: wanted, args: [] }

  const envPython = String(process.env.SHORE_PYTHON || '').trim()
  if (envPython) return { command: envPython, args: [] }

  // 鱼塘 venv 是最现成的一份（同一台机器上已经装好 17.4.2 + mcp 1.30.0）；
  // 它不是硬编码绝对路径，是 `homedir()` 拼出来的跨机器候选。
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
    if (probePython(c.command, c.args)) return c
  }
  // 都探测失败也返回默认，让 MCP 启动时报错、插件记日志（而不是在这里抛）
  return { command: wanted, args: [] }
}
