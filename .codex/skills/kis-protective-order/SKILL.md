---
name: kis-protective-order
description: "KIS 보유종목의 손익절 라인 감시와 조건 도달 시 매도 설정을 할 때 반드시 사용.
  '손절 설정', '익절 설정', '손익절 라인', '도달가 감시', '도달하면 매도',
  '지정가 손절', '시장가 손절', '익절 지정가', '수익보존 매도', '보호주문',
  'OCO', '손절가 바꿔줘', '익절가 바꿔줘'라고 할 때 자동 실행된다.
  국내·미국 보유종목의 앱 레벨 감시를 설정·조회·점검하며, 실전투자 매도 발생 가능 설정 전에는
  종목·수량·도달가·주문방식·모드를 표시하고 반드시 사용자 확인을 받는다."
---

# KIS 손익절 감시 보호주문

## Purpose

보유 중인 국내/미국 주식에 대해 익절·손절 도달가를 감시하고, 조건 도달 시 매도 주문을 제출하는 앱 레벨 보호주문을 설정한다.
이 기능은 KIS 서버 네이티브 OCO가 아니라 Strategy Builder 백엔드의 감시 루프와 실시간 가격 스트림을 사용한다.

## Safety Rules

- `appkey`, `appsecret`, 토큰, 계좌번호 원문을 출력하지 않는다.
- `~/KIS/config/kis_devlp.yaml`은 사용자 요청 없이 읽거나 수정하지 않는다.
- 보호주문은 보유종목에만 설정한다. 보유수량·평균단가·현재가를 먼저 확인한다.
- 실전(`prod`)에서 매도 주문이 발생할 수 있는 감시 설정을 저장하기 전에는 종목, 수량, 도달가, 주문방식, 지정가, 모드를 표시하고 사용자 확인을 받는다.
- 미국 주식은 보호주문 매도를 지정가로만 처리한다. 사용자가 시장가를 요청해도 지정가 제한을 설명하고 지정가로 설정한다.
- 국내 주식은 익절/손절 주문방식으로 `market` 또는 `limit`을 사용할 수 있다.
- 지정가 방식은 지정가가 필요하다. 지정가가 없으면 도달가를 기준으로 제안하되 사용자 확인 전에는 저장하지 않는다.
- 손절 도달가는 기준가보다 낮아야 하고, 익절 도달가는 기준가보다 높아야 한다.
- 앱 레벨 감시는 백엔드/네트워크/인증 상태에 의존한다. 서버가 멈추면 자동 매도도 멈출 수 있음을 명시한다.
- 모의투자나 미국 주간거래 등 KIS가 특정 주문을 미지원하면 오류 메시지를 그대로 설명한다.

## API Surface

Strategy Builder API 기준:

```http
GET  /api/orders/protective
POST /api/orders/protective
POST /api/orders/protective/check
POST /api/orders/protective/settings
WS   /api/orders/protective/prices/ws
```

UI:

- 모의: `http://ww.tailea9a3f.ts.net:8081/review`
- 실전: `http://ww.tailea9a3f.ts.net:8083/review`

## Workflow

### 1. 인증/모드 확인

```bash
GET /api/auth/status
```

인증이 없거나 만료됐으면 `/auth vps` 또는 `/auth prod`를 안내한다.
현재 모드가 `prod`라면 저장 전에 사용자 확인을 받아야 한다.

### 2. 보유종목 조회

시장에 따라 보유종목을 먼저 조회한다.

```http
GET /api/orders/account
GET /api/overseas/holdings
```

확인할 값:

- 종목코드/종목명
- 보유수량
- 평균단가 또는 기준가
- 현재가
- 시장 구분: `domestic` 또는 `us`
- 미국 거래소: `NASD`, `NYSE`, `AMEX`

보유수량이 없으면 보호주문을 만들지 않는다.

### 3. 설정값 정규화

보호주문 저장 필드:

```json
{
  "stock_code": "005930",
  "stock_name": "삼성전자",
  "quantity": 10,
  "entry_price": 73000,
  "enabled": true,
  "take_profit_enabled": true,
  "take_profit_trigger_price": 79000,
  "take_profit_order_type": "limit",
  "take_profit_limit_price": 79000,
  "stop_loss_enabled": true,
  "stop_loss_trigger_price": 70000,
  "stop_loss_order_type": "market",
  "stop_loss_limit_price": null,
  "market": "domestic",
  "exchange": null,
  "currency": "KRW"
}
```

국내:

- 익절 주문: `limit` 또는 `market`
- 손절 주문: `market` 또는 `limit`
- `market`이면 limit price는 `null`
- `limit`이면 limit price가 필요

미국:

- 익절 주문과 손절 주문 모두 `limit`으로 강제
- `take_profit_limit_price`, `stop_loss_limit_price`가 필요
- 통화는 `USD`

### 4. 저장 전 확인

실전(`prod`)이면 아래처럼 확인한다.

```text
보호주문 설정
모드: 실전투자(prod)
종목: 삼성전자(005930)
수량: 10주
익절: 79,000원 도달 시 지정가 79,000원 매도
손절: 70,000원 도달 시 시장가 매도
앱 레벨 감시: 서버/인증 상태에 의존
진행할까요?
```

사용자가 확인하기 전에는 `POST /api/orders/protective`를 호출하지 않는다.

### 5. 저장/점검

```http
POST /api/orders/protective
```

저장 후 바로 상태를 재조회한다.

```http
GET /api/orders/protective
```

즉시 1회 점검이 필요하면:

```http
POST /api/orders/protective/check
```

감시 주기 변경:

```json
{
  "monitor_interval_seconds": 15
}
```

### 6. 주문 발생 후 확인

조건 도달로 매도 주문이 제출되면:

- 보호주문 상태: `exit_submitted`, `closed`, `active`, `submit_failed` 등 확인
- 국내 미체결: `GET /api/orders/pending`
- 미국 미체결: `GET /api/overseas/pending`
- 실패 시 `last_error`를 그대로 설명한다.

## Interaction With Strategy Orders

전략 매수 주문과 함께 손익절이 요청되면:

1. `kis-order-executor`로 전략 신호와 주문 가능 여부를 먼저 확인한다.
2. 매수 주문 요청에 `protective_order`를 포함할 수 있다.
3. 매수 접수 후 보유/미체결/보호주문 상태를 재조회한다.
4. 이미 보유 중인 종목의 라인 수정은 이 스킬의 `/api/orders/protective` 저장 흐름을 사용한다.

## Troubleshooting

- `take_profit_trigger_price must be positive`: 익절 도달가가 없거나 0 이하다.
- `stop_loss_trigger_price must be positive`: 손절 도달가가 없거나 0 이하다.
- `익절 주문 방식이 올바르지 않습니다`: 주문방식은 `market` 또는 `limit`만 허용된다.
- `미국 주식`: 주문방식이 강제로 `limit`이 된다.
- `last_error`에 `제공하지 않습니다` 또는 `not supported`: KIS 모의/시장/시간대 제약이다. 오류 원문을 사용자에게 전달한다.
- `EGW00201`: KIS 초당 거래건수 제한이다. 감시 주기를 늘리거나 잠시 후 재시도한다.
