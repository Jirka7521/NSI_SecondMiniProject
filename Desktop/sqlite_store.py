from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


_DEFAULT_SCHEMA_SQL = """PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT NOT NULL UNIQUE,
    first_seen_at_iso8601 TEXT NOT NULL,
    last_seen_at_iso8601 TEXT NOT NULL,
    last_uptime_seconds INTEGER NOT NULL DEFAULT 0,
    measurement_period_seconds INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS measurements (
    measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    temperature_c REAL NOT NULL,
    measured_at_iso8601 TEXT NOT NULL,
    created_at_iso8601 TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (device_id) REFERENCES devices(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_measurements_device_id ON measurements (device_id);
CREATE INDEX IF NOT EXISTS idx_measurements_measured_at ON measurements (measured_at_iso8601);
"""


class TelemetrySQLiteStore:
    """SQLite persistence for device registry and telemetry measurements."""

    _REQUIRED_DEVICE_COLUMNS = {
        "id",
        "device_name",
        "first_seen_at_iso8601",
        "last_seen_at_iso8601",
        "last_uptime_seconds",
        "measurement_period_seconds",
        "message_count",
    }
    _REQUIRED_MEASUREMENT_COLUMNS = {
        "measurement_id",
        "device_id",
        "temperature_c",
        "measured_at_iso8601",
        "created_at_iso8601",
    }

    def __init__(self, db_path: Path, schema_path: Path) -> None:
        self._db_path = db_path
        self._schema_path = schema_path
        self._lock = threading.Lock()

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema_file_exists()
        self._initialize_database()

    def _get_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.execute("PRAGMA foreign_keys = ON;")
        return connection

    def _ensure_schema_file_exists(self) -> None:
        if self._schema_path.exists():
            return

        self._schema_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_path.write_text(_DEFAULT_SCHEMA_SQL, encoding="utf-8")

    def _initialize_database(self) -> None:
        schema_sql = self._schema_path.read_text(encoding="utf-8")
        try:
            with self._get_connection() as connection:
                connection.executescript(schema_sql)
                self._validate_schema(connection)
        except RuntimeError as exc:
            self._recreate_database(schema_sql, str(exc))

    def _recreate_database(self, schema_sql: str, reason: str) -> None:
        """Rebuild DB file when schema is incompatible with required structure."""
        if self._db_path.exists():
            backup_path = self._db_path.with_suffix(f"{self._db_path.suffix}.invalid")
            counter = 1
            while backup_path.exists():
                backup_path = self._db_path.with_suffix(f"{self._db_path.suffix}.invalid{counter}")
                counter += 1

            self._db_path.replace(backup_path)
            print(f"[DB] Recreated SQLite DB because schema was invalid: {reason}")
            print(f"[DB] Previous DB moved to: {backup_path}")

        with self._get_connection() as connection:
            connection.executescript(schema_sql)
            self._validate_schema(connection)

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        device_columns = self._read_table_columns(connection, "devices")
        measurement_columns = self._read_table_columns(connection, "measurements")

        missing_device = self._REQUIRED_DEVICE_COLUMNS.difference(device_columns)
        missing_measurement = self._REQUIRED_MEASUREMENT_COLUMNS.difference(measurement_columns)
        if missing_device or missing_measurement:
            raise RuntimeError(
                "SQLite schema is invalid. Missing columns: "
                f"devices={sorted(missing_device)}, measurements={sorted(missing_measurement)}"
            )

        if not self._has_measurements_foreign_key(connection):
            raise RuntimeError("SQLite schema is invalid. measurements.device_id foreign key is missing.")

    @staticmethod
    def _read_table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table_name});").fetchall()
        return {str(row[1]) for row in rows}

    @staticmethod
    def _has_measurements_foreign_key(connection: sqlite3.Connection) -> bool:
        rows = connection.execute("PRAGMA foreign_key_list(measurements);").fetchall()
        for row in rows:
            target_table = str(row[2])
            from_column = str(row[3])
            to_column = str(row[4])
            if target_table == "devices" and from_column == "device_id" and to_column == "id":
                return True
        return False

    def save_telemetry_message(
        self,
        *,
        device_name: str,
        measured_at_iso8601: str,
        temperature_c: float,
        uptime_seconds: int,
        measurement_period_seconds: int,
    ) -> None:
        with self._lock:
            with self._get_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO devices (
                        device_name,
                        first_seen_at_iso8601,
                        last_seen_at_iso8601,
                        last_uptime_seconds,
                        measurement_period_seconds,
                        message_count
                    )
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(device_name)
                    DO UPDATE SET
                        last_seen_at_iso8601 = excluded.last_seen_at_iso8601,
                        last_uptime_seconds = excluded.last_uptime_seconds,
                        measurement_period_seconds = excluded.measurement_period_seconds,
                        message_count = devices.message_count + 1
                    """,
                    (
                        device_name,
                        measured_at_iso8601,
                        measured_at_iso8601,
                        uptime_seconds,
                        measurement_period_seconds,
                    ),
                )

                row = connection.execute(
                    "SELECT id FROM devices WHERE device_name = ?",
                    (device_name,),
                ).fetchone()
                if not row:
                    raise RuntimeError(f"Could not resolve numeric device id for '{device_name}'.")
                numeric_device_id = int(row[0])

                connection.execute(
                    """
                    INSERT INTO measurements (
                        device_id,
                        temperature_c,
                        measured_at_iso8601
                    )
                    VALUES (?, ?, ?)
                    """,
                    (numeric_device_id, temperature_c, measured_at_iso8601),
                )

    def get_last_known_period_seconds(self, device_name: str) -> int | None:
        """Return the last known period for a device, if present in registry."""
        with self._lock:
            with self._get_connection() as connection:
                row = connection.execute(
                    "SELECT measurement_period_seconds FROM devices WHERE device_name = ?",
                    (device_name,),
                ).fetchone()

        if not row:
            return None

        try:
            value = int(row[0])
        except (TypeError, ValueError):
            return None

        return value if value > 0 else None
