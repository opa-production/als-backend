import { AlertTriangle, CheckCircle2, Database, Gauge, XCircle } from "lucide-react";

import { Badge, Card, DefinitionList, ErrorState, Table } from "../components/ui.jsx";
import { useApi } from "../lib/useApi.js";
import { humanise, money, number } from "../lib/format.js";

export default function Ops() {
  const health = useApi("/ops/health");
  const plans = useApi("/ops/plans");

  if (health.error) return <ErrorState error={health.error} onRetry={health.reload} />;

  const data = health.data;

  return (
    <div className="stack-24 content-narrow">
      {health.loading ? (
        <div className="skeleton" style={{ height: 120, borderRadius: "var(--radius)" }} />
      ) : (
        <>
          {data.warnings.length > 0 && (
            <div className="stack-8">
              {data.warnings.map((warning) => (
                <div className="attention warn" key={warning}>
                  <AlertTriangle size={15} strokeWidth={2} />
                  <span>{warning}</span>
                </div>
              ))}
            </div>
          )}

          <div className="grid grid-2">
            <Card
              title="Service"
              note="This one does query the database — unlike /health, which deliberately does not."
              actions={
                data.database_ok ? (
                  <Badge tone="good">Answering</Badge>
                ) : (
                  <Badge tone="danger">Not answering</Badge>
                )
              }
            >
              <DefinitionList
                items={[
                  { label: "Environment", value: <span className="mono">{data.environment}</span> },
                  {
                    label: "Database",
                    value: (
                      <span className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                        <Database size={13} strokeWidth={1.9} className="muted" />
                        {data.database_ok ? "Connected" : "Unreachable"}
                      </span>
                    ),
                  },
                  {
                    label: "Round trip",
                    value: (
                      <span className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                        <Gauge size={13} strokeWidth={1.9} className="muted" />
                        {data.database_latency_ms} ms
                      </span>
                    ),
                  },
                ]}
              />
            </Card>

            <Card
              title="Integrations"
              note="A dashboard showing revenue while Kora has no credentials is showing history from somewhere else."
            >
              <div className="stack-8">
                {Object.entries(data.integrations).map(([name, configured]) => (
                  <div className="row-between" key={name} style={{ fontSize: 13 }}>
                    <span>{humanise(name)}</span>
                    <span
                      className="row"
                      style={{
                        gap: 6,
                        color: configured ? "var(--good-text)" : "var(--text-3)",
                        fontWeight: 500,
                      }}
                    >
                      {configured ? (
                        <CheckCircle2 size={14} strokeWidth={2.1} />
                      ) : (
                        <XCircle size={14} strokeWidth={2.1} />
                      )}
                      {configured ? "Configured" : "No credentials"}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <Card title="Row counts" note="What is actually in the database right now.">
            <div className="grid grid-4" style={{ gap: 12 }}>
              {Object.entries(data.counts).map(([name, count]) => (
                <div key={name}>
                  <div style={{ fontSize: 12.5, color: "var(--text-3)" }}>{humanise(name)}</div>
                  <div
                    className="tabular"
                    style={{ fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}
                  >
                    {number(count)}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}

      <Card
        title="Plan catalogue, as the server sees it"
        note="The mobile app ships its own copy of these numbers. When a student insists their plan allows something the API refuses, this table is the authoritative side of the argument."
        flush
      >
        <Table
          loading={plans.loading}
          rows={plans.data ?? []}
          rowKey={(row) => row.id}
          columns={[
            {
              key: "name",
              header: "Plan",
              render: (row) => (
                <div>
                  <div className="cell-primary">{row.name}</div>
                  <div className="cell-sub mono">{row.id}</div>
                </div>
              ),
            },
            {
              key: "price",
              header: "Price",
              align: "right",
              render: (row) => (row.price_ksh ? money(row.price_ksh) : <span className="muted">Free</span>),
            },
            {
              key: "seat",
              header: "Per seat",
              align: "right",
              render: (row) =>
                row.seats > 1 ? money(row.price_per_seat_ksh) : <span className="muted">—</span>,
            },
            { key: "days", header: "Days", align: "right", render: (row) => row.duration_days },
            { key: "units", header: "Units", align: "right", render: (row) => row.limits.max_course_units },
            {
              key: "queries",
              header: "AI / day",
              align: "right",
              render: (row) =>
                row.limits.daily_ai_queries === -1 ? "∞" : row.limits.daily_ai_queries,
            },
            {
              key: "pages",
              header: "PDF pages",
              align: "right",
              render: (row) => number(row.limits.total_pdf_pages_pool),
            },
            {
              key: "ocr",
              header: "OCR",
              render: (row) =>
                row.limits.allow_ocr_scans ? (
                  <Badge tone="good">{row.limits.monthly_ocr_page_limit} / month</Badge>
                ) : (
                  <span className="muted">Not included</span>
                ),
            },
            {
              key: "citations",
              header: "Citations",
              render: (row) => <span className="dim">{humanise(row.limits.source_citations)}</span>,
            },
          ]}
        />
      </Card>
    </div>
  );
}
