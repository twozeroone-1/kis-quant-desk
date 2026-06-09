from __future__ import annotations

from pathlib import Path

from scripts import trading_safety_gate


def test_required_agent_rules_are_present() -> None:
    root = Path(__file__).resolve().parents[1]

    assert trading_safety_gate.check_agents(root) == []


def test_import_gate_markers_are_present() -> None:
    root = Path(__file__).resolve().parents[1]

    assert trading_safety_gate.check_import_gate(root) == []


def test_secret_assignment_pattern_detects_hardcoded_secret(tmp_path: Path) -> None:
    candidate = tmp_path / "bad.py"
    candidate.write_text('appsecret = "12345678901234567890"\n', encoding="utf-8")

    text = candidate.read_text(encoding="utf-8")

    assert trading_safety_gate.SECRET_ASSIGNMENT.search(text)
