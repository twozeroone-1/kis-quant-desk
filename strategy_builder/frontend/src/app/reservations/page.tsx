"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { CalendarClock, Loader2, RefreshCw, Send, Trash2 } from "lucide-react";
import {
  cancelReservationOrder,
  getReservationOrders,
  submitReservationOrder,
  type ReservationAction,
  type ReservationMarket,
  type ReservationOrderItem,
  type ReservationOrderType,
} from "@/lib/api";
import { useAuth } from "@/hooks";

type ReservationExchange = "NASD" | "NYSE" | "AMEX";
type Notice = { type: "success" | "error" | "info"; message: string } | null;

const ORDER_NO_KEYS = [
  "reservation_order_no",
  "RSVN_ORD_SEQ",
  "rsvn_ord_seq",
  "OVRS_RSVN_ODNO",
  "ovrs_rsvn_odno",
  "ODNO",
  "odno",
];
const ORDER_DATE_KEYS = [
  "reservation_order_date",
  "RSVN_ORD_ORD_DT",
  "rsvn_ord_ord_dt",
  "RSVN_ORD_RCIT_DT",
  "rsvn_ord_rcit_dt",
  "ORD_DT",
  "ord_dt",
];
const ORDER_ORG_KEYS = ["reservation_order_org_no", "RSVN_ORD_ORGNO", "rsvn_ord_orgno"];
const STOCK_KEYS = ["stock_code", "PDNO", "pdno", "OVRS_PDNO", "ovrs_pdno"];
const NAME_KEYS = ["stock_name", "PRDT_NAME", "prdt_name", "OVRS_ITEM_NAME", "ovrs_item_name"];
const ACTION_KEYS = ["SLL_BUY_DVSN_CD", "sll_buy_dvsn_cd", "SLL_BUY_DVSN_NAME", "sll_buy_dvsn_name"];
const QTY_KEYS = ["quantity", "ORD_QTY", "ord_qty", "FT_ORD_QTY", "ft_ord_qty", "RSVN_ORD_QTY", "rsvn_ord_qty"];
const PRICE_KEYS = ["price", "ORD_UNPR", "ord_unpr", "FT_ORD_UNPR3", "ft_ord_unpr3", "RSVN_ORD_UNPR", "rsvn_ord_unpr"];
const STATUS_KEYS = [
  "status",
  "RSVN_ORD_PRCS_STAT_NAME",
  "rsvn_ord_prcs_stat_name",
  "ORD_STAT_NAME",
  "ord_stat_name",
  "PRCS_STAT_NAME",
  "prcs_stat_name",
  "CNCL_YN",
  "cncl_yn",
];

