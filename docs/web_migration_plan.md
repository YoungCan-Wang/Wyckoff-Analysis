# Wyckoff Web 基建迭代计划（历史路线图）

> 更新于 2026-05-04 | 当前状态：React Web 已承担主要交互入口；本文保留迁移路线与后续方向，不作为当前架构事实源。当前事实请看 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 当前架构

```
React SPA (Cloudflare Pages)              wyckoff-analysis.pages.dev
  ├─ Vercel AI SDK (generateText + tools)
  ├─ Supabase JS SDK (Auth + DB)
  │
  │  POST /api/llm-proxy/chat/completions
  │  Headers: { X-Target-URL: "https://provider.api/v1" }
  ↓
Pages Functions (边缘计算)                 同域 /api/*
  └─ [[path]].ts                          透明代理，转发到 LLM 供应商
      ├─ 读取 X-Target-URL → 目标地址
      ├─ 清洗 CF 内部 headers
      ├─ 转发 request body (streaming)
      └─ 回传 response (含 SSE stream)
  ↓
LLM Providers (外部)
  ├─ 1Route (https://www.1route.dev/v1)
  ├─ DeepSeek (https://api.deepseek.com/v1)
  ├─ OpenAI / Gemini / 智谱 / 通义千问 / 火山引擎
  ↓
Supabase (Auth + PostgreSQL + RLS)        yfyivczvmorpqdyehfmn.supabase.co
```

已完成：
- React + Vite + shadcn/ui 前端，部署到 Cloudflare Pages
- Supabase Auth 登录/注册
- MarketBar 大盘水温组件（读 market_signal_daily）
- Git push 自动触发构建部署
- **Pages Functions /api/llm-proxy** — 解决 LLM 跨域 + 密钥不暴露
- **读盘室 AI Agent** — Vercel AI SDK + 10 个工具（搜索/持仓/大盘/诊断/选股/研报/策略）
- **compatibility 模式** — 兼容 1Route/DeepSeek/通义千问等第三方 OpenAI 接口
- **reasoning_content 回传** — 支持 DeepSeek R1 thinking 模式多步调用
- **读盘室模型快捷切换** — 无需跳转设置页

## 目标架构

```
React SPA (Cloudflare Pages)
  │ useChat() / fetch()
  ↓
Cloudflare Worker (Hono)
  ├─ /api/chat          → SSE streaming (Vercel AI SDK + tool calling)
  ├─ /api/portfolio/*   → D1 CRUD
  ├─ /api/settings/*    → KV read/write
  ├─ /api/market/*      → TickFlow API proxy + R2 缓存
  └─ /api/export/*      → TickFlow + R2 缓存
  ↓
Cloudflare D1 (主数据库)  ←  替代 Supabase PostgreSQL
Cloudflare KV (配置缓存)
Cloudflare R2 (文件/K线缓存)
Supabase Auth (仅保留登录认证)
```

## Cloudflare 存储服务对比

| 服务 | 类型 | 适合场景 | 免费额度 | 本项目用途 |
|------|------|----------|----------|-----------|
| **D1** | SQLite 关系数据库 | 结构化数据、SQL 查询 | 5GB 存储，500 万次读/天，10 万次写/天 | 持仓、推荐记录、聊天历史、funnel 结果 |
| **KV** | 全局键值存储 | 配置、缓存、高频读低频写 | 10 万次读/天，1000 次写/天，1GB 存储 | 用户设置、LLM 配置、session 缓存 |
| **R2** | 对象存储 (S3 兼容) | 文件、大对象、静态资源 | 10GB 存储，100 万次读/月，10 万次写/月 | K 线 OHLCV 缓存、导出 CSV、研报 PDF |
| **Workers** | 边缘计算 | API 路由、代理、业务逻辑 | 10 万次请求/天，10ms CPU | Hono API、Agent SSE、TickFlow 代理 |
| **Pages** | 静态网站托管 | SPA、SSG | 无限带宽，500 次构建/月 | React 前端（已部署） |

## 迁移路线

### Phase 1 — 前端上线 ✅

- [x] React + Vite + shadcn/ui 搭建
- [x] Supabase Auth 接入
- [x] MarketBar 组件
- [x] Cloudflare Pages 部署

### Phase 2 — Worker API + 数据页面

目标：把 Supabase DB 依赖降到最低，数据 CRUD 走 Worker

- [ ] Hono Worker 搭建 + wrangler 部署
- [ ] Auth 中间件（验证 Supabase JWT）
- [ ] Portfolio 页（持仓 CRUD）
- [ ] Settings 页（用户配置）
- [ ] Recommendation Tracking 页（推荐跟踪表格）

### Phase 3 — Cloudflare 存储迁移

目标：将高频数据从 Supabase 迁移到 Cloudflare 存储，降低 Supabase 用量

**D1 迁移（结构化数据）：**
- [ ] 创建 D1 数据库，定义 schema
- [ ] 迁移 `portfolio_holdings` 表 → D1
- [ ] 迁移 `chat_messages` 表 → D1（最大的存储消耗）
- [ ] 迁移 `funnel_results` 表 → D1
- [ ] 迁移 `recommendation_tracking` 表 → D1
- [ ] Worker API 改为读写 D1

**KV 迁移（配置类）：**
- [ ] 创建 KV namespace
- [ ] 迁移 `user_settings` → KV（key: `user:{uid}:settings`）
- [ ] LLM 配置缓存 → KV
- [ ] Session/token 缓存 → KV

**R2 缓存（大文件）：**
- [ ] 创建 R2 bucket
- [ ] K 线 OHLCV 数据缓存（TickFlow 回源 → R2 → 前端）
- [ ] 导出 CSV 暂存
- [ ] CLI cron 每日预热热门股票 K 线到 R2

### Phase 4 — Agent 对话

目标：Web 端完整 Agent 体验

- [ ] `/api/chat` SSE endpoint（Vercel AI SDK + tool calling）
- [ ] 5 个基础工具：search_stock, portfolio, market_overview, query_history, update_portfolio
- [ ] Chat UI（useChat hook + streaming + tool call 展示）
- [ ] analyze_stock 工具（TickFlow OHLCV + Wyckoff 判定）
- [ ] K 线图组件（TradingView Lightweight Charts）
- [ ] generate_ai_report / generate_strategy_decision 工具

### Phase 5 — 优化上线

- [ ] 自定义域名
- [ ] Supabase 降级：仅保留 Auth（PostgreSQL 可关闭或降到最低用量）
- [ ] 性能优化：R2 缓存命中率、D1 查询索引、KV 读取延迟
- [ ] 监控：Workers Analytics + 错误告警

## 迁移后 Supabase 用量预估

| 服务 | 迁移前 | 迁移后 |
|------|--------|--------|
| Auth | 登录认证 | 登录认证（保留） |
| Database | 全部表 ~500MB | 仅 Auth 相关表 ~5MB |
| Bandwidth | 高（前端直连） | 极低（仅 Auth 请求） |

## 成本估算

| 阶段 | 月成本 |
|------|--------|
| 当前（Supabase 全量） | 接近免费额度上限 |
| Phase 3 完成后 | ¥0（全部 Cloudflare 免费层 + Supabase Auth 免费） |
| 规模期（1000+ 用户） | ~$5/月（D1 付费层 $0.75/GB，按需） |
