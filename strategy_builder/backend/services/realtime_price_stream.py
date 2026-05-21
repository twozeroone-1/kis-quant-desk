"""KIS real-time price stream for strategy review protections."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import websockets
from fastapi import WebSocket

import kis_auth as ka
from backend import get_current_mode, is_authenticated
from core import overseas_data_fetcher

logger = logging.getLogger(__name__)

DOMESTIC_CCN_COLUMNS = [
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN",
    "PRDY_VRSS", "PRDY_CTRT", "WGHN_AVRG_STCK_PRC", "STCK_OPRC",
    "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL", "ACML_VOL",
    "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU",
    "CTTR", "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE",
    "PRDY_VOL_VRSS_ACML_VOL_RATE", "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN",
    "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN", "HGPR_VRSS_PRPR",
    "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1",
    "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "VOL_TNRT",
    "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
]

OVERSEAS_CCN_COLUMNS = [
    "SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN",
    "HIGH", "LOW", "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK",
    "VBID", "VASK", "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP",
]

TickHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class PriceSubscription:
    market: str
    stock_code: str
    exchange: str | None = None

    @property
    def key(self) -> str:
        return f"{self.market}:{self.stock_code}:{self.exchange or ''}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _normalize_subscription(item: dict[str, Any]) -> PriceSubscription | None:
    market = str(item.get("market") or "domestic")
    stock_code = str(item.get("stock_code") or item.get("symbol") or "").strip().upper()
    exchange = item.get("exchange")
    if market not in {"domestic", "us"} or not stock_code:
        return None
    return PriceSubscription(market=market, stock_code=stock_code, exchange=str(exchange).upper() if exchange else None)


def _domestic_message(sub: PriceSubscription) -> dict[str, Any]:
    return ka.data_fetch("H0STCNT0", "1", {"tr_key": sub.stock_code})


def _overseas_tr_key(sub: PriceSubscription) -> str:
    resolution = overseas_data_fetcher.resolve_exchange(sub.stock_code, sub.exchange)
    return f"D{resolution.price_exchange}{resolution.symbol}"


def _overseas_message(sub: PriceSubscription) -> dict[str, Any]:
    return ka.data_fetch("HDFSCNT0", "1", {"tr_key": _overseas_tr_key(sub)})


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


class RealtimePriceStream:
    """Single KIS websocket connection shared by review UI and protection engine."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._restart = asyncio.Event()
        self._lock = asyncio.Lock()
        self._protective_subscriptions: set[PriceSubscription] = set()
        self._client_subscriptions: dict[WebSocket, set[PriceSubscription]] = {}
        self._latest_ticks: dict[str, dict[str, Any]] = {}
        self._tick_handler: TickHandler | None = None
        self._connected = False
        self._last_error: str | None = None
        self._last_connected_at: str | None = None

    def set_tick_handler(self, handler: TickHandler) -> None:
        self._tick_handler = handler

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            logger.info("realtime price stream started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._connected = False

    async def set_protective_subscriptions(self, items: list[dict[str, Any]]) -> None:
        subscriptions = {
            sub for item in items
            if (sub := _normalize_subscription(item)) is not None
        }
        async with self._lock:
            if subscriptions == self._protective_subscriptions:
                return
            self._protective_subscriptions = subscriptions
        self._restart.set()

    async def add_client(self, websocket: WebSocket, items: list[dict[str, Any]]) -> None:
        subscriptions = {
            sub for item in items
            if (sub := _normalize_subscription(item)) is not None
        }
        async with self._lock:
            self._client_subscriptions[websocket] = subscriptions
        for sub in subscriptions:
            tick = self._latest_ticks.get(sub.key)
            if tick:
                await websocket.send_json({"type": "price", **tick})
        self._restart.set()

    async def remove_client(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._client_subscriptions.pop(websocket, None)
        self._restart.set()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "subscription_count": len(self._all_subscriptions_snapshot()),
            "last_connected_at": self._last_connected_at,
            "last_error": self._last_error,
            "latest_ticks": list(self._latest_ticks.values()),
        }

    def _all_subscriptions_snapshot(self) -> set[PriceSubscription]:
        subscriptions = set(self._protective_subscriptions)
        for client_subscriptions in self._client_subscriptions.values():
            subscriptions.update(client_subscriptions)
        return subscriptions

    async def _all_subscriptions(self) -> set[PriceSubscription]:
        async with self._lock:
            return self._all_subscriptions_snapshot()

    async def _run(self) -> None:
        while True:
            subscriptions = await self._all_subscriptions()
            if not subscriptions:
                self._connected = False
                self._restart.clear()
                await self._restart.wait()
                continue

            if len(subscriptions) > 40:
                self._last_error = "KIS websocket subscription limit exceeded: max 40"
                logger.warning(self._last_error)
                await asyncio.sleep(5)
                continue

            try:
                if not is_authenticated():
                    self._last_error = "KIS API authentication required"
                    await asyncio.sleep(3)
                    continue

                mode = get_current_mode()
                await asyncio.to_thread(ka.auth_ws, "vps" if mode == "vps" else "prod")
                trenv = ka.getTREnv()
                url = f"{trenv.my_url_ws}/tryitout"

                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._connected = True
                    self._last_connected_at = _now()
                    self._last_error = None
                    await self._send_subscriptions(ws, subscriptions)
                    await self._consume(ws, subscriptions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                logger.warning("realtime price stream failed: %s", exc)
                await asyncio.sleep(2)

    async def _send_subscriptions(self, ws: websockets.ClientConnection, subscriptions: set[PriceSubscription]) -> None:
        for sub in sorted(subscriptions, key=lambda item: item.key):
            msg = _overseas_message(sub) if sub.market == "us" else _domestic_message(sub)
            await ws.send(json.dumps(msg))
            await asyncio.sleep(0.05)

    async def _consume(self, ws: websockets.ClientConnection, subscriptions: set[PriceSubscription]) -> None:
        key_by_tr_key: dict[str, PriceSubscription] = {}
        for sub in subscriptions:
            if sub.market == "us":
                key_by_tr_key[_overseas_tr_key(sub)] = sub
            else:
                key_by_tr_key[sub.stock_code] = sub

        receive_task = asyncio.create_task(ws.recv())
        restart_task = asyncio.create_task(self._restart.wait())
        try:
            while True:
                done, pending = await asyncio.wait(
                    {receive_task, restart_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if restart_task in done:
                    self._restart.clear()
                    receive_task.cancel()
                    return
                if receive_task in done:
                    raw = receive_task.result()
                    await self._handle_raw(raw, key_by_tr_key)
                    receive_task = asyncio.create_task(ws.recv())
        finally:
            for task in (receive_task, restart_task):
                if not task.done():
                    task.cancel()
            self._connected = False

    async def _handle_raw(self, raw: str | bytes, key_by_tr_key: dict[str, PriceSubscription]) -> None:
        text = raw.decode() if isinstance(raw, bytes) else raw
        if not text:
            return

        if text[0] not in {"0", "1"}:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return
            if payload.get("header", {}).get("tr_id") == "PINGPONG":
                return
            if payload.get("body", {}).get("rt_cd") not in (None, "0"):
                self._last_error = payload.get("body", {}).get("msg1")
            return

        parts = text.split("|", 3)
        if len(parts) < 4:
            return
        tr_id = parts[1]
        values = parts[3].split("^")
        columns = OVERSEAS_CCN_COLUMNS if tr_id == "HDFSCNT0" else DOMESTIC_CCN_COLUMNS
        for row_values in _chunks(values, len(columns)):
            if len(row_values) < len(columns):
                continue
            row = dict(zip(columns, row_values))
            tick = self._tick_from_row(tr_id, row, key_by_tr_key)
            if tick:
                await self._publish_tick(tick)

    def _tick_from_row(
        self,
        tr_id: str,
        row: dict[str, Any],
        key_by_tr_key: dict[str, PriceSubscription],
    ) -> dict[str, Any] | None:
        if tr_id == "HDFSCNT0":
            tr_key = str(row.get("SYMB") or "")
            sub = key_by_tr_key.get(tr_key)
            price = _to_float(row.get("LAST"))
            volume = _to_float(row.get("TVOL"))
            tick_time = str(row.get("XHMS") or row.get("KHMS") or "")
        else:
            tr_key = str(row.get("MKSC_SHRN_ISCD") or "")
            sub = key_by_tr_key.get(tr_key)
            price = _to_float(row.get("STCK_PRPR"))
            volume = _to_float(row.get("ACML_VOL"))
            tick_time = str(row.get("STCK_CNTG_HOUR") or "")

        if sub is None or price <= 0:
            return None
        return {
            "market": sub.market,
            "stock_code": sub.stock_code,
            "exchange": sub.exchange,
            "price": price,
            "volume": volume,
            "tick_time": tick_time,
            "received_at": _now(),
        }

    async def _publish_tick(self, tick: dict[str, Any]) -> None:
        key = PriceSubscription(
            market=str(tick["market"]),
            stock_code=str(tick["stock_code"]),
            exchange=tick.get("exchange"),
        ).key
        self._latest_ticks[key] = tick

        handler = self._tick_handler
        if handler:
            await handler(tick)

        stale_clients: list[WebSocket] = []
        async with self._lock:
            client_items = list(self._client_subscriptions.items())
        for websocket, subscriptions in client_items:
            if any(sub.key == key for sub in subscriptions):
                try:
                    await websocket.send_json({"type": "price", **tick})
                except Exception:
                    stale_clients.append(websocket)
        for websocket in stale_clients:
            await self.remove_client(websocket)


_stream: RealtimePriceStream | None = None


def get_realtime_price_stream() -> RealtimePriceStream:
    global _stream
    if _stream is None:
        _stream = RealtimePriceStream()
    return _stream
