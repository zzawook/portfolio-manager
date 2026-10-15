# 포트폴리오 리밸런서 (IBKR)

코어 65% + 위성 35% 목표 비중을 지켜 주는 도구예요. 매달 들어온 현금으로 모자란 종목부터 사고, 비중이 많이 벗어났거나 1년이 되면 매도까지 포함해서 맞춰요. 주문은 항상 계획을 먼저 보여 주고, 사람이 직접 확인해야 나가요.

쓰는 방법은 두 가지예요.

- **웹 화면** (`rebalancer ui`): 도넛 차트로 지금 비중을 보고, 목표 비중을 고치고, AI 채팅으로 초안을 바꾸고, 버튼으로 주문해요.
- **터미널** (`rebalancer plan`, `rebalancer execute`): 같은 계산을 글자로 보여 주고 `yes`를 입력하면 주문해요.

## 설치

Python 3.10 이상이 필요해요.

```bash
git clone https://github.com/zzawook/portfolio-manager.git
cd portfolio-manager
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest            # 단위 테스트
```

## 웹 화면

```bash
python -m rebalancer ui                                           # 모의 계좌 (examples/month01.yaml)
python -m rebalancer ui --mock-file examples/month14.yaml --today 2028-02-03   # 리밸런싱 예시
python -m rebalancer ui --broker ibkr                             # IBKR 계좌
```

브라우저가 `http://127.0.0.1:8765` 로 열려요. 화면은 이렇게 생겼어요.

- **지금 비중**: 도넛 차트예요. 바깥 고리는 종목, 안쪽 고리는 코어와 위성이에요. 칸을 누르면 그 칸 안의 세부 비중으로 들어가고, 종목을 누르면 아래 목록에서 그 줄을 찾아 줘요.
- **목표 비중**: 종목마다 지금 비중(막대)과 목표(눈금)를 같이 보여 줘요. 숫자를 바꾸면 바로 초안에 반영돼요. 펼치면 설명, 위성의 이유와 틀렸다고 볼 조건, 상품 정보, IBKR conid를 고칠 수 있어요. 종목 추가와 빼기도 여기서 해요.
- **초안**: 저장하기 전의 변경은 점선(연필)으로 표시돼요. 아래 막대에서 되돌리기, 목표 저장, 주문 계획 보기를 해요. 저장하면 `portfolio.yaml`을 새로 쓰고, 이전 파일은 `history/` 폴더에 남겨요.
- **주문 계획**: 주문 목록, 종목별 예상 수수료, 주문 후 비중을 보여 줘요. '주문 보내기'를 눌러야만 주문이 나가요. IBKR가 확인을 요청하면 화면에 띄우고, 승인할지 직접 정하게 해요. 리밸런싱은 매도를 먼저 보내고, 체결을 확인한 뒤 그때 현금으로 매수 계획을 다시 계산해서 한 번 더 확인받아요.
- **채팅**: "방산을 5%로 줄이고 그만큼 VWRA에 넣어 줘"처럼 말하면 AI가 초안을 고쳐요. 채팅은 초안만 바꿀 수 있고, 주문을 보내는 기능은 아예 없어요.

화면은 내 컴퓨터(127.0.0.1)에서만 열려요. 실행할 때마다 새 세션 키를 만들어서, 다른 웹사이트가 이 화면의 API를 부를 수 없게 막아요.

## 채팅에 쓸 AI 키

키는 `config.yaml`에 적지 말고, 프로젝트 폴더의 `.env` 파일이나 환경 변수에 넣어요. `.env`는 `.gitignore`에 들어 있어요.

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

다른 회사 키를 쓰려면 `config.yaml`의 `assistant`를 바꿔요.

| provider | 키 변수 | 비고 |
|---|---|---|
| `anthropic` (기본) | `ANTHROPIC_API_KEY` | 기본 모델 `claude-sonnet-5-5` |
| `openai` | `OPENAI_API_KEY` | Responses API를 써요. 기본 모델 `gpt-6.1-sol` |
| `openai-compatible` | `api_key_env`에 적은 이름 | `base_url`과 `model`을 꼭 적어요 |

`openai-compatible` 예시