function today(offsetDays = 0): string {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function field(row: ReservationOrderItem, keys: string[]): string {
  for (const key of keys) {
    const value = row[key];
    if (value === null || value === undefined || typeof value === "object") continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function formatDate(value: string): string {
  const compact = value.replace(/\D/g, "");
  if (compact.length !== 8) return value;
  return `${compact.slice(0, 4)}-${compact.slice(4, 6)}-${compact.slice(6, 8)}`;
}

function formatNumber(value: string): string {
  const numeric = Number(value.replace(/,/g, ""));
  return Number.isFinite(numeric) && value !== "" ? numeric.toLocaleString() : value;
}

function extractError(error: unknown): string {
  if (!(error instanceof Error)) return "처리 중 오류가 발생했습니다";
  try {
    const parsed = JSON.parse(error.message) as { detail?: string };
    return parsed.detail || error.message;
  } catch {
    return error.message;
  }
}

function orderTypeOptions(market: ReservationMarket, action: ReservationAction): ReservationOrderType[] {
  if (market === "domestic") return ["limit", "market", "preopen"];
  return action === "SELL" ? ["limit", "moo"] : ["limit"];
}

export default function ReservationsPage() {
  const { status: authStatus } = useAuth();
  const [market, setMarket] = useState<ReservationMarket>("domestic");
  const [action, setAction] = useState<ReservationAction>("BUY");
  const [orderType, setOrderType] = useState<ReservationOrderType>("limit");
  const [exchange, setExchange] = useState<ReservationExchange>("NASD");
  const [stockCode, setStockCode] = useState("");
  const [stockName, setStockName] = useState("");
  const [quantity, setQuantity] = useState(1);
  const [price, setPrice] = useState(0);
  const [endDate, setEndDate] = useState("");
  const [confirmProd, setConfirmProd] = useState(false);
  const [startDate, setStartDate] = useState(today(-30));
  const [listEndDate, setListEndDate] = useState(today());
  const [orders, setOrders] = useState<ReservationOrderItem[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancellingNo, setCancellingNo] = useState<string | null>(null);

  const isProd = authStatus.mode === "prod";
  const modeLabel = authStatus.mode_display || (isProd ? "실전투자" : "모의투자");
  const availableOrderTypes = useMemo(() => orderTypeOptions(market, action), [market, action]);
  const needsPrice = orderType === "limit";

  useEffect(() => {
    if (!availableOrderTypes.includes(orderType)) {
      setOrderType("limit");
    }
  }, [availableOrderTypes, orderType]);

  const refresh = useCallback(async () => {
    if (!authStatus.authenticated) return;
    setLoading(true);
    try {
      const response = await getReservationOrders({
        market,
        start_date: startDate,
        end_date: listEndDate,
        exchange,
        include_cancelled: true,
      });
      if (response.status === "success") {
        setOrders(response.orders || []);
        setNotice(null);
      } else {
        setOrders([]);
        setNotice({ type: "error", message: response.message || "예약주문 조회 실패" });
      }
    } catch (error) {
      setOrders([]);
      setNotice({ type: "error", message: extractError(error) });
    } finally {
      setLoading(false);
    }
  }, [authStatus.authenticated, exchange, listEndDate, market, startDate]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!authStatus.authenticated) {
      setNotice({ type: "error", message: "인증이 필요합니다" });
      return;
    }
    if (isProd && !confirmProd) {
      setNotice({ type: "error", message: "실전 예약주문 확인이 필요합니다" });
      return;
    }

    setSubmitting(true);
    setNotice(null);
    try {
      const response = await submitReservationOrder({
        market,
        stock_code: stockCode.trim(),
        stock_name: stockName.trim(),
        action,
        quantity,
        price: needsPrice ? price : 0,
        order_type: orderType,
        exchange: market === "us" ? exchange : undefined,
        end_date: market === "domestic" && endDate ? endDate : null,
        confirm_prod: isProd ? confirmProd : false,
      });
      setNotice({
        type: response.status === "success" ? "success" : "error",
        message: response.message || (response.status === "success" ? "예약주문 접수 완료" : "예약주문 접수 실패"),
      });
      if (response.status === "success") {
        setConfirmProd(false);
        await refresh();
      }
    } catch (error) {
      setNotice({ type: "error", message: extractError(error) });
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async (row: ReservationOrderItem) => {
    const reservationOrderNo = field(row, ORDER_NO_KEYS);
    const reservationOrderDate = field(row, ORDER_DATE_KEYS);
    const reservationOrderOrgNo = field(row, ORDER_ORG_KEYS);
    if (!reservationOrderNo || !reservationOrderDate) {
      setNotice({ type: "error", message: "예약주문번호 또는 주문일자를 확인할 수 없습니다" });
      return;
    }
    if (market === "domestic" && !reservationOrderOrgNo) {
      setNotice({ type: "error", message: "국내 예약주문조직번호를 확인할 수 없습니다" });
      return;
    }
    const confirmed = window.confirm(
      isProd ? "실전 예약주문을 취소합니다." : "예약주문을 취소합니다."
    );
    if (!confirmed) return;

    setCancellingNo(reservationOrderNo);
    setNotice(null);
    try {
      const response = await cancelReservationOrder({
        market,
        reservation_order_no: reservationOrderNo,
        reservation_order_date: reservationOrderDate,
        reservation_order_org_no: reservationOrderOrgNo,
        confirm_prod: isProd,
      });
      setNotice({
        type: response.status === "success" ? "success" : "error",
        message: response.message || (response.status === "success" ? "예약주문 취소 완료" : "예약주문 취소 실패"),
      });
      if (response.status === "success") {
        await refresh();
      }
    } catch (error) {
      setNotice({ type: "error", message: extractError(error) });
    } finally {
      setCancellingNo(null);
    }
  };

  return (
    <div className="max-w-7xl mx-auto px-4 py-6">
      <div className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="text-display text-slate-900 dark:text-slate-100 flex items-center gap-3">
            <CalendarClock className="w-7 h-7 text-primary" aria-hidden="true" />
            예약주문
          </h1>
          <div className="mt-2 ml-10 flex items-center gap-2">
            <span className={`rounded px-2 py-1 text-xs font-bold ${
              isProd
                ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300"
                : "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300"
            }`}>
              {modeLabel}
            </span>
          </div>
        </div>

        <button
          type="button"
          onClick={() => void refresh()}
          disabled={!authStatus.authenticated || loading}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 dark:border-slate-700 px-4 py-2 text-sm font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          새로고침
        </button>
      </div>

      {!authStatus.authenticated && (
        <div className="card mb-6 border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-900/20" role="alert">
          <p className="text-body text-yellow-800 dark:text-yellow-200">
            인증이 필요합니다. 우측 상단 설정에서 인증해주세요.
          </p>
        </div>
      )}

      {notice && (
        <div className={`mb-6 rounded-lg border px-4 py-3 text-sm ${
          notice.type === "success"
            ? "border-green-200 bg-green-50 text-green-700 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300"
            : notice.type === "error"
              ? "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
              : "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300"
        }`} role="status">
          {notice.message}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[420px_1fr]">
        <form onSubmit={handleSubmit} className="card p-6 space-y-5">
          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700 dark:text-slate-200">시장</label>
            <div className="grid grid-cols-2 gap-1 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-950 p-1">
              {(["domestic", "us"] as ReservationMarket[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setMarket(item)}
                  className={`rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                    market === item
                      ? "bg-primary text-white"
                      : "text-slate-600 hover:bg-white dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {item === "domestic" ? "한국" : "미국"}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <label className="space-y-2">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">종목코드</span>
              <input
                value={stockCode}
                onChange={(event) => setStockCode(event.target.value)}
                className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
                placeholder={market === "us" ? "NVDA" : "005930"}
                required
              />
            </label>
            <label className="space-y-2">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">종목명</span>
              <input
                value={stockName}
                onChange={(event) => setStockName(event.target.value)}
                className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
              />
            </label>
          </div>

          {market === "us" && (
            <label className="space-y-2 block">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">거래소</span>
              <select
                value={exchange}
                onChange={(event) => setExchange(event.target.value as ReservationExchange)}
                className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
              >
                <option value="NASD">NASDAQ</option>
                <option value="NYSE">NYSE</option>
                <option value="AMEX">AMEX</option>
              </select>
            </label>
          )}

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700 dark:text-slate-200">구분</label>
            <div className="grid grid-cols-2 gap-1 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-950 p-1">
              {(["BUY", "SELL"] as ReservationAction[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setAction(item)}
                  className={`rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                    action === item
                      ? item === "BUY"
                        ? "bg-red-500 text-white"
                        : "bg-blue-500 text-white"
                      : "text-slate-600 hover:bg-white dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {item === "BUY" ? "매수" : "매도"}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700 dark:text-slate-200">주문방식</label>
            <div className="grid grid-cols-3 gap-1 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-950 p-1">
              {availableOrderTypes.map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setOrderType(item)}
                  className={`rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                    orderType === item
                      ? "bg-primary text-white"
                      : "text-slate-600 hover:bg-white dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {item === "limit" ? "지정가" : item === "market" ? "시장가" : item === "preopen" ? "장전" : "MOO"}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <label className="space-y-2">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">수량</span>
              <input
                type="number"
                min={1}
                value={quantity}
                onChange={(event) => setQuantity(Number(event.target.value))}
                className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
                required
              />
            </label>
            <label className="space-y-2">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">가격</span>
              <input
                type="number"
                min={0}
                step={market === "us" ? "0.01" : "1"}
                value={price}
                onChange={(event) => setPrice(Number(event.target.value))}
                disabled={!needsPrice}
                className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm disabled:bg-slate-100 disabled:text-slate-400 dark:disabled:bg-slate-800"
                required={needsPrice}
              />
            </label>
          </div>

          {market === "domestic" && (
            <label className="space-y-2 block">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">종료일</span>
              <input
                type="date"
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
                className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
              />
            </label>
          )}

          {isProd && (
            <label className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
              <input
                type="checkbox"
                checked={confirmProd}
                onChange={(event) => setConfirmProd(event.target.checked)}
                className="h-4 w-4"
              />
              실전 예약주문 확인
            </label>
          )}

          <button
            type="submit"
            disabled={!authStatus.authenticated || submitting}
            className="btn-primary inline-flex w-full items-center justify-center gap-2 disabled:opacity-50"
          >
            {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            접수
          </button>
        </form>

        <section className="card p-0 overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-slate-200 dark:border-slate-800 p-4 md:flex-row md:items-end md:justify-between">
            <div>
              <h2 className="text-heading text-slate-900 dark:text-slate-100">예약주문 목록</h2>
              <p className="text-caption text-slate-500 dark:text-slate-400">{orders.length}건</p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <input
                type="date"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
                className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
              />
              <input
                type="date"
                value={listEndDate}
                onChange={(event) => setListEndDate(event.target.value)}
                className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm"
              />
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full min-w-[780px] text-left text-sm">
              <thead className="bg-slate-50 text-xs font-semibold uppercase text-slate-500 dark:bg-slate-950 dark:text-slate-400">
                <tr>
                  <th className="px-4 py-3">접수일</th>
                  <th className="px-4 py-3">예약번호</th>
                  <th className="px-4 py-3">종목</th>
                  <th className="px-4 py-3">구분</th>
                  <th className="px-4 py-3 text-right">수량</th>
                  <th className="px-4 py-3 text-right">가격</th>
                  <th className="px-4 py-3">상태</th>
                  <th className="px-4 py-3 text-right">관리</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                {loading ? (
                  <tr>
                    <td colSpan={8} className="px-4 py-10 text-center text-slate-500">
                      <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" />
                      조회 중
                    </td>
                  </tr>
                ) : orders.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="px-4 py-10 text-center text-slate-500">
                      예약주문 없음
                    </td>
                  </tr>
                ) : (
                  orders.map((row, index) => {
                    const orderNo = field(row, ORDER_NO_KEYS);
                    const orderDate = field(row, ORDER_DATE_KEYS);
                    const stock = field(row, STOCK_KEYS);
                    const name = field(row, NAME_KEYS);
                    const side = field(row, ACTION_KEYS);
                    const qty = field(row, QTY_KEYS);
                    const orderPrice = field(row, PRICE_KEYS);
                    const status = field(row, STATUS_KEYS);
                    const orgNo = field(row, ORDER_ORG_KEYS);
                    const cancelDisabled = !orderNo || !orderDate || (market === "domestic" && !orgNo);

                    return (
                      <tr key={`${orderNo || "row"}-${index}`} className="bg-white dark:bg-slate-900">
                        <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{formatDate(orderDate)}</td>
                        <td className="px-4 py-3 font-mono text-xs text-slate-700 dark:text-slate-200">{orderNo || "-"}</td>
                        <td className="px-4 py-3">
                          <div className="font-medium text-slate-900 dark:text-slate-100">{name || stock || "-"}</div>
                          {name && stock && <div className="text-xs text-slate-500">{stock}</div>}
                        </td>
                        <td className="px-4 py-3 text-slate-700 dark:text-slate-200">{side || "-"}</td>
                        <td className="px-4 py-3 text-right text-slate-700 dark:text-slate-200">{formatNumber(qty)}</td>
                        <td className="px-4 py-3 text-right text-slate-700 dark:text-slate-200">{formatNumber(orderPrice)}</td>
                        <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{status || "-"}</td>
                        <td className="px-4 py-3 text-right">
                          <button
                            type="button"
                            onClick={() => void handleCancel(row)}
                            disabled={cancelDisabled || cancellingNo === orderNo}
                            className="inline-flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 p-2 text-slate-600 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:text-slate-300 dark:hover:bg-red-950/40 dark:hover:text-red-300"
                            aria-label="예약주문 취소"
                          >
                            {cancellingNo === orderNo ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                          </button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
