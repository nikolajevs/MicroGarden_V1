let currentSelectedDate = "";
let deviceDate = "";         // Сегодняшняя дата по часам платы (из /api/status)
let userPickedDate = false;  // Пользователь сам выбрал в календаре другой день
let chartPoints = [];
let chartViewBox = { width: 1000, height: 400 };

let statusInFlight = false;
let lastStatusOkMs = 0;
const STATUS_TIMEOUT_MS = 5000;  // Зависший запрос иначе навсегда блокировал следующие опросы
const STALE_AFTER_MS = 10000;    // Столько без ответа платы — и показания считаются устаревшими

// Без этого панель при потере связи продолжала показывать последние значения как живые
function setLinkLost(lost) {
    const grid = document.getElementById('live-grid');
    if (grid) grid.classList.toggle('stale', lost);
    const timeEl = document.getElementById('box-time');
    if (lost) {
        timeEl.innerText = 'Нет связи';
        timeEl.className = "status-badge offline-badge";
        renderAlerts([Alerts.LINK_LOST_ALERT]); // без связи остальные оповещения недостоверны
    } else {
        timeEl.className = "status-badge time-badge";
    }
}

async function updateStatus() {
    if (statusInFlight || document.hidden) return;
    statusInFlight = true;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), STATUS_TIMEOUT_MS);
    try {
        const res = await fetch('/api/status', { signal: abort.signal });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        lastStatusOkMs = Date.now();
        setLinkLost(false);
        renderStatus(data);
    } catch (e) {
        console.error("Ошибка API:", e);
        if (Date.now() - lastStatusOkMs > STALE_AFTER_MS) setLinkLost(true);
    } finally {
        clearTimeout(timer);
        statusInFlight = false;
    }
}

function renderStatus(data) {
    if (data.sht_online) {
        document.getElementById('t-val').innerHTML = data.temp.toFixed(1) + '<span class="unit">°C</span>';
        document.getElementById('h-val').innerHTML = data.hum.toFixed(1) + '<span class="unit">%</span>';
        document.getElementById('sensor-status-badge').style.display = 'none';
    } else {
        document.getElementById('t-val').innerHTML = '--:--';
        document.getElementById('h-val').innerHTML = '--:--';
        document.getElementById('sensor-status-badge').style.display = 'inline-block';
    }
    document.getElementById('led-val').innerHTML = Math.round(data.led / 2.55) + '<span class="unit">%</span>';
    document.getElementById('fan1-val').innerHTML = Math.round(data.fan1 / 2.55) + '<span class="unit">%</span>';
    document.getElementById('fan2-val').innerHTML = Math.round(data.fan2 / 2.55) + '<span class="unit">%</span>';
    renderPower(data);

    document.getElementById('box-time').innerText = data.time;

    const pumpEl = document.getElementById('pump-val');
    if (data.pump_active) {
        pumpEl.innerText = 'ВКЛЮЧЕН 💦';
        pumpEl.style.color = 'var(--warning)';
    } else {
        pumpEl.innerText = 'Выключен';
        pumpEl.style.color = 'var(--text-muted)';
    }
    const waterEl = document.getElementById('water-level');
    const noWater = data.water_sensor_enabled && !data.water_present;
    waterEl.textContent = !data.water_sensor_enabled ? 'Датчик воды выключен'
        : data.water_present ? 'Вода в баке: есть' : 'Вода в баке: нет';
    waterEl.classList.toggle('warn', noWater);
    // День, когда сработал полив по расписанию (и когда он был пропущен из-за пустого бака)
    document.getElementById('last-water').textContent = 'Полив по расписанию: ' +
        formatWaterDay(data.last_water_day, data.date) + (data.last_water_skipped ? ' — пропущен' : '');

    const heaterEl = document.getElementById('heater-val');
    if (data.heater_active) {
        heaterEl.innerText = 'ВКЛЮЧЕН 🌞';
        heaterEl.style.color = 'var(--warning)';
    } else {
        heaterEl.innerText = 'Выключен';
        heaterEl.style.color = 'var(--text-muted)';
    }
    // Сколько нагреватель проработал за сутки платы (с полуночи или с последнего включения платы)
    document.getElementById('heater-today').textContent = 'Сегодня: ' + Format.formatWorkTime(data.heater_today_s);

    // График следует за сегодняшним днём платы, пока пользователь сам не выбрал другой:
    // и при первой загрузке, и когда на плате наступила полночь
    deviceDate = data.date;
    if (!userPickedDate && currentSelectedDate !== data.date) {
        currentSelectedDate = data.date;
        const dateInput = document.getElementById('chart-date-selector');
        if (dateInput) dateInput.value = data.date;
        drawSvgChart();
    }

    // В сушке нет ни дня с ночью, ни цикла выращивания — раньше шапка всё равно
    // показывала "День · День цикла: N", и понять по главной, что идёт сушка, было нельзя
    const drying = data.operation_mode === 1;
    const cycleEl = document.getElementById('cycle-str');
    const phaseEl = document.getElementById('cycle-phase');
    const cycleDayEl = document.getElementById('cycle-day');
    if (drying) {
        phaseEl.textContent = '🍂 Сушка';
        cycleEl.className = "status-badge cycle-badge drying-badge";
    } else if (data.is_day) {
        phaseEl.textContent = '☀️ День';
        cycleEl.className = "status-badge cycle-badge day-badge";
    } else {
        phaseEl.textContent = '🌙 Ночь';
        cycleEl.className = "status-badge cycle-badge night-badge";
    }
    document.getElementById('cycle-divider').hidden = drying;
    cycleDayEl.hidden = drying;
    cycleDayEl.textContent = data.grow_day > 0 ? `День цикла: ${data.grow_day}` : 'Цикл не начат';

    // В сушке свет и полив выключены всегда, поэтому лампу и насос с главной убираем
    document.querySelectorAll('[data-growing-only]').forEach(el => { el.hidden = drying; });
    document.getElementById('live-grid').classList.toggle('drying', drying);

    // Неисправность часов (RTC не отвечает, время не выставлено, полив на программном таймере или выключен)
    // показывает колокольчик: правила и тексты в alerts.js.
    renderAlerts(Alerts.collectAlerts(data));
}

