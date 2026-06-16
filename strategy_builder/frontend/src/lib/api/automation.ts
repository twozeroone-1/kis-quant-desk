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
    deferred?: number;
  };
  buy_notional: number;
  sell_notional?: number;
  pending_count: number;
  app_reservation_count: number;
  protective_count: number;
  errors: string[];
  warnings?: string[];
}

export interface AutomationDailyRecordPoint {
  run_id: string;
  time: string;
  equity: number;
  cash: number;
  holdings_value: number;
  buy_notional: number;
  sell_notional: number;
  net_trade_cashflow: number;
}

export interface AutomationDailyRecord {
  source: "automation_report";
  estimate: boolean;
  valid: boolean;
  anomalies: string[];
  start_equity: number;
  end_equity: number;
  pnl: number;
  pnl_pct: number;
  start_cash: number;
  end_cash: number;
  cash_delta: number;
  start_holdings_value: number;
  end_holdings_value: number;
  holdings_value_delta: number;
  buy_notional: number;
  sell_notional: number;
  net_trade_cashflow: number;
  cash_reconciliation_delta: number;
  points: AutomationDailyRecordPoint[];
}

export interface AutomationMonthlyRecordDay {
  date: string;
  session_date: string;
  valid: boolean;
  anomalies: string[];
  run_count: number;
  pnl: number;
  pnl_pct: number;
  start_equity: number;
  end_equity: number;
  start_cash: number;
  end_cash: number;
  cash_delta: number;
  start_holdings_value: number;
  end_holdings_value: number;
  holdings_value_delta: number;
  buy_notional: number;
  sell_notional: number;
  net_trade_cashflow: number;
  cash_reconciliation_delta: number;
  error_count: number;
}

export interface AutomationMonthlyRecord {
  market: AutomationMarket;
  month: string;
  source: "automation_report";
  estimate: boolean;
  summary: {
    day_count: number;
    trading_days: number;
    anomaly_days: number;
    win_days: number;
    loss_days: number;
    flat_days: number;
    pnl: number;
    pnl_pct: number;
    account_pnl: number;
    account_pnl_pct: number;
    start_equity: number;
    end_equity: number;
    buy_notional: number;
    sell_notional: number;
    net_trade_cashflow: number;
    cash_delta: number;
    cash_reconciliation_delta: number;
    error_count: number;
  };
  days: AutomationMonthlyRecordDay[];
}

export interface AutomationPositionJournalEntry {
  symbol: string;
  name: string;
  market?: string | null;
  quantity: number;
  entry_price: number;
  entry_notional: number;
  opened_at: string;
  opened_run_id: string;
  status: "active" | "closed" | "exiting" | string;
  status_label: string;
  exit_reason?: string | null;
  exit_reason_label?: string | null;
  exit_at?: string | null;
  exit_run_id?: string | null;
  exit_price?: number | null;
  exit_notional?: number | null;
  exit_order_type?: string | null;
  protection_id?: string | null;
  protection_status?: string | null;
  last_error?: string | null;
  held_days: number;
  held_over_2_days: boolean;
}

export interface AutomationSession {
  session_date: string;
  mode: "vps";
  updated_at: string;
  run_count: number;
  runs: AutomationRunSummary[];
  cumulative_buy_notional: number;
  cumulative_sell_notional?: number;
  daily_record?: AutomationDailyRecord | null;
  position_journal?: AutomationPositionJournalEntry[];
  position_journal_summary?: {
    total: number;
    active: number;
    closed: number;
    exiting: number;
    unknown?: number;
    take_profit: number;
    stop_loss: number;
    strategy_sell: number;
    held_over_2_days: number;
  };
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
    deferred?: number;
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
  warnings?: string[];
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

export async function getAutomationMonthlyRecord(market: AutomationMarket, month: string) {
  return apiGet<{ status: "success"; data: AutomationMonthlyRecord }>(
    `/api/automation/${market}/records/monthly?month=${encodeURIComponent(month)}`
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
