import { useCallback, useState, useMemo, useRef, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createChart, HistogramSeries, type Time } from 'lightweight-charts'
import { supabase } from '@/lib/supabase'
import { checkWhitelist } from '@/lib/kline'
import { WyckoffLoading } from '@/components/loading'
import { usePreferences } from '@/lib/preferences'
import { useAuthStore } from '@/stores/auth'

type MarketTab = 'cn' | 'us' | 'hk'

const MARKET_TABLE: Record<MarketTab, string> = {
  cn: 'recommendation_tracking',
  us: 'recommendation_tracking_us',
  hk: 'recommendation_tracking_hk',
}

interface Recommendation {
  code: number | string
  name: string | null
  recommend_date: number
  initial_price: number | null
  current_price: number | null
  change_pct: number | null
  mfe_pct?: number | null
  mae_pct?: number | null
  range_amp_pct?: number | null
  mfe_price?: number | null
  mae_price?: number | null
  performance_days?: number | null
  is_ai_recommended: boolean
  rag_vetoed: boolean
  funnel_score: number | null
  recommend_count: number | null
  recommend_reason: string | null
}

interface SummaryStats {
  count: number
  avg: number | null
  best: number | null
  worst: number | null
  totalRecommendations: number
}

interface TrackingReadyContentProps {
  activeDates: number[]
  activeOldestDate: number | null
  filtered: Recommendation[]
  latestDate: number | null
  market: MarketTab
  onlyAI: boolean
  search: string
  selectedWindow: RecommendationWindow
  sortBy: SortBy
  sortOrder: SortOrder
  stats: SummaryStats | null
  visibleData: Recommendation[]
  windowRows: Recommendation[]
  onOnlyAIChange: (value: boolean) => void
  onSearchChange: (value: string) => void
  onSelectedWindowChange: (value: RecommendationWindow) => void
  onSortByChange: (value: SortBy) => void
  onSortOrderChange: (value: SortOrder) => void
}

const RETENTION_DATES = 30
const AVG_WINDOWS = [5, 10, 15, 20, 25, 30] as const
type RecommendationWindow = (typeof AVG_WINDOWS)[number]
type SortBy = 'date' | 'change' | 'score' | 'count' | 'mfe' | 'mae'
type SortOrder = 'desc' | 'asc'

async function fetchTracking(market: MarketTab): Promise<Recommendation[]> {
  const { data, error } = await supabase
    .from(MARKET_TABLE[market])
    .select('*')
    .order('recommend_date', { ascending: false })
    .limit(2000)
  if (error) throw new Error(`${MARKET_TABLE[market]}: ${error.message}`)
  return data || []
}

