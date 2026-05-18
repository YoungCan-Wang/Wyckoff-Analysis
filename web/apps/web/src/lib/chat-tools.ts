import type { SupabaseClient } from '@supabase/supabase-js'
import type { generateText as GenerateTextFn } from 'ai'
import { isCnSymbol, normalizeTickFlowSymbol } from './kline'

export interface KlineRow {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface ToolDeps {
  supabase: SupabaseClient
  fetch: typeof globalThis.fetch
  generateText: typeof GenerateTextFn
}

export interface LLMToolConfig {
  api_key: string
  model: string
  base_url: string
}

export function buildKlineDigest(data: KlineRow[]): string {
  if (data.length === 0) return '无可用K线数据'
  const last = data[data.length - 1]!
  const avg = (arr: number[]) => arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : 0
  const slice = (n: number) => data.slice(-n)
  const ma = (n: number) => avg(slice(n).map(d => d.close))
  const vol = (n: number) => avg(slice(n).map(d => d.volume))
  const p20 = slice(20)

  const lines = [
    `K线共${data.length}根，最新日期 ${last.date}`,
    `最新收盘 ${last.close.toFixed(2)}，开盘 ${last.open.toFixed(2)}，高 ${last.high.toFixed(2)}，低 ${last.low.toFixed(2)}`,
    `MA5=${ma(5).toFixed(2)} MA10=${ma(10).toFixed(2)} MA20=${ma(20).toFixed(2)}`,
  ]
  if (data.length >= 50) lines.push(`MA50=${ma(50).toFixed(2)}`)
  if (data.length >= 120) lines.push(`MA120=${ma(120).toFixed(2)}`)
  lines.push(
    `近20日最高 ${Math.max(...p20.map(d => d.high)).toFixed(2)}，最低 ${Math.min(...p20.map(d => d.low)).toFixed(2)}`,
    `近5日均量 ${vol(5).toFixed(0)}，近20日均量 ${vol(20).toFixed(0)}`,
    `量比(5/20) ${(vol(5) / (vol(20) || 1)).toFixed(2)}`,
  )

  const recent5 = slice(5)
  lines.push('近5日走势: ' + recent5.map(d => {
    const chg = ((d.close - d.open) / d.open * 100).toFixed(1)
    return `${d.date.slice(5)} ${Number(chg) >= 0 ? '+' : ''}${chg}%`
  }).join(' → '))

  return lines.join('\n')
}

export async function fetchUserDataKeys(deps: ToolDeps, userId: string): Promise<{ tickflow: string | null; tushare: string | null }> {
  const { data } = await deps.supabase
    .from('user_settings')
    .select('tickflow_api_key, tushare_token')
    .eq('user_id', userId)
    .single()
  return {
    tickflow: String(data?.tickflow_api_key || '').trim() || null,
    tushare: String(data?.tushare_token || '').trim() || null,
  }
}

export async function fetchTickFlowKey(deps: ToolDeps, userId: string): Promise<string | null> {
  const keys = await fetchUserDataKeys(deps, userId)
  return keys.tickflow
}

function normalizeTushareCode(code: string): string {
  const c = code.replace(/\.\w+$/, '')
  if (c.startsWith('6')) return `${c}.SH`
  if (c.startsWith('4') || c.startsWith('8') || c.startsWith('9')) return `${c}.BJ`
  return `${c}.SZ`
}

async function tusharePost(deps: ToolDeps, token: string, api_name: string, params: Record<string, string>, fields: string) {
  const resp = await deps.fetch('/api/llm-proxy/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Target-URL': 'https://api.tushare.pro' },
    body: JSON.stringify({ api_name, token, params, fields }),
  })
  if (!resp.ok) return null
  return (await resp.json()) as { data?: { fields?: string[]; items?: unknown[][] } }
}

