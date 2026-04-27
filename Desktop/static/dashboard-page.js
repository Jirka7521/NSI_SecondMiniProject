"use strict";

// -----------------------------------------------------------------------------
// Element references and API endpoint setup
// -----------------------------------------------------------------------------

const scriptElement = document.currentScript;
const devicesApiPath = scriptElement?.dataset.devicesApi || "/api/dashboard/devices";
const historyApiPath = scriptElement?.dataset.historyApi || "/api/dashboard/temperature-history";

const historyFormElement = document.getElementById("history-form");
const deviceSelectElement = document.getElementById("device-select");
const relativeControlsElement = document.getElementById("relative-controls");
const absoluteControlsElement = document.getElementById("absolute-controls");
const fromInputElement = document.getElementById("from-input");
const toInputElement = document.getElementById("to-input");
const windowValueElement = document.getElementById("window-value");
const windowUnitElement = document.getElementById("window-unit");
const formMessageElement = document.getElementById("form-message");
const chartCanvasElement = document.getElementById("temperature-chart");

// We keep chart instance globally so every new search can replace old graph
// cleanly without stacking multiple chart objects in memory.
let temperatureChart = null;

// -----------------------------------------------------------------------------
// General utility functions
// -----------------------------------------------------------------------------

function setFormMessage(message, isError = false) {
    if (!formMessageElement) {
        return;
    }

    formMessageElement.textContent = message;
    formMessageElement.classList.toggle("is-error", isError);
}

function getSelectedMode() {
    const selectedRadio = document.querySelector('input[name="mode"]:checked');
    return selectedRadio?.value === "absolute" ? "absolute" : "relative";
}

function formatIsoToLocalLabel(isoText) {
    // If parsing fails (unexpected value), we still show original text.
    const parsed = new Date(isoText);
    if (Number.isNaN(parsed.getTime())) {
        return String(isoText || "");
    }

    // A compact but still readable format for X-axis labels.
    return new Intl.DateTimeFormat("cs-CZ", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    }).format(parsed);
}

function toIsoStringFromDatetimeLocal(localValue) {
    // `datetime-local` has no timezone info; browser Date interprets it as local time.
    // We convert to UTC ISO so the backend gets an unambiguous timestamp.
    if (!localValue) {
        return null;
    }

    const parsed = new Date(localValue);
    if (Number.isNaN(parsed.getTime())) {
        return null;
    }

    return parsed.toISOString();
}

// -----------------------------------------------------------------------------
// Form mode toggling (absolute vs relative time window)
// -----------------------------------------------------------------------------

function updateModeVisibility() {
    const mode = getSelectedMode();
    const showAbsolute = mode === "absolute";

    absoluteControlsElement?.classList.toggle("hidden", !showAbsolute);
    absoluteControlsElement?.setAttribute("aria-hidden", showAbsolute ? "false" : "true");

    relativeControlsElement?.classList.toggle("hidden", showAbsolute);
    relativeControlsElement?.setAttribute("aria-hidden", showAbsolute ? "true" : "false");
}

function bindModeEvents() {
    const modeRadios = document.querySelectorAll('input[name="mode"]');
    for (const radio of modeRadios) {
        radio.addEventListener("change", updateModeVisibility);
    }
}

// -----------------------------------------------------------------------------
// Device loading
// -----------------------------------------------------------------------------

async function loadDevices() {
    if (!deviceSelectElement) {
        return;
    }

    try {
        const response = await fetch(devicesApiPath, { cache: "no-store" });
        const payload = await response.json();

        if (!response.ok) {
            throw new Error(payload.message || `Request failed: ${response.status}`);
        }

        const devices = Array.isArray(payload.devices) ? payload.devices : [];
        deviceSelectElement.innerHTML = "";

        if (devices.length === 0) {
            const emptyOption = document.createElement("option");
            emptyOption.value = "";
            emptyOption.textContent = "Žádné zařízení";
            deviceSelectElement.appendChild(emptyOption);
            setFormMessage("Nebylo nalezeno žádné registrované zařízení.", true);
            return;
        }

        const placeholderOption = document.createElement("option");
        placeholderOption.value = "";
        placeholderOption.textContent = "Vyberte zařízení";
        deviceSelectElement.appendChild(placeholderOption);

        for (const device of devices) {
            const option = document.createElement("option");
            option.value = String(device.device_id);
            option.textContent = String(device.device_id);
            deviceSelectElement.appendChild(option);
        }

        setFormMessage("Zařízení načtena. Zvolte filtr a potvrďte formulář.");
    } catch (error) {
        console.error(error);
        deviceSelectElement.innerHTML = '<option value="">Chyba načítání zařízení</option>';
        setFormMessage(`Nepodařilo se načíst zařízení: ${error.message}`, true);
    }
}

