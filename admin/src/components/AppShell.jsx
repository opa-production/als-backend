/*
 * The frame every screen sits in.
 *
 * A fixed coloured rail on the left and a white sheet on the right. The rail is
 * the only saturated surface in the product, which is what lets the content
 * area stay completely quiet — every bit of colour on the white side then means
 * something, because nothing there is decorative.
 */

import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpenCheck,
  GraduationCap,
  LayoutDashboard,
  Library,
  LogOut,
  Receipt,
  RefreshCw,
  RotateCw,
  ScrollText,
  ShieldCheck,
  Users,
  UsersRound,
  Wallet,
} from "lucide-react";

import { USING_MOCKS } from "../lib/api.js";
import { Avatar, Button } from "./ui.jsx";

const NAV = [
  {
    section: null,
    items: [{ to: "/", label: "Overview", icon: LayoutDashboard, end: true }],
  },
  {
    section: "Money",
    items: [
      { to: "/revenue", label: "Revenue", icon: Wallet },
      { to: "/payments", label: "Payments", icon: Receipt },
      { to: "/subscriptions", label: "Subscriptions", icon: RefreshCw },
      { to: "/groups", label: "Friends plans", icon: UsersRound },
    ],
  },
  {
    section: "People",
    items: [{ to: "/users", label: "Students", icon: Users }],
  },
  {
    section: "System",
    items: [
      { to: "/content", label: "Content", icon: Library },
      { to: "/ops", label: "Operations", icon: Activity },
    ],
  },
  {
    section: "Governance",
    items: [
      { to: "/audit", label: "Audit log", icon: ScrollText },
      { to: "/admins", label: "Administrators", icon: ShieldCheck },
    ],
  },
];

/** Route → what the topbar says. Titles live in one place so they cannot drift
 *  from the nav labels beside them. */
const TITLES = [
  [/^\/$/, "Overview", "How the business is doing today"],
  [/^\/revenue/, "Revenue", "What comes in, and from which plan"],
  [/^\/payments\/[^/]+$/, "Payment", "One charge, in full"],
  [/^\/payments/, "Payments", "Every charge Kora has told us about"],
  [/^\/subscriptions/, "Subscriptions", "Who is entitled to what, and until when"],
  [/^\/groups\/[^/]+$/, "Friends plan", "One group and its seats"],
  [/^\/groups/, "Friends plans", "One payment, up to five seats"],
  [/^\/users\/[^/]+$/, "Student", "Everything on one account"],
  [/^\/users/, "Students", "Search, filter and act on accounts"],
  [/^\/content/, "Content", "What students have filed, and the extraction queue"],
  [/^\/ops/, "Operations", "Service health and the plan catalogue"],
  [/^\/audit/, "Audit log", "Every administrative action, in order"],
  [/^\/admins/, "Administrators", "Who can reach this console"],
];

function titleFor(pathname) {
  const match = TITLES.find(([pattern]) => pattern.test(pathname));
  return match ? { title: match[1], subtitle: match[2] } : { title: "Admin", subtitle: "" };
}

export default function AppShell({ admin, onSignOut, attentionCount = 0 }) {
  const location = useLocation();
  const { title, subtitle } = titleFor(location.pathname);

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Main">
        <div className="sidebar-brand">
          <span className="sidebar-mark">
            <GraduationCap size={17} strokeWidth={2.1} />
          </span>
          <span className="sidebar-wordmark">
            <strong>Ardena</strong>
            <span>Admin</span>
          </span>
        </div>

        <div className="sidebar-nav">
          {NAV.map((group, index) => (
            <div key={group.section ?? `group-${index}`}>
              {group.section && <div className="nav-section">{group.section}</div>}
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) => `nav-item ${isActive ? "is-active" : ""}`}
                >
                  <item.icon size={16} strokeWidth={1.9} />
                  {item.label}
                  {item.to === "/" && attentionCount > 0 && (
                    <span className="nav-count">{attentionCount}</span>
                  )}
                </NavLink>
              ))}
            </div>
          ))}
        </div>

        <div className="sidebar-foot">
          <div className="sidebar-user">
            <Avatar name={admin?.full_name || admin?.email} onDark />
            <span className="sidebar-user-name">
              <strong>{admin?.full_name || admin?.email}</strong>
              <span>{admin?.role}</span>
            </span>
            <button className="icon-btn-dark" onClick={onSignOut} aria-label="Sign out" title="Sign out">
              <LogOut size={15} strokeWidth={1.9} />
            </button>
          </div>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <div>
            <h1>{title}</h1>
            {subtitle && <div className="topbar-sub">{subtitle}</div>}
          </div>

          <div className="topbar-actions">
            {USING_MOCKS && (
              <span className="mock-banner" title="VITE_USE_MOCKS is on — no request leaves the browser">
                <BookOpenCheck size={13} strokeWidth={2.1} />
                Sample data
              </span>
            )}
            <Button
              size="sm"
              icon={RotateCw}
              iconOnly
              aria-label="Reload this page"
              onClick={() => window.location.reload()}
            />
          </div>
        </header>

        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
