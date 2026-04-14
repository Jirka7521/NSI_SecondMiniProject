# NSI Second Mini Project

Overview
--------

This project provides a small IoT demo: an ESP32 device publishes DHT sensor telemetry to an MQTT broker, and a simple Flask dashboard shows the latest measurement and allows sending control commands (LED toggle, telemetry period updates, test telemetry).

Repository layout
-----------------

- `Desktop/` — Host-side Flask dashboard and static frontend.
  - `server.py` — Flask app that subscribes to MQTT topics, caches latest telemetry, and exposes API endpoints.
  - `static/` — Frontend assets (JavaScript, CSS).
  - `templates/` — Jinja HTML templates (dashboard page).
  - `.env` — Local environment variables (ignored by git) for MQTT and Flask configuration.
- `ESP32/NSI_seconMiniProject/` — PlatformIO embedded firmware for ESP32.
  - `platformio.ini` — PlatformIO project config.
  - `include/config.h` — Build-time configuration constants (Wi‑Fi, MQTT topics, pins).
  - `src/main.cpp` — Firmware that publishes telemetry and handles MQTT commands.

Supported platforms
-------------------

- Host: Windows (tested); Linux/macOS should work for the Flask app and PlatformIO build.
- Device: ESP32 (PlatformIO environment configured in `platformio.ini`).

High-level flow
---------------

- ESP32 connects to Wi‑Fi and MQTT broker, sets a Last Will (LWT) on the status topic, and periodically publishes telemetry JSON to the telemetry topic.
- Flask dashboard subscribes to telemetry and status topics and maintains an in-memory latest snapshot served via a JSON API.
- The frontend polls the API to show the latest measurement and device online/offline status.

Configuration
-------------

Two configuration places:

- Desktop Flask `.env` (Desktop/.env) — set your MQTT broker host/port, topics, and Flask host/port. Example keys found in file:

  - `FLASK_HOST`, `FLASK_PORT`, `FLASK_DEBUG`
  - `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_KEEPALIVE_SECONDS`
  - `MQTT_TOPIC_FILTER`, `MQTT_LED_COMMAND_TOPIC`, `MQTT_TELEMETRY_TOPIC`, `MQTT_STATUS_TOPIC`, `MQTT_PERIOD_COMMAND_TOPIC`
  - `FRONTEND_REFRESH_MS`

- ESP32 compile-time config (`ESP32/NSI_seconMiniProject/include/config.h`):
  - `WIFI_SSID`, `WIFI_PASSWORD` — Wi‑Fi network credentials.
  - MQTT constants: `MQTT_HOST`, `MQTT_PORT`, `MQTT_CLIENT_ID`, `MQTT_TOPIC`, `MQTT_TOPIC_LED_COMMAND`, `MQTT_TOPIC_PERIOD_COMMAND`, `MQTT_TOPIC_STATUS`, `MQTT_TOPIC_TELEMETRY_WILDCARD`.

Quick start — Desktop (Flask dashboard)
-------------------------------------

1. Create a virtual environment (optional) and install dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r Desktop/requirements.txt
```

2. Configure `Desktop/.env` with values for your MQTT broker and topics (an example `.env` is already present but contains values for a public broker used during development).

3. Run the server:

```powershell
python Desktop/server.py
```

4. Open the dashboard at http://127.0.0.1:5000 (or the `FLASK_HOST`/`FLASK_PORT` you set).

API endpoints
-------------

- `GET /api/latest` — returns the latest telemetry snapshot (JSON).
- `POST /api/commands/led` — send LED command. JSON body: `{ "command": "ON"|"OFF"|"TOGGLE" }`.
- `POST /api/commands/test-temperature` — publish a synthetic 100°C telemetry payload from a desktop test device to the telemetry topic.
- `GET|POST /update_telemetry_period?period=<value>&unit=s|m` — request device to change telemetry period (seconds or minutes).

Quick start — ESP32 (PlatformIO)
--------------------------------

1. Install PlatformIO (VS Code extension or core CLI).
2. Build firmware:

```powershell
cd ESP32/NSI_seconMiniProject
platformio run
```

3. Upload to your board (replace `COM9` with your port):

```powershell
platformio run --target upload --upload-port COM9
```

4. Open serial monitor (PlatformIO monitor) to watch debug logs.

MQTT and Last Will (LWT)
------------------------

- The ESP32 sets a Last Will message on the configured status topic (`MQTT_TOPIC_STATUS`) with payload `OFFLINE` (retained, qos=1). On successful connect the device publishes `ONLINE` (also retained) so the most recent status is available to subscribers.
- Note: LWT delivery depends on the broker and the keepalive interval. If the device disconnects abruptly, the broker publishes the will; however, timing and retained flags vary by broker. The Flask server includes a fallback that infers OFFLINE if the last status update is stale (configurable via `DEVICE_OFFLINE_TIMEOUT_SECONDS` environment variable).

Debugging tips
--------------

- Check PlatformIO serial monitor to verify the firmware prints the LWT setup and the `ONLINE` publish on connect.
- The Flask server logs MQTT connect reason codes and every message on the status topic when `MQTT_DEBUG_LOGGING=true` in `.env`.
- If you do not see `OFFLINE` after power loss:
  - Verify the broker received and published the LWT; many public brokers show retained messages in their web dashboards.
  - Confirm `MQTT_KEEPALIVE_SECONDS` (ESP32) is sufficiently short for your test; reducing it speeds detection but increases overhead.

Project notes
-------------

- The Flask app intentionally keeps only the latest measurement in memory for simplicity — no persistence layer.
- The frontend polls the `/api/latest` endpoint at `FRONTEND_REFRESH_MS` intervals; the API will also mark a device `OFFLINE` if the last status message is older than the configured timeout.

Troubleshooting
---------------

- If Flask cannot start because of missing environment variables: copy `Desktop/.env` and fill real values, or set the necessary vars in your environment.
- If the ESP32 cannot connect to MQTT: check Wi‑Fi credentials in `include/config.h`, ensure the broker host/port are reachable, and verify any firewall rules.
- To validate LWT behavior:
  1. Connect the device and confirm `ONLINE` is published.
  2. Abruptly cut power to the board and watch the broker — it should publish `OFFLINE` to the status topic.
 3. If the broker publishes `OFFLINE` but Flask doesn't display it, enable `MQTT_DEBUG_LOGGING` in `Desktop/.env` and check logs for the status payload and retain flag.

License
-------

MIT License — see the `LICENSE` file in the project root.

Further work
------------

- Persist historical telemetry to a lightweight DB and show charts in the frontend.
- Add authentication to the dashboard and secure MQTT (TLS + auth) for non-local deployment.
