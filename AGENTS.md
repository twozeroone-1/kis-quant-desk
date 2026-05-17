# KIS Plugin — Agent Guide

한국투자증권 Open API의 **전략 설계 → 백테스팅 → 주문 실행** 파이프라인을 자연어로 조작할 수 있게 해주는 플러그인입니다.

---

## Skills

사용자 의도에 따라 아래 스킬을 활성화합니다.

| 스킬 | 트리거 | 설명 |
|------|--------|------|
| `kis-strategy-builder` | "전략 만들어줘", "지표1 + 지표2 조합" | 10개 프리셋 + 80개 지표로 `.kis.yaml` 전략 설계 |
| `kis-backtester` | "백테스트 해줘", "전략 검증" | Lean 엔진 백테스팅, 파라미터 최적화, HTML 리포트 |
| `kis-order-executor` | "신호 확인", "모의투자 실행" | BUY/SELL/HOLD 신호 확인 후 모의/실전 주문 |
| `kis-team` | "다 해줘", "전략부터 주문까지" | Step 1→2→3 풀파이프라인 (단계별 사용자 확인) |
| `kis-cs` | 사용법 문의, 오류, 주식 추천 요구 | 고객 서비스 안내 + 오류코드 해석 |

상세 워크플로우·파라미터는 `skills/<skill-name>/SKILL.md` 참조.

---

## Safety Rules

- `appkey`, `appsecret`, 토큰 등 민감 정보를 코드에 하드코딩하거나 출력하지 않는다.
- `~/KIS/config/kis_devlp.yaml`을 사용자 요청 없이 읽거나 수정하지 않는다.
- 실전(`prod`) 주문 전에 종목·수량·예상금액을 표시하고 반드시 사용자 확인을 받는다.
- 신호 강도 `0.5` 미만이면 주문을 건너뛴다.
- 계좌번호는 마스킹해서 표시한다.

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

- 백테스팅은 Docker + MCP 엔드포인트 `http://127.0.0.1:3846/mcp` 가 필요하다.
  - MCP 상태 확인: `curl -s http://127.0.0.1:3846/health`
  - MCP가 내려가 있으면: `bash backtester/scripts/start_mcp.sh`
- 인증이 없거나 만료됐으면: `/auth` 커맨드 안내
- 기본값은 모의투자(`vps`); 실전은 `/auth prod`로 명시 전환
