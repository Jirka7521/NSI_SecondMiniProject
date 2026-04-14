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
unsigned long publishIntervalMs = PUBLISH_INTERVAL_MS;
bool ledIsOn = false;

// Board LED on GPIO4 is wired as active-low: LOW = ON, HIGH = OFF.
static constexpr bool LED_ACTIVE_LOW = true;
// NOTE: health-status LED indicator removed (manual MQTT commands control the LED)

// ============================================================================
// Utility helpers
// ============================================================================
void setLed(bool on) {
  ledIsOn = on;
  if (LED_ACTIVE_LOW) {
    digitalWrite(LED_PIN, on ? LOW : HIGH);
  } else {
    digitalWrite(LED_PIN, on ? HIGH : LOW);
  }
}

const char *ledStatusText() {
  return ledIsOn ? "on" : "off";
}

bool startsWithPrefix(const String &value, const char *prefix) {
  return value.startsWith(prefix);
}

bool endsWithSuffix(const String &value, const char *suffix) {
  int valueLength = value.length();
  int suffixLength = strlen(suffix);
  if (valueLength < suffixLength) {
    return false;
  }

  return value.substring(valueLength - suffixLength).equals(suffix);
}

bool isTelemetryTopic(const String &topic) {
  return startsWithPrefix(topic, "cvut/nsi/2026/") && endsWithSuffix(topic, "/telemetry");
}

bool isForeignTelemetryTopic(const String &topic) {
  // Consider any telemetry topic as candidate; actual origin check is done by payload's "device" field.
  return isTelemetryTopic(topic);
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

void runWarningBlinkSequence() {
  Serial.println("[WARN] Starting warning blink sequence");
  bool prevState = ledIsOn;

  for (uint8_t i = 0; i < WARNING_BLINK_COUNT; ++i) {
    setLed(true);
    delay(WARNING_BLINK_PERIOD_MS / 2UL);
    setLed(false);
    delay(WARNING_BLINK_PERIOD_MS / 2UL);
  }

  // Restore previous LED state so user-controlled LED isn't lost
  setLed(prevState);
  Serial.print("[WARN] Warning blink sequence complete, restored LED state=");
  Serial.println(ledStatusText());
}

bool parseTemperatureFromTelemetry(const String &payload, float &temperatureOut) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    return false;
  }

  if (!doc["temperature"].is<float>() && !doc["temperature"].is<int>()) {
    return false;
  }

  temperatureOut = doc["temperature"].as<float>();
  return true;
}

bool parsePublishPeriodSeconds(const String &payload, unsigned long &periodOutSeconds) {
  String normalized = payload;
  normalized.trim();
  if (normalized.length() == 0) {
    return false;
  }

  char *endPtr = nullptr;
  unsigned long parsed = strtoul(normalized.c_str(), &endPtr, 10);
  if (endPtr == normalized.c_str() || *endPtr != '\0' || parsed == 0UL) {
    return false;
  }

  periodOutSeconds = parsed;
  return true;
}

void handleLedCommand(const String &payload) {
  Serial.print("[MQTT] handleLedCommand payload='");
  Serial.print(payload);
  Serial.println("'");

  String command = payload;
  command.trim();

  // Try plain text values first
  String upper = command;
  upper.toUpperCase();

  if (upper == "ON" || upper == "1" || upper == "TRUE") {
    setLed(true);
    Serial.println("[MQTT] LED command -> ON");
    return;
  }

  if (upper == "OFF" || upper == "0" || upper == "FALSE") {
    setLed(false);
    Serial.println("[MQTT] LED command -> OFF");
    return;
  }

  if (upper == "TOGGLE") {
    setLed(!ledIsOn);
    Serial.print("[MQTT] LED command -> TOGGLE, new state=");
    Serial.println(ledStatusText());
    return;
  }

  // If payload looks like JSON, try to extract a command field
  if (command.startsWith("{") && command.endsWith("}")) {
    DynamicJsonDocument doc(256);
    DeserializationError err = deserializeJson(doc, command);
    if (!err) {
      if (doc.containsKey("command")) {
        String cmd = doc["command"].as<String>();
        cmd.trim();
        cmd.toUpperCase();
        if (cmd == "ON") { setLed(true); Serial.println("[MQTT] LED JSON command -> ON"); return; }
        if (cmd == "OFF") { setLed(false); Serial.println("[MQTT] LED JSON command -> OFF"); return; }
        if (cmd == "TOGGLE") { setLed(!ledIsOn); Serial.println("[MQTT] LED JSON command -> TOGGLE"); return; }
      }

      // also accept numeric or boolean fields
      if (doc.containsKey("value")) {
        if (doc["value"].is<bool>()) {
          setLed(doc["value"].as<bool>());
          Serial.println("[MQTT] LED JSON value -> boolean");
          return;
        }
        if (doc["value"].is<int>()) {
          setLed(doc["value"].as<int>() != 0);
          Serial.println("[MQTT] LED JSON value -> int");
          return;
        }
      }
    }
  }

  Serial.print("[MQTT] Unsupported LED command/payload: ");
  Serial.println(payload);
}

