---
name: pre-commit-review
description: 提交前语义审阅 — 检查冗余、命名、死代码、啰嗦模式等 ruff 无法覆盖的软规则
user_invocable: true
---

# Pre-Commit Review

对当前 staged 变更进行语义级质量审阅。覆盖机械工具（ruff/tsc）无法检测的问题。

## 执行步骤

1. 运行 `git diff --cached --name-only` 获取待提交文件列表
2. 运行 `git diff --cached` 获取完整 diff
3. 如果没有 staged 变更，改为检查 `git diff HEAD` (unstaged changes)
4. 对每个变更文件，按以下清单逐项审阅

## 审阅清单

### 冗余代码（硬规则 #1）
- [ ] 是否存在只转发一层的 wrapper function？
- [ ] 是否存在 assign-then-immediately-return 的变量？
- [ ] 是否有只有一个 caller、无复用前景的中间抽象？
- [ ] 是否存在无意义的 re-export 或 re-declaration？

### 死代码（软规则 #3）
- [ ] 是否有未使用的 import？
- [ ] 是否有注释掉的代码块？
- [ ] 是否有不可达的分支 (unreachable after return/throw)？

### 命名语义
- [ ] 函数名是否准确描述其行为？（不多不少）
- [ ] 变量名是否自解释？布尔变量是否用 is/has/should 前缀？
- [ ] 是否存在误导性命名？（名字暗示 A 但实际做 B）

### 啰嗦模式
- [ ] 是否有可以用更简洁 idiom 替代的冗长写法？
  - Python: `if x is not None: return x` → 直接表达
  - TS: 手动 `.filter().map()` 可否合并
- [ ] 是否有多余的中间变量？（只用一次且无可读性贡献）
- [ ] 条件分支是否可以用 early return 简化嵌套？

### 架构一致性
- [ ] 新增 web 功能是否放在 Agent tool 里而非新路由？
- [ ] Python 新模块是否放对了目录？(core/tools/integrations/agents)

## 输出格式

对每个发现的问题，报告：
```
[文件:行号] [类别] 问题描述
  建议: 具体修改方案
```

如果全部通过，输出：
```
✅ 审阅通过 — 无语义质量问题
```

## 注意事项

- 不重复 ruff 已能检测的问题（格式、import 排序等）
- 不重复 tsc 已能检测的问题（类型错误、未使用变量等）
- 聚焦"语义层面"的质量问题
- 只报告确定的问题，不报告风格偏好
