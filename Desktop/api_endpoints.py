from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, request

from sqlite_store import TelemetrySQLiteStore
from telemetry_batch_service import TelemetryBatchService


class RestApiEndpoints:
    """
    Class that encapsulates all REST endpoints required by the assignment.

    Keeping routes in a dedicated class/file makes the HTTP API easier to read,
    test and maintain independently from MQTT/dashboard logic in server.py.
    """

    def __init__(self, db_store: TelemetrySQLiteStore) -> None:
        self._db_store = db_store
        # Keep batch validation/insertion logic separate from route glue code.
        self._batch_service = TelemetryBatchService(db_store)

    def register(self, app: Flask) -> None:
        """Register all REST routes on an existing Flask app instance."""

        # Device endpoints.
        app.add_url_rule("/api/devices", view_func=self.get_devices, methods=["GET"])
        app.add_url_rule("/api/devices/<string:device_id>", view_func=self.get_device_detail, methods=["GET"])
        app.add_url_rule("/api/devices/<string:device_id>", view_func=self.delete_device, methods=["DELETE"])

        # Telemetry endpoints.
        app.add_url_rule("/api/telemetry", view_func=self.list_telemetry, methods=["GET"])
        app.add_url_rule("/api/telemetry/<int:measurement_id>", view_func=self.get_telemetry, methods=["GET"])
        app.add_url_rule("/api/telemetry/<int:measurement_id>", view_func=self.delete_telemetry, methods=["DELETE"])
        app.add_url_rule("/api/telemetry", view_func=self.create_telemetry, methods=["POST"])
        app.add_url_rule("/api/telemetry/batch", view_func=self.create_telemetry_batch, methods=["POST"])

    @staticmethod
    def _json_error(message: str, status_code: int) -> tuple[Any, int]:
        """Return JSON error response in one place for consistent API output."""
        return jsonify({"status": "error", "message": message}), status_code

    @staticmethod
    def _parse_iso8601(value: Any) -> str | None:
        """
        Validate timestamp from API payload and normalize it to UTC ISO 8601.

        Output format always uses "Z" suffix to keep timestamps consistent.
        """
        if not isinstance(value, str):
            return None

        text = value.strip()
        if not text:
            return None

        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        """Best-effort int parser; None means invalid input."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        """Best-effort float parser; None means invalid input."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_telemetry_response_with_counts(
        telemetry: list[dict[str, Any]],
        current_count: int,
        total_count: int,
    ) -> Response:
        """
        Build JSON response and append mandatory count headers.

        Assignment text contains "X-Total –Count" with a typographical dash,
        but valid HTTP header name is emitted as "X-Total-Count".
        """
        response = jsonify({"status": "ok", "count": current_count, "telemetry": telemetry})
        response.headers["X-Current-Count"] = str(current_count)
        response.headers["X-Total-Count"] = str(total_count)
        return response

    def list_telemetry(self) -> tuple[Any, int] | Any:
        """
        GET /api/telemetry

        Supported query params:
        - device_id: textual device identifier filter
        - from: ISO 8601 lower timestamp bound
        - to: ISO 8601 upper timestamp bound

        Supported request headers:
        - X-Sort-Field: timestamp | temperature (default: timestamp)
        - X-Sort-Order: asc | desc (default: desc)
        """
        device_id_raw = request.args.get("device_id")
        from_raw = request.args.get("from")
        to_raw = request.args.get("to")

        device_id = None
        if device_id_raw is not None:
            normalized_device_id = str(device_id_raw).strip()
            device_id = normalized_device_id if normalized_device_id else None

        from_iso8601 = None
        if from_raw is not None:
            from_iso8601 = self._parse_iso8601(from_raw)
            if from_iso8601 is None:
                return self._json_error("Invalid query parameter 'from'. Expected ISO 8601.", 400)

        to_iso8601 = None
        if to_raw is not None:
            to_iso8601 = self._parse_iso8601(to_raw)
            if to_iso8601 is None:
                return self._json_error("Invalid query parameter 'to'. Expected ISO 8601.", 400)

        if from_iso8601 is not None and to_iso8601 is not None and from_iso8601 > to_iso8601:
            return self._json_error(
                "Invalid time range: query parameter 'from' must be less than or equal to 'to'.",
                400,
            )

        sort_field = str(request.headers.get("X-Sort-Field", "timestamp")).strip().lower() or "timestamp"
        if sort_field not in {"timestamp", "temperature"}:
            return self._json_error(
                "Invalid header 'X-Sort-Field'. Allowed values: timestamp, temperature.",
                400,
            )

        sort_order = str(request.headers.get("X-Sort-Order", "desc")).strip().lower() or "desc"
        if sort_order not in {"asc", "desc"}:
            return self._json_error("Invalid header 'X-Sort-Order'. Allowed values: asc, desc.", 400)

        telemetry = self._db_store.list_measurements(
            device_id=device_id,
            from_iso8601=from_iso8601,
            to_iso8601=to_iso8601,
            sort_field=sort_field,
            sort_order=sort_order,
        )
        current_count = len(telemetry)
        total_count = self._db_store.count_all_measurements()

        return self._build_telemetry_response_with_counts(telemetry, current_count, total_count)

    def get_devices(self) -> tuple[Any, int] | Any:
        """
        GET /api/devices

        Returns all detected devices. Assignment requires 4xx if no devices exist.
        """
        devices = self._db_store.list_devices()
        if not devices:
            return self._json_error("No devices found.", 404)

        return jsonify({"status": "ok", "count": len(devices), "devices": devices})

    def get_device_detail(self, device_id: str) -> tuple[Any, int] | Any:
        """
        GET /api/devices/<device_id>

        In this project device_id is the textual device identifier (device_name).
        """
        device = self._db_store.get_device(device_id)
        if device is None:
            return self._json_error(f"Device '{device_id}' not found.", 404)

        return jsonify({"status": "ok", "device": device})

    def get_telemetry(self, measurement_id: int) -> tuple[Any, int] | Any:
        """GET /api/telemetry/<id> - Return one telemetry row by numeric id."""
        telemetry = self._db_store.get_measurement(measurement_id)
        if telemetry is None:
            return self._json_error(f"Telemetry with id={measurement_id} not found.", 404)

        return jsonify({"status": "ok", "telemetry": telemetry})

    def delete_telemetry(self, measurement_id: int) -> tuple[Any, int] | Any:
        """DELETE /api/telemetry/<id> - Delete one telemetry row by numeric id."""
        deleted = self._db_store.delete_measurement(measurement_id)
        if deleted is None:
            return self._json_error(f"Telemetry with id={measurement_id} not found.", 404)

        return jsonify({"status": "ok", "deleted": deleted})

    def delete_device(self, device_id: str) -> tuple[Any, int] | Any:
        """
        DELETE /api/devices/<device_id>

        Database schema already has ON DELETE CASCADE on measurements, so deleting
        the device removes all related telemetry rows automatically.
        """
        deleted = self._db_store.delete_device(device_id)
        if deleted is None:
            return self._json_error(f"Device '{device_id}' not found.", 404)

        return jsonify({"status": "ok", "deleted": deleted})

    def create_telemetry(self) -> tuple[Any, int] | Any:
        """
        POST /api/telemetry

        Expected payload keys:
        - device (required)
        - timestamp (required, ISO 8601)
        - measure-period (required, integer > 0)
        - temperature (required, number)
        - uptime (optional; fallback logic implemented below)
        - led (optional; accepted and returned, but not persisted in SQLite schema)
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self._json_error("Request body must be a JSON object.", 400)

        # Required field: device
        device = str(payload.get("device", "")).strip()
        if not device:
            return self._json_error("Missing required field 'device'.", 400)

        # Required field: timestamp
        timestamp = self._parse_iso8601(payload.get("timestamp"))
        if timestamp is None:
            return self._json_error("Missing or invalid required field 'timestamp' (ISO 8601).", 400)

        # Required field: measure-period
        measure_period = self._parse_int(payload.get("measure-period"))
        if measure_period is None or measure_period <= 0:
            return self._json_error("Missing or invalid required field 'measure-period' (integer > 0).", 400)

        # Required field: temperature
        temperature = self._parse_float(payload.get("temperature"))
        if temperature is None:
            return self._json_error("Missing or invalid required field 'temperature'.", 400)

        # Optional field: uptime
        # Assignment asks for logic when uptime is not present in payload.
        # We use previously known device uptime when available, otherwise 0.
        uptime = self._parse_int(payload.get("uptime"))
        uptime_source = "payload"
        if uptime is None:
            last_uptime = self._db_store.get_device_last_uptime(device)
            if last_uptime is None:
                uptime = 0
                uptime_source = "default-0"
            else:
                uptime = last_uptime
                uptime_source = "last-known-device-value"

        if uptime < 0:
            return self._json_error("Field 'uptime' must be >= 0.", 400)

        # Optional field: led (accepted even though schema has no LED column).
        led = payload.get("led")

        self._db_store.save_telemetry_message(
            device_name=device,
            measured_at_iso8601=timestamp,
            temperature_c=temperature,
            uptime_seconds=uptime,
            measurement_period_seconds=measure_period,
        )

        # Return created response with normalized payload and uptime decision info.
        response_body = {
            "status": "created",
            "telemetry": {
                "device": device,
                "timestamp": timestamp,
                "measure-period": measure_period,
                "temperature": temperature,
                "uptime": uptime,
                "led": led,
            },
            "uptime_source": uptime_source,
        }
        return jsonify(response_body), 201

    def create_telemetry_batch(self) -> tuple[Any, int] | Any:
        """
        POST /api/telemetry/batch

        Accepts JSON array containing 1..1000 telemetry records.
        Batch is inserted in a single transaction, so either every valid row is
        stored or none are stored.
        """
        payload = request.get_json(silent=True)

        try:
            inserted_count, error = self._batch_service.validate_and_insert_batch(payload)
        except Exception as exc:
            # Any unexpected DB/runtime failure should produce a clear 5xx response.
            return self._json_error(f"Batch insert failed: {exc}", 500)

        if error is not None:
            return jsonify(error), 400

        return (
            jsonify(
                {
                    "status": "ok",
                    "inserted_count": inserted_count,
                }
            ),
            201,
        )