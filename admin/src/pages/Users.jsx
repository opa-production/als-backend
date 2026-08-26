import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowDown, ArrowUp, Search, UserX } from "lucide-react";

import { Avatar, Badge, Card, Empty, ErrorState, Pagination, SearchInput, Select, Table } from "../components/ui.jsx";
import { useApi, useDebounced } from "../lib/useApi.js";
import { date, money, relative, tierTone } from "../lib/format.js";

const TIERS = [
  { value: "trial", label: "Trial" },
  { value: "standard", label: "Focus" },
  { value: "pro", label: "Synapse" },
  { value: "friends", label: "Friends" },
];

const STATUSES = [
  { value: "active", label: "Active" },
  { value: "paying", label: "Paying" },
  { value: "trial", label: "On trial" },
  { value: "expired", label: "Lapsed" },
  { value: "unverified", label: "Unverified" },
  { value: "deleted", label: "Deleted" },
];

const LIMIT = 25;

export default function Users() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const [search, setSearch] = useState(params.get("q") ?? "");
  const [tier, setTier] = useState(params.get("tier") ?? "");
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [sort, setSort] = useState("created_at");
  const [order, setOrder] = useState("desc");
  const [offset, setOffset] = useState(0);

  const debounced = useDebounced(search);

  // The filter state lives in the URL so a support conversation can be a
  // pasted link rather than a list of instructions.
  useEffect(() => {
    const next = {};
    if (debounced) next.q = debounced;
    if (tier) next.tier = tier;
    if (status) next.status = status;
    setParams(next, { replace: true });
  }, [debounced, tier, status, setParams]);

  // Any filter change puts you back on page one. Staying on page 7 of a result
  // set that now has two pages shows an empty table and reads as a bug.
  useEffect(() => {
    setOffset(0);
  }, [debounced, tier, status, sort, order]);

  const { data, error, loading, refetching, reload } = useApi("/users", {
    q: debounced,
    tier,
    status,
    sort,
    order,
    limit: LIMIT,
    offset,
  });

  if (error) return <ErrorState error={error} onRetry={reload} />;

  function toggleSort(key) {
    if (sort === key) setOrder(order === "asc" ? "desc" : "asc");
    else {
      setSort(key);
      setOrder(key === "name" ? "asc" : "desc");
    }
  }

  const sortIcon = (key) =>
    sort === key ? (
      order === "asc" ? (
        <ArrowUp size={12} strokeWidth={2.2} />
      ) : (
        <ArrowDown size={12} strokeWidth={2.2} />
      )
    ) : null;

  return (
    <div className="content-narrow">
      <div className="filters">
        <SearchInput
          icon={Search}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Name, phone, email, institution or account id"
          aria-label="Search students"
        />
        <Select
          value={tier}
          onChange={(event) => setTier(event.target.value)}
          options={TIERS}
          placeholder="Any plan"
          aria-label="Filter by plan"
        />
        <Select
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          options={STATUSES}
          placeholder="Any status"
          aria-label="Filter by status"
        />
      </div>

      <Card flush>
        <div className={refetching ? "is-refetching" : ""}>
          <Table
            loading={loading}
            rows={data?.items ?? []}
            rowKey={(row) => row.id}
            onRowClick={(row) => navigate(`/users/${row.id}`)}
            empty={
              <Empty icon={UserX} title="No students match that">
                <p style={{ marginTop: 4 }}>Try a shorter search, or clear a filter.</p>
              </Empty>
            }
            columns={[
              {
                key: "name",
                header: "Student",
                sortable: true,
                onSort: () => toggleSort("name"),
                sortIcon: sortIcon("name"),
                render: (row) => (
                  <div className="row" style={{ gap: 10 }}>
                    <Avatar name={row.full_name} />
                    <div style={{ minWidth: 0 }}>
                      <div className="cell-primary">{row.full_name || "(no name)"}</div>
                      <div className="cell-sub">{row.phone ?? row.email ?? "—"}</div>
                    </div>
                  </div>
                ),
              },
              {
                key: "institution",
                header: "Institution",
                render: (row) => (
                  <span className="dim" title={row.institution}>
                    {row.institution || "—"}
                  </span>
                ),
              },
              {
                key: "plan",
                header: "In force",
                render: (row) => (
                  <div className="row" style={{ gap: 6 }}>
                    <Badge tone={tierTone(row.tier)}>{row.plan_name}</Badge>
                    {!row.verified && row.tier !== "expired" && (
                      <Badge tone="danger">Unverified</Badge>
                    )}
                  </div>
                ),
              },
              {
                key: "expires",
                header: "Renews / ends",
                sortable: true,
                onSort: () => toggleSort("expires_at"),
                sortIcon: sortIcon("expires_at"),
                render: (row) =>
                  row.expires_at ? (
                    <div>
                      <div>{date(row.expires_at)}</div>
                      <div className="cell-sub">{relative(row.expires_at)}</div>
                    </div>
                  ) : (
                    <span className="muted">—</span>
                  ),
              },
              {
                key: "paid",
                header: "Paid",
                align: "right",
                render: (row) =>
                  row.total_paid_ksh ? (
                    money(row.total_paid_ksh)
                  ) : (
                    <span className="muted">—</span>
                  ),
              },
              {
                key: "joined",
                header: "Joined",
                align: "right",
                sortable: true,
                onSort: () => toggleSort("created_at"),
                sortIcon: sortIcon("created_at"),
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