// Колокольчик в шапке: число активных оповещений и их список (правила — в alerts.js).
// Оповещения — текущие неисправности; они исчезают, когда причина устранена.
let lastAlertCount = null; // null — первый показ после загрузки страницы: качать нечего

function renderAlerts(alerts) {
    const { count, level } = Alerts.summarizeAlerts(alerts);
    const badge = document.getElementById('alerts-count');
    badge.hidden = count === 0;
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.className = 'bell-count' + (level === 'warn' ? ' warn' : '');
    const bell = document.getElementById('alerts-bell');
    bell.setAttribute('aria-label', count ? `Оповещений: ${count}` : 'Оповещений нет');
    bell.classList.toggle('is-bad', level === 'bad'); // цвет значка — по самому тяжёлому оповещению
    bell.classList.toggle('is-warn', level === 'warn');
    if (count > lastAlertCount && lastAlertCount !== null) { // новое оповещение: колокольчик качнётся
        bell.classList.remove('ring');
        void bell.offsetWidth; // перезапуск анимации
        bell.classList.add('ring');
    }
    lastAlertCount = count;
    document.title = (count ? `(${count}) ` : '') + 'MicroGarden V1';

    const list = document.getElementById('alerts-list');
    list.textContent = '';
    for (const alert of alerts) {
        const item = document.createElement('li');
        const dot = document.createElement('span');
        dot.className = 'alert-dot ' + alert.level;
        const text = document.createElement('span');
        text.textContent = alert.text; // текст только из alerts.js, но вставляем как текст
        item.appendChild(dot);
        item.appendChild(text);
        list.appendChild(item);
    }
    document.getElementById('alerts-empty').hidden = count > 0;
}

function setAlertsPanel(open) {
    document.getElementById('alerts-panel').hidden = !open;
    document.getElementById('alerts-bell').setAttribute('aria-expanded', open ? 'true' : 'false');
}

document.getElementById('alerts-bell').addEventListener('click', (e) => {
    e.stopPropagation();
    setAlertsPanel(document.getElementById('alerts-panel').hidden);
});
document.addEventListener('click', (e) => {
    const panel = document.getElementById('alerts-panel');
    if (!panel.hidden && !panel.contains(e.target)) setAlertsPanel(false);
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setAlertsPanel(false);
});

