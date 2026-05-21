/**
 * API Module Exports
 */

// Client
export {
  apiGet,
  apiPost,
  apiPut,
  apiDelete,
  ApiError,
  API_BASE,
  type ApiResponse,
  type LogEntry,
} from "./client";

// Auth
export {
  login,
  getAuthStatus,
  logout,
  switchMode,
} from "./auth";

// Symbols
export {
  searchSymbols,
  getMasterStatus,
  collectMasterFiles,
} from "./symbols";

// Account
export {
  getAccountInfo,
  getHoldings,
  getBalance,
  getBuyableAmount,
} from "./account";

// Strategies
export {
  listStrategies,
  listCustomStrategies,
  executeStrategy,
  listIndicators,
  buildStrategy,
  previewStrategy,
  previewCodeFromState,
  type StrategiesListResponse,
  type IndicatorsResponse,
  type BuildRequest,
  type BuildResponse,
  type PreviewResponse,
  type PreviewCodeResponse,
} from "./strategies";

// Orders
export {
  executeOrder,
  getOrdersAccount,
  clearAccountCache,
  getPendingOrders,
  cancelOrder,
  getProtectiveOrders,
  saveProtectiveOrder,
  saveProtectiveSettings,
  checkProtectiveOrders,
  type OrderResponse,
  type AccountInfo as OrdersAccountInfo,
  type HoldingItem,
  type PendingOrder,
  type PendingOrdersResponse,
  type CancelOrderRequest,
  type CancelOrderResponse,
  type ExitOrderType,
  type ProtectiveOrder,
  type ProtectiveOrdersResponse,
  type ProtectiveSettings,
  type ProtectiveRealtimeStatus,
  type ProtectiveRealtimeTick,
  type ProtectiveOrderUpsertRequest,
} from "./orders";

// Market
export {
  getOrderbook,
  getCurrentPrice,
  type OrderbookData,
  type OrderbookResponse,
  type PriceData,
  type PriceResponse,
} from "./market";

// Overseas
export {
  searchOverseasSymbol,
  getOverseasPrice,
  getOverseasHoldings,
  getOverseasBalance,
  getOverseasBuyableAmount,
  getOverseasPendingOrders,
  executeOverseasOrder,
  cancelOverseasOrder,
  type OverseasExchange,
  type OverseasSymbolInfo,
} from "./overseas";