```yaml
assistant:
  provider: openai-compatible
  base_url: https://generativelanguage.googleapis.com/v1beta/openai   # Gemini
  api_key_env: GEMINI_API_KEY
  model: (Google 문서의 모델 이름)
```

OpenRouter는 `https://openrouter.ai/api/v1`, 내 컴퓨터의 Ollama는 `http://localhost:11434/v1`(키 없음)을 쓰면 돼요. 모델 이름은 각 회사의 모델 목록에서 확인하세요. API 사용료는 claude.ai나 ChatGPT 구독과 별도로, 쓴 만큼 각 회사에 내요. 키가 없으면 채팅만 꺼지고 나머지는 그대로 동작해요.

## 예상 수수료

주문 계획에 종목별 예상 수수료를 보여 주고, 매수 수량을 정할 때 수수료만큼 현금을 미리 빼 둬요. 기본값은 IBKR Pro **Fixed** 요금제, 런던거래소(LSE) 달러 거래 기준이에요 (2026년 10월 IBKR 싱가포르 공식 표).

| 요금제 | 수수료 | 1,000불 주문 예상 |
|---|---|---|
| Fixed | 거래 금액의 0.05%, 최소 4.00불 | 약 4.36불 (GST 9% 포함) |
| Tiered | 0.05%, 최소 1.70불, 최대 39불 + 거래소와 청산 수수료 약 0.21불 | 약 2.06불 |

주문 하나가 약 7,000불보다 작으면 Tiered가 싸요. Tiered를 쓰면 `config.yaml`의 `fees`에서 `min_per_order: 1.70`, `max_per_order: 39`, `extra_per_order: 0.21`로 바꾸세요. 실제 요금제는 IBKR 설정에서 바꿀 수 있어요. 매달 SGD를 USD로 바꿀 때 드는 IBKR 환전 수수료(0.2bp, 최소 2불)도 계획 화면에 따로 적어 줘요.

예상치일 뿐이에요. 실제 금액은 IBKR 체결 내역에서 확인하세요.

## 터미널로 쓰기

```bash
python -m rebalancer check                                    # 설정이 규칙에 맞는지 확인
python -m rebalancer plan --today 2027-01-05                  # 첫 달 예시 (현금 3,000불)
python -m rebalancer plan --mock-file examples/month14.yaml --today 2028-02-03   # 리밸런싱 예시
python -m rebalancer execute --today 2027-01-05               # 모의 실행 연습
```

`examples/`의 가격은 계산을 보여 주기 위한 예시예요. 실제 시세가 아니에요.
모의 실행 기록은 `state.mock.json`에 따로 남아서, 실제 계좌 실행을 막지 않아요.

## 동작 규칙

| 상황 | 하는 일 |
|---|---|
| 평소 (정기 적립) | 목표보다 가장 모자란 종목부터 최대 3종목, 1주 단위로 매수. 매도 없음 |
| 한 종목이 목표보다 5%p 이상 많을 때 | 매도 포함 리밸런싱 |
| 매년 1월 | 매도 포함 리밸런싱 (1년에 한 번) |
| 투자 금액이 2만 불 미만일 때 | 매도 없이 적립으로만 비중을 맞춤 |
| 목표에서 뺀 종목 (정리 대상) | 다시 사지 않고, 리밸런싱 때 전부 팔아요 |

숫자는 전부 `config.yaml`의 `rules`에서 바꿀 수 있어요. 목표 비중과 종목은 `portfolio.yaml`에 있어요.

## 안전장치

- 계획을 보여 주고, 웹에서는 '주문 보내기' 버튼을, 터미널에서는 `yes`를 입력해야 주문이 나가요.
- 채팅에는 주문 기능이 없어요. 초안만 고칠 수 있어요.
- 같은 달에 두 번 주문하면 막아요. 꼭 필요할 때만 화면의 체크박스나 `--force`로 풀어요.
- 목록에 있는 종목 외에는 주문하지 않아요.
- 1회 주문 한도(기본 5,000불)를 넘는 주문은 막아요.
- 보유 수량보다 많이 팔지 않고, 매수 합계는 수수료까지 쓸 수 있는 현금 안에서만 잡아요.
- 증권사가 보내는 경고는 하나씩 직접 승인해야 해요. 15분 안에 답하지 않으면 그 주문은 보내지 않아요.
- 위성 종목은 이유와 틀렸다고 볼 조건이 없으면 저장도 주문도 안 돼요. 아이디어당 상한은 15%예요.
- 목표를 바꾸면 다음 실행 때 변경 내역이 `state.json`의 `journal`에 남아요.
- 실행 기록, `.env`, `history/`는 `.gitignore`에 들어 있어서 저장소에 올라가지 않아요.

