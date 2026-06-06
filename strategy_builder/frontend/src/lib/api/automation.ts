import { apiGet } from "./client";

export interface AutomationRunSummary {
  run_id: string;
  slot: string;
  scheduled_at_et?: string;
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
  signals: Array<Record<string, unknown>>;
  orders: Array<Record<string, unknown>>;
  submitted_sells: Array<Record<string, unknown>>;
  errors: string[];
  account_before: Record<string, unknown>;
  account_after: Record<string, unknown>;
}

export async function getUsAutomationSessions() {
  return apiGet<{ status: "success"; sessions: AutomationSession[]; total_count: number }>(
    "/api/automation/us/sessions"
  );
}

export async function getUsAutomationSession(sessionDate: string) {
  return apiGet<{ status: "success"; data: AutomationSession }>(
    `/api/automation/us/sessions/${sessionDate}`
  );
}

export async function getUsAutomationRun(runId: string) {
  return apiGet<{ status: "success"; data: AutomationRunDetail }>(
    `/api/automation/us/runs/${runId}`
  );
}
