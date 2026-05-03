import { createOpenAI } from '@ai-sdk/openai'
import { generateText, tool } from 'ai'
import { z } from 'zod'
import { supabase } from './supabase'

const SYSTEM_PROMPT = `# 角色设定

你就是理查德·D·威科夫（Richard D. Wyckoff）本人。
你以"综合人（Composite Man）"视角审视一切：每一根 K 线背后都有一个阴谋，每一次放量都是主力在行动。
你的语气冷峻、老练、一针见血。直接告诉对方盘面的真相。

# 你手里的武器

1. **搜索** — search_stock：在全市场中搜索股票（名称或代码）
2. **查看持仓** — view_portfolio：查看用户的持仓列表和资金
3. **大盘水温** — market_overview：查看市场信号、指数走势
4. **战绩追踪** — query_recommendations：查询推荐跟踪记录
5. **尾盘记录** — query_tail_buy：查询尾盘买入记录
6. **调仓操作** — update_portfolio：新增、修改或删除持仓
7. **个股诊断** — analyze_stock：对单只股票做威科夫深度诊断（K线+量价+阶段）
8. **漏斗选股** — screen_stocks：查看最新一期漏斗选股结果
9. **AI 研报** — generate_ai_report：为指定股票生成威科夫深度研报
10. **策略建议** — generate_strategy_decision：基于持仓+大盘给出操作建议

# 工具路由原则

只做用户要求的事，绝不多做。
- "我有什么持仓" → view_portfolio
- "帮我看看某只股票" → analyze_stock
- "大盘怎么样" → market_overview
- "推荐了什么" → query_recommendations
- "尾盘买了啥" → query_tail_buy
- "帮我选股" / "今天有什么好票" → screen_stocks
- "帮我出个研报" → generate_ai_report
- "我该怎么操作" / "给个建议" → generate_strategy_decision

# 行为铁律

1. 数据先行：所有分析基于工具返回的真实数据，绝不凭空编造数字。
2. 中文输出：使用中文回复，用 Markdown 格式让信息清晰。
3. 风险声明：涉及具体操作建议时，附带风险提示。`

export interface LLMConfig {
  api_key: string
  model: string
  base_url: string
}

export async function loadLLMConfig(userId: string): Promise<LLMConfig | null> {
  const { data } = await supabase
    .from('user_settings')
    .select('*')
    .eq('user_id', userId)
    .single()

  if (!data) return null

  const provider = data.chat_provider || '1route'
  let api_key = '', model = '', base_url = ''

  if (provider === 'gemini') {
    api_key = data.gemini_api_key || ''
    model = data.gemini_model || 'gemini-2.0-flash'
    base_url = data.gemini_base_url || ''
  } else if (provider === 'openai') {
    api_key = data.openai_api_key || ''
    model = data.openai_model || 'gpt-4o'
    base_url = data.openai_base_url || 'https://api.openai.com/v1'
  } else if (provider === 'deepseek') {
    api_key = data.deepseek_api_key || ''
    model = data.deepseek_model || 'deepseek-chat'
    base_url = data.deepseek_base_url || 'https://api.deepseek.com/v1'
  } else {
    const custom = typeof data.custom_providers === 'string'
      ? JSON.parse(data.custom_providers || '{}')
      : (data.custom_providers || {})
    const info = custom[provider] || {}
    api_key = info.apikey || info.api_key || ''
    model = info.model || ''
    base_url = info.baseurl || info.base_url || ''
  }

  if (!api_key) return null
  return { api_key, model, base_url }
}

async function fetchTickFlowKey(userId: string): Promise<string | null> {
  const { data } = await supabase
    .from('user_settings')
    .select('tickflow_api_key')
    .eq('user_id', userId)
    .single()
  return data?.tickflow_api_key || null
}

interface KlineRow {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

async function fetchKlineForAgent(code: string, apiKey: string): Promise<KlineRow[]> {
  const end = new Date()
  end.setDate(end.getDate() - 1)
  const start = new Date()
  start.setDate(start.getDate() - 500)
  const fmt = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, '')

