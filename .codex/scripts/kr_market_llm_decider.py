#!/usr/bin/env python3
"""LLM decision layer for KRX paper-trading automation.

The LLM can only approve, reduce, or block already computed candidates. It
cannot create orders, bypass vps-only mode, or weaken the deterministic risk
checks in kr_market_auto_run.py.
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


def extract_base_url(text: str) -> str | None:
    matches = re.findall(r"(?im)^\s*Base\s*URL\s*:\s*(https?://\S+)\s*$", text)
    if not matches:
        return None
    local = [item for item in matches if "127.0.0.1" in item or "localhost" in item]
    return (local[-1] if local else matches[-1]).strip()


def extract_model(text: str) -> str | None:
    match = re.search(r"(?im)^\s*Model\s*:\s*([A-Za-z0-9._/-]+)", text)
    return match.group(1).strip() if match else None


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
    if explicit:
        return normalize_base_url(explicit)
    return DEFAULT_BASE_URL


def load_model() -> str:
    explicit = os.environ.get("KR_MARKET_LLM_MODEL") or os.environ.get("CLIPROXY_MODEL")
    if explicit:
        return explicit
    return DEFAULT_MODEL


def compact_context(payload: dict[str, Any]) -> dict[str, Any]:
    account = payload.get("account_before", {}).get("account", {})
    safety = payload.get("safety", {})
    return {
        "slot": payload.get("slot"),
        "date": payload.get("date"),
        "mode": "vps",
        "news_summary": payload.get("news_summary", {}),
        "headlines": payload.get("headlines", [])[:8],
        "account": {
            "total_eval": account.get("deposit", {}).get("total_eval"),
            "deposit": account.get("deposit", {}).get("deposit"),
            "holdings": account.get("holdings", []),
        },
        "signals": payload.get("signals", []),
        "planned_buys": payload.get("planned_buys", []),
        "submitted_sells": payload.get("submitted_sells", []),
        "risk": {
            "total_new_buy_pct": safety.get("total_new_buy_pct"),
            "daily_loss_pct": safety.get("daily_loss_pct"),
            "take_profit_pct": safety.get("take_profit_pct"),
            "stop_loss_pct": safety.get("stop_loss_pct"),
            "min_buy_strength": safety.get("min_buy_strength"),
            "bought_today_before": safety.get("bought_today_before"),
        },
    }


def system_prompt() -> str:
    return (
        "You are a conservative Korean-market paper-trading decision layer. "
        "You only approve, reduce, or block the BUY candidates already proposed "
        "by deterministic strategy code. You must not invent new symbols, "
        "increase quantities above proposed quantities, suggest prod trading, "
        "or ignore risk limits. Return JSON only."
    )


def user_prompt(context: dict[str, Any]) -> str:
    return (
        "Decide whether to approve the proposed Korean-market vps paper-trading "
        "BUY candidates. Consider the news, macro regime, signals, holdings, "
        "and risk budget. Output exactly this JSON shape:\n"
        "{\n"
        '  "market_regime": "string",\n'
        '  "risk_level": "low|normal|high",\n'
        '  "should_trade": true,\n'
        '  "approved_buys": [\n'
        '    {"code": "string", "max_quantity": 0, "confidence": 0.0, "reason": "string"}\n'
        "  ],\n"
        '  "blocked_symbols": [\n'
        '    {"code": "string", "reason": "string"}\n'
        "  ],\n"
        '  "notes": "string"\n'
        "}\n\n"
        "Rules: approve only planned_buys codes; max_quantity must be <= proposed "
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
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = float(os.environ.get("KR_MARKET_LLM_TIMEOUT", "90"))
    response = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=request_body,
        timeout=timeout,
    )
    if response.status_code in {400, 422} and "response_format" in response.text:
        request_body.pop("response_format", None)
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=request_body,
            timeout=timeout,
        )
    response.raise_for_status()
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    decision = extract_json(text)
    return {
        "status": "success",
        "provider": "cliproxyapi",
        "base_url": base_url,
        "model": model,
        "decision": sanitize_decision(decision, context.get("planned_buys", [])),
    }


def sanitize_decision(decision: dict[str, Any], planned_buys: list[dict[str, Any]]) -> dict[str, Any]:
    planned_by_code = {str(order.get("code")): order for order in planned_buys}
    approved = []
    for item in decision.get("approved_buys", []) or []:
        code = str(item.get("code", ""))
        if code not in planned_by_code:
            continue
        proposed_qty = int(planned_by_code[code].get("quantity") or 0)
        max_qty = int(float(item.get("max_quantity") or 0))
        if max_qty <= 0:
            continue
        approved.append({
            "code": code,
            "max_quantity": min(max_qty, proposed_qty),
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0))),
            "reason": str(item.get("reason") or "")[:500],
        })

    blocked = []
    for item in decision.get("blocked_symbols", []) or []:
        code = str(item.get("code", ""))
        if code in planned_by_code:
            blocked.append({
                "code": code,
                "reason": str(item.get("reason") or "")[:500],
            })

    return {
        "market_regime": str(decision.get("market_regime") or "unknown")[:100],
        "risk_level": str(decision.get("risk_level") or "high")[:20],
        "should_trade": bool(decision.get("should_trade", bool(approved))),
        "approved_buys": approved,
        "blocked_symbols": blocked,
        "notes": str(decision.get("notes") or "")[:1000],
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

    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        payload = json.load(sys.stdin)

    try:
        result = call_llm(payload)
    except Exception as exc:
        result = fail_closed(str(exc))

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
