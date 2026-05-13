<div align="center">

# Wyckoff Trading Agent

**A株 / 香港株 / 米国株向けワイコフ出来高分析エージェント -- 自然言語で話しかけると、相場を読み解く**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](../LICENSE)
[![Web App](https://img.shields.io/badge/Web-React%20App-0ea5e9.svg)](https://wyckoff-analysis.pages.dev/)
[![Homepage](https://img.shields.io/badge/homepage-Wyckoff%20Homepage-0ea5e9.svg)](https://youngcan-wang.github.io/wyckoff-homepage/)

[中文](../README.md) | [English](README_EN.md) | [Español](README_ES.md) | [한국어](README_KO.md) | [アーキテクチャ](ARCHITECTURE.md)

</div>

---

自然言語でワイコフの達人と対話しよう。彼は10個の専門ツール + 5個の汎用能力を操り、多段階推論を自律的に連鎖させ、「仕掛けるべきか否か」を教えてくれる。

Web + CLI + MCP の三系統対応、Gemini / Claude / OpenAI / DeepSeek から選択可能、GitHub Actions による完全自動化。

プロジェクトホームページ：**[youngcan-wang.github.io/wyckoff-homepage](https://youngcan-wang.github.io/wyckoff-homepage/)**

> リスク開示：WyckoffAgent は教育・研究・情報提供を目的としたツールです。投資助言ではなく、個人の財務状況を網羅するものでも、将来の成果を保証するものでもありません。

## ドキュメントナビ

| 知りたいこと | 参照先 |
|---|---|
| 使い方、デプロイ、設定 | この README |
| アーキテクチャ、Actions、データテーブル、キャッシュ方針 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| ファネル、AI レポート、OMS、バックテストロジック | [../README_STRATEGY.md](../README_STRATEGY.md) |
| 用語集 | [../GLOSSARY.md](../GLOSSARY.md) |
| 研究ノートと運用 | [../wiki_repo_new/Home.md](../wiki_repo_new/Home.md) |

## Special Thanks

<table>
  <tr>
    <td width="150" align="center">
      <a href="https://tickflow.org/auth/register?ref=5N4NKTCPL4">
        <img src="../attach/tickflow-logo.png" alt="TickFlow" width="120" />
      </a>
    </td>
    <td>
      <strong><a href="https://tickflow.org/auth/register?ref=5N4NKTCPL4">TickFlow</a></strong><br />
      WyckoffAgent の A株 / 米国株 / 香港株の高品質マーケットデータ能力を支えてくれる TickFlow に感謝します。
    </td>
  </tr>
</table>

## オンライン利用

インストール不要です。

**React Web App**：**[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/)**

AI Agent 対話、ポートフォリオ管理、ファネル選股、推奨追跡、データ出力、ストリーミング表示、ツール呼び出し可視化を備えた React SPA。

| 対話室 | ファネル選股 |
|:---:|:---:|
| <img src="screenshots/web-chat.png" width="450" /> | <img src="screenshots/web-screen.png" width="450" /> |

| 推奨追跡 | ポートフォリオ |
|:---:|:---:|
| <img src="screenshots/web-track.png" width="450" /> | <img src="screenshots/web-portfolio.png" width="450" /> |

**Streamlit メンテナンス入口**：**[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

Streamlit は初期 MVP を支えた入口で、現在もデータ出力・単一銘柄分析・設定などに利用できます。

| 対話室 | データ出力 |
|:---:|:---:|
| <img src="../attach/web-chat.png" width="450" /> | <img src="../attach/web-export.png" width="450" /> |

## 機能一覧

| 機能 | 説明 |
|------|------|
| 対話型エージェント | 自然言語で診断・スクリーニング・レポートを起動、LLM がツールを自律編成；ファイル読み書き・コマンド実行・Web取得も可能 |
| スキル | 内蔵スラッシュコマンド（`/screen`、`/checkup`、`/report`、`/strategy`、`/backtest`）でワンタップ複合ワークフロー実行；`~/.wyckoff/skills/*.md` でユーザー拡張可能 |
| 5層ファネルスクリーニング | A株全市場と香港株 / 米国株の独立 universe を、6チャネル + セクター共鳴 + ミクロ狙撃で走査 |
| AI 3陣営レポート | ロジック破綻 / 備蓄キャンプ / 発射台 -- LLM が独立審判 |
| ポートフォリオ診断 | 一括ヘルスチェック：移動平均構造、アキュムレーション段階、トリガーシグナル、ストップロス状態 |
| プライベート判断 | 保有 + 候補を総合し EXIT/TRIM/HOLD/PROBE/ATTACK 指令を出力、Telegram プッシュ対応 |
| 引け値買い戦略 | 13:50に実行、ルールスコアリング + LLM再評価の二段階で終盤エントリー対象を選別 |
| シグナル確認プール | L4 トリガーシグナルを 1-3 日の価格確認後に操作可能 |
| レコメンド追跡 | 過去の推奨銘柄の終値を自動同期、累積リターンを算出 |
| 日足バックテスト | ファネルヒット後 N 日間のリターンをリプレイ、勝率 / シャープレシオ / 最大ドローダウンを出力 |
| プレマーケットリスク管理 | A50 + VIX モニタリング、4段階アラートプッシュ |
| ローカルダッシュボード | `wyckoff dashboard` — 推奨・シグナル・保有・Agent記憶・対話ログ、ダーク/ライトテーマ、日中バイリンガル |
| Agent 記憶 | クロスセッション記憶：対話結論を自動抽出、次回クエリ時に関連コンテキストを注入 |
| コンテキスト圧縮 | 動的閾値（モデルcontext windowの25%）で自動圧縮、ツール結果のスマート要約で重要データを保持 |
| ツール確認 | `exec_command`、`write_file`、`update_portfolio` は実行前にユーザー承認が必要 |
| 汎用 Agent 能力 | コマンド実行・ファイル読み書き・Web取得 — CSV パスを送れば即分析 |
| MCP Server | MCP プロトコルで10個のツールを公開、Claude Code / Cursor / 任意のMCPクライアントに対応 |
| マルチチャネル通知 | Feishu / WeCom / DingTalk / Telegram |

## データソース

個別銘柄の日足データは自動フォールバック：

```
tickflow → tushare → akshare → baostock → efinance
```

いずれかのソースが利用不可の場合、自動的に次へ切り替え。手動操作は不要。

> **推奨：TickFlow 接続で A株 / 米国株 / 香港株のリアルタイム・分時データ能力が強化されます**
> 登録：[TickFlow登録リンク](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## ローカル利用

### CLI — 推奨

ターミナルネイティブのワークフローで、最も機能が揃っています。バックグラウンドタスク、記憶、Skills、MCP Server、ローカル SQLite 保存に対応。

### ワンライナーインストール（推奨）

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/WyckoffTradingAgent/main/install.sh | bash
```

Python の検出、uv のインストール、隔離環境の作成を自動で行います。完了後 `wyckoff` で起動。

### Homebrew

```bash
brew tap YoungCan-Wang/wyckoff
brew install wyckoff
```

### pip

```bash
uv venv && source .venv/bin/activate
uv pip install youngcan-wyckoff-analysis
wyckoff
```

### 起動後 — ワンクリック Agent 設定

起動後たった二ステップ：
1. `/model` — モデル選択（Gemini / Claude / OpenAI）、API Key 入力
2. 質問を入力して対話開始 — 登録不要、ポートフォリオはローカル保存

```
> 000001 と 600519、どちらが買いか見てほしい
> ポートフォリオを審判して
> 今の相場の温度感は？
```

> オプション：`/login` でクラウド同期（マルチデバイス）。ログインしなくても全機能利用可能。

アップグレード：`wyckoff update`

| 起動画面 | 保有照会 |
|:---:|:---:|
| <img src="../attach/cli-home.png" width="450" /> | <img src="../attach/cli-running.png" width="450" /> |

| 診断レポート | 操作指令 |
|:---:|:---:|
| <img src="../attach/cli-analysis.png" width="450" /> | <img src="../attach/cli-result.png" width="450" /> |

### ローカルダッシュボード

```bash
wyckoff dashboard
```

ローカル HTTP ダッシュボード（デフォルト 8765）を起動し、ブラウザを自動で開きます。すべてのデータはローカル SQLite に保存されます。

推奨、シグナル、保有、Agent 記憶、設定、対話ログ、Agent ログ、同期状態に対応。ダーク/ライトテーマと日中バイリンガル UI を備えています。

| データ概要 | 対話ログ | Trace 詳細 |
|:---:|:---:|:---:|
| <img src="../attach/dashboard-overview.png" width="300" /> | <img src="../attach/dashboard-chatlog.png" width="300" /> | <img src="../attach/dashboard-chatlog-trace.png" width="300" /> |

### バックテストグリッド

18 パラメータの並列実行、最適パラメータ・シャープマトリクス・戦略ヘルスチェックを出力：

| 最適パラメータ & ランキング | パラメータマトリクス |
|:---:|:---:|
| <img src="../attach/backtest-grid-1.png" width="450" /> | <img src="../attach/backtest-grid-2.png" width="450" /> |

### ローカル Web

CLI と同じローカル SQLite データを共有する React SPA：

```bash
cd web/apps/web
pnpm install
pnpm dev
```

旧 Streamlit のローカル起動：

```bash
git clone https://github.com/YoungCan-Wang/WyckoffTradingAgent.git
cd WyckoffTradingAgent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Web App：**[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/)**

Streamlit：**[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

## ツール

エージェントの武器庫 — 10個の定量ツール + 5個の汎用能力：

| ツール | 機能 |
|--------|------|
| `search_stock_by_name` | 名前 / コード / ピンインによるあいまい検索 |
| `analyze_stock` | ワイコフ診断 / 直近 OHLCV 相場データ（mode 切替） |
| `portfolio` | 保有一覧表示 / 一括ポートフォリオ診断（mode 切替） |
| `update_portfolio` | 保有の追加/変更/削除、余剰資金設定、追跡記録削除 |
| `get_market_overview` | 市場全体の温度感 |
| `screen_stocks` | 5層ファネルによる全市場スクリーニング（⚡バックグラウンド） |
| `generate_ai_report` | 3陣営 AI 詳細レポート（⚡バックグラウンド） |
| `generate_strategy_decision` | 保有銘柄の去就 + 新規エントリー判断（⚡バックグラウンド） |
| `query_history` | 過去の推奨 / シグナルプール / 引け値買い履歴の照会 |
| `run_backtest` | ファネル戦略のバックテスト（⚡バックグラウンド） |
| `check_background_tasks` | バックグラウンドタスク進捗照会 |
| `exec_command` | ローカルシェルコマンドの実行 |
| `read_file` | ローカルファイルの読み取り（CSV/Excel自動解析） |
| `write_file` | ファイルの書き込み（レポート/データのエクスポート） |
| `web_fetch` | Webコンテンツの取得（金融ニュース/公告） |

ツールの呼び出し順序と回数は LLM がリアルタイムに判断。事前編成は不要。CSV パスを送れば読み込み、「パッケージをインストールして」と言えば実行。

## 5層ファネル

| 層 | 名称 | 処理内容 |
|----|------|----------|
| L1 | ゴミ除去 | ST / BSE / STAR Market を除外、時価総額 >= 35億元、日次平均出来高 >= 5,000万元 |
| L2 | 6チャネル選別 | 主要上昇 / 点火 / 潜伏 / アキュムレーション / 閑散出来高 / 支持 |
| L3 | セクター共鳴 | 業種 Top-N 分布フィルター |
| L4 | ミクロ狙撃 | Spring / LPS / SOS / EVR / Compression の5大トリガーシグナル |
| L5 | AI 審判 | LLM による3陣営分類：ロジック破綻 / 備蓄 / 発射台 |

## 日次自動化

リポジトリ内蔵の GitHub Actions 定時タスク：

| タスク | 時刻（北京時間） | 説明 |
|--------|-----------------|------|
| ファネル + AI レポート + プライベート判断 | 日-木 18:25 | 完全自動、結果を Feishu / Telegram にプッシュ |
| 引け値買い戦略 | 月-金 13:50 | ルールスコアリング + LLM再評価 |
| プレマーケットリスク管理 | 月-金 08:20 | A50 + VIX アラート |
| ストップ高復習 | 月-金 19:25 | 当日騰落率 >= 8% の銘柄を復習 |
| レコメンド追跡リプライシング | 日-木 23:00 | 終値を同期 |
| バックテストグリッド | 毎月1日・15日 04:00 | 18並列パラメータ → 集約レポート |
| DB メンテナンス | 毎日 23:05 | 相場、注文、シグナル、市場シグナルなどのローリングデータをクリーンアップ |

## モデル対応

**CLI**：Gemini / Claude / OpenAI、`/model` でワンタッチ切替。任意の OpenAI 互換エンドポイントに対応（DeepSeek / Qwen / Kimi 等）。

**Web / Pipeline**：1Route / Gemini / OpenAI / Zhipu / Minimax / DeepSeek / Qwen / Volcengine。Kimi などの OpenAI 互換プロバイダーは `base_url` / `custom_providers` で接続できます。

## 設定

**ゼロ設定で利用開始** — 起動後 `/model add` で任意の LLM API Key を追加するだけ。ポートフォリオは自動的にローカル保存。

上級設定（`.env` ファイルまたは GitHub Actions Secrets）：

| 変数 | 説明 | 必須？ |
|------|------|--------|
| LLM API Key | `/model add` で対話式設定 | はい |
| `TUSHARE_TOKEN` | 株式市場データ（`/config set tushare_token`） | はい |
| `SUPABASE_URL` / `SUPABASE_KEY` | クラウドポートフォリオ同期（マルチデバイス） | オプション |
| `TICKFLOW_API_KEY` | TickFlow リアルタイム/分時データ | オプション |
| `FEISHU_WEBHOOK_URL` | Feishu プッシュ | オプション |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram プッシュ | オプション |

> データソース：[TickFlow →](https://tickflow.org/auth/register?ref=5N4NKTCPL4) ｜ LLM API：[1Route →](https://www.1route.dev/register?aff=359904261)

全設定項目と GitHub Actions Secrets の詳細は [アーキテクチャドキュメント](ARCHITECTURE.md) を参照。

## MCP Server

[MCP プロトコル](https://modelcontextprotocol.io/)経由でワイコフ分析機能を公開。Claude Code / Cursor / 任意のMCPクライアントから10個のツールを直接呼び出し可能。

```bash
# MCP依存のインストール
uv pip install youngcan-wyckoff-analysis[mcp]

# Claude Codeへの登録
claude mcp add wyckoff -- wyckoff-mcp
```

または MCP クライアントの設定ファイルに手動追加：

```json
{
  "mcpServers": {
    "wyckoff": {
      "command": "wyckoff-mcp",
      "env": {
        "TUSHARE_TOKEN": "your_token",
        "TICKFLOW_API_KEY": "your_key"
      }
    }
  }
}
```

登録後、Claude Code / Cursor で「000001を診断して」と聞くだけでワイコフツールが呼び出されます。

## Wyckoff Skills

軽量なワイコフ分析機能の再利用：[`YoungCan-Wang/wyckoff_skill`](https://github.com/YoungCan-Wang/wyckoff_skill.git)

AIアシスタントに「ワイコフ視点」を素早く装着するのに最適。

## コミュニティ

| Feishu グループ | QQ グループ | Feishu 個人 |
|:---:|:---:|:---:|
| <img src="../attach/飞书群二维码.png" width="200" /> | <img src="../attach/QQ群二维码.jpg" width="200" /><br/>グループ番号: 761348919 | <img src="../attach/飞书个人二维码.png" width="200" /> |

## スポンサー

役に立ったら Star をお願いします。利益が出たら作者にハンバーガーをおごってください。

| Alipay | WeChat |
|:---:|:---:|
| <img src="../attach/支付宝收款码.jpg" width="200" /> | <img src="../attach/微信收款码.png" width="200" /> |

## リスク警告

> **本ツールは過去の出来高・価格パターンに基づき潜在的な銘柄を発見するものです。過去のパフォーマンスは将来の成果を保証するものではなく、すべてのスクリーニング・推奨・バックテスト結果は投資助言を構成するものではありません。投資はご自身の判断で行ってください。**

## ライセンス

[AGPL-3.0](../LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/WyckoffTradingAgent&type=Date)](https://star-history.com/#YoungCan-Wang/WyckoffTradingAgent&Date)
