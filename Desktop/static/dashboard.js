"use strict";

// -----------------------------------------------------------------------------
// Frontend constants and element references
// -----------------------------------------------------------------------------

const dashboardElement = document.getElementById("dashboard");
const measurementDateTimeElement = document.getElementById("measurement-datetime");
const ledStatusElement = document.getElementById("led-status");
const runtimeElement = document.getElementById("runtime");
const temperatureElement = document.getElementById("temperature");
const deviceStatusBannerElement = document.getElementById("device-status-banner");
const deviceStatusValueElement = document.getElementById("device-status-value");
const deviceStatusUpdatedElement = document.getElementById("device-status-updated");
const temperatureUnitSelectElement = document.getElementById("temperature-unit");
const timeZoneSelectElement = document.getElementById("time-zone");
const ledOnButton = document.getElementById("led-on");
const ledOffButton = document.getElementById("led-off");
const ledToggleButton = document.getElementById("led-toggle");
const sendTemperatureTestButton = document.getElementById("send-temperature-test");
const telemetryPeriodValueElement = document.getElementById("telemetry-period-value");
const telemetryPeriodUnitElement = document.getElementById("telemetry-period-unit");
const setTelemetryPeriodButton = document.getElementById("set-telemetry-period");
const commandStatusElement = document.getElementById("command-status");

const apiPath = dashboardElement.dataset.apiPath;
const ledCommandPath = dashboardElement.dataset.ledCommandPath;
const temperatureTestPath = dashboardElement.dataset.temperatureTestPath;
const updatePeriodPath = dashboardElement.dataset.updatePeriodPath;
const refreshMs = Number(dashboardElement.dataset.refreshMs || "2000");
const TEMPERATURE_UNIT_COOKIE = "dashboard_temperature_unit";
const TIMEZONE_COOKIE = "dashboard_time_zone";

let selectedTemperatureUnit = "C";
let selectedTimeZone = "LOCAL";

// -----------------------------------------------------------------------------
// Small formatting helper functions
// -----------------------------------------------------------------------------

function formatRuntime(runtimeSeconds) {
    if (runtimeSeconds === null || runtimeSeconds === undefined || runtimeSeconds === "") {
        return "-- s";
    }

    const numericValue = Number(runtimeSeconds);
    if (Number.isNaN(numericValue)) {
        return `${runtimeSeconds}`;
    }

    return `${numericValue} s`;
}

function celsiusToFahrenheit(celsiusValue) {
    return (celsiusValue * 9) / 5 + 32;
}

function formatTemperature(temperatureCelsius) {
    if (temperatureCelsius === null || temperatureCelsius === undefined || temperatureCelsius === "") {
        return selectedTemperatureUnit === "F" ? "-- °F" : "-- °C";
    }

    const numericValue = Number(temperatureCelsius);
    if (Number.isNaN(numericValue)) {
        return `${temperatureCelsius}`;
    }

    if (selectedTemperatureUnit === "F") {
        return `${celsiusToFahrenheit(numericValue).toFixed(1)} °F`;
    }

    return `${numericValue.toFixed(1)} °C`;
}

function formatDateTime(valueIsoString, timeZone) {
    if (!valueIsoString) {
        return "Waiting for data...";
    }

    // Some payloads may already be formatted; try parsing to Date.
    const parsed = new Date(valueIsoString);
    if (Number.isNaN(parsed.getTime())) {
        return valueIsoString;
    }

    if (!timeZone || timeZone === "LOCAL") {
        return parsed.toLocaleString();
    }

    try {
        const opts = { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone };
        return new Intl.DateTimeFormat(undefined, opts).format(parsed);
    } catch (err) {
        return parsed.toLocaleString();
    }
}

function getCookieValue(name) {
    const encodedName = `${encodeURIComponent(name)}=`;
    const cookies = document.cookie ? document.cookie.split(";") : [];

    for (const cookiePart of cookies) {
        const trimmedPart = cookiePart.trim();
        if (trimmedPart.startsWith(encodedName)) {
            return decodeURIComponent(trimmedPart.slice(encodedName.length));
        }
    }

    return null;
}

