"""Generate a PDF or CSV report of temperature readings from the SQLite database."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ._version import __version__
from .categories import FEVER, HIGH_FEVER, HYPOTHERMIA, LOW_GRADE_FEVER, NORMAL, classify, convert
from .config import (
    DEFAULT_PATIENT_CONFIG,
    DEFAULT_REPORT_CONFIG,
    ConfigError,
    PatientConfig,
    ReportConfig,
    load_config,
    load_profile_details,
    load_report_config,
)
from .storage import ensure_schema

_PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}

_PAGE_SIZES = {"letter": letter, "a4": A4}

# Date/time strftime patterns for each date_format preset.
_DATE_TIME_FORMATS = {
    "us": "%m/%d/%Y %I:%M:%S %p",
    "world": "%d/%m/%Y %H:%M:%S",
}

# Maximum number of x-axis date labels to show on the chart before thinning
# them out, so labels don't overlap when there are many readings.
_CHART_MAX_LABELS = 10

# Fever/hypothermia band -> background color for table rows / chart legend.
_CATEGORY_COLORS = {
    HYPOTHERMIA: colors.HexColor("#cfe2f3"),
    NORMAL: colors.HexColor("#d9ead3"),
    LOW_GRADE_FEVER: colors.HexColor("#fff2cc"),
    FEVER: colors.HexColor("#fce5cd"),
    HIGH_FEVER: colors.HexColor("#f4cccc"),
}

# Band -> severity rank, higher is worse (hypothermia and high fever are
# both medically concerning, but high fever is ranked worst since that's
# the far more common reason someone is checking a thermometer at all).
# Used to pick the "worst" band within a rollup period.
_CATEGORY_SEVERITY = {
    NORMAL: 0,
    LOW_GRADE_FEVER: 1,
    FEVER: 2,
    HYPOTHERMIA: 3,
    HIGH_FEVER: 4,
}

_UNIT_LABELS = {"c": "°C", "f": "°F"}


def _format_datetime(recorded_at: datetime, date_format: str) -> str:
    """Format a UTC timestamp in local time using the given date_format preset.

    Args:
        recorded_at: A timezone-aware UTC datetime.
        date_format: "us" (MM/DD/YYYY, 12-hour) or "world" (DD/MM/YYYY, 24-hour).

    Returns:
        The formatted local date/time string.
    """
    return recorded_at.astimezone().strftime(_DATE_TIME_FORMATS[date_format])


@dataclass
class ReportRow:
    """One reading row as read back from the database."""

    recorded_at: datetime
    address: str
    profile: str | None
    value: float
    unit: str
    temperature_type: str | None
    battery_percent: int | None
    error_code: str | None


def _resolve_range(
    period: str, from_date: str | None, to_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """Resolve the requested period/from/to options into a UTC datetime range.

    Args:
        period: One of "7d", "30d", "90d", "1y", "all".
        from_date: Explicit start date (YYYY-MM-DD), overrides ``period``.
        to_date: Explicit end date (YYYY-MM-DD), inclusive. Defaults to now
            if omitted while ``from_date`` is set.

    Returns:
        A ``(start, end)`` tuple of timezone-aware UTC datetimes. Both are
        None when the range is unbounded ("all" with no explicit dates).
    """
    if from_date:
        start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = (
            datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            + timedelta(days=1)
            if to_date
            else datetime.now(timezone.utc)
        )
        return start, end

    if period == "all":
        return None, None

    days = _PERIOD_DAYS[period]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start, end


def fetch_rows(
    db_path: str,
    address: str | None,
    start: datetime | None,
    end: datetime | None,
    profile: str | None = None,
) -> list[ReportRow]:
    """Query readings from the database within an optional address/date range.

    Args:
        db_path: Path to the SQLite database file.
        address: Restrict to a single device's BLE address, if given.
        start: Inclusive UTC start of the date range, or None for no lower bound.
        end: Exclusive UTC end of the date range, or None for no upper bound.
        profile: Restrict to readings tagged with this profile name, if given.

    Returns:
        Matching rows ordered oldest first.
    """
    query = (
        "SELECT recorded_at, address, profile, value, unit, temperature_type, "
        "battery_percent, error_code FROM readings"
    )
    clauses: list[str] = []
    params: list[str] = []

    if address:
        clauses.append("address = ?")
        params.append(address)
    if profile:
        clauses.append("profile = ?")
        params.append(profile)
    if start is not None:
        clauses.append("recorded_at >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("recorded_at < ?")
        params.append(end.isoformat())

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY recorded_at ASC"

    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(query, params)
        return [
            ReportRow(
                recorded_at=datetime.fromisoformat(row[0]),
                address=row[1],
                profile=row[2],
                value=row[3],
                unit=row[4],
                temperature_type=row[5],
                battery_percent=row[6],
                error_code=row[7],
            )
            for row in cursor.fetchall()
        ]
    finally:
        connection.close()


def _display_value(row: ReportRow, unit: str) -> float:
    """Return the reading's value converted to the report's display unit."""
    return convert(row.value, row.unit, unit)


def _who(row: ReportRow) -> str:
    """Return the profile name if set, else a device label.

    Unlike etekcity-bp-daemon's ``_who`` (which falls back to "User N" from
    a hardware slot), this device class has no slot concept at all -- an
    untagged reading's only identity is which physical device it came from.
    """
    return row.profile or f"Device {row.address}"


def _apply_profile_overrides(
    report_config: ReportConfig, patient_config: PatientConfig
) -> ReportConfig:
    """Apply a profile's unit/date_format/page_size overrides onto report_config.

    Each override is independent and only applied if the profile actually
    set it, so e.g. one household member can override just the unit while
    still using the shared date_format and page_size.

    Args:
        report_config: The base (shared) report configuration.
        patient_config: Supplies the profile's overrides, if any.

    Returns:
        A copy of ``report_config`` with the profile's overrides applied,
        or ``report_config`` unchanged if the profile set none of them.
    """
    overrides = {}
    if patient_config.unit:
        overrides["unit"] = patient_config.unit
    if patient_config.date_format:
        overrides["date_format"] = patient_config.date_format
    if patient_config.page_size:
        overrides["page_size"] = patient_config.page_size
    return replace(report_config, **overrides) if overrides else report_config


def build_csv(rows: list[ReportRow], output_path: str, report_config: ReportConfig) -> None:
    """Write reading rows to a CSV file.

    Args:
        rows: Reading rows to include, oldest first.
        output_path: Filesystem path to write the CSV to.
        report_config: Controls which columns are shown, the display unit,
            and the date/time format.
    """
    unit_label = _UNIT_LABELS[report_config.unit]

    header = ["Date/Time (local)"]
    if report_config.include_address:
        header.append("Address")
    if report_config.include_profile:
        header.append("Who")
    header.extend([f"Temperature ({unit_label})", "Site", "Category"])
    if report_config.include_battery:
        header.append("Battery %")
    header.append("Error")

    with open(output_path, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(header)
        for row in rows:
            values: list[object] = [_format_datetime(row.recorded_at, report_config.date_format)]
            if report_config.include_address:
                values.append(row.address)
            if report_config.include_profile:
                values.append(_who(row))
            values.extend(
                [
                    f"{_display_value(row, report_config.unit):.1f}",
                    row.temperature_type or "",
                    classify(row.value, row.unit),
                ]
            )
            if report_config.include_battery:
                values.append(row.battery_percent if row.battery_percent is not None else "")
            values.append(row.error_code or "")
            writer.writerow(values)


def _header_style_commands() -> list[tuple]:
    """Return the header/grid/font style commands shared by every table layout."""
    return [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f5d8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]


def _build_table(rows: list[ReportRow], report_config: ReportConfig) -> Table:
    """Build the PDF reading table, with rows shaded by fever/hypothermia band.

    Args:
        rows: Reading rows to include, oldest first.
        report_config: Controls which columns are shown, the display unit,
            and the date/time format.

    Returns:
        A styled reportlab Table.
    """
    unit_label = _UNIT_LABELS[report_config.unit]

    header = ["Date/Time (local)"]
    if report_config.include_address:
        header.append("Address")
    if report_config.include_profile:
        header.append("Who")
    numeric_col = len(header)
    header.extend([f"Temp\n({unit_label})", "Site", "Category"])
    if report_config.include_battery:
        header.append("Battery")

    data = [header]
    categories: list[str] = []
    for row in rows:
        category = classify(row.value, row.unit)
        categories.append(category)

        values: list[object] = [_format_datetime(row.recorded_at, report_config.date_format)]
        if report_config.include_address:
            values.append(row.address)
        if report_config.include_profile:
            values.append(_who(row))
        values.extend(
            [
                f"{_display_value(row, report_config.unit):.1f}",
                row.temperature_type or "-",
                category,
            ]
        )
        if report_config.include_battery:
            values.append(f"{row.battery_percent}%" if row.battery_percent is not None else "-")
        data.append(values)

    style_commands = _header_style_commands()
    style_commands.append(("ALIGN", (numeric_col, 1), (numeric_col, -1), "RIGHT"))
    for row_index, category in enumerate(categories, start=1):
        color = _CATEGORY_COLORS.get(category, colors.white)
        style_commands.append(("BACKGROUND", (0, row_index), (-1, row_index), color))

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle(style_commands))
    return table


_COMPACT_LAYOUT_COLUMN_GROUPS = 3


def _build_compact_table(rows: list[ReportRow], report_config: ReportConfig) -> Table:
    """Build the compact layout: Date/Temperature only, side by side.

    Readings fill one column group top-to-bottom before moving to the next
    group, so a full page of readings doesn't leave most of the page width
    empty the way a single narrow table would. Three groups (rather than
    etekcity-bp-daemon's two), since a temperature reading only needs two
    columns to mean anything on its own, leaving more page width to fill.

    Args:
        rows: Reading rows to include, oldest first.
        report_config: Controls the display unit and date/time format.

    Returns:
        A styled reportlab Table.
    """
    unit_label = _UNIT_LABELS[report_config.unit]
    groups = min(_COMPACT_LAYOUT_COLUMN_GROUPS, len(rows))
    rows_per_column = -(-len(rows) // groups)  # ceil division

    group_header = ["Date/Time", f"Temp ({unit_label})"]
    header = group_header * groups
    data = [header]
    for r in range(rows_per_column):
        line: list[object] = []
        for g in range(groups):
            idx = g * rows_per_column + r
            if idx < len(rows):
                row = rows[idx]
                line.append(_format_datetime(row.recorded_at, report_config.date_format))
                line.append(f"{_display_value(row, report_config.unit):.1f}")
            else:
                line.extend(["", ""])
        data.append(line)

    align_cols = [i for i in range(len(header)) if i % 2 == 1]
    style_commands = _header_style_commands()
    style_commands.extend(("ALIGN", (idx, 1), (idx, -1), "RIGHT") for idx in align_cols)

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle(style_commands))
    return table


def _rollup_key(recorded_at: datetime, period: str) -> tuple[int, int]:
    """Return the (year, period-number) bucket a reading's local time falls in."""
    local = recorded_at.astimezone()
    if period == "month":
        return (local.year, local.month)
    iso_year, iso_week, _ = local.isocalendar()
    return (iso_year, iso_week)


def _rollup_label(key: tuple[int, int], period: str) -> str:
    """Render a rollup bucket key as a human-readable period label."""
    if period == "month":
        year, month = key
        return date(year, month, 1).strftime("%B %Y")
    iso_year, iso_week = key
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%m/%d')}-{sunday.strftime('%m/%d')}/{iso_year}"


def _range_str(values: list[float]) -> str:
    """Format a list of values as "avg (min-max)", or "-" if empty."""
    if not values:
        return "-"
    return f"{sum(values) / len(values):.1f} ({min(values):.1f}-{max(values):.1f})"


def _build_rollup_buckets(
    rows: list[ReportRow], period: str
) -> dict[tuple[int, int, str], list[ReportRow]]:
    """Group reading rows into per-person weekly or monthly buckets.

    Bucketed by (period, person) rather than just period -- averaging two
    different people's temperatures into one "week" row would be medically
    meaningless, the same reasoning as the chart's per-person lines.
    ``_who`` covers both tagged profiles and the untagged device-address
    fallback, so an untagged device still gets split correctly if more
    than one physical thermometer is in use.

    Args:
        rows: Reading rows to include, oldest first.
        period: "week" (ISO calendar week) or "month" (calendar month).

    Returns:
        (year, period-number, person) -> rows in that bucket, sorted
        period-major then person-minor (not insertion order, since one
        person's readings can interleave with another's across weeks).
    """
    buckets: dict[tuple[int, int, str], list[ReportRow]] = {}
    for row in rows:
        key = (*_rollup_key(row.recorded_at, period), _who(row))
        buckets.setdefault(key, []).append(row)
    return dict(sorted(buckets.items(), key=lambda item: item[0]))


def _build_rollup_table(rows: list[ReportRow], report_config: ReportConfig) -> Table:
    """Build the rollup layout: one row per week/month per person instead of per reading.

    Each row shows the reading count, avg/min/max temperature, and the
    worst fever/hypothermia band seen in that period -- a year of daily
    readings becomes ~52 rows instead of 365. A "Who" column is included
    whenever the report spans more than one person, so same-period rows
    for different people aren't indistinguishable.

    Args:
        rows: Reading rows to include, oldest first.
        report_config: Controls the display unit, date/time format, and
            rollup period.

    Returns:
        A styled reportlab Table.
    """
    unit_label = _UNIT_LABELS[report_config.unit]
    buckets = _build_rollup_buckets(rows, report_config.rollup_period)
    multi_person = len({_who(row) for row in rows}) > 1

    header = ["Period"]
    if multi_person:
        header.append("Who")
    header.extend(["Readings", f"Temp\navg (min-max) {unit_label}", "Worst\nCategory"])

    data = [header]
    worst_categories: list[str] = []
    for key, bucket_rows in buckets.items():
        period_key, who = key[:2], key[2]
        display_values = [_display_value(r, report_config.unit) for r in bucket_rows]

        worst_category = None
        worst_rank = -1
        for row in bucket_rows:
            category = classify(row.value, row.unit)
            rank = _CATEGORY_SEVERITY.get(category, -1)
            if rank > worst_rank:
                worst_rank = rank
                worst_category = category
        worst_categories.append(worst_category)

        values: list[object] = [_rollup_label(period_key, report_config.rollup_period)]
        if multi_person:
            values.append(who)
        values.extend([len(bucket_rows), _range_str(display_values), worst_category])
        data.append(values)

    numeric_col = 2 if multi_person else 1
    style_commands = _header_style_commands()
    style_commands.append(("ALIGN", (numeric_col, 1), (numeric_col, -1), "RIGHT"))
    for row_index, category in enumerate(worst_categories, start=1):
        color = _CATEGORY_COLORS.get(category, colors.white)
        style_commands.append(("BACKGROUND", (0, row_index), (-1, row_index), color))

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle(style_commands))
    return table


