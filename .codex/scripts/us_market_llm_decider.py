#!/usr/bin/env python3
"""LLM decision layer for US paper-trading automation.

The LLM can only approve, reduce, or block already computed BUY candidates. It
cannot create symbols, bypass vps-only mode, or weaken deterministic risk gates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
DEFAULT_MODEL = "gpt-5-codex"


def _read_text_file(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def redact_secret(text: str) -> str:
    text = re.sub(r"(?i)(api key:\s*)[A-Za-z0-9._-]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._-]+", r"\1<redacted>", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer <redacted>", text)
    return text


def extract_api_key(text: str) -> str:
    match = re.search(r"(?im)^\s*API\s*key\s*:\s*([A-Za-z0-9._-]+)\s*$", text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if re.fullmatch(r"[A-Za-z0-9._-]+", stripped):
        return stripped
    raise RuntimeError("API key file found, but no parseable API key was detected.")


def load_api_key() -> str:
    for name in ("CLIPROXY_API_KEY", "CLIPROXYAPI_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in ("CLIPROXY_API_KEY_FILE", "CLIPROXYAPI_KEY_FILE", "OPENAI_API_KEY_FILE"):
        path = os.environ.get(name, "").strip()
        if path and Path(path).expanduser().exists():
            return extract_api_key(_read_text_file(path))
    raise RuntimeError(
        "No CLIProxy/OpenAI API key found. Set CLIPROXY_API_KEY, OPENAI_API_KEY, "
        "or CLIPROXY_API_KEY_FILE."
    )


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return DEFAULT_BASE_URL
    return value if value.endswith("/v1") else f"{value}/v1"


def load_base_url() -> str:
    explicit = (
        os.environ.get("CLIPROXY_API_BASE")
        or os.environ.get("CLIPROXYAPI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
    )
    return normalize_base_url(explicit or DEFAULT_BASE_URL)


def load_model() -> str:
    return os.environ.get("US_MARKET_LLM_MODEL") or os.environ.get("KR_MARKET_LLM_MODEL") or os.environ.get("CLIPROXY_MODEL") or DEFAULT_MODEL


def compact_context(payload: dict[str, Any]) -> dict[str, Any]:
    account = payload.get("account", {})
    strategy = payload.get("strategy", {})
    return {
        "slot": payload.get("slot"),
        "session_date": payload.get("date"),
        "mode": "vps",
        "currency": "USD",
        "news_summary": payload.get("news_summary", {}),
        "headlines": payload.get("headlines", [])[:8],
        "account": {
            "equity": account.get("equity"),
            "cash": account.get("cash"),
            "holdings": account.get("holdings", []),
        },
        "signals": payload.get("signals", []),
        "planned_buys": payload.get("planned_buys", []),
        "submitted_sells": payload.get("submitted_sells", []),
        "risk": strategy.get("risk", {}),
    }


def system_prompt() -> str:
    return (
        "You are a conservative US-market paper-trading decision layer. "
        "Approve, reduce, or block only the BUY candidates proposed by the "
        "deterministic strategy. Do not invent symbols, increase quantities, "
        "suggest prod trading, or ignore risk limits. Return JSON only."
    )


def user_prompt(context: dict[str, Any]) -> str:
    return (
        "Decide whether to approve proposed US-market vps paper-trading BUY "
        "candidates. Consider news, macro regime, signals, holdings, and risk. "
        "Output exactly this JSON shape:\n"
        "{\n"
        '  "market_regime": "string",\n'
        '  "risk_level": "low|normal|high",\n'
        '  "should_trade": true,\n'
        '  "approved_buys": [\n'
        '    {"symbol": "string", "max_quantity": 0, "confidence": 0.0, "reason": "string"}\n'
        "  ],\n"
        '  "blocked_symbols": [\n'
        '    {"symbol": "string", "reason": "string"}\n'
        "  ],\n"
        '  "notes": "string"\n'
        "}\n\n"
        "Rules: approve only planned_buys symbols; max_quantity must be <= proposed "
        "quantity; confidence must be 0..1; if uncertainty is high, block. "
        "Context:\n"
        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def sanitize_decision(decision: dict[str, Any], planned_buys: list[dict[str, Any]]) -> dict[str, Any]:
    planned_by_symbol = {str(order.get("symbol")).upper(): order for order in planned_buys}
    approved = []
    for item in decision.get("approved_buys", []) or []:
        symbol = str(item.get("symbol", "")).upper()
        if symbol not in planned_by_symbol:
            continue
        proposed_qty = int(planned_by_symbol[symbol].get("quantity") or 0)
        max_qty = int(float(item.get("max_quantity") or 0))
        if max_qty <= 0:
            continue
        approved.append({
            "symbol": symbol,
            "max_quantity": min(max_qty, proposed_qty),
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0))),
            "reason": str(item.get("reason") or "")[:500],
        })

    blocked = []
    for item in decision.get("blocked_symbols", []) or []:
        symbol = str(item.get("symbol", "")).upper()
        if symbol in planned_by_symbol:
            blocked.append({"symbol": symbol, "reason": str(item.get("reason") or "")[:500]})

    return {
        "market_regime": str(decision.get("market_regime") or "unknown")[:100],
        "risk_level": str(decision.get("risk_level") or "high")[:20],
        "should_trade": bool(decision.get("should_trade", bool(approved))),
        "approved_buys": approved,
        "blocked_symbols": blocked,
        "notes": str(decision.get("notes") or "")[:1000],
    }


def call_llm(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = load_base_url()
    model = load_model()
    api_key = load_api_key()
    context = compact_context(payload)
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(context)},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = float(os.environ.get("US_MARKET_LLM_TIMEOUT", os.environ.get("KR_MARKET_LLM_TIMEOUT", "90")))
    response = requests.post(f"{base_url}/chat/completions", headers=headers, json=request_body, timeout=timeout)
    if response.status_code in {400, 422} and "response_format" in response.text:
        request_body.pop("response_format", None)
        response = requests.post(f"{base_url}/chat/completions", headers=headers, json=request_body, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    return {
        "status": "success",
        "provider": "cliproxyapi",
        "base_url": base_url,
        "model": model,
        "decision": sanitize_decision(extract_json(text), context.get("planned_buys", [])),
    }


def fail_closed(message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "provider": "cliproxyapi",
        "error": redact_secret(message)[:1000],
        "decision": {
            "market_regime": "unknown",
            "risk_level": "high",
            "should_trade": False,
            "approved_buys": [],
            "blocked_symbols": [],
            "notes": "LLM decision failed; automation continues without LLM gating.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="Payload JSON path. Defaults to stdin.")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else json.load(sys.stdin)
    try:
        result = call_llm(payload)
    except Exception as exc:
        result = fail_closed(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
