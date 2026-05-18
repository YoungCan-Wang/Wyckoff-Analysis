import { describe, it, expect, vi } from 'vitest'
import type { ToolDeps, KlineRow } from '../chat-tools'
import {
  buildKlineDigest,
  execSearchStock,
  execViewPortfolio,
  execMarketOverview,
  execQueryRecommendations,
  execQueryTailBuy,
  execExecutePortfolioUpdate,
  execScreenStocks,
  execAnalyzeStock,
  execMarketHistory,
} from '../chat-tools'

function createMockChain(resolvedData: unknown = null, error: unknown = null) {
  const chain: Record<string, unknown> = {}
  const terminal = () => Promise.resolve({ data: resolvedData, error })
  for (const method of ['select', 'eq', 'ilike', 'order', 'limit', 'delete', 'update']) {
    chain[method] = vi.fn().mockReturnValue(chain)
  }
  chain['insert'] = vi.fn().mockImplementation(terminal)
  chain['single'] = vi.fn().mockImplementation(terminal)
  chain['upsert'] = vi.fn().mockImplementation(terminal)
  // make the chain itself thenable for queries without .single()
  chain['then'] = (resolve: (v: unknown) => void) => resolve({ data: resolvedData, error })
  return chain
}

function createPortfolioWriteDeps(updateRows: unknown[]) {
  const updateChain = createMockChain(updateRows)
  const insertChain = createMockChain(null)
  const mockFrom = vi.fn()
    .mockReturnValueOnce(updateChain)
    .mockReturnValueOnce(insertChain)
  const deps = {
    supabase: { from: mockFrom } as unknown as ToolDeps['supabase'],
    fetch: vi.fn(),
    generateText: vi.fn(),
  } as unknown as ToolDeps
  return { deps, updateChain, insertChain }
}

function createMockDeps(tableData: Record<string, unknown> = {}): ToolDeps {
  const mockFrom = vi.fn().mockImplementation((table: string) => {
    const data = tableData[table] ?? null
    return createMockChain(data)
  })

  return {
    supabase: { from: mockFrom } as unknown as ToolDeps['supabase'],
    fetch: vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve({}) } as Response),
    generateText: vi.fn().mockResolvedValue({ text: 'mocked LLM response' }),
  }
}

function makeKlineRows(n: number, base = 10): KlineRow[] {
  return Array.from({ length: n }, (_, i) => ({
    date: `2024-01-${String(i + 1).padStart(2, '0')}`,
    open: base + i * 0.1,
    high: base + i * 0.1 + 0.5,
    low: base + i * 0.1 - 0.3,
    close: base + i * 0.12,
    volume: 100000 + i * 1000,
  }))
}

describe('buildKlineDigest', () => {
  it('returns placeholder for empty data', () => {
    expect(buildKlineDigest([])).toBe('无可用K线数据')
  })

  it('produces stable output for 5 rows', () => {
    const rows = makeKlineRows(5)
    expect(buildKlineDigest(rows)).toMatchSnapshot()
  })

  it('produces stable output for 20 rows', () => {
    const rows = makeKlineRows(20)
    expect(buildKlineDigest(rows)).toMatchSnapshot()
  })

  it('includes MA50 for 50+ rows', () => {
    const rows = makeKlineRows(60)
    const result = buildKlineDigest(rows)
    expect(result).toContain('MA50=')
  })

  it('includes MA120 for 120+ rows', () => {
    const rows = makeKlineRows(130)
    const result = buildKlineDigest(rows)
    expect(result).toContain('MA120=')
  })
})

describe('execSearchStock', () => {
  it('returns not-found message when no results', async () => {
    const deps = createMockDeps({
      recommendation_tracking: [],
      portfolio_positions: [],
      tail_buy_history: [],
    })
    const result = await execSearchStock(deps, 'user1', '999999')
    expect(result).toContain('未找到匹配')
  })

  it('returns formatted stock list with code and name', async () => {
    const stocks = [{ code: 600519, name: '贵州茅台' }]
    const deps = createMockDeps({
      recommendation_tracking: stocks,
      portfolio_positions: [],
      tail_buy_history: [],
    })
    const result = await execSearchStock(deps, 'user1', '贵州')
    expect(result).toContain('600519')
    expect(result).toContain('贵州茅台')
  })
})

describe('execViewPortfolio', () => {
  it('returns empty portfolio message', async () => {
    const deps = createMockDeps({
      portfolios: { free_cash: 50000 },
      portfolio_positions: [],
    })
    const result = await execViewPortfolio(deps, 'user1')
    expect(result).toContain('当前无持仓')
    expect(result).toContain('50,000')
  })

  it('returns formatted positions', async () => {
    const deps = createMockDeps({
      portfolios: { free_cash: 10000 },
      portfolio_positions: [
        { code: '000001', name: '平安银行', shares: 1000, cost_price: 12.5, buy_dt: '2024-01-01', stop_loss: 11.0 },
      ],
    })
    const result = await execViewPortfolio(deps, 'user1')
    expect(result).toContain('持仓 1 只')
    expect(result).toContain('平安银行')
    expect(result).toContain('1000股')
  })
})

describe('execMarketOverview', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ market_signal_daily: [] })
    const result = await execMarketOverview(deps)
    expect(result).toBe('暂无最新市场信号数据')
  })

  it('returns formatted market data', async () => {
    const deps = createMockDeps({
      market_signal_daily: [
        { benchmark_regime: 'RISK_ON', main_index_close: 3200, main_index_today_pct: 1.5, a50_close: 14000, a50_pct_chg: 0.8, vix_close: 15.2 },
      ],
    })
    const result = await execMarketOverview(deps)
    expect(result).toContain('偏强')
    expect(result).toContain('3200')
  })
})

