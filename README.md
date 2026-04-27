# NSI Second & Third Mini Project

Overview
--------

This repository contains a small IoT demo: an ESP32-based device publishes DHT sensor telemetry to an MQTT broker, and a simple Flask dashboard (hosted on your desktop) shows the latest measurement and allows sending control commands (LED toggle, telemetry period updates, test telemetry).

Repository layout
-----------------

- `Desktop/` — Host-side Flask dashboard and static frontend.
  - `server.py` — Flask app that subscribes to MQTT topics, caches the latest telemetry snapshot in memory, and exposes simple API endpoints for the frontend and control actions.
  - `static/` — Frontend assets (JavaScript, CSS).
  - `templates/` — Jinja HTML templates (dashboard page).
  - `.env` — Local environment variables (ignored by git) for MQTT and Flask configuration. Copy and edit Desktop/.env for local runs.
- `ESP32/NSI_secondMiniProject/` — PlatformIO firmware for the ESP32 device.
  - `platformio.ini` — PlatformIO project configuration.
  - `include/config.h` — Build-time configuration constants (Wi‑Fi, MQTT topics, pins).
  - `src/main.cpp` — Firmware that publishes telemetry and handles MQTT commands.

Supported platforms
-------------------

- Host: Windows (tested). Linux and macOS are expected to work for the Flask app and PlatformIO builds with minimal changes.
- Device: ESP32 (PlatformIO environment configured in `platformio.ini`).

High-level flow
---------------

- The ESP32 connects to Wi‑Fi and the configured MQTT broker, sets a Last Will & Testament (LWT) on the status topic, and periodically publishes telemetry JSON to the telemetry topic.
- The Flask dashboard subscribes to telemetry and status topics and keeps the most recent snapshot in memory, exposing it over a small JSON API to the frontend.
- The frontend polls the API to show the latest measurement and the device online/offline status.

Configuration
-------------

Two primary configuration places:

- Desktop Flask `.env` (Desktop/.env) — set your MQTT broker host/port, topics, and Flask host/port. Example keys used by the server:
  - `FLASK_HOST`, `FLASK_PORT`, `FLASK_DEBUG`
  - `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_KEEPALIVE_SECONDS`
  - `MQTT_TOPIC_FILTER`, `MQTT_LED_COMMAND_TOPIC`, `MQTT_TELEMETRY_TOPIC`, `MQTT_STATUS_TOPIC`, `MQTT_PERIOD_COMMAND_TOPIC`
  - `FRONTEND_REFRESH_MS`, `DEVICE_OFFLINE_TIMEOUT_SECONDS`, `MQTT_DEBUG_LOGGING`

- ESP32 compile-time config (`ESP32/NSI_secondMiniProject/include/config.h`):
  - `WIFI_SSID`, `WIFI_PASSWORD` — Wi‑Fi network credentials.
  - MQTT constants: `MQTT_HOST`, `MQTT_PORT`, `MQTT_CLIENT_ID`, `MQTT_TOPIC`, `MQTT_TOPIC_LED_COMMAND`, `MQTT_TOPIC_PERIOD_COMMAND`, `MQTT_TOPIC_STATUS`, `MQTT_TOPIC_TELEMETRY_WILDCARD`.

Quick start — Desktop (Flask dashboard)
-------------------------------------

1. (Optional) Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate
pip install -r Desktop/requirements.txt
```

2. Copy `Desktop/.env.example` (if present) to `Desktop/.env` and set values for your MQTT broker and desired topics. If you don't have a broker, you can run a local broker (e.g., `mosquitto`) or use a public broker for testing.

3. Run the Flask server:

```powershell
python Desktop/server.py
```

4. Open the dashboard at http://127.0.0.1:5000 (or at the host/port configured by `FLASK_HOST`/`FLASK_PORT`).

API endpoints (desktop server)
-----------------------------

- `GET /api/latest` — returns the latest telemetry snapshot (JSON).
- `POST /api/commands/led` — send an LED command. JSON body: { "command": "ON" | "OFF" | "TOGGLE" }.
- `POST /api/commands/test-temperature` — publish a synthetic 100°C telemetry payload from a desktop test device to the telemetry topic (useful for testing the dashboard).
- `GET|POST /update_telemetry_period?period=<value>&unit=s|m` — request the device to change its telemetry period (seconds or minutes).

Quick start — ESP32 (PlatformIO)
--------------------------------

1. Install PlatformIO (VS Code extension or core CLI) and the required toolchain for ESP32.

2. Build the firmware:

```powershell
cd ESP32/NSI_secondMiniProject
platformio run
```

3. Upload to your board (replace `COM9` with your serial port):

```powershell
platformio run --target upload --upload-port COM9
```

4. Open the serial monitor to view logs and verify the device publishes `ONLINE` on connect and telemetry messages:

```powershell
platformio device monitor --port COM9
```

MQTT and Last Will (LWT)
------------------------

- The ESP32 configures a Last Will message on the status topic with payload `OFFLINE` (retained, qos=1). After a successful connection the device publishes `ONLINE` (retained), ensuring the broker holds a recent status message for subscribers.
- Note: LWT behavior depends on the broker and keepalive settings; the Flask server also treats a device as `OFFLINE` if the last status message is older than `DEVICE_OFFLINE_TIMEOUT_SECONDS`.

Debugging tips
--------------

- Check PlatformIO's serial monitor for firmware logs (LWT setup, `ONLINE` publish, telemetry JSON).
- Enable `MQTT_DEBUG_LOGGING=true` in `Desktop/.env` to see status topic messages and connect reason codes in the Flask server logs.
- If you don't see an `OFFLINE` LWT after abrupt power loss:
  - Confirm the broker received and published the retained will message.
  - Ensure `MQTT_KEEPALIVE_SECONDS` on the device is reasonably short for your test scenario.

Project notes and next steps
---------------------------

- The desktop server intentionally keeps only the latest measurement in memory for simplicity — there's no persistence layer in this demo.
- Future improvements: persist historical telemetry to a lightweight DB and add charts in the frontend, add authentication and TLS for MQTT, and improve the frontend UX.

Troubleshooting
---------------

- If Flask fails to start due to missing environment variables: copy `Desktop/.env` and fill in real values, or export the required variables in your shell.
- If the ESP32 cannot connect to MQTT: verify Wi‑Fi credentials in `include/config.h`, check broker reachability, and confirm firewall or network settings.

License
-------

MIT License — see the `LICENSE` file in the project root.

Credits
-------

This demo was assembled for the NSI second mini project: device telemetry, MQTT, and a minimal Flask dashboard frontend.