async function fetchKlineViaTushare(deps: ToolDeps, code: string, token: string, startDate: string, endDate: string): Promise<KlineRow[]> {
  const tsCode = normalizeTushareCode(code)
  const [dailyJson, adjJson] = await Promise.all([
    tusharePost(deps, token, 'daily', { ts_code: tsCode, start_date: startDate, end_date: endDate }, 'trade_date,open,high,low,close,vol'),
    tusharePost(deps, token, 'adj_factor', { ts_code: tsCode, start_date: startDate, end_date: endDate }, 'trade_date,adj_factor'),
  ])
  const items = dailyJson?.data?.items
  if (!Array.isArray(items) || items.length === 0) return []

  const adjItems = adjJson?.data?.items
  if (!Array.isArray(adjItems) || adjItems.length === 0) return []
  const adjMap = new Map<string, number>()
  let latestDate = ''
  for (const row of adjItems) {
    const dt = String(row[0])
    adjMap.set(dt, Number(row[1]))
    if (dt > latestDate) latestDate = dt
  }
  const latestFactor = adjMap.get(latestDate) || 1

  return items.map(row => {
    const dt = String(row[0] || '')
    const factor = adjMap.get(dt)
    if (!factor) return null
    const ratio = factor / latestFactor
    return {
      date: dt.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
      open: Number(row[1] || 0) * ratio, high: Number(row[2] || 0) * ratio,
      low: Number(row[3] || 0) * ratio, close: Number(row[4] || 0) * ratio,
      volume: Number(row[5] || 0),
    }
  }).filter((d): d is KlineRow => d !== null && d.date !== '' && d.close > 0)
}

function parseKlineRows(rows: unknown[]): KlineRow[] {
  return (rows as Record<string, unknown>[]).map(r => ({
    date: String(r.date || r.trade_date || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
    open: Number(r.open || 0),
    high: Number(r.high || 0),
    low: Number(r.low || 0),
    close: Number(r.close || 0),
    volume: Number(r.volume || r.vol || 0),
  })).filter(d => d.date && d.close > 0)
}

function parseTickFlowTable(table: Record<string, unknown[]>): KlineRow[] {
  const ts = Array.isArray(table.timestamp) ? table.timestamp : []
  if (ts.length === 0) return []
  const o = table.open || [], h = table.high || [], l = table.low || [], c = table.close || [], v = table.volume || []
  return ts.map((t, i) => ({
    date: formatTimestamp(t), open: Number(o[i] || 0), high: Number(h[i] || 0),
    low: Number(l[i] || 0), close: Number(c[i] || 0), volume: Number(v[i] || 0),
  })).filter(d => d.date && d.close > 0)
}

function findTickFlowTable(data: unknown, symbol: string): Record<string, unknown[]> | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const obj = data as Record<string, unknown>
  if (Array.isArray(obj.timestamp)) return obj as Record<string, unknown[]>
  const direct = obj[symbol]
  if (direct && typeof direct === 'object' && !Array.isArray(direct)) {
    const table = direct as Record<string, unknown>
    if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
  }
  for (const value of Object.values(obj)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const table = value as Record<string, unknown>
      if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
    }
  }
  return null
}

function parseTickFlowPayload(json: Record<string, unknown>, symbol: string): KlineRow[] {
  const data = json.data
  if (Array.isArray(data)) return parseKlineRows(data)
  if (Array.isArray(json.records)) return parseKlineRows(json.records)
  const table = findTickFlowTable(data, symbol)
  return table ? parseTickFlowTable(table) : []
}

