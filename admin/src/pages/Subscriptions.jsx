import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle, Search } from "lucide-react";

import {
  Badge,
  Card,
  Empty,
  ErrorState,
  Pagination,
  SearchInput,
  Select,
  Stat,
  Table,
} from "../components/ui.jsx";
import { useApi, useDebounced } from "../lib/useApi.js";
import { date, money, number, relative, tierTone } from "../lib/format.js";

const TIERS = [
  { value: "trial", label: "Trial" },
  { value: "standard", label: "Focus" },
  { value: "pro", label: "Synapse" },
  { value: "friends", label: "Friends" },
];

const LIMIT = 25;

export default function Subscriptions() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const [search, setSearch] = useState("");
  const [tier, setTier] = useState("");
  const [verified, setVerified] = useState(params.get("verified") ?? "");
  const [expiring, setExpiring] = useState(params.get("expiring_days") ?? "");
  const [offset, setOffset] = useState(0);

  const debounced = useDebounced(search);
  const stats = useApi("/subscriptions/stats");

  useEffect(() => {
    setOffset(0);
  }, [debounced, tier, verified, expiring]);

  useEffect(() => {
    const next = {};
    if (verified) next.verified = verified;
    if (expiring) next.expiring_days = expiring;
    setParams(next, { replace: true });
  }, [verified, expiring, setParams]);

  const { data, error, loading, refetching, reload } = useApi("/subscriptions", {
    q: debounced,
    tier,
    verified,
    expiring_days: expiring,
    limit: LIMIT,
    offset,
  });

  if (error) return <ErrorState error={error} onRetry={reload} />;

  const summary = stats.data;

  return (
    <div className="stack-24 content-narrow">
      <div className="grid grid-4">
        <Stat
          label="Entitled right now"
          value={stats.loading ? "—" : number(summary.total_active)}
          hint="Verified and inside their period"
        />
        <Stat
          label="Paying"
          value={stats.loading ? "—" : number(summary.total_paying)}
          hint="On a plan that costs money"
        />
        <Stat
          label="On trial"
          value={stats.loading ? "—" : number(summary.total_trial)}
          hint="Inside their fourteen days"
        />
        <Stat
          label="Monthly recurring revenue"
          value={stats.loading ? "—" : money(summary.mrr_ksh)}
          hint="Friends counted once per group"
        />
      </div>

      {summary?.total_unverified > 0 && (
        <div className="attention critical">
          <AlertTriangle size={15} strokeWidth={2} />
          <span>
            <strong>{summary.total_unverified}</strong> paid subscription
            {summary.total_unverified === 1 ? " is" : "s are"} live without a confirmed payment.
            The app wrote each on the student's word; Kora has never confirmed it. Each is
            either a payment that went missing or a plan nobody paid for.
          </span>
          <button
            className="attention-link"
            onClick={() => setVerified("false")}
            style={{ background: "none", border: 0, cursor: "pointer" }}
          >
            Show them
          </button>
        </div>
      )}

      <div>
        <div className="filters">
          <SearchInput
            icon={Search}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Name, phone or email"
            aria-label="Search subscriptions"
          />
          <Select
            value={tier}
            onChange={(event) => setTier(event.target.value)}
            options={TIERS}
            placeholder="Any plan"
            aria-label="Filter by plan"
          />
          <Select
            value={verified}
            onChange={(event) => setVerified(event.target.value)}
            options={[
              { value: "true", label: "Confirmed" },
              { value: "false", label: "Unverified" },
            ]}
            placeholder="Any payment state"
            aria-label="Filter by payment state"
          />
          <Select
            value={expiring}
            onChange={(event) => setExpiring(event.target.value)}
            options={[
              { value: "3", label: "Ends in 3 days" },
              { value: "7", label: "Ends in 7 days" },
              { value: "30", label: "Ends in 30 days" },
            ]}
            placeholder="Any end date"
            aria-label="Filter by expiry"
          />
        </div>

        <Card flush>
          <div className={refetching ? "is-refetching" : ""}>
            <Table
              loading={loading}
              rows={data?.items ?? []}
              rowKey={(row) => row.id}
              onRowClick={(row) => navigate(`/users/${row.user_id}`)}
              empty={<Empty title="No subscriptions match that" />}
              columns={[
                {
                  key: "who",
                  header: "Student",
                  render: (row) => (
                    <div>
                      <div className="cell-primary">{row.full_name || "(no name)"}</div>
                      <div className="cell-sub">{row.phone ?? row.email ?? "—"}</div>
                    </div>
                  ),
                },
                {
                  key: "plan",
                  header: "Plan",
                  render: (row) => <Badge tone={tierTone(row.tier)}>{row.plan_name}</Badge>,
                },
                {
                  key: "state",
                  header: "State",
                  render: (row) =>
                    row.is_active ? (
                      <Badge tone="good">Entitled</Badge>
                    ) : !row.verified ? (
                      <Badge tone="danger">Unverified</Badge>
                    ) : (
                      <Badge tone="neutral">Lapsed</Badge>
                    ),
                },
                {
                  key: "started",
                  header: "Started",
                  render: (row) => <span className="dim">{date(row.started_at)}</span>,
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
                  key: "seat",
                  header: "Seat",
                  render: (row) =>
                    row.group_id ? (
                      <Link to={`/groups/${row.group_id}`} onClick={(e) => e.stopPropagation()}>
                        Group
                      </Link>
                    ) : (
                      <span className="muted">Own plan</span>
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
    </div>
  );
}
