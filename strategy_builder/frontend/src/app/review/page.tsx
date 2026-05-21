"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, Save, Settings2, ShieldCheck } from "lucide-react";
import { useAccount, useAuth } from "@/hooks";
import {
  checkProtectiveOrders,
  getProtectiveOrders,
  saveProtectiveOrder,
  saveProtectiveSettings,
  type ExitOrderType,
  type ProtectiveOrder,
  type ProtectiveRealtimeStatus,
  type ProtectiveRealtimeTick,
} from "@/lib/api";
import { getWsBase } from "@/lib/api/client";
import type { Holding } from "@/types/account";

type ReviewMarket = "domestic" | "us";

interface ReviewDraft {
  enabled: boolean;
  quantity: string;
  takeProfitEnabled: boolean;
  takeProfitTriggerPrice: string;
  takeProfitOrderType: ExitOrderType;
  takeProfitLimitPrice: string;
  stopLossEnabled: boolean;
  stopLossTriggerPrice: string;
  stopLossOrderType: ExitOrderType;
  stopLossLimitPrice: string;
}

const marketTabs: Array<{ value: ReviewMarket; label: string }> = [
  { value: "domestic", label: "한국" },
  { value: "us", label: "미국" },
];

const formatMoney = (value?: number | null, currency = "KRW") => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString(currency === "USD" ? "en-US" : "ko-KR", {
    style: "currency",
    currency,
    maximumFractionDigits: currency === "USD" ? 2 : 0,
  });
};

const parseNumber = (value: string): number | null => {
  const parsed = Number(String(value).replaceAll(",", "").trim());
  return Number.isFinite(parsed) ? parsed : null;
};

const orderTypeLabel = (value?: string) => (value === "market" ? "시장가" : "지정가");

const holdingKey = (market: ReviewMarket, holding: Pick<Holding, "stock_code" | "exchange">) =>
  `${market}:${holding.stock_code}:${holding.exchange || ""}`;

const orderKey = (order: ProtectiveOrder) =>
  `${order.market || "domestic"}:${order.stock_code}:${order.exchange || ""}`;

function defaultDraft(holding: Holding, protection?: ProtectiveOrder): ReviewDraft {
  const avgPrice = Number(holding.avg_price || protection?.entry_price || 0);
  const takeProfitTrigger = protection?.take_profit_trigger_price ?? Math.round(avgPrice * 1.04 * 100) / 100;
  const stopLossTrigger = protection?.stop_loss_price ?? Math.round(avgPrice * 0.98 * 100) / 100;

  return {
    enabled: protection?.status === "active",
    quantity: String(protection?.quantity || holding.quantity || 1),
    takeProfitEnabled: protection?.take_profit_enabled !== false,
    takeProfitTriggerPrice: String(takeProfitTrigger || ""),
    takeProfitOrderType: protection?.take_profit_order_type || "limit",
    takeProfitLimitPrice: String(protection?.take_profit_limit_price || takeProfitTrigger || ""),
    stopLossEnabled: protection?.stop_loss_enabled !== false,
    stopLossTriggerPrice: String(stopLossTrigger || ""),
    stopLossOrderType: protection?.stop_loss_order_type || "market",
    stopLossLimitPrice: String(protection?.stop_loss_limit_price || stopLossTrigger || ""),
  };
}

