import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Crown } from "lucide-react";

import { Badge, Card, DefinitionList, ErrorState, Table } from "../components/ui.jsx";
import { useApi } from "../lib/useApi.js";
import { date, money, relative } from "../lib/format.js";
import { SeatPips } from "./Groups.jsx";
import { PLANS } from "../lib/plans.js";

export default function GroupDetail() {
  const { id } = useParams();
  const { data, error, loading, reload } = useApi(`/groups/${id}`);

  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (loading || !data) {
    return <div className="skeleton" style={{ height: 320, borderRadius: "var(--radius)" }} />;
  }

  const perSeat = Math.round(PLANS.friends.price_ksh / Math.max(1, data.seats_taken));

  return (
    <div className="stack-24 content-narrow">
      <Link to="/groups" className="row" style={{ gap: 5, fontSize: 13, color: "var(--text-3)" }}>
        <ArrowLeft size={14} strokeWidth={2} />
        All Friends plans
      </Link>

      <div className="row-between" style={{ alignItems: "flex-start" }}>
        <div>
          <div className="row" style={{ gap: 10 }}>
            <span className="page-title mono">{data.invite_code}</span>
            {data.is_active ? <Badge tone="good">Running</Badge> : <Badge tone="neutral">Ended</Badge>}
          </div>
          <p className="section-note">
            Bought {date(data.created_at)} by{" "}
            <Link to={`/users/${data.owner_id}`}>{data.owner_name}</Link>
          </p>
        </div>
        <div className="row" style={{ gap: 10 }}>
          <SeatPips taken={data.seats_taken} total={data.seats} />
          <span className="tabular dim">
            {data.seats_taken} of {data.seats} seats
          </span>
        </div>
      </div>

      <div className="split-main">
        <Card title="Seats" note="The owner holds one of the five." flush>
          <Table
            rows={data.members}
            rowKey={(row) => row.user_id}
            columns={[
              {
                key: "name",
                header: "Student",
                render: (row) => (
                  <div className="row" style={{ gap: 7 }}>
                    <Link to={`/users/${row.user_id}`} className="cell-primary">
                      {row.full_name}
                    </Link>
                    {row.is_owner && (
                      <Crown size={13} strokeWidth={2} style={{ color: "var(--warn)" }} />
                    )}
                  </div>
                ),
              },
              {
                key: "phone",
                header: "Phone",
                render: (row) => <span className="dim">{row.phone ?? "—"}</span>,
              },
              {
                key: "role",
                header: "Role",
                render: (row) =>
                  row.is_owner ? <Badge tone="info">Payer</Badge> : <Badge tone="neutral">Seat</Badge>,
              },
              {
                key: "joined",
                header: "Joined",
                align: "right",
                render: (row) => <span className="dim">{date(row.joined_at)}</span>,
              },
            ]}
          />
        </Card>

        <div className="stack-16">
          <Card title="The plan">
            <DefinitionList
              items={[
                { label: "Paid once", value: money(PLANS.friends.price_ksh) },
                { label: "Seats sold", value: `${data.seats}` },
                { label: "Seats taken", value: `${data.seats_taken}` },
                {
                  label: "Effective per head",
                  value: <span title="What each seated person is costing">{money(perSeat)}</span>,
                },
                {
                  label: "Ends",
                  value: (
                    <span>
                      {date(data.expires_at)} <span className="muted">({relative(data.expires_at)})</span>
                    </span>
                  ),
                },
              ]}
            />
          </Card>

          <Card title="How this counts">
            <p style={{ fontSize: 13, color: "var(--text-2)" }}>
              This group contributes {money(PLANS.friends.price_ksh)} to monthly recurring
              revenue — once, not once per seat. The {data.seats_taken} people on it are counted
              as {data.seats_taken} entitled students. Both figures are correct; they answer
              different questions.
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
