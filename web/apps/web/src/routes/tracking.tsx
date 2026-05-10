import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { supabase } from '@/lib/supabase'
import { WyckoffLoading } from '@/components/loading'
import { usePreferences } from '@/lib/preferences'

interface Recommendation {
  code: number
  name: string | null
  recommend_date: number
  initial_price: number | null
  current_price: number | null
  change_pct: number | null
  is_ai_recommended: boolean
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

const RETENTION_DATES = 30
const AVG_WINDOWS = [5, 10, 15, 20, 25, 30] as const
type RecommendationWindow = (typeof AVG_WINDOWS)[number]
type SortBy = 'date' | 'change' | 'score'
type SortOrder = 'desc' | 'asc'

async function fetchTracking(): Promise<Recommendation[]> {
  const { data } = await supabase
    .from('recommendation_tracking')
    .select('*')
    .order('recommend_date', { ascending: false })
    .limit(2000)
  return data || []
}

export function TrackingPage() {
  const [search, setSearch] = useState('')
  const [onlyAI, setOnlyAI] = useState(false)
  const [sortBy, setSortBy] = useState<SortBy>('date')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')
  const [selectedWindow, setSelectedWindow] = useState<RecommendationWindow>(30)

  const { data = [], isLoading: loading } = useQuery({
    queryKey: ['tracking'],
    queryFn: fetchTracking,
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

  if (loading) {
    return <WyckoffLoading />
  }

  return (
    <div className="flex h-full flex-col p-6">
      <TrackingHeader latestDate={latestDate} oldestDate={oldestDate} />
      <DateWindowFilter
        activeDateCount={activeDates.length}
        activeOldestDate={activeOldestDate}
        latestDate={latestDate}
        rawCount={windowRows.length}
        selectedWindow={selectedWindow}
        onWindowChange={setSelectedWindow}
      />
      {stats && <SummaryCards selectedWindow={selectedWindow} stats={stats} />}
      <TrackingFilters
        filteredCount={filtered.length}
        onlyAI={onlyAI}
        search={search}
        sortBy={sortBy}
        sortOrder={sortOrder}
        visibleCount={visibleData.length}
        onOnlyAIChange={setOnlyAI}
        onSearchChange={setSearch}
        onSortByChange={setSortBy}
        onSortOrderChange={setSortOrder}
      />
      <TrackingTable
        rows={filtered}
        sortBy={sortBy}
        sortOrder={sortOrder}
        onSortChange={(nextSortBy) => {
          if (nextSortBy === sortBy) {
            setSortOrder(sortOrder === 'desc' ? 'asc' : 'desc')
            return
          }
          setSortBy(nextSortBy)
          setSortOrder('desc')
        }}
      />
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
      <select
        value={sortBy}
        onChange={(event) => onSortByChange(event.target.value as SortBy)}
        className="rounded-lg border border-border px-2 py-1.5 text-sm"
      >
        <option value="date">{t('tracking.sortDate')}</option>
        <option value="change">{t('tracking.sortChange')}</option>
        <option value="score">{t('tracking.sortScore')}</option>
      </select>
      <select
        value={sortOrder}
        onChange={(event) => onSortOrderChange(event.target.value as SortOrder)}
        className="rounded-lg border border-border px-2 py-1.5 text-sm"
      >
        <option value="desc">{t('tracking.sortDesc')}</option>
        <option value="asc">{t('tracking.sortAsc')}</option>
      </select>
      <span className="text-xs text-muted-foreground">
        {filteredCount} / {visibleCount} {t('common.stocks')}
      </span>
    </div>
  )
}

function TrackingTable({
  rows,
  sortBy,
  sortOrder,
  onSortChange,
}: {
  rows: Recommendation[]
  sortBy: SortBy
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-border">
      <div className="h-full overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-muted/80 backdrop-blur">
            <tr>
              <th className="px-3 py-2 text-left font-medium">{t('common.code')}</th>
              <th className="px-3 py-2 text-left font-medium">{t('common.name')}</th>
              <SortableHeader
                align="right"
                active={sortBy === 'date'}
                label={t('tracking.recommendDate')}
                order={sortOrder}
                onClick={() => onSortChange('date')}
              />
              <th className="px-3 py-2 text-right font-medium">{t('tracking.initialPrice')}</th>
              <th className="px-3 py-2 text-right font-medium">{t('tracking.currentPrice')}</th>
              <SortableHeader
                align="right"
                active={sortBy === 'change'}
                label={t('tracking.changePct')}
                order={sortOrder}
                onClick={() => onSortChange('change')}
              />
              <SortableHeader
                align="right"
                active={sortBy === 'score'}
                label={t('tracking.score')}
                order={sortOrder}
                onClick={() => onSortChange('score')}
              />
              <th className="px-3 py-2 text-center font-medium">AI</th>
            </tr>
          </thead>
          <tbody style={{ contentVisibility: 'auto', containIntrinsicSize: '0 40000px' }}>
            {rows.length === 0 ? (
              <tr className="border-t border-border">
                <td colSpan={8} className="px-3 py-8 text-center text-muted-foreground">
                  {t('tracking.empty')}
                </td>
              </tr>
            ) : (
              rows.map((row, index) => <TrackingRow key={`${row.code}-${row.recommend_date}-${index}`} row={row} />)
            )}
          </tbody>
        </table>
      </div>
    </div>
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

function TrackingRow({ row }: { row: Recommendation }) {
  return (
    <tr className="border-t border-border hover:bg-muted/20">
      <td className="px-3 py-2 font-mono">{String(row.code).padStart(6, '0')}</td>
      <td className="px-3 py-2">{row.name || '-'}</td>
      <td className="px-3 py-2 text-right text-muted-foreground">{formatDate(row.recommend_date)}</td>
      <td className="px-3 py-2 text-right">{row.initial_price?.toFixed(2) || '-'}</td>
      <td className="px-3 py-2 text-right">{row.current_price?.toFixed(2) || '-'}</td>
      <td className={`px-3 py-2 text-right font-medium ${pctColor(row.change_pct)}`}>{formatPct(row.change_pct)}</td>
      <td className="px-3 py-2 text-right">{row.funnel_score?.toFixed(1) || '-'}</td>
      <td className="px-3 py-2 text-center">
        {row.is_ai_recommended && <span className="inline-block h-2 w-2 rounded-full bg-indigo-500" />}
      </td>
    </tr>
  )
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
  const byCode = new Map<number, Recommendation>()
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
  const values = rows.map((row) => row.change_pct).filter(isFiniteNumber)
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

function pctColor(value: number | null): string {
  if (!isFiniteNumber(value)) return 'text-muted-foreground'
  return value >= 0 ? 'text-up' : 'text-down'
}

function formatDate(d: number): string {
  const s = String(d)
  if (s.length !== 8) return s
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`
}
