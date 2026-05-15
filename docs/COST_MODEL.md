# WyckoffAgent 运行成本

感谢开源社区的认可，也感谢越来越多用户持续使用 WyckoffAgent。

随着形态复盘、持仓与在线分析服务的使用量上升，Supabase 和 Render 的免费额度已经不再够用。从 **2026-06-03** 起，项目基础设施成本开始陡增：从原本约 **CNY 490/月**，变为约 **CNY 840/月**。

![Supabase quota grace period](screenshots/supabase-quota-grace-2026-06-03.svg)

详情如下：

| 基础设施 | 当前选择 | 成本 | 起算口径 | 作用 |
| --- | --- | ---: | --- | --- |
| 行情数据 | TickFlow | CNY 199/月 | 项目设立初期 | 提供 A 股 / 港股 / 美股行情，是漏斗、改价、尾盘判断的数据源。 |
| 数据库 | Supabase Pro | USD 25/月，约 CNY 175/月 | 2026-06-03 起 | 存储形态复盘、持仓、用户配置、市场信号和任务结果。 |
| 在线分析服务 | Render Web Service | USD 25/月，约 CNY 175/月 | 2026-06-03 起 | 承载 Web App 与 Agent 的在线分析请求，让用户可以稳定获取筛选、诊断和回测结果。 |
| AI 推理 | Gemini API | USD 20/月，约 CNY 140/月 | 项目设立初期 | 生成 AI 研报、复盘摘要和部分尾盘决策辅助。 |
| 开发维护 | Codex | USD 20/月，约 CNY 140/月 | 项目设立初期 | 项目迭代。 |
| 域名 | Wyckoff 相关域名 | USD 20/年，约 CNY 12/月 | 2026年6月起 | 用于项目主页、Web App 和后续 API 品牌入口。 |

按 USD 1 = CNY 7 粗算，当前项目总成本约 **CNY 840/月**。
