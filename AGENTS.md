# KIS Plugin — Agent Guide

한국투자증권 Open API의 **전략 설계 → 백테스팅 → 주문 실행** 파이프라인을 자연어로 조작할 수 있게 해주는 플러그인입니다.

---

## Skills

사용자 의도에 따라 아래 스킬을 활성화합니다.

| 스킬 | 트리거 | 설명 |
|------|--------|------|
| `kis-strategy-builder` | "전략 만들어줘", "지표1 + 지표2 조합" | 프리셋 확인 또는 지표 조합으로 `.kis.yaml` 전략 설계 |
| `kis-backtester` | "백테스트 해줘", "전략 검증" | Lean 엔진 백테스팅, 파라미터 최적화, HTML 리포트 |
| `kis-order-executor` | "신호 확인", "모의투자 실행" | BUY/SELL/HOLD 신호 확인 후 모의/실전 주문 |
| `kis-reservation-order` | "예약주문", "예약 매수/매도", "MOO 예약" | 모의는 앱 레벨 예약, 실전은 브로커 예약 접수·조회·취소·정정 |
| `kis-protective-order` | "손절 설정", "익절 설정", "도달가 감시", "보호주문" | 보유종목 손익절 라인 감시 및 도달 시 매도 설정 |
| `kis-team` | "다 해줘", "전략부터 주문까지" | Step 1→2→3 풀파이프라인 (단계별 사용자 확인) |
| `kis-cs` | 사용법 문의, 오류, 주식 추천 요구 | 고객 서비스 안내 + 오류코드 해석 |

상세 워크플로우·파라미터는 `skills/<skill-name>/SKILL.md` 참조.

---

## Safety Rules

- `appkey`, `appsecret`, 토큰 등 민감 정보를 코드에 하드코딩하거나 출력하지 않는다.
- `~/KIS/config/kis_devlp.yaml`을 사용자 요청 없이 읽거나 수정하지 않는다.
- Strategy Builder 운영 엔드포인트는 `8081`/`8083` 기준으로 판단한다. `8000`은 컨테이너 내부 또는 로컬 개발용 백엔드 포트일 수 있으므로 모의/실전 운영 상태 판단에 사용하지 않는다.
- 포트, cron, 인증, 자동화, 주문 경로처럼 운영 의도가 중요한 상태를 판단하기 전에는 `MEMORY.md`, 최근 git 커밋, 가능한 경우 agentmemory 기록을 먼저 확인하고, 현재 관측값이 의도된 구조인지 임시/개발 상태인지 구분한다.
- `http://ww.tailea9a3f.ts.net:8081` 및 `http://127.0.0.1:8081`은 모의투자(`vps`) 전용 Strategy Builder 엔드포인트다.
- `http://ww.tailea9a3f.ts.net:8083` 및 `http://127.0.0.1:8083`은 실전투자(`prod`) 전용 Strategy Builder 엔드포인트다.
- `KIS_LOCK_MODE` 분리 규칙을 따른다. `8081`은 `prod` 로그인을 거부해야 하고, `8083`은 `vps` 로그인을 거부해야 한다.
- 분리 목적은 모의 자동화를 `8081`에서 매일 계속 돌려 수익률·안정성을 검증하고, 실전 자동화 테스트는 `8083`에서 별도 승인 절차로 동시에 검증하기 위함이다.
- 자동화 스크립트에서 API 엔드포인트를 바꿔야 하면 공통 `KIS_STRATEGY_API`보다 모드별 `KIS_VPS_STRATEGY_API` 또는 `KIS_PROD_STRATEGY_API`를 우선 사용한다.
- 실전(`prod`) 주문 전에 종목·수량·예상금액을 표시하고 반드시 사용자 확인을 받는다.
- 실전(`prod`) 예약주문 접수·취소·정정 전에도 종목·수량·가격·예상금액·모드를 표시하고 반드시 사용자 확인을 받는다.
- 실전(`prod`) 손익절 감시 설정 전에도 종목·수량·도달가·주문방식·모드를 표시하고 반드시 사용자 확인을 받는다.
- 신호 강도 `0.5` 미만이면 주문을 건너뛴다.
- 계좌번호는 마스킹해서 표시한다.
- 실행 가능한 프리셋은 각 실행 도구의 API 결과를 기준으로 한다. 백테스터는 `list_presets_tool`, 실시간 실행은 `/api/strategies`가 source of truth다.
- 커스텀 `.kis.yaml`은 실행 전 `.codex/scripts/validate_kis_yaml.py`와 백테스터 `validate_yaml_tool`을 통과시킨다.

## Order Execution Guardrails

주문 실행 요청을 받으면 아래 순서를 지킨다.

- `/builder` UI를 직접 조작하지 않더라도 동일한 `builder_state` 또는 `.kis.yaml` 기준으로 지표·진입·청산·리스크 조건을 먼저 확인한다.
- 사용자가 종목을 지정하지 않았으면 대표 종목 몇 개로 바로 주문하지 않는다. 당일 후보군을 섹터·유동성 기준으로 선별하고, 후보군 전체 신호를 실행한다.
- 주문 전 신호표를 `BUY` / `SELL` / `HOLD`로 분리해 확인한다. `SELL` 신호는 보유 수량이 있을 때만 매도 대상으로 처리한다.
- 신규 매수 전에는 익절가·손절가·자동 설정 가능 여부를 함께 계산한다. OCO/조건부 손절 주문이 지원되지 않으면 그 한계를 명시하고, 가능한 경우 익절 지정가 주문을 즉시 설정한다.
- 신규 매수 후에는 보유/미체결 상태를 재조회해 접수 여부와 리스크 주문 상태를 확인한다.

