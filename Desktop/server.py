from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from paho.mqtt import client as mqtt_client

from api_endpoints import RestApiEndpoints
from sqlite_store import TelemetrySQLiteStore


# -----------------------------------------------------------------------------
# Configuration constants
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE_PATH = BASE_DIR / ".env"

# Load the .env file before reading any environment variables.
load_dotenv(ENV_FILE_PATH)


def get_required_env(name: str) -> str:
    """Return environment variable value or fail fast with clear message."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def get_required_int_env(name: str) -> int:
    """Parse required integer environment variable or fail fast."""
    raw_value = get_required_env(name)
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be integer, got: {raw_value}") from exc


def parse_int(value: Any) -> int | None:
    """Convert a value to int and return None when conversion is not possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> float | None:
    """Convert value to float and return None on parse failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_iso8601(value: Any) -> str | None:
    """Validate and normalize ISO-8601 timestamp into UTC text form."""
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


def parse_device_id_from_topic(topic: str, topic_prefix: str, telemetry_suffix: str) -> str | None:
    """Extract cvut login/device identifier from topic prefix/login/suffix."""
    if not topic.startswith(topic_prefix) or not topic.endswith(telemetry_suffix):
        return None

    middle = topic[len(topic_prefix) : len(topic) - len(telemetry_suffix)]
    if not middle or "/" in middle:
        return None

    return middle


# Flask server settings.
FLASK_HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "true").lower() == "true"

# MQTT connection settings.
MQTT_BROKER_HOST = get_required_env("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = get_required_int_env("MQTT_BROKER_PORT")
MQTT_KEEPALIVE_SECONDS = get_required_int_env("MQTT_KEEPALIVE_SECONDS")
MQTT_LED_COMMAND_TOPIC = get_required_env("MQTT_LED_COMMAND_TOPIC")
MQTT_TELEMETRY_TOPIC = get_required_env("MQTT_TELEMETRY_TOPIC")
MQTT_STATUS_TOPIC = get_required_env("MQTT_STATUS_TOPIC")
MQTT_PERIOD_COMMAND_TOPIC = get_required_env("MQTT_PERIOD_COMMAND_TOPIC")

MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "cvut/nsi/2026/").strip() or "cvut/nsi/2026/"
MQTT_TOPIC_TELEMETRY_SUFFIX = (
    os.environ.get("MQTT_TOPIC_TELEMETRY_SUFFIX", "/telemetry").strip() or "/telemetry"
)
DEFAULT_MQTT_TOPIC_FILTER = f"{MQTT_TOPIC_PREFIX}+{MQTT_TOPIC_TELEMETRY_SUFFIX}"
MQTT_TOPIC_FILTER = os.environ.get("MQTT_TOPIC_FILTER", DEFAULT_MQTT_TOPIC_FILTER).strip() or DEFAULT_MQTT_TOPIC_FILTER

# We intentionally identify test telemetry as a separate producer.
DESKTOP_TEST_DEVICE_ID = os.environ.get("DESKTOP_TEST_DEVICE_ID", "desktop-telemetry-tester")

# Frontend polling interval in milliseconds.
FRONTEND_REFRESH_MS = int(os.environ.get("FRONTEND_REFRESH_MS", "2000"))
MQTT_DEBUG_LOGGING = os.environ.get("MQTT_DEBUG_LOGGING", "true").lower() == "true"
DEVICE_OFFLINE_TIMEOUT_SECONDS = int(os.environ.get("DEVICE_OFFLINE_TIMEOUT_SECONDS", "90"))

# API route constants (configurable via environment variables).
ROUTE_DASHBOARD = os.environ.get("ROUTE_DASHBOARD", "/")
ROUTE_LATEST = os.environ.get("ROUTE_LATEST", "/api/latest")
ROUTE_LED_COMMAND = os.environ.get("ROUTE_LED_COMMAND", "/api/commands/led")
ROUTE_TEMPERATURE_TEST = os.environ.get("ROUTE_TEMPERATURE_TEST", "/api/commands/test-temperature")
ROUTE_UPDATE_TELEMETRY_PERIOD = os.environ.get("ROUTE_UPDATE_TELEMETRY_PERIOD", "/update_telemetry_period")

# Telemetry period limits (seconds).
TELEMETRY_PERIOD_MIN_SECONDS = int(os.environ.get("TELEMETRY_PERIOD_MIN_SECONDS", "1"))
TELEMETRY_PERIOD_MAX_SECONDS = int(os.environ.get("TELEMETRY_PERIOD_MAX_SECONDS", "300"))

# Allowed LED commands as a comma-separated env value (default: ON,OFF,TOGGLE).
ALLOWED_LED_COMMANDS = set(
    [c.strip().upper() for c in os.environ.get("ALLOWED_LED_COMMANDS", "ON,OFF,TOGGLE").split(",") if c.strip()]
)

# SQLite settings.
SQLITE_DB_PATH_VALUE = os.environ.get("SQLITE_DB_PATH", "telemetry.sqlite3").strip() or "telemetry.sqlite3"
SQLITE_SCHEMA_PATH_VALUE = os.environ.get("SQLITE_SCHEMA_PATH", "schema.sql").strip() or "schema.sql"
SQLITE_DB_PATH = Path(SQLITE_DB_PATH_VALUE)
SQLITE_SCHEMA_PATH = Path(SQLITE_SCHEMA_PATH_VALUE)
if not SQLITE_DB_PATH.is_absolute():
    SQLITE_DB_PATH = BASE_DIR / SQLITE_DB_PATH
if not SQLITE_SCHEMA_PATH.is_absolute():
    SQLITE_SCHEMA_PATH = BASE_DIR / SQLITE_SCHEMA_PATH


# -----------------------------------------------------------------------------
# In-memory telemetry state
# -----------------------------------------------------------------------------


class TelemetryStore:
    """Thread-safe in-memory telemetry snapshot and helpers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "date": None,
            "runtime": None,
            "ledstatus": "unknown",
            "temperature": None,
            "measurement_period_seconds": None,
            "device_status": "UNKNOWN",
            "device_status_updated_at": None,
        }

    def update_latest_data_from_payload(self, payload_dict: dict[str, Any]) -> None:
        """Normalize MQTT payload fields into a stable frontend shape."""
        with self._lock:
            self._data["date"] = payload_dict.get("date")
            self._data["runtime"] = payload_dict.get("runtime")
            self._data["ledstatus"] = str(payload_dict.get("ledstatus", "unknown"))
            self._data["temperature"] = payload_dict.get("temperature")
            self._data["measurement_period_seconds"] = payload_dict.get("period_seconds")
            # Any valid telemetry frame proves the device is reachable now.
            self._data["device_status"] = "ONLINE"
            self._data["device_status_updated_at"] = datetime.now(tz=timezone.utc).isoformat()

    @staticmethod
    def normalize_device_status(raw_status: str) -> str:
        """Normalize status payload to ONLINE/OFFLINE/UNKNOWN."""
        normalized = raw_status.strip().strip('"').strip("'").upper()
        if normalized in {"ONLINE", "OFFLINE"}:
            return normalized
        return "UNKNOWN"

    def update_device_status(self, raw_status: str) -> None:
        """Update cached device online/offline status and update time."""
        normalized = self.normalize_device_status(raw_status)
        if MQTT_DEBUG_LOGGING:
            print(f"[MQTT][DEBUG] Device status update raw='{raw_status}' normalized='{normalized}'")

        with self._lock:
            self._data["device_status"] = normalized
            self._data["device_status_updated_at"] = datetime.now(tz=timezone.utc).isoformat()

    @staticmethod
    def _is_status_stale(status_updated_at: Any, now_utc: datetime) -> bool:
        """Return True when last status update is older than configured timeout."""
        if not status_updated_at:
            return True

        if not isinstance(status_updated_at, str):
            return True

        text = status_updated_at.strip()
        if not text:
            return True

        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return True

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        age_seconds = (now_utc - parsed).total_seconds()
        return age_seconds > DEVICE_OFFLINE_TIMEOUT_SECONDS

    def build_latest_snapshot_with_fallback(self) -> dict[str, Any]:
        """Return latest data and infer OFFLINE if ONLINE status is stale."""
        with self._lock:
            snapshot = dict(self._data)

        now_utc = datetime.now(tz=timezone.utc)
        current_status = self.normalize_device_status(str(snapshot.get("device_status", "UNKNOWN")))
        if current_status == "ONLINE" and self._is_status_stale(snapshot.get("device_status_updated_at"), now_utc):
            snapshot["device_status"] = "OFFLINE"
            snapshot["device_status_inferred"] = True
        else:
            snapshot["device_status"] = current_status
            snapshot["device_status_inferred"] = False

        return snapshot