function renderPower(data) {
    const valueEl = document.getElementById('power-val');
    const detailsEl = document.getElementById('power-details');
    const warnEl = document.getElementById('power-warning');
    if (data.power_ok) {
        const watts = data.power_w < 10 ? data.power_w.toFixed(1) : Math.round(data.power_w);
        valueEl.innerHTML = watts + '<span class="unit">Вт</span>';
        detailsEl.textContent = `${data.bus_v.toFixed(1)} В · ${data.current_a.toFixed(2)} А`;
        warnEl.textContent = 'Свет выключен: низкое питание; включение разрешится от 22,5 В';
        warnEl.hidden = !data.led_supply_blocked;
    } else {
        valueEl.innerHTML = '--<span class="unit">Вт</span>';
        detailsEl.textContent = 'нет данных';
        // Без данных измерителя свет гасится только если плата не знает время или у неё не отвечает датчик
        // SHT4x; иначе он работает по расписанию, но без защиты от просадки питания.
        warnEl.textContent = data.led_supply_blocked
            ? 'Свет выключен: нет данных питания, а часы или датчик температуры не в порядке'
            : 'Измеритель питания не отвечает: свет работает по расписанию, защиты от низкого напряжения нет';
        warnEl.hidden = false;
    }
    const energyEl = document.getElementById('energy-val');
    energyEl.textContent = typeof data.energy_kwh === 'number' ? `Сегодня: ${data.energy_kwh.toFixed(2)} кВт·ч` : 'Сегодня: -- кВт·ч';
    energyEl.title = 'С полуночи или с последнего включения платы';
}

// last_water_day — номер дня (unixtime / 86400) по часам платы; null — отметок ещё не было
function formatWaterDay(code, today) {
    if (typeof code !== 'number' || code <= 0) return 'ещё не было';
    const iso = new Date(code * 86400000).toISOString().slice(0, 10);
    if (iso === today) return 'сегодня';
    return `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;
}

async function drawSvgChart() {
    if (!currentSelectedDate) return;
    try {
        // Запрашиваем конкретный файл лога через API
        const res = await fetch(`/api/log?date=${encodeURIComponent(currentSelectedDate)}`);
        const text = await res.text();
        const lines = text.trim().split('\n');

        // Проверяем, есть ли данные кроме заголовка
        if (lines.length <= 1) {
            document.getElementById('svgChart').innerHTML = `<text x="500" y="200" fill="var(--text-muted)" text-anchor="middle" font-size="16">Нет записей лога за выбранный день</text>`;
            chartPoints = [];
            drawPowerChart([]);
            return;
        }

        const dataLines = lines.slice(1);
        const data = [];
        const power = [];
        // Берем каждую точку для отображения суточного хода (так как точек за день много, прореживаем по надобности)
        // Для суточного файла можно отображать каждую 10-ю или 15-ю запись для красивой плавности
        const step = dataLines.length > 300 ? Math.floor(dataLines.length / 100) : 1;

        for (let i = 0; i < dataLines.length; i += step) {
            const cols = dataLines[i].split(';');
            if (cols.length < 3) continue;
            // Извлекаем "HH:MM" из полной метки времени YYYY-MM-DDTHH:MM:SS
            const timePart = cols[0].split('T')[1]?.substring(0, 5) || "--:--";
            // 9-я колонка power_w появилась вместе с INA238: средняя мощность за 10 минут.
            // В старых файлах её нет, пустая — мощность в этот интервал неизвестна.
            const watts = cols.length >= 9 && cols[8].trim() !== '' ? parseFloat(cols[8]) : NaN;
            if (Number.isFinite(watts)) power.push({ time: timePart, watts });
            // Строки, записанные при отказавшем датчике (sht_ok = 0), хранят последнее старое
            // значение — на графике оно выглядело бы как настоящее измерение
            if (cols.length >= 7 && cols[6].trim() === '0') continue;
            const temp = parseFloat(cols[1]), hum = parseFloat(cols[2]);
            if (!Number.isFinite(temp) || !Number.isFinite(hum)) continue;
            data.push({ time: timePart, temp, hum, watts });
        }
        drawPowerChart(power);

        if (data.length === 0) {
            document.getElementById('svgChart').innerHTML = `<text x="500" y="200" fill="var(--text-muted)" text-anchor="middle" font-size="16">Нет записей лога за выбранный день</text>`;
            chartPoints = [];
            return;
        }

        const width = 1000, height = 400, padding = { top: 40, right: 60, bottom: 40, left: 60 };
        const chartWidth = width - padding.left - padding.right, chartHeight = height - padding.top - padding.bottom;

        const temps = data.map(d => d.temp), hums = data.map(d => d.hum);
        const minTemp = Math.floor(Math.min(...temps) - 1), maxTemp = Math.ceil(Math.max(...temps) + 1);
        const minHum = Math.floor(Math.min(...hums) - 5), maxHum = Math.ceil(Math.max(...hums) + 5);

        // Единственная точка (первая запись за день) ставится по центру: деление на
        // (длина − 1) давало NaN, и график не рисовался до появления второй записи
        const getX = (index) => data.length > 1
            ? padding.left + (index / (data.length - 1)) * chartWidth
            : padding.left + chartWidth / 2;
        const getYTemp = (val) => padding.top + chartHeight - ((val - minTemp) / (maxTemp - minTemp)) * chartHeight;
        const fillYHum = (val) => padding.top + chartHeight - ((val - minHum) / (maxHum - minHum)) * chartHeight;

        let tempPath = `M ${getX(0)} ${getYTemp(data[0].temp)}`, humPath = `M ${getX(0)} ${fillYHum(data[0].hum)}`;
        for (let i = 1; i < data.length; i++) {
            tempPath += ` L ${getX(i)} ${getYTemp(data[i].temp)}`;
            humPath += ` L ${getX(i)} ${fillYHum(data[i].hum)}`;
        }

        const svg = document.getElementById('svgChart');
        let svgContent = '';

        // Сетка
        for (let i = 0; i <= 4; i++) {
            const y = padding.top + (chartHeight / 4) * i;
            svgContent += `<line class="grid-line" x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" />`;
            svgContent += `<text class="axis-text" x="${padding.left - 10}" y="${y + 4}" text-anchor="end">${(maxTemp - ((maxTemp - minTemp) / 4) * i).toFixed(1)}°C</text>`;
            svgContent += `<text class="axis-text" x="${width - padding.right + 10}" y="${y + 4}" text-anchor="start">${Math.round(maxHum - ((maxHum - minHum) / 4) * i)}%</text>`;
        }

        // Временные метки по оси X
        const timeStep = Math.max(1, Math.floor(data.length / 6));
        for (let i = 0; i < data.length; i += timeStep) {
            svgContent += `<text class="axis-text" x="${getX(i)}" y="${height - padding.bottom + 20}" text-anchor="middle">${data[i].time}</text>`;
        }

        svgContent += `<path class="line-temp" d="${tempPath}" /><path class="line-hum" d="${humPath}" />`;
        if (data.length === 1) {
            // Линия из одной точки не видна — рисуем сами точки
            svgContent += `<circle cx="${getX(0)}" cy="${getYTemp(data[0].temp)}" r="5" fill="var(--danger)" />`;
            svgContent += `<circle cx="${getX(0)}" cy="${fillYHum(data[0].hum)}" r="5" fill="var(--primary)" />`;
        }

        // Элементы наведения (изначально скрыты через opacity в CSS)
        svgContent += `<line id="hoverLine" class="hover-line" x1="0" y1="${padding.top}" x2="0" y2="${height - padding.bottom}" />`;
        svgContent += `<circle id="hoverDotTemp" class="hover-dot hover-dot-temp" cx="0" cy="0" />`;
        svgContent += `<circle id="hoverDotHum" class="hover-dot hover-dot-hum" cx="0" cy="0" />`;
        // Прозрачный слой поверх графика — ловит движения мыши по всей области
        svgContent += `<rect class="chart-overlay" x="${padding.left}" y="${padding.top}" width="${chartWidth}" height="${chartHeight}" />`;

        svg.innerHTML = svgContent;

        // Сохраняем точки для обработчика наведения мыши
        chartViewBox = { width, height };
        chartPoints = data.map((d, i) => ({
            x: getX(i), yTemp: getYTemp(d.temp), yHum: fillYHum(d.hum),
            time: d.time, temp: d.temp, hum: d.hum, watts: d.watts
        }));
    } catch (e) { console.error("Ошибка построения графиков:", e); }
}

