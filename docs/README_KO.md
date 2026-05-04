<div align="center">

# Wyckoff Trading Agent

**A주 와이코프 거래량-가격 분석 AI 에이전트 -- 자연어로 말하면, 차트를 읽어줍니다**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](../LICENSE)
[![Web App](https://img.shields.io/badge/Web-React%20App-0ea5e9.svg)](https://wyckoff-analysis.pages.dev/home)
[![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B.svg)](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)

[中文](../README.md) | [English](README_EN.md) | [日本語](README_JA.md) | [Español](README_ES.md) | [아키텍처](ARCHITECTURE.md)

</div>

---

자연어로 와이코프 전문가와 대화하세요. 10가지 전문 도구 + 5가지 범용 능력을 자율적으로 조합하여 다단계 추론을 수행하고, "매수할 것인가, 관망할 것인가"에 대한 결론을 제시합니다.

Web + CLI + MCP 트리플 채널, Gemini / Claude / OpenAI / DeepSeek 멀티 모델 전환, GitHub Actions 자동화 스케줄링 지원.

## 기능 개요

| 기능 | 설명 |
|------|------|
| 대화형 에이전트 | 자연어로 진단, 스크리닝, 리서치 리포트를 실행하며, LLM이 도구를 자율적으로 편성; 파일 읽기/쓰기, 명령 실행, 웹 가져오기도 가능 |
| 스킬 | 내장 슬래시 명령(`/screen`, `/checkup`, `/report`, `/strategy`, `/backtest`)으로 원탭 복합 워크플로우 실행; `~/.wyckoff/skills/*.md`로 사용자 확장 가능 |
| 5단계 퍼널 스크리닝 | 전체 시장 ~4,500종목 → ~30 후보군, 6채널 + 섹터 공명 + 미시 저격 |
| AI 3진영 리포트 | 논리 파산 / 비축 진영 / 도약대, LLM이 독립적으로 판단 |
| 포트폴리오 진단 | 일괄 건강 검진: 이동평균 구조, 매집 단계, 트리거 시그널, 손절 상태 |
| 개인 리밸런싱 | 보유 종목 + 후보를 종합하여 EXIT/TRIM/HOLD/PROBE/ATTACK 지시 출력, Telegram 푸시 |
| 장 마감 매수 전략 | 13:50에 실행, 규칙 점수 + LLM 재평가 2단계로 장 마감 진입 대상 선별 |
| 시그널 확인 풀 | L4 트리거 시그널이 1-3일 가격 확인 후에만 실행 가능 |
| 추천 추적 | 과거 추천 종목의 종가 자동 동기화, 누적 수익률 계산 |
| 일봉 백테스트 | 퍼널 적중 후 N일 수익률 재생, 승률/Sharpe/최대 낙폭 출력 |
| 장전 리스크 관리 | A50 + VIX 모니터링, 4단계 경보 푸시 |
| 로컬 대시보드 | `wyckoff dashboard` — 추천, 시그널, 포트폴리오, Agent 기억, 대화 로그; 다크/라이트 테마, 중/영 이중 언어 |
| Agent 기억 | 세션 간 기억: 대화 결론 자동 추출, 다음 질의 시 관련 컨텍스트 주입 |
| 컨텍스트 압축 | 동적 임계값(모델 context window의 25%)으로 자동 압축, 도구 결과 스마트 요약으로 핵심 데이터 보존 |
| 도구 확인 | `exec_command`, `write_file`, `update_portfolio`는 실행 전 사용자 승인 필요 |
| 범용 Agent 능력 | 명령 실행, 파일 읽기/쓰기, 웹 가져오기 — CSV 경로를 보내면 즉시 분석 |
| MCP Server | MCP 프로토콜로 10가지 도구 공개, Claude Code / Cursor / 모든 MCP 클라이언트 지원 |
| 다채널 알림 | Feishu / WeCom / DingTalk / Telegram |

## 데이터 소스

개별 종목 일봉 데이터 자동 폴백:

```
tickflow → tushare → akshare → baostock → efinance
```

어느 소스든 사용 불가 시 자동 전환되며, 별도 개입이 필요 없습니다.

> **권장: TickFlow 연결로 실시간/분봉 데이터 강화**
> 등록: [TickFlow 등록 링크](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## 빠른 시작

### 원라인 설치 (권장)

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/Wyckoff-Analysis/main/install.sh | bash
```

Python 감지, uv 설치, 격리 환경 생성을 자동으로 수행합니다. 완료 후 `wyckoff`로 실행.

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

### 시작하기 — 원클릭 Agent 설정

실행 후 두 단계만:
1. `/model` — 모델 선택 (Gemini / Claude / OpenAI), API Key 입력
2. 질문 입력으로 대화 시작 — 회원가입 불필요, 포트폴리오 데이터는 로컬 저장

```
> 000001과 600519 중 어느 것이 더 매수 가치가 있는지 분석해줘
> 내 포트폴리오를 진단해줘
> 시장 온도가 어때?
```

> 선택사항: `/login`으로 클라우드 동기화(멀티 디바이스). 로그인 없이도 모든 기능 사용 가능.

업데이트: `wyckoff update`

### 백테스트 그리드

18개 파라미터 병렬 실행, 최적 파라미터, Sharpe 매트릭스, 전략 건강 체크 출력:

| 최적 파라미터 & 순위 | 파라미터 매트릭스 |
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

Web App: **[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/home)**

Streamlit: **[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

## 도구

에이전트의 무기고 — 10가지 정량 도구 + 5가지 범용 능력:

| 도구 | 기능 |
|------|------|
| `search_stock_by_name` | 종목명 / 코드 / 병음 퍼지 검색 |
| `analyze_stock` | 와이코프 진단 / 최근 OHLCV 시세 (모드 전환) |
| `portfolio` | 보유 목록 조회 / 일괄 포트폴리오 진단 (모드 전환) |
| `update_portfolio` | 보유 추가 / 수정 / 삭제, 가용 현금 설정, 추적 기록 삭제 |
| `get_market_overview` | 시장 온도 개요 |
| `screen_stocks` | 5단계 퍼널 전체 시장 스크리닝 (⚡백그라운드) |
| `generate_ai_report` | 3진영 AI 심층 리포트 (⚡백그라운드) |
| `generate_strategy_decision` | 보유 종목 유지/매도 + 신규 매수 의사결정 (⚡백그라운드) |
| `query_history` | 과거 추천 / 시그널 풀 / 장 마감 매수 이력 조회 |
| `run_backtest` | 퍼널 전략 과거 백테스트 (⚡백그라운드) |
| `check_background_tasks` | 백그라운드 작업 진행 조회 |
| `exec_command` | 로컬 셸 명령 실행 |
| `read_file` | 로컬 파일 읽기 (CSV/Excel 자동 파싱) |
| `write_file` | 파일 쓰기 (리포트/데이터 내보내기) |
| `web_fetch` | 웹 콘텐츠 가져오기 (금융 뉴스/공시) |

도구 호출 순서와 횟수는 LLM이 실시간으로 결정하며, 사전 편성이 필요 없습니다. CSV 경로를 보내면 읽고, "패키지 설치해줘"라고 하면 실행합니다.

## 5단계 퍼널

| 단계 | 명칭 | 기능 |
|------|------|------|
| L1 | 불량 종목 제거 | ST / BSE / STAR 시장 제외, 시가총액 >= 35억, 일평균 거래대금 >= 5천만 |
| L2 | 6채널 선별 | 주상승 / 점화 / 잠복 / 매집 / 극저량 / 호가방어 |
| L3 | 섹터 공명 | 업종 Top-N 분포 필터링 |
| L4 | 미시 저격 | Spring / LPS / SOS / EVR 4대 트리거 시그널 |
| L5 | AI 심판 | LLM 3진영 분류: 논리 파산 / 비축 / 도약대 |

## 일일 자동화

리포지토리에 내장된 GitHub Actions 스케줄 작업:

| 작업 | 시간 (베이징) | 설명 |
|------|--------------|------|
| 퍼널 스크리닝 + AI 리포트 + 개인 결정 | 일-목 18:25 | 완전 자동, Feishu/Telegram 푸시 |
| 장 마감 매수 전략 | 월-금 13:50 | 규칙 점수 + LLM 재평가 |
| 장전 리스크 관리 | 월-금 08:20 | A50 + VIX 경보 |
| 상한가 복기 | 월-금 19:25 | 당일 상승률 >= 8% 복기 |
| 추천 추적 가격 갱신 | 일-목 23:00 | 종가 동기화 |
| 백테스트 그리드 | 매월 1일, 15일 04:00 | 18개 병렬 파라미터 → 종합 리포트 |
| DB 유지보수 | 매일 23:05 | 시세, 주문, 신호, 시장 신호 등 롤링 윈도우 데이터를 정리 |

## 모델 지원

**CLI**: Gemini / Claude / OpenAI, `/model`로 원클릭 전환, 임의의 OpenAI 호환 엔드포인트 지원 (DeepSeek / Qwen / Kimi 등).

**Web / Pipeline**: 1Route / Gemini / OpenAI / Zhipu / Minimax / DeepSeek / Qwen / Volcengine. Kimi 등 OpenAI 호환 제공사는 `base_url` / `custom_providers`로 연결할 수 있습니다.

## 설정

**제로 설정으로 시작** — 실행 후 `/model add`로 아무 LLM API Key만 추가하면 됩니다. 포트폴리오 데이터는 자동으로 로컬 저장.

고급 설정 (`.env` 파일 또는 GitHub Actions Secrets):

| 변수 | 설명 | 필수? |
|------|------|-------|
| LLM API Key | `/model add`로 대화형 설정 | 예 |
| `TUSHARE_TOKEN` | 주식 시장 데이터 (`/config set tushare_token`) | 예 |
| `SUPABASE_URL` / `SUPABASE_KEY` | 클라우드 포트폴리오 동기화 (멀티 디바이스) | 선택 |
| `TICKFLOW_API_KEY` | TickFlow 실시간/분봉 데이터 | 선택 |
| `FEISHU_WEBHOOK_URL` | Feishu 푸시 | 선택 |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram 푸시 | 선택 |

전체 설정 항목 및 GitHub Actions Secrets 설명은 [아키텍처 문서](ARCHITECTURE.md)를 참조하세요.

## MCP Server

[MCP 프로토콜](https://modelcontextprotocol.io/)을 통해 와이코프 분석 기능을 노출하여 Claude Code / Cursor / 모든 MCP 클라이언트에서 10가지 도구를 직접 호출할 수 있습니다.

```bash
# MCP 의존성 설치
uv pip install youngcan-wyckoff-analysis[mcp]

# Claude Code에 등록
claude mcp add wyckoff -- wyckoff-mcp
```

또는 MCP 클라이언트 설정 파일에 수동 추가:

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

등록 후 Claude Code / Cursor에서 "000001 진단해줘"라고 물어보면 와이코프 도구가 호출됩니다.

## Wyckoff Skills

경량 와이코프 분석 기능 재사용: [`YoungCan-Wang/wyckoff_skill`](https://github.com/YoungCan-Wang/wyckoff_skill.git)

AI 어시스턴트에 빠르게 "와이코프 시각"을 장착하기에 적합합니다.

## 위험 경고

> **이 도구는 과거 거래량-가격 패턴을 기반으로 잠재적 종목을 발견합니다. 과거 실적이 미래 수익을 보장하지 않으며, 모든 스크리닝, 추천, 백테스트 결과는 투자 조언을 구성하지 않습니다. 투자는 본인의 판단하에 진행하십시오.**

## License

[AGPL-3.0](../LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/Wyckoff-Analysis&type=Date)](https://star-history.com/#YoungCan-Wang/Wyckoff-Analysis&Date)
