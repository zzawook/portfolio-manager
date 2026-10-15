# 포트폴리오 리밸런서 (IBKR)

코어 65% + 위성 35% 목표 비중을 지켜 주는 작은 CLI예요. 매달 들어온 현금으로 모자란 종목부터 사고, 비중이 많이 벗어났거나 1년이 되면 매도까지 포함해서 맞춰요. 주문은 항상 계획을 먼저 보여 주고, `yes`를 입력해야 나가요.

## 설치

Python 3.10 이상이 필요해요.

```bash
git clone https://github.com/zzawook/portfolio-manager.git
cd portfolio-manager
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest            # 단위 테스트
```

## 계좌 없이 바로 해 보기

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

숫자는 전부 `config.yaml`의 `rules`에서 바꿀 수 있어요. 리밸런싱은 매도만 먼저 보내고, 체결을 확인한 뒤 그때 현금으로 매수 계획을 다시 계산해서 한 번 더 확인받아요.

## 안전장치

- 계획을 보여 주고 `yes`를 입력해야 주문이 나가요.
- 같은 달에 두 번 실행하면 막아요. 꼭 필요할 때만 `--force`를 붙이세요.
- 설정에 있는 6종목 외에는 주문하지 않아요.
- 1회 주문 한도(기본 5,000불)를 넘는 주문은 막아요.
- 보유 수량보다 많이 팔지 않아요.
- 증권사가 보내는 경고 메시지는 하나씩 직접 승인해야 해요. 자동 승인은 없어요.
- 위성 종목은 `thesis`(이유)와 `invalidation`(틀렸다고 볼 조건)이 없으면 실행을 거부해요. 아이디어당 상한은 15%예요.
- 목표 비중을 바꾸면 다음 실행 때 변경 내역이 `state.json`의 `journal`에 남아요.
- 실행 기록(`state.json`, `state.mock.json`)은 `.gitignore`에 들어 있어서 저장소에 올라가지 않아요.

## IBKR 계좌에 연결하기

1. IBKR Pro 계좌를 열고 입금해요. 모의계좌도 실계좌가 열려 있고 입금돼 있어야 API로 쓸 수 있어요.
2. IBKR에서 Client Portal Gateway를 내려받아 실행해요. 압축을 푼 폴더에서 `bin/run.sh root/conf.yaml` (Windows는 `bin\run.bat root\conf.yaml`).
3. 브라우저로 https://localhost:5000 에 들어가 로그인해요. 세션이 끊기면 다시 로그인하면 돼요.
4. 계좌번호를 환경 변수로 넣어요: `export IBKR_ACCOUNT_ID=DU1234567` (모의계좌는 `DU`로 시작해요). `config.yaml`의 `broker.ibkr.account_id`에 적어도 되지만, 환경 변수로 두면 git에 올라가지 않아요.
5. `python -m rebalancer resolve`로 종목별 후보를 보고, LSE 달러 라인의 conid를 `instruments`에 채워요.
6. `python -m rebalancer plan --broker ibkr`로 실제 잔고 기준 계획을 확인해요.
7. 모의계좌로 `python -m rebalancer execute --broker ibkr`를 먼저 해 보고, 그다음 실계좌로 옮겨요.

시세 스냅샷은 시세 구독 상태에 따라 지연 시세가 오거나 비어 있을 수 있어요. 보유 중인 종목의 시세가 없으면 계획을 멈춰요.

## 언제 실행하나

주문이 당일 지정가라서 LSE 거래 시간에 실행하세요. 싱가포르 시간으로 영국 서머타임(3월 말~10월 말)엔 오후 3시~밤 11시 30분, 그 외 기간엔 오후 4시~밤 12시 30분이에요.

## 알려진 한계 (v0.1)

- IBKR 연동은 공식 문서 기준으로 작성하고 가짜 응답으로 테스트했어요. 실제 Gateway에는 아직 붙여 보지 않았으니 꼭 모의계좌로 먼저 확인하세요.
- 1주 단위로만 계산해요. 소수점 매수는 하지 않아요.
- 지정가는 센트 단위로 맞춰요. 종목의 호가 단위와 안 맞아서 거부되면, IBKR의 호가 규칙을 읽어 맞추는 기능을 다음 버전에 넣으면 돼요.
- 수수료는 예비금(`cash_reserve`)으로만 감안해요.
- USD 한 통화만 지원해요. SGD를 USD로 바꾸는 건 IBKR에서 직접 하세요.
- 매도 체결은 직접 확인하고 Enter를 눌러야 다음 단계로 넘어가요.

## 파일 구조

```
config.yaml              목표 비중과 규칙
examples/                계좌 없이 연습하는 예시 잔고
rebalancer/
  config.py              설정 읽기와 규칙 검사
  planner.py             주문 계획 계산 (적립 / 리밸런싱)
  execute.py             안전 점검, 확인, 주문 전송, 기록
  report.py              계획 출력
  state.py               실행 기록과 목표 변경 journal
  brokers/mock.py        예시 파일로 동작하는 모의 브로커
  brokers/ibkr.py        Client Portal Gateway 연동
tests/                   단위 테스트
```
