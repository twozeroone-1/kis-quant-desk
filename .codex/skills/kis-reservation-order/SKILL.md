---
name: kis-reservation-order
description: "KIS 예약주문을 접수·조회·취소·정정할 때 반드시 사용. '예약주문',
  '예약 매수', '예약 매도', '장전 예약', '미국장 예약', '내일 장 시작에 주문',
  'MOO 예약', '예약주문 취소', '예약주문 조회'라고 할 때 자동 실행된다.
  모의(vps)는 Strategy Builder 앱 레벨 예약(reservation_source=app)만 사용하고,
  실전(prod)은 KIS 브로커 예약(reservation_source=broker)만 사용한다. 실전투자
  예약주문 전에는 종목·수량·가격·예상금액·모드를 표시하고 반드시 사용자 확인을 받는다."
---

# KIS 예약주문

## Purpose

국내/미국 주식 예약주문을 접수하거나, 예약주문 상태를 조회·취소·정정한다.
모의투자(`vps`, 8081)는 KIS 브로커 예약주문을 쓰지 않고 Strategy Builder 앱 레벨 예약만 사용한다.
실전투자(`prod`, 8083)는 KIS 브로커 예약주문만 사용한다.
일반 주문 실행과 다르게 예약주문은 모드별 실행 주체와 지원 방식이 다르므로 이 스킬을 우선 적용한다.

## Safety Rules

- `appkey`, `appsecret`, 토큰, 계좌번호 원문을 출력하지 않는다.
- `~/KIS/config/kis_devlp.yaml`은 사용자 요청 없이 읽거나 수정하지 않는다.
- 기본 모드는 모의투자(`vps`)다. 실전은 `/auth prod`로 명시 전환된 경우만 사용한다.
- 모의(`vps`, 8081) 예약주문은 반드시 `reservation_source: "app"`을 사용한다. 브로커 예약(`reservation_source: "broker"`)으로 접수·조회·취소·정정하지 않는다.
- 모의 앱 예약은 KIS 서버 예약이 아니라 Strategy Builder 서버가 `scheduled_at` KST 시각에 일반 주문을 제출하는 앱 레벨 예약이다. 서버, 인증, 네트워크 상태에 의존한다는 한계를 설명한다.
- 실전(`prod`, 8083) 예약주문은 반드시 `reservation_source: "broker"`를 사용한다. 앱 레벨 예약은 실전에 사용하지 않는다.
- 실전(`prod`) 예약주문 접수·취소·정정 전에는 종목, 매수/매도, 수량, 가격, 예상금액, 모드를 표시하고 사용자 확인을 받아야 한다.
- 실전 API 요청에는 `confirm_prod: true`가 필요하다. 확인이 없으면 요청하지 않는다.
- 예약매도는 보유수량을 먼저 조회하고, 보유수량이 부족하면 접수하지 않는다.
- 사용자가 종목을 지정하지 않았으면 바로 예약주문하지 않는다. 후보군 신호 확인 또는 종목 선택을 먼저 진행한다.
- 모의 앱 예약 정정은 지원하지 않는다. 취소 후 재등록으로 처리한다.
- 모의 앱 예약에서 지원하지 않는 주문방식(`preopen`, `moo`)은 브로커 경로로 우회하지 말고 지원 불가로 안내한다.
- 예약주문은 접수 시점에 증거금·잔고를 브로커가 최종 보장하지 않을 수 있다. 매수 가능금액과 매도 가능수량은 앱에서 먼저 확인한다.

## Supported Orders

