import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { RefreshCcw, Search } from "lucide-react";

import { useAdmin, useToast } from "../App.jsx";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  Modal,
  Pagination,
  SearchInput,
  Select,
  Table,
} from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { hasRole } from "../lib/auth.js";
import { useApi, useDebounced } from "../lib/useApi.js";
import { dateTime, humanise, money, paymentTone } from "../lib/format.js";

const STATUSES = [
  { value: "success", label: "Success" },
  { value: "pending", label: "Pending" },
  { value: "failed", label: "Failed" },
  { value: "abandoned", label: "Abandoned" },
];

const CHANNELS = [
  { value: "mobile_money", label: "Mobile money" },
  { value: "card", label: "Card" },
  { value: "bank", label: "Bank" },
];

const TIERS = [
  { value: "standard", label: "Focus" },
  { value: "pro", label: "Synapse" },
  { value: "friends", label: "Friends" },
];

const LIMIT = 25;

export default function Payments() {
  const admin = useAdmin();
  const toast = useToast();
  const [params, setParams] = useSearchParams();

  const [search, setSearch] = useState("");
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [channel, setChannel] = useState("");
  const [tier, setTier] = useState("");
  const [offset, setOffset] = useState(0);
  const [reconciling, setReconciling] = useState(null);

  const debounced = useDebounced(search);

  useEffect(() => {
    setOffset(0);
  }, [debounced, status, channel, tier]);

  useEffect(() => {
    setParams(status ? { status } : {}, { replace: true });
  }, [status, setParams]);

  const { data, error, loading, refetching, reload } = useApi("/payments", {
    q: debounced,
    status,
    channel,
    tier,
    limit: LIMIT,
    offset,
  });

  if (error) return <ErrorState error={error} onRetry={reload} />;

  const canReconcile = hasRole(admin, "admin");

  async function reconcile(payment) {
    try {
      const result = await api.post(`/payments/${payment.reference}/reconcile`);
      toast(result.message, result.ok ? "good" : "danger");
      reload();
    } catch (caught) {
      toast(caught.message, "danger");
    } finally {
      setReconciling(null);
    }
  }

  return (
    <div className="content-narrow">
      {status === "pending" && (
        <div className="attention warn" style={{ marginBottom: 16 }}>
          <RefreshCcw size={15} strokeWidth={2} />
          <span>
            A charge stuck on <strong>pending</strong> usually means the student paid and the
            webhook never arrived. Reconciling re-reads Kora's own record and applies the
            answer — it is safe to run more than once.
          </span>
        </div>
      )}

      <div className="filters">
        <SearchInput
          icon={Search}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Kora reference, name or phone"
          aria-label="Search payments"
        />
        <Select
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          options={STATUSES}
          placeholder="Any status"
          aria-label="Filter by status"
        />
        <Select
          value={channel}
          onChange={(event) => setChannel(event.target.value)}
          options={CHANNELS}
          placeholder="Any channel"
          aria-label="Filter by channel"
        />
        <Select
          value={tier}
          onChange={(event) => setTier(event.target.value)}
          options={TIERS}
          placeholder="Any plan"
          aria-label="Filter by plan"
        />
      </div>

      <Card flush>
        <div className={refetching ? "is-refetching" : ""}>
          <Table
            loading={loading}
            rows={data?.items ?? []}
            rowKey={(row) => row.id}
            empty={<Empty title="No payments match that" />}
            columns={[
              {
                key: "when",
                header: "When",
                render: (row) => (
                  <div>
                    <div>{dateTime(row.created_at)}</div>
                    <div className="cell-sub mono">{row.reference}</div>
                  </div>
                ),
              },
              {
                key: "who",
                header: "Student",
                render: (row) => (
                  <div>
                    <Link to={`/users/${row.user_id}`} className="cell-primary">
                      {row.full_name || "(deleted)"}
                    </Link>
                    <div className="cell-sub">{row.phone ?? "—"}</div>
                  </div>
                ),
              },
              {
                key: "tier",
                header: "Plan",
                render: (row) => <span className="dim">{humanise(row.tier)}</span>,
              },
              {
                key: "channel",
                header: "Channel",
                render: (row) => <span className="dim">{humanise(row.channel ?? "—")}</span>,
              },
              {
                key: "status",
                header: "Status",
                render: (row) => <Badge tone={paymentTone(row.status)}>{humanise(row.status)}</Badge>,
              },
              {
                key: "amount",
                header: "Amount",
                align: "right",
                render: (row) => money(row.amount_kes),
              },
              {
                key: "action",
                header: "",
                align: "right",
                render: (row) =>
                  canReconcile && row.status !== "success" ? (
                    <Button size="sm" icon={RefreshCcw} onClick={() => setReconciling(row)}>
                      Reconcile
                    </Button>
                  ) : null,
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

      {reconciling && (
        <ReconcileDialog
          payment={reconciling}
          onClose={() => setReconciling(null)}
          onConfirm={() => reconcile(reconciling)}
        />
      )}
    </div>
  );
}

function ReconcileDialog({ payment, onClose, onConfirm }) {
  const [busy, setBusy] = useState(false);

  return (
    <Modal
      title="Re-check this payment with Kora"
      note="Kora's record is the authority. If it says the charge succeeded, the plan is activated exactly as the webhook would have done."
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            onClick={async () => {
              setBusy(true);
              await onConfirm();
              setBusy(false);
            }}
          >
            Reconcile
          </Button>
        </>
      }
    >
      <div className="stack-16">
        <div className="code-block">{payment.reference}</div>
        <p style={{ fontSize: 13, color: "var(--text-2)" }}>
          {payment.full_name} · {money(payment.amount_kes)} · currently{" "}
          <strong>{humanise(payment.status)}</strong>
        </p>
        <p className="field-hint">
          Safe to run more than once — the charge is keyed on its reference, so a payment
          already credited is recognised rather than credited twice.
        </p>
      </div>
    </Modal>
  );
}