  const url = `https://api.tickflow.io/v1/stock/history?symbol=${code}&start_date=${fmt(start)}&end_date=${fmt(end)}&adjust=qfq&limit=250`
  try {
    const resp = await fetch(url, { headers: { Authorization: `Bearer ${apiKey}` } })
    if (!resp.ok) return []
    const json = await resp.json()
    const rows = json.data || json.records || json || []
    if (!Array.isArray(rows)) return []
    return rows.map((r: Record<string, unknown>) => ({
      date: String(r.date || r.trade_date || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
      open: Number(r.open || 0),
      high: Number(r.high || 0),
      low: Number(r.low || 0),
      close: Number(r.close || 0),
      volume: Number(r.volume || r.vol || 0),
    })).filter((d: KlineRow) => d.date && d.close > 0)
  } catch {
    return []
  }
}

function buildKlineDigest(data: KlineRow[]): string {
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

function createReasoningFetch(): typeof globalThis.fetch {
  const cache: string[] = []

  return async (input, init) => {
    if (init?.body && typeof init.body === 'string') {
      try {
        const body = JSON.parse(init.body)
        if (Array.isArray(body.messages)) {
          let idx = 0
          for (const msg of body.messages) {
            if (msg.role === 'assistant' && !msg.reasoning_content && idx < cache.length) {
              msg.reasoning_content = cache[idx]
            }
            if (msg.role === 'assistant') idx++
          }
          init = { ...init, body: JSON.stringify(body) }
        }
      } catch {}
    }

    const res = await globalThis.fetch(input, init)

    const clone = res.clone()
    clone.json().then((data: Record<string, unknown>) => {
      const choices = data?.choices as Array<{ message?: { reasoning_content?: string } }> | undefined
      const rc = choices?.[0]?.message?.reasoning_content
      if (rc) cache.push(rc)
    }).catch(() => {})

    return res
  }
}

function createProxiedProvider(config: LLMConfig) {
  return createOpenAI({
    apiKey: config.api_key,
    baseURL: '/api/llm-proxy',
    headers: { 'X-Target-URL': config.base_url },
    compatibility: 'compatible',
    fetch: createReasoningFetch(),
  })
}

function buildTools(userId: string, config: LLMConfig) {
  return {
    search_stock: tool({
      description: '搜索股票，支持代码或名称。返回匹配的股票列表。',
      parameters: z.object({
        query: z.string().describe('股票代码或名称关键词'),
      }),
      execute: async ({ query }) => {
        const q = query.trim()
        const isCode = /^\d+$/.test(q)
        let result

        if (isCode) {
          result = await supabase
            .from('recommendation_tracking')
            .select('code, name')
            .eq('code', parseInt(q))
            .limit(5)
        } else {
          result = await supabase
            .from('recommendation_tracking')
            .select('code, name')
            .ilike('name', `%${q}%`)
            .limit(10)
        }

        const rows = result.data || []
        if (rows.length === 0) return `未找到匹配"${query}"的股票`

        const seen = new Set<number>()
        const unique = rows.filter((r) => {
          if (seen.has(r.code)) return false
          seen.add(r.code)
          return true
        })

        return unique.map((r) =>
          `${String(r.code).padStart(6, '0')} ${r.name}`
        ).join('\n')
      },
    }),

    view_portfolio: tool({
      description: '查看用户当前持仓列表（代码、名称、股数、成本价）和可用资金。',
      parameters: z.object({}),
      execute: async () => {
        const portfolioId = `USER_LIVE:${userId}`

        const [pfResult, posResult] = await Promise.all([
          supabase.from('portfolios').select('free_cash').eq('portfolio_id', portfolioId).single(),
          supabase.from('portfolio_positions').select('code, name, shares, cost_price, buy_dt, stop_loss').eq('portfolio_id', portfolioId),
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
      },
    }),

    market_overview: tool({
      description: '查看最新大盘行情信号：市场状态（regime）、上证指数、A50、VIX、市场提示。',
      parameters: z.object({}),
      execute: async () => {
        const { data } = await supabase
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
      },
    }),

    query_recommendations: tool({
      description: '查询推荐跟踪记录，显示推荐的股票及其涨跌表现。',
      parameters: z.object({
        limit: z.number().optional().default(20).describe('返回条数，默认20'),
      }),
      execute: async ({ limit }) => {
        const { data } = await supabase
          .from('recommendation_tracking')
          .select('code, name, recommend_date, initial_price, current_price, change_pct, is_ai_recommended, funnel_score')
          .order('recommend_date', { ascending: false })
          .limit(limit)

        if (!data || data.length === 0) return '暂无推荐记录'

        const lines = data.map((r) => {
          const code = String(r.code).padStart(6, '0')
          const chg = r.change_pct >= 0 ? `+${r.change_pct.toFixed(2)}%` : `${r.change_pct.toFixed(2)}%`
          const ai = r.is_ai_recommended ? ' [AI]' : ''
          return `${code} ${r.name} | 推荐日${r.recommend_date} | ${r.initial_price?.toFixed(2)}→${r.current_price?.toFixed(2)} ${chg}${ai}`
        })

        return `最近 ${data.length} 条推荐记录：\n\n${lines.join('\n')}`
      },
    }),

    query_tail_buy: tool({
      description: '查询尾盘买入策略的历史记录（BUY/WATCH 决策、评分、LLM 理由）。',
      parameters: z.object({
        limit: z.number().optional().default(20).describe('返回条数'),
      }),
      execute: async ({ limit }) => {
        const { data } = await supabase
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
      },
    }),

    update_portfolio: tool({
      description: '新增、修改或删除持仓。action: "add"新增, "update"修改, "delete"删除。',
      parameters: z.object({
        action: z.enum(['add', 'update', 'delete']).describe('操作类型'),
        code: z.string().describe('6位股票代码'),
        name: z.string().optional().describe('股票名称'),
        shares: z.number().optional().describe('股数'),
        cost_price: z.number().optional().describe('成本价'),
        stop_loss: z.number().optional().describe('止损价'),
      }),
      execute: async ({ action, code, name, shares, cost_price, stop_loss }) => {
        const portfolioId = `USER_LIVE:${userId}`

        if (action === 'delete') {
          const { error } = await supabase
            .from('portfolio_positions')
            .delete()
            .eq('portfolio_id', portfolioId)
            .eq('code', code)
          return error ? `删除失败: ${error.message}` : `已删除 ${code} ${name || ''}`
        }

        if (action === 'add' || action === 'update') {
          if (!name || !shares || !cost_price) {
            return '新增/修改持仓需要 name、shares、cost_price 参数'
          }
          const record: Record<string, unknown> = {
            portfolio_id: portfolioId,
            code,
            name,
            shares,
            cost_price,
            buy_dt: new Date().toISOString().slice(0, 10),
          }
          if (stop_loss !== undefined) record.stop_loss = stop_loss
          const { error } = await supabase
            .from('portfolio_positions')
            .upsert(record)
          return error ? `操作失败: ${error.message}` : `已${action === 'add' ? '新增' : '更新'} ${code} ${name} ${shares}股 @¥${cost_price}${stop_loss ? ` 止损¥${stop_loss}` : ''}`
        }

        return '未知操作'
      },
    }),

    analyze_stock: tool({
      description: '对单只股票做威科夫深度诊断：K线走势、量价关系、均线形态、阶段判断。需要股票代码。',
      parameters: z.object({
        code: z.string().describe('6位股票代码'),
        name: z.string().optional().describe('股票名称'),
      }),
      execute: async ({ code, name }) => {
        const tickflowKey = await fetchTickFlowKey(userId)
        if (!tickflowKey) {
          return `未配置 TickFlow API Key，无法获取 ${code} 的K线数据。请在设置页配置。`
        }

        const kline = await fetchKlineForAgent(code, tickflowKey)
        if (kline.length === 0) {
          return `无法获取 ${code} ${name || ''} 的K线数据，请检查代码是否正确。`
        }

        const digest = buildKlineDigest(kline)

        const llm = createProxiedProvider(config)
        const result = await generateText({
          model: llm(config.model),
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
      },
    }),

    screen_stocks: tool({
      description: '查看最新一期漏斗选股结果：AI推荐的候选股票列表及其评分。',
      parameters: z.object({}),
      execute: async () => {
        const { data } = await supabase
          .from('recommendation_tracking')
          .select('code, name, recommend_date, funnel_score, change_pct, is_ai_recommended')
          .eq('is_ai_recommended', true)
          .order('recommend_date', { ascending: false })
          .limit(30)

        if (!data || data.length === 0) return '暂无选股结果'

        const latestDate = data[0]!.recommend_date
        const latest = data.filter(r => r.recommend_date === latestDate)

        const lines = latest.map((r) => {
          const code = String(r.code).padStart(6, '0')
          const score = r.funnel_score?.toFixed(2) || '--'
          const chg = r.change_pct != null ? (r.change_pct >= 0 ? `+${r.change_pct.toFixed(2)}%` : `${r.change_pct.toFixed(2)}%`) : '--'
          return `${code} ${r.name} | 漏斗分 ${score} | 推荐后涨跌 ${chg}`
        })

        return `最新选股日期 ${latestDate}，共 ${latest.length} 只 AI 候选：\n\n${lines.join('\n')}`
      },
    }),

    generate_ai_report: tool({
      description: '为指定股票生成威科夫深度研报（AI分析），支持多只股票批量生成。',
      parameters: z.object({
        codes: z.array(z.string()).describe('股票代码数组，如 ["600519", "000858"]'),
      }),
      execute: async ({ codes }) => {
        const tickflowKey = await fetchTickFlowKey(userId)
        if (!tickflowKey) return '未配置 TickFlow API Key，无法生成研报。'

        const results: string[] = []
        for (const code of codes.slice(0, 3)) {
          const kline = await fetchKlineForAgent(code, tickflowKey)
          if (kline.length === 0) {
            results.push(`## ${code}\n无法获取K线数据\n`)
            continue
          }
          const digest = buildKlineDigest(kline)
          const llm = createProxiedProvider(config)
          const result = await generateText({
            model: llm(config.model),
            system: `你是威科夫分析大师。为 ${code} 撰写一份简明研报，包含：阶段判断、量价特征、关键价位、操作建议。200字以内。`,
            prompt: digest,
          })
          results.push(`## ${code}\n${result.text || '无输出'}\n`)
        }

        return results.join('\n---\n\n')
      },
    }),

    generate_strategy_decision: tool({
      description: '基于当前持仓和市场状态，给出买入/卖出/持有的操作建议。',
      parameters: z.object({}),
      execute: async () => {
        const portfolioId = `USER_LIVE:${userId}`

        const [posResult, signalResult] = await Promise.all([
          supabase.from('portfolio_positions').select('code, name, shares, cost_price, stop_loss').eq('portfolio_id', portfolioId),
          supabase.from('market_signal_daily').select('*').order('trade_date', { ascending: false }).limit(1).single(),
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

        const llm = createProxiedProvider(config)
        const result = await generateText({
          model: llm(config.model),
          system: '你是威科夫大师。基于用户的持仓和当前市场环境，为每只持仓股给出操作建议（买入加仓/持有/减仓/卖出），并给出整体仓位管理建议。简洁明了，必须附带风险提示。',
          prompt: `当前持仓:\n${posInfo}\n\n市场环境:\n${marketInfo}`,
        })

        return result.text || '无法生成建议'
      },
    }),
  }
}

export interface StreamCallbacks {
  onText: (text: string) => void
  onToolCall: (toolName: string) => void
  onFinish: (finalText: string) => void
  onError: (error: Error) => void
}

export async function runChatAgentStream(
  config: LLMConfig,
  userId: string,
  messages: { role: 'user' | 'assistant'; content: string }[],
  callbacks: StreamCallbacks,
): Promise<void> {
  const provider = createProxiedProvider(config)

  const tools = buildTools(userId, config)

  try {
    const result = await generateText({
      model: provider(config.model),
      system: SYSTEM_PROMPT,
      messages,
      tools,
      maxSteps: 10,
      onStepFinish: ({ toolCalls, text }) => {
        if (toolCalls && toolCalls.length > 0) {
          for (const tc of toolCalls) {
            callbacks.onToolCall(tc.toolName)
          }
        }
        if (text) {
          callbacks.onText(text)
        }
      },
    })

    callbacks.onFinish(result.text)
  } catch (err) {
    callbacks.onError(err instanceof Error ? err : new Error(String(err)))
  }
}
