#!/usr/bin/env python3
"""Run KR/US custom-candidate automation reports in vps report-only mode."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[4]
API_BASE = "http://127.0.0.1:8081"
KR_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
US_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,9}$")
DATE_PATTERN = re.compile(r"^[0-9]{8}$")


@dataclass(frozen=True)
class MarketRun:
    market: str
    symbols: list[str]
    date: str
    run_id: str

    @property
    def runtime_dir(self) -> Path:
        return PROJECT_ROOT / ".codex" / "runtime" / f"{self.market}_market_auto"

    @property
    def json_path(self) -> Path:
        return self.runtime_dir / f"{self.run_id}.json"

    @property
    def markdown_path(self) -> Path:
        return self.runtime_dir / f"{self.run_id}.md"


def parse_symbol_list(raw: str | None, pattern: re.Pattern[str], *, uppercase: bool = False) -> list[str]:
    if not raw:
        return []
    values: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        symbol = token.strip()
        if uppercase:
            symbol = symbol.upper()
        if not symbol:
            continue
        if not pattern.fullmatch(symbol):
            invalid.append(token.strip())
            continue
        if symbol not in seen:
            seen.add(symbol)
            values.append(symbol)
    if invalid:
        raise SystemExit(f"Invalid symbols: {', '.join(invalid)}")
    return values


def today(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y%m%d")


def time_tag(tz_name: str, suffix: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime(f"%Y%m%d_%H%M_{suffix}")


def sanitize_run_tag(value: str) -> str:
    tag = value.strip().strip("_")
    if not re.fullmatch(r"[A-Za-z0-9_]+", tag):
        raise SystemExit("--run-tag may contain only letters, numbers, and underscores")
    return tag


def validate_date(value: str, label: str) -> str:
    if not DATE_PATTERN.fullmatch(value):
        raise SystemExit(f"{label} must be YYYYMMDD: {value}")
    return value


def request_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to read {url}: {exc}") from exc


def verify_vps_auth() -> None:
    payload = request_json(f"{API_BASE}/api/auth/status")
    if payload.get("mode") != "vps" or not payload.get("authenticated"):
        raise SystemExit(
            "8081 is not authenticated in vps mode. "
            f"status={payload.get('mode')!r}, authenticated={payload.get('authenticated')!r}"
        )


def run_command(args: list[str], *, env: dict[str, str]) -> int:
    print(f"$ {' '.join(args)}", flush=True)
    proc = subprocess.run(args, cwd=PROJECT_ROOT, env=env, text=True)
    return proc.returncode


def check_calendar(run: MarketRun) -> None:
    script = PROJECT_ROOT / ".codex" / "scripts" / f"{run.market}_market_calendar.py"
    if run.market == "us":
        args = [
            "uv",
            "run",
            "--project",
            "strategy_builder",
            "python",
            str(script.resolve()),
            "--date",
            run.date,
            "--check-open",
        ]
    else:
        args = ["uv", "run", "python", str(script.relative_to(PROJECT_ROOT)), "--date", run.date, "--check-open"]
    code = run_command(args, env=os.environ.copy())
    if code != 0:
        raise SystemExit(f"{run.market.upper()} market calendar check failed or market is closed for {run.date}")


def run_market(run: MarketRun) -> None:
    env = os.environ.copy()
    env.update({
        "KIS_TRADE_MODE": "vps",
        "KIS_VPS_STRATEGY_API": API_BASE,
    })
    if run.market == "kr":
        env["KR_MARKET_CANDIDATE_SYMBOLS"] = ",".join(run.symbols)
        script = ".codex/scripts/kr_market_auto_run.py"
        args = [
            "uv",
            "run",
            "python",
            script,
            "--slot",
            "manual",
            "--date",
            run.date,
            "--run-id",
            run.run_id,
            "--report-only",
            "--trade-mode",
            "vps",
        ]
    else:
        env["US_MARKET_CANDIDATE_SYMBOLS"] = ",".join(run.symbols)
        script = ".codex/scripts/us_market_auto_run.py"
        args = [
            "uv",
            "run",
            "--project",
            "strategy_builder",
            "python",
            str((PROJECT_ROOT / script).resolve()),
            "--slot",
            "manual",
            "--date",
            run.date,
            "--run-id",
            run.run_id,
            "--report-only",
            "--llm-mode",
            "off",
        ]
    code = run_command(args, env=env)
    if code != 0:
        raise SystemExit(f"{run.market.upper()} report-only run failed with exit code {code}")


def count_orders(payload: dict[str, Any]) -> int:
    total = 0
    for key in ("orders", "submitted_buys", "submitted_sells"):
        value = payload.get(key)
        if isinstance(value, list):
            total += len(value)
    return total


def signal_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in payload.get("signals", []):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "UNKNOWN")
        counts[action] = counts.get(action, 0) + 1
    return counts


def read_report_summary(run: MarketRun) -> dict[str, Any]:
    if not run.json_path.exists():
        raise SystemExit(f"Report JSON was not created: {run.json_path}")
    payload = json.loads(run.json_path.read_text(encoding="utf-8"))
    api_payload: dict[str, Any] | None = None
    try:
        api_payload = request_json(f"{API_BASE}/api/automation/{run.market}/runs/{run.run_id}")
    except RuntimeError as exc:
        api_payload = {"status": "unavailable", "message": str(exc)}
    return {
        "market": run.market,
        "date": run.date,
        "run_id": run.run_id,
        "symbols": run.symbols,
        "json_path": str(run.json_path.relative_to(PROJECT_ROOT)),
        "markdown_path": str(run.markdown_path.relative_to(PROJECT_ROOT)),
        "status": payload.get("status"),
        "report_only": payload.get("report_only"),
        "candidate_mode": payload.get("candidate_selection", {}).get("mode"),
        "selected_symbols": [
            item.get("symbol") or item.get("code")
            for item in payload.get("candidate_selection", {}).get("selected", [])
            if isinstance(item, dict)
        ],
        "signal_counts": signal_counts(payload),
        "order_count": count_orders(payload),
        "warnings": payload.get("warnings") or payload.get("candidate_selection", {}).get("warnings") or [],
        "errors": payload.get("errors") or payload.get("candidate_selection", {}).get("errors") or [],
        "api_status": api_payload.get("status") if isinstance(api_payload, dict) else None,
    }


def build_runs(args: argparse.Namespace) -> list[MarketRun]:
    tag = sanitize_run_tag(args.run_tag)
    kr_date = validate_date(args.date or args.kr_date or today("Asia/Seoul"), "KR date")
    us_date = validate_date(args.date or args.us_date or today("America/New_York"), "US date")
    kr_symbols = parse_symbol_list(args.kr, KR_SYMBOL_PATTERN)
    us_symbols = parse_symbol_list(args.us, US_SYMBOL_PATTERN, uppercase=True)
    runs: list[MarketRun] = []
    if kr_symbols:
        base = args.kr_run_id or f"{kr_date}_{time_tag('Asia/Seoul', 'KST')[-8:]}_{tag}"
        runs.append(MarketRun("kr", kr_symbols, kr_date, base))
    if us_symbols:
        base = args.us_run_id or f"{us_date}_{time_tag('America/New_York', 'ET')[-7:]}_{tag}"
        runs.append(MarketRun("us", us_symbols, us_date, base))
    if not runs:
        raise SystemExit("Provide at least one of --kr or --us")
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kr", help="Comma-separated Korean 6-digit stock codes.")
    parser.add_argument("--us", help="Comma-separated US tickers.")
    parser.add_argument("--date", help="Apply one YYYYMMDD date to both markets.")
    parser.add_argument("--kr-date", help="KR session date in YYYYMMDD. Defaults to today in KST.")
    parser.add_argument("--us-date", help="US session date in YYYYMMDD. Defaults to today in New York.")
    parser.add_argument("--kr-run-id", help="Explicit KR run id.")
    parser.add_argument("--us-run-id", help="Explicit US run id.")
    parser.add_argument("--run-tag", default="custom_report", help="Safe suffix for generated run IDs.")
    parser.add_argument("--skip-calendar-check", action="store_true", help="Run even if the calendar guard is closed.")
    args = parser.parse_args()

    runs = build_runs(args)
    verify_vps_auth()
    summaries: list[dict[str, Any]] = []
    for run in runs:
        if not args.skip_calendar_check:
            check_calendar(run)
        run_market(run)
        summaries.append(read_report_summary(run))
    print(json.dumps({"status": "success", "runs": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
