---
name: architecture-check
description: 校验架构约束 — 新增文件/模块是否符合项目架构规则（路由、目录、数据隔离）
user_invocable: true
---

# Architecture Check

校验当前变更是否符合项目架构约束。在新增文件、新增路由、新增模块时使用。

## 执行步骤

1. 运行 `git diff --cached --name-only --diff-filter=A` 获取新增文件
2. 若无 staged 变更，使用 `git diff HEAD --name-only --diff-filter=A`
3. 结合 `git diff` 内容，对每个新增/修改的文件进行架构合规检查

## 架构规则

### Rule 1: Web 端不加页面

**约束**: 新功能通过 Agent tool 实现，不增加新路由页面。

**检查方式**:
- `web/apps/web/src/routes/` 下是否有新增 `.tsx` 文件？
- `main.tsx` 或路由配置中是否注册了新 `<Route>`？
- 如果是，该功能是否确实无法通过 chat agent tool 实现？

**合规判断**:
- 新增路由 → 违规（除非有充分理由：如 OAuth callback）
- 新增 `chat-tools.ts` 中的 exec* 函数 → 合规

### Rule 2: Python 目录归属

**约束**: 每个目录有明确职责，新模块必须放对位置。

| 目录 | 职责 | 什么该放这里 |
|------|------|-------------|
| `core/` | 业务逻辑核心 | Wyckoff 引擎、策略、诊断 |
| `tools/` | 可复用工具函数 | 数据获取、排名、筛选 |
| `integrations/` | 外部系统对接 | Supabase、LLM、数据源 |
| `agents/` | Agent 逻辑 | Chat agent、session |
| `cli/` | CLI 界面 | 命令、TUI、provider |
| `pages/` | Streamlit 页面 | (遗留，不新增) |
| `scripts/` | 一次性/定时任务 | daily_job、backtest |
| `utils/` | 通用工具 | 日期、通知、helpers |

**检查方式**:
- 新 `.py` 文件所在目录是否匹配其职责？
- 是否在 `pages/` 下新增了文件？（应迁移到 agent）

### Rule 3: 数据隔离 (Route A)

**约束**: 信号数据共享，持仓/配置按用户隔离。

**检查方式**:
- 新增 Supabase 查询是否带 `user_id` 过滤？（持仓/配置表必须带）
- 信号/推荐表的查询是否错误地加了用户过滤？（应共享）

| 表类型 | 隔离策略 |
|--------|---------|
| portfolio, settings, watchlist | 必须带 user_id |
| recommendations, signals, market_regime | 共享，不按用户过滤 |

### Rule 4: 依赖注入与可测试性

**约束**: 新增 web agent tool 必须遵循 ToolDeps 模式。

**检查方式**:
- 新 `exec*` 函数是否接受 `deps: ToolDeps` 参数？
- 是否直接 import 了 supabase client 而非通过 deps？
- 是否有对应的测试用例在 `__tests__/chat-tools.test.ts`？

## 输出格式

对每个违规项：
```
[VIOLATION] Rule N: 规则名称
  文件: path/to/file
  问题: 具体描述
  建议: 如何修正
```

全部合规时：
```
✅ 架构检查通过 — 所有新增/变更符合项目约束
```

## 使用场景

- 新增文件时主动调用
- Code review 时作为检查项
- 重构后确认没有破坏架构边界
