---
name: kis-strategy-builder
description: "KIS 트레이딩 전략을 설계하거나 .kis.yaml 파일을 만들 때 반드시 사용. '전략 만들어줘',
  '전략 설계', 'YAML 전략', '지표 조합', '매매 조건 짜줘', '전략 파일', 'RSI 전략 만들어줘',
  'MACD+볼린저 전략', '골든크로스 전략', 'strategy builder'라고 할 때 자동 실행된다.
  strategy_builder 비주얼 빌더 안내, 실행 가능 프리셋 확인, 기술적 지표(RSI/MACD/BB/EMA 등) 기반
  진입·청산 조건 설계, .kis.yaml 포맷 생성, DSL 조건식 작성을 수행한다.
  완성된 YAML은 백테스팅(Step 2)이나 주문 실행(Step 3)에 바로 사용 가능하다."
---

# [Step 1] KIS 전략 설계

## Purpose

strategy_builder 비주얼 빌더를 활용해 기술적 지표 기반 트레이딩 전략을 설계하고 `.kis.yaml` 파일로 내보낸다.
완성된 YAML은 백테스팅(Step 2) 또는 실시간 신호 생성(Step 3)에 바로 사용한다.

## 서버 시작 (필요 시)

```bash
# Backend
cd <프로젝트루트>/strategy_builder && uv run uvicorn backend.main:app --reload --port 8000

# Frontend
cd <프로젝트루트>/strategy_builder/frontend && pnpm dev
# → http://localhost:3000/builder
```

## Workflow

### 1. 전략 유형 파악

- 실행 가능한 백테스터 프리셋 vs. 커스텀 `.kis.yaml` 설계
- 카테고리: `trend` / `momentum` / `mean_reversion` / `volatility` / `oscillator`
- 프리셋 ID·파라미터는 하드코딩하지 말고 가능하면 `list_presets_tool` 결과를 기준으로 한다.
- `kis-backtester/references/yaml-templates.md`는 커스텀 YAML 예시이며, MCP `run_preset_backtest_tool`의 `strategy_id` 목록과 동일하다고 가정하지 않는다.

### 2. 지표 선택

83개 기술지표 (전체 활성화):

| 계열 | 지표 |
|------|------|
| 이동평균 | SMA, EMA, VWAP |
| 모멘텀 | RSI, MACD, ROC, Returns |
| 변동성 | BB, ATR, STD, Volatility, ZScore |
| 오실레이터 | Stoch, CCI, Williams%R, MFI, IBS |
| 추세 | ADX, Disparity |
| 거래량 | OBV |
| 기타 | Consecutive, Change, CustomCandle |

### 3. 진입·청산 조건 설계

**연산자**: `greater_than` / `less_than` / `cross_above` / `cross_below` / `equals` / `not_equal` / `breaks`

> `greater_than_or_equal` / `gte` / `lte` 는 **지원하지 않는다**.
> `>= 50` 조건은 `greater_than: 50` (정수 RSI에서 실질 동일) 으로 표현한다.

**로직 결합**: `AND` / `OR`

**캔들 패턴** (66종 — 아래는 예시, 전체 목록은 `candlestick.py`의 `PATTERN_DETECTORS` 참조):
`hammer`, `inverted_hammer`, `doji`, `engulfing`, `harami`,
`morning_star`, `evening_star`, `three_white_soldiers`, `three_black_crows`,
`shooting_star`, `hanging_man`, `piercing`

### 4. 리스크 관리

`risk`는 최상위 키 (`strategy` 블록 밖). `enabled: true`와 `percent` 필드가 필수다.

```yaml
risk:
  stop_loss:
    enabled: true
    percent: 3.0        # % 단위
  take_profit:
    enabled: true
    percent: 8.0        # % 단위
  trailing_stop:
    enabled: true
    percent: 2.0        # % 단위 (선택)
```

> `risk: {}` 또는 `strategy` 안에 `risk:` 를 넣으면 백테스터 런타임 오류 발생.

### 5. 파라미터 확인

YAML 생성 전, 사용자에게 주요 파라미터를 표로 보여주고 확인받는다:

```
| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| period   | 14     | RSI 기간 |
| oversold | 30     | 매수 진입 기준 |
| overbought | 70   | 매도 청산 기준 |
| stop_loss | 3.0   | 손절 % |

이 값으로 전략을 생성할까요? 변경할 항목이 있으면 말씀해주세요.
```

사용자가 확인하면 해당 값을 YAML에 직접 반영한다.

### 6. YAML 생성

**필수 규칙**: 조건(entry/exit)의 `value` 필드에는 반드시 **숫자 리터럴**을 넣는다.
`$param_name` 변수 참조를 넣으면 백테스터가 validation 에러를 낸다.
`params` 섹션에 정의한 기본값을 조건에 직접 대입한다.

