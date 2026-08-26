import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  BadgeCheck,
  Gift,
  RotateCcw,
  Smartphone,
  Trash2,
  Undo2,
} from "lucide-react";

import { useAdmin, useToast } from "../App.jsx";
import {
  Avatar,
  Badge,
  Button,
  Card,
  DefinitionList,
  ErrorState,
  Field,
  Input,
  Meter,
  Modal,
  Select,
  Table,
  Textarea,
} from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { hasRole } from "../lib/auth.js";
import { useApi } from "../lib/useApi.js";
import {
  bytes,
  date,
  dateTime,
  humanise,
  money,
  number,
  paymentTone,
  relative,
  tierTone,
} from "../lib/format.js";

const GRANTABLE = [
  { value: "standard", label: "Focus — KES 150 / 30 days" },
  { value: "pro", label: "Synapse — KES 350 / 30 days" },
  { value: "friends", label: "Friends — KES 1,250 / 30 days, 5 seats" },
  { value: "trial", label: "Trial — 14 days" },
  { value: "expired", label: "Expired — end everything now" },
];

export default function UserDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const admin = useAdmin();
  const toast = useToast();

  const { data, error, loading, reload } = useApi(`/users/${id}`);
  const [dialog, setDialog] = useState(null);

  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (loading || !data) return <DetailSkeleton />;

  const canGrant = hasRole(admin, "admin");
  // The subscription row and what the API would actually allow can disagree —
  // an unverified payment, a period that has run out. That disagreement is
  // usually the answer to whatever ticket brought you here, so it is shown
  // rather than resolved silently.
  const mismatch = data.subscription && data.subscription.tier !== data.effective_tier;

  async function act(work, successMessage) {
    try {
      await work();
      toast(successMessage);
      reload();
      setDialog(null);
    } catch (caught) {
      toast(caught.message, "danger");
    }
  }

  return (
    <div className="stack-24 content-narrow">
      <div>
        <Link to="/users" className="row" style={{ gap: 5, fontSize: 13, color: "var(--text-3)" }}>
          <ArrowLeft size={14} strokeWidth={2} />
          All students
        </Link>
      </div>

      <div className="row-between" style={{ alignItems: "flex-start" }}>
        <div className="row" style={{ gap: 14 }}>
          <Avatar name={data.full_name} size="lg" />
          <div>
            <div className="row" style={{ gap: 8 }}>
              <span className="page-title">{data.full_name || "(no name)"}</span>
              {data.deleted_at && <Badge tone="danger">Deleted</Badge>}
            </div>
            <div className="row" style={{ gap: 10, marginTop: 3, fontSize: 13 }}>
              <span className="dim">{data.phone ?? "no phone"}</span>
              <span className="muted">·</span>
              <span className="dim">{data.email ?? "no email"}</span>
              <span className="muted">·</span>
              <span className="dim">Joined {date(data.created_at)}</span>
            </div>
          </div>
        </div>

        <div className="row">
          <Button icon={Smartphone} size="sm" onClick={() => setDialog("device")}>
            Release device lock
          </Button>
          {canGrant && (
            <Button icon={Gift} size="sm" variant="primary" onClick={() => setDialog("grant")}>
              Grant a plan
            </Button>
          )}
          {canGrant &&
            (data.deleted_at ? (
              <Button icon={Undo2} size="sm" onClick={() => setDialog("restore")}>
                Restore
              </Button>
            ) : (
              <Button
                icon={Trash2}
                size="sm"
                variant="danger"
                iconOnly
                aria-label="Delete account"
                onClick={() => setDialog("delete")}
              />
            ))}
        </div>
      </div>

      {mismatch && (
        <div className="attention critical">
          <AlertTriangle size={15} strokeWidth={2} />
          <span>
            The subscription row says <strong>{data.subscription.plan_name}</strong>, but this
            account is being served as <strong>{data.effective_plan_name}</strong>
            {data.subscription.verified === false
              ? " — the payment was never confirmed by Kora."
              : " — the period has run out."}{" "}
            The student sees a working plan; the API refuses every metered request.
          </span>
        </div>
      )}

      <div className="split-main">
        <div className="stack-16">
          <Card
            title="Subscription"
            note="What the row says, and what the API actually allows."
            actions={
              data.subscription?.is_active ? (
                <Badge tone="good">Entitled</Badge>
              ) : (
                <Badge tone="danger">Not entitled</Badge>
              )
            }
          >
            {data.subscription ? (
              <DefinitionList
                items={[
                  {
                    label: "Plan on the row",
                    value: (
                      <span className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                        <Badge tone={tierTone(data.subscription.tier)}>
                          {data.subscription.plan_name}
                        </Badge>
                        {!data.subscription.verified && <Badge tone="danger">Unverified</Badge>}
                      </span>
                    ),
                  },
                  {
                    label: "In force",
                    value: (
                      <Badge tone={tierTone(data.effective_tier)}>
                        {data.effective_plan_name}
                      </Badge>
                    ),
                  },
                  { label: "Started", value: date(data.subscription.started_at) },
                  {
                    label: "Ends",
                    value: data.subscription.expires_at ? (
                      <span>
                        {date(data.subscription.expires_at)}{" "}
                        <span className="muted">({relative(data.subscription.expires_at)})</span>
                      </span>
                    ) : (
                      "—"
                    ),
                  },
                  {
                    label: "Group seat",
                    value: data.subscription.group_id ? (
                      <Link to={`/groups/${data.subscription.group_id}`}>Yes — view group</Link>
                    ) : (
                      <span className="muted">No</span>
                    ),
                  },
                  { label: "Lifetime spend", value: money(data.total_paid_ksh) },
                ]}
              />
            ) : (
              <p className="muted">This account has never had a subscription row.</p>
            )}
          </Card>

          <Card
            title="Usage this period"
            note="A bare counter means nothing without the allowance beside it."
            actions={
              canGrant && (
                <Button size="sm" icon={RotateCcw} onClick={() => setDialog("usage")}>
                  Reset
                </Button>
              )
            }
          >
            <UsageRows usage={data.usage} limits={data.limits} />
          </Card>

          <Card title="Payments" note={`${data.payments.length} most recent charges.`} flush>
            <Table
              rows={data.payments}
              rowKey={(row) => row.id}
              empty={<div className="empty">No charge has ever been attempted.</div>}
              columns={[
                {
                  key: "date",
                  header: "When",
                  render: (row) => (
                    <div>
                      <div>{date(row.created_at)}</div>
                      <div className="cell-sub mono">{row.reference}</div>
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
              ]}
            />
          </Card>
        </div>

        <div className="stack-16">
          <Card title="Profile">
            <DefinitionList
              items={[
                { label: "Institution", value: data.institution || "—" },
                { label: "Programme", value: data.program || "—" },
                { label: "Year", value: data.year_of_study ?? "—" },
                { label: "Semester", value: data.semester ?? "—" },
                { label: "Account id", value: <span className="mono">{data.id.slice(0, 18)}…</span> },
              ]}
            />
          </Card>

          <Card title="Activity">
            <DefinitionList
              items={[
                { label: "Units", value: number(data.activity.units) },
                { label: "Materials", value: number(data.activity.materials) },
                { label: "Storage", value: bytes(data.activity.storage_bytes) },
                { label: "Chats", value: number(data.activity.chats) },
                { label: "Messages", value: number(data.activity.messages) },
                { label: "Days studied", value: number(data.activity.study_days) },
              ]}
            />
          </Card>

          <Card
            title="Devices"
            note="One device may be signed in at a time — the lock is why support gets called."
          >
            {data.devices.length === 0 ? (
              <p className="muted">No device has ever registered.</p>
            ) : (
              <div className="stack-8">
                {data.devices.map((device) => (
                  <div className="row-between" key={device.id} style={{ fontSize: 13 }}>
                    <span className="row" style={{ gap: 8 }}>
                      <Smartphone size={14} strokeWidth={1.9} className="muted" />
                      <span>
                        {humanise(device.platform)} · {device.app_version}
                        <span className="cell-sub">Last seen {relative(device.updated_at)}</span>
                      </span>
                    </span>
                    {device.is_active_device && (
                      <Badge tone="good">
                        <BadgeCheck size={11} strokeWidth={2.4} />
                        Signed in
                      </Badge>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          {data.groups.length > 0 && (
            <Card title="Friends plans">
              <div className="stack-8">
                {data.groups.map((group) => (
                  <div className="row-between" key={group.id} style={{ fontSize: 13 }}>
                    <Link to={`/groups/${group.id}`} className="mono">
                      {group.invite_code}
                    </Link>
                    <span className="row" style={{ gap: 8 }}>
                      <span className="muted tabular">
                        {group.seats_taken}/{group.seats} seats
                      </span>
                      {group.is_owner && <Badge tone="info">Owner</Badge>}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>

      {dialog === "grant" && (
        <GrantDialog
          user={data}
          onClose={() => setDialog(null)}
          onSubmit={(body) =>
            act(
              () => api.post(`/users/${id}/subscription`, body),
              `${data.full_name} is now on ${GRANTABLE.find((p) => p.value === body.tier)?.label.split(" —")[0]}.`
            )
          }
        />
      )}

      {dialog === "device" && (
        <ReasonDialog
          title="Release the device lock"
          note="The account can then sign in on a new phone. This grants nothing — it only unblocks someone who already paid."
          confirmLabel="Release lock"
          defaultReason="New device"
          onClose={() => setDialog(null)}
          onSubmit={(reason) =>
            act(
              () => api.post(`/users/${id}/device-reset`, null, { reason }),
              "Device lock released."
            )
          }
        />
      )}

      {dialog === "usage" && (
        <UsageResetDialog
          usage={data.usage}
          onClose={() => setDialog(null)}
          onSubmit={(body) =>
            act(() => api.post(`/users/${id}/usage/reset`, body), "Usage counters cleared.")
          }
        />
      )}

      {dialog === "delete" && (
        <ReasonDialog
          title="Delete this account"
          note="A tombstone, not a hard delete — the account can be restored, and a device that has been offline will hear about the deletion when it next syncs."
          confirmLabel="Delete account"
          danger
          onClose={() => setDialog(null)}
          onSubmit={(reason) =>
            act(async () => {
              await api.del(`/users/${id}`, { reason });
              navigate("/users");
            }, "Account deleted.")
          }
        />
      )}

      {dialog === "restore" && (
        <ReasonDialog
          title="Restore this account"
          note="Their data comes back. The free trial does not — a restored account does not get another fourteen days."
          confirmLabel="Restore"
          onClose={() => setDialog(null)}
          onSubmit={(reason) =>
            act(() => api.post(`/users/${id}/restore`, null, { reason }), "Account restored.")
          }
        />
      )}
    </div>
  );
}

// --- Usage --------------------------------------------------------------------

const METRIC_LIMIT = {
  ai_queries: "daily_ai_queries",
  quizzes_weekly: "quiz_count",
  quizzes_lifetime: "quiz_count",
  ocr_pages: "monthly_ocr_page_limit",
  pdf_pages: "total_pdf_pages_pool",
};

function UsageRows({ usage, limits }) {
  const entries = Object.entries(usage ?? {});
  if (!entries.length) return <p className="muted">Nothing metered has been used yet.</p>;

  return (
    <div className="stack-16">
      {entries.map(([metric, periods]) => {
        const [period, count] = Object.entries(periods)[0] ?? ["—", 0];
        const limit = limits?.[METRIC_LIMIT[metric]] ?? 0;
        return (
          <div className="row-between" key={metric}>
            <div>
              <div style={{ fontSize: 13 }}>{humanise(metric)}</div>
              <div className="cell-sub mono">{period}</div>
            </div>
            <Meter used={count} limit={limit} />
          </div>
        );
      })}
    </div>
  );
}

// --- Dialogs ------------------------------------------------------------------

function GrantDialog({ user, onClose, onSubmit }) {
  const [tier, setTier] = useState("pro");
  const [days, setDays] = useState("");
  const [extend, setExtend] = useState(true);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    await onSubmit({
      tier,
      days: days ? Number(days) : null,
      extend,
      reason,
    });
    setBusy(false);
  }

  return (
    <Modal
      title="Grant a plan"
      note={`${user.full_name} will be entitled immediately, with no payment taken.`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={reason.trim().length < 3}
            onClick={submit}
          >
            Grant plan
          </Button>
        </>
      }
    >
      <form className="stack-16" onSubmit={submit}>
        <Field label="Plan">
          <Select value={tier} onChange={(event) => setTier(event.target.value)} options={GRANTABLE} />
        </Field>

        <Field
          label="Length in days"
          hint="Leave empty to use the plan's own period — 30 days for a paid plan."
        >
          <Input
            type="number"
            min={1}
            max={730}
            value={days}
            placeholder="30"
            onChange={(event) => setDays(event.target.value)}
          />
        </Field>

        <label className="row" style={{ gap: 8, fontSize: 13, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={extend}
            onChange={(event) => setExtend(event.target.checked)}
          />
          <span>
            Add to any time they have left
            <span className="field-hint" style={{ display: "block" }}>
              On the same plan, a goodwill week on top of ten days left leaves seventeen —
              not seven.
            </span>
          </span>
        </label>

        <Field
          label="Reason"
          hint="Required, and it goes in the audit log. Six months from now this is the only thing that explains a free Synapse account."
        >
          <Textarea
            value={reason}
            required
            minLength={3}
            placeholder="Compensation for the sync outage on the 14th"
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
      </form>
    </Modal>
  );
}

function UsageResetDialog({ usage, onClose, onSubmit }) {
  const [metric, setMetric] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const options = Object.keys(usage ?? {}).map((key) => ({ value: key, label: humanise(key) }));

  return (
    <Modal
      title="Clear usage counters"
      note="Hands back an allowance the plan sells, so it is written to the audit log like any other grant."
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={reason.trim().length < 3}
            onClick={async () => {
              setBusy(true);
              await onSubmit({ metric: metric || null, reason });
              setBusy(false);
            }}
          >
            Clear counters
          </Button>
        </>
      }
    >
      <div className="stack-16">
        <Field label="Which counter" hint="Leave on 'every counter' to clear all of them.">
          <Select
            value={metric}
            onChange={(event) => setMetric(event.target.value)}
            options={options}
            placeholder="Every counter"
          />
        </Field>
        <Field label="Reason">
          <Textarea
            value={reason}
            placeholder="Questions were charged twice by the retry bug"
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
      </div>
    </Modal>
  );
}

function ReasonDialog({ title, note, confirmLabel, defaultReason = "", danger, onClose, onSubmit }) {
  const [reason, setReason] = useState(defaultReason);
  const [busy, setBusy] = useState(false);

  return (
    <Modal
      title={title}
      note={note}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            busy={busy}
            disabled={reason.trim().length < 3}
            onClick={async () => {
              setBusy(true);
              await onSubmit(reason);
              setBusy(false);
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <Field label="Reason" hint="Goes in the audit log.">
        <Textarea value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
    </Modal>
  );
}

// --- Loading ------------------------------------------------------------------

function DetailSkeleton() {
  return (
    <div className="stack-24 content-narrow">
      <div className="row" style={{ gap: 14 }}>
        <div className="skeleton" style={{ width: 44, height: 44, borderRadius: "50%" }} />
        <div className="stack-8">
          <div className="skeleton" style={{ width: 180, height: 16 }} />
          <div className="skeleton" style={{ width: 260, height: 11 }} />
        </div>
      </div>
      <div className="split-main">
        <div className="skeleton" style={{ height: 320, borderRadius: "var(--radius)" }} />
        <div className="skeleton" style={{ height: 320, borderRadius: "var(--radius)" }} />
      </div>
    </div>
  );
}