| 모드 | 소스 | 시장 | 접수 | 조회 | 취소 | 정정 |
|------|------|------|------|------|------|------|
| 모의 `vps` | 앱 `app` | 국내 `domestic` | 지정가 `limit`, 시장가 `market` + `scheduled_at` | 앱 상태 조회 | 앱 예약 취소 | 미지원. 취소 후 재등록 |
| 모의 `vps` | 앱 `app` | 미국 `us` | 지정가 `limit` + `scheduled_at` | 앱 상태 조회 | 앱 예약 취소 | 미지원. 취소 후 재등록 |
| 실전 `prod` | 브로커 `broker` | 국내 `domestic` | 지정가 `limit`, 시장가 `market`, 장전 `preopen` | 브로커 조회 | 브로커 취소 | 국내만 가능 |
| 실전 `prod` | 브로커 `broker` | 미국 `us` | 지정가 `limit`, 매도 MOO `moo` | 브로커 조회 | 브로커 취소 | API 정정 없음. 취소 후 재접수 |

## API Surface

Strategy Builder API 기준:

```http
POST /api/orders/reservations
GET  /api/orders/reservations
POST /api/orders/reservations/cancel
POST /api/orders/reservations/modify
```

공통 파라미터:

- 모의 앱 예약: `reservation_source=app`
- 실전 브로커 예약: `reservation_source=broker`
- 앱/브로커 통합 조회가 필요할 때만 `reservation_source=all`을 조회에 사용할 수 있다. 접수·취소·정정에는 사용하지 않는다.

UI:

- 모의: `http://ww.tailea9a3f.ts.net:8081/reservations`
- 실전: `http://ww.tailea9a3f.ts.net:8083/reservations`

## Workflow

### 1. 인증/모드 확인

```bash
GET /api/auth/status
```

인증이 없거나 만료됐으면 `/auth vps` 또는 `/auth prod`를 안내한다.
현재 모드에 따라 예약 소스를 먼저 결정한다.

- `vps`: 앱 레벨 예약만 사용한다. API 요청에는 `reservation_source: "app"`을 넣는다.
- `prod`: 브로커 예약만 사용한다. 예약주문 실행 전 사용자 확인을 받고 `reservation_source: "broker"`, `confirm_prod: true`를 넣는다.

### 2. 요청 정규화

- 국내 종목코드: 6자리 코드 예: `005930`
- 미국 종목코드: 대문자 심볼 예: `NVDA`
- 미국 거래소: `NASD`, `NYSE`, `AMEX`
- 매수/매도: `BUY`, `SELL`
- 모의 앱 국내 주문방식: `limit`, `market`
- 모의 앱 미국 주문방식: `limit`
- 실전 브로커 국내 주문방식: `limit`, `market`, `preopen`
- 실전 브로커 미국 주문방식: `limit`; `moo`는 미국 `SELL`만 가능
- 지정가 주문은 가격이 0보다 커야 한다.
- 모의 앱 예약은 `scheduled_at`이 필수다. KST 기준 ISO 문자열 또는 `YYYY-MM-DDTHH:mm` 형식을 사용한다.
- 모의 앱 예약은 `expires_at`을 선택으로 넣을 수 있다. 생략하면 실행시각 이후 기본 만료가 적용된다.

### 3. 주문 전 조회

예약매수:

- 현재가와 매수 가능금액을 조회한다.
- 예상금액을 계산한다. 국내는 원화, 미국은 USD 기준으로 표시한다.

예약매도:

- 국내는 국내 보유종목, 미국은 해외 보유종목을 조회한다.
- 보유수량이 없거나 수량이 부족하면 접수하지 않는다.

### 4. 접수

모의 앱 예약:

```json
{
  "market": "us",
  "stock_code": "NVDA",
  "stock_name": "NVIDIA",
  "action": "BUY",
  "quantity": 1,
  "price": 1000,
  "order_type": "limit",
  "exchange": "NASD",
  "reservation_source": "app",
  "scheduled_at": "2026-06-05T22:30:00+09:00",
  "expires_at": "2026-06-05T23:00:00+09:00",
  "confirm_prod": false
}
```

실전 브로커 예약:

```json
{
  "market": "domestic",
  "stock_code": "005930",
  "stock_name": "삼성전자",
  "action": "BUY",
  "quantity": 1,
  "price": 70000,
  "order_type": "limit",
  "reservation_source": "broker",
  "end_date": "2026-06-08",
  "confirm_prod": true
}
```

국내 실전 브로커 예약주문은 필요하면 `end_date`를 `YYYY-MM-DD` 또는 `YYYYMMDD`로 넣는다.
실전 모드에서는 사용자 확인 후에만 `confirm_prod: true`로 요청한다.

### 5. 조회

```http
GET /api/orders/reservations?reservation_source=app&market=domestic&start_date=2026-05-01&end_date=2026-05-28
GET /api/orders/reservations?reservation_source=app&market=us&exchange=NASD&start_date=2026-05-01&end_date=2026-05-28
GET /api/orders/reservations?reservation_source=broker&market=domestic&start_date=2026-05-01&end_date=2026-05-28
```

응답이 `status: error`이면 KIS 오류코드와 메시지를 사용자에게 그대로 전달한다.

### 6. 취소/정정

취소:

모의 앱 예약 취소:

```json
{
  "market": "domestic",
  "reservation_source": "app",
  "reservation_order_no": "app-reservation-id",
  "confirm_prod": false
}
```

실전 브로커 예약 취소:

```json
{
  "market": "domestic",
  "reservation_source": "broker",
  "reservation_order_no": "12345",
  "reservation_order_date": "20260528",
  "reservation_order_org_no": "001",
  "confirm_prod": true
}
```

- 모의 앱 취소는 앱 예약 ID(`reservation_order_no`)만 필요하다.
- 실전 국내 브로커 취소는 `reservation_order_org_no`가 필요하다.
- 실전 미국 브로커 취소는 `reservation_order_date`, `reservation_order_no`가 필요하다.
- 실전 취소도 사용자 확인 후 `confirm_prod: true`로 요청한다.

정정:

- 모의 앱 예약 정정은 지원하지 않는다. 취소 후 재등록한다.
- 실전 국내 브로커 예약주문만 `/api/orders/reservations/modify`를 사용한다.
- 실전 미국 브로커 예약주문 정정은 취소 후 재접수로 처리한다.

## When Combined With Strategy Signals

사용자가 "이 전략으로 예약주문"처럼 말하면:

1. `kis-order-executor` 절차에 따라 전략 신호를 먼저 확인한다.
2. 신호표를 `BUY` / `SELL` / `HOLD`로 분리한다.
3. 신호 강도 `0.5` 미만은 예약주문 후보에서 제외한다.
4. 사용자가 예약주문 조건(시장, 주문방식, 가격, 날짜)을 확인한 뒤 이 스킬의 접수 절차를 따른다.

## Troubleshooting

- `confirm_prod required`: 실전 예약주문 확인 없이 요청한 상태다. 사용자 확인 후 재시도한다.
- `미보유 종목은 예약매도할 수 없습니다`: 보유수량 확인 결과 예약매도 불가다.
- `앱 예약주문은 8081 모의투자(vps)에서만 사용할 수 있습니다`: 앱 예약을 실전 또는 잘못된 모드에서 요청한 상태다. 8081 vps로 전환한다.
- `앱 예약은 미국 지정가 주문만 지원합니다`: 모의 앱 예약에서 미국 `moo` 또는 시장가를 요청한 상태다. 지정가로 바꾸거나 실전 브로커 예약 가능 여부를 별도 확인한다.
- `앱 예약주문 정정은 지원하지 않습니다`: 모의 앱 예약은 취소 후 재등록한다.
- `모의투자 ... 제공하지 않습니다`: 브로커 모의 예약 경로를 사용한 상태일 수 있다. 모의에서는 `reservation_source=app`으로 다시 처리한다.
- `EGW00201`: KIS 초당 거래건수 제한이다. 잠시 대기한 뒤 재시도한다.
