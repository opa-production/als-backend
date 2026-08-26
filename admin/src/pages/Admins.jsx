import { useState } from "react";
import { Info, KeyRound, ShieldCheck, ShieldX, UserPlus } from "lucide-react";

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
  Modal,
  Select,
  Table,
  Textarea,
} from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { hasRole } from "../lib/auth.js";
import { useApi } from "../lib/useApi.js";
import { dateTime, relative } from "../lib/format.js";

const ROLES = [
  { value: "support", label: "Support — read everything, release device locks" },
  { value: "admin", label: "Admin — grant plans, reconcile payments, delete accounts" },
  { value: "owner", label: "Owner — everything, plus managing these accounts" },
];

const ROLE_TONE = { owner: "info", admin: "good", support: "neutral" };

export default function Admins() {
  const me = useAdmin();
  const toast = useToast();
  const { data, error, loading, reload } = useApi("/admins");
  const [dialog, setDialog] = useState(null);

  if (error) return <ErrorState error={error} onRetry={reload} />;

  const isOwner = hasRole(me, "owner");

  async function act(work, message) {
    try {
      await work();
      toast(message);
      reload();
      setDialog(null);
    } catch (caught) {
      toast(caught.message, "danger");
    }
  }

  return (
    <div className="stack-24 content-narrow">
      <div className="row-between">
        <p className="section-note" style={{ maxWidth: "70ch" }}>
          Everyone here reads the same data; the role decides what they can change. It is read
          from the row on every request, never from the token — so a demotion takes effect
          immediately rather than whenever a session happens to expire.
        </p>
        {isOwner && (
          <Button variant="primary" icon={UserPlus} onClick={() => setDialog({ kind: "create" })}>
            Add an administrator
          </Button>
        )}
      </div>

      <Card flush>
        <Table
          loading={loading}
          rows={data ?? []}
          rowKey={(row) => row.id}
          columns={[
            {
              key: "who",
              header: "Administrator",
              render: (row) => (
                <div className="row" style={{ gap: 10 }}>
                  <Avatar name={row.full_name || row.email} />
                  <div>
                    <div className="cell-primary">
                      {row.full_name || row.email}
                      {row.id === me.id && <span className="muted"> · you</span>}
                    </div>
                    <div className="cell-sub">{row.email}</div>
                  </div>
                </div>
              ),
            },
            {
              key: "role",
              header: "Role",
              render: (row) => <Badge tone={ROLE_TONE[row.role]}>{row.role}</Badge>,
            },
            {
              key: "state",
              header: "State",
              render: (row) =>
                row.is_active ? (
                  <Badge tone="good">Active</Badge>
                ) : (
                  <Badge tone="neutral">Deactivated</Badge>
                ),
            },
            {
              key: "seen",
              header: "Last signed in",
              render: (row) =>
                row.last_login_at ? (
                  <div>
                    <div>{dateTime(row.last_login_at)}</div>
                    <div className="cell-sub">{relative(row.last_login_at)}</div>
                  </div>
                ) : (
                  <span className="muted">Never</span>
                ),
            },
            {
              key: "actions",
              header: "",
              align: "right",
              render: (row) =>
                isOwner ? (
                  <div className="row" style={{ justifyContent: "flex-end" }}>
                    <Button
                      size="sm"
                      icon={KeyRound}
                      onClick={() => setDialog({ kind: "edit", admin: row })}
                    >
                      Manage
                    </Button>
                    {row.is_active && row.id !== me.id && (
                      <Button
                        size="sm"
                        variant="danger"
                        icon={ShieldX}
                        iconOnly
                        aria-label={`Remove ${row.email}`}
                        onClick={() => setDialog({ kind: "remove", admin: row })}
                      />
                    )}
                  </div>
                ) : null,
            },
          ]}
        />
      </Card>

      <Card title="How access is granted">
        <div className="stack-16">
          <div className="attention info">
            <Info size={15} strokeWidth={2} />
            <span>
              There is no self-service sign-up and no password-reset email. An owner sets a
              password here; the very first owner is created at a shell with{" "}
              <code className="mono">python scripts/create_admin.py</code>. A console that can
              create its own first account is one anyone who finds the URL can create an
              account on.
            </span>
          </div>

          <DefinitionList
            items={[
              { label: "Support", value: "Reads everything. Releases device locks." },
              {
                label: "Admin",
                value: "Grants and revokes plans, resets counters, reconciles payments.",
              },
              { label: "Owner", value: "Creates, changes and removes administrators." },
              {
                label: "Deactivation",
                value: "Ends every live session immediately, not at the next sign-in.",
              },
            ]}
          />
        </div>
      </Card>

      {dialog?.kind === "create" && (
        <CreateDialog
          onClose={() => setDialog(null)}
          onSubmit={(body) =>
            act(() => api.post("/admins", body), `${body.email} can now sign in.`)
          }
        />
      )}

      {dialog?.kind === "edit" && (
        <EditDialog
          admin={dialog.admin}
          onClose={() => setDialog(null)}
          onSubmit={(body) =>
            act(() => api.patch(`/admins/${dialog.admin.id}`, body), "Administrator updated.")
          }
        />
      )}

      {dialog?.kind === "remove" && (
        <RemoveDialog
          admin={dialog.admin}
          onClose={() => setDialog(null)}
          onSubmit={(reason) =>
            act(
              () => api.del(`/admins/${dialog.admin.id}`, { reason }),
              `${dialog.admin.email} can no longer sign in.`
            )
          }
        />
      )}
    </div>
  );
}

