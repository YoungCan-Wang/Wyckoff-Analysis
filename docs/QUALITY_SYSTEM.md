# 质量体系

确保任何开发者（人类或 AI 模型）都无法在不被拦截的情况下让代码质量劣化。

---

## 五层防线架构

### 总览

```
┌──────────────────────────────────────────────────────────────────┐
│  L5  Skills 语义层   │  LLM 驱动的语义审阅（冗余/架构/命名）    │
├──────────────────────────────────────────────────────────────────┤
│  L4  Harness 回归层  │  snapshot / golden-file 契约测试         │
├──────────────────────────────────────────────────────────────────┤
│  L3  CI 门禁层       │  ruff + quality_gate + tsc + pytest +    │
│                      │  vitest（PR/push 自动触发）               │
├──────────────────────────────────────────────────────────────────┤
│  L2  本地拦截层      │  pre-commit hooks（ruff + 函数长度）      │
├──────────────────────────────────────────────────────────────────┤
│  L1  规范层          │  AGENTS.md（所有 AI 模型 + 人类遵守）     │
└──────────────────────────────────────────────────────────────────┘
```

### 拦截时序流

```
开发者写代码
    │
    ├──── /pre-commit-review ────▶ L5 语义审阅（可选，人工触发）
    │
    ▼
┌─────────┐     ┌─────────────────────────────────────┐
│ git add │────▶│ L2 pre-commit 自动触发              │
└─────────┘     │  ├── ruff --fix (lint 自动修复)     │
                │  ├── ruff format (格式化)           │
                │  └── quality_gate --check-functions │
                │       ├── PASS → 允许 commit        │
                │       └── FAIL → 拒绝 commit        │
                └─────────────────────────────────────┘
                    │ (commit 成功)
                    ▼
              ┌─────────┐
              │ git push│
              └─────────┘
                    │
                    ▼
┌───────────────────────────────────────────────────────────┐
│ L3 GitHub Actions CI                                      │
│  ┌─────────────────────────┐  ┌────────────────────────┐ │
│  │ python job              │  │ web-check job          │ │
│  │  ├── ruff check        │  │  ├── tsc --noEmit      │ │
│  │  ├── ruff format --chk │  │  └── vitest run (L4)   │ │
│  │  ├── quality_gate --ci │  └────────────────────────┘ │
│  │  ├── py_compile        │                             │
│  │  └── pytest (含 L4)    │                             │
│  └─────────────────────────┘                             │
│       ├── ALL PASS → ✅ 允许合并                         │
│       └── ANY FAIL → ❌ 阻止合并                         │
└───────────────────────────────────────────────────────────┘
```

---

## L1 — 规范层

### 架构图

```
┌──────────────────────────────────────────────────┐
│              AGENTS.md (Single Source of Truth)   │
│  ┌──────────────────────────────────────────┐    │
│  │ 规则: ≤50行, 无死代码, pass ruff, ...    │    │
│  └──────────────────────────────────────────┘    │
└───────────┬──────────────┬───────────────┬───────┘
            │              │               │
            ▼              ▼               ▼
┌───────────────┐ ┌──────────────┐ ┌──────────────────────┐
│  CLAUDE.md    │ │ .cursorrules │ │ copilot-instructions │
│  (Claude)     │ │ (Cursor)     │ │ (GitHub Copilot)     │
└───────────────┘ └──────────────┘ └──────────────────────┘
            │              │               │
            ▼              ▼               ▼
     ┌─────────────────────────────────────────┐
     │  所有 AI 模型遵循统一规则开发代码        │
     └─────────────────────────────────────────┘
```

**文件**: `AGENTS.md`（项目根目录）

核心红线：
- 单函数/方法 ≤ 50 行
- 不留死代码、调试代码
- 注释只留终态
- 新增代码必须通过 `ruff check` 和 `ruff format --check`

---

## L2 — 本地拦截层

### 架构图

```
┌─────────────────────────────────────────────────────┐
│  .pre-commit-config.yaml                            │
│                                                     │
│  Hook 1: ruff (astral-sh/ruff-pre-commit)           │
│  ┌───────────────────────────────────────────┐      │
│  │ ruff check --fix  → 自动修复 lint 问题    │      │
│  │ ruff format       → 强制统一格式          │      │
│  └───────────────────────────────────────────┘      │
│                        │                            │
│                        ▼ PASS                       │
│  Hook 2: quality-gate (local)                       │
│  ┌───────────────────────────────────────────┐      │
│  │ scripts/quality_gate.py --check-functions │      │
│  │                                           │      │
│  │ 扫描 Python (ast) + TS (regex)            │      │
│  │      │                                    │      │
│  │      ├── 新函数 > 50行?  → ❌ FAIL         │      │
│  │      ├── 白名单函数变长? → ❌ FAIL         │      │
│  │      └── 全部 OK?        → ✅ PASS         │      │
│  └───────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

```bash
# 安装
pip install pre-commit && pre-commit install

