#ifndef NSI_SECOND_MINI_PROJECT_CONFIG_H
#define NSI_SECOND_MINI_PROJECT_CONFIG_H

#include <Arduino.h>
#include <DHT.h>

// ============================================================================
// Wi-Fi configuration
// ============================================================================
static constexpr char WIFI_SSID[] = "YOUR_WIFI_SSID";
static constexpr char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";

// ============================================================================
// MQTT broker configuration
// ============================================================================
static constexpr char MQTT_HOST[] = "broker.example.com";
static constexpr uint16_t MQTT_PORT = 1883;
static constexpr char MQTT_CLIENT_ID[] = "esp32-dht11-client";
static constexpr char MQTT_USERNAME[] = "";
static constexpr char MQTT_PASSWORD[] = "";
static constexpr char MQTT_TOPIC[] = "cvut/nsi/2026/majejirji5/telemetry";

// Connection behavior
static constexpr int MQTT_KEEP_ALIVE_SECONDS = 30;
static constexpr int MQTT_SOCKET_TIMEOUT_MS = 2000;

// ============================================================================
// NTP configuration (UTC output with Z suffix)
// ============================================================================
static constexpr char NTP_SERVER_1[] = "pool.ntp.org";
static constexpr char NTP_SERVER_2[] = "time.google.com";
static constexpr char NTP_SERVER_3[] = "time.cloudflare.com";
static constexpr long NTP_GMT_OFFSET_SECONDS = 0;
static constexpr int NTP_DAYLIGHT_OFFSET_SECONDS = 0;
static constexpr unsigned long NTP_SYNC_TIMEOUT_MS = 15000UL;

// ============================================================================
// Hardware pin mapping
// ============================================================================
static constexpr uint8_t DHT_PIN = 0;
static constexpr uint8_t LED_PIN = 4;
static constexpr uint8_t DHT_TYPE = DHT11;

// ============================================================================
// Application timing
// ============================================================================
static constexpr unsigned long PUBLISH_INTERVAL_MS = 10000UL;
static constexpr uint32_t SERIAL_BAUD_RATE = 115200;

#endif