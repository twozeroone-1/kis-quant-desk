/**
 * Orders API
 * 
 * Account Information and Pending Orders API:
 * - GET /account: 통합 계좌 정보 (예수금 + 보유종목)
 * - GET /pending: 미체결 주문 목록
 * - POST /cancel: 주문 취소
 * - POST /account/clear-cache: 캐시 삭제
 */

import { apiGet, apiPost, type LogEntry } from "./client";
import type { OrderRequest } from "@/types/order";
import { executeOverseasOrder } from "./overseas";

export interface OrderResponse {
  status: "success" | "error";
  message: string;
  data?: {
    order_id: string;
    status: string;
    message: string;
    protective_order?: Record<string, unknown>;
    protective_order_error?: string;
  };
  logs: LogEntry[];
}

export interface AccountInfo {
  deposit: {
    deposit: number;
    total_eval: number;
    purchase_amount: number;
    eval_amount: number;
    profit_loss: number;
  };
  holdings: HoldingItem[];
  holdings_count: number;
  cached_at?: string;
  stale?: boolean;
  error?: string;
}

export interface HoldingItem {
  stock_code: string;
  stock_name: string;
  quantity: number;
  avg_price: number;
  current_price: number;
  eval_amount: number;
  profit_loss: number;
  profit_rate: number;
}

export interface PendingOrder {
  order_no: string;
  org_no?: string;
  stock_code: string;
  stock_name: string;
  order_type: string;
  order_qty: number;
  order_price: number;
  filled_qty: number;
  unfilled_qty: number;
  order_time: string;
}

export interface PendingOrdersResponse {
  status: string;
  orders: PendingOrder[];
  total_count: number;
}

export interface CancelOrderRequest {
  order_no: string;
  org_no: string;
  stock_code: string;
  qty: number;
}

export interface CancelOrderResponse {
  status: string;
  success: boolean;
  order_no: string;
  message: string;
}

export type ReservationMarket = "domestic" | "us";
export type ReservationAction = "BUY" | "SELL";
export type ReservationOrderType = "limit" | "market" | "preopen" | "moo";

export interface ReservationOrderItem {
  [key: string]: unknown;
}

export interface ReservationSubmitRequest {
  market: ReservationMarket;
  stock_code: string;
  stock_name?: string;
  action: ReservationAction;
  quantity: number;
  price: number;
  order_type: ReservationOrderType;
  exchange?: "NASD" | "NYSE" | "AMEX";
  end_date?: string | null;
  confirm_prod?: boolean;
}

export interface ReservationCancelRequest {
  market: ReservationMarket;
  reservation_order_no: string;
  reservation_order_date: string;
  reservation_order_org_no?: string;
  confirm_prod?: boolean;
}

export interface ReservationModifyRequest extends ReservationSubmitRequest {
  reservation_order_no: string;
  reservation_order_date: string;
  reservation_order_org_no: string;
}

export interface ReservationSubmitResponse {
  status: "success" | "error";
  message: string;
  data?: Record<string, unknown>;
}

export interface ReservationListParams {
  market?: ReservationMarket;
  start_date?: string;
  end_date?: string;
  stock_code?: string;
  action?: ReservationAction | "";
  exchange?: "NASD" | "NYSE" | "AMEX";
  include_cancelled?: boolean;
}

export interface ReservationListResponse {
  status: "success" | "error";
  message?: string;
  orders: ReservationOrderItem[];
  total_count: number;
  market?: ReservationMarket;
  start_date?: string;
  end_date?: string;
  data?: Record<string, unknown>;
}

export type ExitOrderType = "market" | "limit";

export interface ProtectiveOrder {
  id?: string;
  status: string;
  source?: string;
  market?: "domestic" | "us";
  exchange?: string | null;
  currency?: string;
  stock_code: string;
  stock_name: string;
  quantity: number;
  entry_price: number;
  take_profit_enabled?: boolean;
  take_profit_trigger_price?: number;
  take_profit_limit_price?: number;
  take_profit_order_type?: ExitOrderType;
  stop_loss_enabled?: boolean;
  stop_loss_price?: number;
  stop_loss_limit_price?: number;
  stop_loss_order_type?: ExitOrderType;
  last_price?: number;
  last_checked_at?: string;
  exit_reason?: string;
  exit_order_type?: ExitOrderType;
  last_error?: string;
  next_retry_at?: string | null;
  retry_count?: number;
  last_error_code?: string | null;
  unsupported_paths?: string[];
  app_exit_reservation_status?: "waiting_retry" | string;
  app_exit_reserved_at?: string;
  app_exit_reason?: string;
  app_exit_reservation?: {
    status?: string;
    market?: "us";
    env_dv?: string;
    stock_code?: string;
    exchange?: string | null;
    quantity?: number;
    exit_reason?: string;
    order_type?: ExitOrderType;
    limit_price?: number | null;
    current_price?: number;
    reserved_at?: string;
    last_attempt_at?: string;
    last_error?: string;
    next_retry_at?: string | null;
    retry_count?: number;
    last_error_code?: string | null;
    unsupported_paths?: string[];
    note?: string;
  };
}

