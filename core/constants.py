# Supabase 内置 anon 凭据（公开客户端密钥，安全由 RLS 保证）
SUPABASE_ANON_URL = "https://yfyivczvmorpqdyehfmn.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlmeWl2Y3p2bW9ycHFkeWVoZm1uIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njg4MjQ0NTIsImV4cCI6MjA4NDQwMDQ1Mn0."
    "1kGvwz6zCHy49ch8Nc5OearbP_r4b6Z_02_t-Vu6KTs"
)

# Database Table Names
TABLE_USER_SETTINGS = "user_settings"
TABLE_STOCK_HIST_CACHE = "stock_hist_cache"
TABLE_MARKET_SIGNAL_DAILY = "market_signal_daily"
TABLE_RECOMMENDATION_TRACKING = "recommendation_tracking"
TABLE_SIGNAL_PENDING = "signal_pending"
TABLE_PORTFOLIOS = "portfolios"
TABLE_PORTFOLIO_POSITIONS = "portfolio_positions"
TABLE_TRADE_ORDERS = "trade_orders"
TABLE_DAILY_NAV = "daily_nav"
TABLE_TAIL_BUY_HISTORY = "tail_buy_history"

# Local SQLite DB path
from pathlib import Path as _Path

LOCAL_DB_PATH = _Path.home() / ".wyckoff" / "wyckoff.db"