function setCookie(name, value, maxAgeSeconds) {
    document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}; path=/; max-age=${maxAgeSeconds}; samesite=lax`;
}

function loadTemperatureUnitFromCookie() {
    const cookieUnit = (getCookieValue(TEMPERATURE_UNIT_COOKIE) || "").toUpperCase();
    if (cookieUnit === "F") {
        selectedTemperatureUnit = "F";
    } else {
        selectedTemperatureUnit = "C";
    }

    if (temperatureUnitSelectElement) {
        temperatureUnitSelectElement.value = selectedTemperatureUnit;
    }
}

function loadTimeZoneFromCookie() {
    const cookieTz = getCookieValue(TIMEZONE_COOKIE);
    if (cookieTz) {
        selectedTimeZone = cookieTz;
    } else {
        // default to browser timezone
        selectedTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'LOCAL';
    }

    if (timeZoneSelectElement) {
        // If the cookie contains a zone not present in select, add it
        const exists = Array.from(timeZoneSelectElement.options).some(o => o.value === selectedTimeZone);
        if (!exists) {
            const opt = document.createElement('option');
            opt.value = selectedTimeZone;
            opt.textContent = selectedTimeZone;
            timeZoneSelectElement.appendChild(opt);
        }
        timeZoneSelectElement.value = selectedTimeZone;
    }
}

function updateDeviceStatusUi(statusValue, statusUpdatedAt) {
    if (!deviceStatusBannerElement || !deviceStatusValueElement || !deviceStatusUpdatedElement) {
        return;
    }

    const normalizedStatus = String(statusValue || "UNKNOWN").toUpperCase();
    let className = "status-unknown";

    if (normalizedStatus === "ONLINE") {
        className = "status-online";
    } else if (normalizedStatus === "OFFLINE") {
        className = "status-offline";
    }

    deviceStatusBannerElement.classList.remove("status-online", "status-offline", "status-unknown");
    deviceStatusBannerElement.classList.add(className);
    deviceStatusValueElement.textContent = normalizedStatus;
    deviceStatusUpdatedElement.textContent = statusUpdatedAt
        ? `Last status update: ${formatDateTime(statusUpdatedAt, selectedTimeZone)}`
        : "Waiting for status message...";
}

// -----------------------------------------------------------------------------
// Rendering and polling logic
// -----------------------------------------------------------------------------

function renderData(data) {
    measurementDateTimeElement.textContent = formatDateTime(data.date, selectedTimeZone);
    ledStatusElement.textContent = data.ledstatus || "unknown";
    runtimeElement.textContent = formatRuntime(data.runtime);
    temperatureElement.textContent = formatTemperature(data.temperature);
    updateDeviceStatusUi(data.device_status, data.device_status_updated_at);
}

function setCommandStatus(message, isError = false) {
    if (!commandStatusElement) {
        return;
    }

    commandStatusElement.textContent = message;
    commandStatusElement.classList.toggle("error", isError);
}

async function postJson(path, body) {
    const response = await fetch(path, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify(body || {}),
    });

    const responseData = await response.json();
    if (!response.ok) {
        throw new Error(responseData.message || `Request failed: ${response.status}`);
    }

    return responseData;
}

async function sendLedCommand(command) {
    try {
        const result = await postJson(ledCommandPath, { command });
        setCommandStatus(`LED command sent: ${result.command}`);
    } catch (error) {
        setCommandStatus(`LED command failed: ${error.message}`, true);
        console.error(error);
    }
}

async function sendTemperatureTest() {
    try {
        const result = await postJson(temperatureTestPath, {});
        setCommandStatus(
            `Test telemetry sent: ${result.payload.temperature} °C from ${result.payload.device}`
        );
    } catch (error) {
        setCommandStatus(`Telemetry test failed: ${error.message}`, true);
        console.error(error);
    }
}

function toPeriodSeconds(rawValue, rawUnit) {
    // Require integer input in the UI. The rawValue is expected to be
    // an integer string (e.g. "10" or "2"), not a float.
    const numericValue = Number(rawValue);
    if (!Number.isFinite(numericValue) || numericValue <= 0) {
        return null;
    }

    if (!Number.isInteger(numericValue)) {
        // Signal invalid (non-integer) to caller by returning null.
        return null;
    }

    if (rawUnit === "m") {
        return numericValue * 60;
    }

    return numericValue;
}

async function sendTelemetryPeriodUpdate() {
    if (!telemetryPeriodValueElement || !telemetryPeriodUnitElement) {
        return;
    }

    const selectedUnit = telemetryPeriodUnitElement.value === "m" ? "m" : "s";
    const inputValue = telemetryPeriodValueElement.value;
    // Clear previous input error state
    telemetryPeriodValueElement.classList.remove('input-error');
    setCommandStatus('');

    const periodSeconds = toPeriodSeconds(inputValue, selectedUnit);

    if (periodSeconds === null) {
        telemetryPeriodValueElement.classList.add('input-error');
        setCommandStatus('Telemetry period must be an integer > 0.', true);
        return;
    }

    if (periodSeconds < 1 || periodSeconds > 300) {
        telemetryPeriodValueElement.classList.add('input-error');
        setCommandStatus('Telemetry period must be between 1 second and 5 minutes.', true);
        return;
    }

    const query = new URLSearchParams({ period: String(inputValue), unit: selectedUnit });
    const requestPath = `${updatePeriodPath}?${query.toString()}`;

    try {
        const response = await fetch(requestPath, { method: "POST" });
        const payload = await response.json();

        if (!response.ok) {
            throw new Error(payload.message || `Request failed: ${response.status}`);
        }

        setCommandStatus(
            `Telemetry period updated: ${payload.period_seconds} s -> topic ${payload.topic}`
        );
    } catch (error) {
        setCommandStatus(`Telemetry period update failed: ${error.message}`, true);
        console.error(error);
    }
}

async function loadLatestData() {
    try {
        const response = await fetch(apiPath, { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`Request failed: ${response.status}`);
        }

        const latest = await response.json();
        renderData(latest);
    } catch (error) {
        // Keep the page usable even if temporary network/server issue appears.
        measurementDateTimeElement.textContent = "Cannot load data";
        console.error(error);
    }
}

if (ledOnButton) {
    ledOnButton.addEventListener("click", () => sendLedCommand("ON"));
}

if (ledOffButton) {
    ledOffButton.addEventListener("click", () => sendLedCommand("OFF"));
}

if (ledToggleButton) {
    ledToggleButton.addEventListener("click", () => sendLedCommand("TOGGLE"));
}

if (sendTemperatureTestButton) {
    sendTemperatureTestButton.addEventListener("click", sendTemperatureTest);
}

if (setTelemetryPeriodButton) {
    setTelemetryPeriodButton.addEventListener("click", sendTelemetryPeriodUpdate);
}

if (temperatureUnitSelectElement) {
    temperatureUnitSelectElement.addEventListener("change", () => {
        selectedTemperatureUnit = temperatureUnitSelectElement.value === "F" ? "F" : "C";
        setCookie(TEMPERATURE_UNIT_COOKIE, selectedTemperatureUnit, 60 * 60 * 24 * 365);
        loadLatestData();
    });
}

if (timeZoneSelectElement) {
    timeZoneSelectElement.addEventListener('change', () => {
        selectedTimeZone = timeZoneSelectElement.value || 'LOCAL';
        setCookie(TIMEZONE_COOKIE, selectedTimeZone, 60 * 60 * 24 * 365);
        loadLatestData();
    });
}

loadTemperatureUnitFromCookie();
loadTimeZoneFromCookie();

// First fetch immediately, then continue polling in fixed intervals.
loadLatestData();
window.setInterval(loadLatestData, refreshMs);
