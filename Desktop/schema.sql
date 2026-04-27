PRAGMA foreign_keys = ON;

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
