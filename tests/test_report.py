from dataclasses import replace
from datetime import datetime, timezone

from health_thermometer_daemon.categories import HIGH_FEVER
from health_thermometer_daemon.config import DEFAULT_PATIENT_CONFIG, DEFAULT_REPORT_CONFIG
from health_thermometer_daemon.report import (
    _apply_profile_overrides,
    _build_chart,
    _build_compact_table,
    _build_rollup_buckets,
    _build_rollup_table,
    _build_table,
    _range_str,
    _resolve_range,
    _rollup_key,
    _rollup_label,
    _summary_paragraphs,
    build_csv,
    build_pdf,
    fetch_rows,
)
from health_thermometer_daemon.storage import ReadingStore

_ADDRESS = "AA:BB:CC:DD:EE:FF"


def _record(store, recorded_at, profile=None, value=37.0, unit="C"):
    store.record(
        recorded_at=recorded_at,
        measured_at=None,
        address=_ADDRESS,
        profile=profile,
        value=value,
        unit=unit,
        temperature_type=None,
        battery_percent=None,
        error_code=None,
    )


def test_fetch_rows_ordered_oldest_first(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-02T00:00:00+00:00")
    _record(store, "2026-01-01T00:00:00+00:00")
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    assert [row.recorded_at.day for row in rows] == [1, 2]


def test_fetch_rows_filters_by_profile(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Alice")
    _record(store, "2026-01-01T00:05:00+00:00", profile="Bob")
    store.close()

    rows = fetch_rows(db_path, None, None, None, profile="Alice")
    assert len(rows) == 1
    assert rows[0].profile == "Alice"


def test_resolve_range_period():
    start, end = _resolve_range("7d", None, None)
    assert (end - start).days == 7


def test_resolve_range_all_is_unbounded():
    assert _resolve_range("all", None, None) == (None, None)


def test_resolve_range_explicit_dates():
    start, end = _resolve_range("all", "2026-01-01", "2026-01-05")
    assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 6, tzinfo=timezone.utc)


def test_build_csv_writes_header_and_rows(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", value=39.6)
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    output = str(tmp_path / "report.csv")
    build_csv(rows, output, DEFAULT_REPORT_CONFIG)

    content = open(output).read()
    assert "Category" in content
    assert "High Fever" in content


def test_include_profile_defaults_to_hidden(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Alice")
    store.close()
    rows = fetch_rows(db_path, None, None, None)

    csv_output = str(tmp_path / "report.csv")
    build_csv(rows, csv_output, DEFAULT_REPORT_CONFIG)
    assert "Who" not in open(csv_output).read()
    assert "Alice" not in open(csv_output).read()

    table = _build_table(rows, DEFAULT_REPORT_CONFIG)
    header = table._cellvalues[0]
    assert "Who" not in header
    assert "Alice" not in table._cellvalues[1]


def test_include_profile_yes_shows_who_column(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Alice")
    store.close()
    rows = fetch_rows(db_path, None, None, None)
    report_config = replace(DEFAULT_REPORT_CONFIG, include_profile=True)

    csv_output = str(tmp_path / "report.csv")
    build_csv(rows, csv_output, report_config)
    content = open(csv_output).read()
    assert "Who" in content
    assert "Alice" in content

    table = _build_table(rows, report_config)
    assert "Who" in table._cellvalues[0]
    assert "Alice" in table._cellvalues[1]


def test_apply_profile_overrides_only_applies_set_fields():
    patient = replace(DEFAULT_PATIENT_CONFIG, unit="f")
    result = _apply_profile_overrides(DEFAULT_REPORT_CONFIG, patient)
    assert result.unit == "f"
    assert result.date_format == DEFAULT_REPORT_CONFIG.date_format
    assert result.page_size == DEFAULT_REPORT_CONFIG.page_size


def test_apply_profile_overrides_noop_when_nothing_set():
    result = _apply_profile_overrides(DEFAULT_REPORT_CONFIG, DEFAULT_PATIENT_CONFIG)
    assert result == DEFAULT_REPORT_CONFIG


def test_build_compact_table_fills_column_major(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    for i in range(5):
        _record(store, f"2026-01-0{i + 1}T00:00:00+00:00", value=36.0 + i * 0.1)
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    table = _build_compact_table(rows, DEFAULT_REPORT_CONFIG)
    body = table._cellvalues

    assert body[0] == ["Date/Time", "Temp (°C)"] * 3
    # 5 rows / 3 groups = ceil(5/3) = 2 rows per column; group 1 gets the
    # first 2 readings top-to-bottom, group 2 the next 2, group 3 the last
    # one plus a blank pad row.
    assert len(body) == 3
    assert body[1][1] == "36.0"  # first reading, first group
    assert body[1][3] == "36.2"  # third reading, second group
    assert body[2][4:] == ["", ""]  # padded blank row in the third group


def test_build_compact_table_fewer_rows_than_groups(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00")
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    table = _build_compact_table(rows, DEFAULT_REPORT_CONFIG)
    # Only one reading -- should collapse to a single column group, not pad
    # out extra empty ones.
    assert len(table._cellvalues[0]) == 2


def test_rollup_key_and_label_week():
    dt = datetime(2026, 1, 8, tzinfo=timezone.utc)
    key = _rollup_key(dt, "week")
    assert key == dt.astimezone().isocalendar()[:2]
    label = _rollup_label(key, "week")
    assert "/" in label and "-" in label


def test_rollup_key_and_label_month():
    dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
    key = _rollup_key(dt, "month")
    local = dt.astimezone()
    assert key == (local.year, local.month)
    assert _rollup_label(key, "month") == local.strftime("%B %Y")


def test_range_str():
    assert _range_str([]) == "-"
    assert _range_str([37.0]) == "37.0 (37.0-37.0)"
    assert _range_str([37.0, 38.0, 36.5]) == "37.2 (36.5-38.0)"


def test_build_rollup_table_buckets_by_week_and_flags_worst_category(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    # Same ISO week (Thu + Fri).
    _record(store, "2026-01-01T00:00:00+00:00", value=36.5)
    _record(store, "2026-01-02T00:00:00+00:00", value=40.0)
    # Following week.
    _record(store, "2026-01-08T00:00:00+00:00", value=37.0)
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    table = _build_rollup_table(rows, DEFAULT_REPORT_CONFIG)
    body = table._cellvalues

    assert len(body) == 3  # header + 2 weekly buckets
    assert body[1][1] == 2  # first bucket has 2 readings
    assert body[1][-1] == HIGH_FEVER  # worst of Normal/High Fever
    assert body[2][1] == 1


def test_build_rollup_table_monthly(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-05T00:00:00+00:00")
    _record(store, "2026-02-05T00:00:00+00:00")
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    monthly_config = replace(DEFAULT_REPORT_CONFIG, rollup_period="month")
    table = _build_rollup_table(rows, monthly_config)
    assert len(table._cellvalues) == 3  # header + 2 monthly buckets


def test_build_pdf_layout_permutations(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    for i in range(3):
        _record(store, f"2026-01-0{i + 1}T00:00:00+00:00", value=36.5 + i * 0.1)
    store.close()
    rows = fetch_rows(db_path, None, None, None)

    permutations = {
        "chart_only": replace(DEFAULT_REPORT_CONFIG, include_table=False),
        "table_only": replace(DEFAULT_REPORT_CONFIG, include_chart=False),
        "compact": replace(DEFAULT_REPORT_CONFIG, table_layout="compact"),
        "rollup": replace(DEFAULT_REPORT_CONFIG, table_layout="rollup"),
        "neither": replace(DEFAULT_REPORT_CONFIG, include_chart=False, include_table=False),
    }
    for name, config in permutations.items():
        output = str(tmp_path / f"{name}.pdf")
        build_pdf(rows, output, config)
        with open(output, "rb") as pdf_file:
            assert pdf_file.read(4) == b"%PDF"


def test_build_pdf_with_patient_config(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", value=38.0)
    _record(store, "2026-01-06T00:00:00+00:00", value=37.0)
    store.close()

    rows = fetch_rows(db_path, None, None, None)
    patient = replace(
        DEFAULT_PATIENT_CONFIG,
        name="Alice Smith",
        email="alice@example.com",
        notes="Recovering from flu",
    )
    output = str(tmp_path / "report.pdf")
    build_pdf(rows, output, DEFAULT_REPORT_CONFIG, patient)

    with open(output, "rb") as pdf_file:
        assert pdf_file.read(4) == b"%PDF"


def _two_person_rows(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-05T08:00:00+00:00", profile="Alice", value=38.0)
    _record(store, "2026-01-06T08:00:00+00:00", profile="Bob", value=36.5)
    store.close()
    return fetch_rows(db_path, None, None, None)


def test_rollup_buckets_split_same_period_by_person(tmp_path):
    rows = _two_person_rows(tmp_path)
    buckets = _build_rollup_buckets(rows, "week")
    # Same ISO week, but two distinct people -- must not be one blended bucket.
    assert len(buckets) == 2
    keys = list(buckets)
    assert keys[0][:2] == keys[1][:2]
    assert {key[2] for key in keys} == {"Alice", "Bob"}
    for bucket_rows in buckets.values():
        assert len(bucket_rows) == 1


def test_rollup_table_adds_who_column_when_multi_person(tmp_path):
    rows = _two_person_rows(tmp_path)
    table = _build_rollup_table(rows, DEFAULT_REPORT_CONFIG)
    header = table._cellvalues[0]
    assert "Who" in header
    who_col = header.index("Who")
    people = {row[who_col] for row in table._cellvalues[1:]}
    assert people == {"Alice", "Bob"}
    # Neither person's numbers should equal a blended average of both.
    temp_col = header.index("Temp\navg (min-max) °C")
    values = [row[temp_col] for row in table._cellvalues[1:]]
    assert "38.0 (38.0-38.0)" in values
    assert "36.5 (36.5-36.5)" in values


def test_rollup_table_omits_who_column_for_single_person(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-05T08:00:00+00:00", profile="Alice")
    store.close()
    rows = fetch_rows(db_path, None, None, None)
    table = _build_rollup_table(rows, DEFAULT_REPORT_CONFIG)
    assert "Who" not in table._cellvalues[0]


def test_summary_paragraphs_split_by_person(tmp_path):
    rows = _two_person_rows(tmp_path)
    from reportlab.lib.styles import getSampleStyleSheet

    styles = getSampleStyleSheet()
    elements = _summary_paragraphs(rows, DEFAULT_REPORT_CONFIG, styles)
    text = " ".join(el.text for el in elements)
    assert "Alice" in text
    assert "Bob" in text
    assert "avg 38.0" in text
    assert "avg 36.5" in text
    # Never a blended combined average across both people.
    assert "avg 37.2" not in text and "avg 37.3" not in text


def test_summary_paragraphs_single_block_for_one_person(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-05T08:00:00+00:00", profile="Alice", value=38.0)
    store.close()
    rows = fetch_rows(db_path, None, None, None)
    from reportlab.lib.styles import getSampleStyleSheet

    styles = getSampleStyleSheet()
    elements = _summary_paragraphs(rows, DEFAULT_REPORT_CONFIG, styles)
    text = " ".join(el.text for el in elements)
    assert "<b>Alice</b>" not in text
    assert "avg 38.0" in text


def test_chart_draws_one_line_per_person(tmp_path):
    rows = _two_person_rows(tmp_path)
    drawing = _build_chart(rows, DEFAULT_REPORT_CONFIG)
    # One LinePlot with one series per person (Alice, Bob) -- unlike a
    # blood-pressure daemon there's only one metric (temperature), so
    # there's no systolic/diastolic pair to double the series count.
    chart = drawing.contents[0]
    assert len(chart.data) == 2


def test_chart_single_person_has_one_series(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-05T08:00:00+00:00", profile="Alice", value=38.0)
    _record(store, "2026-01-06T08:00:00+00:00", profile="Alice", value=37.5)
    store.close()
    rows = fetch_rows(db_path, None, None, None)
    drawing = _build_chart(rows, DEFAULT_REPORT_CONFIG)
    chart = drawing.contents[0]
    assert len(chart.data) == 1


def test_build_pdf_renders_multi_person_report(tmp_path):
    rows = _two_person_rows(tmp_path)
    output = str(tmp_path / "report.pdf")
    build_pdf(rows, output, DEFAULT_REPORT_CONFIG)
    with open(output, "rb") as pdf_file:
        assert pdf_file.read(4) == b"%PDF"
