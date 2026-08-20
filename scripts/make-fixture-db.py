#!/usr/bin/env python3
"""Create a tiny fixture SQLite database for smoke/CI testing.

The schema here is duplicated from storage.py's _SCHEMA rather than
imported, so this script has no dependency on the package being installed.
Keep the two in sync if the readings table's columns change.
"""

import sqlite3
import sys
from datetime import datetime, timezone


def main() -> None:
    db_path = sys.argv[1]
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE readings (
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
        )
        """
    )
    con.execute(
        "INSERT INTO readings "
        "(recorded_at, measured_at, address, profile, value, unit, "
        "temperature_type, battery_percent, error_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            None,
            "AA:BB:CC:DD:EE:FF",
            None,
            37.0,
            "C",
            "Ear (usually ear lobe)",
            90,
            None,
        ),
    )
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
