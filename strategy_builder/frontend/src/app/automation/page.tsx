"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, BookOpen, Download, RefreshCw } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useAuth } from "@/hooks";
import {
  getAutomationMonthlyRecord,
  getAutomationRun,
  getAutomationSession,
  getAutomationSessions,
  type AutomationMarket,
  type AutomationMonthlyRecord,
  type AutomationRunDetail,
  type AutomationSession,
} from "@/lib/api";

type AutomationView = "execution" | "record";

const marketConfig: Record<
  AutomationMarket,
  {
    label: string;
    description: string;
    timeLabel: string;
    timezone: string;
    currency: "USD" | "KRW";
    selectLabel: string;
    empty: string;
  }
> = {
  us: {
    label: "미국장 자동매매",
    description: "XNYS 정규장 시간당 모의투자 실행 기록",
    timeLabel: "UTC+09:00",
    timezone: "Asia/Seoul",
    currency: "USD",
    selectLabel: "미국장 세션 선택",
    empty: "아직 생성된 미국장 자동매매 세션이 없습니다.",
  },
  kr: {
    label: "한국장 자동매매",
    description: "KRX 정규장 시간당 모의투자 실행 기록",
    timeLabel: "KST",
    timezone: "Asia/Seoul",
    currency: "KRW",
    selectLabel: "한국장 세션 선택",
    empty: "아직 생성된 한국장 자동매매 세션이 없습니다.",
  },
};

const moneyFormatters = {
  USD: new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }),
  KRW: new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency: "KRW",
    maximumFractionDigits: 0,
  }),
};

const viewTabs: Array<{ key: AutomationView; Icon: LucideIcon; label: string }> = [
  { key: "execution", Icon: Activity, label: "실행" },
  { key: "record", Icon: BookOpen, label: "기록" },
];

function statusClass(status: string) {
  if (status === "completed") return "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300";
  if (status === "report_only" || status === "market_closed") return "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300";
  if (status === "failed") return "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300";
  return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
}

function marketTime(value: string | undefined, market: AutomationMarket) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: marketConfig[market].timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function sessionDateToMonth(sessionDate: string | undefined) {
  if (!sessionDate || sessionDate.length < 6) return "";
  return `${sessionDate.slice(0, 4)}-${sessionDate.slice(4, 6)}`;
}

function sessionDateToIso(sessionDate: string | undefined) {
  if (!sessionDate || sessionDate.length !== 8) return "";
  return `${sessionDate.slice(0, 4)}-${sessionDate.slice(4, 6)}-${sessionDate.slice(6, 8)}`;
}

function dayLabel(value: string) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
  }).format(new Date(`${value}T00:00:00+09:00`));
}

function monthLabel(value: string) {
  if (!value) return "-";
  const [year, month] = value.split("-");
  return `${year}년 ${Number(month)}월`;
}

function signedMoney(formatter: Intl.NumberFormat, value: number) {
  if (!Number.isFinite(value) || value === 0) return formatter.format(0);
  return value > 0 ? `+${formatter.format(value)}` : formatter.format(value);
}