## Reservation Order Guardrails

예약주문 요청을 받으면 `kis-reservation-order` 스킬을 우선 적용한다.

- 일반 주문과 예약주문을 혼동하지 않는다. 예약주문은 `/api/orders/reservations` 계열 API만 사용한다.
- 모의투자(`vps`, 8081) 예약주문은 반드시 앱 레벨 예약(`reservation_source=app`)만 사용한다. KIS 브로커 예약(`reservation_source=broker`)으로 우회하지 않는다.
- 모의 앱 예약은 KIS 서버 예약이 아니라 Strategy Builder 앱이 `scheduled_at` KST 시각에 일반 주문을 제출하는 방식이다. 서버·인증·네트워크 상태에 의존한다는 한계를 설명한다.
- 모의 앱 국내 예약주문은 `limit`, `market`만 허용한다.
- 모의 앱 미국 예약주문은 `limit`만 허용한다.
- 모의 앱 예약 정정은 지원하지 않고 취소 후 재등록으로 처리한다.
- 실전투자(`prod`, 8083) 예약주문은 브로커 예약(`reservation_source=broker`)만 사용한다.
- 실전 브로커 국내 예약주문은 `limit`, `market`, `preopen`을 허용한다.
- 실전 브로커 미국 예약주문은 `limit`을 기본으로 하며, `moo`는 미국 매도에만 허용한다.
- 예약매도는 보유수량 확인 후 보유수량이 있을 때만 처리한다.
- 실전 국내 브로커 예약주문 정정은 지원하지만, 실전 미국 브로커 예약주문 정정은 취소 후 재접수로 처리한다.
- KIS가 모의투자 브로커 예약주문 미지원 오류를 반환하면 오류를 숨기지 말고 설명하되, 모의 예약은 앱 레벨(`reservation_source=app`)로 다시 처리한다.

## Protective Order Guardrails

손익절 라인 감시나 도달 시 매도 설정 요청을 받으면 `kis-protective-order` 스킬을 우선 적용한다.

- 보호주문은 보유종목에만 설정한다. 보유수량·평균단가·현재가를 먼저 확인한다.
- 보호주문은 KIS 서버 OCO가 아니라 Strategy Builder 앱 레벨 감시다. 서버·인증·네트워크 상태에 의존한다는 한계를 명시한다.
- 국내는 손절/익절 매도 주문방식으로 `market` 또는 `limit`을 허용한다.
- 미국은 손절/익절 매도 주문방식을 `limit`으로 강제한다.
- 미국 모의 보호매도는 브로커 예약으로 우회하지 않고 앱 레벨 큐에서 일반 지정가 주문을 재시도한다.
- 미국 모의 보호매도는 주문번호 수신 후에도 보유수량과 미체결을 확인하며, 미체결 재가격 주기가 지나면 취소 후 최신 현재가 기준으로 재제출한다.
- 미국 보호매도 지정가는 손절/익절별 하향 오프셋 설정을 적용할 수 있다.
- 손절 도달가는 기준가보다 낮고, 익절 도달가는 기준가보다 높아야 한다.
- 지정가 매도 방식은 지정가를 함께 확인한다.
- 조건 도달 후 주문 실패 시 `last_error` 원문을 숨기지 않고 설명한다.

---

## Hooks (에이전트별)

보안 훅은 에이전트별로 분리되어 있다.

| 에이전트 | 매니페스트 | 스크립트 위치 | 비고 |
|----------|-----------|-------------|------|
| Claude Code | `hooks/hooks.json` | `hooks/kis-*.sh` | PreToolUse/PostToolUse — 도구 호출 단위 |
| Gemini CLI | `.gemini/settings.json` (inline) | `.gemini/hooks/kis-*.sh` | BeforeTool/AfterTool/SessionEnd — 도구 호출 단위 |
| Cursor | `hooks/hooks_cursor.json` | `hooks/cursor/*.sh` | afterAgentResponse/stop — 세션 단위 |
| Codex | `.codex/config.toml` | — | 훅 미지원; `approval_policy = "on-request"` + AGENTS.md 규칙으로 대체 |

- 각 플랫폼은 이벤트 키를 strict validation하므로 hooks 파일을 공유할 수 없다. 플랫폼별 별도 파일을 사용한다.
- Gemini CLI는 `.gemini/settings.json`에 hooks가 inline으로 포함되어 있다. 별도 설정 불필요.
- Cursor는 PreToolUse(도구 호출 전 차단)를 지원하지 않는다. 주문 안전장치는 `rules/kis-safety.mdc`로 대체한다.
- Codex는 훅을 지원하지 않는다. 실전(prod) 주문 전 반드시 수동으로 사용자 확인을 받아야 한다.

---

## Operational Checks

- Strategy Builder 모의 상태 확인: `curl -s http://127.0.0.1:8081/api/auth/status`
- Strategy Builder 실전 상태 확인: `curl -s http://127.0.0.1:8083/api/auth/status`
- 백테스팅은 Docker + MCP 엔드포인트 `http://127.0.0.1:3846/mcp` 가 필요하다.
  - MCP 상태 확인: `curl -s http://127.0.0.1:3846/health`
  - MCP가 내려가 있으면: `bash backtester/scripts/start_mcp.sh`
- 인증이 없거나 만료됐으면: `/auth` 커맨드 안내
- 기본값은 모의투자(`vps`); 실전은 `/auth prod`로 명시 전환