describe('execMarketHistory', () => {
  it('uses TickFlow index K-line history for historical market questions', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: ' tf-test ', tushare_token: '' } })
    deps.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        data: {
          '000001.SH': {
            timestamp: [1704067200000, 1704153600000, 1704240000000],
            open: [3000, 3010, 3020],
            high: [3030, 3040, 3050],
            low: [2990, 3000, 3010],
            close: [3020, 3030, 3040],
            volume: [1000, 1200, 1300],
          },
        },
      }),
    } as Response) as unknown as ToolDeps['fetch']

    const result = await execMarketHistory(deps, 'user1', {}, 100, 'sse')

    expect(result).toBe('mocked LLM response')
    expect(deps.fetch).toHaveBeenCalledWith(
      expect.stringContaining('symbol=000001.SH'),
      expect.objectContaining({ headers: expect.objectContaining({ 'x-api-key': 'tf-test' }) }),
    )
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('最近3个交易日'),
    }))
  })

  it('explains missing TickFlow key', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: '', tushare_token: '' } })

    const result = await execMarketHistory(deps, 'user1', {}, 100, 'sse')

    expect(result).toContain('配置 TickFlow API Key')
    expect(deps.fetch).not.toHaveBeenCalled()
  })
})

describe('execQueryRecommendations', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ recommendation_tracking: [] })
    const result = await execQueryRecommendations(deps, 10)
    expect(result).toBe('暂无推荐记录')
  })

  it('formats recommendation entries', async () => {
    const deps = createMockDeps({
      recommendation_tracking: [
        { code: 600519, name: '贵州茅台', recommend_date: 20240101, recommend_count: 3, initial_price: 1800, current_price: 1900, change_pct: 5.56, is_ai_recommended: true },
      ],
    })
    const result = await execQueryRecommendations(deps, 10)
    expect(result).toContain('600519')
    expect(result).toContain('推荐3次')
    expect(result).toContain('+5.56%')
    expect(result).toContain('[AI]')
  })
})

describe('execQueryTailBuy', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ tail_buy_history: [] })
    const result = await execQueryTailBuy(deps, 10)
    expect(result).toBe('暂无尾盘买入记录')
  })
})

describe('execExecutePortfolioUpdate', () => {
  it('handles delete action', async () => {
    const deps = createMockDeps({ portfolio_positions: null })
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'delete', '600519', '贵州茅台', null, null, null)
    expect(result).toContain('已删除')
    expect(result).toContain('600519')
  })

  it('rejects add without required fields', async () => {
    const deps = createMockDeps({})
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', null, null, null, null)
    expect(result).toContain('执行失败')
  })

  it('handles add action with all fields', async () => {
    const deps = createMockDeps({ portfolio_positions: null })
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', '贵州茅台', 100, 1800, 1700)
    expect(result).toContain('已新增')
    expect(result).toContain('100股')
  })

  it('updates an existing position without inserting a duplicate row', async () => {
    const { deps, updateChain, insertChain } = createPortfolioWriteDeps([{ id: 'pos-1' }])

    const result = await execExecutePortfolioUpdate(deps, 'user1', 'update', '600519', '贵州茅台', 200, 1810, 1700)

    expect(result).toContain('已更新')
    expect(updateChain.update).toHaveBeenCalledWith(expect.objectContaining({ code: '600519', shares: 200 }))
    expect(updateChain.eq).toHaveBeenCalledWith('portfolio_id', 'USER_LIVE:user1')
    expect(updateChain.eq).toHaveBeenCalledWith('code', '600519')
    expect(insertChain.insert).not.toHaveBeenCalled()
  })

  it('inserts a position only when no existing row matches', async () => {
    const { deps, insertChain } = createPortfolioWriteDeps([])

    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', '贵州茅台', 100, 1800, 1700)

    expect(result).toContain('已新增')
    expect(insertChain.insert).toHaveBeenCalledWith(expect.objectContaining({ portfolio_id: 'USER_LIVE:user1', code: '600519' }))
  })
})

describe('execScreenStocks', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ recommendation_tracking: [] })
    const result = await execScreenStocks(deps)
    const parsed = JSON.parse(result)
    expect(parsed.stocks).toEqual([])
    expect(parsed.meta.ai_count).toBe(0)
  })
})

describe('execAnalyzeStock', () => {
  it('uses TickFlow batch fallback for market symbols', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: ' tf-test ', tushare_token: '' } })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: {} }) })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          data: {
            'AAPL.US': {
              timestamp: [1704067200000, 1704153600000],
              open: [100, 101],
              high: [102, 103],
              low: [99, 100],
              close: [101, 102],
              volume: [1000, 1200],
            },
          },
        }),
      })
    deps.fetch = fetchMock as unknown as ToolDeps['fetch']

    const result = await execAnalyzeStock(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      'AAPL.US',
      '苹果',
    )

    expect(result).toBe('mocked LLM response')
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('/api/llm-proxy/v1/klines/batch?'),
      expect.objectContaining({ headers: expect.objectContaining({ 'x-api-key': 'tf-test' }) }),
    )
  })

  it('explains missing TickFlow key for market symbols', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: '', tushare_token: '' } })

    const result = await execAnalyzeStock(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      'AAPL.US',
      '苹果',
    )

    expect(result).toContain('设置页配置 TickFlow API Key')
    expect(deps.fetch).not.toHaveBeenCalled()
  })
})
