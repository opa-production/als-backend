/*
 * Charts.
 *
 * Hand-drawn SVG rather than a charting library, for three reasons that all
 * turned out to matter here: the mark specs below are exact (2px lines, ≤24px
 * bars, a 4px rounded data-end, a 2px surface gap between neighbours) and most
 * libraries fight you on them; the brief rules out gradients, and almost every
 * library's default area chart is a fade-to-transparent fill; and a dependency
 * that renders a canvas cannot also render the table view that sits behind
 * every chart here.
 *
 * Rules held to throughout, none of them cosmetic:
 *
 *   · One y-axis, always. Two measures of different scale get two charts — a
 *     dual axis invents a correlation the data does not contain.
 *   · Gridlines are solid hairlines one step off the surface. Dashed reads as
 *     "threshold" or "projection" when it is just a grid.
 *   · Every day in the window is drawn, zeros included. A line that skips the
 *     empty days slopes straight through the outage that made them.
 *   · Labels are selective — the peak and the endpoint, never a number on
 *     every point.
 *   · Text never wears the series colour. Identity comes from a coloured mark
 *     beside the text.
 *   · Every chart has a table twin, so no value is reachable only by hover.
 */

import { useState } from "react";
import { TableProperties, LineChart as LineChartIcon } from "lucide-react";

import { axisDay, fullDay } from "../lib/format.js";
import { useMeasuredWidth } from "../lib/useApi.js";

const MARGIN = { top: 14, right: 14, bottom: 26, left: 48 };

/** Clean axis numbers — 0 / 500 / 1,000, never 0 / 437 / 874. */
function niceCeiling(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

function ticksFor(max, count = 4) {
  const ceiling = niceCeiling(max);
  return Array.from({ length: count + 1 }, (_, i) => (ceiling / count) * i);
}

/** A column with its top corners rounded and its baseline square. */
function columnPath(x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, Math.max(0, height));
  if (height <= 0) return "";
  return [
    `M${x},${y + height}`,
    `L${x},${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `L${x + width - r},${y}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `L${x + width},${y + height}`,
    "Z",
  ].join(" ");
}

// --- Time series --------------------------------------------------------------

/**
 * A daily series over one axis.
 *
 * `variant="area"` for a continuous quantity (money, cumulative things);
 * `variant="column"` for a count of discrete events per day. The area fill is
 * a flat 10% wash of the line colour — a tint, never a gradient.
 */
