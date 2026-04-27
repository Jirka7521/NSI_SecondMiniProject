from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request
from uuid import uuid4


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Base URL of the running Flask server.
# If your server runs on different host/port, adjust this value.
API_BASE_URL = "http://127.0.0.1:5000"
BATCH_ENDPOINT = f"{API_BASE_URL}/api/telemetry/batch"

# Resolve DB path relative to this script so it works from any CWD.
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "telemetry.sqlite3"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def iso_at(offset_seconds: int) -> str:
    """Return a deterministic UTC ISO-8601 timestamp with Z suffix."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.isoformat().replace("+00:00", "Z")


def post_json(url: str, payload: Any) -> tuple[int, Any]:
    """
    Send JSON request via stdlib urllib and return (HTTP status, parsed body).

    We intentionally use stdlib only, so this script needs no extra dependency.
    """
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )

    try:
        with request.urlopen(req, timeout=10) as response:
            status_code = int(response.status)
            response_raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(response_raw) if response_raw else None
            return status_code, parsed
    except error.HTTPError as exc:
        # Even for 4xx/5xx responses we want body details for diagnostics.
        response_raw = exc.read().decode("utf-8", errors="replace")
        parsed = json.loads(response_raw) if response_raw else None
        return int(exc.code), parsed


def cleanup_test_devices(device_names: list[str]) -> None:
    """
    Delete previous test devices by name.

    Thanks to FK cascade in schema, deleting device also removes its measurements.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executemany("DELETE FROM devices WHERE device_name = ?", [(name,) for name in device_names])


def count_measurements_for_device(device_name: str) -> int:
    """Count rows in measurements table for one textual device id."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM measurements m
            JOIN devices d ON d.id = m.device_id
            WHERE d.device_name = ?
            """,
            (device_name,),
        ).fetchone()

    return int(row[0]) if row else 0


# -----------------------------------------------------------------------------
# Demo scenario
# -----------------------------------------------------------------------------


def main() -> None:
    # Use unique device ids every run to keep output understandable and isolated.
    run_id = uuid4().hex[:8]
    valid_device = f"batch-valid-{run_id}"
    invalid_device = f"batch-invalid-{run_id}"

    print("=== Batch endpoint test ===")
    print(f"Endpoint: {BATCH_ENDPOINT}")
    print(f"DB path : {DB_PATH}")

    # Clean any leftovers (mostly useful when script is rerun quickly).
    cleanup_test_devices([valid_device, invalid_device])

    # 1) SUCCESS branch: complete valid batch should be inserted.
    valid_batch = [
        {
            "device": valid_device,
            "timestamp": iso_at(0),
            "measure-period": 5,
            "temperature": 22.5,
            "uptime": 10,
        },
        {
            "device": valid_device,
            "timestamp": iso_at(1),
            "measure-period": 5,
            "temperature": 22.9,
            "uptime": 15,
        },
        {
            "device": valid_device,
            "timestamp": iso_at(2),
            "measure-period": 5,
            "temperature": 23.2,
            "uptime": 20,
        },
    ]

    success_status, success_body = post_json(BATCH_ENDPOINT, valid_batch)
    print("\n[1] Valid batch result")
    print(f"HTTP status: {success_status}")
    print(f"Body      : {json.dumps(success_body, ensure_ascii=False)}")

    valid_count = count_measurements_for_device(valid_device)
    print(f"DB count for {valid_device}: {valid_count} (expected 3)")

    # 2) REJECT branch: invalid record in the middle must cancel whole transaction.
    invalid_batch = [
        {
            "device": invalid_device,
            "timestamp": iso_at(10),
            "measure-period": 7,
            "temperature": 20.1,
            "uptime": 100,
        },
        {
            # This is the intentionally broken row in the middle of batch.
            # Timestamp is invalid, so API should reject entire batch and report index 1.
            "device": invalid_device,
            "timestamp": "not-a-valid-iso-timestamp",
            "measure-period": 7,
            "temperature": 20.2,
            "uptime": 110,
        },
        {
            "device": invalid_device,
            "timestamp": iso_at(12),
            "measure-period": 7,
            "temperature": 20.3,
            "uptime": 120,
        },
    ]

    reject_status, reject_body = post_json(BATCH_ENDPOINT, invalid_batch)
    print("\n[2] Invalid batch result")
    print(f"HTTP status: {reject_status}")
    print(f"Body      : {json.dumps(reject_body, ensure_ascii=False)}")

    # Critical requirement check: after rejected batch, DB must contain zero rows
    # for this device (no partial insert of the first valid row is allowed).
    invalid_count = count_measurements_for_device(invalid_device)
    print("\n[3] Atomicity verification after rejected batch")
    print(f"DB count for {invalid_device}: {invalid_count} (expected 0)")

    if reject_status < 400:
        print("WARNING: invalid batch was expected to fail with 4xx but it did not.")

    if invalid_count != 0:
        print("WARNING: DB contains partial data after rejected batch.")
    else:
        print("OK: rejected batch left no partial data in DB.")


if __name__ == "__main__":
    main()
