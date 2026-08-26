import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ScrollText } from "lucide-react";

import { Avatar, Card, Empty, ErrorState, Pagination, Select, Table } from "../components/ui.jsx";
import { useApi } from "../lib/useApi.js";
import { dateTime, humanise, relative } from "../lib/format.js";

const LIMIT = 30;

/** Tone per action family. Money and deletion read louder than a sign-in. */
const ACTION_TONE = {
  "subscription.granted": "var(--info-text)",
  "subscription.revoked": "var(--danger-text)",
  "user.deleted": "var(--danger-text)",
  "admin.removed": "var(--danger-text)",
  "payment.reconciled": "var(--good-text)",
};

export default function Audit() {
  const [action, setAction] = useState("");
  const [adminId, setAdminId] = useState("");
  const [offset, setOffset] = useState(0);

  const actions = useApi("/audit/actions");
  const admins = useApi("/admins");

  useEffect(() => {
    setOffset(0);
  }, [action, adminId]);

  const { data, error, loading, refetching, reload } = useApi("/audit", {
    action,
    admin_id: adminId,
    limit: LIMIT,
    offset,
  });

  if (error) return <ErrorState error={error} onRetry={reload} />;

  return (
    <div className="content-narrow">
      <p className="section-note" style={{ marginBottom: 16 }}>
        Written in the same transaction as the change it describes, so it cannot record
        something that was rolled back. Nothing in this console edits or deletes an entry, and
        any admin can read it — a log only the people who can change it may see is not much of
        a check on them.
      </p>

      <div className="filters">
        <Select
          value={action}
          onChange={(event) => setAction(event.target.value)}
          options={(actions.data ?? []).map((row) => ({
            value: row.action,
            label: `${row.action} (${row.count})`,
          }))}
          placeholder="Any action"
          aria-label="Filter by action"
        />
        <Select
          value={adminId}
          onChange={(event) => setAdminId(event.target.value)}
          options={(admins.data ?? []).map((row) => ({
            value: row.id,
            label: row.full_name || row.email,
          }))}
          placeholder="Any administrator"
          aria-label="Filter by administrator"
        />
      </div>

      <Card flush>
        <div className={refetching ? "is-refetching" : ""}>
          <Table
            loading={loading}
            rows={data?.items ?? []}
            rowKey={(row) => row.id}
            empty={<Empty icon={ScrollText} title="No entries match that" />}
            columns={[
              {
                key: "when",
                header: "When",
                render: (row) => (
                  <div>
                    <div>{dateTime(row.created_at)}</div>
                    <div className="cell-sub">{relative(row.created_at)}</div>
                  </div>
                ),
              },
              {
                key: "who",
                header: "Administrator",
                render: (row) => (
                  <div className="row" style={{ gap: 9 }}>
                    <Avatar name={row.admin_email} />
                    <div style={{ minWidth: 0 }}>
                      <div className="cell-primary">{row.admin_email}</div>
                      <div className="cell-sub mono">{row.ip ?? "—"}</div>
                    </div>
                  </div>
                ),
              },
              {
                key: "action",
                header: "Action",
                render: (row) => (
                  <span
                    className="mono"
                    style={{ color: ACTION_TONE[row.action] ?? "var(--text-2)", fontWeight: 500 }}
                  >
                    {row.action}
                  </span>
                ),
              },
              {
                key: "summary",
                header: "What happened",
                render: (row) => (
                  <div style={{ maxWidth: 460 }}>
                    <div>{row.summary}</div>
                    {row.meta?.reason && (
                      <div className="cell-sub">Reason recorded: {row.meta.reason}</div>
                    )}
                  </div>
                ),
              },
              {
                key: "target",
                header: "Target",
                align: "right",
                render: (row) =>
                  row.target_type === "user" && row.target_id ? (
                    <Link to={`/users/${row.target_id}`} className="mono" style={{ fontSize: 12 }}>
                      {row.target_id.slice(0, 8)}
                    </Link>
                  ) : (
                    <span className="muted">{humanise(row.target_type) || "—"}</span>
                  ),
              },
            ]}
          />
        </div>

        {data && data.total > 0 && (
          <div className="card-foot">
            <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </div>
        )}
      </Card>
    </div>
  );
}
