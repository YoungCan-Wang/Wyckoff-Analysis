# Requirements Document

## Introduction

为现有的A股历史行情导出工具集成AI智能分析功能，专注于威科夫分析方法，为用户提供专业的技术分析报告和投资建议。该功能将直接集成到现有的Streamlit Web界面中，用户获取股票数据后可一键获得AI分析结果。

## Glossary

- **Gemini_Client**: Google Gemini API客户端，负责调用Gemini进行股票分析
- **Data_Formatter**: 数据格式化器，将股票数据整理为适合AI分析的格式
- **Cache_Manager**: 缓存管理器，存储和管理分析结果缓存
- **Config_Manager**: 配置管理器，管理API密钥和系统配置
- **User_Interface**: 用户界面，现有Streamlit应用的扩展部分
- **Report_Display**: 报告展示组件，格式化和展示AI分析结果

## Requirements

### Requirement 1: 威科夫技术分析

**User Story:** 作为股票投资者，我希望AI能基于威科夫分析方法分析我的股票数据，以便我了解当前的市场阶段和价量关系。

#### Acceptance Criteria

1. WHEN 用户点击AI分析按钮 THEN THE AI_Analysis_Engine SHALL 对股票的OHLCV数据进行威科夫分析
2. WHEN 进行威科夫分析 THEN THE Wyckoff_Analyzer SHALL 识别当前市场阶段（吸筹、拉升、派发、下跌）
3. WHEN 分析价量关系 THEN THE Wyckoff_Analyzer SHALL 评估成交量与价格变动的协调性
4. WHEN 检测关键价位 THEN THE Wyckoff_Analyzer SHALL 识别支撑位、阻力位和突破点
5. WHEN 分析完成 THEN THE Analysis_Report_Generator SHALL 生成包含市场阶段判断和操作建议的报告

### Requirement 2: 数据准备与格式化

**User Story:** 作为系统开发者，我希望能将股票数据格式化为适合AI分析的格式，以便Gemini能准确理解和分析数据。

#### Acceptance Criteria

1. WHEN 准备分析数据 THEN THE Data_Formatter SHALL 提取关键价格和成交量指标
2. WHEN 格式化数据 THEN THE Data_Formatter SHALL 计算30日和10日的统计摘要
3. WHEN 构建提示词 THEN THE Data_Formatter SHALL 将数据整理为清晰的文本格式
4. WHEN 数据准备完成 THEN THE Data_Formatter SHALL 将格式化数据传递给Gemini API
5. WHEN 使用现有数据 THEN THE Data_Formatter SHALL 利用akshare提供的换手率和振幅数据

### Requirement 3: Gemini AI分析集成

**User Story:** 作为用户，我希望使用已调教好的Gemini Gem进行股票分析，以便获得专业的威科夫技术分析报告。

#### Acceptance Criteria

1. WHEN 调用Gemini API THEN THE Gemini_Client SHALL 使用配置的API密钥安全地发送请求
2. WHEN 使用自定义Gem THEN THE Gemini_Client SHALL 应用已调教好的提示词和配置
3. WHEN 生成分析报告 THEN THE Gemini_Client SHALL 返回包含市场阶段、价量分析和操作建议的完整报告
4. WHEN 分析完成 THEN THE Report_Display SHALL 以易读格式展示分析结果
5. WHEN 展示报告 THEN THE Report_Display SHALL 包含免责声明和风险提示

### Requirement 4: 错误处理与重试机制

**User Story:** 作为系统管理员，我希望系统能可靠地处理API调用失败，以便确保用户体验的稳定性。

#### Acceptance Criteria

1. WHEN Gemini API调用失败 THEN THE Gemini_Client SHALL 实施指数退避重试机制
2. WHEN 网络超时 THEN THE Gemini_Client SHALL 返回友好的错误提示并建议重试
3. WHEN API配额不足 THEN THE Gemini_Client SHALL 显示配额限制信息
4. WHEN 重试失败 THEN THE Gemini_Client SHALL 记录错误日志并通知用户
5. WHEN 发生错误 THEN THE User_Interface SHALL 提供重试按钮和错误详情

### Requirement 5: 结果缓存优化

**User Story:** 作为用户，我希望重复查询相同股票时能快速获得结果，以便提高使用效率。

#### Acceptance Criteria

1. WHEN 相同股票重复分析 THEN THE Cache_Manager SHALL 返回缓存的分析结果
2. WHEN 缓存过期 THEN THE Cache_Manager SHALL 自动清理过期数据并重新分析
3. WHEN 缓存命中 THEN THE Cache_Manager SHALL 显示缓存时间和有效期
4. WHEN 用户请求 THEN THE Cache_Manager SHALL 提供强制刷新选项
5. WHEN 缓存数据 THEN THE Cache_Manager SHALL 使用股票代码和日期作为缓存键

### Requirement 6: Streamlit界面集成

**User Story:** 作为用户，我希望AI分析功能无缝集成到现有界面中，以便我能方便地使用这个功能。

#### Acceptance Criteria

1. WHEN 股票数据加载完成 THEN THE User_Interface SHALL 显示"AI智能分析"按钮
2. WHEN 用户点击分析按钮 THEN THE User_Interface SHALL 显示分析进度和状态信息
3. WHEN 分析完成 THEN THE User_Interface SHALL 在当前页面展示分析报告
4. WHEN 分析失败 THEN THE User_Interface SHALL 显示错误信息和重试选项
5. WHEN 移动设备访问 THEN THE User_Interface SHALL 适配移动端显示格式

### Requirement 7: 配置管理

**User Story:** 作为系统管理员，我希望能安全地管理API密钥和配置，以便系统稳定运行。

#### Acceptance Criteria

1. WHEN 配置API密钥 THEN THE Config_Manager SHALL 使用Streamlit Secrets安全存储
2. WHEN 读取配置 THEN THE Config_Manager SHALL 验证必需配置项的存在
3. WHEN 配置缺失 THEN THE Config_Manager SHALL 返回明确的配置错误提示
4. WHEN 更新配置 THEN THE Config_Manager SHALL 支持热更新而无需重启应用
5. WHEN 部署到生产 THEN THE Config_Manager SHALL 区分开发和生产环境配置