// Отдельный график мощности под основным: свой масштаб в ваттах, скрыт, если данных нет
function drawPowerChart(points) {
    const svg = document.getElementById('powerChart');
    const show = points.length > 0;
    svg.toggleAttribute('hidden', !show); // SVG elements have no .hidden property
    document.getElementById('power-chart-title').hidden = !show;
    if (!show) { svg.innerHTML = ''; return; }

    const width = 1000, height = 200, padding = { top: 20, right: 60, bottom: 30, left: 60 };
    const chartWidth = width - padding.left - padding.right, chartHeight = height - padding.top - padding.bottom;
    const maxWatts = Math.max(10, Math.ceil(Math.max(...points.map(p => p.watts)) / 10) * 10);
    const getX = (index) => points.length > 1
        ? padding.left + (index / (points.length - 1)) * chartWidth
        : padding.left + chartWidth / 2;
    const getY = (val) => padding.top + chartHeight - (Math.max(0, val) / maxWatts) * chartHeight;

    let content = '';
    for (let i = 0; i <= 2; i++) {
        const y = padding.top + (chartHeight / 2) * i;
        content += `<line class="grid-line" x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" />`;
        content += `<text class="axis-text" x="${padding.left - 10}" y="${y + 4}" text-anchor="end">${Math.round(maxWatts - (maxWatts / 2) * i)} Вт</text>`;
    }
    const timeStep = Math.max(1, Math.floor(points.length / 6));
    for (let i = 0; i < points.length; i += timeStep) {
        content += `<text class="axis-text" x="${getX(i)}" y="${height - padding.bottom + 20}" text-anchor="middle">${points[i].time}</text>`;
    }
    let path = `M ${getX(0)} ${getY(points[0].watts)}`;
    for (let i = 1; i < points.length; i++) path += ` L ${getX(i)} ${getY(points[i].watts)}`;
    content += `<path class="line-power" d="${path}" />`;
    if (points.length === 1) content += `<circle cx="${getX(0)}" cy="${getY(points[0].watts)}" r="5" fill="var(--warning)" />`;
    svg.innerHTML = content;
}

