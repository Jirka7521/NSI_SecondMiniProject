#include <Arduino.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <MQTT.h>
#include <WiFi.h>
#include <time.h>

#include "config.h"

// ============================================================================
// Hardware and network clients
// ============================================================================
DHT dht(DHT_PIN, DHT_TYPE);
WiFiClient wifiClient;
MQTTClient mqttClient(1024);

// ============================================================================
// Runtime state
// ============================================================================
unsigned long lastPublishMs = 0;
bool ledIsOn = false;

// ============================================================================
// Utility helpers
// ============================================================================
void setLed(bool on) {
  ledIsOn = on;
  digitalWrite(LED_PIN, on ? HIGH : LOW);
}

const char *ledStatusText() {
  return ledIsOn ? "on" : "off";
}

bool isClockSynced() {
  return time(nullptr) > 1700000000;
}

String utcTimestampNow() {
  time_t now = time(nullptr);
  struct tm tmUtc;
  gmtime_r(&now, &tmUtc);

  char iso8601[25];
  strftime(iso8601, sizeof(iso8601), "%Y-%m-%dT%H:%M:%SZ", &tmUtc);
  return String(iso8601);
}

// ============================================================================
// Connectivity setup
// ============================================================================
void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("[WiFi] Connecting to ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.print("[WiFi] Connected. IP: ");
  Serial.println(WiFi.localIP());
}

void syncClockWithNtp() {
  if (isClockSynced()) {
    return;
  }

  Serial.println("[NTP] Syncing time...");
  configTime(NTP_GMT_OFFSET_SECONDS, NTP_DAYLIGHT_OFFSET_SECONDS, NTP_SERVER_1, NTP_SERVER_2,
             NTP_SERVER_3);

  unsigned long start = millis();
  while (!isClockSynced() && millis() - start < NTP_SYNC_TIMEOUT_MS) {
    delay(300);
    Serial.print("#");
  }

  Serial.println();
  if (isClockSynced()) {
    Serial.print("[NTP] Synced UTC time: ");
    Serial.println(utcTimestampNow());
  } else {
    Serial.println("[NTP] Sync timeout. Will retry in loop.");
  }
}

void connectMqtt() {
  if (mqttClient.connected()) {
    return;
  }

  mqttClient.begin(MQTT_HOST, MQTT_PORT, wifiClient);
  mqttClient.setKeepAlive(MQTT_KEEP_ALIVE_SECONDS);
  mqttClient.setTimeout(MQTT_SOCKET_TIMEOUT_MS);
  mqttClient.setWill(MQTT_TOPIC, "{\"status\":\"offline\"}", false, 1);

  Serial.print("[MQTT] Connecting to ");
  Serial.print(MQTT_HOST);
  Serial.print(":");
  Serial.println(MQTT_PORT);

  while (!mqttClient.connected()) {
    bool connected;
    if (strlen(MQTT_USERNAME) > 0) {
      connected = mqttClient.connect(MQTT_CLIENT_ID, MQTT_USERNAME, MQTT_PASSWORD);
    } else {
      connected = mqttClient.connect(MQTT_CLIENT_ID);
    }

    if (connected) {
      Serial.println("[MQTT] Connected.");
      return;
    }

    Serial.print("[MQTT] Failed, code=");
    Serial.print(mqttClient.lastError());
    Serial.println(". Retry in 2s.");
    delay(2000);
  }
}

void ensureConnectionsAndTime() {
  connectWifi();
  syncClockWithNtp();
  connectMqtt();

  // LED reflects healthy online state.
  setLed(WiFi.status() == WL_CONNECTED && mqttClient.connected() && isClockSynced());
}

// ============================================================================
// Telemetry payload
// ============================================================================
String createTelemetryPayload(float temperatureC) {
  JsonDocument doc;
  doc["date"] = utcTimestampNow();
  doc["runtime"] = static_cast<unsigned long>(millis() / 1000UL);
  doc["ledstatus"] = ledStatusText();
  doc["temperature"] = temperatureC;

  String payload;
  serializeJson(doc, payload);
  return payload;
}

void publishTelemetry() {
  float temperatureC = dht.readTemperature();

  if (isnan(temperatureC)) {
    Serial.println("[DHT11] Read failed.");
    return;
  }

  String payload = createTelemetryPayload(temperatureC);
  bool ok = mqttClient.publish(MQTT_TOPIC, payload, false, 1);

  Serial.print("[MQTT] Publish ");
  Serial.print(ok ? "OK" : "FAILED");
  Serial.print(" -> ");
  Serial.println(payload);
}

// ============================================================================
// Arduino entry points
// ============================================================================
void setup() {
  Serial.begin(SERIAL_BAUD_RATE);
  delay(200);

  pinMode(LED_PIN, OUTPUT);
  setLed(false);

  dht.begin();
  ensureConnectionsAndTime();

  lastPublishMs = millis();
}

void loop() {
  ensureConnectionsAndTime();
  mqttClient.loop();

  unsigned long now = millis();
  if (now - lastPublishMs >= PUBLISH_INTERVAL_MS) {
    lastPublishMs = now;
    publishTelemetry();
  }
}