latest_store = TelemetryStore()
sqlite_store = TelemetrySQLiteStore(SQLITE_DB_PATH, SQLITE_SCHEMA_PATH)

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

# Register required REST API endpoints from a dedicated module/class.
rest_api_endpoints = RestApiEndpoints(sqlite_store)
rest_api_endpoints.register(app)


# -----------------------------------------------------------------------------
# MQTT callbacks and startup
# -----------------------------------------------------------------------------


def parse_device_status_payload(raw_payload: str) -> str:
    """
    Parse status payload and return ONLINE/OFFLINE/UNKNOWN.

    Accept both plain text ("ONLINE", "OFFLINE") and JSON payloads
    like {"status": "OFFLINE"} or {"state": "ONLINE"}.
    """
    stripped = raw_payload.strip()
    if not stripped:
        return "UNKNOWN"

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                for key in ("status", "state", "device_status"):
                    if key in parsed:
                        return TelemetryStore.normalize_device_status(str(parsed[key]))
        except json.JSONDecodeError:
            pass

    return TelemetryStore.normalize_device_status(stripped)


def is_connect_success(reason_code: Any) -> bool:
    """Return True when MQTT connect reason code means success."""
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        pass

    value_attr = getattr(reason_code, "value", None)
    if isinstance(value_attr, int):
        return value_attr == 0

    return str(reason_code).strip().lower() in {"0", "success"}


