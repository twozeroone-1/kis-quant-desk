/**
 * Domestic screening and candidate discovery APIs
 */

import { apiGet } from "./client";

export type ScreeningStatus = "success" | "error";
export type ScreeningRecord = Record<string, unknown>;

export interface ScreeningMetadata {
  api_url?: string | null;
  tr_id?: string | null;
  error_code?: string | null;
}

export interface ScreeningListResponse {
  status: ScreeningStatus;
  message?: string;
  items: ScreeningRecord[];
  total_count: number;
  data?: ScreeningMetadata;
}

export interface ScreeningMultiResponse {
  status: ScreeningStatus;
  message?: string;
  outputs: Record<string, ScreeningRecord[]>;
  counts: Record<string, number>;
  data?: ScreeningMetadata;
}

export interface ConditionSearchParams {
  user_id?: string;
}

export interface MarketCapRankParams {
  market_div?: string;
  input_iscd?: string;
  div_cls?: string;
  price_min?: string;
  price_max?: string;
  volume_min?: string;
  max_depth?: number;
}

export interface FluctuationRankParams extends MarketCapRankParams {
  rank_sort?: string;
  count?: string;
  price_cls?: string;
  target_cls?: string;
  target_exclude?: string;
  rate_min?: string;
  rate_max?: string;
}

export interface VolumeRankParams extends MarketCapRankParams {
  blng_cls?: string;
  target_cls?: string;
  target_exclude?: string;
  input_date?: string;
}

export interface VolumePowerRankParams extends MarketCapRankParams {
  target_cls?: string;
  target_exclude?: string;
}

export interface ForeignInstitutionParams {
  market_div?: string;
  input_iscd?: string;
  div_cls?: string;
  rank_sort?: string;
  etc_cls?: string;
}

export interface InvestorDailyParams {
  date?: string;
  market_div?: string;
  max_depth?: number;
}

export interface MinuteChartParams {
  market_div?: string;
  input_time?: string;
  include_past?: string;
}

function withQuery(path: string, params?: object): string {
  const query = new URLSearchParams();
  Object.entries(params ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== "") {
      query.set(key, String(value));
    }
  });
  const queryString = query.toString();
  return queryString ? `${path}?${queryString}` : path;
}

export function getConditionSearches(params?: ConditionSearchParams): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(withQuery("/api/screening/condition-searches", params));
}

export function getConditionSearchResults(
  seq: string,
  params?: ConditionSearchParams
): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(
    withQuery(`/api/screening/condition-searches/${encodeURIComponent(seq)}/results`, params)
  );
}

export function getMarketCapRank(params?: MarketCapRankParams): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(withQuery("/api/screening/rankings/market-cap", params));
}

export function getFluctuationRank(params?: FluctuationRankParams): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(withQuery("/api/screening/rankings/fluctuation", params));
}

export function getVolumeRank(params?: VolumeRankParams): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(withQuery("/api/screening/rankings/volume", params));
}

export function getVolumePowerRank(params?: VolumePowerRankParams): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(withQuery("/api/screening/rankings/volume-power", params));
}

export function getInvestorTrendEstimate(stockCode: string): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(`/api/screening/investors/trend/${encodeURIComponent(stockCode)}`);
}

export function getForeignInstitutionTotal(params?: ForeignInstitutionParams): Promise<ScreeningListResponse> {
  return apiGet<ScreeningListResponse>(withQuery("/api/screening/investors/foreign-institution", params));
}

export function getInvestorTradeDaily(
  stockCode: string,
  params?: InvestorDailyParams
): Promise<ScreeningMultiResponse> {
  return apiGet<ScreeningMultiResponse>(
    withQuery(`/api/screening/investors/daily/${encodeURIComponent(stockCode)}`, params)
  );
}

export function getMinuteChart(stockCode: string, params?: MinuteChartParams): Promise<ScreeningMultiResponse> {
  return apiGet<ScreeningMultiResponse>(
    withQuery(`/api/screening/minute-chart/${encodeURIComponent(stockCode)}`, params)
  );
}
