/*
 * The shared pieces.
 *
 * One module rather than fifteen files, because these are small and always
 * imported together — and because a console's consistency comes from there
 * being exactly one Button, not from a folder structure.
 */

import { Fragment, useEffect } from "react";
import {
  AlertCircle,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Info,
  Loader2,
  Minus,
  X,
} from "lucide-react";

import { initials } from "../lib/format.js";

// --- Card ---------------------------------------------------------------------

export function Card({ title, note, actions, children, flush = false, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2 className="section-title">{title}</h2>}
            {note && <p className="section-note">{note}</p>}
          </div>
          {actions && <div className="row">{actions}</div>}
        </header>
      )}
      <div className={flush ? "card-body-flush" : "card-body"}>{children}</div>
    </section>
  );
}

// --- Button -------------------------------------------------------------------

export function Button({
  variant = "default",
  size = "md",
  icon: Icon,
  iconOnly = false,
  busy = false,
  children,
  className = "",
  ...rest
}) {
  const classes = [
    "btn",
    variant !== "default" && variant,
    size === "sm" && "sm",
    iconOnly && "icon-only",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button className={classes} disabled={busy || rest.disabled} {...rest}>
      {busy ? (
        <Loader2 size={15} className="spin" style={{ animation: "spin 900ms linear infinite" }} />
      ) : (
        Icon && <Icon size={size === "sm" ? 14 : 15} strokeWidth={1.9} />
      )}
      {!iconOnly && children}
    </button>
  );
}

