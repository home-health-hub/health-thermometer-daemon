from datetime import datetime, timedelta, timezone

from health_thermometer_daemon.prune import count_old_rows, delete_old_rows
from health_thermometer_daemon.storage import ReadingStore

_ADDRESS = "AA:BB:CC:DD:EE:FF"


def _record(store, recorded_at):
    store.record(
        recorded_at=recorded_at,
        measured_at=None,
        address=_ADDRESS,
        profile=None,
        value=37.0,
        unit="C",
        temperature_type=None,
        battery_percent=None,
        error_code=None,
    )


def test_count_and_delete_old_rows(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    now = datetime.now(timezone.utc)
    _record(store, (now - timedelta(days=400)).isoformat())
    _record(store, (now - timedelta(days=1)).isoformat())
    store.close()

    cutoff = now - timedelta(days=365)
    assert count_old_rows(db_path, cutoff, None) == 1

    deleted = delete_old_rows(db_path, cutoff, None)
    assert deleted == 1
    assert count_old_rows(db_path, cutoff, None) == 0


def test_count_and_delete_old_rows_restricted_by_address(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    now = datetime.now(timezone.utc)
    old_at = (now - timedelta(days=400)).isoformat()
    store.record(
        recorded_at=old_at,
        measured_at=None,
        address=_ADDRESS,
        profile=None,
        value=37.0,
        unit="C",
        temperature_type=None,
        battery_percent=None,
        error_code=None,
    )
    store.record(
        recorded_at=old_at,
        measured_at=None,
        address="11:22:33:44:55:66",
        profile=None,
        value=37.0,
        unit="C",
        temperature_type=None,
        battery_percent=None,
        error_code=None,
    )
    store.close()

    cutoff = now - timedelta(days=365)
    assert count_old_rows(db_path, cutoff, _ADDRESS) == 1
    deleted = delete_old_rows(db_path, cutoff, _ADDRESS)
    assert deleted == 1
    assert count_old_rows(db_path, cutoff, None) == 1