## IBKR 계좌에 연결하기

1. IBKR Pro 계좌를 열고 입금해요. 모의계좌도 실계좌가 열려 있고 입금돼 있어야 API로 쓸 수 있어요.
2. IBKR에서 Client Portal Gateway를 내려받아 실행해요. 압축을 푼 폴더에서 `bin/run.sh root/conf.yaml` (Windows는 `bin\run.bat root\conf.yaml`).
3. 브라우저로 https://localhost:5000 에 들어가 로그인해요. 세션이 끊기면 다시 로그인하면 돼요.
4. 계좌번호를 `.env`에 넣어요: `IBKR_ACCOUNT_ID=DU1234567` (모의계좌는 `DU`로 시작해요).
5. 웹 화면을 `--broker ibkr`로 켜고, 종목을 펼쳐 'IBKR에서 conid 찾기'로 LSE 달러 라인을 골라 저장해요. 터미널에서는 `python -m rebalancer resolve`로 후보를 볼 수 있어요.
6. 모의계좌로 먼저 주문해 보고, 그다음 실계좌로 옮겨요. 실계좌면 화면 위에 빨간 점과 'IBKR 실계좌'가 표시되고, 보내기 버튼에도 '실계좌로'가 붙어요.

시세 스냅샷은 시세 구독 상태에 따라 지연 시세가 오거나 비어 있을 수 있어요. 보유 중인 종목의 시세가 없으면 계획을 멈춰요.

## 언제 실행하나

주문이 당일 지정가라서 LSE 거래 시간에 실행하세요. 싱가포르 시간으로 영국 서머타임(3월 말~10월 말)엔 오후 3시~밤 11시 30분, 그 외 기간엔 오후 4시~밤 12시 30분이에요.

## 알려진 한계 (v0.2)

- IBKR 연동은 공식 문서 기준으로 작성하고 가짜 응답으로 테스트했어요. 실제 Gateway에는 아직 붙여 보지 않았으니 꼭 모의계좌로 먼저 확인하세요.
- 채팅의 OpenAI와 호환 서버 연동도 공식 문서 기준으로 작성하고 가짜 응답으로 테스트했어요.
- 1주 단위로만 계산해요. 소수점 매수는 하지 않아요.
- 지정가는 센트 단위로 맞춰요. 호가 단위가 안 맞아 거부되면 다음 버전에서 IBKR 호가 규칙을 읽게 하면 돼요.
- USD 한 통화만 지원해요. SGD를 USD로 바꾸는 건 IBKR에서 직접 하세요.
- 수수료는 LSE 기준 예상치예요. 다른 거래소 종목은 같은 공식으로 어림해요.

## 파일 구조

```
config.yaml              규칙, 브로커, 수수료, 채팅 설정
portfolio.yaml           목표 비중과 종목 (웹 화면에서 저장하면 바뀌어요)
examples/                계좌 없이 연습하는 예시 잔고
rebalancer/
  config.py              설정 읽기와 규칙 검사
  portfolio.py           초안 편집, 저장, 변경 내역
  planner.py             주문 계획 계산 (적립 / 리밸런싱, 수수료 포함)
  execute.py             터미널 실행: 안전 점검, 확인, 주문 전송, 기록
  jobs.py                웹 실행: 버튼으로 시작한 주문, 경고 승인 대기, 2단계 리밸런싱
  server.py              웹 서버 (127.0.0.1, 세션 키)
  static/                웹 화면 (HTML, CSS, JS)
  assistant.py           채팅: 초안만 고치는 도구들
  llm.py                 AI 공급자 연결 (Anthropic, OpenAI, OpenAI 호환)
  report.py              터미널 출력
  state.py               실행 기록과 목표 변경 journal
  envfile.py             .env 읽기
  brokers/mock.py        예시 파일로 동작하는 모의 브로커
  brokers/ibkr.py        Client Portal Gateway 연동
tests/                   단위 테스트
```