# Line color per person, cycled if there are more people than colors.
_CHART_COLORS = [
    colors.HexColor("#cc0000"),
    colors.HexColor("#2f5d8a"),
    colors.HexColor("#e69138"),
    colors.HexColor("#6aa84f"),
]

_LEGEND_ROW_HEIGHT = 14
_LEGEND_SWATCH_WIDTH = 14


def _add_chart_legend(
    drawing: Drawing, entries: list[tuple[str, object]], x: float, top_y: float
) -> None:
    """Draw one legend row per (person, color) entry."""
    for i, (person, color) in enumerate(entries):
        row_y = top_y - i * _LEGEND_ROW_HEIGHT
        drawing.add(
            Line(
                x,
                row_y + 3,
                x + _LEGEND_SWATCH_WIDTH,
                row_y + 3,
                strokeColor=color,
                strokeWidth=3,
            )
        )
        drawing.add(
            String(x + _LEGEND_SWATCH_WIDTH + 4, row_y, person, fontName="Helvetica", fontSize=8)
        )


def _build_chart(rows: list[ReportRow], report_config: ReportConfig) -> Drawing:
    """Build a line chart of temperature over time.

    One line per distinct person (see ``_who``), each in its own color --
    averaging or interleaving different people's readings onto one line
    would be medically meaningless. All series share a common numeric
    x-axis (days since the earliest reading in the report) rather than a
    shared category-per-reading axis, so gaps in one person's readings
    don't distort another's, and actual elapsed time between readings is
    reflected instead of treating every reading as equally spaced.

    Args:
        rows: Reading rows to include, oldest first.
        report_config: Supplies the display unit and date/time format.

    Returns:
        A reportlab Drawing containing the chart, or just a "not enough
        data" note if fewer than two readings are present.
    """
    unit_label = _UNIT_LABELS[report_config.unit]

    if len(rows) < 2:
        drawing = Drawing(480, 260)
        drawing.add(String(10, 130, "Not enough data to plot a chart."))
        return drawing

    by_person: dict[str, list[ReportRow]] = {}
    for row in rows:
        by_person.setdefault(_who(row), []).append(row)
    people = sorted(by_person)

    reference_date = rows[0].recorded_at

    def day_offset(row: ReportRow) -> float:
        return (row.recorded_at - reference_date).total_seconds() / 86400

    series_data = []
    legend_entries = []
    for i, person in enumerate(people):
        color = _CHART_COLORS[i % len(_CHART_COLORS)]
        points = [
            (day_offset(row), _display_value(row, report_config.unit)) for row in by_person[person]
        ]
        series_data.append(points)
        legend_entries.append((person, color))

    multi_person = len(people) > 1
    legend_height = (len(people) * _LEGEND_ROW_HEIGHT + 10) if multi_person else 0
    drawing = Drawing(480, 260 + legend_height)

    chart = LinePlot()
    chart.x = 50
    chart.y = 40 + legend_height
    chart.width = 400
    chart.height = 180
    chart.data = series_data

    all_values = [value for series in series_data for _, value in series]
    chart.yValueAxis.valueMin = min(all_values) - 1
    chart.yValueAxis.valueMax = max(all_values) + 1

    all_days = [x for series in series_data for x, _ in series]
    chart.xValueAxis.valueMin = min(all_days)
    chart.xValueAxis.valueMax = max(all_days)
    span_days = max(all_days) - min(all_days)
    if span_days > 0:
        chart.xValueAxis.valueStep = max(1, span_days // _CHART_MAX_LABELS + 1)
    date_pattern = "%m/%d" if report_config.date_format == "us" else "%d/%m"
    chart.xValueAxis.labelTextFormat = lambda value: (
        (reference_date + timedelta(days=value)).astimezone().strftime(date_pattern)
    )

    for i, (_, color) in enumerate(legend_entries):
        chart.lines[i].strokeColor = color
        chart.lines[i].strokeWidth = 1.5

    drawing.add(chart)
    caption = "Multiple people" if multi_person else "Temperature"
    drawing.add(
        String(
            chart.x,
            chart.y + chart.height + 25,
            f"{caption}, {unit_label}, over time",
            fontName="Helvetica-Bold",
            fontSize=10,
        )
    )
    if multi_person:
        _add_chart_legend(drawing, legend_entries, chart.x, legend_height - 5)
    return drawing


def _summary_lines(rows: list[ReportRow], report_config: ReportConfig) -> list[str]:
    """Build min/max/average text lines and a category breakdown.

    Args:
        rows: Reading rows to include, oldest first. Should already be
            restricted to one person -- averaging different people's
            readings together would be medically meaningless.
        report_config: Supplies the display unit.

    Returns:
        Text lines, empty if ``rows`` is empty.
    """
    if not rows:
        return []

    unit_label = _UNIT_LABELS[report_config.unit]
    display_values = [_display_value(row, report_config.unit) for row in rows]
    lines = [
        f"Temperature: avg {sum(display_values) / len(display_values):.1f}, "
        f"min {min(display_values):.1f}, max {max(display_values):.1f} {unit_label}"
    ]

    counts: dict[str, int] = {}
    for row in rows:
        category = classify(row.value, row.unit)
        counts[category] = counts.get(category, 0) + 1
    breakdown = ", ".join(f"{name}: {count}" for name, count in counts.items())
    lines.append(f"Category breakdown: {breakdown}")

    return lines


def _summary_paragraphs(rows: list[ReportRow], report_config: ReportConfig, styles) -> list:
    """Build the summary section: one avg/min/max block per person if more than one.

    Blending different people's averages together would be medically
    meaningless (see the chart and rollup layout, which apply the same
    per-person split), so this prints one labeled block per distinct
    person -- see ``_who`` -- when the report spans more than one, and the
    original unlabeled single block otherwise.

    Args:
        rows: Reading rows to include, oldest first.
        report_config: Supplies the display unit.
        styles: A reportlab stylesheet, as returned by getSampleStyleSheet().

    Returns:
        Paragraph elements, empty if ``rows`` is empty.
    """
    people = sorted({_who(row) for row in rows})
    if len(people) <= 1:
        return [Paragraph(line, styles["Normal"]) for line in _summary_lines(rows, report_config)]

    elements = []
    for person in people:
        person_rows = [row for row in rows if _who(row) == person]
        lines = _summary_lines(person_rows, report_config)
        if not lines:
            continue
        elements.append(Paragraph(f"<b>{escape(person)}</b>", styles["Normal"]))
        elements.extend(Paragraph(line, styles["Normal"]) for line in lines)
    return elements


def build_pdf(
    rows: list[ReportRow],
    output_path: str,
    report_config: ReportConfig = DEFAULT_REPORT_CONFIG,
    patient_config: PatientConfig = DEFAULT_PATIENT_CONFIG,
) -> None:
    """Render reading rows as a chart, summary, and table in a PDF file.

    Args:
        rows: Reading rows to include, oldest first.
        output_path: Filesystem path to write the PDF to.
        report_config: Controls which columns are shown, the display unit,
            the date/time format, the page size, whether a summary is
            printed, whether the chart and/or table are included at all,
            and (if the table is included) which layout it renders as
            (full/compact/rollup).
        patient_config: Optional patient name/email/notes to print below
            the title (fields left blank are omitted).
    """
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(output_path, pagesize=_PAGE_SIZES[report_config.page_size])
    elements = [
        Paragraph("Temperature Report", styles["Title"]),
        Paragraph(
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            f" &middot; {len(rows)} reading(s)",
            styles["Normal"],
        ),
    ]
    if patient_config.name:
        elements.append(Paragraph(f"Patient: {escape(patient_config.name)}", styles["Normal"]))
    if patient_config.email:
        elements.append(Paragraph(f"Email: {escape(patient_config.email)}", styles["Normal"]))
    if patient_config.notes:
        elements.append(Paragraph(f"Notes: {escape(patient_config.notes)}", styles["Normal"]))
    if report_config.include_summary:
        elements.extend(_summary_paragraphs(rows, report_config, styles))
    elements.append(Spacer(1, 0.2 * inch))

    if report_config.include_chart:
        elements.append(_build_chart(rows, report_config))
        elements.append(Spacer(1, 0.2 * inch))

    if report_config.include_table:
        if report_config.table_layout == "compact":
            elements.append(_build_compact_table(rows, report_config))
        elif report_config.table_layout == "rollup":
            elements.append(_build_rollup_table(rows, report_config))
        else:
            elements.append(_build_table(rows, report_config))

    doc.build(elements)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="health-thermometer-report",
        description="Generate a PDF or CSV report from the daemon's reading database.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "-c", "--config", help="Path to the daemon's INI config file (reads db_path from it)"
    )
    source.add_argument(
        "-d", "--db", help="Path to the SQLite database file, bypassing the config file"
    )
    parser.add_argument(
        "-F", "--format", choices=["pdf", "csv"], default="pdf",
        help="Output format (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output", help="Output file path (default: temperature-report.<format>)"
    )
    parser.add_argument(
        "-p", "--period", choices=["7d", "30d", "90d", "1y", "all"], default="all",
        help="Preset date range (default: %(default)s)",
    )
    parser.add_argument(
        "-f", "--from", dest="from_date", metavar="YYYY-MM-DD",
        help="Explicit start date, overrides --period",
    )
    parser.add_argument(
        "-t", "--to", dest="to_date", metavar="YYYY-MM-DD",
        help="Explicit end date (inclusive), defaults to now",
    )
    parser.add_argument(
        "-a", "--address", help="Restrict the report to one device's BLE address"
    )
    parser.add_argument(
        "-P",
        "--profile",
        help=(
            "Restrict to readings tagged with this profile name (requires "
            "--config); also personalizes the report (name/email/notes, "
            "unit/date-format/page-size overrides) from that profile's "
            "[profile.<name>] section"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)

    if args.profile and not args.config:
        print("Error: --profile requires --config (profile details live in the config file)")
        return 1

    db_path = args.db
    report_config = DEFAULT_REPORT_CONFIG
    if args.config:
        try:
            db_path = load_config(args.config).db_path
            report_config = load_report_config(args.config)
        except ConfigError as exc:
            print(f"Error: {exc}")
            return 1

    ensure_schema(db_path)

    patient_config = DEFAULT_PATIENT_CONFIG
    if args.profile:
        try:
            patient_config = load_profile_details(args.config, args.profile)
        except ConfigError as exc:
            print(f"Error: {exc}")
            return 1

    start, end = _resolve_range(args.period, args.from_date, args.to_date)
    output = args.output or f"temperature-report.{args.format}"

    rows = fetch_rows(db_path, args.address, start, end, args.profile)
    if not rows:
        print("No readings found for the given range/filters.")
        return 1

    # A profile's own unit/date_format/page_size (if set) override the
    # shared report config for its reports, so e.g. one household member
    # can see Celsius while another sees Fahrenheit.
    effective_report_config = _apply_profile_overrides(report_config, patient_config)

    if args.format == "csv":
        build_csv(rows, output, effective_report_config)
    else:
        build_pdf(rows, output, effective_report_config, patient_config)
    print(f"Wrote {len(rows)} reading(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
