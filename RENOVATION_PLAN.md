# 🚀 Akshare 智能投研平台改造计划 (Project Evolution)

## 1. 核心目标 (Objective)
**将 `akshare` 从单纯的 "数据下载工具" 升级为 "AI 驱动的智能投研仪表盘"。**

利用 `daily_stock_analysi` 仓库中的 **AI 分析 (Gemini/DeepSeek)**、**舆情搜索** 和 **自动推送** 能力，赋能 `akshare` 现有的 **数据获取** 与 **Streamlit 可视化** 界面，实现 **"数据 + 智能 + 交互"** 的 1+1>2 效果。

---

## 2. 架构改造方案 (Architecture)

### 2.1 现有架构 (As-Is)
*   **前端/交互**: Streamlit (无状态，仅用于参数输入和展示)。
*   **核心逻辑**: Python 脚本直接调 `akshare` 接口。
*   **存储**: 本地 CSV 文件。
*   **痛点**: 用户只能看历史数据，缺乏买卖建议、基本面分析和实时监控。

### 2.2 目标架构 (To-Be)
*   **前端 (UI)**: 增强版 Streamlit，增加 "AI 诊股"、"舆情监控"、"大盘日报" Tab。
*   **中间件 (Middleware)**: 引入轻量级数据库和缓存，管理自选股和历史分析记录。
*   **分析引擎 (Brain)**: 移植 `daily_stock_analysi` 的 `GeminiAnalyzer` 和 `SearchService`。
*   **服务层 (Service)**: 异步任务队列，处理耗时的 AI 分析和报告生成。

---

## 3. 技术选型与中间件 (Technology Stack)

为了保持项目的轻量化同时兼顾扩展性，推荐以下选型：

| 组件 | 推荐方案 | 理由 | 替代方案 |
| :--- | :--- | :--- | :--- |
| **数据库** | **SQLite (本地/开发)** <br> **Supabase (生产/云端)** | SQLite 无需部署，适合单机；Supabase (PostgreSQL) 提供免费层，适合多端同步。 | MySQL, PostgreSQL |
| **缓存/队列** | **Redis** (可选) | 用于缓存 AI 分析结果（避免重复扣费/耗时）和管理待分析的股票队列。 | Python Queue (内存级) |
| **ORM 框架** | **SQLAlchemy** | `daily_stock_analysi` 已使用，方便直接移植代码。 | Peewee, Tortoise ORM |
| **Web 框架** | **Streamlit** (维持) | 现有基础好，适合快速构建数据 Dashboard。 | NiceGUI, Flask+React |

---

## 4. API 平台与服务 (External APIs)

| 服务类型 | 推荐平台 | 关键用途 | 成本优势 |
| :--- | :--- | :--- | :--- |
| **大语言模型 (LLM)** | **Google Gemini 2.0 Flash** | 核心分析引擎。速度快，上下文窗口大，且目前有免费层。 | 免费 (Rate Limit 内) |
| **备选 LLM** | **DeepSeek-V3** | 国内访问稳定，API 成本极低，推理能力强。 | 低成本 (~¥2/百万token) |
| **联网搜索** | **Tavily API** | 获取个股最新新闻、财报预期。专为 AI Agent 优化。 | 免费层 (1000次/月) |
| **消息推送** | **Feishu (飞书) Webhook** | 发送日报、预警提醒。支持富文本卡片。 | 免费 |

---

## 5. 运营成本预估 (Cost Estimation)

假设用户量为 **个人使用 或 小团队 (5-10人)**，每日分析 **20只自选股**。

### 5.1 方案 A：零成本白嫖版 (Zero Cost)
*   **服务器**: 使用 **GitHub Codespaces** (每月60小时免费) 或 **Streamlit Community Cloud** (免费托管)。
*   **数据库**: **SQLite** (存储在仓库中) 或 **Google Sheets** (作为简易数据库)。
*   **API**:
    *   Gemini API (Free Tier)。
    *   Tavily (Free Tier)。
*   **总计**: **$0.00 / 月**
*   *限制*: 无法持久化大量数据，计算资源有限，需手动触发或依赖 Github Actions。

### 5.2 方案 B：高可用低成本版 (Recommended)
适合希望稳定运行、数据不丢失的场景。

