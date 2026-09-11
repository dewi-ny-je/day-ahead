// Bootstrap
import * as bootstrap from 'bootstrap'
import 'bootstrap-icons/font/bootstrap-icons.css'

// HTMX
import htmx from 'htmx.org'

window.htmx = htmx

// Chart.js
import {
    Chart,
    LineController,
    BarController,
    PieController,
    DoughnutController,
    LineElement,
    BarElement,
    ArcElement,
    PointElement,
    LinearScale,
    CategoryScale,
    Tooltip,
    Legend
} from 'chart.js'

Chart.register(
    LineController,
    BarController,
    PieController,
    DoughnutController,
    LineElement,
    BarElement,
    ArcElement,
    PointElement,
    LinearScale,
    CategoryScale,
    Tooltip,
    Legend
)

window.Chart = Chart

function fillCurrentTimezoneFields(root = document) {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

    root
        .querySelectorAll('input[data-current-tz], select[data-current-tz], textarea[data-current-tz]')
        .forEach((field) => {
            field.value = timezone;
        });
}

// CodeMirror is only needed on the config, secrets and log pages, so it is
// loaded on demand to keep it out of the main bundle.
function loadCodeEditors() {
    if (!document.querySelector('[data-code-editor]')) return;

    import('./code-editor.js').then((module) => module.initCodeEditors());
}

document.addEventListener('DOMContentLoaded', () => {
    fillCurrentTimezoneFields();

    loadCodeEditors();

    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]')
    const tooltipList = [...tooltipTriggerList].map(tooltipTriggerEl => new bootstrap.Tooltip(tooltipTriggerEl))
});

document.body.addEventListener("htmx:responseError", function (event) {
    const errorElement = document.getElementById("htmx-error");
    const messageElement = document.getElementById("htmx-error-message");

    const response = event.detail.xhr.responseText;
    const status = event.detail.xhr.status;

    messageElement.textContent =
        response || `Er is een fout opgetreden (${status}).`;

    errorElement.classList.remove("d-none");
});

document.body.addEventListener("htmx:sendError", function () {
    const errorElement = document.getElementById("htmx-error");
    const messageElement = document.getElementById("htmx-error-message");

    messageElement.textContent =
        "De server kon niet worden bereikt.";

    errorElement.classList.remove("d-none");
});

import TomSelect from "tom-select";
import "tom-select/dist/css/tom-select.bootstrap5.css";

document.querySelectorAll('.tom-select').forEach((el) => {
    let settings = {plugins: ['change_listener'],};
    new TomSelect(el, settings);
});


// Eigen styling als laatste
import './main.scss'

window.toDatetimeLocalValue = (date, withTime = true) => {
    const pad = n => String(n).padStart(2, '0');

    return [
        date.getFullYear(),
        pad(date.getMonth() + 1),
        pad(date.getDate()),
    ].join('-') + (withTime ? 'T' + [
        pad(date.getHours()),
        pad(date.getMinutes()),
    ].join(':') : '');
}