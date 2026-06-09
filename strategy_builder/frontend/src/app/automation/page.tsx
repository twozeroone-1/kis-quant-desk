"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Download, RefreshCw } from "lucide-react";
import { useAuth } from "@/hooks";
import {
  getAutomationRun,
  getAutomationSession,
  getAutomationSessions,
  type AutomationMarket,
  type AutomationRunDetail,
  type AutomationSession,
} from "@/lib/api";

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

function orderSymbol(item: Record<string, unknown>) {
  return String(item.symbol ?? item.code ?? item.stock_code ?? "-");
}

export default function AutomationPage() {
  const { status } = useAuth();
  const [market, setMarket] = useState<AutomationMarket>("us");
  const [sessions, setSessions] = useState<AutomationSession[]>([]);
  const [session, setSession] = useState<AutomationSession | null>(null);
  const [run, setRun] = useState<AutomationRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const money = moneyFormatters[marketConfig[market].currency];
  const config = marketConfig[market];
  const latestCash = Number(session?.latest_account?.cash ?? 0);

  const loadSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getAutomationSessions(market);
      setSessions(response.sessions);
      if (response.sessions.length) {
        const selected = await getAutomationSession(market, response.sessions[0].session_date);
        setSession(selected.data);
      } else {
        setSession(null);
      }
      setRun(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "자동매매 리포트를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, [market]);

  useEffect(() => {
    if (status.mode === "vps") void loadSessions();
  }, [loadSessions, status.mode]);

  const selectMarket = (nextMarket: AutomationMarket) => {
    if (nextMarket === market) return;
    setMarket(nextMarket);
    setSessions([]);
    setSession(null);
    setRun(null);
  };

  const selectSession = async (sessionDate: string) => {
    setError(null);
    setRun(null);
    try {
      const response = await getAutomationSession(market, sessionDate);
      setSession(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "세션을 불러오지 못했습니다.");
    }
  };

  const selectRun = async (runId: string) => {
    setError(null);
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

      {error && (
        <div className="flex items-start gap-2 border border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300 rounded-md p-3 text-sm">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span className="break-words">{error}</span>
        </div>
      )}

      {session && (
        <>
          <section className="grid grid-cols-2 lg:grid-cols-6 gap-3" aria-label="세션 요약">
            {[
              ["실행", `${session.run_count}회`],
              ["누적 매수", money.format(session.cumulative_buy_notional)],
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
              <table className="w-full min-w-[820px] text-sm">
                <thead className="bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
                  <tr>
                    <th className="text-left px-3 py-2">{config.timeLabel}</th>
                    <th className="text-left px-3 py-2">상태</th>
                    <th className="text-right px-3 py-2">BUY/SELL/HOLD</th>
                    <th className="text-right px-3 py-2">제출/체결/실패</th>
                    <th className="text-right px-3 py-2">매수금액</th>
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
                        <td className="px-3 py-3 text-right tabular-nums">{money.format(item.buy_notional)}</td>
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

              <div className="grid lg:grid-cols-2 gap-6">
                <div>
                  <h4 className="text-sm font-semibold mb-2">주문 결과</h4>
                  <div className="border border-slate-200 dark:border-slate-800 rounded-md overflow-x-auto">
                    <table className="w-full min-w-[440px] text-sm">
                      <thead className="bg-slate-100 dark:bg-slate-800">
                        <tr>
                          <th className="text-left px-3 py-2">종목</th>
                          <th className="text-left px-3 py-2">방향</th>
                          <th className="text-right px-3 py-2">수량</th>
                          <th className="text-left px-3 py-2">상태</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                        {[...(run.submitted_sells ?? []), ...(run.orders ?? [])].map((item, index) => (
                          <tr key={`${orderSymbol(item)}-${index}`}>
                            <td className="px-3 py-2">{orderSymbol(item)}</td>
                            <td className="px-3 py-2">{String(item.action ?? "-")}</td>
                            <td className="px-3 py-2 text-right">{String(item.quantity ?? 0)}</td>
                            <td className="px-3 py-2">{String(item.order_status ?? item.status ?? "-")}</td>
                          </tr>
                        ))}
                        {!run.orders?.length && !run.submitted_sells?.length && (
                          <tr><td colSpan={4} className="px-3 py-5 text-center text-slate-500">제출 주문 없음</td></tr>
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

      {!loading && !session && !error && (
        <div className="border border-slate-200 dark:border-slate-800 rounded-md p-8 text-center text-slate-500">
          {config.empty}
        </div>
      )}
    </div>
  );
}
