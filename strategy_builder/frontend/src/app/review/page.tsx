"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, RefreshCw, Save, Settings2, ShieldCheck } from "lucide-react";
import { useAccount, useAuth } from "@/hooks";
import {
  checkProtectiveOrders,
  getProtectiveOrders,
  saveProtectiveOrder,
  saveProtectiveSettings,
  type ExitOrderType,
  type ProtectiveMonitorHealth,
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

const finiteNumber = (value: unknown, fallback = 0) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const orderTypeLabel = (value?: string) => (value === "market" ? "시장가" : "지정가");
const exitReasonLabel = (value?: string) => (value === "take_profit" ? "익절" : value === "stop_loss" ? "손절" : "매도");
const protectionStatusLabel = (value?: string) => {
  if (value === "active") return "감시 중";
  if (value === "disabled") return "감시 꺼짐";
  if (value === "exit_submitted") return "매도 제출";
  if (value === "submit_failed") return "제출 실패";
  if (value === "closed") return "종료";
  return value || "-";
};
const reservationStatusLabel = (value?: string) => {
  if (value === "waiting_retry") return "예약 재시도 대기";
  if (value === "submitted_unconfirmed") return "매도 제출, 체결 확인 중";
  if (value === "filled") return "체결 완료";
  if (value === "submitted") return "제출 완료";
  return value || "-";
};
const formatDateTime = (value?: string | null) => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
};

const holdingKey = (market: ReviewMarket, holding: Pick<Holding, "stock_code" | "exchange">) =>
  `${market}:${holding.stock_code}:${holding.exchange || ""}`;

const orderKey = (order: ProtectiveOrder) =>
  `${order.market || "domestic"}:${order.stock_code}:${order.exchange || ""}`;

const detachedProtectionStatuses = new Set(["active", "exit_submitted", "submit_failed"]);

