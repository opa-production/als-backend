import { useState } from "react";
import {
  GraduationCap,
  KeyRound,
  Mail,
  ScrollText,
  ShieldCheck,
  Wallet,
} from "lucide-react";

import { Button, Field, Input } from "../components/ui.jsx";
import { USING_MOCKS, signIn } from "../lib/api.js";

export default function Login({ onSignedIn }) {
  const [email, setEmail] = useState(USING_MOCKS ? "ceo@ardena.co.ke" : "");
  const [password, setPassword] = useState(USING_MOCKS ? "sample-data-password" : "");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const admin = await signIn(email, password);
      onSignedIn(admin);
    } catch (caught) {
      // The API answers a wrong password and an unknown address identically,
      // and so does this screen. Two different messages here would undo that
      // and turn the form into a way to find out who has access.
      setError(caught.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <aside className="login-aside">
        <div className="row" style={{ gap: 10 }}>
          <span className="sidebar-mark">
            <GraduationCap size={17} strokeWidth={2.1} />
          </span>
          <span className="sidebar-wordmark">
            <strong>Ardena</strong>
            <span>Admin</span>
          </span>
        </div>

        <div>
          <h2>The console behind the Learning System.</h2>
          <p>
            Students, plans, and every shilling that has moved — with an audit
            entry for anything you change.
          </p>

          <div className="login-points">
            <div className="login-point">
              <Wallet size={16} strokeWidth={1.9} />
              <span>Revenue, MRR and per-plan performance from live tables.</span>
            </div>
            <div className="login-point">
              <ShieldCheck size={16} strokeWidth={1.9} />
              <span>Grant plans, release device locks, reconcile lost payments.</span>
            </div>
            <div className="login-point">
              <ScrollText size={16} strokeWidth={1.9} />
              <span>Every action written to the log, in the same transaction.</span>
            </div>
          </div>
        </div>

        <p style={{ fontSize: 12, color: "var(--sidebar-ink-3)" }}>
          Ardena Learning System · Nairobi
        </p>
      </aside>

      <div className="login-form-wrap">
        <form className="login-form stack-16" onSubmit={submit}>
          <div>
            <h2 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.02em" }}>Sign in</h2>
            <p className="section-note">Administrator access only.</p>
          </div>

          <Field label="Email" htmlFor="email">
            <div className="search">
              <Mail size={15} strokeWidth={1.9} />
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@ardena.co.ke"
                style={{ paddingLeft: 32 }}
              />
            </div>
          </Field>

          <Field label="Password" htmlFor="password" error={error}>
            <div className="search">
              <KeyRound size={15} strokeWidth={1.9} />
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="••••••••••••"
                style={{ paddingLeft: 32 }}
              />
            </div>
          </Field>

          <Button type="submit" variant="primary" busy={busy} style={{ width: "100%" }}>
            Sign in
          </Button>

          {USING_MOCKS ? (
            <p className="field-hint">
              Running on sample data — any password works. Try{" "}
              <code className="mono">support@ardena.co.ke</code> to see the console with a
              support role.
            </p>
          ) : (
            <p className="field-hint">
              No account? An owner creates one, or bootstrap the first with{" "}
              <code className="mono">scripts/create_admin.py</code>.
            </p>
          )}
        </form>
      </div>
    </div>
  );
}
