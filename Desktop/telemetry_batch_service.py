from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlite_store import TelemetrySQLiteStore


class TelemetryBatchService:
    """
    Validate and normalize telemetry batches before persisting them atomically.

    This class keeps batch-specific behavior away from Flask route handlers, so the
    HTTP layer stays compact and easier to test.
    """

    # Assignment requires accepting 1..1000 records in a single request.
    MIN_BATCH_SIZE = 1
    MAX_BATCH_SIZE = 1000

    def __init__(self, db_store: TelemetrySQLiteStore) -> None:
        self._db_store = db_store

    @staticmethod
    def _parse_iso8601(value: Any) -> str | None:
        """
        Parse timestamp text and normalize to UTC with "Z" suffix.

        Returning None indicates invalid format/type according to validation rules.
        """
        if not isinstance(value, str):
            return None

        text = value.strip()
        if not text:
            return None

        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None

        # Accept naive timestamps but treat them as UTC for consistent storage.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)

        return parsed.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        """Parse integer values used by uptime/measure-period fields."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        """Parse temperature to float; None means invalid input."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _validate_record(self, record: Any, index: int) -> tuple[dict[str, Any] | None, str | None]:
        """
        Validate one telemetry object and return normalized data for DB insertion.

        The return contract is `(normalized_record, error_message)` where exactly one
        value is non-None.
        """
        if not isinstance(record, dict):
            return None, f"Record at index {index} is not a JSON object."

        # Required field: textual device identifier.
        device_name = str(record.get("device", "")).strip()
        if not device_name:
            return None, "Missing required field 'device'."

        # Required field: timestamp in ISO 8601.
        timestamp = self._parse_iso8601(record.get("timestamp"))
        if timestamp is None:
            return None, "Missing or invalid required field 'timestamp' (ISO 8601)."

        # Required field: measurement period must be a positive integer.
        measure_period = self._parse_int(record.get("measure-period"))
        if measure_period is None or measure_period <= 0:
            return None, "Missing or invalid required field 'measure-period' (integer > 0)."

        # Required field: temperature must be numeric.
        temperature = self._parse_float(record.get("temperature"))
        if temperature is None:
            return None, "Missing or invalid required field 'temperature'."

        # Optional field: uptime, with identical fallback behavior as single POST.
        uptime = self._parse_int(record.get("uptime"))
        if uptime is None:
            last_uptime = self._db_store.get_device_last_uptime(device_name)
            uptime = 0 if last_uptime is None else last_uptime

        if uptime < 0:
            return None, "Field 'uptime' must be >= 0."

        normalized = {
            "device_name": device_name,
            "measured_at_iso8601": timestamp,
            "temperature_c": temperature,
            "uptime_seconds": uptime,
            "measurement_period_seconds": measure_period,
        }
        return normalized, None

    def validate_and_insert_batch(self, payload: Any) -> tuple[int | None, dict[str, Any] | None]:
        """
        Validate the full batch first, then insert all rows in a single transaction.

        Returns `(inserted_count, error)`:
        - on success: `(N, None)`
        - on validation failure: `(None, error_dict)`
        """
        if not isinstance(payload, list):
            return None, {
                "status": "error",
                "message": "Request body must be a JSON array.",
                "error_index": None,
                "error": "Invalid payload type.",
            }

        batch_size = len(payload)
        if batch_size < self.MIN_BATCH_SIZE or batch_size > self.MAX_BATCH_SIZE:
            return None, {
                "status": "error",
                "message": f"Batch size must be between {self.MIN_BATCH_SIZE} and {self.MAX_BATCH_SIZE} records.",
                "error_index": None,
                "error": "Invalid batch size.",
            }

        # First pass: validate everything and locate the first invalid index, if any.
        normalized_records: list[dict[str, Any]] = []
        for index, record in enumerate(payload):
            normalized, error_message = self._validate_record(record, index)
            if error_message is not None:
                return None, {
                    "status": "error",
                    "message": "Batch validation failed.",
                    "error_index": index,
                    "error": error_message,
                }

            if normalized is None:
                return None, {
                    "status": "error",
                    "message": "Batch validation failed.",
                    "error_index": index,
                    "error": "Unknown validation error.",
                }

            normalized_records.append(normalized)

        # Second pass: one DB transaction for all rows (all-or-nothing).
        self._db_store.save_telemetry_batch(normalized_records)
        return len(normalized_records), None