| 项目 | 规格/服务 | 预估费用 (月) |
| :--- | :--- | :--- |
| **云服务器 (VPS)** | 2vCPU, 2GB RAM (如 RackNerd, CloudCone 特价机) | **$3.00 - $5.00** |
| **数据库** | Supabase Free Tier (500MB, 足够存数年分析记录) | **$0.00** |
| **LLM API** | DeepSeek (假设每日输入输出共 100k token) | **~$0.50** (约 ¥3-4) |
| **搜索 API** | Tavily Free Tier (够用) 或 DuckDuckGo (免费库) | **$0.00** |
| **总计** | | **$3.50 - $5.50 (约 ¥25 - ¥40)** |

---

## 6. 用户系统与配置管理 (User System)

为了实现 **"一次配置，多端同步"** 的体验，避免用户每次打开网页都要重新输入 API Key，我们将引入用户账户体系。

### 6.1 认证方案 (Authentication)
*   **推荐方案**: **Supabase Auth**
*   **登录方式**:
    *   **邮箱/密码**: 最基础方式。
    *   **Social Login**: 支持 **GitHub** 和 **Google** (通过 Supabase 一键集成，无需自己维护 OAuth 后端)。
*   **优势**:
    *   与数据库 (PostgreSQL) 无缝集成。
    *   利用 **RLS (Row Level Security)** 策略，确保用户只能读取自己的数据，从数据库层面杜绝数据泄露。
    *   免费额度（50,000 MAU）完全覆盖个人/小团队需求。

### 6.2 设置页面 (Settings Page)
用户登录后，侧边栏增加 **"⚙️ 设置 (Settings)"** 页面，提供可视化表单管理敏感信息：

| 配置项 | 说明 | 存储策略 |
| :--- | :--- | :--- |
| **LLM API Key** | Gemini / DeepSeek / OpenAI Key | **加密存储** (AES) 或 仅本地 LocalStorage (更安全但无法跨端) |
| **Search API Key** | Tavily / Bocha Key | 同上 |
| **Notification** | 飞书 Webhook / 企业微信 Webhook | 明文存储 (风险较低) |
| **自选股列表** | 用户关注的股票代码 | 关联 UserID 存储在数据库 |

### 6.3 开发成本分析 (Developer Cost)
接入登录系统对开发者来说**硬成本为 $0**，仅需投入开发时间。目前的云服务生态对独立开发者非常友好：

| 服务组件 | 供应商 | 费用 | 免费额度/限制 |
| :--- | :--- | :--- | :--- |
| **三方登录接口** | Google / GitHub | **$0** | 申请 OAuth App 完全免费，无调用费用。 |
| **身份认证服务** | Supabase Auth | **$0** | **50,000 月活用户 (MAU)**。对个人/小工具而言相当于无限。 |
| **用户数据库** | Supabase Database | **$0** | **500MB** 存储空间。仅存储 UserID 和配置文本，足够存数十万条记录。 |
| **托管服务** | Streamlit Cloud / Vercel | **$0** | Hobby/Community 计划免费，适合非商业用途。 |

**结论**：引入登录系统**不会增加任何运营费用**，可以放心接入。

### 6.4 后续三方登录接入方案 (Future OAuth Integration)

为了降低用户的注册门槛，计划在后续版本中引入 **GitHub** 和 **Google** 登录。由于 Streamlit 是纯 Python 后端渲染框架，处理 OAuth 回调 (Callback) 存在一定技术挑战，因此需要专门的实现方案。

#### 1. 技术难点
Streamlit 的运行机制是无状态的，且 URL 路由控制较弱。OAuth 流程中，第三方平台授权后会重定向回 `http://your-app/callback?code=xxx`。
*   **挑战点**: 如何在 Streamlit 中捕获这个 `code` 并将其交换为 Session，同时不打断用户体验。

#### 2. 实施步骤

**Step 1: Supabase 控制台配置**
1.  **GitHub**:
    *   在 GitHub Developer Settings 创建 OAuth App。
    *   `Homepage URL`: 填 Supabase 项目 URL。
    *   `Callback URL`: 填 `https://<project-ref>.supabase.co/auth/v1/callback`。
    *   获取 Client ID / Secret 填入 Supabase。
2.  **Google**:
    *   在 Google Cloud Console 创建 OAuth Client ID。
    *   配置重定向 URI 同上。
3.  **URL 重定向白名单**:
    *   在 Supabase Auth -> URL Configuration 中，必须将 Streamlit 的部署地址（如 `http://localhost:8501` 或生产域名）加入 **Redirect URLs** 列表。

**Step 2: 前端代码改造 (`auth_component.py`)**
在现有邮箱登录界面下方增加 OAuth 按钮区域：

