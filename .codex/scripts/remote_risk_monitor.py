#!/usr/bin/env python3
"""Remote Strategy Builder risk monitor.

Polls the remote Strategy Builder account endpoint and submits mock sell orders
for an explicit allowlist of positions when take-profit or stop-loss thresholds
are reached. Credentials are read from environment variables only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_POSITIONS = {
    "329180": {"name": "HD현대중공업", "take_profit_pct": 4.0, "stop_loss_pct": -2.0},
    "010120": {"name": "LS ELECTRIC", "take_profit_pct": 4.0, "stop_loss_pct": -2.0},
    "005380": {"name": "현대차", "take_profit_pct": 4.0, "stop_loss_pct": -2.0},
    "069500": {"name": "KODEX 200", "take_profit_pct": 25.0, "stop_loss_pct": 15.0},
    "122630": {"name": "KODEX 레버리지", "take_profit_pct": 35.0, "stop_loss_pct": 20.0},
}


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=self.headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {raw[:300]}") from exc


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def profit_rate(current_price: float, avg_price: float) -> float:
    if avg_price <= 0:
        return 0.0
    return (current_price - avg_price) / avg_price * 100


def clear_account_cache(client: ApiClient) -> None:
    try:
        client.request("POST", "/api/orders/account/clear-cache")
    except Exception as exc:
        print(f"[{now()}] WARN cache clear failed: {exc}", flush=True)


def monitor_once(
    client: ApiClient,
    positions: dict[str, dict[str, Any]],
    state_path: Path,
    dry_run: bool,
) -> None:
    state = load_state(state_path)
    account = client.request("GET", "/api/orders/account")
    holdings = {
        str(item.get("stock_code")): item
        for item in account.get("holdings", [])
    }

    for code, rule in positions.items():
        existing = state.get(code, {})
        if existing.get("exit_submitted"):
            print(f"[{now()}] {code} already exit-submitted: {existing.get('order_id')}", flush=True)
            continue

        holding = holdings.get(code)
        if not holding or int(holding.get("quantity") or 0) <= 0:
            print(f"[{now()}] {code} not held; skip", flush=True)
            continue

        qty = int(holding.get("quantity") or 0)
        name = str(holding.get("stock_name") or rule.get("name") or code)
        avg_price = float(holding.get("avg_price") or 0)
        current_price = float(holding.get("current_price") or 0)
        rate = profit_rate(current_price, avg_price)
        tp = float(rule["take_profit_pct"])
        sl = float(rule["stop_loss_pct"])

        trigger = None
        if rate >= tp:
            trigger = "take_profit"
        elif rate <= sl:
            trigger = "stop_loss"

        print(
            f"[{now()}] {name}({code}) qty={qty} avg={avg_price:.0f} "
            f"current={current_price:.0f} pnl={rate:.2f}% rule=TP {tp:.1f}% / SL {sl:.1f}%",
            flush=True,
        )

        if not trigger:
            continue

        reason = (
            f"자동 리스크 감시 {trigger}: 수익률 {rate:.2f}% "
            f"(익절 {tp:.1f}%, 손절 {sl:.1f}%)"
        )
        order = {
            "stock_code": code,
            "stock_name": name,
            "action": "SELL",
            "order_type": "market",
            "price": 0,
            "quantity": qty,
            "signal_reason": reason,
        }

        if dry_run:
            print(f"[{now()}] DRY-RUN would submit: {json.dumps(order, ensure_ascii=False)}", flush=True)
            continue

        result = client.request("POST", "/api/orders/execute", order)
        if result.get("status") == "success":
            order_id = (result.get("data") or {}).get("order_id")
            state[code] = {
                "exit_submitted": True,
                "order_id": order_id,
                "trigger": trigger,
                "profit_rate": rate,
                "submitted_at": now(),
            }
            save_state(state_path, state)
            clear_account_cache(client)
            print(f"[{now()}] EXIT submitted {name}({code}) order_id={order_id}", flush=True)
        else:
            print(f"[{now()}] ERROR exit failed {name}({code}): {result}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://ww.tailea9a3f.ts.net:8081")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--state-file", default="/tmp/kis_remote_risk_monitor_state.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    username = os.environ.get("KIS_BUILDER_USER")
    password = os.environ.get("KIS_BUILDER_PASSWORD")
    if not username or not password:
        print("KIS_BUILDER_USER and KIS_BUILDER_PASSWORD are required", file=sys.stderr)
        return 2

    client = ApiClient(args.base_url, username, password)
    state_path = Path(args.state_file)

    while True:
        try:
            monitor_once(client, DEFAULT_POSITIONS, state_path, args.dry_run)
        except Exception as exc:
            print(f"[{now()}] ERROR monitor cycle failed: {exc}", flush=True)

        if args.once:
            return 0
        time.sleep(max(args.interval, 5))


if __name__ == "__main__":
    raise SystemExit(main())