function defaultDraft(holding: Holding, protection: ProtectiveOrder | undefined, market: ReviewMarket): ReviewDraft {
  const hasSavedProtection = Boolean(protection);
  const takeProfitTrigger = hasSavedProtection ? protection?.take_profit_trigger_price : null;
  const stopLossTrigger = hasSavedProtection ? protection?.stop_loss_price : null;
  const takeProfitOrderType = market === "us" ? "limit" : protection?.take_profit_order_type || "limit";
  const stopLossOrderType = market === "us" ? "limit" : protection?.stop_loss_order_type || "market";

  return {
    enabled: protection?.status === "active",
    quantity: String(protection?.quantity || holding.quantity || 1),
    takeProfitEnabled: hasSavedProtection ? protection?.take_profit_enabled !== false : false,
    takeProfitTriggerPrice: String(takeProfitTrigger || ""),
    takeProfitOrderType,
    takeProfitLimitPrice: String(protection?.take_profit_limit_price || takeProfitTrigger || ""),
    stopLossEnabled: hasSavedProtection ? protection?.stop_loss_enabled !== false : false,
    stopLossTriggerPrice: String(stopLossTrigger || ""),
    stopLossOrderType,
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
  const [stopLossOffset, setStopLossOffset] = useState("2");
  const [takeProfitOffset, setTakeProfitOffset] = useState("0.3");
  const [repriceInterval, setRepriceInterval] = useState("60");
  const [repriceStep, setRepriceStep] = useState("0.75");
  const [maxExitOffset, setMaxExitOffset] = useState("5");
  const [monitorHealth, setMonitorHealth] = useState<ProtectiveMonitorHealth | null>(null);
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

  const activeProtectionCount = useMemo(
    () => protectiveOrders.filter((order) => order.status === "active" && (order.market || "domestic") === market).length,
    [market, protectiveOrders]
  );

  const savedProtectionByKey = useMemo(() => {
    const map = new Map<string, ProtectiveOrder>();
    for (const order of protectiveOrders) {
      if ((order.status === "active" || order.status === "disabled") && (order.market || "domestic") === market) {
        map.set(orderKey(order), order);
      }
    }
    return map;
  }, [market, protectiveOrders]);

  const holdingKeys = useMemo(
    () => new Set(holdings.map((holding) => holdingKey(market, holding))),
    [holdings, market]
  );

  const detachedProtections = useMemo(
    () =>
      protectiveOrders.filter(
        (order) =>
          (order.market || "domestic") === market &&
          detachedProtectionStatuses.has(order.status) &&
          !holdingKeys.has(orderKey(order))
      ),
    [holdingKeys, market, protectiveOrders]
  );

  const positionSummary = useMemo(() => {
    const totals = holdings.reduce(
      (acc, holding) => {
        const quantity = finiteNumber(holding.quantity);
        const principal = finiteNumber(holding.avg_price) * quantity;
        const evaluation = finiteNumber(holding.eval_amount)
          || finiteNumber(holding.current_price) * quantity;
        return {
          principal: acc.principal + principal,
          evaluation: acc.evaluation + evaluation,
        };
      },
      { principal: 0, evaluation: 0 }
    );
    const profit = totals.evaluation - totals.principal;
    const profitRate = totals.principal > 0 ? (profit / totals.principal) * 100 : null;
    return { ...totals, profit, profitRate };
  }, [holdings]);

  const realtimeInterestKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const holding of holdings) {
      keys.add(holdingKey(market, holding));
    }
    for (const order of protectiveOrders) {
      if (order.status === "active" && (order.market || "domestic") === market) {
        keys.add(orderKey(order));
      }
    }
    return keys;
  }, [holdings, market, protectiveOrders]);

  const refresh = useCallback(async () => {
    resetThrottle();
    await fetchHoldings();
    await fetchBalance();
    const response = await getProtectiveOrders();
    setProtectiveOrders(response.orders || []);
    setMonitorHealth(response.health || null);
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
    if (response.settings) {
      const displayedStopLossOffset = market === "us"
        ? response.settings.us_stop_loss_limit_offset_pct ?? response.settings.domestic_stop_loss_limit_offset_pct
        : response.settings.domestic_stop_loss_limit_offset_pct ?? response.settings.us_stop_loss_limit_offset_pct;
      const displayedTakeProfitOffset = market === "us"
        ? response.settings.us_take_profit_limit_offset_pct ?? response.settings.domestic_take_profit_limit_offset_pct
        : response.settings.domestic_take_profit_limit_offset_pct ?? response.settings.us_take_profit_limit_offset_pct;
      const displayedRepriceStep = market === "us"
        ? response.settings.us_exit_reprice_step_pct ?? response.settings.domestic_exit_reprice_step_pct
        : response.settings.domestic_exit_reprice_step_pct ?? response.settings.us_exit_reprice_step_pct;
      const displayedMaxExitOffset = market === "us"
        ? response.settings.us_exit_max_offset_pct ?? response.settings.domestic_exit_max_offset_pct
        : response.settings.domestic_exit_max_offset_pct ?? response.settings.us_exit_max_offset_pct;

      if (response.settings.monitor_interval_seconds) {
        setMonitorInterval(String(response.settings.monitor_interval_seconds));
      }
      if (displayedStopLossOffset !== undefined) {
        setStopLossOffset(String(displayedStopLossOffset));
      }
      if (displayedTakeProfitOffset !== undefined) {
        setTakeProfitOffset(String(displayedTakeProfitOffset));
      }
      if (response.settings.exit_reprice_interval_seconds) {
        setRepriceInterval(String(response.settings.exit_reprice_interval_seconds));
      }
      if (displayedRepriceStep !== undefined) {
        setRepriceStep(String(displayedRepriceStep));
      }
      if (displayedMaxExitOffset !== undefined) {
        setMaxExitOffset(String(displayedMaxExitOffset));
      }
    }
  }, [fetchBalance, fetchHoldings, market, resetThrottle]);

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
        const savedProtection = savedProtectionByKey.get(key);
        if (savedProtection) {
          next[key] = defaultDraft(holding, savedProtection, market);
        } else if (!next[key]) {
          next[key] = defaultDraft(holding, undefined, market);
        }
      }
      return next;
    });
  }, [holdings, market, savedProtectionByKey]);

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
    let intentionallyClosed = false;

    const clearRealtimeErrorMessage = () => {
      setMessage((current) =>
        current === "실시간 시세 연결 실패" || current === "실시간 시세 연결 오류"
          ? ""
          : current
      );
    };

    setPriceStreamState("connecting");
    websocket.onopen = () => {
      if (intentionallyClosed) return;
      setPriceStreamState("connected");
      clearRealtimeErrorMessage();
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
          if (payload.realtime?.connected) {
            clearRealtimeErrorMessage();
          }
        }
        if (payload.type === "error") {
          setMessage(payload.message || "실시간 시세 연결 오류");
        }
      } catch {
        setMessage("실시간 시세 메시지를 처리하지 못했습니다.");
      }
    };
    websocket.onclose = () => {
      if (!intentionallyClosed) {
        setPriceStreamState("closed");
      }
    };
    websocket.onerror = () => {
      if (!intentionallyClosed) {
        setPriceStreamState("closed");
        setMessage("실시간 시세 연결 실패");
      }
    };

    return () => {
      intentionallyClosed = true;
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
    const parsedStopLossOffset = parseNumber(stopLossOffset);
    const parsedTakeProfitOffset = parseNumber(takeProfitOffset);
    const parsedRepriceInterval = parseNumber(repriceInterval);
    const parsedRepriceStep = parseNumber(repriceStep);
    const parsedMaxExitOffset = parseNumber(maxExitOffset);
    if (!interval || interval < 5 || interval > 300) {
      setMessage("감시 주기는 5초에서 300초 사이로 설정하세요.");
      return;
    }
    if (
      parsedStopLossOffset === null
      || parsedStopLossOffset < 0
      || parsedStopLossOffset > 10
      || parsedTakeProfitOffset === null
      || parsedTakeProfitOffset < 0
      || parsedTakeProfitOffset > 10
      || parsedRepriceStep === null
      || parsedRepriceStep < 0
      || parsedRepriceStep > 10
      || parsedMaxExitOffset === null
      || parsedMaxExitOffset < Math.max(parsedStopLossOffset, parsedTakeProfitOffset)
      || parsedMaxExitOffset > 10
    ) {
      setMessage("보호매도 지정가 하향 폭은 0%에서 10% 사이이며, 최대 하향 폭은 기본 하향 폭보다 커야 합니다.");
      return;
    }
    if (!parsedRepriceInterval || parsedRepriceInterval < 5 || parsedRepriceInterval > 300) {
      setMessage("재가격 주기는 5초에서 300초 사이로 설정하세요.");
      return;
    }

    setSavingSettings(true);
    setMessage("");
    try {
      const response = await saveProtectiveSettings({
        monitor_interval_seconds: Math.round(interval),
        exit_reprice_interval_seconds: Math.round(parsedRepriceInterval),
        ...(market === "us"
          ? {
              us_stop_loss_limit_offset_pct: parsedStopLossOffset,
              us_take_profit_limit_offset_pct: parsedTakeProfitOffset,
              us_exit_reprice_step_pct: parsedRepriceStep,
              us_exit_max_offset_pct: parsedMaxExitOffset,
            }
          : {
              domestic_stop_loss_limit_offset_pct: parsedStopLossOffset,
              domestic_take_profit_limit_offset_pct: parsedTakeProfitOffset,
              domestic_exit_reprice_step_pct: parsedRepriceStep,
              domestic_exit_max_offset_pct: parsedMaxExitOffset,
            }),
      });
      setMonitorInterval(String(response.settings.monitor_interval_seconds));
      setStopLossOffset(String(
        (market === "us"
          ? response.settings.us_stop_loss_limit_offset_pct ?? response.settings.domestic_stop_loss_limit_offset_pct
          : response.settings.domestic_stop_loss_limit_offset_pct ?? response.settings.us_stop_loss_limit_offset_pct)
        ?? parsedStopLossOffset
      ));
      setTakeProfitOffset(String(
        (market === "us"
          ? response.settings.us_take_profit_limit_offset_pct ?? response.settings.domestic_take_profit_limit_offset_pct
          : response.settings.domestic_take_profit_limit_offset_pct ?? response.settings.us_take_profit_limit_offset_pct)
        ?? parsedTakeProfitOffset
      ));
      setRepriceInterval(String(response.settings.exit_reprice_interval_seconds ?? parsedRepriceInterval));
      setRepriceStep(String(
        (market === "us"
          ? response.settings.us_exit_reprice_step_pct ?? response.settings.domestic_exit_reprice_step_pct
          : response.settings.domestic_exit_reprice_step_pct ?? response.settings.us_exit_reprice_step_pct)
        ?? parsedRepriceStep
      ));
      setMaxExitOffset(String(
        (market === "us"
          ? response.settings.us_exit_max_offset_pct ?? response.settings.domestic_exit_max_offset_pct
          : response.settings.domestic_exit_max_offset_pct ?? response.settings.us_exit_max_offset_pct)
        ?? parsedMaxExitOffset
      ));
      setMessage("보호주문 실행 설정을 저장했습니다.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "감시 주기 저장 실패");
    } finally {
      setSavingSettings(false);
    }
  };

  const saveHolding = async (holding: Holding) => {
    const key = holdingKey(market, holding);
    const protection = savedProtectionByKey.get(key);
    const draft = drafts[key] || defaultDraft(holding, protection, market);
    const takeProfitOrderType = market === "us" ? "limit" : draft.takeProfitOrderType;
    const stopLossOrderType = market === "us" ? "limit" : draft.stopLossOrderType;
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
      const response = await saveProtectiveOrder({
        stock_code: holding.stock_code,
        stock_name: holding.stock_name,
        quantity,
        entry_price: Number(holding.avg_price || holding.current_price),
        enabled: draft.enabled,
        take_profit_enabled: draft.takeProfitEnabled,
        take_profit_trigger_price: takeProfitTrigger,
        take_profit_order_type: takeProfitOrderType,
        take_profit_limit_price: takeProfitOrderType === "limit" ? takeProfitLimit : null,
        stop_loss_enabled: draft.stopLossEnabled,
        stop_loss_trigger_price: stopLossTrigger,
        stop_loss_order_type: stopLossOrderType,
        stop_loss_limit_price: stopLossOrderType === "limit" ? stopLossLimit : null,
        market,
        exchange: holding.exchange || null,
        currency: market === "us" ? "USD" : "KRW",
      });
      setDrafts((current) => ({
        ...current,
        [key]: defaultDraft(holding, response.order, market),
      }));
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
  const usTotalEval = Number(balance?.total_eval || 0);
  const usOrderableAmount = Number(balance?.orderable_amount ?? balance?.available_amount ?? 0);
  const displayTotalEval = market === "us" && usTotalEval <= 0 && usOrderableAmount > 0
    ? usOrderableAmount
    : balance?.total_eval;
  const totalEvalLabel = market === "us" && usTotalEval <= 0 && usOrderableAmount > 0
    ? "미국 주문가능금액"
    : market === "us"
      ? "미국 평가금액"
      : "총 평가금액";
  const secondaryBalanceLabel = market === "us" ? "주문가능금액" : "예수금";
  const secondaryBalanceAmount = market === "us" ? usOrderableAmount : balance?.deposit;
  const realtimeError = realtimeInterestKeys.size > 0 ? realtimeStatus?.last_error : null;
  const streamLabel =
    priceStreamState === "connected"
      ? "실시간 연결"
      : priceStreamState === "connecting"
        ? "실시간 연결 중"
        : realtimeError
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
          <label className="block">
            <span className="text-caption text-slate-500">손절 지정가 하향</span>
            <div className="mt-1 flex items-center gap-2">
              <input
                value={stopLossOffset}
                onChange={(event) => setStopLossOffset(event.target.value)}
                inputMode="decimal"
                className="w-24 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
              />
              <span className="text-sm text-slate-500">%</span>
            </div>
          </label>
          <label className="block">
            <span className="text-caption text-slate-500">익절 지정가 하향</span>
            <div className="mt-1 flex items-center gap-2">
              <input
                value={takeProfitOffset}
                onChange={(event) => setTakeProfitOffset(event.target.value)}
                inputMode="decimal"
                className="w-24 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
              />
              <span className="text-sm text-slate-500">%</span>
            </div>
          </label>
          <label className="block">
            <span className="text-caption text-slate-500">미체결 재가격</span>
            <div className="mt-1 flex items-center gap-2">
              <input
                value={repriceInterval}
                onChange={(event) => setRepriceInterval(event.target.value)}
                inputMode="numeric"
                className="w-24 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
              />
              <span className="text-sm text-slate-500">초</span>
            </div>
          </label>
          <label className="block">
            <span className="text-caption text-slate-500">재시도 추가 하향</span>
            <div className="mt-1 flex items-center gap-2">
              <input
                value={repriceStep}
                onChange={(event) => setRepriceStep(event.target.value)}
                inputMode="decimal"
                className="w-24 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
              />
              <span className="text-sm text-slate-500">%</span>
            </div>
          </label>
          <label className="block">
            <span className="text-caption text-slate-500">최대 하향 폭</span>
            <div className="mt-1 flex items-center gap-2">
              <input
                value={maxExitOffset}
                onChange={(event) => setMaxExitOffset(event.target.value)}
                inputMode="decimal"
                className="w-24 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
              />
              <span className="text-sm text-slate-500">%</span>
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

      {monitorHealth && (monitorHealth.status !== "healthy" || monitorHealth.stale) && (
        <div className="mb-6 flex items-start gap-3 border border-red-200 bg-red-50 p-4 text-red-900 dark:border-red-900 dark:bg-red-950/30 dark:text-red-100" role="alert">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
          <div>
            <strong className="block text-sm">보호주문 감시 상태를 확인하세요</strong>
            <p className="mt-1 text-sm">
              상태 {monitorHealth.status || "-"} · 호출 제한 {monitorHealth.rate_limited_order_count || 0}건 ·
              장기 미체결 {monitorHealth.overdue_exit_count || 0}건 · 마지막 완료 {formatDateTime(monitorHealth.last_cycle_completed_at)}
            </p>
          </div>
        </div>
      )}

      <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-4 mb-6">
        <div className="card p-4">
          <span className="text-caption text-slate-500">보유 종목</span>
          <strong className="block text-2xl mt-1">{holdings.length}개</strong>
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">감시 중</span>
          <strong className="block text-2xl mt-1">{activeProtectionCount}개</strong>
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">{totalEvalLabel}</span>
          <strong className="block text-2xl mt-1">{formatMoney(displayTotalEval, currency)}</strong>
          <span className="block mt-1 text-xs text-slate-500">
            {secondaryBalanceLabel} {formatMoney(secondaryBalanceAmount, currency)}
          </span>
          {market === "us" && usTotalEval <= 0 && usOrderableAmount > 0 && (
            <span className="block mt-1 text-xs text-slate-500">평가금액(API) {formatMoney(balance?.total_eval, "USD")}</span>
          )}
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">투입 대비 평가</span>
          <strong className={`block text-2xl mt-1 ${positionSummary.profit >= 0 ? "text-green-600" : "text-red-600"}`}>
            {formatMoney(positionSummary.profit, currency)}
          </strong>
          <span className="block mt-1 text-xs text-slate-500">
            {positionSummary.profitRate === null ? "수익률 -" : `수익률 ${positionSummary.profitRate >= 0 ? "+" : ""}${positionSummary.profitRate.toFixed(2)}%`}
          </span>
          <span className="block mt-1 text-xs text-slate-500">
            원금 {formatMoney(positionSummary.principal, currency)}
          </span>
        </div>
        <div className="card p-4">
          <span className="text-caption text-slate-500">{streamLabel}</span>
          <strong className="block text-2xl mt-1">{realtimeInterestKeys.size}개</strong>
          {realtimeError && (
            <span className="block mt-1 text-xs text-red-600">{realtimeError}</span>
          )}
        </div>
      </section>

      <section className="space-y-4">
        {holdings.length === 0 && detachedProtections.length === 0 ? (
          <div className="card p-12 text-center text-slate-500">
            {market === "us" ? "미국 보유 종목이 없습니다" : "한국 보유 종목이 없습니다"}
          </div>
        ) : (
          <>
          {holdings.map((holding) => {
            const key = holdingKey(market, holding);
            const savedProtection = savedProtectionByKey.get(key);
            const activeProtection = activeProtectionByKey.get(key);
            const draft = drafts[key] || defaultDraft(holding, savedProtection, market);
            const appReservation = activeProtection?.app_exit_reservation;
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
                      {activeProtection && (
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
                        {market === "us" ? (
                          <div className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-700 dark:text-slate-200">
                            지정가
                          </div>
                        ) : (
                          <select
                            value={draft.stopLossOrderType}
                            onChange={(event) => updateDraft(key, { stopLossOrderType: event.target.value as ExitOrderType })}
                            className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                          >
                            <option value="market">시장가</option>
                            <option value="limit">지정가</option>
                          </select>
                        )}
                      </label>
                      <label>
                        <span className="text-caption text-slate-500">지정가</span>
                        <input
                          value={draft.stopLossLimitPrice}
                          onChange={(event) => updateDraft(key, { stopLossLimitPrice: event.target.value })}
                          disabled={market !== "us" && draft.stopLossOrderType === "market"}
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
                        {market === "us" ? (
                          <div className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-700 dark:text-slate-200">
                            지정가
                          </div>
                        ) : (
                          <select
                            value={draft.takeProfitOrderType}
                            onChange={(event) => updateDraft(key, { takeProfitOrderType: event.target.value as ExitOrderType })}
                            className="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900"
                          >
                            <option value="limit">지정가</option>
                            <option value="market">시장가</option>
                          </select>
                        )}
                      </label>
                      <label>
                        <span className="text-caption text-slate-500">지정가</span>
                        <input
                          value={draft.takeProfitLimitPrice}
                          onChange={(event) => updateDraft(key, { takeProfitLimitPrice: event.target.value })}
                          disabled={market !== "us" && draft.takeProfitOrderType === "market"}
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

                {activeProtection && (
                  <div className="mt-4 text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
                    <span>익절 주문: {orderTypeLabel(activeProtection.take_profit_order_type)}</span>
                    <span>손절 주문: {orderTypeLabel(activeProtection.stop_loss_order_type)}</span>
                    <span>마지막 점검: {activeProtection.last_checked_at || "-"}</span>
                    {appReservation?.status === "waiting_retry" && (
                      <span className="text-amber-600">
                        로컬 예약매도 대기: {exitReasonLabel(appReservation.exit_reason)}{" "}
                        {orderTypeLabel(appReservation.order_type)}
                        {appReservation.limit_price ? ` ${formatMoney(appReservation.limit_price, currency)}` : ""} 재시도
                      </span>
                    )}
                    {appReservation?.last_error && (
                      <span className="text-slate-500">최근 KIS 응답: {appReservation.last_error}</span>
                    )}
                    {activeProtection.last_error && !appReservation && <span className="text-red-600">오류: {activeProtection.last_error}</span>}
                  </div>
                )}
              </article>
            );
          })}

          {detachedProtections.length > 0 && (
            <div className="space-y-3">
              <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">보유 없는 보호주문</h2>
                  <p className="text-sm text-slate-500 dark:text-slate-400">
                    보유종목 조회에는 없지만 보호주문 상태 파일에 남아 있는 감시 항목입니다.
                  </p>
                </div>
                <span className="text-sm font-medium text-amber-700 dark:text-amber-300">
                  {detachedProtections.length}개 확인 필요
                </span>
              </div>

              {detachedProtections.map((order, index) => {
                const appReservation = order.app_exit_reservation;
                const reservationStatus = appReservation?.status || order.app_exit_reservation_status;
                const nextRetryAt = appReservation?.next_retry_at || order.next_retry_at;
                const lastErrorCode = appReservation?.last_error_code || order.last_error_code;
                const unsupportedPaths = appReservation?.unsupported_paths || order.unsupported_paths || [];
                const lastError = appReservation?.last_error || order.last_error;
                const key = order.id || `${orderKey(order)}:${index}`;

                return (
                  <article
                    key={key}
                    className="card p-5 border-amber-200 dark:border-amber-800 bg-amber-50/50 dark:bg-amber-900/10"
                  >
                    <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                      <div>
                        <div className="flex flex-wrap items-center gap-3">
                          <h3 className="text-xl font-bold text-slate-900 dark:text-slate-100">{order.stock_name}</h3>
                          <span className="px-2 py-1 rounded bg-white dark:bg-slate-800 text-xs font-mono">
                            {order.stock_code}
                          </span>
                          {order.exchange && (
                            <span className="px-2 py-1 rounded bg-white dark:bg-slate-800 text-xs">
                              {order.exchange}
                            </span>
                          )}
                          <span className="px-2 py-1 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 text-xs font-bold">
                            보유 없음
                          </span>
                          <span className="px-2 py-1 rounded bg-primary/10 text-primary text-xs font-bold">
                            {protectionStatusLabel(order.status)}
                          </span>
                        </div>
                        <div className="mt-3 grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3 text-sm">
                          <span>감시 수량 <strong>{Number(order.quantity || 0).toLocaleString()}주</strong></span>
                          <span>진입 <strong>{formatMoney(order.entry_price, currency)}</strong></span>
                          <span>익절 <strong>{formatMoney(order.take_profit_trigger_price, currency)}</strong></span>
                          <span>손절 <strong>{formatMoney(order.stop_loss_price, currency)}</strong></span>
                          <span>마지막 점검 <strong>{formatDateTime(order.last_checked_at)}</strong></span>
                          <span>예약 상태 <strong>{reservationStatusLabel(reservationStatus)}</strong></span>
                        </div>
                      </div>
                    </div>

                    <div className="mt-4 text-xs text-slate-600 dark:text-slate-300 flex flex-wrap gap-x-4 gap-y-1">
                      <span>익절 주문: {orderTypeLabel(order.take_profit_order_type)}</span>
                      <span>손절 주문: {orderTypeLabel(order.stop_loss_order_type)}</span>
                      {nextRetryAt && <span className="text-amber-700 dark:text-amber-300">다음 재시도: {formatDateTime(nextRetryAt)}</span>}
                      {lastErrorCode && <span>오류코드: {lastErrorCode}</span>}
                      {unsupportedPaths.length > 0 && <span>미지원 경로: {unsupportedPaths.join(", ")}</span>}
                    </div>
                    {lastError && (
                      <p className="mt-2 text-xs text-slate-600 dark:text-slate-300 break-words">
                        최근 KIS 응답: {lastError}
                      </p>
                    )}
                  </article>
                );
              })}
            </div>
          )}
          </>
        )}
      </section>
    </div>
  );
}
