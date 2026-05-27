#!/usr/bin/env python3
"""Lightweight .kis.yaml guardrail checks for Codex-generated strategies."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


OPERATORS = {
    "greater_than",
    "less_than",
    "cross_above",
    "cross_below",
    "equals",
    "not_equal",
    "breaks",
}
SPECIAL_INDICATORS = {"open", "high", "low", "close", "volume"}
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def walk_strings(value: Any, path: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, str) and value.startswith("$"):
        findings.append(path or "<root>")
    elif isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            findings.extend(walk_strings(item, next_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            findings.extend(walk_strings(item, f"{path}[{idx}]"))
    return findings


def validate_document(data: Any, source: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["document must be a YAML mapping"]

    if data.get("version") != "1.0":
        errors.append('version must be "1.0"')

    strategy = data.get("strategy")
    if not isinstance(strategy, dict):
        return errors + ["strategy must be a mapping"]

    strategy_id = strategy.get("id")
    if not isinstance(strategy_id, str) or not ID_RE.match(strategy_id):
        errors.append("strategy.id must be snake_case using lowercase letters, digits, and underscores")

    if "risk" in strategy:
        errors.append("risk must be a top-level key, not inside strategy")

    indicators = strategy.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        errors.append("strategy.indicators must be a non-empty list")
        indicators = []

    aliases: dict[str, str] = {}
    for idx, indicator in enumerate(indicators):
        if not isinstance(indicator, dict):
            errors.append(f"strategy.indicators[{idx}] must be a mapping")
            continue
        alias = indicator.get("alias")
        indicator_id = indicator.get("id")
        if not isinstance(alias, str) or not alias:
            errors.append(f"strategy.indicators[{idx}].alias is required")
            continue
        if alias in aliases:
            errors.append(f"duplicate indicator alias: {alias}")
        aliases[alias] = str(indicator_id or "")

    for section in ("entry", "exit"):
        block = strategy.get(section)
        if not isinstance(block, dict):
            errors.append(f"strategy.{section} must be a mapping")
            continue
        conditions = block.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            errors.append(f"strategy.{section}.conditions must be a non-empty list")
            continue
        for idx, condition in enumerate(conditions):
            prefix = f"strategy.{section}.conditions[{idx}]"
            if not isinstance(condition, dict):
                errors.append(f"{prefix} must be a mapping")
                continue

            indicator = condition.get("indicator")
            if indicator not in aliases and indicator not in SPECIAL_INDICATORS:
                errors.append(f"{prefix}.indicator must match an alias or OHLCV field")

            operator = condition.get("operator")
            if operator not in OPERATORS:
                errors.append(f"{prefix}.operator is unsupported: {operator}")

            has_value = "value" in condition
            has_compare_to = "compare_to" in condition
            if has_value == has_compare_to:
                errors.append(f"{prefix} must use exactly one of value or compare_to")
            if has_value and not is_number(condition.get("value")):
                errors.append(f"{prefix}.value must be a numeric literal")
            if has_compare_to and condition.get("compare_to") not in aliases and condition.get("compare_to") not in SPECIAL_INDICATORS:
                errors.append(f"{prefix}.compare_to must match an alias or OHLCV field")

            compare_to = condition.get("compare_to")
            if (
                isinstance(indicator, str)
                and isinstance(compare_to, str)
                and indicator != compare_to
                and aliases.get(indicator) == "macd"
                and aliases.get(compare_to) == "macd"
            ):
                errors.append(f"{prefix} compares two MACD aliases; use one alias with output/compare_output")

    risk = data.get("risk")
    if risk is not None and not isinstance(risk, dict):
        errors.append("risk must be a mapping")
    elif isinstance(risk, dict):
        for name in ("stop_loss", "take_profit", "trailing_stop"):
            item = risk.get(name)
            if item is None:
                continue
            if not isinstance(item, dict):
                errors.append(f"risk.{name} must be a mapping")
                continue
            if item.get("enabled") is True and not is_number(item.get("percent")):
                errors.append(f"risk.{name}.percent must be numeric when enabled")

    for path in walk_strings(data):
        errors.append(f"{path} uses a $param placeholder; materialize it before validation/backtest")

    return [f"{source}: {error}" for error in errors]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate common .kis.yaml guardrails.")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    all_errors: list[str] = []
    for source in args.files:
        try:
            data = yaml.safe_load(source.read_text(encoding="utf-8"))
        except Exception as exc:
            all_errors.append(f"{source}: failed to parse YAML: {exc}")
            continue
        all_errors.extend(validate_document(data, source))

    if all_errors:
        for error in all_errors:
            print(error, file=sys.stderr)
        return 1

    print(f"validated {len(args.files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