export function TrackingPage() {
  const [market, setMarket] = useState<MarketTab>('cn')
  const [search, setSearch] = useState('')
  const [onlyAI, setOnlyAI] = useState(false)
  const [sortBy, setSortBy] = useState<SortBy>('date')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')
  const [selectedWindow, setSelectedWindow] = useState<RecommendationWindow>(30)

  const user = useAuthStore((s) => s.user)
  const whitelist = useQuery({
    queryKey: ['whitelist', user?.id],
    queryFn: () => checkWhitelist(user!.id),
    enabled: !!user?.id && market !== 'cn',
  })

  const needsGate = market !== 'cn'
  const isWhitelisted = !needsGate || whitelist.data === true

  const { data = [], isLoading: loading, error: fetchError } = useQuery({
    queryKey: ['tracking', market],
    queryFn: () => fetchTracking(market),
    enabled: !needsGate || isWhitelisted,
    retry: 1,
  })

  const latestDates = useMemo(() => getLatestRecommendDates(data, RETENTION_DATES), [data])
  const activeDates = useMemo(() => latestDates.slice(0, selectedWindow), [latestDates, selectedWindow])
  const windowRows = useMemo(() => {
    const dateSet = new Set(activeDates)
    return data.filter((row) => dateSet.has(row.recommend_date))
  }, [data, activeDates])
  const visibleData = useMemo(() => dedupeRecommendations(windowRows), [windowRows])

  const filtered = useMemo(() => {
    let result = visibleData
    if (search) {
      const q = search.toLowerCase()
      result = result.filter(
        (r) => String(r.code).includes(q) || (r.name ?? '').toLowerCase().includes(q),
      )
    }
    if (onlyAI) {
      result = result.filter((r) => r.is_ai_recommended)
    }
    return sortRecommendations(result, sortBy, sortOrder)
  }, [visibleData, search, onlyAI, sortBy, sortOrder])

  const stats = useMemo(() => buildSummaryStats(visibleData), [visibleData])
  const latestDate = latestDates[0] ?? null
  const oldestDate = latestDates.at(-1) ?? null
  const activeOldestDate = activeDates.at(-1) ?? null
  if (needsGate && whitelist.isLoading) return <WyckoffLoading />

  return (
    <div className="h-full overflow-auto p-6">
      <TrackingHeader latestDate={latestDate} oldestDate={oldestDate} />
      <MarketTabs market={market} onMarketChange={setMarket} />
      {needsGate && !isWhitelisted ? (
        <BetaLocked />
      ) : fetchError ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-4 text-sm text-destructive">
          {fetchError.message}
        </div>
      ) : loading ? (
        <WyckoffLoading />
      ) : (
        <TrackingReadyContent
          activeDates={activeDates}
          activeOldestDate={activeOldestDate}
          filtered={filtered}
          latestDate={latestDate}
          market={market}
          onlyAI={onlyAI}
          search={search}
          selectedWindow={selectedWindow}
          sortBy={sortBy}
          sortOrder={sortOrder}
          stats={stats}
          visibleData={visibleData}
          windowRows={windowRows}
          onOnlyAIChange={setOnlyAI}
          onSearchChange={setSearch}
          onSelectedWindowChange={setSelectedWindow}
          onSortByChange={setSortBy}
          onSortOrderChange={setSortOrder}
        />
      )}
    </div>
  )
}

function TrackingReadyContent(props: TrackingReadyContentProps) {
  const {
    activeDates,
    activeOldestDate,
    filtered,
    latestDate,
    market,
    onlyAI,
    search,
    selectedWindow,
    sortBy,
    sortOrder,
    stats,
    visibleData,
    windowRows,
    onOnlyAIChange,
    onSearchChange,
    onSelectedWindowChange,
    onSortByChange,
    onSortOrderChange,
  } = props
  const handleSort = useCallback((next: SortBy) => {
    if (next === sortBy) { onSortOrderChange(sortOrder === 'desc' ? 'asc' : 'desc'); return }
    onSortByChange(next); onSortOrderChange('desc')
  }, [onSortByChange, onSortOrderChange, sortBy, sortOrder])
  return (
    <>
      <DateWindowFilter activeDateCount={activeDates.length} activeOldestDate={activeOldestDate} latestDate={latestDate} rawCount={windowRows.length} selectedWindow={selectedWindow} onWindowChange={onSelectedWindowChange} />
      {stats && <SummaryCards selectedWindow={selectedWindow} stats={stats} />}
      <WinRatePanel rows={visibleData} />
      <TrackingFilters filteredCount={filtered.length} market={market} onlyAI={onlyAI} search={search} sortBy={sortBy} sortOrder={sortOrder} visibleCount={visibleData.length} onOnlyAIChange={onOnlyAIChange} onSearchChange={onSearchChange} onSortByChange={onSortByChange} onSortOrderChange={onSortOrderChange} />
      <TrackingTable rows={filtered} sortBy={sortBy} sortOrder={sortOrder} onSortChange={handleSort} market={market} />
    </>
  )
}

