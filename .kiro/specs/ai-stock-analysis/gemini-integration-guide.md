# Gemini API 集成指南

## 概述

本指南说明如何将你已调教好的Gemini Gem集成到现有的Streamlit A股分析应用中。使用Google Gemini API可以直接调用你的自定义Gem，无需重新训练或调整提示词。

## Gemini API vs OpenAI API

### 优势
- ✅ **免费额度更高**: Gemini提供更慷慨的免费API调用额度
- ✅ **已有调教好的Gem**: 直接使用你的自定义提示词和配置
- ✅ **多模态能力**: 支持文本、图片、视频等多种输入
- ✅ **中文支持好**: 对中文股票分析更友好

### 成本对比
- **Gemini Pro**: 免费（有限额）或 $0.00025/1K tokens
- **OpenAI GPT-4**: $0.03/1K tokens (输入) + $0.06/1K tokens (输出)

## 技术实现方案

### 1. 安装依赖

```bash
pip install google-generativeai streamlit python-dotenv
```

更新 `requirements.txt`:
```txt
akshare>=1.18.9
pandas>=2.3.3
streamlit>=1.52.2
google-generativeai>=0.3.0
python-dotenv>=1.0.0
```

### 2. 获取Gemini API密钥

1. 访问 [Google AI Studio](https://makersuite.google.com/app/apikey)
2. 创建或选择项目
3. 生成API密钥
4. 保存密钥到Streamlit Secrets

### 3. 配置Streamlit Secrets

在Streamlit Cloud部署设置中添加secrets：

```toml
# .streamlit/secrets.toml (本地开发)
[gemini]
api_key = "your-gemini-api-key-here"

# 可选：如果你的Gem有特定ID
gem_id = "your-gem-id-here"
```

在Streamlit Cloud上：
1. 进入应用设置 → Secrets
2. 添加相同的配置

### 4. 核心代码实现

#### 4.1 创建Gemini客户端模块

```python
# gemini_client.py
import google.generativeai as genai
import streamlit as st
from typing import Optional
import pandas as pd

class GeminiStockAnalyzer:
    """Gemini API客户端，用于股票分析"""
    
    def __init__(self, api_key: str):
        """初始化Gemini客户端"""
        genai.configure(api_key=api_key)
        
        # 使用Gemini Pro模型
        self.model = genai.GenerativeModel('gemini-pro')
        
        # 如果有自定义Gem，可以在这里配置
        # self.model = genai.GenerativeModel('gemini-pro', 
        #                                    system_instruction=your_gem_instructions)
    
    def analyze_stock(self, 
                     symbol: str, 
                     name: str, 
                     df: pd.DataFrame,
                     sector: str = "") -> str:
        """
        分析股票数据并返回威科夫分析报告
        
        Args:
            symbol: 股票代码
            name: 股票名称
            df: OHLCV数据DataFrame
            sector: 行业信息
            
        Returns:
            分析报告文本
        """
        # 准备数据摘要
        data_summary = self._prepare_data_summary(df, symbol, name, sector)
        
        # 构建提示词（如果你有自定义Gem，这里的提示词会更简洁）
        prompt = self._build_analysis_prompt(data_summary)
        
        try:
            # 调用Gemini API
            response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            raise Exception(f"Gemini API调用失败: {str(e)}")
    
    def _prepare_data_summary(self, 
                             df: pd.DataFrame, 
                             symbol: str, 
                             name: str,
                             sector: str) -> dict:
        """准备数据摘要用于分析"""
        recent_30 = df.tail(30)
        recent_10 = df.tail(10)
        
        return {
            "symbol": symbol,
            "name": name,
            "sector": sector,
            "total_days": len(df),
            "date_range": f"{df['Date'].iloc[0]} 至 {df['Date'].iloc[-1]}",
            
            # 价格数据
            "current_price": float(df['Close'].iloc[-1]),
            "price_change_30d": float((df['Close'].iloc[-1] - df['Close'].iloc[-30]) / df['Close'].iloc[-30] * 100),
            "highest_30d": float(recent_30['High'].max()),
            "lowest_30d": float(recent_30['Low'].min()),
            
            # 成交量数据
            "avg_volume_30d": float(recent_30['Volume'].mean()),
            "volume_trend": "放量" if recent_10['Volume'].mean() > recent_30['Volume'].mean() else "缩量",
            
            # 换手率和振幅
            "avg_turnover_30d": float(recent_30['TurnoverRate'].mean()),
            "avg_amplitude_30d": float(recent_30['Amplitude'].mean()),
            
            # 最近10天详细数据
            "recent_data": recent_10[['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'TurnoverRate', 'Amplitude']].to_dict('records')
        }
    
    def _build_analysis_prompt(self, data_summary: dict) -> str:
        """构建分析提示词"""
        
        # 如果你使用自定义Gem，这个提示词可以更简洁
        # 因为Gem已经包含了你的专业指令
        
        prompt = f"""
请对以下A股股票进行威科夫技术分析：

## 基本信息
- 股票代码：{data_summary['symbol']}
- 股票名称：{data_summary['name']}
- 所属行业：{data_summary['sector']}
- 分析周期：{data_summary['date_range']} (共{data_summary['total_days']}个交易日)

## 价格数据
- 当前价格：{data_summary['current_price']:.2f}元
- 30日涨跌幅：{data_summary['price_change_30d']:.2f}%
- 30日最高价：{data_summary['highest_30d']:.2f}元
- 30日最低价：{data_summary['lowest_30d']:.2f}元

## 成交量数据
- 30日平均成交量：{data_summary['avg_volume_30d']:,.0f}股
- 成交量趋势：{data_summary['volume_trend']}
- 30日平均换手率：{data_summary['avg_turnover_30d']:.2f}%
- 30日平均振幅：{data_summary['avg_amplitude_30d']:.2f}%

## 最近10个交易日详细数据
{self._format_recent_data(data_summary['recent_data'])}

请从威科夫分析角度提供：
1. **市场阶段判断**：当前处于吸筹、拉升、派发还是下跌阶段？
2. **价量关系分析**：价格与成交量的配合情况如何？
3. **关键价位识别**：支撑位、阻力位在哪里？
4. **操作建议**：买入、持有还是卖出？给出具体理由和风险提示。

请用专业但易懂的语言回答，适合普通投资者阅读。
"""
        return prompt
    
    def _format_recent_data(self, recent_data: list) -> str:
        """格式化最近数据为表格"""
        lines = ["日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 | 换手率 | 振幅"]
        lines.append("-" * 80)
        
        for row in recent_data:
            lines.append(
                f"{row['Date']} | "
                f"{row['Open']:.2f} | "
                f"{row['High']:.2f} | "
                f"{row['Low']:.2f} | "
                f"{row['Close']:.2f} | "
                f"{row['Volume']:,.0f} | "
                f"{row['TurnoverRate']:.2f}% | "
                f"{row['Amplitude']:.2f}%"
            )
        
        return "\n".join(lines)


@st.cache_resource
def get_gemini_analyzer():
    """获取Gemini分析器实例（使用缓存）"""
    try:
        api_key = st.secrets["gemini"]["api_key"]
        return GeminiStockAnalyzer(api_key)
    except Exception as e:
        st.error(f"无法初始化Gemini分析器: {e}")
        return None
```

#### 4.2 集成到Streamlit应用

在 `streamlit_app.py` 中添加AI分析功能：

```python
# 在文件开头导入
from gemini_client import get_gemini_analyzer

# 在数据展示部分后添加AI分析功能
if st.button("🤖 AI智能分析（威科夫方法）", type="secondary", use_container_width=True):
    analyzer = get_gemini_analyzer()
    
    if analyzer is None:
        st.error("AI分析服务未配置，请联系管理员")
    else:
        with st.spinner("🧠 AI正在进行威科夫技术分析，请稍候..."):
            try:
                # 调用Gemini进行分析
                analysis_result = analyzer.analyze_stock(
                    symbol=st.session_state.current_symbol,
                    name=name,
                    df=df_export,
                    sector=sector
                )
                
                # 展示分析结果
                st.markdown("### 🎯 AI分析报告")
                st.markdown(analysis_result)
                
                # 添加免责声明
                st.caption(
                    "⚠️ 本分析仅供参考，不构成投资建议。"
                    "投资有风险，入市需谨慎。请结合自身情况做出投资决策。"
                )
                
            except Exception as e:
                st.error(f"分析失败: {str(e)}")
                st.info("请稍后重试，或检查网络连接")
```

### 5. 使用自定义Gem

如果你已经创建了自定义Gem，可以通过以下方式使用：

```python
# 方法1: 使用system_instruction
self.model = genai.GenerativeModel(
    'gemini-pro',
    system_instruction="""
    你是一位专业的A股威科夫技术分析师...
    [你的Gem的完整提示词]
    """
)

# 方法2: 如果Gem有特定ID（需要查看Google AI Studio）
# 注意：截至2025年1月，Gem可能还不支持直接通过API调用
# 但你可以将Gem的提示词复制到system_instruction中
```

### 6. 错误处理和重试机制

```python
import time
from typing import Optional

def analyze_with_retry(analyzer: GeminiStockAnalyzer, 
                      symbol: str, 
                      name: str, 
                      df: pd.DataFrame,
                      sector: str,
                      max_retries: int = 3) -> Optional[str]:
    """带重试机制的分析函数"""
    
    for attempt in range(max_retries):
        try:
            result = analyzer.analyze_stock(symbol, name, df, sector)
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 指数退避
                st.warning(f"分析失败，{wait_time}秒后重试... (尝试 {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                st.error(f"分析失败，已达到最大重试次数: {str(e)}")
                return None
    
    return None
```

## 部署到Streamlit Cloud

### 步骤：

1. **更新requirements.txt**
   ```txt
   akshare>=1.18.9
   pandas>=2.3.3
   streamlit>=1.52.2
   google-generativeai>=0.3.0
   ```

2. **配置Secrets**
   - 在Streamlit Cloud应用设置中
   - 添加Gemini API密钥到Secrets

3. **推送代码到GitHub**
   ```bash
   git add .
   git commit -m "Add Gemini AI analysis feature"
   git push origin main
   ```

4. **Streamlit自动重新部署**
   - 代码推送后自动触发部署
   - 等待几分钟即可使用新功能

## 成本估算

### Gemini API定价（2025年1月）
- **免费额度**: 每分钟60次请求
- **付费**: $0.00025/1K tokens (输入) + $0.0005/1K tokens (输出)

### 预估成本
- 每次分析约消耗: 2K-5K tokens
- 每次分析成本: $0.001-0.003
- 月成本（100次分析）: $0.1-0.3

**远低于OpenAI GPT-4的成本！**

## 优化建议

1. **缓存分析结果**: 相同股票24小时内返回缓存结果
2. **批量分析**: 支持一次分析多只股票
3. **流式输出**: 使用`stream=True`实时显示分析过程
4. **多语言支持**: Gemini对中文支持很好，无需特殊处理

## 下一步

1. 创建`gemini_client.py`文件
2. 更新`streamlit_app.py`集成AI分析按钮
3. 配置Streamlit Secrets
4. 本地测试
5. 部署到Streamlit Cloud

需要我帮你生成完整的代码文件吗？
