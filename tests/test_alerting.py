from dataclasses import replace
from datetime import datetime, timezone

from health_thermometer_daemon.alerting import check_alerts
from health_thermometer_daemon.config import DEFAULT_ALERT_CONFIG, DEFAULT_PATIENT_CONFIG
from health_thermometer_daemon.storage import ReadingStore

_ADDRESS = "AA:BB:CC:DD:EE:FF"
_OTHER_ADDRESS = "11:22:33:44:55:66"


def _record(store, recorded_at, address=_ADDRESS, profile=None, value=37.0, unit="C"):
    store.record(
        recorded_at=recorded_at,
        measured_at=None,
        address=address,
        profile=profile,
        value=value,
        unit=unit,
        temperature_type=None,
        battery_percent=None,
        error_code=None,
    )


def test_no_alerts_when_disabled_checks(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00")
    store.close()

    config = replace(DEFAULT_ALERT_CONFIG, state_path=str(tmp_path / "state.json"))
    alerts = check_alerts(db_path, config)
    assert alerts == []


def test_staleness_alert(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00")
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://localhost"],
        stale_after_days=2,
        state_path=str(tmp_path / "state.json"),
    )
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, now=now)
    assert len(alerts) == 1
    assert "No reading" in alerts[0].message
    assert alerts[0].urls == ["json://localhost"]


def test_staleness_alert_throttled_on_repeat_check(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00")
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://localhost"],
        stale_after_days=2,
        state_path=str(tmp_path / "state.json"),
    )
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    first = check_alerts(db_path, config, now=now)
    second = check_alerts(db_path, config, now=now)
    assert len(first) == 1
    assert len(second) == 0


def test_high_temp_alert_fires_once(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", value=39.6)
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://localhost"],
        high_temp_alert_celsius=38.0,
        state_path=str(tmp_path / "state.json"),
    )
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    first = check_alerts(db_path, config, now=now)
    second = check_alerts(db_path, config, now=now)
    assert len(first) == 1
    assert "fever-range" in first[0].message.lower()
    assert len(second) == 0


def test_low_temp_alert(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", value=34.0)
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://localhost"],
        high_temp_alert_celsius=0,
        low_temp_alert_celsius=35.0,
        state_path=str(tmp_path / "state.json"),
    )
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, now=now)
    assert len(alerts) == 1
    assert "hypothermia-range" in alerts[0].message.lower()


def test_separate_addresses_tracked_independently(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", address=_ADDRESS, value=39.6)
    _record(store, "2026-01-01T00:00:00+00:00", address=_OTHER_ADDRESS, value=36.8)
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://localhost"],
        high_temp_alert_celsius=38.0,
        state_path=str(tmp_path / "state.json"),
    )
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, now=now)
    assert len(alerts) == 1
    assert _ADDRESS in alerts[0].message


def test_profile_apprise_urls_override_routes_to_profile_only(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Alice", value=39.6)
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://shared"],
        high_temp_alert_celsius=38.0,
        state_path=str(tmp_path / "state.json"),
    )
    alice = replace(DEFAULT_PATIENT_CONFIG, apprise_urls=["json://alice-phone"])
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, profile_configs={"Alice": alice}, now=now)
    assert len(alerts) == 1
    assert alerts[0].urls == ["json://alice-phone"]
    assert "Alice" in alerts[0].message


def test_profile_without_override_falls_back_to_global_urls(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Bob", value=39.6)
    store.close()

    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://shared"],
        high_temp_alert_celsius=38.0,
        state_path=str(tmp_path / "state.json"),
    )
    bob = replace(DEFAULT_PATIENT_CONFIG)  # no apprise_urls override
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, profile_configs={"Bob": bob}, now=now)
    assert len(alerts) == 1
    assert alerts[0].urls == ["json://shared"]


def test_profile_stale_after_days_override(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Alice")
    store.close()

    # Global staleness check disabled; Alice overrides it to 1 day.
    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://shared"],
        stale_after_days=0,
        state_path=str(tmp_path / "state.json"),
    )
    alice = replace(DEFAULT_PATIENT_CONFIG, stale_after_days=1)
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, profile_configs={"Alice": alice}, now=now)
    assert len(alerts) == 1
    assert "No reading" in alerts[0].message


def test_profile_high_temp_alert_celsius_override(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    _record(store, "2026-01-01T00:00:00+00:00", profile="Alice", value=39.0)
    store.close()

    # Global high-temp threshold effectively disabled by being set very
    # high; Alice overrides it to a value her reading actually crosses.
    config = replace(
        DEFAULT_ALERT_CONFIG,
        enabled=True,
        apprise_urls=["json://shared"],
        high_temp_alert_celsius=100.0,
        state_path=str(tmp_path / "state.json"),
    )
    alice = replace(DEFAULT_PATIENT_CONFIG, high_temp_alert_celsius=38.0)
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    alerts = check_alerts(db_path, config, profile_configs={"Alice": alice}, now=now)
    assert len(alerts) == 1
    assert "fever-range" in alerts[0].message.lower()
    assert "Alice" in alerts[0].message