export function TimeSeries({
  points = [],
  variant = "area",
  color = "var(--series-1)",
  height = 210,
  valueLabel = "Value",
  formatValue = (v) => String(v),
  formatAxis = (v) => String(v),
}) {
  const [ref, width] = useMeasuredWidth(680);
  const [hover, setHover] = useState(null);
  const [asTable, setAsTable] = useState(false);

  const plotWidth = Math.max(60, width - MARGIN.left - MARGIN.right);
  const plotHeight = Math.max(60, height - MARGIN.top - MARGIN.bottom);

  const values = points.map((p) => p.value);
  const max = Math.max(1, ...values);
  const ticks = ticksFor(max);
  const ceiling = ticks[ticks.length - 1];

  const xFor = (index) =>
    points.length <= 1
      ? plotWidth / 2
      : (index / (points.length - 1)) * plotWidth;
  const yFor = (value) => plotHeight - (value / ceiling) * plotHeight;

  const peakIndex = values.indexOf(Math.max(...values));
  const lastIndex = points.length - 1;

  function pointerMove(event) {
    const box = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - box.left - MARGIN.left;
    const ratio = plotWidth ? x / plotWidth : 0;
    const index = Math.round(ratio * (points.length - 1));
    if (index >= 0 && index < points.length) setHover(index);
  }

  if (asTable) {
    return (
      <TableTwin
        points={points}
        valueLabel={valueLabel}
        formatValue={formatValue}
        onBack={() => setAsTable(false)}
      />
    );
  }

  return (
    <div className="chart" ref={ref}>
      <ViewToggle showing="chart" onToggle={() => setAsTable(true)} />

      <svg
        width={width}
        height={height}
        role="img"
        aria-label={`${valueLabel} per day`}
        onMouseMove={pointerMove}
        onMouseLeave={() => setHover(null)}
      >
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={0}
                x2={plotWidth}
                y1={yFor(tick)}
                y2={yFor(tick)}
                stroke="var(--grid)"
                strokeWidth={1}
              />
              <text
                x={-10}
                y={yFor(tick)}
                dy="0.32em"
                textAnchor="end"
                fontSize={11}
                fill="var(--text-3)"
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {formatAxis(tick)}
              </text>
            </g>
          ))}

          {variant === "column" ? (
            <Columns
              points={points}
              plotWidth={plotWidth}
              plotHeight={plotHeight}
              yFor={yFor}
              color={color}
              hover={hover}
            />
          ) : (
            <AreaLine
              points={points}
              xFor={xFor}
              yFor={yFor}
              plotHeight={plotHeight}
              color={color}
            />
          )}

          {/* X ticks: about six, so labels never collide. A chart that decides
              its own tick count by data length ends up with 90 overlapping
              dates the first time someone picks "last 90 days". */}
          {points.map((point, index) => {
            const stride = Math.max(1, Math.ceil(points.length / 6));
            if (index % stride !== 0 && index !== lastIndex) return null;
            if (index !== lastIndex && lastIndex - index < stride * 0.6) return null;
            return (
              <text
                key={point.day}
                x={variant === "column" ? bandCentre(index, points.length, plotWidth) : xFor(index)}
                y={plotHeight + 17}
                textAnchor="middle"
                fontSize={11}
                fill="var(--text-3)"
              >
                {axisDay(point.day)}
              </text>
            );
          })}

          {/* Selective direct labels: the peak, and the endpoint when it is not
              the peak. Never a number on every point. */}
          {points.length > 2 && max > 0 && (
            <DirectLabel
              index={peakIndex}
              points={points}
              variant={variant}
              plotWidth={plotWidth}
              xFor={xFor}
              yFor={yFor}
              formatValue={formatValue}
            />
          )}

          {hover !== null && points[hover] && (
            <Crosshair
              index={hover}
              points={points}
              variant={variant}
              plotWidth={plotWidth}
              plotHeight={plotHeight}
              xFor={xFor}
              yFor={yFor}
              color={color}
            />
          )}

          <line
            x1={0}
            x2={plotWidth}
            y1={plotHeight}
            y2={plotHeight}
            stroke="var(--axis)"
            strokeWidth={1}
          />
        </g>
      </svg>

      {hover !== null && points[hover] && (
        <div
          className="chart-tip"
          style={{
            left: Math.min(
              width - 60,
              Math.max(
                60,
                MARGIN.left +
                  (variant === "column"
                    ? bandCentre(hover, points.length, plotWidth)
                    : xFor(hover))
              )
            ),
            top: MARGIN.top + yFor(points[hover].value) - 12,
          }}
        >
          <div className="chart-tip-label">{fullDay(points[hover].day)}</div>
          <div className="chart-tip-row">
            <span className="legend-swatch" style={{ background: color }} />
            {formatValue(points[hover].value)}
          </div>
        </div>
      )}
    </div>
  );
}

function bandCentre(index, count, plotWidth) {
  const band = plotWidth / count;
  return index * band + band / 2;
}

function Columns({ points, plotWidth, plotHeight, yFor, color, hover }) {
  const band = plotWidth / points.length;
  // ≤24px, and a 2px gap in the surface colour between neighbours. The gap is
  // what separates touching bars — never a stroke drawn around them.
  const barWidth = Math.max(2, Math.min(24, band - 2));

  return (
    <g>
      {points.map((point, index) => {
        const y = yFor(point.value);
        const barHeight = plotHeight - y;
        if (barHeight <= 0) return null;
        return (
          <path
            key={point.day}
            d={columnPath(index * band + (band - barWidth) / 2, y, barWidth, barHeight, 4)}
            fill={color}
            opacity={hover === null || hover === index ? 1 : 0.4}
          />
        );
      })}
    </g>
  );
}