export interface ProtectiveOrdersResponse {
  status: string;
  orders: ProtectiveOrder[];
  total_count: number;
  settings?: ProtectiveSettings;
  realtime?: ProtectiveRealtimeStatus;
}

export interface ProtectiveSettings {
  monitor_interval_seconds: number;
  price_source?: "websocket" | "rest";
}

export interface ProtectiveRealtimeTick {
  market: "domestic" | "us";
  stock_code: string;
  exchange?: string | null;
  price: number;
  volume?: number;
  tick_time?: string;
  received_at?: string;
}

export interface ProtectiveRealtimeStatus {
  connected: boolean;
  subscription_count: number;
  last_connected_at?: string | null;
  last_error?: string | null;
  latest_ticks?: ProtectiveRealtimeTick[];
}

export interface ProtectiveOrderUpsertRequest {
  stock_code: string;
  stock_name: string;
  quantity: number;
  entry_price: number;
  enabled: boolean;
  take_profit_enabled: boolean;
  take_profit_trigger_price?: number | null;
  take_profit_order_type: ExitOrderType;
  take_profit_limit_price?: number | null;
  stop_loss_enabled: boolean;
  stop_loss_trigger_price?: number | null;
  stop_loss_order_type: ExitOrderType;
  stop_loss_limit_price?: number | null;
  market?: "domestic" | "us";
  exchange?: string | null;
  currency?: string;
}

/**
 * 주문 실행
 */
export async function executeOrder(request: OrderRequest): Promise<OrderResponse> {
  if (request.market === "us") {
    return executeOverseasOrder(request);
  }

  return apiPost<OrderResponse>("/api/orders/execute", {
    stock_code: request.stock_code,
    stock_name: request.stock_name,
    action: request.action,
    order_type: request.order_type,
    price: request.price || 0,
    quantity: request.quantity,
    signal_reason: request.signal_reason || "수동 주문",
    protective_order: request.protective_order,
    market: request.market || "domestic",
    exchange: request.exchange,
    confirm_prod: request.confirm_prod,
  });
}

/**
 * 통합 계좌 정보 조회 (30초 캐싱)
 * 
 * Note: 이 함수는 orders API를 통해 계좌 정보를 조회합니다.
 * account.ts의 getAccountInfo와 다른 API를 사용합니다.
 */
export async function getOrdersAccount(): Promise<AccountInfo> {
  const response = await apiGet<{ status: string } & AccountInfo>("/api/orders/account");
  return response;
}

/**
 * 계좌 정보 캐시 삭제
 */
export async function clearAccountCache(): Promise<{ status: string; message: string }> {
  return apiPost<{ status: string; message: string }>("/api/orders/account/clear-cache");
}

/**
 * 미체결 주문 목록 조회
 */
export async function getPendingOrders(): Promise<PendingOrdersResponse> {
  return apiGet<PendingOrdersResponse>("/api/orders/pending");
}

/**
 * 주문 취소
 */
export async function cancelOrder(request: CancelOrderRequest): Promise<CancelOrderResponse> {
  return apiPost<CancelOrderResponse>("/api/orders/cancel", request);
}

export async function submitReservationOrder(
  request: ReservationSubmitRequest
): Promise<ReservationSubmitResponse> {
  return apiPost<ReservationSubmitResponse>("/api/orders/reservations", request);
}

export async function getReservationOrders(
  params: ReservationListParams = {}
): Promise<ReservationListResponse> {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.set(key, String(value));
  });
  const query = search.toString();
  return apiGet<ReservationListResponse>(`/api/orders/reservations${query ? `?${query}` : ""}`);
}

export async function cancelReservationOrder(
  request: ReservationCancelRequest
): Promise<ReservationSubmitResponse> {
  return apiPost<ReservationSubmitResponse>("/api/orders/reservations/cancel", request);
}

export async function modifyReservationOrder(
  request: ReservationModifyRequest
): Promise<ReservationSubmitResponse> {
  return apiPost<ReservationSubmitResponse>("/api/orders/reservations/modify", request);
}

export async function getProtectiveOrders(): Promise<ProtectiveOrdersResponse> {
  return apiGet<ProtectiveOrdersResponse>("/api/orders/protective");
}

export async function saveProtectiveOrder(request: ProtectiveOrderUpsertRequest): Promise<{ status: string; order: ProtectiveOrder }> {
  return apiPost<{ status: string; order: ProtectiveOrder }>("/api/orders/protective", request);
}

export async function saveProtectiveSettings(
  request: ProtectiveSettings
): Promise<{ status: string; settings: ProtectiveSettings }> {
  return apiPost<{ status: string; settings: ProtectiveSettings }>("/api/orders/protective/settings", request);
}

export async function checkProtectiveOrders(): Promise<ProtectiveOrdersResponse> {
  return apiPost<ProtectiveOrdersResponse>("/api/orders/protective/check");
}
