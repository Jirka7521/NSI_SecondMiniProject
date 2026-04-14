"""
Simple Flask dashboard server for MQTT telemetry.

What this file does:
1. Loads configuration from .env.
2. Connects to an MQTT broker (no secure/auth mode) and subscribes to topics.
3. Stores only the latest measurement in memory.
4. Serves a web page (dashboard) and a JSON API for that latest measurement.

The goal is readability, so constants and comments are intentionally verbose.
"""

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

# We always resolve paths relative to this file so the app works from any CWD.
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
# This project intentionally uses plain MQTT without authentication/TLS,
# because the requirement is local/simple broker usage only.
MQTT_BROKER_HOST = get_required_env("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = get_required_int_env("MQTT_BROKER_PORT")
MQTT_KEEPALIVE_SECONDS = get_required_int_env("MQTT_KEEPALIVE_SECONDS")
MQTT_TOPIC_FILTER = get_required_env("MQTT_TOPIC_FILTER")
MQTT_LED_COMMAND_TOPIC = get_required_env("MQTT_LED_COMMAND_TOPIC")
MQTT_TELEMETRY_TOPIC = get_required_env("MQTT_TELEMETRY_TOPIC")

# We intentionally identify test telemetry as a separate producer.
DESKTOP_TEST_DEVICE_ID = os.environ.get("DESKTOP_TEST_DEVICE_ID", "desktop-telemetry-tester")

# Frontend polling interval in milliseconds.
FRONTEND_REFRESH_MS = int(os.environ.get("FRONTEND_REFRESH_MS", "2000"))

# API route constants.
ROUTE_DASHBOARD = "/"
ROUTE_LATEST = "/api/latest"
ROUTE_LED_COMMAND = "/api/commands/led"
ROUTE_TEMPERATURE_TEST = "/api/commands/test-temperature"

ALLOWED_LED_COMMANDS = {"ON", "OFF", "TOGGLE"}


# -----------------------------------------------------------------------------
# In-memory telemetry state
# -----------------------------------------------------------------------------

# Thread lock protects shared state because MQTT callbacks run in a background
# network thread while Flask serves requests in main or worker threads.
latest_data_lock = threading.Lock()

# Default structure shown before first MQTT message arrives.
latest_data: dict[str, Any] = {
    "date": None,
    "runtime": None,
    "ledstatus": "unknown",
    "temperature": None,
}


# -----------------------------------------------------------------------------
# Flask app setup
# -----------------------------------------------------------------------------

app = Flask(__name__)
mqtt_subscriber_client: mqtt_client.Client | None = None


def build_measurement_timestamp(value_from_payload: Any) -> str:
    """
    Convert payload timestamp to a standardized ISO string.

    Accepted formats:
    - ISO datetime string (we keep it as-is)
    - Unix timestamp in seconds (int/float)
    - Missing/unknown value -> current UTC time
    """
    if isinstance(value_from_payload, str) and value_from_payload.strip():
        return value_from_payload

    if isinstance(value_from_payload, (int, float)):
        dt = datetime.fromtimestamp(value_from_payload, tz=timezone.utc)
        return dt.isoformat()

    return datetime.now(tz=timezone.utc).isoformat()


def update_latest_data_from_payload(payload_dict: dict[str, Any]) -> None:
    """
    Normalize MQTT payload fields into a stable frontend shape.

        Expected payload keys (examples):
    {
            "date": "2026-04-14T10:30:00Z",
            "runtime": 123,
            "ledstatus": "on",
            "temperature": 24.7
    }

    To keep integration flexible, we also accept a few common aliases.
    """
    measurement_date = build_measurement_timestamp(
        payload_dict.get("date", payload_dict.get("measurement_datetime", payload_dict.get("timestamp")))
    )
    runtime = payload_dict.get(
        "runtime", payload_dict.get("runtime_seconds", payload_dict.get("device_runtime_seconds"))
    )
    led_status = str(payload_dict.get("ledstatus", payload_dict.get("led_status", payload_dict.get("led", "unknown"))))
    temperature = payload_dict.get(
        "temperature", payload_dict.get("temperature_celsius", payload_dict.get("temp"))
    )

    with latest_data_lock:
        latest_data["date"] = measurement_date
        latest_data["runtime"] = runtime
        latest_data["ledstatus"] = led_status
        latest_data["temperature"] = temperature


# -----------------------------------------------------------------------------
# MQTT callbacks and startup
# -----------------------------------------------------------------------------

def on_mqtt_connect(client: mqtt_client.Client, userdata: Any, flags: Any, reason_code: int, properties: Any = None) -> None:
    """Subscribe to MQTT topic filter after successful broker connection."""
    if reason_code == 0:
        # Subscribe to configured topic filter and keep only
        # the most recently received message in memory.
        client.subscribe(MQTT_TOPIC_FILTER)
        print(f"[MQTT] Connected. Subscribed to topic filter: {MQTT_TOPIC_FILTER}")
    else:
        print(f"[MQTT] Connection failed with reason code: {reason_code}")


def on_mqtt_message(client: mqtt_client.Client, userdata: Any, message: mqtt_client.MQTTMessage) -> None:
    """Parse incoming MQTT JSON payload and update cached latest data."""
    raw_payload = message.payload.decode("utf-8", errors="replace")

    try:
        payload_dict = json.loads(raw_payload)
        if isinstance(payload_dict, dict):
            update_latest_data_from_payload(payload_dict)
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
        frontend_refresh_ms=FRONTEND_REFRESH_MS,
    )


@app.route(ROUTE_LATEST)
def get_latest_data() -> Any:
    """Return the latest telemetry snapshot as JSON."""
    with latest_data_lock:
        snapshot = dict(latest_data)

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


if __name__ == "__main__":
    # Start MQTT before Flask so data can begin flowing immediately.
    start_mqtt_background_loop()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

