"""Renders a standalone, print-ready HTML report for one lot/date-range.

No charting library — every chart here is hand-built SVG, consistent with
this project's "no frameworks" approach (the annotation UI hand-draws its
own canvas overlays the same way). The page is meant to be opened in a
browser and printed to PDF (Cmd/Ctrl+P) as the "hand it to a client"
deliverable, so everything is self-contained (no external requests) and
print CSS hides the one interactive control (the Print button).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from html import escape
from typing import Any

ACCENT = "#147c72"
ACCENT_STRONG = "#0e5d56"
OCCUPIED = "#dc2626"
VACANT = "#26a69a"
MUTED = "#6f7782"
LINE = "#d8dde5"


def _format_pct(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.1f}%"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_date_label(iso_date: str) -> str:
    return datetime.fromisoformat(iso_date).strftime("%b %-d")


def _format_hour_label(hour: int) -> str:
    if hour == 0:
        return "12am"
    if hour < 12:
        return f"{hour}am"
    if hour == 12:
        return "12pm"
    return f"{hour - 12}pm"


def _nice_number(value: float, round_value: bool) -> float:
    """Heckbert's "nice numbers for graph labels" algorithm -- picks a
    clean value (1/2/5 x a power of 10) near `value`, for axis ticks."""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    if round_value:
        if fraction < 1.5:
            nice_fraction = 1
        elif fraction < 3:
            nice_fraction = 2
        elif fraction < 7:
            nice_fraction = 5
        else:
            nice_fraction = 10
    else:
        if fraction <= 1:
            nice_fraction = 1
        elif fraction <= 2:
            nice_fraction = 2
        elif fraction <= 5:
            nice_fraction = 5
        else:
            nice_fraction = 10
    return nice_fraction * (10 ** exponent)


def _nice_ticks(max_value: float, target_count: int = 4) -> tuple[float, list[float]]:
    """Returns (nice_max, ticks) -- an axis max rounded up to a clean value,
    and the evenly-spaced clean tick values from 0 to nice_max."""
    if not (max_value > 0):
        return 1.0, [0.0, 1.0]
    nice_range = _nice_number(max_value, False)
    step = _nice_number(nice_range / max(1, target_count - 1), True)
    nice_max = math.ceil(max_value / step) * step
    ticks = []
    v = 0.0
    while v <= nice_max + step / 1e6:
        ticks.append(round(v / step) * step)
        v += step
    return nice_max, ticks


def _bar_chart(
    labels: list[str],
    values: list[float | None],
    *,
    color: str,
    value_formatter,
    axis_formatter=None,
    now_index: int | None = None,
    width: int = 760,
    height: int = 220,
) -> str:
    """A simple vertical bar chart. `values` entries may be None (no data),
    rendered as an empty gap rather than a zero-height bar, so "no data" and
    "genuinely zero" never look the same."""
    if not labels:
        return '<p class="empty-note">No data in this range.</p>'
    padding_left, padding_bottom, padding_top = 42, 28, 12
    plot_width = width - padding_left - 10
    plot_height = height - padding_bottom - padding_top
    n = len(labels)
    bar_width = max(2.0, plot_width / n * 0.65)
    gap = plot_width / n
    numeric = [v for v in values if v is not None]
    data_max = max(numeric) if numeric else 1.0
    data_max = max(data_max, 1e-9)
    format_axis_value = axis_formatter or value_formatter
    nice_max, axis_ticks = _nice_ticks(data_max)

    bars = []
    ticks = []
    label_stride = max(1, n // 12)
    for i, (label, value) in enumerate(zip(labels, values)):
        x = padding_left + i * gap + (gap - bar_width) / 2
        if value is not None:
            bar_height = (value / nice_max) * plot_height
            y = padding_top + (plot_height - bar_height)
            title = f"{label}: {value_formatter(value)}"
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
                f'fill="{color}" rx="1.5"><title>{escape(title)}</title></rect>'
            )
        if i % label_stride == 0 or i == n - 1:
            tick_x = x + bar_width / 2
            ticks.append(
                f'<text x="{tick_x:.1f}" y="{height - 8}" font-size="10" fill="{MUTED}" '
                f'text-anchor="middle">{escape(label)}</text>'
            )
    y_gridlines = []
    y_labels = []
    for tick in axis_ticks:
        y = padding_top + plot_height - (tick / nice_max) * plot_height
        y_gridlines.append(
            f'<line x1="{padding_left}" y1="{y:.1f}" x2="{width - 10}" y2="{y:.1f}" '
            f'stroke="{LINE}" stroke-width="1"/>'
        )
        y_labels.append(
            f'<text x="{padding_left - 6:.1f}" y="{y:.1f}" font-size="10" fill="{MUTED}" '
            f'text-anchor="end" dominant-baseline="middle">{escape(format_axis_value(tick))}</text>'
        )
    # A "now" reference line -- mirrors static/dashboard.js's barChartSvg,
    # see there for the rationale. Dashed (never solid, so it never reads
    # as a gridline) and drawn on top of the bars.
    now_marker = ""
    if now_index is not None and 0 <= now_index < n:
        now_x = padding_left + now_index * gap + gap / 2
        now_marker = (
            f'<line x1="{now_x:.1f}" y1="{padding_top}" x2="{now_x:.1f}" y2="{padding_top + plot_height:.1f}" '
            f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="3,3"/>'
            f'<text x="{now_x + 4:.1f}" y="{padding_top + 8:.1f}" font-size="9" fill="{MUTED}">now</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        + "".join(y_gridlines)
        + "".join(y_labels)
        + "".join(bars)
        + "".join(ticks)
        + now_marker
        + "</svg>"
    )


def _dwell_bars(dwell_by_space: list[dict[str, Any]], *, width: int = 760) -> str:
    """Horizontal bars, one per space, sorted busiest-first. Built for lots
    with dozens of spaces, where a vertical bar-per-space chart would be
    unreadable."""
    if not dwell_by_space:
        return '<p class="empty-note">No spaces marked for this lot.</p>'
    rows = sorted(
        dwell_by_space,
        key=lambda d: (d["average_seconds"] is None, -(d["average_seconds"] or 0)),
    )
    numeric = [d["average_seconds"] for d in rows if d["average_seconds"] is not None]
    max_value = max(numeric) if numeric else 1.0
    row_height = 22
    label_width = 130
    bar_area = width - label_width - 90
    height = row_height * len(rows) + 10
    bars = []
    for i, row in enumerate(rows):
        y = 8 + i * row_height
        label = row["label"]
        value = row["average_seconds"]
        bar_w = (value / max_value) * bar_area if value else 0
        color = ACCENT if value else "#c7ccd3"
        value_text = _format_duration(value) if value is not None else "no activity"
        bars.append(
            f'<text x="{label_width - 8}" y="{y + 13}" font-size="11" fill="#20242a" '
            f'text-anchor="end">{escape(label)}</text>'
            f'<rect x="{label_width}" y="{y + 3}" width="{max(bar_w, 1.5):.1f}" height="14" '
            f'fill="{color}" rx="2"/>'
            f'<text x="{label_width + bar_w + 6:.1f}" y="{y + 13}" font-size="10" fill="{MUTED}">'
            f"{escape(value_text)}</text>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">' + "".join(bars) + "</svg>"
    )


def render_report_html(
    lot: dict[str, Any], report: dict[str, Any], camera: dict[str, Any] | None = None
) -> str:
    summary = report["summary"]
    # report["start"]/["end"] are UTC (see app/analytics.py) -- converted to
    # local here purely for display, so the printed date range header shows
    # the calendar days this actually covers for Rob, not UTC's.
    start = datetime.fromisoformat(report["start"]).astimezone()
    end = datetime.fromisoformat(report["end"]).astimezone()
    generated_at = datetime.now().strftime("%b %-d, %Y %-I:%M %p")

    occupancy_labels = [_format_date_label(d["date"]) for d in report["occupancy_by_day"]]
    occupancy_values = [d["rate"] for d in report["occupancy_by_day"]]
    occupancy_chart = _bar_chart(
        occupancy_labels,
        occupancy_values,
        color=ACCENT,
        value_formatter=_format_pct,
        axis_formatter=lambda v: f"{round(v * 100)}%",
    )

    turnover_labels = [_format_date_label(d["date"]) for d in report["turnover_by_day"]]
    turnover_values = [float(d["arrivals"]) for d in report["turnover_by_day"]]
    turnover_chart = _bar_chart(
        turnover_labels,
        turnover_values,
        color=ACCENT_STRONG,
        value_formatter=lambda v: f"{int(v)} arrivals",
        axis_formatter=lambda v: f"{round(v)}",
    )

    hour_labels = [_format_hour_label(h["hour"]) for h in report["peak_hours"]]
    hour_values = [h["rate"] for h in report["peak_hours"]]
    # Only draw the "now" line when the range's end is today -- otherwise
    # (a fully historical range) every hour should already have a complete
    # day's worth of data behind it, and the marker would be noise. Mirrors
    # static/dashboard.js's same check.
    now_local = datetime.now().astimezone()
    range_ends_today = end.date() == now_local.date()
    hour_chart = _bar_chart(
        hour_labels,
        hour_values,
        color=OCCUPIED,
        value_formatter=_format_pct,
        axis_formatter=lambda v: f"{round(v * 100)}%",
        now_index=now_local.hour if range_ends_today else None,
    )

    dwell_chart = _dwell_bars(report["dwell_by_space"])

    peak_hour_label = (
        _format_hour_label(summary["peak_hour"]) if summary["peak_hour"] is not None else "—"
    )
    off_peak_hour_label = (
        _format_hour_label(summary["off_peak_hour"]) if summary["off_peak_hour"] is not None else "—"
    )

    lot_name = escape(lot["name"]) if lot else "—"
    client_name = escape(lot["client_name"]) if lot else "—"
    address = escape(lot["address"]) if lot and lot.get("address") else None
    camera_name = escape(camera["name"]) if camera else None
    title_suffix = f" — {camera_name}" if camera_name else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{client_name} — {lot_name}{title_suffix} — Parking Report</title>
<style>
  :root {{
    --bg: #f5f6f8; --panel: #ffffff; --text: #20242a; --muted: {MUTED};
    --line: {LINE}; --accent: {ACCENT}; --accent-strong: {ACCENT_STRONG};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .page {{ max-width: 860px; margin: 0 auto; padding: 32px 24px 64px; }}
  .print-bar {{ display: flex; justify-content: flex-end; margin-bottom: 12px; }}
  .print-bar button {{
    border: 1px solid var(--line); background: #fff; border-radius: 6px;
    padding: 8px 14px; font: inherit; cursor: pointer; color: var(--text);
  }}
  .print-bar button:hover {{ border-color: var(--accent); }}
  header.report-header {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 24px 28px; margin-bottom: 20px;
  }}
  header.report-header .client {{ font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; font-weight: 700; }}
  header.report-header h1 {{ font-size: 26px; margin: 4px 0 6px; }}
  header.report-header .meta {{ color: var(--muted); font-size: 13px; }}
  .camera-badge {{
    display: inline-block; background: var(--accent); color: #fff; font-size: 12px;
    font-weight: 700; border-radius: 999px; padding: 3px 10px; margin-left: 8px; vertical-align: middle;
  }}
  .stat-grid {{
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px;
  }}
  .stat-card {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px;
  }}
  .stat-card .label {{ font-size: 11px; text-transform: uppercase; color: var(--muted); font-weight: 700; letter-spacing: 0.03em; }}
  .stat-card .value {{ font-size: 22px; font-weight: 700; margin-top: 6px; }}
  .stat-card .sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  section.panel {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 20px 24px; margin-bottom: 18px;
  }}
  section.panel h2 {{ font-size: 15px; margin: 0 0 4px; }}
  section.panel .panel-sub {{ font-size: 12px; color: var(--muted); margin: 0 0 14px; }}
  .empty-note {{ color: var(--muted); font-size: 13px; }}
  footer {{ color: var(--muted); font-size: 11px; text-align: center; margin-top: 28px; }}
  @media print {{
    body {{ background: #fff; }}
    .print-bar {{ display: none; }}
    section.panel, header.report-header, .stat-card {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>
<div class="page">
  <div class="print-bar"><button onclick="window.print()">Print / Save as PDF</button></div>

  <header class="report-header">
    <div class="client">{client_name}</div>
    <h1>{lot_name}{f" &middot; {address}" if address else ""}{f'<span class="camera-badge">{camera_name}</span>' if camera_name else ""}</h1>
    <div class="meta">{start.strftime("%b %-d, %Y")} – {(end - timedelta(seconds=1)).strftime("%b %-d, %Y")} &nbsp;·&nbsp; Generated {generated_at} &nbsp;·&nbsp; {summary["space_count"]} monitored spaces{" on this camera" if camera_name else ""}</div>
  </header>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">Occupancy Rate</div>
      <div class="value">{_format_pct(summary["overall_occupancy_rate"])}</div>
      <div class="sub">of monitored time</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Arrivals</div>
      <div class="value">{summary["total_arrivals"]}</div>
      <div class="sub">parking events</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg. Dwell Time</div>
      <div class="value">{_format_duration(summary["overall_average_dwell_seconds"])}</div>
      <div class="sub">per parking event</div>
    </div>
    <div class="stat-card">
      <div class="label">Peak / Quietest Hour</div>
      <div class="value">{peak_hour_label} / {off_peak_hour_label}</div>
      <div class="sub">{_format_pct(summary["peak_hour_rate"])} / {_format_pct(summary["off_peak_hour_rate"])} occupied</div>
    </div>
  </div>

  <section class="panel">
    <h2>Occupancy Rate Over Time</h2>
    <p class="panel-sub">% of monitored time each day the lot was occupied, across all spaces.</p>
    {occupancy_chart}
  </section>

  <section class="panel">
    <h2>Turnover</h2>
    <p class="panel-sub">Number of distinct parking events (arrivals) per day.</p>
    {turnover_chart}
  </section>

  <section class="panel">
    <h2>Peak / Off-Peak Hours</h2>
    <p class="panel-sub">Average occupancy rate by hour of day, across the full date range.</p>
    {hour_chart}
  </section>

  <section class="panel">
    <h2>Average Dwell Time by Space</h2>
    <p class="panel-sub">How long a typical car stays in each space, busiest first.</p>
    {dwell_chart}
  </section>

  <footer>Generated by the parking occupancy monitoring system.</footer>
</div>
</body>
</html>"""