// -----------------------------------------------------------------------------
// Building API query from form inputs
// -----------------------------------------------------------------------------

function buildHistoryRequestUrl() {
    const selectedDeviceId = deviceSelectElement?.value?.trim();
    if (!selectedDeviceId) {
        throw new Error("Vyberte prosím zařízení.");
    }

    const mode = getSelectedMode();
    const params = new URLSearchParams();
    params.set("device_id", selectedDeviceId);
    params.set("mode", mode);

    if (mode === "absolute") {
        const fromIso = toIsoStringFromDatetimeLocal(fromInputElement?.value || "");
        const toIso = toIsoStringFromDatetimeLocal(toInputElement?.value || "");

        if (!fromIso || !toIso) {
            throw new Error("V absolutním režimu vyplňte oba časy Od/Do.");
        }

        params.set("from", fromIso);
        params.set("to", toIso);
    } else {
        const rawWindowValue = Number(windowValueElement?.value || "");
        if (!Number.isInteger(rawWindowValue) || rawWindowValue <= 0) {
            throw new Error("Velikost relativního okna musí být celé číslo > 0.");
        }

        const selectedUnit = windowUnitElement?.value || "minute";
        params.set("window_value", String(rawWindowValue));
        params.set("window_unit", selectedUnit);
    }

    return `${historyApiPath}?${params.toString()}`;
}

// -----------------------------------------------------------------------------
// Chart rendering
// -----------------------------------------------------------------------------

function renderTemperatureChart(responsePayload) {
    if (!chartCanvasElement) {
        return;
    }

    const points = Array.isArray(responsePayload.points) ? responsePayload.points : [];
    const labels = points.map((point) => formatIsoToLocalLabel(point.timestamp));
    const values = points.map((point) => Number(point.temperature));

    if (temperatureChart) {
        temperatureChart.destroy();
        temperatureChart = null;
    }

    if (points.length === 0) {
        setFormMessage("V tomto časovém okně nejsou dostupná žádná data.");
        return;
    }

    const denseDataset = points.length >= 120;

    temperatureChart = new Chart(chartCanvasElement, {
        type: "line",
        data: {
            labels,
            datasets: [
                {
                    label: "Teplota [°C]",
                    data: values,
                    borderColor: "#b91c1c",
                    backgroundColor: "rgba(185, 28, 28, 0.15)",
                    borderWidth: 2,
                    fill: true,
                    pointRadius: denseDataset ? 0 : 2,
                    pointHoverRadius: denseDataset ? 2 : 4,
                    tension: 0.18,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 700,
                easing: "easeOutQuart",
            },
            scales: {
                x: {
                    title: {
                        display: true,
                        text: "Čas měření",
                    },
                    ticks: {
                        maxRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: 12,
                    },
                },
                y: {
                    title: {
                        display: true,
                        text: "Teplota [°C]",
                    },
                },
            },
            plugins: {
                title: {
                    display: true,
                    text: `Vývoj teploty - zařízení ${responsePayload.device_id}`,
                },
                legend: {
                    display: true,
                },
                // Built-in decimation keeps chart readable for hundreds of points.
                decimation: {
                    enabled: true,
                    algorithm: "lttb",
                    samples: 200,
                },
            },
        },
    });

    setFormMessage(
        `Načteno ${responsePayload.count} záznamů (${formatIsoToLocalLabel(responsePayload.from)} -> ${formatIsoToLocalLabel(responsePayload.to)}).`
    );
}

// -----------------------------------------------------------------------------
// Fetch + submit flow
// -----------------------------------------------------------------------------

async function fetchAndRenderHistory() {
    const requestUrl = buildHistoryRequestUrl();
    setFormMessage("Načítám data...");

    const response = await fetch(requestUrl, { cache: "no-store" });
    const payload = await response.json();

    if (!response.ok) {
        throw new Error(payload.message || `Request failed: ${response.status}`);
    }

    renderTemperatureChart(payload);
}

function bindFormSubmit() {
    if (!historyFormElement) {
        return;
    }

    historyFormElement.addEventListener("submit", async (event) => {
        event.preventDefault();

        try {
            await fetchAndRenderHistory();
        } catch (error) {
            console.error(error);
            setFormMessage(`Načtení grafu selhalo: ${error.message}`, true);
        }
    });
}

// -----------------------------------------------------------------------------
// App bootstrap
// -----------------------------------------------------------------------------

bindModeEvents();
updateModeVisibility();
bindFormSubmit();
loadDevices();
