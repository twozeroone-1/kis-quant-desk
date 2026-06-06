#!/usr/bin/env python3
"""Telegram approval gate for live KIS automation orders."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE_DIR = PROJECT_ROOT / ".codex" / "runtime" / "kr_market_auto_prod" / "telegram_approvals"


class TelegramApprovalError(RuntimeError):
    """Raised when the approval request cannot be sent or polled safely."""


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on", "allow"}


def telegram_approval_enabled() -> bool:
    return env_truthy("KIS_PROD_TELEGRAM_APPROVAL")


def _approval_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _approval_payload(value[key])
            for key in sorted(value)
            if key != "confirm_prod"
        }
    if isinstance(value, list):
        return [_approval_payload(item) for item in value]
    return value


def canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        _approval_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def approval_timeout_seconds() -> int:
    raw = os.environ.get("TELEGRAM_APPROVAL_TIMEOUT_SECONDS", "900").strip()
    return max(1, int(float(raw)))


def poll_interval_seconds() -> float:
    raw = os.environ.get("TELEGRAM_POLL_INTERVAL_SECONDS", "2").strip()
    return max(0.2, float(raw))


class TelegramApprovalClient:
    def __init__(self, token: str):
        if not token:
            raise TelegramApprovalError("TELEGRAM_BOT_TOKEN is required")
        self._base_url = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"
        try:
            response = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as exc:
            raise TelegramApprovalError(f"telegram {method} request failed: {type(exc).__name__}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramApprovalError(
                f"telegram {method} returned non-json status {response.status_code}"
            ) from exc

        if not response.ok or not data.get("ok", False):
            description = str(data.get("description") or response.text or "")[:300]
            raise TelegramApprovalError(
                f"telegram {method} failed: status={response.status_code} description={description}"
            )
        return data

    def send_message(self, chat_id: str, text: str, reply_markup: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
        )

    def get_updates(self, offset: int | None, timeout_seconds: float) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": int(max(0, timeout_seconds)),
            "allowed_updates": ["callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        data = self._request("getUpdates", payload)
        return list(data.get("result") or [])

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self._request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def edit_message_reply_markup(self, chat_id: str, message_id: int, reply_markup: dict[str, Any]) -> None:
        self._request(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        )


def _format_number(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _format_order_type(value: Any) -> str:
    labels = {"market": "시장가", "limit": "지정가"}
    return labels.get(str(value or "").lower(), str(value or "-"))


def _format_message(
    approval_id: str,
    approval_hash: str,
    details: dict[str, Any],
    expires_at: datetime,
) -> str:
    action = details.get("action") or "-"
    stock_name = details.get("stock_name") or details.get("stock_code") or "-"
    stock_code = details.get("stock_code") or "-"
    amount = details.get("estimated_amount")
    if amount in (None, ""):
        price = float(details.get("price") or 0)
        quantity = int(float(details.get("quantity") or 0))
        amount = price * quantity if price and quantity else None

    lines = [
        "실전 주문 승인 요청",
        "",
        "- 모드: 실전투자(prod)",
        f"- 시장: {details.get('market_label') or '국내'}",
        f"- 액션: {action}",
        f"- 종목: {stock_name}({stock_code})",
        f"- 수량: {_format_number(details.get('quantity'))}",
        f"- 주문유형/가격: {_format_order_type(details.get('order_type'))} / {_format_number(details.get('price'))}",
        f"- 예상금액: {_format_number(amount)}원",
        f"- 신호 강도: {details.get('signal_strength', '-')}",
        f"- 사유: {details.get('reason') or '-'}",
        f"- 손절/익절 설정: {details.get('protection_summary') or '-'}",
        f"- 만료 시간: {expires_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 승인 ID: {approval_id}",
        f"- Payload hash: {approval_hash[:16]}",
    ]
    return "\n".join(lines)


def _write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _callback_chat_id(callback_query: dict[str, Any]) -> str:
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is not None:
        return str(chat_id)
    from_user = callback_query.get("from") or {}
    user_id = from_user.get("id")
    return str(user_id) if user_id is not None else ""


def _remove_buttons(client: Any, chat_id: str, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        client.edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
    except Exception:
        return


def request_approval(
    payload: dict[str, Any],
    details: dict[str, Any],
    *,
    store_dir: Path | None = None,
    client: Any | None = None,
    allowed_chat_id: str | None = None,
    timeout_seconds: int | None = None,
    poll_interval: float | None = None,
    time_fn=time.monotonic,
    sleep_fn=time.sleep,
    now_fn=lambda: datetime.now().astimezone(),
) -> dict[str, Any]:
    """Ask Telegram for approval and return the terminal approval record."""

    chat_id = str(allowed_chat_id or os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "")).strip()
    if not chat_id:
        return {"status": "error", "message": "TELEGRAM_ALLOWED_CHAT_ID is required"}

    client = client or TelegramApprovalClient(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    store_dir = store_dir or DEFAULT_STORE_DIR
    timeout_seconds = approval_timeout_seconds() if timeout_seconds is None else max(1, int(timeout_seconds))
    poll_interval = poll_interval_seconds() if poll_interval is None else max(0.2, float(poll_interval))

    approval_id = secrets.token_hex(8)
    approve_token = f"a_{secrets.token_urlsafe(12)}"
    reject_token = f"r_{secrets.token_urlsafe(12)}"
    created_at = now_fn()
    expires_at = created_at + timedelta(seconds=timeout_seconds)
    approval_hash = payload_hash(payload)
    record_path = store_dir / f"{approval_id}.json"
    record = {
        "approval_id": approval_id,
        "status": "pending",
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "payload_hash": approval_hash,
        "payload": _approval_payload(payload),
        "details": details,
        "callback_tokens": {"approve": approve_token, "reject": reject_token},
    }
    _write_record(record_path, record)

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "승인", "callback_data": approve_token},
                {"text": "거절", "callback_data": reject_token},
            ]
        ]
    }
    try:
        message = client.send_message(
            chat_id,
            _format_message(approval_id, approval_hash, details, expires_at),
            reply_markup,
        )
    except Exception as exc:
        record.update({"status": "error", "message": str(exc)[:1000], "completed_at": now_fn().isoformat()})
        _write_record(record_path, record)
        return {key: value for key, value in record.items() if key != "callback_tokens"}
    message_id = ((message.get("result") or {}).get("message_id"))
    record["telegram_message"] = {
        "chat_id": chat_id,
        "message_id": message_id,
    }
    _write_record(record_path, record)

    offset: int | None = None
    deadline = time_fn() + timeout_seconds
    while time_fn() < deadline:
        try:
            updates = client.get_updates(offset, min(poll_interval, max(0.0, deadline - time_fn())))
        except Exception as exc:
            record.update({"status": "error", "message": str(exc), "completed_at": now_fn().isoformat()})
            _write_record(record_path, record)
            return {key: value for key, value in record.items() if key != "callback_tokens"}

        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                offset = int(update_id) + 1
            callback_query = update.get("callback_query") or {}
            callback_data = callback_query.get("data")
            if callback_data not in {approve_token, reject_token}:
                continue
            callback_id = str(callback_query.get("id") or "")
            response_chat_id = _callback_chat_id(callback_query)
            if response_chat_id != chat_id:
                if callback_id:
                    try:
                        client.answer_callback_query(callback_id, "허용되지 않은 채팅입니다.")
                    except Exception:
                        pass
                continue

            status = "approved" if callback_data == approve_token else "rejected"
            if callback_id:
                try:
                    client.answer_callback_query(callback_id, "승인되었습니다." if status == "approved" else "거절되었습니다.")
                except Exception:
                    pass
            _remove_buttons(client, chat_id, message_id)
            record.update(
                {
                    "status": status,
                    "completed_at": now_fn().isoformat(),
                    "response_chat_id": response_chat_id,
                    "callback_query_id": callback_id,
                }
            )
            _write_record(record_path, record)
            return {key: value for key, value in record.items() if key != "callback_tokens"}

        sleep_fn(poll_interval)

    _remove_buttons(client, chat_id, message_id)
    record.update({"status": "timeout", "completed_at": now_fn().isoformat()})
    _write_record(record_path, record)
    return {key: value for key, value in record.items() if key != "callback_tokens"}
