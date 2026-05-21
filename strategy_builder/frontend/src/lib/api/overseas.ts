/**
 * Overseas stock API
 */

import { apiGet, apiPost, type ApiResponse } from "./client";
import type { Holding, Balance, BuyableInfo } from "@/types/account";
import type { OrderRequest } from "@/types/order";
import type { OrderResponse, PendingOrdersResponse, CancelOrderRequest, CancelOrderResponse } from "./orders";
import type { PriceResponse } from "./market";

export type OverseasExchange = "NASD" | "NYSE" | "AMEX";

export interface OverseasSymbolInfo {
  symbol: string;
  exchange: OverseasExchange;
  price_exchange: string;
  name: string;
  warning?: string;
}

export async function searchOverseasSymbol(
  symbol: string,
  exchange?: OverseasExchange
): Promise<ApiResponse<OverseasSymbolInfo>> {
  const query = exchange ? `?exchange=${exchange}` : "";
  return apiGet<ApiResponse<OverseasSymbolInfo>>(`/api/overseas/search/${symbol}${query}`);
}

export async function getOverseasPrice(
  symbol: string,
  exchange?: OverseasExchange,
  envDv: string = "vps"
): Promise<PriceResponse> {
  const params = new URLSearchParams({ env_dv: envDv });
  if (exchange) params.append("exchange", exchange);
  return apiGet<PriceResponse>(`/api/overseas/price/${symbol}?${params}`);
}

export async function getOverseasHoldings(): Promise<ApiResponse<Holding[]>> {
  return apiGet<ApiResponse<Holding[]>>("/api/overseas/holdings");
}

export async function getOverseasBalance(): Promise<ApiResponse<Balance>> {
  return apiGet<ApiResponse<Balance>>("/api/overseas/balance");
}

export async function getOverseasBuyableAmount(
  symbol: string,
  price: number = 0,
  exchange?: OverseasExchange
): Promise<ApiResponse<BuyableInfo>> {
  const params = new URLSearchParams();
  if (price > 0) params.append("price", String(price));
  if (exchange) params.append("exchange", exchange);
  const query = params.toString() ? `?${params}` : "";
  return apiGet<ApiResponse<BuyableInfo>>(`/api/overseas/buyable/${symbol}${query}`);
}

export async function getOverseasPendingOrders(): Promise<PendingOrdersResponse> {
  return apiGet<PendingOrdersResponse>("/api/overseas/pending");
}

export async function executeOverseasOrder(request: OrderRequest): Promise<OrderResponse> {
  return apiPost<OrderResponse>("/api/overseas/order", {
    ...request,
    market: "us",
    price: request.price || 0,
  });
}

export async function cancelOverseasOrder(
  request: CancelOrderRequest & { exchange?: OverseasExchange }
): Promise<CancelOrderResponse> {
  return apiPost<CancelOrderResponse>("/api/overseas/cancel", request);
}
