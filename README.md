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

## 채팅에 쓸 AI

기본값은 **ChatGPT 구독**이에요 (`assistant.provider: chatgpt`). OpenAI의 Sign in with ChatGPT로 로그인하면, 이 앱이 내 ChatGPT Plus나 Pro 플랜 사용량으로 답해요. API 키도 추가 요금도 없어요.

1. 웹 화면의 채팅 창에서 'ChatGPT로 로그인'을 눌러요.
2. ChatGPT에서 이 앱(portfolio-manager)이 플랜을 쓰도록 허용해요.
3. `http://127.0.0.1:8765/auth/callback` 으로 돌아오면 끝이에요. 로그인 정보는 `chatgpt-auth.json`에 저장돼요 (본인만 읽을 수 있고, git에 안 올라가요).

알아 둘 점
- OpenAI가 2026년 9월 말에 낸 미리보기 기능이라, 요청 형식이 바뀌면 이 앱도 고쳐야 할 수 있어요.
- Plus나 Pro 플랜에서 돼요. Business, Enterprise, Edu 플랜은 안 돼요.
- 약관상 본인 계정으로, 본인 컴퓨터나 본인만 쓰는 서버에서만 써야 해요. 다른 사람이 내 로그인으로 채팅하게 하면 안 돼요.
- 사용량은 플랜에 포함된 Codex 사용량과 같은 한도에서 빠져요. ChatGPT 설정의 사용량(Usage) 화면에서 이 앱의 주간 한도를 따로 정할 수 있어요.
- 서버에서 돌릴 때는 SSH 터널을 같은 포트(8765)로 열어야 로그인 뒤 화면으로 돌아와요 (`deploy/tunnel.sh`).
- 로그인은 30일 동안 쓰지 않거나 ChatGPT 쪽에서 연결을 끊으면 풀려요. 그러면 채팅 창에 로그인 버튼이 다시 나와요.
- 연결을 끊으려면 채팅 창의 '연결 끊기'를 누르거나, ChatGPT 설정의 Login connections에서 이 앱을 끊으세요.

API 키로 쓰고 싶으면 `config.yaml`의 `assistant.provider`를 바꾸고, 키를 `.env`에 넣어요. API 사용료는 ChatGPT나 claude.ai 구독과 별도로 쓴 만큼 내요.

| provider | 인증 | 비고 |
|---|---|---|
| `chatgpt` (기본) | 채팅 창에서 로그인 | ChatGPT Plus나 Pro 플랜. 기본 모델 `gpt-6.1-sol` |
| `openai` | `OPENAI_API_KEY` | Responses API. 기본 모델 `gpt-6.1-sol` |
| `anthropic` | `ANTHROPIC_API_KEY` | 기본 모델 `claude-sonnet-5-5` |
| `openai-compatible` | `api_key_env`에 적은 이름 | `base_url`과 `model`을 꼭 적어요 |

`openai-compatible` 예시

```yaml
assistant:
  provider: openai-compatible
  base_url: https://generativelanguage.googleapis.com/v1beta/openai   # Gemini
  api_key_env: GEMINI_API_KEY
  model: (Google 문서의 모델 이름)
```

OpenRouter는 `https://openrouter.ai/api/v1`, Ollama는 `http://localhost:11434/v1`(키 없음)을 쓰면 돼요. 채팅이 꺼져 있어도 나머지는 그대로 동작해요.

## 서버(EC2)에 설치

앱은 서버의 127.0.0.1에서만 열리고, 내 컴퓨터에서 SSH 터널로 들어가요. 보안 그룹에 포트를 새로 열 필요가 없어요 (열지 마세요).

내 컴퓨터에서, 이 폴더 안에서:

```bash
bash deploy/push.sh <ssh 호스트>        # 코드를 보내고 서버에서 설치 (다시 실행하면 업데이트)
bash deploy/tunnel.sh <ssh 호스트>      # 터널을 열어 두고
# 브라우저: http://127.0.0.1:8765
```

`<ssh 호스트>`는 평소 `ssh` 뒤에 치는 이름이에요 (`~/.ssh/config`의 별칭이나 `ubuntu@주소`). `ssh -i 키.pem`으로 접속한다면 `SSH_KEY=~/키.pem bash deploy/push.sh ubuntu@주소`처럼 앞에 붙여요 (`tunnel.sh`도 같아요). Python이나 venv 패키지가 없으면 설치 스크립트가 sudo로 깔아요. 설치는 `portfolio-ui`라는 systemd 서비스로 등록돼서, 서버가 다시 켜져도 자동으로 실행돼요. 업데이트할 때 서버의 `config.yaml`, `portfolio.yaml`(저장한 목표), `.env`, ChatGPT 로그인, 실행 기록은 덮어쓰지 않아요.

IBKR를 연결할 때는 서버에서 한 번 더:

```bash
cd ~/portfolio-manager && bash deploy/install-ibkr-gateway.sh
```

Java와 IBKR Client Portal Gateway를 설치하고, 서버 안에서 오는 연결만 받게 설정해요. 터널을 연 상태로 `https://127.0.0.1:5000` 에서 IBKR에 로그인하고, 서버의 `.env`에 `IBKR_ACCOUNT_ID`를, `config.yaml`의 `broker.type`에 `ibkr`를 넣은 뒤 `sudo systemctl restart portfolio-ui` 하면 돼요. IBKR 로그인은 하루에 한 번 이상 끊기니까, 주문하기 전에 다시 로그인하세요. 웹 화면이 켜져 있는 동안은 1분마다 세션을 유지해요.

