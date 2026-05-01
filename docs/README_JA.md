<div align="center">

# Wyckoff Trading Agent

**中国A株ワイコフ出来高分析エージェント -- 自然言語で話しかけると、相場を読み解く**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](../LICENSE)
[![Streamlit](https://img.shields.io/badge/demo-Streamlit-FF4B4B.svg)](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)

[中文](../README.md) | [English](README_EN.md) | [Español](README_ES.md) | [한국어](README_KO.md) | [アーキテクチャ](ARCHITECTURE.md)

</div>

---

自然言語でワイコフの達人と対話しよう。彼は10個の専門ツール + 5個の汎用能力を操り、多段階推論を自律的に連鎖させ、「仕掛けるべきか否か」を教えてくれる。

Web + CLI + MCP の三系統対応、Gemini / Claude / OpenAI / DeepSeek から選択可能、GitHub Actions による完全自動化。

## 機能一覧

| 機能 | 説明 |
|------|------|
| 対話型エージェント | 自然言語で診断・スクリーニング・レポートを起動、LLM がツールを自律編成；ファイル読み書き・コマンド実行・Web取得も可能 |
| スキル | 内蔵スラッシュコマンド（`/screen`、`/checkup`、`/report`、`/strategy`、`/backtest`）でワンタップ複合ワークフロー実行；`~/.wyckoff/skills/*.md` でユーザー拡張可能 |
| 5層ファネルスクリーニング | 全市場 ~4,500 銘柄 → ~30 候補、6チャネル + セクター共鳴 + ミクロ狙撃 |
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

> **推奨：TickFlow接続でリアルタイム/分時データが強化されます**
> 登録：[TickFlow登録リンク](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## クイックスタート

### ワンライナーインストール（推奨）

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/Wyckoff-Analysis/main/install.sh | bash
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

### バックテストグリッド

18 パラメータの並列実行、最適パラメータ・シャープマトリクス・戦略ヘルスチェックを出力：

| 最適パラメータ & ランキング | パラメータマトリクス |
|:---:|:---:|
| <img src="../attach/backtest-grid-1.png" width="450" /> | <img src="../attach/backtest-grid-2.png" width="450" /> |

### Web

```bash
git clone https://github.com/YoungCan-Wang/Wyckoff-Analysis.git
cd Wyckoff-Analysis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

オンラインデモ：**[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

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
| L4 | ミクロ狙撃 | Spring / LPS / SOS / EVR の4大トリガーシグナル |
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
| キャッシュメンテナンス | 毎日 23:05 | 期限切れ相場キャッシュをクリーンアップ |

## モデル対応

**CLI**：Gemini / Claude / OpenAI、`/model` でワンタッチ切替。任意の OpenAI 互換エンドポイントに対応（DeepSeek / Qwen / Kimi 等）。

**Web / Pipeline**：Gemini / OpenAI / DeepSeek / Qwen / Kimi / Zhipu / Volcengine / Minimax -- 計8プロバイダー。

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

## リスク警告

> **本ツールは過去の出来高・価格パターンに基づき潜在的な銘柄を発見するものです。過去のパフォーマンスは将来の成果を保証するものではなく、すべてのスクリーニング・推奨・バックテスト結果は投資助言を構成するものではありません。投資はご自身の判断で行ってください。**

## ライセンス

[AGPL-3.0](../LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/Wyckoff-Analysis&type=Date)](https://star-history.com/#YoungCan-Wang/Wyckoff-Analysis&Date)
