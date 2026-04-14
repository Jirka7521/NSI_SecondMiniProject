"use strict";

// -----------------------------------------------------------------------------
// Frontend constants and element references
// -----------------------------------------------------------------------------

const dashboardElement = document.getElementById("dashboard");
const measurementDateTimeElement = document.getElementById("measurement-datetime");
const ledStatusElement = document.getElementById("led-status");
const runtimeElement = document.getElementById("runtime");
const temperatureElement = document.getElementById("temperature");

const apiPath = dashboardElement.dataset.apiPath;
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

// First fetch immediately, then continue polling in fixed intervals.
loadLatestData();
window.setInterval(loadLatestData, refreshMs);