function CreateDialog({ onClose, onSubmit }) {
  const [form, setForm] = useState({ email: "", full_name: "", password: "", role: "support" });
  const [busy, setBusy] = useState(false);

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value });
  const valid = form.email.includes("@") && form.password.length >= 12;

  return (
    <Modal
      title="Add an administrator"
      note="The password is set once here and never shown again — there is no read path for it."
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={!valid}
            onClick={async () => {
              setBusy(true);
              await onSubmit(form);
              setBusy(false);
            }}
          >
            Create
          </Button>
        </>
      }
    >
      <div className="stack-16">
        <Field label="Email">
          <Input type="email" value={form.email} onChange={set("email")} placeholder="name@ardena.co.ke" />
        </Field>
        <Field label="Name">
          <Input value={form.full_name} onChange={set("full_name")} placeholder="Grace Wanjiku" />
        </Field>
        <Field
          label="Password"
          hint="At least 12 characters. Length is the only rule that reliably matters; the rest push people towards Passw0rd!."
          error={form.password && form.password.length < 12 ? "Too short." : null}
        >
          <Input type="password" value={form.password} onChange={set("password")} />
        </Field>
        <Field label="Role">
          <Select value={form.role} onChange={set("role")} options={ROLES} />
        </Field>
      </div>
    </Modal>
  );
}

function EditDialog({ admin, onClose, onSubmit }) {
  const [role, setRole] = useState(admin.role);
  const [isActive, setIsActive] = useState(admin.is_active);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const body = {};
  if (role !== admin.role) body.role = role;
  if (isActive !== admin.is_active) body.is_active = isActive;
  if (password) body.password = password;

  return (
    <Modal
      title={`Manage ${admin.email}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={Object.keys(body).length === 0 || (password && password.length < 12)}
            onClick={async () => {
              setBusy(true);
              await onSubmit(body);
              setBusy(false);
            }}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="stack-16">
        <Field label="Role">
          <Select value={role} onChange={(event) => setRole(event.target.value)} options={ROLES} />
        </Field>

        <label className="row" style={{ gap: 8, fontSize: 13, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={isActive}
            onChange={(event) => setIsActive(event.target.checked)}
          />
          <span>
            Can sign in
            <span className="field-hint" style={{ display: "block" }}>
              Turning this off revokes every live session at once.
            </span>
          </span>
        </label>

        <Field
          label="New password"
          hint="Leave empty to keep the current one. Setting it signs this administrator out everywhere — which is the point of changing it under suspicion."
          error={password && password.length < 12 ? "At least 12 characters." : null}
        >
          <Input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="••••••••••••"
          />
        </Field>
      </div>
    </Modal>
  );
}

function RemoveDialog({ admin, onClose, onSubmit }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <Modal
      title={`Remove ${admin.email}`}
      note="Deactivates and ends every session. The row stays, because the audit log points at it — a trail whose actors have been deleted is a trail of anonymous actions."
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={busy}
            disabled={reason.trim().length < 3}
            onClick={async () => {
              setBusy(true);
              await onSubmit(reason);
              setBusy(false);
            }}
          >
            <ShieldCheck size={14} strokeWidth={2} />
            Remove access
          </Button>
        </>
      }
    >
      <Field label="Reason" hint="Goes in the audit log.">
        <Textarea
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Left the team on the 30th"
        />
      </Field>
    </Modal>
  );
}
