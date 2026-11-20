/**
 * lib/schema.mjs —— MCP inputSchema → 宿主 defineTool 的「属性表」
 * ============================================================================
 * 宿主的 defineTool 不是吃整份 JSON Schema：`parameters` 要是 `{ 参数名: 值schema }`，
 * 且值 schema 只认 type/oneOf/properties/required/items/enum/const/description/title/
 * default/examples/additionalProperties —— **不认 anyOf**（实测报
 * `parameters.type must be a value schema object` / `... or use oneOf`）。
 *
 * 所以这里做三件事：
 *   ① 顶层 properties 拎出来当属性表；
 *   ② 顶层 required 数组合成到每个属性上（required: true）；
 *   ③ 递归把 anyOf 改成 oneOf、剥掉不认识的键、items/properties 递归规整。
 * ============================================================================
 */

const ALLOWED = [
  'type', 'oneOf', 'properties', 'required', 'additionalProperties', 'items',
  'enum', 'const', 'description', 'title', 'default', 'examples',
]

const TYPES = new Set(['object', 'array', 'string', 'number', 'integer', 'boolean', 'null', 'json'])

/** 规整一个值 schema（不是属性表） */
export const normSchema = (node) => {
  if (!node || typeof node !== 'object') return { type: 'string' }
  const src = Array.isArray(node) ? node[0] : node
  const one = src.anyOf ? { ...src, oneOf: src.anyOf } : src
  const out = {}
  for (const k of ALLOWED) {
    if (one[k] === undefined) continue
    if (k === 'properties') {
      out.properties = {}
      for (const [pk, pv] of Object.entries(one.properties || {})) out.properties[pk] = normSchema(pv)
    } else if (k === 'items') {
      out.items = normSchema(one.items)
    } else if (k === 'oneOf') {
      out.oneOf = (Array.isArray(one.oneOf) ? one.oneOf : []).map(normSchema)
    } else if (k === 'type' && !TYPES.has(one.type)) {
      out.type = 'json'
    } else {
      out[k] = one[k]
    }
  }
  if (!out.type && !out.oneOf && !out.enum && !out.const && !out.properties) out.type = 'string'
  return out
}

/** MCP inputSchema → { 参数名: 值schema } */
export const paramsOf = (input) => {
  const src = input && typeof input === 'object' ? input : {}
  const props = src.properties && typeof src.properties === 'object' ? src.properties : {}
  const required = new Set(Array.isArray(src.required) ? src.required : [])
  const out = {}
  for (const [k, v] of Object.entries(props)) {
    out[k] = normSchema(v)
    if (required.has(k)) out[k].required = true
  }
  return out
}
