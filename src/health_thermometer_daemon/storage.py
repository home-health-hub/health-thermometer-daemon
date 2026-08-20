"""SQLite storage backend for temperature readings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    measured_at TEXT,
    address TEXT NOT NULL,
    profile TEXT,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    temperature_type TEXT,
    battery_percent INTEGER,
    error_code TEXT
);
"""


def ensure_schema(db_path: str) -> None:
    """Create the readings table if it doesn't already exist.

    Safe to call from any entry point (daemon, API server, etc.) regardless
    of whether the database file already exists or which one touches it
    first.

    Args:
        db_path: Filesystem path to the SQLite database file. Parent
            directories are created automatically if missing.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def get_distinct_profiles(db_path: str) -> set[str]:
    """Return the distinct non-null profile tags actually used in the database.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        The set of distinct profile names, empty if none are tagged yet or
        the ``readings`` table doesn't exist.
    """
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT profile FROM readings WHERE profile IS NOT NULL"
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        connection.close()


def get_reading_recorded_at(db_path: str, row_id: int) -> str | None:
    """Look up a reading's recorded_at timestamp, without modifying it.

    Args:
        db_path: Filesystem path to the SQLite database file.
        row_id: The reading's primary key, as returned by ``record()``.

    Returns:
        The stored ISO-8601 ``recorded_at`` string, or None if no row
        matches ``row_id``.
    """
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT recorded_at FROM readings WHERE id = ?", (row_id,)
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        connection.close()


def set_reading_profile(db_path: str, row_id: int, profile: str) -> bool:
    """Tag a previously recorded reading with a profile name.

    Args:
        db_path: Filesystem path to the SQLite database file.
        row_id: The reading's primary key, as returned by ``record()``.
        profile: The profile name to assign.

    Returns:
        True if a row was updated, False if no row matched ``row_id``.
    """
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            "UPDATE readings SET profile = ? WHERE id = ?", (profile, row_id)
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


class ReadingStore:
    """Persists temperature readings to a local SQLite database.

    Args:
        db_path: Filesystem path to the SQLite database file. Parent
            directories are created automatically if missing.
    """

    def __init__(self, db_path: str) -> None:
        ensure_schema(db_path)
        self._connection = sqlite3.connect(db_path)

    def record(
        self,
        recorded_at: str,
        measured_at: str | None,
        address: str,
        profile: str | None,
        value: float,
        unit: str,
        temperature_type: str | None,
        battery_percent: int | None,
        error_code: str | None,
    ) -> int:
        """Insert one reading row.

        Args:
            recorded_at: ISO-8601 UTC timestamp of when the daemon received
                the reading -- always present, regardless of whether the
                device itself sent a timestamp.
            measured_at: ISO-8601 timestamp from the device's own optional
                embedded date-time field, or None if the device didn't
                include one (legal per the Health Thermometer Profile spec
                -- most instant-read thermometers likely won't set it).
            address: BLE address of the device that produced the reading.
            profile: Profile name, if already known at insert time.
                Normally None -- like etekcity-bp-daemon, there's no
                per-device "user slot" concept for this device class, so
                profiles are tagged after the fact via
                ``set_reading_profile()`` once ntfy/dunstify gets an
                answer.
            value: Temperature value, exactly as the device reported it --
                no unit conversion happens at storage time.
            unit: "C" or "F", per the device's own Flags byte.
            temperature_type: Optional body-site string (e.g.
                "Ear (usually ear lobe)"), if the device included one.
            battery_percent: Battery level from the device's Battery
                Service, read at the time of this reading, if the device
                exposes it.
            error_code: Reserved for future use -- the driver currently
                just returns None from a failed decode rather than a
                structured error, so this is unused for now.

        Returns:
            The inserted row's primary key.
        """
        cursor = self._connection.execute(
            """
            INSERT INTO readings (
                recorded_at, measured_at, address, profile, value, unit,
                temperature_type, battery_percent, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recorded_at,
                measured_at,
                address,
                profile,
                value,
                unit,
                temperature_type,
                battery_percent,
                error_code,
            ),
        )
        self._connection.commit()
        return cursor.lastrowid

    def close(self) -> None:
        """Close the underlying database connection."""
        self._connection.close()