function signedPercent(value: number) {
  if (!Number.isFinite(value) || value === 0) return "0.00%";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function valueTone(value: number) {
  if (value > 0) return "text-green-700 dark:text-green-300";
  if (value < 0) return "text-red-700 dark:text-red-300";
  return "text-slate-700 dark:text-slate-300";
}

function orderSymbol(item: Record<string, unknown>) {
  return String(item.symbol ?? item.code ?? item.stock_code ?? "-");
}

function orderNotional(item: Record<string, unknown>) {
  const explicit = Number(item.notional ?? item.amount ?? 0);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  const price = Number(item.limit_price ?? item.target_price ?? item.price ?? 0);
  const quantity = Number(item.quantity ?? 0);
  return Number.isFinite(price) && Number.isFinite(quantity) ? price * quantity : 0;
}

function textList(value: unknown) {
  if (!Array.isArray(value)) return "-";
  const items = value.map((item) => String(item)).filter(Boolean);
  return items.length ? items.join("; ") : "-";
}

export default function AutomationPage() {
  const { status } = useAuth();
  const [market, setMarket] = useState<AutomationMarket>("us");
  const [sessions, setSessions] = useState<AutomationSession[]>([]);
  const [session, setSession] = useState<AutomationSession | null>(null);
  const [run, setRun] = useState<AutomationRunDetail | null>(null);
  const [view, setView] = useState<AutomationView>("execution");
  const [selectedMonth, setSelectedMonth] = useState("");
  const [monthlyRecord, setMonthlyRecord] = useState<AutomationMonthlyRecord | null>(null);
  const [monthlyLoading, setMonthlyLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const money = moneyFormatters[marketConfig[market].currency];
  const config = marketConfig[market];
  const latestCash = Number(session?.latest_account?.cash ?? 0);
  const dailyRecord = session?.daily_record ?? null;
  const monthOptions = useMemo(
    () =>
      Array.from(
        new Set(sessions.map((item) => sessionDateToMonth(item.session_date)).filter(Boolean))
      )
        .sort()
        .reverse(),
    [sessions]
  );

  const loadSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getAutomationSessions(market);
      setSessions(response.sessions);
      if (response.sessions.length) {
        const selected = await getAutomationSession(market, response.sessions[0].session_date);
        setSession(selected.data);
        const defaultMonth = sessionDateToMonth(response.sessions[0].session_date);
        setSelectedMonth((current) =>
          current &&
          response.sessions.some((item) => sessionDateToMonth(item.session_date) === current)
            ? current
            : defaultMonth
        );
      } else {
        setSession(null);
        setSelectedMonth("");
        setMonthlyRecord(null);
      }
      setRun(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "자동매매 리포트를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, [market]);

  const loadMonthlyRecord = useCallback(async () => {
    if (!selectedMonth) return;
    setMonthlyLoading(true);
    setError(null);
    try {
      const response = await getAutomationMonthlyRecord(market, selectedMonth);
      setMonthlyRecord(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "월간 기록을 불러오지 못했습니다.");
    } finally {
      setMonthlyLoading(false);
    }
  }, [market, selectedMonth]);

  useEffect(() => {
    if (status.mode === "vps") void loadSessions();
  }, [loadSessions, status.mode]);

  useEffect(() => {
    if (status.mode === "vps" && view === "record" && selectedMonth) void loadMonthlyRecord();
  }, [loadMonthlyRecord, selectedMonth, status.mode, view]);

  const selectMarket = (nextMarket: AutomationMarket) => {
    if (nextMarket === market) return;
    setMarket(nextMarket);
    setSessions([]);
    setSession(null);
    setRun(null);
    setSelectedMonth("");
    setMonthlyRecord(null);
  };

  const selectSession = async (sessionDate: string) => {
    setError(null);
    setRun(null);
    try {
      const response = await getAutomationSession(market, sessionDate);
      setSession(response.data);
      setSelectedMonth(sessionDateToMonth(sessionDate));
    } catch (err) {
      setError(err instanceof Error ? err.message : "세션을 불러오지 못했습니다.");
    }
  };

  const selectMonthlyDay = async (sessionDate: string) => {
    setView("record");
    await selectSession(sessionDate);
  };

  const selectRun = async (runId: string) => {
    setError(null);
    setView("execution");
    try {
      const response = await getAutomationRun(market, runId);
      setRun(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "실행 상세를 불러오지 못했습니다.");
    }
  };

  const selectedRunSummary = useMemo(
    () => session?.runs.find((item) => item.run_id === run?.run_id),
    [run?.run_id, session?.runs]
  );

  if (status.mode !== "vps") {
    return (
      <div className="max-w-7xl mx-auto px-4 py-8">
        <div className="border border-slate-200 dark:border-slate-800 p-6 rounded-lg">
          자동매매 리포트는 8081 모의투자 환경에서만 제공됩니다.
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto px-4 py-6 space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="inline-flex rounded-md border border-slate-300 dark:border-slate-700 overflow-hidden mb-3">
            {(["us", "kr"] as const).map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={market === item}
                onClick={() => selectMarket(item)}
                className={`h-9 px-4 text-sm font-medium ${
                  market === item
                    ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                    : "bg-white text-slate-700 hover:bg-slate-100 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                {marketConfig[item].label}
              </button>
            ))}
          </div>
          <h2 className="text-heading">{config.label}</h2>
          <p className="text-body text-slate-500 mt-1">{config.description}</p>
        </div>
        <div className="flex items-center gap-2">
          <select
            aria-label={config.selectLabel}
            value={session?.session_date ?? ""}
            onChange={(event) => void selectSession(event.target.value)}
            className="h-10 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 rounded-md px-3 text-sm"
          >
            {!sessions.length && <option value="">세션 없음</option>}
            {sessions.map((item) => (
              <option key={item.session_date} value={item.session_date}>
                {item.session_date}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void loadSessions()}
            className="h-10 w-10 inline-flex items-center justify-center border border-slate-300 dark:border-slate-700 rounded-md hover:bg-slate-100 dark:hover:bg-slate-800"
            aria-label="리포트 새로고침"
            title="새로고침"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      <div
        className="inline-flex rounded-md border border-slate-300 dark:border-slate-700 overflow-hidden"
        role="tablist"
        aria-label="자동매매 보기"
      >
        {viewTabs.map(({ key, Icon, label }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={view === key}
            onClick={() => {
              setView(key);
              if (key === "record") setRun(null);
            }}
            className={`h-9 px-4 inline-flex items-center gap-2 text-sm font-medium ${
              view === key
                ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                : "bg-white text-slate-700 hover:bg-slate-100 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-800"
            }`}
          >
            <Icon className="w-4 h-4" />
            {label}
          </button>
        ))}
      </div>

      {error && (
        <div className="flex items-start gap-2 border border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300 rounded-md p-3 text-sm">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span className="break-words">{error}</span>
        </div>
      )}

      {session && (
        <>
          {view === "execution" && (
            <>
          <section className="grid grid-cols-2 lg:grid-cols-7 gap-3" aria-label="세션 요약">
            {[
              ["실행", `${session.run_count}회`],
              ["누적 매수", money.format(session.cumulative_buy_notional ?? 0)],
              ["누적 매도", money.format(session.cumulative_sell_notional ?? 0)],
              ["리포트 기준 주문가능", money.format(latestCash)],
              ["남은 자동매수 한도", money.format(session.remaining_buy_budget)],
              ["남은 손실 한도", money.format(session.remaining_loss_budget)],
              ["오류", `${session.totals.errors}건`],
            ].map(([label, value]) => (
              <div key={label} className="card min-w-0">
                <span className="text-caption text-slate-500">{label}</span>
                <strong className="block text-lg mt-1 break-words">{value}</strong>
              </div>
            ))}
          </section>

          <section className="border-y border-slate-200 dark:border-slate-800">
            <div className="py-4">
              <h3 className="text-subheading">시간별 타임라인</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[920px] text-sm">
                <thead className="bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
                  <tr>
                    <th className="text-left px-3 py-2">{config.timeLabel}</th>
                    <th className="text-left px-3 py-2">상태</th>
                    <th className="text-right px-3 py-2">BUY/SELL/HOLD</th>
                    <th className="text-right px-3 py-2">제출/체결/실패</th>
                    <th className="text-right px-3 py-2">매수금액</th>
                    <th className="text-right px-3 py-2">매도금액</th>
                    <th className="text-right px-3 py-2">대기/보호</th>
                    <th className="text-right px-3 py-2">리포트</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                  {session.runs.map((item) => {
                    const scheduledAt = market === "kr" ? item.scheduled_at_kst : item.started_at;
                    return (
                      <tr key={item.run_id} className="hover:bg-slate-50 dark:hover:bg-slate-900/60">
                        <td className="px-3 py-3 font-medium">
                          <button type="button" onClick={() => void selectRun(item.run_id)} className="hover:text-primary">
                            {marketTime(scheduledAt ?? item.started_at, market)}
                          </button>
                        </td>
                        <td className="px-3 py-3">
                          <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${statusClass(item.status)}`}>
                            {item.status}
                          </span>
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums">
                          {item.signal_counts.BUY}/{item.signal_counts.SELL}/{item.signal_counts.HOLD}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums">
                          {item.order_counts.submitted}/{item.order_counts.filled}/{item.order_counts.failed}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums">{money.format(item.buy_notional ?? 0)}</td>
                        <td className="px-3 py-3 text-right tabular-nums">{money.format(item.sell_notional ?? 0)}</td>
                        <td className="px-3 py-3 text-right tabular-nums">
                          {item.pending_count}/{item.protective_count}
                        </td>
                        <td className="px-3 py-3">
                          <div className="flex justify-end gap-1">
                            {(["md", "json"] as const).map((format) => (
                              <a
                                key={format}
                                href={`/api/automation/${market}/runs/${item.run_id}/download?format=${format}`}
                                className="h-8 px-2 inline-flex items-center gap-1 border border-slate-300 dark:border-slate-700 rounded-md hover:bg-slate-100 dark:hover:bg-slate-800"
                                title={`${format.toUpperCase()} 리포트 다운로드`}
                              >
                                <Download className="w-3.5 h-3.5" />
                                <span className="text-xs uppercase">{format}</span>
                              </a>
                            ))}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {run && (
            <section className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-subheading">{run.run_id}</h3>
                <span className="text-caption text-slate-500">
                  {run.duration_seconds.toFixed(1)}초 · {selectedRunSummary?.errors.length ?? 0} errors
                </span>
              </div>

              {(run.strategy_orchestration || run.strategy_run || run.order_decisions?.length) && (
                <div className="grid lg:grid-cols-2 gap-6">
                  <div>
                    <h4 className="text-sm font-semibold mb-2">전략 오케스트레이션</h4>
                    <div className="border border-slate-200 dark:border-slate-800 rounded-md overflow-hidden">
                      <div className="grid grid-cols-2 gap-px bg-slate-200 dark:bg-slate-800 text-sm">
                        {[
                          ["레짐", String(run.strategy_orchestration?.regime ?? run.market_risk?.regime ?? "-")],
                          ["전략 수", `${run.strategy_orchestration?.enabled_count ?? run.strategy_orchestration?.enabled?.length ?? 0}`],
                          ["성공/실패", `${run.strategy_run?.successful_strategy_count ?? 0}/${run.strategy_run?.failed_strategy_count ?? 0}`],
                          ["Risk gate", String(run.strategy_orchestration?.risk_gate_open ?? run.market_risk?.risk_gate_open ?? "-")],
                        ].map(([label, value]) => (
                          <div key={label} className="bg-white dark:bg-slate-950 p-3 min-w-0">
                            <span className="block text-caption text-slate-500">{label}</span>
                            <strong className="block mt-1 break-words">{value}</strong>
                          </div>
                        ))}
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full min-w-[520px] text-sm">
                          <thead className="bg-slate-100 dark:bg-slate-800">
                            <tr>
                              <th className="text-left px-3 py-2">전략</th>
                              <th className="text-left px-3 py-2">계열</th>
                              <th className="text-right px-3 py-2">가중치</th>
                              <th className="text-left px-3 py-2">이유</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                            {(run.strategy_orchestration?.enabled ?? []).map((item, index) => (
                              <tr key={`${String(item.id ?? index)}-${index}`}>
                                <td className="px-3 py-2">{String(item.name ?? item.id ?? "-")}</td>
                                <td className="px-3 py-2">{String(item.family ?? "-")}</td>
                                <td className="px-3 py-2 text-right tabular-nums">{Number(item.weight ?? 0).toFixed(2)}</td>
                                <td className="px-3 py-2 break-words">{String(item.reason ?? "-")}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>

                  <div>
                    <h4 className="text-sm font-semibold mb-2">주문 게이트</h4>
                    <div className="border border-slate-200 dark:border-slate-800 rounded-md overflow-x-auto">
                      <table className="w-full min-w-[520px] text-sm">
                        <thead className="bg-slate-100 dark:bg-slate-800">
                          <tr>
                            <th className="text-left px-3 py-2">종목</th>
                            <th className="text-left px-3 py-2">신호</th>
                            <th className="text-right px-3 py-2">강도</th>
                            <th className="text-left px-3 py-2">상태</th>
                            <th className="text-left px-3 py-2">이유</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                          {(run.order_decisions ?? []).map((item, index) => (
                            <tr key={`${orderSymbol(item)}-${index}`}>
                              <td className="px-3 py-2">{String(item.name ?? orderSymbol(item))}</td>
                              <td className="px-3 py-2">{String(item.action ?? "-")}</td>
                              <td className="px-3 py-2 text-right tabular-nums">{Number(item.strength ?? 0).toFixed(2)}</td>
                              <td className="px-3 py-2">{String(item.status ?? "-")}</td>
                              <td className="px-3 py-2 break-words">{textList(item.reasons)}</td>
                            </tr>
                          ))}
                          {!run.order_decisions?.length && (
                            <tr><td colSpan={5} className="px-3 py-5 text-center text-slate-500">주문 게이트 기록 없음</td></tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              )}

              <div className="grid lg:grid-cols-2 gap-6">
                <div>
                  <h4 className="text-sm font-semibold mb-2">주문 결과</h4>
                  <div className="border border-slate-200 dark:border-slate-800 rounded-md overflow-x-auto">
                    <table className="w-full min-w-[540px] text-sm">
                      <thead className="bg-slate-100 dark:bg-slate-800">
                        <tr>
                          <th className="text-left px-3 py-2">종목</th>
                          <th className="text-left px-3 py-2">방향</th>
                          <th className="text-right px-3 py-2">수량</th>
                          <th className="text-right px-3 py-2">금액</th>
                          <th className="text-left px-3 py-2">상태</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                        {[...(run.submitted_sells ?? []), ...(run.orders ?? [])].map((item, index) => (
                          <tr key={`${orderSymbol(item)}-${index}`}>
                            <td className="px-3 py-2">{orderSymbol(item)}</td>
                            <td className="px-3 py-2">{String(item.action ?? "-")}</td>
                            <td className="px-3 py-2 text-right">{String(item.quantity ?? 0)}</td>
                            <td className="px-3 py-2 text-right tabular-nums">{money.format(orderNotional(item))}</td>
                            <td className="px-3 py-2">{String(item.order_status ?? item.status ?? "-")}</td>
                          </tr>
                        ))}
                        {!run.orders?.length && !run.submitted_sells?.length && (
                          <tr><td colSpan={5} className="px-3 py-5 text-center text-slate-500">제출 주문 없음</td></tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>

                <div>
                  <h4 className="text-sm font-semibold mb-2">오류</h4>
                  <div className="border border-slate-200 dark:border-slate-800 rounded-md min-h-28 p-3">
                    {run.errors?.length ? (
                      <ul className="space-y-2 text-sm text-red-700 dark:text-red-300">
                        {run.errors.map((item, index) => <li key={index} className="break-words">{item}</li>)}
                      </ul>
                    ) : (
                      <p className="text-sm text-slate-500">기록된 오류 없음</p>
                    )}
                  </div>
                </div>
              </div>
            </section>
          )}
            </>
          )}

          {view === "record" && (
            <div className="space-y-8">
              <section className="space-y-4" aria-label="월간 기록">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-subheading">월간 기록</h3>
                    <p className="text-caption text-slate-500 mt-1">자동매매 일일 리포트 합계</p>
                  </div>
                  <select
                    aria-label="월 선택"
                    value={selectedMonth}
                    onChange={(event) => {
                      setMonthlyRecord(null);
                      setSelectedMonth(event.target.value);
                    }}
                    className="h-10 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 rounded-md px-3 text-sm"
                  >
                    {monthOptions.map((month) => (
                      <option key={month} value={month}>
                        {monthLabel(month)}
                      </option>
                    ))}
                  </select>
                </div>

                {monthlyRecord && monthlyRecord.month === selectedMonth ? (
                  <>
                    <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
                      {[
                        ["월 손익 합계", signedMoney(money, monthlyRecord.summary.pnl), valueTone(monthlyRecord.summary.pnl)],
                        ["계좌 평가 변화", signedMoney(money, monthlyRecord.summary.account_pnl), valueTone(monthlyRecord.summary.account_pnl)],
                        ["승/패/보합", `${monthlyRecord.summary.win_days}/${monthlyRecord.summary.loss_days}/${monthlyRecord.summary.flat_days}`, ""],
                        ["누적 매수", money.format(monthlyRecord.summary.buy_notional), ""],
                        ["누적 매도", money.format(monthlyRecord.summary.sell_notional), ""],
                        ["데이터 이상", `${monthlyRecord.summary.anomaly_days}일`, monthlyRecord.summary.anomaly_days ? "text-amber-700 dark:text-amber-300" : ""],
                      ].map(([label, value, tone]) => (
                        <div key={label} className="card min-w-0">
                          <span className="text-caption text-slate-500">{label}</span>
                          <strong className={`block text-lg mt-1 break-words ${tone}`}>{value}</strong>
                        </div>
                      ))}
                    </div>

                    <section className="border-y border-slate-200 dark:border-slate-800">
                      <div className="py-4 flex flex-wrap items-center justify-between gap-2">
                        <h4 className="text-sm font-semibold">{monthLabel(monthlyRecord.month)} 일별 손익</h4>
                        <span className="text-caption text-slate-500">
                          {monthlyRecord.summary.day_count}일 · 계좌 수익률 {signedPercent(monthlyRecord.summary.account_pnl_pct)}
                        </span>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full min-w-[980px] text-sm">
                          <thead className="bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
                            <tr>
                              <th className="text-left px-3 py-2">날짜</th>
                              <th className="text-right px-3 py-2">평가손익</th>
                              <th className="text-right px-3 py-2">수익률</th>
                              <th className="text-right px-3 py-2">평가액</th>
                              <th className="text-right px-3 py-2">매수</th>
                              <th className="text-right px-3 py-2">매도</th>
                              <th className="text-right px-3 py-2">현금 변화</th>
                              <th className="text-right px-3 py-2">실행/오류</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                            {monthlyRecord.days.map((day) => (
                              <tr
                                key={day.session_date}
                                className={
                                  day.session_date === session.session_date
                                    ? "bg-slate-50 dark:bg-slate-900/70"
                                    : "hover:bg-slate-50 dark:hover:bg-slate-900/60"
                                }
                              >
                                <td className="px-3 py-3 font-medium">
                                  <button
                                    type="button"
                                    onClick={() => void selectMonthlyDay(day.session_date)}
                                    className="hover:text-primary"
                                  >
                                    {dayLabel(day.date)}
                                  </button>
                                </td>
                                <td className={`px-3 py-3 text-right tabular-nums ${valueTone(day.pnl)}`}>
                                  {day.valid ? signedMoney(money, day.pnl) : "데이터 이상"}
                                </td>
                                <td className={`px-3 py-3 text-right tabular-nums ${valueTone(day.pnl_pct)}`}>
                                  {day.valid ? signedPercent(day.pnl_pct) : "-"}
                                </td>
                                <td className="px-3 py-3 text-right tabular-nums">
                                  {day.valid
                                    ? `${money.format(day.start_equity)} → ${money.format(day.end_equity)}`
                                    : "-"}
                                </td>
                                <td className="px-3 py-3 text-right tabular-nums">{money.format(day.buy_notional)}</td>
                                <td className="px-3 py-3 text-right tabular-nums">{money.format(day.sell_notional)}</td>
                                <td className={`px-3 py-3 text-right tabular-nums ${valueTone(day.cash_delta)}`}>
                                  {signedMoney(money, day.cash_delta)}
                                </td>
                                <td className="px-3 py-3 text-right tabular-nums">
                                  {day.run_count}/{day.error_count}
                                </td>
                              </tr>
                            ))}
                            {!monthlyRecord.days.length && (
                              <tr>
                                <td colSpan={8} className="px-3 py-8 text-center text-slate-500">
                                  이 달의 기록이 없습니다.
                                </td>
                              </tr>
                            )}
                          </tbody>
                        </table>
                      </div>
                    </section>
                  </>
                ) : (
                  <div className="border border-slate-200 dark:border-slate-800 rounded-md p-6 text-center text-slate-500">
                    {monthlyLoading ? "월간 기록을 불러오는 중입니다." : "이 달의 기록이 없습니다."}
                  </div>
                )}
              </section>

              {dailyRecord ? (
              <section className="space-y-6" aria-label="일일 기록">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-subheading">선택일 기록</h3>
                  <span className={`text-caption ${dailyRecord.valid ? "text-slate-500" : "text-amber-700 dark:text-amber-300"}`}>
                    {dayLabel(sessionDateToIso(session.session_date))}
                    {dailyRecord.valid ? "" : " · 데이터 이상"}
                  </span>
                </div>
                {!dailyRecord.valid && (
                  <div className="border border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300 rounded-md p-3 text-sm">
                    계좌 스냅샷이 비정상적으로 튀어 이 날짜는 월간 손익 합계에서 제외했습니다.
                  </div>
                )}
                <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
                  {[
                    ["일일 평가손익", dailyRecord.valid ? signedMoney(money, dailyRecord.pnl) : "데이터 이상", dailyRecord.valid ? valueTone(dailyRecord.pnl) : "text-amber-700 dark:text-amber-300"],
                    ["수익률", dailyRecord.valid ? signedPercent(dailyRecord.pnl_pct) : "-", dailyRecord.valid ? valueTone(dailyRecord.pnl_pct) : ""],
                    ["평가액", dailyRecord.valid ? `${money.format(dailyRecord.start_equity)} → ${money.format(dailyRecord.end_equity)}` : "-", ""],
                    ["현금 변화", signedMoney(money, dailyRecord.cash_delta), valueTone(dailyRecord.cash_delta)],
                    ["보유평가 변화", signedMoney(money, dailyRecord.holdings_value_delta), valueTone(dailyRecord.holdings_value_delta)],
                    ["순거래 현금흐름", signedMoney(money, dailyRecord.net_trade_cashflow), valueTone(dailyRecord.net_trade_cashflow)],
                  ].map(([label, value, tone]) => (
                    <div key={label} className="card min-w-0">
                      <span className="text-caption text-slate-500">{label}</span>
                      <strong className={`block text-lg mt-1 break-words ${tone}`}>{value}</strong>
                    </div>
                  ))}
                </div>

                <div className="grid lg:grid-cols-4 gap-3">
                  {[
                    ["시작 현금", money.format(dailyRecord.start_cash)],
                    ["매수", signedMoney(money, -dailyRecord.buy_notional)],
                    ["매도", signedMoney(money, dailyRecord.sell_notional)],
                    ["종료 현금", money.format(dailyRecord.end_cash)],
                  ].map(([label, value]) => (
                    <div
                      key={label}
                      className="border-y border-slate-200 dark:border-slate-800 py-4 min-w-0"
                    >
                      <span className="text-caption text-slate-500">{label}</span>
                      <strong className="block text-xl mt-1 break-words">{value}</strong>
                    </div>
                  ))}
                </div>

                <section className="border-y border-slate-200 dark:border-slate-800">
                  <div className="py-4 flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-subheading">돈의 흐름</h3>
                    <span className="text-caption text-slate-500">
                      리포트 스냅샷 기준 추정치 · 차이 {signedMoney(money, dailyRecord.cash_reconciliation_delta)}
                    </span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[860px] text-sm">
                      <thead className="bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
                        <tr>
                          <th className="text-left px-3 py-2">{config.timeLabel}</th>
                          <th className="text-right px-3 py-2">평가액</th>
                          <th className="text-right px-3 py-2">현금</th>
                          <th className="text-right px-3 py-2">보유평가</th>
                          <th className="text-right px-3 py-2">매수</th>
                          <th className="text-right px-3 py-2">매도</th>
                          <th className="text-right px-3 py-2">순거래</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                        {dailyRecord.points.map((point) => (
                          <tr key={`${point.run_id}-${point.time}`} className="hover:bg-slate-50 dark:hover:bg-slate-900/60">
                            <td className="px-3 py-3 font-medium">
                              <button type="button" onClick={() => void selectRun(point.run_id)} className="hover:text-primary">
                                {marketTime(point.time, market)}
                              </button>
                            </td>
                            <td className="px-3 py-3 text-right tabular-nums">{money.format(point.equity)}</td>
                            <td className="px-3 py-3 text-right tabular-nums">{money.format(point.cash)}</td>
                            <td className="px-3 py-3 text-right tabular-nums">{money.format(point.holdings_value)}</td>
                            <td className="px-3 py-3 text-right tabular-nums">{money.format(point.buy_notional)}</td>
                            <td className="px-3 py-3 text-right tabular-nums">{money.format(point.sell_notional)}</td>
                            <td className={`px-3 py-3 text-right tabular-nums ${valueTone(point.net_trade_cashflow)}`}>
                              {signedMoney(money, point.net_trade_cashflow)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              </section>
            ) : (
              <div className="border border-slate-200 dark:border-slate-800 rounded-md p-8 text-center text-slate-500">
                기록 가능한 계좌 스냅샷이 없습니다.
              </div>
              )}
            </div>
          )}
        </>
      )}

      {!loading && !session && !error && (
        <div className="border border-slate-200 dark:border-slate-800 rounded-md p-8 text-center text-slate-500">
          {config.empty}
        </div>
      )}
    </div>
  );
}
