"use strict";

// -----------------------------------------------------------------------------
// Frontend constants and element references
// -----------------------------------------------------------------------------

const dashboardElement = document.getElementById("dashboard");
const measurementDateTimeElement = document.getElementById("measurement-datetime");
const ledStatusElement = document.getElementById("led-status");
const runtimeElement = document.getElementById("runtime");
const temperatureElement = document.getElementById("temperature");
const ledOnButton = document.getElementById("led-on");
const ledOffButton = document.getElementById("led-off");
const ledToggleButton = document.getElementById("led-toggle");
const sendTemperatureTestButton = document.getElementById("send-temperature-test");
const commandStatusElement = document.getElementById("command-status");

const apiPath = dashboardElement.dataset.apiPath;
const ledCommandPath = dashboardElement.dataset.ledCommandPath;
const temperatureTestPath = dashboardElement.dataset.temperatureTestPath;
const refreshMs = Number(dashboardElement.dataset.refreshMs || "2000");

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

function formatTemperature(temperatureCelsius) {
    if (temperatureCelsius === null || temperatureCelsius === undefined || temperatureCelsius === "") {
        return "-- °C";
    }

    const numericValue = Number(temperatureCelsius);
    if (Number.isNaN(numericValue)) {
        return `${temperatureCelsius}`;
    }

    return `${numericValue.toFixed(1)} °C`;
}

// -----------------------------------------------------------------------------
// Rendering and polling logic
// -----------------------------------------------------------------------------

function renderData(data) {
    measurementDateTimeElement.textContent = data.date || "Waiting for data...";
    ledStatusElement.textContent = data.ledstatus || "unknown";
    runtimeElement.textContent = formatRuntime(data.runtime);
    temperatureElement.textContent = formatTemperature(data.temperature);
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

// First fetch immediately, then continue polling in fixed intervals.
loadLatestData();
window.setInterval(loadLatestData, refreshMs);