def validate_telemetry_payload(payload_dict: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate mandatory telemetry fields and normalize them for storage."""
    if not isinstance(payload_dict, dict):
        return None, "Payload is not a JSON object."

    temperature = parse_float(payload_dict.get("temperature"))
    if temperature is None:
        return None, "Missing or invalid required field 'temperature'."

    measurement_ts = parse_iso8601(payload_dict.get("date"))
    if measurement_ts is None:
        return None, "Missing or invalid required field 'date' (must be ISO 8601)."

    raw_period = payload_dict.get("period_seconds", payload_dict.get("measurement_period_seconds", payload_dict.get("period")))
    period_seconds = None
    if raw_period is not None:
        parsed_period = parse_int(raw_period)
        if parsed_period is not None and parsed_period > 0:
            period_seconds = parsed_period

    runtime = parse_int(payload_dict.get("runtime", payload_dict.get("runtime_seconds")))
    if runtime is None or runtime < 0:
        runtime = 0

    led_status = str(payload_dict.get("ledstatus", payload_dict.get("led_status", "unknown")))

    normalized_payload = {
        "date": measurement_ts,
        "runtime": runtime,
        "ledstatus": led_status,
        "temperature": temperature,
        "period_seconds": period_seconds,
    }
    return normalized_payload, None


class MQTTManager:
    """Encapsulates MQTT subscriber client and its callbacks."""

    def __init__(self, store: TelemetryStore, db_store: TelemetrySQLiteStore) -> None:
        self.store = store
        self.db_store = db_store

    def on_connect(
        self,
        client: mqtt_client.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if MQTT_DEBUG_LOGGING:
            print(f"[MQTT][DEBUG] on_connect reason_code={reason_code} type={type(reason_code).__name__}")

        if is_connect_success(reason_code):
            topic_filter_result = client.subscribe(MQTT_TOPIC_FILTER)
            status_result = client.subscribe(MQTT_STATUS_TOPIC)
            if MQTT_DEBUG_LOGGING:
                print(f"[MQTT][DEBUG] subscribe({MQTT_TOPIC_FILTER}) -> {topic_filter_result}")
                print(f"[MQTT][DEBUG] subscribe({MQTT_STATUS_TOPIC}) -> {status_result}")
            print(
                f"[MQTT] Connected. Subscribed to topic filter: {MQTT_TOPIC_FILTER} "
                f"and status topic: {MQTT_STATUS_TOPIC}"
            )
        else:
            print(f"[MQTT] Connection failed with reason code: {reason_code}")

    def on_message(self, client: mqtt_client.Client, userdata: Any, message: mqtt_client.MQTTMessage) -> None:
        raw_payload = message.payload.decode("utf-8", errors="replace")

        if MQTT_DEBUG_LOGGING:
            print(
                "[MQTT][DEBUG] message "
                f"topic='{message.topic}' qos={message.qos} retain={message.retain} payload='{raw_payload}'"
            )

        if message.topic == MQTT_STATUS_TOPIC:
            self.store.update_device_status(parse_device_status_payload(raw_payload))
            return

        device_name = parse_device_id_from_topic(message.topic, MQTT_TOPIC_PREFIX, MQTT_TOPIC_TELEMETRY_SUFFIX)
        if not device_name:
            print(f"[MQTT] Invalid telemetry topic format: {message.topic}")
            return

        try:
            payload_dict = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            print(f"[MQTT] Invalid JSON payload from {device_name}: {exc}")
            return

        validated, error_message = validate_telemetry_payload(payload_dict)
        if error_message:
            print(f"[MQTT] Dropped telemetry from {device_name}: {error_message}")
            return

        self.store.update_latest_data_from_payload(validated)

        period_seconds = validated.get("period_seconds")
        if period_seconds is None:
            period_seconds = self.db_store.get_last_known_period_seconds(device_name)
            if period_seconds is None:
                period_seconds = TELEMETRY_PERIOD_MIN_SECONDS
            validated["period_seconds"] = period_seconds

        try:
            self.db_store.save_telemetry_message(
                device_name=device_name,
                measured_at_iso8601=str(validated["date"]),
                temperature_c=float(validated["temperature"]),
                uptime_seconds=int(validated["runtime"]),
                measurement_period_seconds=int(validated["period_seconds"]),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[DB] Failed to persist telemetry for {device_name}: {exc}")

    def build_client(self) -> mqtt_client.Client:
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        return client


mqtt_manager = MQTTManager(latest_store, sqlite_store)


def build_mqtt_subscriber_client() -> mqtt_client.Client:
    """Create and configure MQTT client used for telemetry subscription."""
    return mqtt_manager.build_client()


def build_mqtt_publisher_client() -> mqtt_client.Client:
    """Create MQTT client used for one-shot publish requests from dashboard."""
    return mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)


def publish_mqtt_payload(topic: str, payload: str) -> None:
    """Publish text payload to topic and raise RuntimeError on failure."""
    publisher = build_mqtt_publisher_client()

    try:
        publisher.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_KEEPALIVE_SECONDS)
        result_info = publisher.publish(topic, payload)
        result_info.wait_for_publish(timeout=2.0)

        if result_info.rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed with rc={result_info.rc}")
    finally:
        publisher.disconnect()


def start_mqtt_background_loop() -> None:
    """
    Connect to broker and keep MQTT network loop running in background thread.

    If connection fails, Flask still starts, so frontend remains available.
    """
    global mqtt_subscriber_client
    mqtt_subscriber_client = build_mqtt_subscriber_client()

    try:
        mqtt_subscriber_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_KEEPALIVE_SECONDS)
        mqtt_subscriber_client.loop_start()
        print(f"[MQTT] Trying broker {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    except Exception as exc:  # noqa: BLE001
        print(f"[MQTT] Could not connect: {exc}")


def should_start_mqtt_loop() -> bool:
    """Start MQTT loop only in the effective Flask process."""
    if not FLASK_DEBUG:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


# -----------------------------------------------------------------------------
# Flask routes
# -----------------------------------------------------------------------------

@app.route(ROUTE_DASHBOARD)
def dashboard() -> str:
    """Serve dashboard page."""
    return render_template(
        "index.html",
        api_latest_path=ROUTE_LATEST,
        api_led_command_path=ROUTE_LED_COMMAND,
        api_temperature_test_path=ROUTE_TEMPERATURE_TEST,
        api_update_period_path=ROUTE_UPDATE_TELEMETRY_PERIOD,
        mqtt_led_command_topic=MQTT_LED_COMMAND_TOPIC,
        mqtt_telemetry_topic=MQTT_TELEMETRY_TOPIC,
        mqtt_period_command_topic=MQTT_PERIOD_COMMAND_TOPIC,
        frontend_refresh_ms=FRONTEND_REFRESH_MS,
    )


@app.route(ROUTE_LATEST)
def get_latest_data() -> Any:
    """Return the latest telemetry snapshot as JSON."""
    snapshot = latest_store.build_latest_snapshot_with_fallback()
    return jsonify(snapshot)


@app.post(ROUTE_LED_COMMAND)
def send_led_command() -> Any:
    """Send ON/OFF/TOGGLE command to LED MQTT topic."""
    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip().upper()

    if command not in ALLOWED_LED_COMMANDS:
        return jsonify(
            {
                "status": "error",
                "message": f"Unsupported LED command. Allowed: {sorted(ALLOWED_LED_COMMANDS)}",
            }
        ), 400

    try:
        publish_mqtt_payload(MQTT_LED_COMMAND_TOPIC, command)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": f"Publish failed: {exc}"}), 502

    return jsonify({"status": "sent", "topic": MQTT_LED_COMMAND_TOPIC, "command": command})


@app.post(ROUTE_TEMPERATURE_TEST)
def send_temperature_test() -> Any:
    """Publish simulated telemetry frame from desktop test device."""
    telemetry_payload = {
        "date": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": 0,
        "ledstatus": "unknown",
        "temperature": 100,
        "period_seconds": 2,
        "device": DESKTOP_TEST_DEVICE_ID,
    }

    try:
        publish_mqtt_payload(MQTT_TELEMETRY_TOPIC, json.dumps(telemetry_payload))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": f"Publish failed: {exc}"}), 502

    return jsonify(
        {
            "status": "sent",
            "topic": MQTT_TELEMETRY_TOPIC,
            "payload": telemetry_payload,
        }
    )


@app.route(ROUTE_UPDATE_TELEMETRY_PERIOD, methods=["GET", "POST"])
def update_telemetry_period() -> Any:
    """
    Update ESP32 telemetry period and publish value to MQTT as seconds.

    Supported query params:
    - period: numeric value
    - unit: "s" (default) or "m"

    Valid final range is 1..300 seconds.
    """
    raw_period = (request.args.get("period") or "").strip()
    raw_unit = (request.args.get("unit") or "s").strip().lower()

    if not raw_period:
        return jsonify({"status": "error", "message": "Missing query parameter 'period'."}), 400

    try:
        period_value = float(raw_period)
    except ValueError:
        return jsonify({"status": "error", "message": f"Invalid period value: {raw_period}"}), 400

    if period_value <= 0:
        return jsonify({"status": "error", "message": "Period must be > 0."}), 400

    if raw_unit not in {"s", "m"}:
        return jsonify({"status": "error", "message": "Unsupported unit. Use 's' or 'm'."}), 400

    period_seconds = int(round(period_value * 60.0)) if raw_unit == "m" else int(round(period_value))

    if period_seconds < TELEMETRY_PERIOD_MIN_SECONDS or period_seconds > TELEMETRY_PERIOD_MAX_SECONDS:
        return jsonify(
            {
                "status": "error",
                "message": (
                    "Period out of range. Allowed interval: "
                    f"{TELEMETRY_PERIOD_MIN_SECONDS}..{TELEMETRY_PERIOD_MAX_SECONDS} seconds."
                ),
            }
        ), 400

    try:
        publish_mqtt_payload(MQTT_PERIOD_COMMAND_TOPIC, str(period_seconds))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": f"Publish failed: {exc}"}), 502

    return jsonify(
        {
            "status": "sent",
            "topic": MQTT_PERIOD_COMMAND_TOPIC,
            "period_seconds": period_seconds,
        }
    )


if __name__ == "__main__":
    # Start MQTT before Flask so data can begin flowing immediately.
    if should_start_mqtt_loop():
        start_mqtt_background_loop()
    else:
        print("[MQTT] Skipping subscriber startup in Flask reloader parent process.")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