# 手动运行
pre-commit run --all-files
```

---

## L3 — CI 门禁层

### 架构图

```
┌─────────────────────────────────────────────────────────────┐
│  .github/workflows/ci.yml                                   │
│  触发条件: push/PR to main (*.py, web/**, pyproject.toml)   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────── python job ───────────────────┐          │
│  │                                               │          │
│  │  ruff check .                                 │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  ruff format --check .                        │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  quality_gate --ci                            │          │
│  │  ┌──────────────────────────────────────┐     │          │
│  │  │ 函数长度检查 (硬卡口)               │     │          │
│  │  │ LOC 趋势对比 (>5% 告警)             │     │          │
│  │  └──────────────────────────────────────┘     │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  py_compile (编译检查)                        │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  pytest tests/ -x -q  ◀── 含 L4 harness      │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  daily_job --dry-run                          │          │
│  └───────────────────────────────────────────────┘          │
│                                                             │
│  ┌──────────────── web-check job ────────────────┐          │
│  │                                               │          │
│  │  pnpm install --frozen-lockfile               │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  tsc --noEmit (类型安全)                      │          │
│  │       │                                       │          │
│  │       ▼                                       │          │
│  │  vitest run   ◀── L4 harness (TS 侧)         │          │
│  └───────────────────────────────────────────────┘          │
│                                                             │
│  ALL PASS → ✅ PR 可合并                                    │
│  ANY FAIL → ❌ 阻止合并                                     │
└─────────────────────────────────────────────────────────────┘
```

---

## L4 — Harness 回归层

防止工具函数的输出格式和行为契约在重构时静默变化。

### Harness 总架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Harness 测试体系                              │
├──────────────────────────────┬──────────────────────────────────────┤
│   TypeScript 侧 (Vitest)    │    Python 侧 (pytest)               │
│                              │                                      │
│   chat-tools.test.ts         │    test_holding_diagnostic.py        │
│   ├── Snapshot 回归          │    ├── 功能验证                      │
│   ├── 契约测试               │    ├── Golden-file 回归              │
│   └── 副作用验证             │    └── 边界条件                      │
│                              │                                      │
│   Golden: __snapshots__/     │    Golden: tests/golden/*.txt        │
│   (Vitest 内置)              │    (自定义 assert_golden)            │
└──────────────────────────────┴──────────────────────────────────────┘
```

### TypeScript Harness 依赖注入架构

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   ┌─────────────────────────────────────────────────────┐        │
│   │              ToolDeps Interface                      │        │
│   │  {                                                  │        │
│   │    supabase: SupabaseClient                         │        │
│   │    fetch: typeof globalThis.fetch                   │        │
│   │    generateText: typeof ai.generateText             │        │
│   │  }                                                  │        │
│   └───────────────┬─────────────────────┬───────────────┘        │
│                   │                     │                        │
│        ┌──────────▼──────────┐  ┌───────▼───────────────┐        │
│        │   Production 模式   │  │    Test 模式 (Harness)│        │
│        │                     │  │                       │        │
│        │  supabase = 真实    │  │  supabase = Mock      │        │
│        │  fetch = 真实       │  │  fetch = vi.fn()      │        │
│        │  generateText = SDK │  │  generateText = stub  │        │
│        └──────────┬──────────┘  └───────┬───────────────┘        │
│                   │                     │                        │
│                   ▼                     ▼                        │
│   ┌───────────────────────────────────────────────────────┐      │
│   │          exec* 函数 (chat-tools.ts)                   │      │
│   │                                                       │      │
│   │  execSearchStock(deps, userId, query)                 │      │
│   │  execViewPortfolio(deps, userId)                      │      │
│   │  execMarketOverview(deps)                             │      │
│   │  execAnalyzeStock(deps, userId, config, model, ...)   │      │
│   │  ...（共 10 个）                                       │      │
│   └───────────────────────────────────────────────────────┘      │
│                   │                                              │
│                   ▼                                              │
│   ┌───────────────────────────────────────────────────────┐      │
│   │  输出: string (格式化文本)                             │      │
│   │                                                       │      │
│   │  ┌─────────────────────────────────────────────────┐  │      │
│   │  │  Snapshot 断言 (toMatchSnapshot)                 │  │      │
│   │  │  → 首次运行生成 .snap 文件                       │  │      │
│   │  │  → 后续运行自动对比                              │  │      │
│   │  │  → 输出变化 → 测试失败 → 开发者审核决定          │  │      │
│   │  └─────────────────────────────────────────────────┘  │      │
│   └───────────────────────────────────────────────────────┘      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### TypeScript 文件分层

```
chat-agent.ts (编排层 — 不直接测试)
│
│  构造 deps = { supabase, fetch, generateText }
│  构造 model = provider.chat(config.model)
│  注册 tool schema (zod) + 路由到 exec*
│
└──── imports ────▶ chat-tools.ts (可测试逻辑层)
                    │
                    ├── buildKlineDigest()     ← 纯函数，无 deps
                    ├── fetchTickFlowKey()     ← deps.supabase
                    ├── fetchKlineForAgent()   ← deps.fetch
                    ├── fetchQuotes()          ← deps.fetch
                    ├── execSearchStock()      ← deps.supabase + deps.fetch
                    ├── execViewPortfolio()    ← deps.supabase
                    ├── execMarketOverview()   ← deps.supabase
                    ├── execQueryRecommend..() ← deps.supabase
                    ├── execQueryTailBuy()     ← deps.supabase
                    ├── execExecutePortf...()  ← deps.supabase
                    ├── execAnalyzeStock()     ← deps.* (全部)
                    ├── execScreenStocks()     ← deps.supabase
                    ├── execGenerateAiReport() ← deps.* (全部)
                    └── execStrategyDecision() ← deps.* (全部)
```

### Python Harness Golden-file 流程

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│   make_ohlcv(n=250, trend="up", seed=1)                      │
│   (确定性合成数据 — 相同参数永远产出相同 DataFrame)           │
│        │                                                     │
│        ▼                                                     │
│   diagnose_one_stock(code, name, cost, df)                   │
│        │                                                     │
│        ▼                                                     │
│   format_diagnostic_text(diagnostic)                         │
│        │                                                     │
│        ▼                                                     │
│   ┌────────────────────────────────────────────┐             │
│   │ assert_golden("diagnostic_healthy.txt", text)│            │
│   └─────────────────────┬──────────────────────┘             │
│                         │                                    │
│             ┌───────────┴───────────┐                        │
│             │                       │                        │
│             ▼                       ▼                        │
│   ┌─────────────────┐    ┌──────────────────┐               │
│   │ 文件不存在       │    │ 文件已存在        │               │
│   │ → 创建 golden   │    │ → 逐字符对比      │               │
│   │ → pytest.skip   │    │   ├── 一致 → PASS │               │
│   └─────────────────┘    │   └── 不一致 →FAIL│               │
│                          └──────────────────┘               │
│                                    │                        │
│                                    ▼ (FAIL 时)              │
│                    开发者审核变更是否预期                      │
│                    ├── 预期 → rm golden/*.txt → 重新生成      │
│                    └── 非预期 → 修复代码中的回归              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### 测试覆盖矩阵

```
┌────────────────────────────────────────────────────────────────────┐
│                    测试类型 × 覆盖目标                              │
├──────────────────┬───────────┬───────────┬────────────┬───────────┤
│                  │ 输出格式  │ 行为正确  │ 不崩溃    │ 副作用    │
├──────────────────┼───────────┼───────────┼────────────┼───────────┤
│ buildKlineDigest │ Snapshot  │     -     │  空数组   │     -     │
│ execSearchStock  │     -     │ 含代码名称│  空结果   │     -     │
│ execViewPortfolio│     -     │ 含持仓数  │  空仓     │     -     │
│ execMarketOverview│    -     │ 含 regime │  空数据   │     -     │
│ execExecutePort..│     -     │ 含✅消息  │  缺参数   │ upsert ✓  │
│ diagnose_one_stock│    -     │ 健康评级  │  短数据   │     -     │
│ format_diagnostic│ Golden    │     -     │     -     │     -     │
└──────────────────┴───────────┴───────────┴────────────┴───────────┘
```

**位置**: `web/apps/web/src/lib/__tests__/chat-tools.test.ts`

**运行**:
```bash
cd web/apps/web && pnpm test
```

**更新 snapshot**:
```bash
cd web/apps/web && npx vitest run -u
```

### Python 侧

**位置**: `tests/test_holding_diagnostic.py`

**运行**:
```bash
pytest tests/test_holding_diagnostic.py -v
```

**更新 golden file**:
```bash
rm tests/golden/*.txt && pytest tests/test_holding_diagnostic.py -v
```

**辅助工具**:
- `tests/helpers/synthetic_data.py` — 确定性合成 OHLCV 数据生成器
- `tests/helpers/golden.py` — 轻量 golden-file 断言工具
- `tests/golden/` — golden 输出文件（提交到 git）

---

## L5 — Skills 语义审阅层

填补 L1（规范写了）和 L2（机器执行）之间的缝隙：那些 regex/AST 无法检测、但 LLM 能判断的语义质量问题。

### 架构图

```
┌───────────────────────────────────────────────────────────────┐
│  .claude/skills/ (项目级 Skill)                               │
│                                                               │
│  ┌─────────────────────────┐  ┌────────────────────────────┐ │
│  │ /pre-commit-review      │  │ /architecture-check        │ │
│  │                         │  │                            │ │
│  │ 审阅维度:               │  │ 校验维度:                  │ │
│  │ ├── 冗余代码            │  │ ├── Web 不加页面           │ │
│  │ ├── 死代码(语义级)      │  │ ├── Python 目录归属        │ │
│  │ ├── 命名语义            │  │ ├── 数据隔离 Route A       │ │
│  │ └── 啰嗦模式            │  │ └── ToolDeps 可测试性      │ │
│  └─────────────────────────┘  └────────────────────────────┘ │
│               │                            │                  │
│               ▼                            ▼                  │
│  ┌────────────────────────────────────────────────────────┐   │
│  │  LLM 语义理解 (Claude Code)                            │   │
│  │  读取 git diff → 按清单逐项判断 → 输出违规或通过       │   │
│  └────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────┘
```

### 与其他层的关系

```
L1 AGENTS.md ──── "写了规则"
      │
      │  L5 Skills 是 L1 的"可执行版本"
      │  把文字规范变成可调用的审查动作
      │
      ▼
L5 Skills ──── "LLM 理解并执行规则"
      │
      │  L2/L3/L4 是机械化自动执行
      │  L5 是按需人工触发的语义审查
      │
      ▼
L2-L4 自动化 ── "机器强制执行"
```

### Skill 清单

| Skill | 触发方式 | 覆盖范围 |
|-------|---------|---------|
| `/pre-commit-review` | 提交前手动调用 | 冗余、死代码、命名、啰嗦模式 |
| `/architecture-check` | 新增文件时调用 | 路由约束、目录归属、数据隔离、DI 模式 |

### 设计原则

1. **只做项目特有约束** — 通用审阅由系统 skill (`/simplify`, `/review`) 覆盖
2. **按需触发，不自动** — 避免打断心流、避免成本浪费
3. **不重复机械工具** — ruff/tsc 能查的不在 skill 里重复检查
4. **规则可追溯** — 每条审阅规则都对应 AGENTS.md 中的具体条款

### 文件位置

```
.claude/skills/
├── pre-commit-review.md    # 提交前语义审阅
└── architecture-check.md   # 架构约束校验
```

### 使用方式

```bash
# 在 Claude Code 中调用
/pre-commit-review          # 审阅当前 staged 变更
/architecture-check         # 校验新增文件的架构合规性
```

---

## 函数长度控制

| 机制 | 说明 |
|------|------|
| 软限制 | 新增函数 > 80 行 → CI WARNING（不阻断） |
| 白名单 | `.metrics/func_whitelist.json` 记录存量超标函数 |
| 趋势跟踪 | 白名单函数变更长时输出 WARNING |
| 基准更新 | `python scripts/quality_gate.py --snapshot` |

核心原则：不卡行数，卡冗余。每个函数、变量、抽象层必须有存在的理由。

---

## LOC 趋势监控

| 指标 | 位置 |
|------|------|
| 基准 | `.metrics/loc.json`（Python + TypeScript 分模块行数） |
| 阈值 | 总量增长 > 5% 时 CI 输出 WARNING |
| 更新 | `python scripts/quality_gate.py --snapshot` |

---

## 命令速查

```bash
# Python lint + format
ruff check . && ruff format --check .

# 函数长度检查
python scripts/quality_gate.py --check-functions

# 完整 CI 模式（函数 + LOC 趋势）
python scripts/quality_gate.py --ci

# 更新基准（当有意增长后）
python scripts/quality_gate.py --snapshot

# TypeScript 类型检查
cd web/apps/web && npx --package=typescript tsc --noEmit

# Web 测试（harness）
cd web/apps/web && pnpm test

# Python 测试
pytest tests/ -x -q

# Pre-commit（全量）
pre-commit run --all-files

# Claude Code Skills（语义审阅）
/pre-commit-review          # 提交前语义审阅
/architecture-check         # 架构约束校验
/simplify                   # 通用代码精简（系统skill）
```

---

## 新增工具的检查清单

当添加新的 agent tool 时：

1. 在 `chat-tools.ts` 中实现 `exec*` 函数（接受 `ToolDeps` 参数）
2. 在 `chat-agent.ts` 的 `buildTools` 中注册 tool schema
3. 在 `__tests__/chat-tools.test.ts` 中添加至少一个契约测试
4. 运行 `pnpm test` 确认通过
5. 运行 `python scripts/quality_gate.py --check-functions` 确认无超标
6. 如果涉及 Python 侧新模块，在 `tests/` 中添加对应测试