function AreaLine({ points, xFor, yFor, plotHeight, color }) {
  if (!points.length) return null;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${xFor(i)},${yFor(p.value)}`).join(" ");
  const area = `${line} L${xFor(points.length - 1)},${plotHeight} L${xFor(0)},${plotHeight} Z`;
  const lastIndex = points.length - 1;

  return (
    <g>
      {/* A flat 10% wash. Not a gradient — the brief rules those out, and a
          fade also makes the area's own baseline ambiguous. */}
      <path d={area} fill={color} opacity={0.1} />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {/* End marker: r=4 (8px) with a 2px surface ring so it stays legible
          where it crosses the line or the axis. */}
      <circle
        cx={xFor(lastIndex)}
        cy={yFor(points[lastIndex].value)}
        r={4}
        fill={color}
        stroke="var(--bg)"
        strokeWidth={2}
      />
    </g>
  );
}

function DirectLabel({ index, points, variant, plotWidth, xFor, yFor, formatValue }) {
  const point = points[index];
  if (!point) return null;

  const x = variant === "column" ? bandCentre(index, points.length, plotWidth) : xFor(index);
  const y = yFor(point.value);

  // Nudge inward at the edges so the text does not overflow the plot. A label
  // that will not fit is moved, never clipped.
  const anchor = x < 40 ? "start" : x > plotWidth - 40 ? "end" : "middle";

  return (
    <text
      x={x}
      y={y - 9}
      textAnchor={anchor}
      fontSize={11}
      fontWeight={600}
      fill="var(--text-2)"
      style={{ fontVariantNumeric: "tabular-nums" }}
    >
      {formatValue(point.value)}
    </text>
  );
}

function Crosshair({ index, points, variant, plotWidth, plotHeight, xFor, yFor, color }) {
  const x = variant === "column" ? bandCentre(index, points.length, plotWidth) : xFor(index);

  return (
    <g pointerEvents="none">
      <line x1={x} x2={x} y1={0} y2={plotHeight} stroke="var(--axis)" strokeWidth={1} />
      {variant !== "column" && (
        <circle
          cx={x}
          cy={yFor(points[index].value)}
          r={4}
          fill={color}
          stroke="var(--bg)"
          strokeWidth={2}
        />
      )}
    </g>
  );
}

function ViewToggle({ showing, onToggle }) {
  const Icon = showing === "chart" ? TableProperties : LineChartIcon;
  return (
    <button
      type="button"
      className="btn sm ghost"
      onClick={onToggle}
      style={{ position: "absolute", right: 0, top: -34 }}
      aria-label={showing === "chart" ? "Show as a table" : "Show as a chart"}
    >
      <Icon size={14} strokeWidth={1.9} />
      {showing === "chart" ? "Table" : "Chart"}
    </button>
  );
}

/**
 * The table twin.
 *
 * Every chart has one. A value that can only be read by hovering is a value
 * that cannot be read by keyboard, cannot be copied, and cannot be checked.
 */
function TableTwin({ points, valueLabel, formatValue, onBack }) {
  return (
    <div className="chart" style={{ position: "relative" }}>
      <ViewToggle showing="table" onToggle={onBack} />
      <div className="table-wrap" style={{ maxHeight: 260, overflowY: "auto" }}>
        <table className="table">
          <thead>
            <tr>
              <th>Day</th>
              <th style={{ textAlign: "right" }}>{valueLabel}</th>
            </tr>
          </thead>
          <tbody>
            {[...points].reverse().map((point) => (
              <tr key={point.day}>
                <td>{fullDay(point.day)}</td>
                <td className="cell-num">{formatValue(point.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- Bar list -----------------------------------------------------------------

/**
 * Horizontal bars for comparing magnitude across a handful of named things.
 *
 * One hue for every bar. Colouring each bar darker-where-bigger would
 * double-encode the length as hue and burn the only free channel the chart has
 * on information it is already showing.
 */
export function BarList({ items, formatValue = (v) => String(v), color = "var(--series-1)" }) {
  const max = Math.max(1, ...items.map((item) => item.value));

  return (
    <div className="barlist">
      {items.map((item) => (
        <div className="barlist-row" key={item.label}>
          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {item.label}
          </span>
          <span className="tabular" style={{ color: "var(--text-2)", fontWeight: 500 }}>
            {formatValue(item.value)}
          </span>
          <div className="barlist-track">
            <div
              className="barlist-fill"
              style={{ width: `${(item.value / max) * 100}%`, background: color }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

// --- Funnel -------------------------------------------------------------------

const RAMP = ["var(--ramp-1)", "var(--ramp-2)", "var(--ramp-3)", "var(--ramp-4)", "var(--ramp-5)"];

/**
 * Ordered stages, on a one-hue ramp.
 *
 * A ramp is legitimate here precisely because the stages *are* ordered — you
 * cannot reach "paying" without passing "signed up". On unordered categories
 * the same ramp would be a mistake.
 */
export function Funnel({ stages, formatValue = (v) => String(v) }) {
  const top = Math.max(1, stages[0]?.value ?? 1);

  return (
    <div className="stack-16">
      {stages.map((stage, index) => {
        const share = (stage.value / top) * 100;
        return (
          <div key={stage.label}>
            <div className="row-between" style={{ marginBottom: 5 }}>
              <span style={{ fontSize: 13 }}>{stage.label}</span>
              <span className="row" style={{ gap: 10 }}>
                <span className="tabular" style={{ fontWeight: 600 }}>
                  {formatValue(stage.value)}
                </span>
                <span className="tabular muted" style={{ fontSize: 12, minWidth: 42, textAlign: "right" }}>
                  {share.toFixed(0)}%
                </span>
              </span>
            </div>
            <div style={{ height: 10, background: "var(--bg-sunken)", borderRadius: 999 }}>
              <div
                style={{
                  width: `${Math.max(share, 1.5)}%`,
                  height: "100%",
                  borderRadius: 999,
                  background: RAMP[Math.min(index, RAMP.length - 1)],
                }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// --- Part-to-whole ------------------------------------------------------------

const SHARE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];

/**
 * One bar split into up to three named parts, with a legend and a value list.
 *
 * Capped at three because the fourth categorical slot cannot clear the
 * colour-blindness floor against the others when any two can end up adjacent.
 * A fourth part folds into "Other" rather than getting a generated hue.
 */
export function ShareBar({ items, formatValue = (v) => String(v) }) {
  const total = items.reduce((sum, item) => sum + item.value, 0) || 1;
  const top = items.slice(0, 3);
  const rest = items.slice(3);

  const segments = rest.length
    ? [...top, { label: "Other", value: rest.reduce((sum, item) => sum + item.value, 0) }]
    : top;

  return (
    <div className="stack-16">
      <div className="sharebar">
        {segments.map((segment, index) => (
          <div
            key={segment.label}
            className="sharebar-seg"
            style={{
              width: `${(segment.value / total) * 100}%`,
              background: index < 3 ? SHARE_COLORS[index] : "var(--neutral)",
            }}
            title={`${segment.label}: ${formatValue(segment.value)}`}
          />
        ))}
      </div>

      <div className="stack-8">
        {segments.map((segment, index) => (
          <div className="row-between" key={segment.label} style={{ fontSize: 13 }}>
            <span className="chart-legend-item">
              <span
                className="legend-swatch"
                style={{ background: index < 3 ? SHARE_COLORS[index] : "var(--neutral)" }}
              />
              {segment.label}
            </span>
            <span className="row" style={{ gap: 10 }}>
              <span className="tabular" style={{ fontWeight: 500 }}>
                {formatValue(segment.value)}
              </span>
              <span className="tabular muted" style={{ fontSize: 12, minWidth: 40, textAlign: "right" }}>
                {((segment.value / total) * 100).toFixed(0)}%
              </span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Sparkline ----------------------------------------------------------------

/** A 12-point trend for a stat tile. No axes, no labels — it is a shape. */
export function Sparkline({ points, color = "var(--series-1)", width = 78, height = 24 }) {
  if (!points?.length) return null;

  const values = points.map((p) => (typeof p === "number" ? p : p.value));
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;

  const path = values
    .map((value, index) => {
      const x = (index / Math.max(1, values.length - 1)) * width;
      const y = height - ((value - min) / span) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg width={width} height={height} aria-hidden="true" style={{ display: "block" }}>
      <path d={path} fill="none" stroke={color} strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
