from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request

from sqlite_store import TelemetrySQLiteStore


class DashboardPageController:
    """
    Register and implement dashboard routes for temperature history visualization.

    This controller keeps all dashboard-specific endpoints in one class so that
    `server.py` remains focused on MQTT/live data concerns.
    """

    _RELATIVE_UNITS_TO_SECONDS = {
        "second": 1,
        "minute": 60,
        "hour": 60 * 60,
        "day": 24 * 60 * 60,
    }

    def __init__(self, db_store: TelemetrySQLiteStore) -> None:
        self._db_store = db_store

    def register(self, app: Flask) -> None:
        """Register dashboard page and supporting JSON API routes."""
        app.add_url_rule("/dashboard", view_func=self.dashboard_page, methods=["GET"])
        app.add_url_rule("/api/dashboard/devices", view_func=self.get_dashboard_devices, methods=["GET"])
        app.add_url_rule(
            "/api/dashboard/temperature-history",
            view_func=self.get_temperature_history,
            methods=["GET"],
        )

    @staticmethod
    def _json_error(message: str, status_code: int) -> tuple[Any, int]:
        """Build consistent JSON error payload for dashboard API responses."""
        return jsonify({"status": "error", "message": message}), status_code

    @staticmethod
    def _to_z_iso8601(value: datetime) -> str:
        """Convert timezone-aware datetime to canonical UTC string with Z suffix."""
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_iso8601(value: Any) -> datetime | None:
        """Parse ISO-8601 timestamp and normalize it to UTC datetime."""
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

        if parsed.tzinfo is None:
            # Keep behavior explicit: naive timestamps are interpreted as UTC.
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)

        return parsed

    @staticmethod
    def _parse_positive_int(value: Any) -> int | None:
        """Parse positive integer from query value; None means invalid input."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None

        if parsed <= 0:
            return None

        return parsed

    def dashboard_page(self) -> str:
        """Render CSR dashboard shell. Data is loaded with JavaScript calls."""
        return render_template(
            "dashboard.html",
            dashboard_devices_api_path="/api/dashboard/devices",
            dashboard_history_api_path="/api/dashboard/temperature-history",
        )

    def get_dashboard_devices(self) -> Any:
        """
        Return registered devices for dashboard selector.

        Frontend only needs textual ids, but we also return metadata to keep
        the endpoint useful for future UI improvements.
        """
        devices = self._db_store.list_devices()
        device_items = [
            {
                "device_id": str(device["device_id"]),
                "first_seen_at": str(device["first_seen_at"]),
                "last_seen_at": str(device["last_seen_at"]),
                "message_count": int(device["message_count"]),
            }
            for device in devices
        ]

        return jsonify({"status": "ok", "count": len(device_items), "devices": device_items})

    def _resolve_time_range_from_request(self) -> tuple[str, str] | tuple[None, tuple[Any, int]]:
        """
        Build a from/to interval from query parameters.

        Supported modes:
        - absolute: explicit `from` and `to` timestamps
        - relative: `to=now` and user-selected `window_value` + `window_unit`
        """
        mode = str(request.args.get("mode", "relative")).strip().lower() or "relative"

        if mode == "absolute":
            from_dt = self._parse_iso8601(request.args.get("from"))
            to_dt = self._parse_iso8601(request.args.get("to"))

            if from_dt is None:
                return None, self._json_error("Invalid or missing query parameter 'from'.", 400)
            if to_dt is None:
                return None, self._json_error("Invalid or missing query parameter 'to'.", 400)
        elif mode == "relative":
            raw_unit = str(request.args.get("window_unit", "minute")).strip().lower() or "minute"
            unit_seconds = self._RELATIVE_UNITS_TO_SECONDS.get(raw_unit)
            if unit_seconds is None:
                allowed = ", ".join(sorted(self._RELATIVE_UNITS_TO_SECONDS.keys()))
                return None, self._json_error(f"Invalid window_unit. Allowed values: {allowed}.", 400)

            window_value = self._parse_positive_int(request.args.get("window_value"))
            if window_value is None:
                return None, self._json_error("Invalid or missing window_value. Use integer > 0.", 400)

            to_dt = datetime.now(tz=timezone.utc)
            from_dt = to_dt - timedelta(seconds=window_value * unit_seconds)
        else:
            return None, self._json_error("Invalid mode. Allowed values: absolute, relative.", 400)

        if from_dt > to_dt:
            return None, self._json_error("Invalid range: 'from' must be less than or equal to 'to'.", 400)

        return self._to_z_iso8601(from_dt), self._to_z_iso8601(to_dt)

    def get_temperature_history(self) -> tuple[Any, int] | Any:
        """
        Return temperature history for selected device and selected time window.

        Query params:
        - device_id (required)
        - mode=absolute -> requires from,to
        - mode=relative -> requires window_value,window_unit (second/minute/hour/day)
        """
        device_id = str(request.args.get("device_id", "")).strip()
        if not device_id:
            return self._json_error("Missing query parameter 'device_id'.", 400)

        # Validate that user-selected device exists in registry.
        if self._db_store.get_device(device_id) is None:
            return self._json_error(f"Device '{device_id}' not found.", 404)

        resolved_range = self._resolve_time_range_from_request()
        if isinstance(resolved_range[0], type(None)):
            return resolved_range[1]

        from_iso8601, to_iso8601 = resolved_range

        # Sort by timestamp ascending to render chart left->right chronologically.
        rows = self._db_store.list_measurements(
            device_id=device_id,
            from_iso8601=from_iso8601,
            to_iso8601=to_iso8601,
            sort_field="timestamp",
            sort_order="asc",
        )

        points = [
            {
                "timestamp": str(row["timestamp"]),
                "temperature": float(row["temperature"]),
            }
            for row in rows
        ]

        return jsonify(
            {
                "status": "ok",
                "device_id": device_id,
                "from": from_iso8601,
                "to": to_iso8601,
                "count": len(points),
                "points": points,
            }
        )
