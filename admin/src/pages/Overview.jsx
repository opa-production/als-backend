import { useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  Building2,
  CheckCircle2,
  Info,
  Users,
  Wallet,
} from "lucide-react";

import { BarList, Funnel, TimeSeries } from "../components/charts.jsx";
import { Badge, Card, ErrorState, Stat, Table } from "../components/ui.jsx";
import { useApi } from "../lib/useApi.js";
import { compact, money, moneyCompact, number, percent } from "../lib/format.js";

const RANGES = [
  { value: 7, label: "7d" },
  { value: 30, label: "30d" },
  { value: 90, label: "90d" },
];

const LEVEL_ICON = { critical: AlertTriangle, warn: AlertTriangle, info: Info };

export default function Overview() {
  const [range, setRange] = useState(30);

  const overview = useApi("/overview");
  const revenueSeries = useApi("/overview/timeseries", { metric: "revenue", days: range });
  const signupSeries = useApi("/overview/timeseries", { metric: "signups", days: range });
  const institutions = useApi("/overview/institutions", { limit: 8 });

  if (overview.error) return <ErrorState error={overview.error} onRetry={overview.reload} />;

  const data = overview.data;
  const revenue = data?.revenue;
  const users = data?.users;
  const funnel = data?.funnel;

  return (
    <div className="stack-24 content-narrow">
      {/* Filters sit above everything they scope — one row, never inside a
          chart card. A control in one card that silently repaints another is
          how a dashboard stops being believed. */}
      <div className="row-between">
        <div className="row" style={{ gap: 8 }}>
          {data?.attention?.length === 0 && (
            <span className="row" style={{ gap: 6, fontSize: 13, color: "var(--good-text)" }}>
              <CheckCircle2 size={15} strokeWidth={2} />
              Nothing needs attention.
            </span>
          )}
        </div>
        <RangePicker value={range} onChange={setRange} />
      </div>

      {data?.attention?.length > 0 && (
        <div className="stack-8">
          {data.attention.map((item) => {
            const Icon = LEVEL_ICON[item.level] ?? Info;
            return (
              <div className={`attention ${item.level}`} key={item.code}>
                <Icon size={15} strokeWidth={2} />
                <span>{item.message}</span>
                {item.link && (
                  <Link className="attention-link" to={item.link}>
                    Open
                    <ArrowRight size={13} strokeWidth={2.2} />
                  </Link>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className={`grid grid-4 ${overview.refetching ? "is-refetching" : ""}`}>
        <Stat
          icon={Users}
          label="Students"
          value={overview.loading ? "—" : number(users.total)}
          deltaLabel={overview.loading ? "" : `${number(users.new_30d)} joined in 30 days`}
        />
        <Stat
          icon={Wallet}
          label="Paying customers"
          value={overview.loading ? "—" : number(revenue.paying_customers)}
          deltaLabel={
            overview.loading ? "" : `${percent(funnel.trial_conversion_pct)} of finished trials`
          }
        />
        <Stat
          label="Monthly recurring revenue"
          value={overview.loading ? "—" : money(revenue.mrr_ksh)}
          deltaLabel={overview.loading ? "" : `${money(revenue.arpu_ksh)} per paying customer`}
        />
        <Stat
          label="Revenue, last 30 days"
          value={overview.loading ? "—" : money(revenue.last_30d_ksh)}
          delta={revenue?.growth_30d_pct}
          deltaLabel="vs the 30 days before"
        />
      </div>

      <div className="split-main">
        <Card
          title="Revenue"
          note={`Successful charges per day, last ${range} days. Empty days are drawn, not skipped.`}
        >
          <div className={revenueSeries.refetching ? "is-refetching" : ""}>
            {revenueSeries.loading ? (
              <div className="skeleton" style={{ height: 210 }} />
            ) : (
              <TimeSeries
                points={revenueSeries.data.points}
                variant="area"
                valueLabel="Revenue"
                formatValue={money}
                formatAxis={(value) => moneyCompact(value).replace("KES ", "")}
              />
            )}
          </div>
        </Card>

        <Card title="Funnel" note="Signup through to a paying account.">
          {overview.loading ? (
            <div className="skeleton" style={{ height: 210 }} />
          ) : (
            <>
              <Funnel
                formatValue={number}
                stages={[
                  { label: "Signed up", value: funnel.signed_up },
                  { label: "Started a trial", value: funnel.started_trial },
                  { label: "Trial finished", value: funnel.trial_expired },
                  { label: "Paid at least once", value: funnel.ever_paid },
                  { label: "Paying now", value: funnel.paying_now },
                ]}
              />
              <p className="section-note" style={{ marginTop: 16 }}>
                Conversion counts only students whose trial has actually run out —
                anyone still inside their fourteen days has not decided yet, and
                counting them as a "no" makes a good week of signups look like a
                drop.
              </p>
            </>
          )}
        </Card>
      </div>

      <Card
        title="Plan performance"
        note="Seats are people; MRR is money. For Friends the two do not divide into each other — one payment, up to five seats."
        flush
      >
        <Table
          loading={overview.loading}
          rows={data?.plans ?? []}
          rowKey={(row) => row.tier}
          columns={[
            {
              key: "name",
              header: "Plan",
              render: (row) => (
                <div>
                  <div className="cell-primary">{row.name}</div>
                  <div className="cell-sub">
                    {row.price_ksh ? `${money(row.price_ksh)} / 30 days` : "Free"}
                  </div>
                </div>
              ),
            },
            {
              key: "active",
              header: "Active",
              align: "right",
              render: (row) => number(row.active),
            },
            {
              key: "paying",
              header: "Paying",
              align: "right",
              render: (row) =>
                row.tier === "trial" ? <span className="muted">—</span> : number(row.paying),
            },
            {
              key: "unverified",
              header: "Unverified",
              align: "right",
              render: (row) =>
                row.unverified ? (
                  <Badge tone="danger">{row.unverified}</Badge>
                ) : (
                  <span className="muted">0</span>
                ),
            },
            {
              key: "expiring",
              header: "Expiring 7d",
              align: "right",
              render: (row) => number(row.expiring_7d),
            },
            {
              key: "mrr",
              header: "MRR",
              align: "right",
              render: (row) => money(row.mrr_ksh),
            },
            {
              key: "rev30",
              header: "Revenue 30d",
              align: "right",
              render: (row) => money(row.revenue_30d_ksh),
            },
          ]}
        />
      </Card>

      <div className="grid grid-2">
        <Card title="New students" note={`Signups per day, last ${range} days.`}>
          <div className={signupSeries.refetching ? "is-refetching" : ""}>
            {signupSeries.loading ? (
              <div className="skeleton" style={{ height: 210 }} />
            ) : (
              <TimeSeries
                points={signupSeries.data.points}
                variant="column"
                valueLabel="Signups"
                formatValue={number}
                formatAxis={compact}
              />
            )}
          </div>
        </Card>

        <Card
          title="Where students are"
          note="Blank institutions are left out — a bar labelled 'did not answer' tells you about the form, not the market."
          actions={<Building2 size={15} strokeWidth={1.9} className="muted" />}
        >
          {institutions.loading ? (
            <div className="skeleton" style={{ height: 210 }} />
          ) : (
            <BarList
              formatValue={number}
              items={(institutions.data ?? []).map((row) => ({
                label: row.institution,
                value: row.users,
              }))}
            />
          )}
        </Card>
      </div>
    </div>
  );
}

function RangePicker({ value, onChange }) {
  return (
    <div className="btn-group" role="group" aria-label="Time range">
      {RANGES.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? "is-active" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