function formatTimestamp(value: unknown): string {
  const n = Number(value)
  if (Number.isFinite(n) && n > 0) return new Date(n + 8 * 3600_000).toISOString().slice(0, 10)
  return String(value || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3').slice(0, 10)
}

async function fetchKlineViaTickFlow(deps: ToolDeps, code: string, apiKey: string, count = 250): Promise<KlineRow[]> {
  const symbol = normalizeTickFlowSymbol(code)
  const params = new URLSearchParams({
    symbol, period: '1d', count: String(count), adjust: 'forward',
  })
  const resp = await deps.fetch(`/api/llm-proxy/v1/klines?${params}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (resp.ok) {
    const rows = parseTickFlowPayload(await resp.json(), symbol)
    if (rows.length) return rows
  }
  const batchParams = new URLSearchParams({ symbols: symbol, period: '1d', count: String(count), adjust: 'forward' })
  const batchResp = await deps.fetch(`/api/llm-proxy/v1/klines/batch?${batchParams}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (!batchResp.ok) return []
  return parseTickFlowPayload(await batchResp.json(), symbol)
}


export async function fetchKlineForAgent(deps: ToolDeps, code: string, keys: { tickflow: string | null; tushare: string | null }, _userId: string): Promise<KlineRow[]> {
  const end = new Date(); end.setDate(end.getDate() - 1)
  const start = new Date(); start.setDate(start.getDate() - 500)
  const fmt = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, '')
  const isCn = isCnSymbol(code)

  if (keys.tickflow) {
    try { const r = await fetchKlineViaTickFlow(deps, code, keys.tickflow); if (r.length) return r } catch { /* */ }
  }
  if (isCn && keys.tushare) {
    try { const r = await fetchKlineViaTushare(deps, code, keys.tushare, fmt(start), fmt(end)); if (r.length) return r.sort((a, b) => a.date.localeCompare(b.date)) } catch { /* */ }
  }
  return []
}

export async function fetchQuotes(
  deps: ToolDeps,
  tickflowKey: string | null,
  stocks: { code: number }[],
): Promise<Record<string, Record<string, number>>> {
  if (!tickflowKey || stocks.length === 0) return {}
  try {
    const symbols = stocks.map(r => {
      const c = String(r.code).padStart(6, '0')
      if (c.startsWith('6')) return `${c}.SH`
      if (c.startsWith('4') || c.startsWith('8') || c.startsWith('9')) return `${c}.BJ`
      return `${c}.SZ`
    }).join(',')
    const resp = await deps.fetch(
      `/api/llm-proxy/v1/quotes?symbols=${symbols}`,
      { headers: { 'x-api-key': tickflowKey, 'X-Target-URL': 'https://api.tickflow.org' } },
    )
    if (!resp.ok) return {}
    const json = await resp.json() as { data?: Record<string, number>[] }
    const result: Record<string, Record<string, number>> = {}
    for (const row of (json.data || [])) {
      const sym = String((row as Record<string, unknown>).symbol || '')
      const code6 = sym.split('.')[0] || ''
      if (code6) result[code6] = row
    }
    return result
  } catch { return {} }
}

export async function execSearchStock(deps: ToolDeps, userId: string, query: string): Promise<string> {
  const q = query.trim()
  const isCode = /^\d+$/.test(q)

  const tables = ['recommendation_tracking', 'portfolio_positions', 'tail_buy_history'] as const
  const allRows: { code: number; name: string }[] = []

  for (const table of tables) {
    const res = isCode
      ? await deps.supabase.from(table).select('code, name').eq('code', parseInt(q)).limit(5)
      : await deps.supabase.from(table).select('code, name').ilike('name', `%${q}%`).limit(10)
    if (res.data) allRows.push(...res.data)
  }

  if (allRows.length === 0) return `未找到匹配"${query}"的股票`

  const seen = new Set<number>()
  const unique = allRows.filter((r) => {
    if (seen.has(r.code)) return false
    seen.add(r.code)
    return true
  }).slice(0, 10)

  const tickflowKey = await fetchTickFlowKey(deps, userId)
  const quotes = await fetchQuotes(deps, tickflowKey, unique)

  const lines = unique.map(r => {
    const code6 = String(r.code).padStart(6, '0')
    const qt = quotes[code6]
    if (qt) {
      const price = qt.close || qt.last || qt.price || qt.current || 0
      const pct = qt.pct_chg ?? ((qt.close && qt.pre_close) ? ((qt.close - qt.pre_close) / qt.pre_close * 100) : null)
      const pctStr = pct != null ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : ''
      return `${code6} ${r.name} | ¥${price.toFixed(2)} ${pctStr}`
    }
    return `${code6} ${r.name}`
  })

  return lines.join('\n')
}

export async function execViewPortfolio(deps: ToolDeps, userId: string): Promise<string> {
  const portfolioId = `USER_LIVE:${userId}`

  const [pfResult, posResult] = await Promise.all([
    deps.supabase.from('portfolios').select('free_cash').eq('portfolio_id', portfolioId).single(),
    deps.supabase.from('portfolio_positions').select('code, name, shares, cost_price, buy_dt, stop_loss').eq('portfolio_id', portfolioId),
  ])

  const cash = pfResult.data?.free_cash || 0
  const positions = posResult.data || []

  if (positions.length === 0) {
    return `当前无持仓。可用资金：¥${cash.toLocaleString()}`
  }

  const lines = positions.map((p) => {
    const sl = p.stop_loss ? ` | 止损¥${p.stop_loss.toFixed(2)}` : ''
    return `${p.code} ${p.name} | ${p.shares}股 | 成本¥${p.cost_price.toFixed(2)} | 建仓${p.buy_dt || '未知'}${sl}`
  })
  const totalCost = positions.reduce((s, p) => s + p.shares * p.cost_price, 0)

  return [
    `持仓 ${positions.length} 只，可用资金 ¥${cash.toLocaleString()}，持仓成本合计 ¥${totalCost.toLocaleString()}`,
    '',
    ...lines,
  ].join('\n')
}

export async function execMarketOverview(deps: ToolDeps): Promise<string> {
  const { data } = await deps.supabase
    .from('market_signal_daily')
    .select('*')
    .order('trade_date', { ascending: false })
    .limit(3)

  if (!data || data.length === 0) return '暂无最新市场信号数据'

  const merged: Record<string, unknown> = { ...data[0] }
  for (const row of data) {
    for (const key of ['benchmark_regime', 'main_index_close', 'main_index_today_pct']) {
      if (!merged[key] && row[key]) merged[key] = row[key]
    }
    for (const key of ['a50_close', 'a50_pct_chg']) {
      if (!merged[key] && row[key]) merged[key] = row[key]
    }
    for (const key of ['vix_close', 'vix_pct_chg']) {
      if (!merged[key] && row[key]) merged[key] = row[key]
    }
  }

  const regimeMap: Record<string, string> = {
    RISK_ON: '偏强', NEUTRAL: '中性', RISK_OFF: '偏弱', CRASH: '极弱', BLACK_SWAN: '恶劣',
  }
  const regime = String(merged.benchmark_regime || 'NEUTRAL')
  const close = Number(merged.main_index_close || 0)
  const pct = Number(merged.main_index_today_pct || 0)
  const a50Close = Number(merged.a50_close || 0)
  const a50Pct = Number(merged.a50_pct_chg || 0)
  const vixClose = Number(merged.vix_close || 0)
  const title = String(merged.banner_title || '')
  const body = String(merged.banner_message || '')

  return [
    `大盘状态：${regimeMap[regime] || regime}`,
    close ? `上证指数：${close.toFixed(0)} (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)` : '',
    a50Close ? `A50：${a50Close.toFixed(0)} (${a50Pct >= 0 ? '+' : ''}${a50Pct.toFixed(2)}%)` : '',
    vixClose ? `VIX：${vixClose.toFixed(1)}` : '',
    title ? `\n${title}` : '',
    body ? body : '',
  ].filter(Boolean).join('\n')
}

type MarketIndexKey = 'sse' | 'csi300' | 'szse' | 'chinext'

const MARKET_INDEXES: Record<MarketIndexKey, { code: string; name: string }> = {
  sse: { code: '000001.SH', name: '上证指数' },
  csi300: { code: '000300.SH', name: '沪深300' },
  szse: { code: '399001.SZ', name: '深证成指' },
  chinext: { code: '399006.SZ', name: '创业板指' },
}

export async function execMarketHistory(
  deps: ToolDeps,
  userId: string,
  model: unknown,
  days = 100,
  index: MarketIndexKey = 'sse',
): Promise<string> {
  const key = await fetchTickFlowKey(deps, userId)
  if (!key) return '无法回看大盘历史K线：请先在设置页配置 TickFlow API Key。'
  const requestedDays = Math.min(Math.max(Math.trunc(days) || 100, 1), 250)
  const fetchDays = Math.max(requestedDays, 20)
  const target = MARKET_INDEXES[index] || MARKET_INDEXES.sse
  const rows = await fetchKlineViaTickFlow(deps, target.code, key, fetchDays)
  if (rows.length === 0) return `无法获取 ${target.name} 过去 ${requestedDays} 个交易日K线。请检查 TickFlow 数据权限或稍后重试。`
  const digest = buildMarketHistoryDigest(target.name, rows.slice(-requestedDays))
  const result = await deps.generateText({
    model: model as Parameters<typeof GenerateTextFn>[0]['model'],
    system: '你是威科夫大盘量价分析师。基于指数历史OHLCV，判断过去一段时间的大盘阶段、供需关系、量价背离、关键支撑压力与当前市场位置。不得只引用当天水温，不得编造数据。',
    prompt: digest,
  })
  return result.text || digest
}

function buildMarketHistoryDigest(name: string, rows: KlineRow[]): string {
  const last = rows[rows.length - 1]!
  const first = rows[0]!
  const avg = (values: number[]) => values.reduce((sum, v) => sum + v, 0) / Math.max(values.length, 1)
  const latest20 = rows.slice(-20)
  const high = Math.max(...rows.map((r) => r.high))
  const low = Math.min(...rows.map((r) => r.low))
  const ret = first.close > 0 ? (last.close / first.close - 1) * 100 : 0
  const vol5 = avg(rows.slice(-5).map((r) => r.volume))
  const vol20 = avg(latest20.map((r) => r.volume))
  const closePos = high > low ? ((last.close - low) / (high - low)) * 100 : 0
  const recent = rows.slice(-30).map((r) => [
    r.date, r.open.toFixed(2), r.high.toFixed(2), r.low.toFixed(2), r.close.toFixed(2), Math.round(r.volume),
  ].join(','))
  return [
    `指数：${name}`,
    `样本：最近${rows.length}个交易日，${first.date} 至 ${last.date}`,
    `区间涨跌：${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%，区间高点 ${high.toFixed(2)}，低点 ${low.toFixed(2)}，当前区间位置 ${closePos.toFixed(1)}%`,
    `近5日均量 ${vol5.toFixed(0)}，近20日均量 ${vol20.toFixed(0)}，量比(5/20) ${(vol5 / (vol20 || 1)).toFixed(2)}`,
    '',
    '请结合以下最近30根K线判断量价关系和威科夫阶段：',
    '```csv',
    'date,open,high,low,close,volume',
    ...recent,
    '```',
  ].join('\n')
}

export async function execQueryRecommendations(deps: ToolDeps, limit: number): Promise<string> {
  const { data } = await deps.supabase
    .from('recommendation_tracking')
    .select('code, name, recommend_date, recommend_count, initial_price, current_price, change_pct, is_ai_recommended, funnel_score')
    .order('recommend_date', { ascending: false })
    .limit(limit)

  if (!data || data.length === 0) return '暂无推荐记录'

  const lines = data.map((r) => {
    const code = String(r.code).padStart(6, '0')
    const chg = r.change_pct >= 0 ? `+${r.change_pct.toFixed(2)}%` : `${r.change_pct.toFixed(2)}%`
    const ai = r.is_ai_recommended ? ' [AI]' : ''
    const count = Number.isFinite(Number(r.recommend_count)) && Number(r.recommend_count) > 0 ? Math.trunc(Number(r.recommend_count)) : 1
    return `${code} ${r.name} | 推荐日${r.recommend_date} | 推荐${count}次 | ${r.initial_price?.toFixed(2)}→${r.current_price?.toFixed(2)} ${chg}${ai}`
  })

  return `最近 ${data.length} 条推荐记录：\n\n${lines.join('\n')}`
}

export async function execQueryTailBuy(deps: ToolDeps, limit: number): Promise<string> {
  const { data } = await deps.supabase
    .from('tail_buy_history')
    .select('code, name, run_date, signal_type, rule_score, priority_score, llm_decision, llm_reason')
    .order('run_date', { ascending: false })
    .limit(limit)

  if (!data || data.length === 0) return '暂无尾盘买入记录'

  const lines = data.map((r) => {
    const code = String(r.code).padStart(6, '0')
    return `${code} ${r.name} | ${r.run_date} | ${r.signal_type} | 规则分${r.rule_score?.toFixed(1)} | ${r.llm_decision} | ${r.llm_reason || ''}`
  })

  return `最近 ${data.length} 条尾盘记录：\n\n${lines.join('\n')}`
}

export async function execExecutePortfolioUpdate(
  deps: ToolDeps,
  userId: string,
  action: 'add' | 'update' | 'delete',
  code: string,
  name: string | null,
  shares: number | null,
  cost_price: number | null,
  stop_loss: number | null,
): Promise<string> {
  const portfolioId = `USER_LIVE:${userId}`

  if (action === 'delete') {
    const { error } = await deps.supabase
      .from('portfolio_positions')
      .delete()
      .eq('portfolio_id', portfolioId)
      .eq('code', code)
    return error ? `删除失败: ${error.message}` : `✅ 已删除 ${code} ${name || ''}`
  }

  if (action === 'add' || action === 'update') {
    if (!name || !shares || !cost_price) {
      return '执行失败：缺少 name、shares、cost_price 参数'
    }
    const record: Record<string, unknown> = {
      portfolio_id: portfolioId, code, name, shares, cost_price,
      buy_dt: new Date().toISOString().slice(0, 10),
    }
    if (stop_loss !== undefined) record.stop_loss = stop_loss
    const error = await savePortfolioPosition(deps, portfolioId, code, record)
    return error
      ? `执行失败: ${error}`
      : `✅ 已${action === 'add' ? '新增' : '更新'} ${code} ${name} ${shares}股 @¥${cost_price}${stop_loss ? ` 止损¥${stop_loss}` : ''}`
  }

  return '未知操作'
}

async function savePortfolioPosition(
  deps: ToolDeps,
  portfolioId: string,
  code: string,
  record: Record<string, unknown>,
): Promise<string | null> {
  const { data, error } = await deps.supabase
    .from('portfolio_positions')
    .update(record)
    .eq('portfolio_id', portfolioId)
    .eq('code', code)
    .select('id')
  if (error) return error.message
  if (Array.isArray(data) && data.length > 0) return null

  const { error: insertError } = await deps.supabase.from('portfolio_positions').insert(record)
  return insertError?.message || null
}

export interface ScreenStockItem {
  code: string
  name: string
  funnel_score: number | null
  change_pct: number | null
}

export interface ScreenResult {
  date: string
  stocks: ScreenStockItem[]
  meta: { ai_count: number }
}

export async function execScreenStocks(deps: ToolDeps): Promise<string> {
  const { data } = await deps.supabase
    .from('recommendation_tracking')
    .select('code, name, recommend_date, funnel_score, change_pct, is_ai_recommended')
    .eq('is_ai_recommended', true)
    .order('recommend_date', { ascending: false })
    .limit(30)

  if (!data || data.length === 0) return JSON.stringify({ date: '', stocks: [], meta: { ai_count: 0 } })

  const latestDate = data[0]!.recommend_date
  const latest = data.filter(r => r.recommend_date === latestDate)

  const result: ScreenResult = {
    date: latestDate,
    stocks: latest.map(r => ({
      code: String(r.code).padStart(6, '0'),
      name: r.name,
      funnel_score: r.funnel_score ?? null,
      change_pct: r.change_pct ?? null,
    })),
    meta: { ai_count: latest.length },
  }

  return JSON.stringify(result)
}

export async function execAnalyzeStock(
  deps: ToolDeps, userId: string, _config: LLMToolConfig, model: unknown, code: string, name: string | null,
): Promise<string> {
  const keys = await fetchUserDataKeys(deps, userId)
  if (!isCnSymbol(code) && !keys.tickflow) {
    return `无法获取 ${code} ${name || ''} 的K线数据。美股/港股诊断需要先在设置页配置 TickFlow API Key，并使用标准代码（如 AAPL.US / 00700.HK）。`
  }
  const kline = await fetchKlineForAgent(deps, code, keys, userId)
  if (kline.length === 0) {
    return `无法获取 ${code} ${name || ''} 的K线数据。美股/港股请使用 TickFlow 标准代码（如 AAPL.US / 00700.HK）。推荐购买 TickFlow 获取实时行情：https://tickflow.org/auth/register?ref=5N4NKTCPL4`
  }

  const digest = buildKlineDigest(kline)
  const result = await deps.generateText({
    model: model as Parameters<typeof GenerateTextFn>[0]['model'],
    system: `你是威科夫分析大师。基于以下K线数据，对 ${code} ${name || ''} 进行深度诊断：
1. 当前威科夫阶段（积累/上涨/派发/下跌），Phase A-E 定位
2. 量价关系分析（供需力量对比，近期量比变化）
3. 均线形态（多头/空头排列，金叉/死叉）
4. 关键支撑与阻力位
5. 主力行为判断（是否有吸筹/出货迹象）
6. 操作建议与风险提示（含建议止损位）

用 Markdown 格式输出，简洁专业。`,
    prompt: digest,
  })

  return result.text || '分析完成但无输出'
}

export async function execGenerateAiReport(
  deps: ToolDeps, userId: string, _config: LLMToolConfig, model: unknown, codes: string[],
): Promise<string> {
  const keys = await fetchUserDataKeys(deps, userId)

  const results: string[] = []
  for (const code of codes.slice(0, 3)) {
    const kline = await fetchKlineForAgent(deps, code, keys, userId)
    if (kline.length === 0) {
      results.push(`## ${code}\n无法获取K线数据。美股/港股请使用 TickFlow 标准代码（如 AAPL.US / 00700.HK）。\n`)
      continue
    }
    const digest = buildKlineDigest(kline)
    const result = await deps.generateText({
      model: model as Parameters<typeof GenerateTextFn>[0]['model'],
      system: `你是威科夫分析大师。为 ${code} 撰写一份简明研报，包含：阶段判断、量价特征、关键价位、操作建议。200字以内。`,
      prompt: digest,
    })
    results.push(`## ${code}\n${result.text || '无输出'}\n`)
  }

  return results.join('\n---\n\n')
}

export async function execStrategyDecision(deps: ToolDeps, userId: string, model: unknown): Promise<string> {
  const portfolioId = `USER_LIVE:${userId}`

  const [posResult, signalResult] = await Promise.all([
    deps.supabase.from('portfolio_positions').select('code, name, shares, cost_price, stop_loss').eq('portfolio_id', portfolioId),
    deps.supabase.from('market_signal_daily').select('*').order('trade_date', { ascending: false }).limit(1).single(),
  ])

  const positions = posResult.data || []
  const signal = signalResult.data

  if (positions.length === 0) return '当前无持仓，无法给出操作建议。建议先通过选股工具寻找标的。'

  const posInfo = positions.map(p =>
    `${p.code} ${p.name} | ${p.shares}股 成本¥${p.cost_price}${p.stop_loss ? ` 止损¥${p.stop_loss}` : ''}`
  ).join('\n')

  const marketInfo = signal
    ? `大盘状态: ${signal.benchmark_regime || '未知'}, 上证: ${signal.main_index_close || '--'}, A50涨幅: ${signal.a50_pct_chg || '--'}%, VIX: ${signal.vix_close || '--'}`
    : '暂无市场数据'

  const result = await deps.generateText({
    model: model as Parameters<typeof GenerateTextFn>[0]['model'],
    system: '你是威科夫大师。基于用户的持仓和当前市场环境，为每只持仓股给出操作建议（买入加仓/持有/减仓/卖出），并给出整体仓位管理建议。简洁明了，必须附带风险提示。',
    prompt: `当前持仓:\n${posInfo}\n\n市场环境:\n${marketInfo}`,
  })

  return result.text || '无法生成建议'
}