export default function ReviewPage() {
  const { status: authStatus } = useAuth();
  const [market, setMarket] = useState<ReviewMarket>("domestic");
  const { holdings, balance, fetchHoldings, fetchBalance, resetThrottle, isLoading } = useAccount(market);
  const [protectiveOrders, setProtectiveOrders] = useState<ProtectiveOrder[]>([]);
  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  const [monitorInterval, setMonitorInterval] = useState("15");
  const [livePrices, setLivePrices] = useState<Record<string, ProtectiveRealtimeTick>>({});
  const [realtimeStatus, setRealtimeStatus] = useState<ProtectiveRealtimeStatus | null>(null);
  const [priceStreamState, setPriceStreamState] = useState<"idle" | "connecting" | "connected" | "closed">("idle");
  const [message, setMessage] = useState("");
  const [savingSymbol, setSavingSymbol] = useState<string | null>(null);
  const [savingSettings, setSavingSettings] = useState(false);
  const [checking, setChecking] = useState(false);

  const activeProtectionByKey = useMemo(() => {
    const map = new Map<string, ProtectiveOrder>();
    for (const order of protectiveOrders) {
      if (order.status === "active" && (order.market || "domestic") === market) {
        map.set(orderKey(order), order);
      }
    }
    return map;
  }, [market, protectiveOrders]);

  const refresh = useCallback(async () => {
    resetThrottle();
    await fetchHoldings();
    await fetchBalance();
    const response = await getProtectiveOrders();
    setProtectiveOrders(response.orders || []);
    if (response.realtime) {
      setRealtimeStatus(response.realtime);
    }
    if (response.realtime?.latest_ticks) {
      const ticks: Record<string, ProtectiveRealtimeTick> = {};
      for (const tick of response.realtime.latest_ticks) {
        ticks[`${tick.market}:${tick.stock_code}:${tick.exchange || ""}`] = tick;
      }
      setLivePrices((current) => ({ ...current, ...ticks }));
    }
    if (response.settings?.monitor_interval_seconds) {
      setMonitorInterval(String(response.settings.monitor_interval_seconds));
    }
  }, [fetchBalance, fetchHoldings, resetThrottle]);

  useEffect(() => {
    if (authStatus.authenticated) {
      refresh().catch((error) => setMessage(error instanceof Error ? error.message : "전략 검토 데이터 조회 실패"));
    }
  }, [authStatus.authenticated, authStatus.mode, market, refresh]);

  useEffect(() => {
    setDrafts((current) => {
      const next = { ...current };
      for (const holding of holdings) {
        const key = holdingKey(market, holding);
        if (!next[key]) {
          next[key] = defaultDraft(holding, activeProtectionByKey.get(key));
        }
      }
      return next;
    });
  }, [activeProtectionByKey, holdings, market]);

  useEffect(() => {
    if (!authStatus.authenticated || holdings.length === 0) {
      setPriceStreamState("idle");
      return;
    }

    const websocket = new WebSocket(`${getWsBase()}/api/orders/protective/prices/ws`);
    const symbols = holdings.map((holding) => ({
      market,
      stock_code: holding.stock_code,
      exchange: holding.exchange || null,
    }));

    setPriceStreamState("connecting");
    websocket.onopen = () => {
      setPriceStreamState("connected");
      websocket.send(JSON.stringify({ type: "subscribe", symbols }));
    };
    websocket.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "price") {
          const tick = payload as ProtectiveRealtimeTick & { type: string };
          setLivePrices((current) => ({
            ...current,
            [`${tick.market}:${tick.stock_code}:${tick.exchange || ""}`]: tick,
          }));
        }
        if (payload.type === "status") {
          setRealtimeStatus(payload.realtime);
        }
        if (payload.type === "error") {
          setMessage(payload.message || "실시간 시세 연결 오류");
        }
      } catch {
        setMessage("실시간 시세 메시지를 처리하지 못했습니다.");
      }
    };
    websocket.onclose = () => setPriceStreamState("closed");
    websocket.onerror = () => setMessage("실시간 시세 연결 실패");

    return () => {
      websocket.close();
    };
  }, [authStatus.authenticated, holdings, market]);

  const updateDraft = (key: string, patch: Partial<ReviewDraft>) => {
    setDrafts((current) => ({
      ...current,
      [key]: {
        ...current[key],
        ...patch,
      },
    }));
  };

  const saveSettings = async () => {
    const interval = parseNumber(monitorInterval);
    if (!interval || interval < 5 || interval > 300) {
      setMessage("감시 주기는 5초에서 300초 사이로 설정하세요.");
      return;
    }

    setSavingSettings(true);
    setMessage("");
    try {
      const response = await saveProtectiveSettings({ monitor_interval_seconds: Math.round(interval) });
      setMonitorInterval(String(response.settings.monitor_interval_seconds));
      setMessage(`감시 주기를 ${response.settings.monitor_interval_seconds}초로 저장했습니다.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "감시 주기 저장 실패");
    } finally {
      setSavingSettings(false);
    }
  };

  const saveHolding = async (holding: Holding) => {
    const key = holdingKey(market, holding);
    const protection = activeProtectionByKey.get(key);
    const draft = drafts[key] || defaultDraft(holding, protection);
    const quantity = parseNumber(draft.quantity);
    const takeProfitTrigger = parseNumber(draft.takeProfitTriggerPrice);
    const takeProfitLimit = parseNumber(draft.takeProfitLimitPrice);
    const stopLossTrigger = parseNumber(draft.stopLossTriggerPrice);
    const stopLossLimit = parseNumber(draft.stopLossLimitPrice);

    if (!quantity || quantity <= 0 || quantity > holding.quantity) {
      setMessage(`${holding.stock_name} 감시 수량을 다시 확인하세요.`);
      return;
    }

    setSavingSymbol(key);
    setMessage("");
    try {
      await saveProtectiveOrder({
        stock_code: holding.stock_code,
        stock_name: holding.stock_name,
        quantity,
        entry_price: Number(holding.avg_price || holding.current_price),
        enabled: draft.enabled,
        take_profit_enabled: draft.takeProfitEnabled,
        take_profit_trigger_price: takeProfitTrigger,
        take_profit_order_type: draft.takeProfitOrderType,
        take_profit_limit_price: draft.takeProfitOrderType === "limit" ? takeProfitLimit : null,
        stop_loss_enabled: draft.stopLossEnabled,
        stop_loss_trigger_price: stopLossTrigger,
        stop_loss_order_type: draft.stopLossOrderType,
        stop_loss_limit_price: draft.stopLossOrderType === "limit" ? stopLossLimit : null,
        market,
        exchange: holding.exchange || null,
        currency: market === "us" ? "USD" : "KRW",
      });
      await refresh();
      setMessage(`${holding.stock_name} 감시 설정을 저장했습니다.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "감시 설정 저장 실패");
    } finally {
      setSavingSymbol(null);
    }
  };

  const runCheck = async () => {
    setChecking(true);
    setMessage("");
    try {
      const response = await checkProtectiveOrders();
      setProtectiveOrders(response.orders || []);
      if (response.realtime) {
        setRealtimeStatus(response.realtime);
      }
      if (response.settings?.monitor_interval_seconds) {
        setMonitorInterval(String(response.settings.monitor_interval_seconds));
      }
      setMessage("감시 조건을 즉시 점검했습니다.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "감시 점검 실패");
    } finally {
      setChecking(false);
    }
  };

  const currency = market === "us" ? "USD" : "KRW";
  const streamLabel =
    priceStreamState === "connected"
      ? "실시간 연결"
      : priceStreamState === "connecting"
        ? "실시간 연결 중"
        : realtimeStatus?.last_error
          ? "실시간 오류"
          : "실시간 대기";

  return (
    <div className="max-w-7xl mx-auto px-4 py-6">
      <div className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="text-display text-slate-900 dark:text-slate-100 flex items-center gap-3">
            <ShieldCheck className="w-7 h-7 text-primary" />
            전략 검토
          </h1>
          <p className="text-body text-slate-500 dark:text-slate-400 mt-1 ml-10">
            보유 종목별 손익절 감시와 조건 도달 시 매도 방식을 관리합니다
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={refresh}
            disabled={!authStatus.authenticated || isLoading}
            className="flex items-center gap-2 px-4 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-sm font-medium hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50 focus-ring"
          >
            <RefreshCw className={`w-4 h-4 ${isLoading ? "animate-spin" : ""}`} />
            새로고침
          </button>
          <button
            onClick={runCheck}
            disabled={!authStatus.authenticated || checking}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white text-sm font-medium disabled:opacity-50 focus-ring"
          >
            <ShieldCheck className="w-4 h-4" />
            지금 점검
          </button>
        </div>
      </div>

      <div className="mb-6 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="inline-flex rounded-lg border border-slate-200 dark:border-slate-700 p-1 bg-white dark:bg-slate-900 w-fit">
          {marketTabs.map((tab) => (
            <button
              key={tab.value}
              onClick={() => setMarket(tab.value)}
              className={`px-4 py-2 rounded-md text-sm font-medium focus-ring ${
                market === tab.value
                  ? "bg-primary text-white"
                  : "text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-end gap-2">
          <label className="block">
            <span className="text-caption text-slate-500">감시 주기</span>
            <div className="mt-1 flex items-center gap-2">
              <input
                value={monitorInterval}
                onChange={(event) => setMonitorInterval(event.target.value)}
                inputMode="numeric"
                className="w-24 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
              />
              <span className="text-sm text-slate-500">초</span>
            </div>
          </label>
          <button
            onClick={saveSettings}
            disabled={!authStatus.authenticated || savingSettings}
            className="flex items-center gap-2 px-4 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-sm font-medium hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50 focus-ring"
          >
            <Settings2 className="w-4 h-4" />
            저장
          </button>
        </div>
      </div>

      {!authStatus.authenticated && (
        <div className="card mb-6 border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-900/20" role="alert">
          <p className="text-body text-yellow-800 dark:text-yellow-200">
            인증이 필요합니다. 우측 상단 설정에서 인증해주세요.
          </p>
        </div>
      )}

      {message && (
        <div className="card mb-6 p-4" role="status">
          <p className="text-sm text-slate-700 dark:text-slate-200">{message}</p>
        </div>
      )}

      <section className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div className="card p-4">
          <span className="text-caption text-slate-500">보유 종목</span>
          <strong className="block text-2xl mt-1">{holdings.length}개</strong>
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">감시 중</span>
          <strong className="block text-2xl mt-1">{activeProtectionByKey.size}개</strong>
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">총 평가금액</span>
          <strong className="block text-2xl mt-1">{formatMoney(balance?.total_eval, currency)}</strong>
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">{streamLabel}</span>
          <strong className="block text-2xl mt-1">{realtimeStatus?.subscription_count || 0}개</strong>
          {realtimeStatus?.last_error && (
            <span className="block mt-1 text-xs text-red-600">{realtimeStatus.last_error}</span>
          )}
        </div>
      </section>

      <section className="space-y-4">
        {holdings.length === 0 ? (
          <div className="card p-12 text-center text-slate-500">
            {market === "us" ? "미국 보유 종목이 없습니다" : "한국 보유 종목이 없습니다"}
          </div>
        ) : (
          holdings.map((holding) => {
            const key = holdingKey(market, holding);
            const protection = activeProtectionByKey.get(key);
            const draft = drafts[key] || defaultDraft(holding, protection);
            const livePrice = livePrices[key]?.price;
            const displayPrice = livePrice || holding.current_price;
            const profitPositive = Number(holding.profit_rate) >= 0;
            const saving = savingSymbol === key;
            return (
              <article key={key} className="card p-5">
                <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                  <div>
                    <div className="flex flex-wrap items-center gap-3">
                      <h2 className="text-xl font-bold text-slate-900 dark:text-slate-100">{holding.stock_name}</h2>
                      <span className="px-2 py-1 rounded bg-slate-100 dark:bg-slate-800 text-xs font-mono">
                        {holding.stock_code}
                      </span>
                      {holding.exchange && (
                        <span className="px-2 py-1 rounded bg-slate-100 dark:bg-slate-800 text-xs">
                          {holding.exchange}
                        </span>
                      )}
                      {protection && (
                        <span className="px-2 py-1 rounded bg-primary/10 text-primary text-xs font-bold">
                          감시 중
                        </span>
                      )}
                    </div>
                    <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
                      <span>수량 <strong>{holding.quantity.toLocaleString()}주</strong></span>
                      <span>평균 <strong>{formatMoney(holding.avg_price, currency)}</strong></span>
                      <span>
                        현재{" "}
                        <strong>{formatMoney(displayPrice, currency)}</strong>
                        {livePrice && <em className="ml-1 not-italic text-primary">실시간</em>}
                      </span>
                      <span>평가 <strong>{formatMoney(holding.eval_amount, currency)}</strong></span>
                      <span>
                        손익{" "}
                        <strong className={profitPositive ? "text-green-600" : "text-red-600"}>
                          {holding.profit_rate?.toFixed?.(2) ?? holding.profit_rate}%
                        </strong>
                      </span>
                    </div>
                  </div>
                  <label className="flex items-center gap-2 text-sm font-medium">
                    <input
                      type="checkbox"
                      checked={draft.enabled}
                      onChange={(event) => updateDraft(key, { enabled: event.target.checked })}
                      className="w-4 h-4"
                    />
                    감시 사용
                  </label>
                </div>

                <div className="mt-5 grid grid-cols-1 lg:grid-cols-[120px_1fr_1fr_auto] gap-4 items-end">
                  <label className="block">
                    <span className="text-caption text-slate-500">감시 수량</span>
                    <input
                      value={draft.quantity}
                      onChange={(event) => updateDraft(key, { quantity: event.target.value })}
                      inputMode="numeric"
                      className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                    />
                  </label>

                  <div className="rounded-lg border border-slate-200 dark:border-slate-700 p-3">
                    <label className="flex items-center gap-2 text-sm font-medium mb-3">
                      <input
                        type="checkbox"
                        checked={draft.stopLossEnabled}
                        onChange={(event) => updateDraft(key, { stopLossEnabled: event.target.checked })}
                      />
                      손절/수익보존 매도
                    </label>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                      <label>
                        <span className="text-caption text-slate-500">도달가</span>
                        <input
                          value={draft.stopLossTriggerPrice}
                          onChange={(event) => updateDraft(key, { stopLossTriggerPrice: event.target.value })}
                          inputMode="decimal"
                          className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                        />
                      </label>
                      <label>
                        <span className="text-caption text-slate-500">주문</span>
                        <select
                          value={draft.stopLossOrderType}
                          onChange={(event) => updateDraft(key, { stopLossOrderType: event.target.value as ExitOrderType })}
                          className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                        >
                          <option value="market">시장가</option>
                          <option value="limit">지정가</option>
                        </select>
                      </label>
                      <label>
                        <span className="text-caption text-slate-500">지정가</span>
                        <input
                          value={draft.stopLossLimitPrice}
                          onChange={(event) => updateDraft(key, { stopLossLimitPrice: event.target.value })}
                          disabled={draft.stopLossOrderType === "market"}
                          inputMode="decimal"
                          className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 disabled:opacity-50"
                        />
                      </label>
                    </div>
                  </div>

                  <div className="rounded-lg border border-slate-200 dark:border-slate-700 p-3">
                    <label className="flex items-center gap-2 text-sm font-medium mb-3">
                      <input
                        type="checkbox"
                        checked={draft.takeProfitEnabled}
                        onChange={(event) => updateDraft(key, { takeProfitEnabled: event.target.checked })}
                      />
                      익절 매도
                    </label>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                      <label>
                        <span className="text-caption text-slate-500">도달가</span>
                        <input
                          value={draft.takeProfitTriggerPrice}
                          onChange={(event) => updateDraft(key, { takeProfitTriggerPrice: event.target.value })}
                          inputMode="decimal"
                          className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                        />
                      </label>
                      <label>
                        <span className="text-caption text-slate-500">주문</span>
                        <select
                          value={draft.takeProfitOrderType}
                          onChange={(event) => updateDraft(key, { takeProfitOrderType: event.target.value as ExitOrderType })}
                          className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                        >
                          <option value="limit">지정가</option>
                          <option value="market">시장가</option>
                        </select>
                      </label>
                      <label>
                        <span className="text-caption text-slate-500">지정가</span>
                        <input
                          value={draft.takeProfitLimitPrice}
                          onChange={(event) => updateDraft(key, { takeProfitLimitPrice: event.target.value })}
                          disabled={draft.takeProfitOrderType === "market"}
                          inputMode="decimal"
                          className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 disabled:opacity-50"
                        />
                      </label>
                    </div>
                  </div>

                  <button
                    onClick={() => saveHolding(holding)}
                    disabled={saving}
                    className="flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-primary text-white text-sm font-medium disabled:opacity-50 focus-ring"
                  >
                    <Save className="w-4 h-4" />
                    {saving ? "저장 중" : "저장"}
                  </button>
                </div>

                {protection && (
                  <div className="mt-4 text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
                    <span>익절 주문: {orderTypeLabel(protection.take_profit_order_type)}</span>
                    <span>손절 주문: {orderTypeLabel(protection.stop_loss_order_type)}</span>
                    <span>마지막 점검: {protection.last_checked_at || "-"}</span>
                    {protection.last_error && <span className="text-red-600">오류: {protection.last_error}</span>}
                  </div>
                )}
              </article>
            );
          })
        )}
      </section>
    </div>
  );
}
