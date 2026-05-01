# 架构文档说明

## 文档结构

### 📘 [ARCHITECTURE.md](ARCHITECTURE.md) — 系统架构文档（原有）

**面向对象**：开发者、贡献者、用户

**内容**：
- ✅ 系统全景图
- ✅ 三通道复用（Web/CLI/MCP）
- ✅ 工具清单（18 个：10 共享 + 5 本地 + 3 委派）
- ✅ 五层漏斗引擎
- ✅ 数据源集成
- ✅ 定时任务说明
- ✅ 目录结构

**特点**：全面、详细、偏向"是什么"

---

### 🔍 [ARCHITECTURE_ANALYSIS.md](ARCHITECTURE_ANALYSIS.md) — 架构深度分析（新增）

**面向对象**：架构师、高级开发者、代码审查者

**内容**：
- ✅ 核心架构设计（ReAct Loop + 三层架构）
- ✅ 核心机制解析（后台任务、消息排队、上下文压缩、Loop Guard）
- ✅ 高级特性（Sub-Agent 委派、Agent 记忆、工具确认、Provider 抽象）
- ✅ 架构优点总结（解决的问题 + 工程实践亮点）
- ✅ 待提升点与优化建议（8 个具体建议）
- ✅ 架构文档优化建议（ADR、设计模式、性能指标、故障处理、扩展指南）
- ✅ 架构成熟度评估（4.6/5.0）

**特点**：深度、批判性、偏向"为什么"和"怎么改进"

---

## 两份文档的关系

```
ARCHITECTURE.md          ARCHITECTURE_ANALYSIS.md
     │                            │
     │ 描述"是什么"                │ 分析"为什么"
     │ 系统组成                    │ 设计决策
     │ 数据流向                    │ 优缺点
     │ 配置说明                    │ 改进建议
     │                            │
     └────────────┬───────────────┘
                  │
            互补关系
```

**建议阅读顺序**：
1. 先读 `ARCHITECTURE.md` 了解系统全貌
2. 再读 `ARCHITECTURE_ANALYSIS.md` 理解设计思路和改进方向

---

## 后续文档规划

根据 `ARCHITECTURE_ANALYSIS.md` 的建议，可以补充：

### 📁 ADR/ — 架构决策记录
- `001-react-vs-plan.md` — 为什么选择 ReAct 而不是 Plan-and-Execute
- `002-thread-vs-asyncio.md` — 为什么后台任务用 Thread 而不是 asyncio
- `003-sqlite-vs-redis.md` — 为什么本地存储用 SQLite 而不是 Redis

### 📊 PERFORMANCE.md — 性能指标
- Token 消耗统计（各操作的输入/输出 token）
- 响应时间统计（平均耗时、P95 耗时）
- 后台任务并发能力

### 🛠️ TROUBLESHOOTING.md — 故障排查
- 常见故障场景（LLM 超时、数据源不可用、Doom Loop）
- 检测与恢复机制
- 日志与监控

### 🎨 DESIGN_PATTERNS.md — 设计模式
- ReAct Loop 实现细节
- Provider 抽象模式
- Sub-Agent 委派模式
- 后台任务管理模式

### 🤝 CONTRIBUTING.md — 贡献指南
- 如何新增工具
- 如何新增 LLM Provider
- 如何新增 Sub-Agent
- 测试策略

---

**最后更新**：2026-04-27  
**维护者**：Wyckoff Analysis Team