```python
# 伪代码示例
if st.button("GitHub 登录"):
    # 1. 调用 Supabase SDK 获取授权 URL
    res = supabase.auth.sign_in_with_oauth({
        "provider": "github",
        "options": {
            "redirect_to": "http://localhost:8501"  # 登录成功后跳回当前页面
        }
    })
    # 2. 使用 meta 标签自动跳转到 GitHub 授权页
    if res.url:
        st.markdown(f'<meta http-equiv="refresh" content="0;url={res.url}">', unsafe_allow_html=True)
```

**Step 3: 回调处理与会话恢复**
这是最关键的一步。当 GitHub 跳回 `http://localhost:8501` 时，URL 中会携带 Hash Fragment (例如 `#access_token=...&refresh_token=...`) 或 Query Param。
*   **Supabase 默认行为**: 使用 Hash 传递 Token。
*   **Streamlit 限制**: Python 后端很难直接读取 URL 的 Hash 部分（因为 Hash 不会发送到服务器）。
*   **解决方案**:
    *   **方案 A (推荐)**: 依赖 `supabase-py` 客户端的持久化能力。如果 OAuth 流程在浏览器端写入了 LocalStorage，客户端初始化时尝试 `get_session()` 可能自动恢复。
    *   **方案 B (高级)**: 修改 Supabase 配置使用 PKCE 流程，使 Token 以 Query 参数形式传递 (`?code=...`)，然后使用 `st.query_params` 获取 `code` 并通过 `exchange_code_for_session()` 换取 Token。

#### 3. 预期效果
用户点击 GitHub 图标 -> 跳转授权 -> 自动跳回 Streamlit -> 页面刷新并显示 "欢迎, [用户名]"。无需输入密码，体验极佳。

---

## 7. 功能迁移路线图 (Roadmap)

### 0. 进度概览 (Progress Overview)
- **第一阶段：核心大脑移植**
    - ✅ 依赖整合 (requirements.txt, .env)
    - ⬜️ 代码复用 (analyzer.py, search_service.py)
    - ⬜️ UI 集成 (API Key 输入框)
- **第二阶段：可视化增强**
    - ⬜️ AI 诊股页面
    - ⬜️ 舆情展示
- **第三阶段：用户系统**
    - ⬜️ Supabase 接入
    - ⬜️ 登录组件
    - ⬜️ 配置持久化
- **功能优化 (Ad-hoc)**
    - ✅ 飞书 Webhook 通知支持
    - ✅ 移除批量下载数量限制
    - ✅ 环境变量管理 (.env)

### 第一阶段：移植核心大脑 (Brain Transplant) [进行中]
1.  **代码复用**: 将 `daily_stock_analysi/analyzer.py` (AI 分析) 和 `daily_stock_analysi/search_service.py` (搜素) 复制到 `akshare/analysis/` 目录。
2.  **UI 集成**: 在 `streamlit_app.py` 侧边栏增加 "API Key 配置" 输入框 (Gemini/Tavily)，避免硬编码。

### 第二阶段：可视化增强 (Visualization) [未开始]
1.  **AI 诊股页面**: 新增 Streamlit 页面，用户输入代码 -> 后台调 akshare 获取数据 -> 调 LLM 分析 -> 页面展示 Markdown 格式的 "买卖建议" 和 "风险提示"。
2.  **舆情展示**: 在 K 线图下方展示由 Tavily 搜索到的最近 3 条重大利好/利空新闻。

### 第三阶段：用户系统与云端同步 (Cloud Sync) [未开始]
1.  **Supabase 接入**: 初始化 Supabase 项目，创建 `users` 和 `user_settings` 表。
2.  **登录组件**: 集成 `streamlit-supabase-auth`，实现登录/注册 UI。
3.  **配置持久化**: 将侧边栏的输入框值读取/保存到 Supabase，实现"家里配置，公司可用"。

### 第四阶段：自动化与推送 (Automation) [未开始]
1.  **自选股管理**: 使用数据库记录用户常看的股票。
2.  **后台任务**: 编写简单的 Python 脚本 (`daily_job.py`)，遍历自选股调用分析函数，通过 Webhook 推送到用户手机。
3.  **定时执行**: 利用 VPS 的 `crontab` 或 Github Actions 定时运行 `daily_job.py`。

---

## 8. 总结 (Conclusion)
通过引入 **Gemini/DeepSeek** 和 **Supabase/SQLite**，我们可以以极低的成本将 `akshare` 从一个冷冰冰的数据工具变成一个**会思考的私人投资助理**。
推荐先采用 **方案 A (零成本)** 进行原型开发，验证效果后再迁移至 **方案 B**。