function MarketTabs({ market, onMarketChange }: { market: MarketTab; onMarketChange: (m: MarketTab) => void }) {
  const { t } = usePreferences()
  const tabs: { key: MarketTab; label: string }[] = [
    { key: 'cn', label: t('tracking.tabCN') },
    { key: 'us', label: t('tracking.tabUS') },
    { key: 'hk', label: t('tracking.tabHK') },
  ]
  return (
    <div className="mb-4 flex gap-1 rounded-lg border border-border p-1 w-fit">
      {tabs.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => onMarketChange(key)}
          className={`rounded-md px-3 py-1 text-sm font-medium transition-colors ${market === key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted'}`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function BetaLocked() {
  const { t } = usePreferences()
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <div className="mb-3 text-4xl">🔒</div>
      <p className="text-muted-foreground">{t('tracking.betaLocked')}</p>
    </div>
  )
}

function TrackingHeader({ latestDate, oldestDate }: { latestDate: number | null; oldestDate: number | null }) {
  const { t } = usePreferences()

  return (
    <div className="mb-5">
      <h1 className="text-xl font-semibold">{t('tracking.title')}</h1>
      <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
        {t('tracking.description')}
        {latestDate && oldestDate && (
          <span className="ml-1">
            {t('tracking.range', { oldest: formatDate(oldestDate), latest: formatDate(latestDate) })}
          </span>
        )}
      </p>
    </div>
  )
}

function DateWindowFilter({
  activeDateCount,
  activeOldestDate,
  latestDate,
  rawCount,
  selectedWindow,
  onWindowChange,
}: {
  activeDateCount: number
  activeOldestDate: number | null
  latestDate: number | null
  rawCount: number
  selectedWindow: RecommendationWindow
  onWindowChange: (value: RecommendationWindow) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">{t('tracking.window')}</span>
        <select
          value={selectedWindow}
          onChange={(event) => onWindowChange(Number(event.target.value) as RecommendationWindow)}
          className="rounded-lg border border-border px-2 py-1.5 text-sm"
        >
          {AVG_WINDOWS.map((size) => (
            <option key={size} value={size}>
              {t('tracking.windowOption', { size })}
            </option>
          ))}
        </select>
      </label>
      {latestDate && activeOldestDate && (
        <span className="text-xs text-muted-foreground">
          {t('tracking.currentWindow', {
            oldest: formatDate(activeOldestDate),
            latest: formatDate(latestDate),
            count: activeDateCount,
            rows: rawCount,
          })}
        </span>
      )}
    </div>
  )
}

function SummaryCards({ selectedWindow, stats }: { selectedWindow: RecommendationWindow; stats: SummaryStats }) {
  const { t } = usePreferences()

  return (
    <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-5">
      <StatCard label={t('tracking.coveredStocks')} value={`${stats.count} ${t('common.stocks')}`} />
      <StatCard label={t('tracking.avgChange', { size: selectedWindow })} value={formatPct(stats.avg)} color={pctColor(stats.avg)} />
      <StatCard label={t('tracking.bestChange')} value={formatPct(stats.best)} color={pctColor(stats.best)} />
      <StatCard label={t('tracking.worstChange')} value={formatPct(stats.worst)} color={pctColor(stats.worst)} />
      <StatCard label={t('tracking.totalRecommendations')} value={`${stats.totalRecommendations} ${t('tracking.times')}`} />
    </div>
  )
}

function TrackingFilters({
  filteredCount,
  market,
  onlyAI,
  search,
  sortBy,
  sortOrder,
  visibleCount,
  onOnlyAIChange,
  onSearchChange,
  onSortByChange,
  onSortOrderChange,
}: {
  filteredCount: number
  market: MarketTab
  onlyAI: boolean
  search: string
  sortBy: SortBy
  sortOrder: SortOrder
  visibleCount: number
  onOnlyAIChange: (value: boolean) => void
  onSearchChange: (value: string) => void
  onSortByChange: (value: SortBy) => void
  onSortOrderChange: (value: SortOrder) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="mb-4 flex items-center gap-3">
      <input
        type="text"
        value={search}
        onChange={(event) => onSearchChange(event.target.value)}
        placeholder={t('tracking.searchPlaceholder')}
        className="rounded-lg border border-border px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring/20"
      />
      <label className="flex items-center gap-1.5 text-sm">
        <input
          type="checkbox"
          checked={onlyAI}
          onChange={(event) => onOnlyAIChange(event.target.checked)}
          className="rounded"
        />
        {t('tracking.onlyAI')}
      </label>
      <TrackingSortControls
        market={market}
        sortBy={sortBy}
        sortOrder={sortOrder}
        onSortByChange={onSortByChange}
        onSortOrderChange={onSortOrderChange}
      />
      <span className="text-xs text-muted-foreground">
        {filteredCount} / {visibleCount} {t('common.stocks')}
      </span>
    </div>
  )
}

function TrackingSortControls({
  market,
  sortBy,
  sortOrder,
  onSortByChange,
  onSortOrderChange,
}: {
  market: MarketTab
  sortBy: SortBy
  sortOrder: SortOrder
  onSortByChange: (value: SortBy) => void
  onSortOrderChange: (value: SortOrder) => void
}) {
  const { t } = usePreferences()
  return (
    <>
      <select value={sortBy} onChange={(event) => onSortByChange(event.target.value as SortBy)} className="rounded-lg border border-border px-2 py-1.5 text-sm">
        <option value="date">{t('tracking.sortDate')}</option>
        <option value="change">{t('tracking.sortChange')}</option>
        {market === 'us' && <option value="mfe">{t('tracking.sortMfe')}</option>}
        {market === 'us' && <option value="mae">{t('tracking.sortMae')}</option>}
        <option value="score">{t('tracking.sortScore')}</option>
        <option value="count">{t('tracking.sortRecommendCount')}</option>
      </select>
      <select value={sortOrder} onChange={(event) => onSortOrderChange(event.target.value as SortOrder)} className="rounded-lg border border-border px-2 py-1.5 text-sm">
        <option value="desc">{t('tracking.sortDesc')}</option>
        <option value="asc">{t('tracking.sortAsc')}</option>
      </select>
    </>
  )
}

function TrackingTable({
  rows,
  sortBy,
  sortOrder,
  onSortChange,
  market = 'cn',
}: {
  rows: Recommendation[]
  sortBy: SortBy
  market?: MarketTab
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <TrackingTableHead market={market} sortBy={sortBy} sortOrder={sortOrder} onSortChange={onSortChange} />
          <tbody style={{ contentVisibility: 'auto', containIntrinsicSize: '0 40000px' }}>
            {rows.length === 0 ? (
              <tr className="border-t border-border">
                <td colSpan={market === 'us' ? 11 : 8} className="px-3 py-8 text-center text-muted-foreground">
                  {t('tracking.empty')}
                </td>
              </tr>
            ) : (
              rows.map((row) => <TrackingRow key={`${row.code}-${row.recommend_date}`} row={row} market={market} />)
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function TrackingTableHead({
  market,
  sortBy,
  sortOrder,
  onSortChange,
}: {
  market: MarketTab
  sortBy: SortBy
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()
  return (
    <thead className="sticky top-0 bg-muted/80 backdrop-blur">
      <tr>
        <th className="px-3 py-2 text-left font-medium">{t('common.code')}</th>
        <th className="px-3 py-2 text-left font-medium">{t('common.name')}</th>
        <SortableHeader align="right" active={sortBy === 'date'} label={t('tracking.recommendDate')} order={sortOrder} onClick={() => onSortChange('date')} />
        <SortableHeader align="right" active={sortBy === 'count'} label={t('tracking.recommendCount')} order={sortOrder} onClick={() => onSortChange('count')} />
        <th className="px-3 py-2 text-right font-medium">{t('tracking.initialPrice')}</th>
        <th className="px-3 py-2 text-right font-medium">{t('tracking.currentPrice')}</th>
        <SortableHeader align="right" active={sortBy === 'change'} label={t('tracking.changePct')} order={sortOrder} onClick={() => onSortChange('change')} />
        <SortableHeader align="right" active={sortBy === 'score'} label={t('tracking.score')} order={sortOrder} onClick={() => onSortChange('score')} />
        {market === 'us' && <UsPerformanceHeaders sortBy={sortBy} sortOrder={sortOrder} onSortChange={onSortChange} />}
        <th className="px-3 py-2 text-center font-medium">AI</th>
      </tr>
    </thead>
  )
}

function UsPerformanceHeaders({
  sortBy,
  sortOrder,
  onSortChange,
}: {
  sortBy: SortBy
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()
  return (
    <>
      <SortableHeader align="right" active={sortBy === 'mfe'} label={t('tracking.mfePct')} order={sortOrder} onClick={() => onSortChange('mfe')} />
      <SortableHeader align="right" active={sortBy === 'mae'} label={t('tracking.maePct')} order={sortOrder} onClick={() => onSortChange('mae')} />
      <th className="px-3 py-2 text-right font-medium">{t('tracking.rangeAmpPct')}</th>
    </>
  )
}

function SortableHeader({
  active,
  align,
  label,
  order,
  onClick,
}: {
  active: boolean
  align: 'left' | 'right'
  label: string
  order: SortOrder
  onClick: () => void
}) {
  return (
    <th className={`px-3 py-2 font-medium ${align === 'right' ? 'text-right' : 'text-left'}`}>
      <button
        type="button"
        onClick={onClick}
        className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-muted"
      >
        <span>{label}</span>
        <span className={`text-[10px] ${active ? 'text-primary' : 'text-muted-foreground'}`}>
          {active ? order.toUpperCase() : '--'}
        </span>
      </button>
    </th>
  )
}

function TrackingRow({ row, market = 'cn' }: { row: Recommendation; market?: MarketTab }) {
  const { t } = usePreferences()
  const vetoed = row.rag_vetoed
  const rowCls = vetoed ? 'border-t border-border hover:bg-muted/20 opacity-60 line-through' : 'border-t border-border hover:bg-muted/20'
  const codeDisplay = market === 'cn' ? String(row.code).padStart(6, '0') : String(row.code)
  const scoreKind = trackingScoreKind(row)
  return (
    <tr className={rowCls}>
      <td className="px-3 py-2 font-mono">
        {codeDisplay}
        {vetoed && <span className="ml-1 inline-block h-2 w-2 rounded-full bg-red-500" title="RAG veto" />}
      </td>
      <td className="px-3 py-2">{row.name || '-'}</td>
      <td className="px-3 py-2 text-right text-muted-foreground">{formatDate(row.recommend_date)}</td>
      <td className="px-3 py-2 text-right font-medium">{recommendationCount(row.recommend_count)}</td>
      <td className="px-3 py-2 text-right">{row.initial_price?.toFixed(2) || '-'}</td>
      <td className="px-3 py-2 text-right">{row.current_price?.toFixed(2) || '-'}</td>
      <td className={`px-3 py-2 text-right font-medium ${pctColor(row.change_pct)}`}>{formatPct(row.change_pct)}</td>
      <td className="px-3 py-2 text-right">
        <div className="flex flex-col items-end gap-0.5">
          <span>{formatScore(row.funnel_score)}</span>
          {scoreKind && (
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] leading-none text-muted-foreground">
              {scoreKind === 'priority' ? t('tracking.scorePriority') : t('tracking.scoreRaw')}
            </span>
          )}
        </div>
      </td>
      {market === 'us' && <UsPerformanceCells row={row} />}
      <td className="px-3 py-2 text-center">
        {row.is_ai_recommended && <span className="inline-block h-2 w-2 rounded-full bg-indigo-500" />}
      </td>
    </tr>
  )
}

function trackingScoreKind(row: Recommendation): 'priority' | 'raw' | null {
  if (!isFiniteNumber(row.funnel_score)) return null
  const reason = row.recommend_reason ?? ''
  if (row.funnel_score >= 20) return 'priority'
  if (row.funnel_score >= 10 && (reason.includes('点火破局') || reason.includes('吸筹通道'))) {
    return 'priority'
  }
  return 'raw'
}

function UsPerformanceCells({ row }: { row: Recommendation }) {
  return (
    <>
      <td className={`px-3 py-2 text-right font-medium ${pctColor(row.mfe_pct ?? null)}`}>
        {formatPct(row.mfe_pct ?? null)}
      </td>
      <td className={`px-3 py-2 text-right font-medium ${pctColor(row.mae_pct ?? null)}`}>
        {formatPct(row.mae_pct ?? null)}
      </td>
      <td className="px-3 py-2 text-right text-muted-foreground">{formatPct(row.range_amp_pct ?? null)}</td>
    </>
  )
}

function WinRatePanel({ rows }: { rows: Recommendation[] }) {
  const { t } = usePreferences()
  const values = useMemo(() => rows.map((r) => r.change_pct).filter(isFiniteNumber), [rows])
  if (values.length < 3) return null
  const wins = values.filter((v) => v > 0)
  const losses = values.filter((v) => v <= 0)
  const winRate = (wins.length / values.length) * 100
  const avgWin = wins.length > 0 ? wins.reduce((a, b) => a + b, 0) / wins.length : 0
  const avgLoss = losses.length > 0 ? losses.reduce((a, b) => a + b, 0) / losses.length : 0
  return (
    <div className="mb-5 space-y-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label={t('tracking.winRate')} value={`${winRate.toFixed(1)}%`} color={winRate >= 50 ? 'text-up' : 'text-down'} />
        <StatCard label={t('tracking.avgWin')} value={formatPct(avgWin)} color="text-up" />
        <StatCard label={t('tracking.avgLoss')} value={formatPct(avgLoss)} color="text-down" />
        <StatCard label={t('tracking.profitFactor')} value={avgLoss !== 0 ? Math.abs(avgWin / avgLoss).toFixed(2) : '--'} />
      </div>
      <ReturnHistogram values={values} />
    </div>
  )
}

function ReturnHistogram({ values }: { values: number[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const buckets = useMemo(() => buildReturnBuckets(values), [values])
  useEffect(() => {
    if (!containerRef.current || buckets.length === 0) return
    const isDark = document.documentElement.classList.contains('dark')
    const chart = createChart(containerRef.current, {
      height: 140,
      layout: { background: { color: isDark ? '#0f172a' : '#ffffff' }, textColor: isDark ? '#94a3b8' : '#6b7194', fontSize: 10 },
      grid: { vertLines: { visible: false }, horzLines: { color: isDark ? '#202938' : '#eef1f6' } },
      rightPriceScale: { visible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
    })
    const series = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false })
    series.setData(
      buckets.map((b, i) => ({ time: (2020 * 10000 + 101 + i) as unknown as Time, value: b.count, color: b.midPct >= 0 ? '#ef4444a0' : '#10b981a0' })),
    )
    chart.timeScale().fitContent()
    const resize = () => { if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth }) }
    window.addEventListener('resize', resize)
    resize()
    return () => { window.removeEventListener('resize', resize); chart.remove() }
  }, [buckets])
  return (
    <div>
      <div className="mb-1 text-[11px] text-muted-foreground">收益分布</div>
      <div ref={containerRef} className="w-full overflow-hidden rounded-lg border border-border" />
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
        {buckets.length > 0 && <span>{buckets[0]!.midPct.toFixed(0)}%</span>}
        <span>0%</span>
        {buckets.length > 0 && <span>{buckets[buckets.length - 1]!.midPct.toFixed(0)}%</span>}
      </div>
    </div>
  )
}

function buildReturnBuckets(values: number[]): { midPct: number; count: number }[] {
  if (values.length === 0) return []
  const min = Math.min(...values), max = Math.max(...values)
  const step = Math.max((max - min) / 15, 1)
  const buckets: { midPct: number; count: number }[] = []
  for (let lo = Math.floor(min / step) * step; lo < max + step; lo += step) {
    buckets.push({ midPct: lo + step / 2, count: values.filter((v) => v >= lo && v < lo + step).length })
  }
  return buckets
}

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${color || ''}`}>{value}</div>
    </div>
  )
}

function getLatestRecommendDates(rows: Recommendation[], limit: number): number[] {
  const dates = rows
    .map((row) => row.recommend_date)
    .filter((date) => Number.isFinite(date) && date > 0)
  return [...new Set(dates)].sort((a, b) => b - a).slice(0, limit)
}

function dedupeRecommendations(rows: Recommendation[]): Recommendation[] {
  const sortedRows = [...rows].sort((a, b) => b.recommend_date - a.recommend_date)
  const byCode = new Map<number | string, Recommendation>()
  for (const row of sortedRows) {
    const existing = byCode.get(row.code)
    if (!existing) {
      byCode.set(row.code, {
        ...row,
        recommend_count: recommendationCount(row.recommend_count),
      })
      continue
    }
    existing.is_ai_recommended = existing.is_ai_recommended || row.is_ai_recommended
    existing.rag_vetoed = existing.rag_vetoed || row.rag_vetoed
    existing.recommend_count = Math.max(
      recommendationCount(existing.recommend_count),
      recommendationCount(row.recommend_count),
    )
  }
  return [...byCode.values()]
}

function buildSummaryStats(rows: Recommendation[]): SummaryStats | null {
  if (rows.length === 0) return null
  const totalRecommendations = rows.reduce((total, row) => total + recommendationCount(row.recommend_count), 0)
  const activeRows = rows.filter((row) => !row.rag_vetoed)
  const values = activeRows.map((row) => row.change_pct).filter(isFiniteNumber)
  if (values.length === 0) {
    return { count: rows.length, avg: null, best: null, worst: null, totalRecommendations }
  }
  const sum = values.reduce((total, value) => total + value, 0)
  return {
    count: rows.length,
    avg: sum / values.length,
    best: Math.max(...values),
    worst: Math.min(...values),
    totalRecommendations,
  }
}

function recommendationCount(value: number | null | undefined): number {
  return isFiniteNumber(value) && value > 0 ? Math.trunc(value) : 1
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function sortRecommendations(rows: Recommendation[], sortBy: SortBy, sortOrder: SortOrder): Recommendation[] {
  const direction = sortOrder === 'desc' ? 1 : -1
  return [...rows].sort((a, b) => {
    if (sortBy === 'date') return nullableNumberCompare(a.recommend_date, b.recommend_date, direction)
    if (sortBy === 'change') return nullableNumberCompare(a.change_pct, b.change_pct, direction)
    if (sortBy === 'mfe') return nullableNumberCompare(a.mfe_pct, b.mfe_pct, direction)
    if (sortBy === 'mae') return nullableNumberCompare(a.mae_pct, b.mae_pct, direction)
    if (sortBy === 'count') return nullableNumberCompare(recommendationCount(a.recommend_count), recommendationCount(b.recommend_count), direction)
    return nullableNumberCompare(a.funnel_score, b.funnel_score, direction)
  })
}

function nullableNumberCompare(a: number | null | undefined, b: number | null | undefined, direction: number): number {
  if (isFiniteNumber(a) && isFiniteNumber(b)) return (b - a) * direction
  if (isFiniteNumber(a)) return -1
  if (isFiniteNumber(b)) return 1
  return 0
}

function formatPct(value: number | null): string {
  if (!isFiniteNumber(value)) return '-'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function formatScore(value: number | null): string {
  if (!isFiniteNumber(value)) return '-'
  return value >= 10 ? value.toFixed(1) : value.toFixed(2)
}

function pctColor(value: number | null): string {
  if (!isFiniteNumber(value)) return 'text-muted-foreground'
  return value >= 0 ? 'text-up' : 'text-down'
}

function formatDate(d: number): string {
  const s = String(d)
  if (s.length !== 8) return s
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`
}
