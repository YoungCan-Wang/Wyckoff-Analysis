"""
tools/ -- 可复用 Tool 函数层。

连接 core/（纯计算）与 integrations/（数据源），为 agents/ 和 scripts/ 提供
可独立调用的 Tool 函数。每个 Tool 函数聚焦单一职责，有明确的输入输出。

模块清单：
  - funnel_config    : FunnelConfig 环境变量覆盖
  - symbol_pool      : 股票池解析（环境变量驱动的股票来源选择）
  - data_fetcher     : 并行 OHLCV 批量拉取（进程/线程模式 + 硬超时）
  - market_regime    : 大盘水温 + breadth + regime 分类 + 动态阈值
  - candidate_ranker : L3 候选排名打分（综合动量/缩量/触发/板块）
  - report_builder   : AI 研报 prompt 构建 + 报告解析（三阵营分流）

单元测试：tests/test_tools.py（23 个测试覆盖所有 6 个模块）
"""
