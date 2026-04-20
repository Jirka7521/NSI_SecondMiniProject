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


# Flask server settings.
FLASK_HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "true").lower() == "true"

# MQTT connection settings.
MQTT_BROKER_HOST = get_required_env("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = get_required_int_env("MQTT_BROKER_PORT")
MQTT_KEEPALIVE_SECONDS = get_required_int_env("MQTT_KEEPALIVE_SECONDS")
MQTT_TOPIC_FILTER = get_required_env("MQTT_TOPIC_FILTER")
MQTT_LED_COMMAND_TOPIC = get_required_env("MQTT_LED_COMMAND_TOPIC")
MQTT_TELEMETRY_TOPIC = get_required_env("MQTT_TELEMETRY_TOPIC")
MQTT_STATUS_TOPIC = get_required_env("MQTT_STATUS_TOPIC")
MQTT_PERIOD_COMMAND_TOPIC = get_required_env("MQTT_PERIOD_COMMAND_TOPIC")

# We intentionally identify test telemetry as a separate producer.
DESKTOP_TEST_DEVICE_ID = os.environ.get("DESKTOP_TEST_DEVICE_ID", "desktop-telemetry-tester")

# Frontend polling interval in milliseconds.
FRONTEND_REFRESH_MS = int(os.environ.get("FRONTEND_REFRESH_MS", "2000"))
MQTT_DEBUG_LOGGING = os.environ.get("MQTT_DEBUG_LOGGING", "true").lower() == "true"
DEVICE_OFFLINE_TIMEOUT_SECONDS = int(os.environ.get("DEVICE_OFFLINE_TIMEOUT_SECONDS", "90"))

# API route constants (configurable via environment variables).
# These default values match the previous hard-coded constants so existing
# deployments continue to work if env vars are not set.
ROUTE_DASHBOARD = os.environ.get("ROUTE_DASHBOARD", "/")
ROUTE_LATEST = os.environ.get("ROUTE_LATEST", "/api/latest")
ROUTE_LED_COMMAND = os.environ.get("ROUTE_LED_COMMAND", "/api/commands/led")
ROUTE_TEMPERATURE_TEST = os.environ.get("ROUTE_TEMPERATURE_TEST", "/api/commands/test-temperature")
ROUTE_UPDATE_TELEMETRY_PERIOD = os.environ.get("ROUTE_UPDATE_TELEMETRY_PERIOD", "/update_telemetry_period")

# Telemetry period limits (seconds) - configurable via env for flexible deploys.
TELEMETRY_PERIOD_MIN_SECONDS = int(os.environ.get("TELEMETRY_PERIOD_MIN_SECONDS", "1"))
TELEMETRY_PERIOD_MAX_SECONDS = int(os.environ.get("TELEMETRY_PERIOD_MAX_SECONDS", "300"))

# Allowed LED commands as a comma-separated env value (default: ON,OFF,TOGGLE).
ALLOWED_LED_COMMANDS = set(
    [c.strip().upper() for c in os.environ.get("ALLOWED_LED_COMMANDS", "ON,OFF,TOGGLE").split(",") if c.strip()]
)


# -----------------------------------------------------------------------------
# In-memory telemetry state (refactored into a class)
# -----------------------------------------------------------------------------


class TelemetryStore:
    """Thread-safe in-memory telemetry snapshot and helpers.

    This class centralizes storage and all logic that normalizes incoming
    payloads into the minimal shape the frontend expects.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "date": None,
            "runtime": None,
            "ledstatus": "unknown",
            "temperature": None,
            "device_status": "UNKNOWN",
            "device_status_updated_at": None,
        }

    @staticmethod
    def build_measurement_timestamp(value_from_payload: Any) -> str:
        """
        Convert payload timestamp to a standardized ISO string.

        Accepted formats:
        - ISO datetime string (kept as-is)
        - Unix timestamp in seconds (int/float)
        - Missing/unknown -> current UTC time
        """
        if isinstance(value_from_payload, str) and value_from_payload.strip():
            return value_from_payload

        if isinstance(value_from_payload, (int, float)):
            dt = datetime.fromtimestamp(value_from_payload, tz=timezone.utc)
            return dt.isoformat()

        return datetime.now(tz=timezone.utc).isoformat()

    def update_latest_data_from_payload(self, payload_dict: dict[str, Any]) -> None:
        """Normalize MQTT payload fields into a stable frontend shape."""
        measurement_date = self.build_measurement_timestamp(
            payload_dict.get("date", payload_dict.get("measurement_datetime", payload_dict.get("timestamp")))
        )
        runtime = payload_dict.get(
            "runtime", payload_dict.get("runtime_seconds", payload_dict.get("device_runtime_seconds"))
        )
        led_status = str(
            payload_dict.get("ledstatus", payload_dict.get("led_status", payload_dict.get("led", "unknown")))
        )
        temperature = payload_dict.get(
            "temperature", payload_dict.get("temperature_celsius", payload_dict.get("temp"))
        )

        with self._lock:
            self._data["date"] = measurement_date
            self._data["runtime"] = runtime
            self._data["ledstatus"] = led_status
            self._data["temperature"] = temperature
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

        # Accept common ISO values with trailing Z.
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


# Create a single module-level telemetry store for the Flask routes and
# MQTT callbacks to use. This preserves the original shared-state semantics
# while keeping the code organized.
latest_store = TelemetryStore()

# Create Flask application instance used by route decorators below.
# Explicitly set template and static folders to the Desktop subfolders so
# Flask can locate the shipped assets when running from the project root.
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)


# -----------------------------------------------------------------------------
# MQTT callbacks and startup (refactored into MQTTManager)
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
    # paho-mqtt v2 can provide either int-like reason code or object-like value.
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        pass

    value_attr = getattr(reason_code, "value", None)
    if isinstance(value_attr, int):
        return value_attr == 0

    return str(reason_code).strip().lower() in {"0", "success"}


class MQTTManager:
    """Encapsulates MQTT subscriber client and its callbacks.

    The manager subscribes to the configured topic filter and status
    topic on successful connect and delegates payload processing to the
    shared telemetry store.
    """

    def __init__(self, store: TelemetryStore) -> None:
        self.store = store
        self.client: mqtt_client.Client | None = None

    def on_connect(self, client: mqtt_client.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
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

        try:
            payload_dict = json.loads(raw_payload)
            if isinstance(payload_dict, dict):
                self.store.update_latest_data_from_payload(payload_dict)
            else:
                print("[MQTT] Ignored non-dictionary JSON payload.")
        except json.JSONDecodeError:
            print("[MQTT] Ignored invalid JSON payload.")

    def build_client(self) -> mqtt_client.Client:
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        self.client = client
        return client


# Create MQTT manager instance to be used by the startup logic below.
mqtt_manager = MQTTManager(latest_store)


def build_mqtt_subscriber_client() -> mqtt_client.Client:
    """Factory kept for compatibility; delegates to MQTTManager."""
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
    """Start the MQTT subscriber in a background thread.

    This mirrors the original behavior while using the MQTTManager
    instance for callback logic.
    """
    global mqtt_subscriber_client
    mqtt_subscriber_client = build_mqtt_subscriber_client()

    try:
        mqtt_subscriber_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_KEEPALIVE_SECONDS)
        mqtt_subscriber_client.loop_start()
        print(f"[MQTT] Trying broker {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    except Exception as exc:  # noqa: BLE001
        print(f"[MQTT] Could not connect: {exc}")


# -----------------------------------------------------------------------------
# MQTT callbacks and startup
# -----------------------------------------------------------------------------

def is_connect_success(reason_code: Any) -> bool:
    """Return True when MQTT connect reason code means success."""
    # paho-mqtt v2 can provide either int-like reason code or object-like value.
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        pass

    value_attr = getattr(reason_code, "value", None)
    if isinstance(value_attr, int):
        return value_attr == 0

    return str(reason_code).strip().lower() in {"0", "success"}


def on_mqtt_connect(client: mqtt_client.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
    """Subscribe to MQTT topic filter after successful broker connection."""
    if MQTT_DEBUG_LOGGING:
        print(f"[MQTT][DEBUG] on_connect reason_code={reason_code} type={type(reason_code).__name__}")

    if is_connect_success(reason_code):
        # Subscribe to configured topic filter and status topic.
        # We subscribe status explicitly so ONLINE/OFFLINE works even if
        # topic filter is telemetry-only.
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


def on_mqtt_message(client: mqtt_client.Client, userdata: Any, message: mqtt_client.MQTTMessage) -> None:
    """Parse incoming MQTT JSON payload and update cached latest data."""
    raw_payload = message.payload.decode("utf-8", errors="replace")

    if MQTT_DEBUG_LOGGING:
        print(
            "[MQTT][DEBUG] message "
            f"topic='{message.topic}' qos={message.qos} retain={message.retain} payload='{raw_payload}'"
        )

    if message.topic == MQTT_STATUS_TOPIC:
        latest_store.update_device_status(parse_device_status_payload(raw_payload))
        return

    try:
        payload_dict = json.loads(raw_payload)
        if isinstance(payload_dict, dict):
            latest_store.update_latest_data_from_payload(payload_dict)
        else:
            print("[MQTT] Ignored non-dictionary JSON payload.")
    except json.JSONDecodeError:
        print("[MQTT] Ignored invalid JSON payload.")


def build_mqtt_subscriber_client() -> mqtt_client.Client:
    """Create and configure MQTT client used for telemetry subscription."""
    # Empty client_id lets broker assign/generated handling when supported,
    # which is fine because this dashboard has only one data source/device.
    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)

    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    return client


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
    """Publish simulated telemetry from desktop test device with 100 C value."""
    telemetry_payload = {
        "date": datetime.now(tz=timezone.utc).isoformat(),
        "runtime": 0,
        "ledstatus": "unknown",
        "temperature": 100,
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
    start_mqtt_background_loop()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

