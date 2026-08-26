import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import AppShell from "./components/AppShell.jsx";
import { Toasts } from "./components/ui.jsx";
import { api, signOut as apiSignOut } from "./lib/api.js";
import { getStoredAdmin, getToken, setStoredAdmin } from "./lib/auth.js";

import Admins from "./pages/Admins.jsx";
import Audit from "./pages/Audit.jsx";
import Content from "./pages/Content.jsx";
import GroupDetail from "./pages/GroupDetail.jsx";
import Groups from "./pages/Groups.jsx";
import Login from "./pages/Login.jsx";
import Ops from "./pages/Ops.jsx";
import Overview from "./pages/Overview.jsx";
import Payments from "./pages/Payments.jsx";
import Revenue from "./pages/Revenue.jsx";
import Subscriptions from "./pages/Subscriptions.jsx";
import UserDetail from "./pages/UserDetail.jsx";
import Users from "./pages/Users.jsx";

// --- Toasts -------------------------------------------------------------------

const ToastContext = createContext(() => {});

/** `const toast = useToast(); toast("Saved.")` — the confirmation channel for
 *  every action that changes something. */
export function useToast() {
  return useContext(ToastContext);
}

// --- Signed-in admin ----------------------------------------------------------

const AdminContext = createContext(null);

export function useAdmin() {
  return useContext(AdminContext);
}

// --- App ----------------------------------------------------------------------

export default function App() {
  const [admin, setAdmin] = useState(getStoredAdmin);
  const [checking, setChecking] = useState(Boolean(getToken()));
  const [toasts, setToasts] = useState([]);
  const [attentionCount, setAttentionCount] = useState(0);
  const location = useLocation();

  const pushToast = useCallback((message, tone = "good") => {
    const id = Math.random().toString(36).slice(2);
    setToasts((current) => [...current, { id, message, tone }]);
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 5200);
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  // A stored admin is a cache, not proof. The token is re-checked against the
  // server on load, so a revoked session lands on the sign-in screen instead of
  // rendering a console whose every request is about to fail.
  useEffect(() => {
    if (!getToken()) {
      setChecking(false);
      return;
    }
    let cancelled = false;
    api
      .get("/auth/me")
      .then((me) => {
        if (cancelled) return;
        setAdmin(me);
        setStoredAdmin(me);
      })
      .catch(() => {
        if (!cancelled) setAdmin(null);
      })
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The count on the Overview nav row. Fetched here rather than inside the page
  // so the badge is right even while someone is looking at another screen.
  useEffect(() => {
    if (!admin) return;
    api
      .get("/overview")
      .then((data) => setAttentionCount(data.attention?.length ?? 0))
      .catch(() => setAttentionCount(0));
  }, [admin]);

  const handleSignIn = useCallback((signedIn) => {
    setAdmin(signedIn);
    setStoredAdmin(signedIn);
  }, []);

  const handleSignOut = useCallback(async () => {
    await apiSignOut();
    setAdmin(null);
  }, []);

  const toastValue = useMemo(() => pushToast, [pushToast]);

  if (checking) {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100%" }}>
        <div className="skeleton" style={{ width: 180, height: 12 }} />
      </div>
    );
  }

  if (!admin) {
    return (
      <ToastContext.Provider value={toastValue}>
        <Login onSignedIn={handleSignIn} />
        <Toasts items={toasts} onDismiss={dismissToast} />
      </ToastContext.Provider>
    );
  }

  return (
    <AdminContext.Provider value={admin}>
      <ToastContext.Provider value={toastValue}>
        <Routes>
          <Route
            element={
              <AppShell admin={admin} onSignOut={handleSignOut} attentionCount={attentionCount} />
            }
          >
            <Route path="/" element={<Overview />} />
            <Route path="/revenue" element={<Revenue />} />
            <Route path="/payments" element={<Payments />} />
            <Route path="/subscriptions" element={<Subscriptions />} />
            <Route path="/groups" element={<Groups />} />
            <Route path="/groups/:id" element={<GroupDetail />} />
            <Route path="/users" element={<Users />} />
            <Route path="/users/:id" element={<UserDetail />} />
            <Route path="/content" element={<Content />} />
            <Route path="/ops" element={<Ops />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/admins" element={<Admins />} />
            <Route path="*" element={<Navigate to="/" replace state={{ from: location }} />} />
          </Route>
        </Routes>
        <Toasts items={toasts} onDismiss={dismissToast} />
      </ToastContext.Provider>
    </AdminContext.Provider>
  );
}