다른 서비스가 도는 서버에 같이 올려도 돼요. 앱은 `~/portfolio-manager` 폴더와 자기 서비스(`portfolio-ui`, `ibkr-gateway`)만 써요. 메모리 상한(웹 화면 256MB, IBKR Gateway 640MB)과 낮은 CPU 우선순위로 돌고, 메모리가 모자라면 다른 서비스보다 먼저 멈춰요. 웹 화면은 평소 60MB 정도 써요.

로그 보기: `sudo journalctl -u portfolio-ui -f`

## 예상 수수료

주문 계획에 종목별 예상 수수료를 보여 주고, 매수 수량을 정할 때 수수료만큼 현금을 미리 빼 둬요. 기본값은 IBKR Pro **Tiered** 요금제, 런던거래소(LSE) 달러 거래 기준이에요 (2026년 10월 IBKR 싱가포르 공식 표).

| 요금제 | 수수료 | 1,000불 주문 예상 |
|---|---|---|
| Fixed | 거래 금액의 0.05%, 최소 4.00불 | 약 4.36불 (GST 9% 포함) |
| Tiered | 0.05%, 최소 1.70불, 최대 39불 + 거래소와 청산 수수료 약 0.21불 | 약 2.06불 |

주문 하나가 약 7,000불보다 작으면 Tiered가 싸요. 이 앱은 주문 하나를 5,000불 이하로 막아서 늘 Tiered가 싸요. IBKR 계좌가 Fixed라면 `config.yaml`의 `fees`를 `min_per_order: 4.00`, `max_per_order: 0`, `extra_per_order: 0`으로 바꾸세요. 실제 요금제는 IBKR 설정에서 바꿀 수 있어요. 매달 SGD를 USD로 바꿀 때 드는 IBKR 환전 수수료(0.2bp, 최소 2불)도 계획 화면에 따로 적어 줘요.

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

1. IBKR Pro 계좌를 열고 입금해요. IBKR Lite는 API를 쓸 수 없어요. Pro는 최소 잔고와 계좌 유지비가 없고, 거래할 때 수수료만 내요. 모의계좌도 실계좌가 열려 있고 입금돼 있어야 API로 쓸 수 있어요.
2. IBKR에서 Client Portal Gateway를 내려받아 실행해요. 압축을 푼 폴더에서 `bin/run.sh root/conf.yaml` (Windows는 `bin\run.bat root\conf.yaml`).
3. 브라우저로 https://localhost:5000 에 들어가 로그인해요. 세션이 끊기면 다시 로그인하면 돼요.
4. 계좌번호를 `.env`에 넣어요: `IBKR_ACCOUNT_ID=DU1234567` (모의계좌는 `DU`로 시작해요).
5. 웹 화면을 `--broker ibkr`로 켜고, 종목을 펼쳐 'IBKR에서 conid 찾기'로 LSE 달러 라인을 골라 저장해요. 터미널에서는 `python -m rebalancer resolve`로 후보를 볼 수 있어요.
6. 모의계좌로 먼저 주문해 보고, 그다음 실계좌로 옮겨요. 실계좌면 화면 위에 빨간 점과 'IBKR 실계좌'가 표시되고, 보내기 버튼에도 '실계좌로'가 붙어요.

실시간 시세를 받으려면 IBKR에서 LSE UK (L1) 시세를 구독하세요 (비전문가 월 GBP 1, 2026년 10월 기준). 구독하지 않으면 15분 지연 시세가 오거나 비어 있을 수 있어요. 보유 중인 종목의 시세가 없으면 계획을 멈춰요.

## 언제 실행하나

주문이 당일 지정가라서 LSE 거래 시간에 실행하세요. 싱가포르 시간으로 영국 서머타임(3월 말~10월 말)엔 오후 3시~밤 11시 30분, 그 외 기간엔 오후 4시~밤 12시 30분이에요.

## 알려진 한계 (v0.2)

- IBKR 연동은 공식 문서 기준으로 작성하고 가짜 응답으로 테스트했어요. 실제 Gateway에는 아직 붙여 보지 않았으니 꼭 모의계좌로 먼저 확인하세요.
- 채팅의 ChatGPT 플랜, OpenAI, 호환 서버 연동도 공식 문서 기준으로 작성하고 가짜 응답으로 테스트했어요. ChatGPT 플랜 사용은 OpenAI의 미리보기 기능이라 요청 형식이 바뀔 수 있어요.
- 서버 설치 스크립트는 Ubuntu와 Amazon Linux 기준이에요.
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
  llm.py                 AI 공급자 연결 (ChatGPT 플랜, OpenAI, Anthropic, OpenAI 호환)
  chatgpt_auth.py        Sign in with ChatGPT 로그인과 토큰 관리
  report.py              터미널 출력
  state.py               실행 기록과 목표 변경 journal
  envfile.py             .env 읽기
  brokers/mock.py        예시 파일로 동작하는 모의 브로커
  brokers/ibkr.py        Client Portal Gateway 연동
deploy/                  서버 설치 (push, install, IBKR Gateway, 터널)
tests/                   단위 테스트
```