export function ButtonGroup({ value, onChange, options }) {
  return (
    <div className="btn-group" role="group">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? "is-active" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

// --- Badge --------------------------------------------------------------------

/**
 * State always ships with a word, never a colour alone.
 *
 * The dot is a second channel for anyone who cannot separate the tints — and a
 * third for anyone reading a greyscale printout of this screen.
 */
export function Badge({ tone = "neutral", children }) {
  return (
    <span className={`badge ${tone}`}>
      <span className="badge-dot" />
      {children}
    </span>
  );
}

// --- Stat tile ----------------------------------------------------------------

export function Stat({ label, value, hint, delta, deltaLabel, icon: Icon, children }) {
  const direction = delta === null || delta === undefined ? null : delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  const DeltaIcon = direction === "up" ? ArrowUp : direction === "down" ? ArrowDown : Minus;

  return (
    <div className="stat">
      <div className="stat-label">
        {Icon && <Icon size={14} strokeWidth={1.9} className="muted" />}
        {label}
      </div>
      <div className="stat-value">{value}</div>
      <div className="stat-foot">
        {direction && (
          <span className={`delta ${direction}`}>
            <DeltaIcon size={13} strokeWidth={2.2} />
            {Math.abs(delta).toFixed(1)}%
          </span>
        )}
        {(deltaLabel || hint) && <span>{deltaLabel ?? hint}</span>}
        {children}
      </div>
    </div>
  );
}

// --- Form ---------------------------------------------------------------------

export function Field({ label, hint, error, children, htmlFor }) {
  return (
    <div className="field">
      {label && (
        <label className="field-label" htmlFor={htmlFor}>
          {label}
        </label>
      )}
      {children}
      {error ? <span className="field-error">{error}</span> : hint && <span className="field-hint">{hint}</span>}
    </div>
  );
}

export function Input(props) {
  return <input className="input" {...props} />;
}

export function Textarea(props) {
  return <textarea className="textarea" rows={3} {...props} />;
}

export function Select({ options, placeholder, ...rest }) {
  return (
    <select className="select" {...rest}>
      {placeholder && <option value="">{placeholder}</option>}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export function SearchInput({ icon: Icon, ...rest }) {
  return (
    <div className="search">
      {Icon && <Icon size={15} strokeWidth={1.9} />}
      <input className="input" type="search" {...rest} />
    </div>
  );
}

// --- Table --------------------------------------------------------------------

export function Table({ columns, rows, rowKey, onRowClick, empty, loading }) {
  if (loading) return <TableSkeleton columns={columns.length} />;
  if (!rows.length) return empty ?? <Empty title="Nothing here" />;

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                className={column.sortable ? "sortable" : ""}
                style={column.align === "right" ? { textAlign: "right" } : undefined}
                onClick={column.sortable ? column.onSort : undefined}
              >
                <span className="th-inner">
                  {column.header}
                  {column.sortIcon}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className={onRowClick ? "clickable" : ""}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={column.align === "right" ? "cell-num" : column.cellClass ?? ""}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TableSkeleton({ columns, rows = 8 }) {
  return (
    <div style={{ padding: "12px 14px" }}>
      {Array.from({ length: rows }, (_, rowIndex) => (
        <div
          key={rowIndex}
          style={{
            display: "grid",
            gridTemplateColumns: `repeat(${columns}, 1fr)`,
            gap: 12,
            padding: "9px 0",
          }}
        >
          {Array.from({ length: columns }, (_, cellIndex) => (
            <div
              key={cellIndex}
              className="skeleton"
              style={{ height: 12, width: cellIndex === 0 ? "70%" : "45%" }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function Pagination({ total, limit, offset, onChange }) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  return (
    <div className="pagination">
      <span className="tabular">
        {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}
      </span>
      <div className="row">
        <Button
          size="sm"
          icon={ChevronLeft}
          iconOnly
          aria-label="Previous page"
          disabled={!canPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
        />
        <Button
          size="sm"
          icon={ChevronRight}
          iconOnly
          aria-label="Next page"
          disabled={!canNext}
          onClick={() => onChange(offset + limit)}
        />
      </div>
    </div>
  );
}

// --- Empty / error ------------------------------------------------------------

export function Empty({ icon: Icon, title, children }) {
  return (
    <div className="empty">
      {Icon && (
        <div className="empty-icon">
          <Icon size={19} strokeWidth={1.7} />
        </div>
      )}
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function ErrorState({ error, onRetry }) {
  return (
    <Empty icon={AlertCircle} title="That did not load">
      <p style={{ marginTop: 4 }}>{error?.message ?? "Something went wrong."}</p>
      {onRetry && (
        <div style={{ marginTop: 14 }}>
          <Button size="sm" onClick={onRetry}>
            Try again
          </Button>
        </div>
      )}
    </Empty>
  );
}

// --- Definition list ----------------------------------------------------------

/**
 * A two-column label/value list.
 *
 * `<Fragment key>` rather than a wrapper element per pair: `dt` and `dd` have
 * to be direct children of the `dl` for the CSS grid to lay them out in two
 * columns, and any div between them breaks that.
 */
export function DefinitionList({ items }) {
  return (
    <dl className="dl">
      {items.map(({ label, value }) => (
        <Fragment key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

// --- Tabs ---------------------------------------------------------------------

export function Tabs({ value, onChange, items }) {
  return (
    <div className="tabs" role="tablist">
      {items.map((item) => (
        <button
          key={item.value}
          role="tab"
          aria-selected={item.value === value}
          className={`tab ${item.value === value ? "is-active" : ""}`}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

// --- Modal --------------------------------------------------------------------

export function Modal({ title, note, onClose, children, footer, wide = false }) {
  // Escape closes. A modal you can only leave with the mouse is a modal that
  // traps anyone working through a queue at the keyboard.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header className="modal-head">
          <div>
            <div className="modal-title">{title}</div>
            {note && <p className="section-note">{note}</p>}
          </div>
          <Button variant="ghost" size="sm" icon={X} iconOnly aria-label="Close" onClick={onClose} />
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}

// --- Toast --------------------------------------------------------------------

const TOAST_ICON = { good: CheckCircle2, danger: AlertTriangle, info: Info };
const TOAST_COLOR = { good: "var(--good)", danger: "var(--danger)", info: "var(--info)" };

export function Toasts({ items, onDismiss }) {
  return (
    <div className="toasts">
      {items.map((toast) => {
        const Icon = TOAST_ICON[toast.tone] ?? Info;
        return (
          <div key={toast.id} className={`toast ${toast.tone}`} role="status">
            <Icon size={16} strokeWidth={2} style={{ color: TOAST_COLOR[toast.tone] }} />
            <span style={{ flex: 1 }}>{toast.message}</span>
            <button
              className="icon-btn-dark"
              style={{ color: "var(--text-3)" }}
              aria-label="Dismiss"
              onClick={() => onDismiss(toast.id)}
            >
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

// --- Small bits ---------------------------------------------------------------

export function Avatar({ name, size = "md", onDark = false }) {
  return (
    <span className={`avatar ${size === "lg" ? "lg" : ""} ${onDark ? "on-dark" : ""}`}>
      {initials(name)}
    </span>
  );
}

export function Meter({ used, limit }) {
  // An unlimited allowance has no ratio to draw. A full bar would read as
  // "at the limit", which is the opposite of what it means.
  if (limit === -1) return <span className="muted">Unlimited</span>;
  if (!limit) return <span className="muted">Not included</span>;

  const ratio = Math.min(1, used / limit);
  const tone = ratio >= 1 ? "danger" : ratio >= 0.8 ? "warn" : "";

  return (
    <div style={{ minWidth: 108 }}>
      <div className="meter">
        <div className={`meter-fill ${tone}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="tabular" style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 4 }}>
        {used} of {limit}
      </div>
    </div>
  );
}

export function Chip({ icon: Icon, children }) {
  return (
    <span className="kv-chip">
      {Icon && <Icon size={13} strokeWidth={1.9} />}
      {children}
    </span>
  );
}
