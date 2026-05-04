import { BarChart3, Bot, Briefcase, Download, Filter, MessageSquare, Moon, RadioTower, Settings, TrendingUp } from 'lucide-react'

const workflows = [
  {
    icon: MessageSquare,
    title: '读盘室',
    desc: '用自然语言串联行情查询、单股诊断、漏斗选股、持仓检查和调仓方案。',
  },
  {
    icon: BarChart3,
    title: '单股分析',
    desc: '围绕 Wyckoff 阶段、量价结构、支撑压力、风险提示生成个股诊断。',
  },
  {
    icon: Filter,
    title: '漏斗选股',
    desc: '按市场水温、行业、形态、信号质量逐层筛选候选池，减少手工翻票。',
  },
  {
    icon: Briefcase,
    title: '持仓管理',
    desc: '集中查看现金、成本、仓位和止损信息，为后续跟踪和调仓提供上下文。',
  },
]

const tools = [
  { name: '市场水温', detail: '读取大盘、A50、VIX 与风险状态，作为所有判断的先验条件。' },
  { name: '推荐跟踪', detail: '沉淀 AI 推荐后的价格、状态、来源和后续跟踪结果。' },
  { name: '信号池', detail: '汇总待确认、已确认、过期、拒绝等信号，保留完整处理链路。' },
  { name: '尾盘记录', detail: '记录尾盘买入候选、规则分、优先级和 LLM 决策。' },
  { name: '数据导出', detail: '把推荐、信号、持仓、尾盘记录导出为后续复盘材料。' },
  { name: '模型设置', detail: '管理默认模型、备用模型、数据源 Key 和前端调用配置。' },
]

const playbooks = [
  {
    label: '盘前',
    icon: RadioTower,
    text: '先看市场水温，再筛候选池，避免在系统性风险较高时孤立看个股。',
  },
  {
    label: '盘中',
    icon: TrendingUp,
    text: '围绕重点股票做单股分析，用持仓和推荐跟踪校准风险暴露。',
  },
  {
    label: '尾盘',
    icon: Moon,
    text: '复查尾盘候选和信号池，把需要执行或观察的标的沉淀到记录里。',
  },
  {
    label: '复盘',
    icon: Download,
    text: '导出关键数据，结合读盘室对话形成下一次交易前的上下文。',
  },
]

export function FeatureGuidePage() {
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-8 p-6">
      <header className="border-b border-border pb-5">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Bot size={22} />
          </span>
          <div>
            <h1 className="text-xl font-semibold">功能说明</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Wyckoff 智能投研助手围绕读盘、选股、跟踪、持仓和复盘组织工作流。
            </p>
          </div>
        </div>
      </header>

      <section>
        <div className="mb-4 flex items-end justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold">核心功能</h2>
            <p className="mt-1 text-sm text-muted-foreground">从自然语言读盘到结构化数据沉淀。</p>
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {workflows.map(({ icon: Icon, title, desc }) => (
            <article key={title} className="rounded-lg border border-border bg-white p-4 shadow-sm shadow-primary/5">
              <Icon className="mb-3 text-primary" size={20} />
              <h3 className="text-sm font-semibold">{title}</h3>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">{desc}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1fr_380px]">
        <div>
          <h2 className="mb-4 text-base font-semibold">工具与数据模块</h2>
          <div className="overflow-hidden rounded-lg border border-border bg-white">
            <table className="w-full text-sm">
              <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">模块</th>
                  <th className="px-4 py-3 font-medium">说明</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {tools.map((tool) => (
                  <tr key={tool.name}>
                    <td className="whitespace-nowrap px-4 py-3 font-medium">{tool.name}</td>
                    <td className="px-4 py-3 leading-6 text-muted-foreground">{tool.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <aside>
          <h2 className="mb-4 text-base font-semibold">日常节奏</h2>
          <div className="space-y-3">
            {playbooks.map(({ icon: Icon, label, text }) => (
              <div key={label} className="rounded-lg border border-border bg-sidebar p-4">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Icon size={16} className="text-primary" />
                  {label}
                </div>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">{text}</p>
              </div>
            ))}
          </div>
        </aside>
      </section>

      <section className="rounded-lg border border-border bg-primary/5 p-5">
        <div className="flex items-start gap-3">
          <Settings className="mt-0.5 text-primary" size={18} />
          <div>
            <h2 className="text-sm font-semibold">配置入口</h2>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              首次使用前在设置页配置 Supabase、LLM 模型与数据源；生产环境里，信号、持仓和个人设置会按用户隔离。
            </p>
          </div>
        </div>
      </section>
    </div>
  )
}