```yaml
# ❌ 잘못된 예 — 백테스터 validation 에러 발생
entry:
  conditions:
    - indicator: rsi
      operator: less_than
      value: $rsi_oversold     # ← 문자열이라 실패

# ✅ 올바른 예 — 숫자 리터럴 직접 사용
entry:
  conditions:
    - indicator: rsi
      operator: less_than
      value: 30                # ← 실제 값
```

`.kis.yaml` 전체 예시:

```yaml
version: "1.0"
metadata:
  name: RSI 과매도 전략
  description: RSI 30 이하 진입, 70 이상 청산
  category: momentum
  author: user

strategy:
  id: rsi_oversold            # 필수: snake_case 고유 식별자
  indicators:
    - id: rsi
      alias: rsi
      params:
        period: 14

  entry:
    conditions:
      - indicator: rsi
        operator: less_than
        value: 30
    logic: AND

  exit:
    conditions:
      - indicator: rsi
        operator: greater_than
        value: 70
    logic: AND

risk:
  stop_loss:
    enabled: true
    percent: 3.0
  take_profit:
    enabled: true
    percent: 8.0
```

### 6b. 산출물 핸드오프

전략을 만든 뒤 다음 단계로 넘길 때는 YAML만 던지지 말고 아래 상태를 함께 요약한다.

```json
{
  "strategy_id": "rsi_oversold",
  "yaml_path": "strategies/custom/rsi_oversold.kis.yaml",
  "symbols": ["005930"],
  "market": "domestic",
  "timeframe": "daily",
  "entry": "RSI < 30",
  "exit": "RSI > 70",
  "risk": {"stop_loss_pct": 3.0, "take_profit_pct": 8.0}
}
```

백테스트나 주문 실행 단계는 이 `builder_state` 또는 같은 내용의 `.kis.yaml`을 기준으로 조건을 재확인한다.

### 7. 다중 출력 지표 (MACD 골든크로스)

MACD는 `value`(MACD 라인), `signal`(시그널 라인), `histogram` 세 가지 출력을 가진다.
골든크로스/데드크로스는 **단일 alias**에서 `output`과 `compare_output`으로 두 출력을 비교한다.

```yaml
strategy:
  id: macd_rsi_composite
  indicators:
    - id: macd
      alias: macd           # 하나의 인스턴스만 선언
      params:
        fast: 12
        slow: 26
        signal: 9
    - id: rsi
      alias: rsi
      params:
        period: 14

  entry:
    logic: AND
    conditions:
      - indicator: macd
        output: value         # 왼쪽: MACD 라인
        operator: cross_above
        compare_to: macd      # 오른쪽: 동일 인스턴스
        compare_output: signal  # 오른쪽 출력: 시그널 라인
      - indicator: rsi
        operator: greater_than
        value: 50

  exit:
    logic: OR
    conditions:
      - indicator: macd
        output: value
        operator: cross_below
        compare_to: macd
        compare_output: signal

risk:
  stop_loss:
    enabled: true
    percent: 3.0
  take_profit:
    enabled: true
    percent: 8.0
```

> **중요**: `macd`를 두 개의 alias로 분리해 `compare_to`로 비교하는 방식은
> 두 개의 독립된 Lean MACD 인스턴스를 생성하여 크로스오버가 동작하지 않는다.
> 반드시 **단일 alias + compare_to: 동일alias + compare_output: signal** 패턴을 사용한다.

### 8. 코드 프리뷰 (선택)

```bash
POST /api/strategies/preview
Body: { "yaml": "<yaml 내용>" }
# → 생성된 Python 클래스 코드 확인
```

## 프리셋 기준

실행 가능한 프리셋은 백테스터 MCP의 `list_presets_tool` 결과가 source of truth다.
현재 대표 ID는 `sma_crossover`, `momentum`, `week52_high`, `consecutive_moves`,
`ma_divergence`, `false_breakout`, `strong_close`, `volatility_breakout`,
`short_term_reversal`, `trend_filter_signal`이다.

`golden_cross`, `adx_trend`, `mfi_oversold` 같은 이름은 커스텀 YAML 템플릿으로 사용할 수 있지만,
`run_preset_backtest_tool.strategy_id`로 바로 실행한다고 가정하지 않는다.


## Troubleshooting

- 지표가 NaN → 데이터 부족. `min_period` 이상의 과거 데이터 필요 (SMA20 → 20일 이상)
- YAML 파싱 오류 → 들여쓰기(2스페이스) 확인, `$param_name` 변수가 남아 있으면 숫자 리터럴로 치환
- preview 오류 → 지표 ID와 조건의 `indicator` 필드명 일치 여부 확인
- 실행 전 빠른 점검 → `python3 .codex/scripts/validate_kis_yaml.py <파일.kis.yaml>`

## 다음 단계

- **[Step 2]** `/kis-backtester` — 완성된 YAML로 과거 성과 검증
- **[Step 3]** `/kis-order-executor` — 백테스트 없이 바로 신호 생성 후 주문
