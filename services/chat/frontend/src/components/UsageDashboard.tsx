import { useCallback, useEffect, useState } from "react";
import {
  AccountUsage,
  ActiveBlockUsage,
  RollingUsage,
  TodayUsage,
  UsagePayload,
  UsageWindow,
  getUsage,
} from "../api";

const POLL_MS = 30000;

export interface UsageDashboardProps {
  onClose: () => void;
  visible: boolean;
}

export function UsageDashboard({ onClose, visible }: UsageDashboardProps): JSX.Element {
  const [data, setData] = useState<UsagePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await getUsage();
      setData(d);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    refresh();
    const t = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(t);
  }, [visible, refresh]);

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <button
          type="button"
          className="dashboard-back"
          onClick={onClose}
          aria-label="Close dashboard"
          title="Close"
        >
          ‹
        </button>
        <h1 className="dashboard-title">Usage</h1>
      </header>

      <div className="dashboard-body">
        {error && <div className="dashboard-error">{error}</div>}
        {!data && !error && <div className="dashboard-empty">Loading…</div>}

        {data && (
          <>
            <section className="dashboard-section">
              <h2 className="dashboard-section-title">Plan quota</h2>
              {data.accounts.length === 0 ? (
                <div className="dashboard-empty">No accounts.</div>
              ) : (
                data.accounts.map((acct) => (
                  <AccountQuota key={acct.name} account={acct} />
                ))
              )}
            </section>

            <section className="dashboard-section">
              <h2 className="dashboard-section-title">Today (UTC)</h2>
              <TodaySection today={data.today_utc} />
            </section>

            {data.active_block && (
              <section className="dashboard-section">
                <h2 className="dashboard-section-title">Active 5h block</h2>
                <ActiveBlockSection block={data.active_block} />
              </section>
            )}

            <section className="dashboard-section">
              <h2 className="dashboard-section-title">5-day rolling</h2>
              <RollingSection rolling={data.rolling_5d} />
            </section>
          </>
        )}
      </div>
    </div>
  );
}

function AccountQuota({ account }: { account: AccountUsage }): JSX.Element {
  return (
    <div className="account-quota">
      <div className="account-name">
        {account.name}
        {account.is_active && <span className="account-active"> ← active</span>}
        {!account.available && <span className="account-unavailable"> (unavailable)</span>}
      </div>
      {account.error && <div className="dashboard-error">{account.error}</div>}
      {account.windows.map((w) => (
        <UsageBar key={w.key} window={w} />
      ))}
      {account.extra_usage && (
        <div className="extra-usage">
          {formatCurrency(account.extra_usage.used_credits, account.extra_usage.currency)} /{" "}
          {formatCurrency(account.extra_usage.monthly_limit, account.extra_usage.currency)}
        </div>
      )}
    </div>
  );
}

function UsageBar({ window: w }: { window: UsageWindow }): JSX.Element {
  const pct = Math.max(0, Math.min(100, w.utilization));
  return (
    <div className="usage-bar">
      <div className="usage-bar-label">
        <span className="usage-bar-name">{w.label}</span>
        <span className="usage-bar-pct">{pct.toFixed(1)}%</span>
        {w.resets_in_minutes != null && (
          <span className="usage-bar-resets">resets in {formatMinutes(w.resets_in_minutes)}</span>
        )}
      </div>
      <div className="usage-bar-track">
        <div
          className={`usage-bar-fill ${pct >= 90 ? "is-hot" : pct >= 70 ? "is-warm" : ""}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function TodaySection({ today }: { today: TodayUsage }): JSX.Element {
  return (
    <div>
      <div className="usage-line">
        <span className="usage-line-label">{today.date ?? "today"}</span>
        <span className="usage-line-value">{formatCurrency(today.total_cost_usd, "USD")}</span>
      </div>
      {today.models.length > 0 && (
        <table className="usage-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Total</th>
              <th>Input</th>
              <th>Output</th>
              <th>Cache write</th>
              <th>Cache read</th>
            </tr>
          </thead>
          <tbody>
            {today.models.map((m) => (
              <tr key={m.model}>
                <td className="mono">
                  {m.model}
                  {!m.is_known_pricing && <span className="usage-pricing-warn"> *</span>}
                </td>
                <td>{formatCurrency(m.cost_usd, "USD")}</td>
                <td>{formatCurrency(m.input_cost_usd, "USD")}</td>
                <td>{formatCurrency(m.output_cost_usd, "USD")}</td>
                <td>{formatCurrency(m.cache_write_cost_usd, "USD")}</td>
                <td>{formatCurrency(m.cache_read_cost_usd, "USD")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ActiveBlockSection({ block }: { block: ActiveBlockUsage }): JSX.Element {
  return (
    <div className="active-block">
      <div className="usage-line">
        <span className="usage-line-label">Cost so far</span>
        <span className="usage-line-value">{formatCurrency(block.cost_usd_so_far, "USD")}</span>
      </div>
      {block.tokens_per_minute != null && (
        <div className="usage-line">
          <span className="usage-line-label">Burn rate</span>
          <span className="usage-line-value">
            {formatNumber(Math.round(block.tokens_per_minute))} tok/min
            {block.cost_per_hour != null && (
              <> · {formatCurrency(block.cost_per_hour, "USD")}/h</>
            )}
          </span>
        </div>
      )}
      {block.projected_cost_usd != null && (
        <div className="usage-line">
          <span className="usage-line-label">Projected total</span>
          <span className="usage-line-value">
            {formatCurrency(block.projected_cost_usd, "USD")}
            {block.projected_tokens != null && (
              <> · {formatNumber(block.projected_tokens)} tok</>
            )}
          </span>
        </div>
      )}
      <div className="usage-line">
        <span className="usage-line-label">Time remaining</span>
        <span className="usage-line-value">{formatMinutes(block.remaining_minutes)}</span>
      </div>
    </div>
  );
}

function RollingSection({ rolling }: { rolling: RollingUsage }): JSX.Element {
  return (
    <div>
      <div className="usage-line">
        <span className="usage-line-label">Cost ({rolling.days}d)</span>
        <span className="usage-line-value">{formatCurrency(rolling.cost_usd, "USD")}</span>
      </div>
      <div className="usage-line">
        <span className="usage-line-label">Tokens ({rolling.days}d)</span>
        <span className="usage-line-value">{formatNumber(rolling.tokens)}</span>
      </div>
    </div>
  );
}

function formatMinutes(mins: number): string {
  if (mins < 0) return "0m";
  const h = Math.floor(mins / 60);
  const m = Math.floor(mins % 60);
  if (h === 0) return `${m}m`;
  return `${h}h ${m}m`;
}

function formatCurrency(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat(undefined).format(value);
}
