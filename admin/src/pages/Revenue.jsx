import { useState } from "react";
import { Link } from "react-router-dom";
import { CreditCard, Percent, TrendingUp, Wallet } from "lucide-react";

import { BarList, ShareBar, TimeSeries } from "../components/charts.jsx";
import { Card, ErrorState, Stat, Table } from "../components/ui.jsx";
import { useApi } from "../lib/useApi.js";
import { humanise, money, moneyCompact, number, percent } from "../lib/format.js";

const RANGES = [
  { value: 7, label: "7d" },
  { value: 30, label: "30d" },
  { value: 90, label: "90d" },
  { value: 180, label: "180d" },
];

export default function Revenue() {
  const [range, setRange] = useState(30);
  const [lifetimeWindow, setLifetimeWindow] = useState("");

  const summary = useApi("/revenue/summary");
  const byPlan = useApi("/revenue/by-plan");
  const series = useApi("/revenue/timeseries", { metric: "revenue", days: range });
  const topCustomers = useApi("/revenue/top-customers", {
    limit: 10,
    days: lifetimeWindow || undefined,
  });

  if (summary.error) return <ErrorState error={summary.error} onRetry={summary.reload} />;

  const data = summary.data;

  return (
    <div className="stack-24 content-narrow">
      <div className="row-between">
        <p className="section-note">
          Amounts are whole Kenyan shillings, exactly as stored. Kora charges in the
          major currency unit, so nothing is multiplied or divided anywhere between the
          charge and this page.
        </p>
        <div className="btn-group" role="group" aria-label="Time range">
          {RANGES.map((option) => (
            <button
              key={option.value}
              type="button"
              className={option.value === range ? "is-active" : ""}
              onClick={() => setRange(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-4">
        <Stat
          icon={Wallet}
          label="Revenue, last 30 days"
          value={summary.loading ? "—" : money(data.last_30d_ksh)}
          delta={data?.growth_30d_pct}
          deltaLabel="vs the 30 days before"
        />
        <Stat
          icon={TrendingUp}
          label="Monthly recurring revenue"
          value={summary.loading ? "—" : money(data.mrr_ksh)}
          deltaLabel={summary.loading ? "" : `${number(data.paying_customers)} paying customers`}
        />
        <Stat
          icon={CreditCard}
          label="Average payment"
          value={summary.loading ? "—" : money(data.average_payment_ksh)}
          deltaLabel={summary.loading ? "" : `${number(data.successful_payments)} successful charges`}
        />
        <Stat
          icon={Percent}
          label="Charge success rate"
          value={summary.loading ? "—" : percent(data.success_rate_pct)}
          deltaLabel={
            summary.loading
              ? ""
              : `${number(data.failed_payments)} failed · ${number(data.pending_payments)} pending`
          }
        />
      </div>

      <Card title="Revenue over time" note={`Successful charges per day, last ${range} days.`}>
        <div className={series.refetching ? "is-refetching" : ""}>
          {series.loading ? (
            <div className="skeleton" style={{ height: 240 }} />
          ) : (
            <TimeSeries
              points={series.data.points}
              variant="area"
              height={240}
              valueLabel="Revenue"
              formatValue={money}
              formatAxis={(value) => moneyCompact(value).replace("KES ", "")}
            />
          )}
        </div>
      </Card>

      <div className="grid grid-2">
        <Card
          title="Where the money comes from"
          note="Successful charges, all time, by payment channel."
        >
          {summary.loading ? (
            <div className="skeleton" style={{ height: 140 }} />
          ) : (
            <ShareBar
              formatValue={money}
              items={Object.entries(data.by_channel)
                .map(([channel, total]) => ({ label: humanise(channel), value: total }))
                .sort((a, b) => b.value - a.value)}
            />
          )}
        </Card>

        <Card title="Revenue by plan" note="All-time successful charges.">
          {byPlan.loading ? (
            <div className="skeleton" style={{ height: 140 }} />
          ) : (
            <BarList
              formatValue={money}
              items={byPlan.data
                .filter((row) => row.revenue_all_time_ksh > 0)
                .map((row) => ({ label: row.name, value: row.revenue_all_time_ksh }))
                .sort((a, b) => b.value - a.value)}
            />
          )}
        </Card>
      </div>

      <Card
        title="Plan economics"
        note="Read MRR for Friends carefully — it is counted once per group, not once per seat. One payment of KES 1,250 entitles up to five people."
        flush
      >
        <Table
          loading={byPlan.loading}
          rows={byPlan.data ?? []}
          rowKey={(row) => row.tier}
          columns={[
            {
              key: "plan",
              header: "Plan",
              render: (row) => (
                <div>
                  <div className="cell-primary">{row.name}</div>
                  <div className="cell-sub">
                    {row.price_ksh ? money(row.price_ksh) : "Free"} · 30 days
                  </div>
                </div>
              ),
            },
            { key: "subs", header: "Ever held", align: "right", render: (r) => number(r.subscribers) },
            { key: "active", header: "Active", align: "right", render: (r) => number(r.active) },
            { key: "mrr", header: "MRR", align: "right", render: (r) => money(r.mrr_ksh) },
            {
              key: "rev30",
              header: "Revenue 30d",
              align: "right",
              render: (r) => money(r.revenue_30d_ksh),
            },
            {
              key: "revall",
              header: "Revenue all time",
              align: "right",
              render: (r) => money(r.revenue_all_time_ksh),
            },
          ]}
        />
      </Card>

      <Card
        title="Highest lifetime value"
        note="A short list on a three-price product — worth having because these are the renewals worth an email rather than a dashboard."
        flush
        actions={
          <div className="btn-group" role="group" aria-label="Window">
            {[
              { value: "", label: "All time" },
              { value: 90, label: "90d" },
              { value: 30, label: "30d" },
            ].map((option) => (
              <button
                key={option.label}
                type="button"
                className={String(option.value) === String(lifetimeWindow) ? "is-active" : ""}
                onClick={() => setLifetimeWindow(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        }
      >
        <Table
          loading={topCustomers.loading}
          rows={topCustomers.data ?? []}
          rowKey={(row) => row.user_id}
          columns={[
            {
              key: "name",
              header: "Student",
              render: (row) => (
                <div>
                  <Link to={`/users/${row.user_id}`} className="cell-primary">
                    {row.full_name}
                  </Link>
                  <div className="cell-sub">{row.phone ?? "—"}</div>
                </div>
              ),
            },
            {
              key: "institution",
              header: "Institution",
              render: (row) => <span className="dim">{row.institution || "—"}</span>,
            },
            { key: "count", header: "Charges", align: "right", render: (r) => number(r.payments) },
            {
              key: "total",
              header: "Total paid",
              align: "right",
              render: (r) => <strong>{money(r.total_paid_ksh)}</strong>,
            },
          ]}
        />
      </Card>
    </div>
  );
}