function handleChartHover(e) {
    if (chartPoints.length === 0) return;
    const svg = document.getElementById('svgChart');
    const rect = svg.getBoundingClientRect();
    const scaleX = chartViewBox.width / rect.width;

    // На телефоне координата приходит в touches, а не в самом событии
    const clientX = e.touches && e.touches.length ? e.touches[0].clientX : e.clientX;
    const mouseXsvg = (clientX - rect.left) * scaleX;

    // Ищем ближайшую по X точку данных
    let nearest = chartPoints[0], minDist = Infinity;
    for (const p of chartPoints) {
        const dist = Math.abs(p.x - mouseXsvg);
        if (dist < minDist) { minDist = dist; nearest = p; }
    }

    document.getElementById('hoverLine').setAttribute('x1', nearest.x);
    document.getElementById('hoverLine').setAttribute('x2', nearest.x);
    document.getElementById('hoverLine').style.opacity = 1;

    document.getElementById('hoverDotTemp').setAttribute('cx', nearest.x);
    document.getElementById('hoverDotTemp').setAttribute('cy', nearest.yTemp);
    document.getElementById('hoverDotTemp').style.opacity = 1;

    document.getElementById('hoverDotHum').setAttribute('cx', nearest.x);
    document.getElementById('hoverDotHum').setAttribute('cy', nearest.yHum);
    document.getElementById('hoverDotHum').style.opacity = 1;

    const tooltip = document.getElementById('chart-tooltip');
    tooltip.innerHTML = `<div class="tt-time">${nearest.time}</div><div class="tt-temp">🌡 ${nearest.temp.toFixed(1)}°C</div><div class="tt-hum">💧 ${nearest.hum.toFixed(1)}%</div>` +
        (Number.isFinite(nearest.watts) ? `<div class="tt-power">⚡ ${Math.round(nearest.watts)} Вт</div>` : '');
    tooltip.style.left = (nearest.x / chartViewBox.width) * rect.width + 'px';
    tooltip.style.top = ((nearest.yTemp < nearest.yHum ? nearest.yTemp : nearest.yHum) / chartViewBox.height) * rect.height + 'px';
    tooltip.style.opacity = 1;
}

function handleChartLeave() {
    const hoverLine = document.getElementById('hoverLine');
    const hoverDotTemp = document.getElementById('hoverDotTemp');
    const hoverDotHum = document.getElementById('hoverDotHum');
    if (hoverLine) hoverLine.style.opacity = 0;
    if (hoverDotTemp) hoverDotTemp.style.opacity = 0;
    if (hoverDotHum) hoverDotHum.style.opacity = 0;
    document.getElementById('chart-tooltip').style.opacity = 0;
}

document.getElementById('svgChart').addEventListener('mousemove', handleChartHover);
document.getElementById('svgChart').addEventListener('mouseleave', handleChartLeave);
// Палец: подсказка по точке появляется при касании и ведётся при движении по графику
document.getElementById('svgChart').addEventListener('touchstart', handleChartHover, { passive: true });
document.getElementById('svgChart').addEventListener('touchmove', handleChartHover, { passive: true });

// Слушатель ручного выбора даты в календаре
document.getElementById('chart-date-selector').addEventListener('change', (e) => {
    currentSelectedDate = e.target.value;
    userPickedDate = currentSelectedDate !== deviceDate; // Выбрали сегодня — снова следуем за платой
    drawSvgChart();
});

setInterval(updateStatus, 3000);
updateStatus();

// Раз в 5 минут обновляем график, если смотрим сегодняшний день. "Сегодня" — по часам платы:
// сравнение с UTC-датой браузера не срабатывало первые часы суток (в UTC ещё "вчера").
setInterval(() => {
    if (currentSelectedDate && currentSelectedDate === deviceDate) {
        drawSvgChart();
    }
}, 300000);
