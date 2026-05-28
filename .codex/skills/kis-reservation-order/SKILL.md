---
name: kis-reservation-order
description: "KIS 브로커 예약주문을 접수·조회·취소·정정할 때 반드시 사용. '예약주문',
  '예약 매수', '예약 매도', '장전 예약', '미국장 예약', '내일 장 시작에 주문',
  'MOO 예약', '예약주문 취소', '예약주문 조회'라고 할 때 자동 실행된다.
  국내·미국 예약주문을 모의(vps)와 실전(prod) 모두 지원하되, 실전투자 예약주문 전에는
  종목·수량·가격·예상금액·모드를 표시하고 반드시 사용자 확인을 받는다."
---

# KIS 예약주문

## Purpose

브로커 서버에 국내/미국 주식 예약주문을 접수하거나, 예약주문 상태를 조회·취소·정정한다.
일반 주문 실행과 다르게 예약주문은 시장별 지원 방식과 모의투자 제한이 있으므로 이 스킬을 우선 적용한다.

## Safety Rules

- `appkey`, `appsecret`, 토큰, 계좌번호 원문을 출력하지 않는다.
- `~/KIS/config/kis_devlp.yaml`은 사용자 요청 없이 읽거나 수정하지 않는다.
- 기본 모드는 모의투자(`vps`)다. 실전은 `/auth prod`로 명시 전환된 경우만 사용한다.
- 실전(`prod`) 예약주문 접수·취소·정정 전에는 종목, 매수/매도, 수량, 가격, 예상금액, 모드를 표시하고 사용자 확인을 받아야 한다.
- 실전 API 요청에는 `confirm_prod: true`가 필요하다. 확인이 없으면 요청하지 않는다.
- 예약매도는 보유수량을 먼저 조회하고, 보유수량이 부족하면 접수하지 않는다.
- 사용자가 종목을 지정하지 않았으면 바로 예약주문하지 않는다. 후보군 신호 확인 또는 종목 선택을 먼저 진행한다.
- KIS가 모의투자에서 조회/정정/취소 등 일부 예약업무를 미지원하면 오류 메시지를 숨기지 말고 그대로 설명한다.
- 예약주문은 접수 시점에 증거금·잔고를 브로커가 최종 보장하지 않을 수 있다. 매수 가능금액과 매도 가능수량은 앱에서 먼저 확인한다.

## Supported Orders

| 시장 | 접수 | 조회 | 취소 | 정정 |
|------|------|------|------|------|
| 국내 `domestic` | 지정가 `limit`, 시장가 `market`, 장전 `preopen` | 가능. 모의 미지원 가능 | 가능. 모의 미지원 가능 | 국내만 가능. 모의 미지원 가능 |
| 미국 `us` | 지정가 `limit`, 매도 MOO `moo` | 실전 중심. 모의 미지원 가능 | 가능. 모의 미지원 가능 | API 정정 없음. 취소 후 재접수 |

## API Surface

Strategy Builder API 기준:

```http
POST /api/orders/reservations
GET  /api/orders/reservations
POST /api/orders/reservations/cancel
POST /api/orders/reservations/modify
```

UI:

- 모의: `http://ww.tailea9a3f.ts.net:8081/reservations`
- 실전: `http://ww.tailea9a3f.ts.net:8083/reservations`

## Workflow

### 1. 인증/모드 확인

```bash
GET /api/auth/status
```

인증이 없거나 만료됐으면 `/auth vps` 또는 `/auth prod`를 안내한다.
현재 모드가 `prod`라면 예약주문 실행 전 사용자 확인을 받아야 한다.

### 2. 요청 정규화

- 국내 종목코드: 6자리 코드 예: `005930`
- 미국 종목코드: 대문자 심볼 예: `NVDA`
- 미국 거래소: `NASD`, `NYSE`, `AMEX`
- 매수/매도: `BUY`, `SELL`
- 국내 주문방식: `limit`, `market`, `preopen`
- 미국 주문방식: `limit`; `moo`는 미국 `SELL`만 가능
- 지정가 주문은 가격이 0보다 커야 한다.

### 3. 주문 전 조회

예약매수:

- 현재가와 매수 가능금액을 조회한다.
- 예상금액을 계산한다. 국내는 원화, 미국은 USD 기준으로 표시한다.

예약매도:

- 국내는 국내 보유종목, 미국은 해외 보유종목을 조회한다.
- 보유수량이 없거나 수량이 부족하면 접수하지 않는다.

### 4. 접수

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
  "confirm_prod": false
}
```

국내 예약주문은 필요하면 `end_date`를 `YYYY-MM-DD` 또는 `YYYYMMDD`로 넣는다.
실전 모드에서는 사용자 확인 후 `confirm_prod: true`로 요청한다.

### 5. 조회

```http
GET /api/orders/reservations?market=domestic&start_date=2026-05-01&end_date=2026-05-28
GET /api/orders/reservations?market=us&exchange=NASD&start_date=2026-05-01&end_date=2026-05-28
```

응답이 `status: error`이면 KIS 오류코드와 메시지를 사용자에게 그대로 전달한다.

### 6. 취소/정정

취소:

```json
{
  "market": "domestic",
  "reservation_order_no": "12345",
  "reservation_order_date": "20260528",
  "reservation_order_org_no": "001",
  "confirm_prod": false
}
```

- 국내 취소는 `reservation_order_org_no`가 필요하다.
- 미국 취소는 `reservation_order_date`, `reservation_order_no`가 필요하다.
- 실전 취소도 사용자 확인 후 `confirm_prod: true`로 요청한다.

정정:

- 국내 예약주문만 `/api/orders/reservations/modify`를 사용한다.
- 미국 예약주문 정정은 취소 후 재접수로 처리한다.

## When Combined With Strategy Signals

사용자가 "이 전략으로 예약주문"처럼 말하면:

1. `kis-order-executor` 절차에 따라 전략 신호를 먼저 확인한다.
2. 신호표를 `BUY` / `SELL` / `HOLD`로 분리한다.
3. 신호 강도 `0.5` 미만은 예약주문 후보에서 제외한다.
4. 사용자가 예약주문 조건(시장, 주문방식, 가격, 날짜)을 확인한 뒤 이 스킬의 접수 절차를 따른다.

## Troubleshooting

- `confirm_prod required`: 실전 예약주문 확인 없이 요청한 상태다. 사용자 확인 후 재시도한다.
- `미보유 종목은 예약매도할 수 없습니다`: 보유수량 확인 결과 예약매도 불가다.
- `모의투자 ... 제공하지 않습니다`: KIS 모의투자에서 해당 예약업무가 미지원이다. 실전에서만 가능한 업무인지 설명한다.
- `EGW00201`: KIS 초당 거래건수 제한이다. 잠시 대기한 뒤 재시도한다.
