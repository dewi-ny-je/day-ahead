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
    Title,
    Tooltip,
    Legend
} from 'chart.js'

import {renderChartSpec} from './dao-chart.js'

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
    Title,
    Tooltip,
    Legend
)

window.Chart = Chart

// Laat Chart.js het actieve Bootstrap-thema volgen.
function applyChartTheme() {
    const styles = getComputedStyle(document.documentElement)

    Chart.defaults.color = styles.getPropertyValue('--bs-body-color').trim()
    Chart.defaults.borderColor = styles.getPropertyValue('--bs-border-color').trim()

    Chart.instances && Object.values(Chart.instances).forEach(chart => chart.update())
}

applyChartTheme()

new MutationObserver(applyChartTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-bs-theme'],
})

window.renderChartSpec = renderChartSpec
