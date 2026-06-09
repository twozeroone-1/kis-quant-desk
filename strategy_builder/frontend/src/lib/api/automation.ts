import { apiGet } from "./client";

export type AutomationMarket = "us" | "kr";

export interface AutomationRunSummary {
  run_id: string;
  slot: string;
  scheduled_at_et?: string;
  scheduled_at_kst?: string;
  started_at: string;
  finished_at?: string;
  duration_seconds: number;
  status: string;
  report_only: boolean;
  signal_counts: Record<"BUY" | "SELL" | "HOLD" | "ERROR", number>;
  order_counts: {
    submitted: number;
    filled: number;
    failed: number;
    skipped: number;
  };
  buy_notional: number;
  pending_count: number;
  app_reservation_count: number;
  protective_count: number;
  errors: string[];
}

export interface AutomationSession {
  session_date: string;
  mode: "vps";
  updated_at: string;
  run_count: number;
  runs: AutomationRunSummary[];
  cumulative_buy_notional: number;
  session_buy_limit: number;
  remaining_buy_budget: number;
  session_loss_limit: number;
  remaining_loss_budget: number;
  latest_account?: {
    cash?: number;
    risk_equity?: number;
    equity?: number;
    holdings_value?: number;
    holdings_count?: number;
    risk_equity_sources?: string[];
  };
  totals: {
    submitted: number;
    filled: number;
    failed: number;
    errors: number;
  };
}

export interface AutomationRunDetail {
  run_id: string;
  status: string;
  report_only: boolean;
  started_at: string;
  duration_seconds: number;
  market_risk?: Record<string, unknown>;
  strategy_orchestration?: {
    regime?: string;
    enabled_count?: number;
    risk_gate_open?: boolean;
    warnings?: string[];
    target_strategy_count?: { min?: number; max?: number };
    enabled?: Array<Record<string, unknown>>;
    disabled?: Array<Record<string, unknown>>;
  };
  strategy_run?: {
    successful_strategy_count?: number;
    failed_strategy_count?: number;
    raw_result_count?: number;
    errors?: string[];
    runs?: Array<Record<string, unknown>>;
  };
  order_decisions?: Array<Record<string, unknown>>;
  signals: Array<Record<string, unknown>>;
  orders: Array<Record<string, unknown>>;
  submitted_sells: Array<Record<string, unknown>>;
  errors: string[];
  account_before: Record<string, unknown>;
  account_after: Record<string, unknown>;
}

export async function getAutomationSessions(market: AutomationMarket) {
  return apiGet<{ status: "success"; sessions: AutomationSession[]; total_count: number }>(
    `/api/automation/${market}/sessions`
  );
}

export async function getAutomationSession(market: AutomationMarket, sessionDate: string) {
  return apiGet<{ status: "success"; data: AutomationSession }>(
    `/api/automation/${market}/sessions/${sessionDate}`
  );
}

export async function getAutomationRun(market: AutomationMarket, runId: string) {
  return apiGet<{ status: "success"; data: AutomationRunDetail }>(
    `/api/automation/${market}/runs/${runId}`
  );
}

export function getUsAutomationSessions() {
  return getAutomationSessions("us");
}

export function getUsAutomationSession(sessionDate: string) {
  return getAutomationSession("us", sessionDate);
}

export function getUsAutomationRun(runId: string) {
  return getAutomationRun("us", runId);
}
