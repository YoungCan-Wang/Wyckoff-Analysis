import { Outlet, NavLink, useNavigate } from 'react-router'
import { MessageSquare, Briefcase, TrendingUp, Settings, LogOut, BarChart3, Moon, FileDown, BookOpen, Home, Github, Filter } from 'lucide-react'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'
import { MarketBar } from '@/components/market-bar'

const navItems = [
  { to: '/chat', icon: MessageSquare, label: '读盘室' },
  { to: '/analysis', icon: BarChart3, label: '单股分析' },
  { to: '/screener', icon: Filter, label: '漏斗选股' },
  { to: '/portfolio', icon: Briefcase, label: '持仓' },
  { to: '/tracking', icon: TrendingUp, label: '跟踪' },
  { to: '/tail-buy', icon: Moon, label: '尾盘记录' },
  { to: '/export', icon: FileDown, label: '数据导出' },
  { to: '/guide', icon: BookOpen, label: '功能说明' },
  { to: '/settings', icon: Settings, label: '设置' },
]

const externalLinks = [
  { href: 'https://youngcan-wang.github.io/wyckoff-homepage/', icon: Home, label: '项目主页' },
  { href: 'https://github.com/YoungCan-Wang/Wyckoff-Analysis', icon: Github, label: 'GitHub' },
]

function SidebarFooter({ email, onLogout }: { email: string; onLogout: () => void }) {
  return (
    <div className="border-t border-border p-3">
      {externalLinks.map(({ href, icon: Icon, label }) => (
        <a
          key={href}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="mb-2 flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <Icon size={14} />
          {label}
        </a>
      ))}
      <div className="mb-2 truncate px-3 text-[11px] text-muted-foreground">{email}</div>
      <button
        onClick={onLogout}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <LogOut size={15} />
        退出
      </button>
    </div>
  )
}

export function AppLayout() {
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)

  async function handleLogout() {
    await supabase.auth.signOut()
    navigate('/login', { replace: true })
  }

  return (
    <div className="flex h-screen">
      <aside className="flex w-56 flex-col border-r border-border bg-sidebar">
        <div className="px-5 py-5">
          <h2 className="bg-gradient-to-r from-primary to-purple-500 bg-clip-text text-xl font-bold tracking-tight text-transparent">
            Wyckoff
          </h2>
          <p className="mt-0.5 text-[11px] text-muted-foreground">智能投研助手</p>
        </div>

        <nav className="flex-1 space-y-0.5 px-3">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-all ${
                  isActive
                    ? 'bg-primary/10 font-medium text-primary shadow-sm'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                }`
              }
            >
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>

        <SidebarFooter email={user?.email || 'dev@preview'} onLogout={handleLogout} />
      </aside>

      <div className="flex flex-1 flex-col overflow-hidden">
        <MarketBar />
        <main className="flex-1 overflow-auto bg-background">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