void handlePeriodCommand(const String &payload) {
  Serial.print("[MQTT] handlePeriodCommand payload='");
  Serial.print(payload);
  Serial.println("'");

  unsigned long newPeriodSeconds = 0;
  if (!parsePublishPeriodSeconds(payload, newPeriodSeconds)) {
    // Try JSON with field "period_seconds" or "period"
    if (payload.startsWith("{") && payload.endsWith("}")) {
      DynamicJsonDocument doc(256);
      DeserializationError err = deserializeJson(doc, payload);
      if (!err) {
        if (doc.containsKey("period_seconds") || doc.containsKey("period")) {
          unsigned long p = 0;
          if (doc.containsKey("period_seconds")) p = doc["period_seconds"].as<unsigned long>();
          else p = doc["period"].as<unsigned long>();
          newPeriodSeconds = p;
        }
      }
    }
  }

  if (newPeriodSeconds < PUBLISH_PERIOD_MIN_SECONDS ||
      newPeriodSeconds > PUBLISH_PERIOD_MAX_SECONDS) {
    Serial.print("[MQTT] Invalid period seconds (allowed ");
    Serial.print(PUBLISH_PERIOD_MIN_SECONDS);
    Serial.print("..");
    Serial.print(PUBLISH_PERIOD_MAX_SECONDS);
    Serial.print("): ");
    Serial.println(newPeriodSeconds);
    return;
  }

  publishIntervalMs = newPeriodSeconds * 1000UL;
  Serial.print("[MQTT] Publish interval updated to ");
  Serial.print(publishIntervalMs);
  Serial.print(" ms (");
  Serial.print(newPeriodSeconds);
  Serial.println(" s)");
}

void handleForeignTelemetry(const String &payload) {
  Serial.print("[MQTT] handleForeignTelemetry payload='");
  Serial.print(payload);
  Serial.println("'");

  DynamicJsonDocument doc(512);
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    Serial.println("[MQTT] Could not parse foreign telemetry JSON.");
    return;
  }

  String originDevice = "";
  if (doc.containsKey("device")) {
    originDevice = doc["device"].as<String>();
  }

  if (originDevice.length() > 0 && originDevice == String(MQTT_CLIENT_ID)) {
    Serial.println("[MQTT] Ignoring own telemetry message.");
    return;
  }

  if (!doc.containsKey("temperature")) {
    Serial.println("[MQTT] No temperature field in foreign telemetry.");
    return;
  }

  float foreignTemperature = doc["temperature"].as<float>();
  Serial.print("[MQTT] Foreign temperature = ");
  Serial.println(foreignTemperature);

  if (foreignTemperature > 30.0f) {
    Serial.println("[WARN] Foreign telemetry above 30 C -> warning blink sequence.");
    runWarningBlinkSequence();
  }
}

void onMqttMessage(String &topic, String &payload) {
  Serial.print("[MQTT] Received on ");
  Serial.print(topic);
  Serial.print(" -> ");
  Serial.println(payload);

  if (topic == MQTT_TOPIC_STATUS) {
    Serial.print("[MQTT][DEBUG] Status topic observed: ");
    Serial.println(payload);
    return;
  }

  if (topic == MQTT_TOPIC_LED_COMMAND) {
    handleLedCommand(payload);
    return;
  }

  if (topic == MQTT_TOPIC_PERIOD_COMMAND) {
    handlePeriodCommand(payload);
    return;
  }

  if (isForeignTelemetryTopic(topic)) {
    handleForeignTelemetry(payload);
  }
}

void subscribeToMqttTopics() {
  bool okLed = mqttClient.subscribe(MQTT_TOPIC_LED_COMMAND, 1);
  bool okPeriod = mqttClient.subscribe(MQTT_TOPIC_PERIOD_COMMAND, 1);
  bool okTelemetryWildcard = mqttClient.subscribe(MQTT_TOPIC_TELEMETRY_WILDCARD, 1);
  bool okStatus = mqttClient.subscribe(MQTT_TOPIC_STATUS, 1);

  Serial.print("[MQTT] Subscribe LED command: ");
  Serial.println(okLed ? "OK" : "FAILED");
  Serial.print("[MQTT] Subscribe period command: ");
  Serial.println(okPeriod ? "OK" : "FAILED");
  Serial.print("[MQTT] Subscribe wildcard telemetry: ");
  Serial.println(okTelemetryWildcard ? "OK" : "FAILED");
  Serial.print("[MQTT] Subscribe status topic: ");
  Serial.println(okStatus ? "OK" : "FAILED");
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
  mqttClient.onMessage(onMqttMessage);
  mqttClient.setKeepAlive(MQTT_KEEP_ALIVE_SECONDS);
  mqttClient.setTimeout(MQTT_SOCKET_TIMEOUT_MS);
  Serial.println("[MQTT][DEBUG] Configuring Last Will message...");
  Serial.print("[MQTT][DEBUG] Will topic: ");
  Serial.println(MQTT_TOPIC_STATUS);
  Serial.print("[MQTT][DEBUG] Will payload: ");
  Serial.println(MQTT_STATUS_OFFLINE);
  Serial.println("[MQTT][DEBUG] Will retain=true qos=1");
  mqttClient.setWill(MQTT_TOPIC_STATUS, MQTT_STATUS_OFFLINE, true, 1);

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
      bool statusOk = mqttClient.publish(MQTT_TOPIC_STATUS, MQTT_STATUS_ONLINE, true, 1);
      Serial.print("[MQTT] Status publish ");
      Serial.print(statusOk ? "OK" : "FAILED");
      Serial.print(" -> ");
      Serial.print(MQTT_TOPIC_STATUS);
      Serial.print(" = ");
      Serial.println(MQTT_STATUS_ONLINE);
      Serial.println("[MQTT][DEBUG] If the device dies unexpectedly, broker should publish OFFLINE from Last Will.");
      subscribeToMqttTopics();
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

  // Health-status LED indicator removed: do not override LED here.
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
  doc["device"] = MQTT_CLIENT_ID;

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
  if (now - lastPublishMs >= publishIntervalMs) {
    lastPublishMs = now;
    publishTelemetry();
  }
}