import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search, UsersRound } from "lucide-react";

import {
  Badge,
  Card,
  Empty,
  ErrorState,
  Pagination,
  SearchInput,
  Select,
  Table,
} from "../components/ui.jsx";
import { useApi, useDebounced } from "../lib/useApi.js";
import { date, relative } from "../lib/format.js";

const LIMIT = 25;

export default function Groups() {
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [active, setActive] = useState("");
  const [offset, setOffset] = useState(0);

  const debounced = useDebounced(search);

  useEffect(() => {
    setOffset(0);
  }, [debounced, active]);

  const { data, error, loading, refetching, reload } = useApi("/groups", {
    q: debounced,
    active,
    limit: LIMIT,
    offset,
  });

  if (error) return <ErrorState error={error} onRetry={reload} />;

  return (
    <div className="content-narrow">
      <p className="section-note" style={{ marginBottom: 16 }}>
        A Friends plan is the one place where one payment and several entitlements come apart.
        Five people on Synapse limits for a single charge of KES 1,250 is correct here, and
        looks like a discrepancy everywhere else.
      </p>

      <div className="filters">
        <SearchInput
          icon={Search}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Invite code or owner"
          aria-label="Search groups"
        />
        <Select
          value={active}
          onChange={(event) => setActive(event.target.value)}
          options={[
            { value: "true", label: "Still running" },
            { value: "false", label: "Ended" },
          ]}
          placeholder="Any state"
          aria-label="Filter by state"
        />
      </div>

      <Card flush>
        <div className={refetching ? "is-refetching" : ""}>
          <Table
            loading={loading}
            rows={data?.items ?? []}
            rowKey={(row) => row.id}
            onRowClick={(row) => navigate(`/groups/${row.id}`)}
            empty={<Empty icon={UsersRound} title="No Friends plans match that" />}
            columns={[
              {
                key: "code",
                header: "Invite code",
                render: (row) => <span className="mono cell-primary">{row.invite_code}</span>,
              },
              {
                key: "owner",
                header: "Owner",
                render: (row) => (
                  <div>
                    <div className="cell-primary">{row.owner_name || "(deleted)"}</div>
                    <div className="cell-sub">{row.owner_phone ?? "—"}</div>
                  </div>
                ),
              },
              {
                key: "seats",
                header: "Seats",
                render: (row) => (
                  <div className="row" style={{ gap: 8 }}>
                    <SeatPips taken={row.seats_taken} total={row.seats} />
                    <span className="tabular dim">
                      {row.seats_taken}/{row.seats}
                    </span>
                  </div>
                ),
              },
              {
                key: "state",
                header: "State",
                render: (row) =>
                  row.is_active ? <Badge tone="good">Running</Badge> : <Badge tone="neutral">Ended</Badge>,
              },
              {
                key: "ends",
                header: "Ends",
                render: (row) => (
                  <div>
                    <div>{date(row.expires_at)}</div>
                    <div className="cell-sub">{relative(row.expires_at)}</div>
                  </div>
                ),
              },
              {
                key: "created",
                header: "Bought",
                align: "right",
                render: (row) => <span className="dim">{date(row.created_at)}</span>,
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

/**
 * Five small blocks, filled to match. A seat count is a tiny whole number, and
 * a shape is faster to read at a glance than "3/5" — the digits stay beside it
 * so nothing is carried by the shape alone.
 */
export function SeatPips({ taken, total }) {
  return (
    <span className="row" style={{ gap: 2 }} aria-hidden="true">
      {Array.from({ length: total }, (_, index) => (
        <span
          key={index}
          style={{
            width: 7,
            height: 14,
            borderRadius: 2,
            background: index < taken ? "var(--series-1)" : "var(--bg-sunken)",
          }}
        />
      ))}
    </span>
  );
}
