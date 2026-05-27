# 지표 YAML 파라미터 레퍼런스

이 파일은 자주 쓰는 지표 빠른 참조다. 전체 지원 지표와 output 목록은 실행 직전 `list_indicators_tool` 결과를 기준으로 한다.

| id | 주요 params | output (다중 출력) | 비고 |
|----|-------------|-------------------|------|
| `sma` | period | — | |
| `ema` | period | — | |
| `rsi` | period | — | 0~100 |
| `macd` | fast, slow, signal | value, signal, histogram | |
| `bollinger` | period, std | upper, middle, lower | `std`(표준편차 배수) |
| `stochastic` | k_period, d_period | k, d | 0~100 |
| `atr` | period | — | |
| `adx` | period | value, plus_di, minus_di | |
| `obv` | — | — | |
| `cci` | period | — | |
| `williams_r` | period | — | -100~0 |
| `mfi` | period | — | 0~100 |
| `vwap` | period | — | |
| `consecutive` | direction | — | direction: up\|down |
| `roc` | period | — | 변화율(%) |
| `ibs` | — | — | (close-low)/(high-low) |
| `std` | period | — | 표준편차 |
| `maximum` | period | — | N일 최고값 |
| `minimum` | period | — | N일 최저값 |
| `natr` | period | — | 정규화 ATR |
| `stochrsi` | rsi_period, stoch_period, k_period, d_period | k, d | |
| `close` | — | — | 조건 `indicator` 필드에 특수값으로 사용 |

> **다중 출력 지표 사용법**: 조건에서 `output`/`compare_output` 필드로 참조한다.
> MACD 라인과 signal 라인의 크로스는 같은 alias에서 `output: value`, `compare_to: <same alias>`, `compare_output: signal` 패턴을 우선 사용한다.

# YAML 생성 전 체크

- [ ] `strategy.id`: snake_case, 영문+숫자+_ 조합
- [ ] 모든 indicator `alias` 고유 (같은 지표 2개 사용 시 별도 alias 필수)
- [ ] 조건의 `indicator` 값이 indicators 목록의 alias 또는 `close`와 일치
- [ ] 다중 출력 지표(`macd`, `bollinger`, `stochastic`)는 `output`/`compare_output` 필드 명시
- [ ] `compare_to`와 `value` 동시 사용 금지 (하나만)
- [ ] `risk.stop_loss/take_profit`: `{enabled: true, percent: X.0}` 형식
- [ ] `version: "1.0"` 고정
- [ ] `$param_name` placeholder가 남아 있지 않음
- [ ] `python3 .codex/scripts/validate_kis_yaml.py <파일>` 통과
- [ ] 파일 저장 전 기존 파일 덮어쓰기 여부 사용자 확인
