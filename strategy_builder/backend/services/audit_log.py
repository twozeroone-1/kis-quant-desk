"""Order audit log writer with daily JSONL rotation."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path("/var/log/kis/orders")
DEFAULT_RETENTION_DAYS = 365


def _log_dir() -> Path:
    return Path(os.getenv("AUDIT_LOG_DIR", str(DEFAULT_LOG_DIR)))


def _retention_days() -> int:
    raw = os.getenv("AUDIT_LOG_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Invalid AUDIT_LOG_RETENTION_DAYS=%s; using %s", raw, DEFAULT_RETENTION_DAYS)
        return DEFAULT_RETENTION_DAYS


def _daily_path(now: datetime) -> Path:
    return _log_dir() / f"orders-{now.date().isoformat()}.jsonl"


def _cleanup_expired_logs(now: datetime) -> None:
    cutoff = now.date() - timedelta(days=_retention_days())
    log_dir = _log_dir()
    if not log_dir.exists():
        return

    for path in log_dir.glob("orders-*.jsonl"):
        try:
            day = datetime.strptime(path.stem.removeprefix("orders-"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove expired audit log %s: %s", path, exc)


def write_order_audit(record: dict[str, Any]) -> None:
    """Append one UTC-stamped order audit event."""
    now = datetime.now(timezone.utc)
    payload = {
        "timestamp_utc": now.isoformat(),
        "authenticated_user": record.get("authenticated_user") or "unknown",
        "mode": record.get("mode"),
        "action": record.get("action"),
        "stock_code": record.get("stock_code"),
        "quantity": record.get("quantity"),
        "price": record.get("price"),
        "order_type": record.get("order_type"),
        "result": record.get("result"),
        "order_id": record.get("order_id"),
        "error_message": record.get("error_message"),
    }

    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with _daily_path(now).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            handle.write("\n")
        _cleanup_expired_logs(now)
    except OSError as exc:
        logger.error("Failed to write order audit log: %s", exc)
