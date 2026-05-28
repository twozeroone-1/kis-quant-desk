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
  submitReservationOrder,
  getReservationOrders,
  cancelReservationOrder,
  modifyReservationOrder,
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
  type ReservationMarket,
  type ReservationAction,
  type ReservationOrderType,
  type ReservationOrderItem,
  type ReservationSubmitRequest,
  type ReservationCancelRequest,
  type ReservationModifyRequest,
  type ReservationSubmitResponse,
  type ReservationListParams,
  type ReservationListResponse,
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

// Screening
export {
  getConditionSearches,
  getConditionSearchResults,
  getMarketCapRank,
  getFluctuationRank,
  getVolumeRank,
  getVolumePowerRank,
  getInvestorTrendEstimate,
  getForeignInstitutionTotal,
  getInvestorTradeDaily,
  getMinuteChart,
  type ScreeningStatus,
  type ScreeningRecord,
  type ScreeningMetadata,
  type ScreeningListResponse,
  type ScreeningMultiResponse,
  type ConditionSearchParams,
  type MarketCapRankParams,
  type FluctuationRankParams,
  type VolumeRankParams,
  type VolumePowerRankParams,
  type ForeignInstitutionParams,
  type InvestorDailyParams,
  type MinuteChartParams,
} from "./screening";

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
