let csrfToken = "";
let lastConsoleText = null;
let consoleInFlight = false;
let currentMode = '0';        // Режим, сохранённый на плате: к нему возвращаем список при отмене
let activeTabRestored = false;
const TAB_STORAGE_KEY = 'gi-settings-tab';

// Страница открылась после сохранения (плата вернула ?saved=1 или ?error=…)? Только тогда
// возвращаемся на прежнюю вкладку. При обычном открытии настроек показываем «Климат».
const returnedFromSave = (() => {
    try {
        const params = new URLSearchParams(window.location.search);
        return params.has('saved') || params.has('error') || params.has('time');
    } catch (e) {
        return false;
    }
})();

// sessionStorage может быть недоступен (приватный режим, старый браузер) — тогда просто
// не запоминаем вкладку, а страница работает как раньше
function readStoredTab() {
    try { return typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(TAB_STORAGE_KEY) : null; }
    catch (e) { return null; }
}

function storeTab(tabId) {
    try { if (typeof sessionStorage !== 'undefined') sessionStorage.setItem(TAB_STORAGE_KEY, tabId); }
    catch (e) { /* не критично */ }
}
const wateringDayIds = ["wd_mon", "wd_tue", "wd_wed", "wd_thu", "wd_fri", "wd_sat", "wd_sun"];

async function updateConsole() {
    const panel = document.getElementById('tab-console');
    if (consoleInFlight || document.hidden || !panel.classList.contains('active')) return;
    consoleInFlight = true;
    try {
        const res = await fetch('/api/console');
        if (!res.ok) return;
        const text = await res.text();
        // Сравниваем сам текст: у заполненного кольцевого буфера длина почти не меняется,
        // и сравнение по длине пропускало обновления
        if (text === lastConsoleText) return;
        lastConsoleText = text;

        const el = document.getElementById('serial-console');
        const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        el.textContent = text.length ? text : '(лог пуст)';

        if (document.getElementById('console-autoscroll').checked && wasAtBottom) {
            el.scrollTop = el.scrollHeight;
        }
    } catch (e) {
        // Тихо игнорируем временные ошибки связи с платой.
    } finally {
        consoleInFlight = false;
    }
}

function packWateringDays() {
    let mask = 0;
    wateringDayIds.forEach((id, i) => {
        if (document.getElementById(id).checked) mask |= (1 << i);
    });
    document.getElementById('watering_days').value = mask;
}

function unpackWateringDays(mask) {
    wateringDayIds.forEach((id, i) => {
        document.getElementById(id).checked = !!(mask & (1 << i));
    });
}

// Переносит состояние чекбокса "учитывать датчик воды" в скрытое поле перед отправкой формы —
// та же схема, что и у дней недели полива
function packWaterSensor() {
    document.getElementById('water_sensor_enabled').value = document.getElementById('water_sensor_checkbox').checked ? '1' : '0';
}

// "6" / "0" -> "06:00", для подстановки в <input type="time">
function formatTime(hour, minute) {
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

// Перекладывает выбранное время из <input type="time"> в скрытые поля часов/минут перед отправкой формы
function packLedTimes() {
    const [onHour, onMinute] = document.getElementById('led_on_time').value.split(':');
    const [offHour, offMinute] = document.getElementById('led_off_time').value.split(':');
    document.getElementById('led_on_hour').value = onHour;
    document.getElementById('led_on_minute').value = onMinute;
    document.getElementById('led_off_hour').value = offHour;
    document.getElementById('led_off_minute').value = offMinute;
}

// То же для времени полива: одно поле выбора времени вместо двух числовых (час и минута)
function packWateringTime() {
    const [hour, minute] = document.getElementById('watering_time').value.split(':');
    document.getElementById('watering_hour').value = hour;
    document.getElementById('watering_minute').value = minute;
}

// Плата больше не отдаёт пароли и токен наружу — в поле показываем только факт,
// что значение задано. Пустое поле при сохранении означает "оставить как есть".
function setSecretPlaceholder(id, isSet) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? '•••••••• сохранено, пусто = не менять' : 'не задано';
}

// Галочка "сеть без пароля" — единственный способ стереть сохранённый пароль,
// раз пустое поле теперь означает "не менять"
function attachClearPassToggles() {
    [['wifi_pass_clear', 'wifi_pass'], ['telemetry_token_clear', 'telemetry_token']].forEach(([cbId, inputId]) => {
        const cb = document.getElementById(cbId);
        const input = document.getElementById(inputId);
        if (!cb || !input) return;
        cb.addEventListener('change', () => {
            input.disabled = cb.checked;
            if (cb.checked) input.value = '';
        });
    });
}

function togglePasswordField(id) {
    const el = document.getElementById(id);
    el.type = (el.type === 'password') ? 'text' : 'password';
}

// Показываем баннер ошибки, если сервер отклонил сохранение (?error=ap,wifi в адресе после редиректа)
function showServerErrors() {
    const params = new URLSearchParams(window.location.search);
    const errors = (params.get('error') || '').split(',').filter(Boolean);
    if (errors.includes('ap')) {
        document.getElementById('ap-error-banner').style.display = 'block';
        document.querySelector('[data-tab="tab-network"]').click();
    }
    if (errors.includes('wifi')) {
        document.getElementById('wifi-error-banner').style.display = 'block';
        document.querySelector('[data-tab="tab-network"]').click();
    }
    if (errors.includes('telemetry')) {
        document.getElementById('telemetry-error-banner').style.display = 'block';
        document.querySelector('[data-tab="tab-network"]').click();
    }
    if (errors.includes('time')) {
        document.getElementById('time-error-banner').style.display = 'block';
        document.querySelector('[data-tab="tab-general"]').click();
    }
    if (errors.includes('date')) {
        document.getElementById('date-error-banner').style.display = 'block';
        document.querySelector('[data-tab="tab-general"]').click();
    }
    // ?time=1 — форма часов отправлена; запись и проверку делает плата, итог берём из /api/status
    const timeSent = params.get('time');
    if (timeSent) {
        document.querySelector('[data-tab="tab-general"]').click();
        watchTimeSet();
    }
    // ?saved=1 — плата приняла настройки. Раньше после сохранения не было никакого подтверждения.
    const saved = params.get('saved');
    if (saved) {
        const banner = document.getElementById('save-banner');
        banner.hidden = false;
        setTimeout(() => { banner.hidden = true; }, 4000);
    }
    if (errors.length || saved || timeSent) {
        // Убираем параметры из адресной строки, чтобы баннер не всплывал повторно при обновлении страницы
        window.history.replaceState({}, '', window.location.pathname);
    }
}

// Показывает текстовое сообщение в баннере ошибки конкретной формы и не даёт её отправить
function showFieldError(bannerId, message) {
    const banner = document.getElementById(bannerId);
    banner.textContent = message;
    banner.style.display = 'block';
}

function clearFieldError(bannerId) {
    const banner = document.getElementById(bannerId);
    banner.style.display = 'none';
    banner.textContent = '';
}

// Проверка, что значение поля "мин" не превышает значение поля "макс"; при ошибке — блокирует отправку формы
function attachMinMaxValidation(formId, minId, maxId, bannerId, label) {
    const form = document.getElementById(formId);
    if (!form) return;
    form.addEventListener('submit', (e) => {
        const minVal = parseFloat(document.getElementById(minId).value);
        const maxVal = parseFloat(document.getElementById(maxId).value);
        if (isNaN(minVal) || isNaN(maxVal) || minVal > maxVal) {
            e.preventDefault();
            showFieldError(bannerId, `${label}: минимальное значение не может быть больше максимального.`);
        } else {
            clearFieldError(bannerId);
        }
    });
}

// WiFi роутера: та же логика валидации, что и на сервере (SSID до 32 символов, пароль пусто или 8-63 символа)
function attachWifiValidation() {
    const form = document.getElementById('wifi-form');
    if (!form) return;
    form.addEventListener('submit', (e) => {
        const ssid = document.getElementById('wifi_ssid').value.trim();
        const pass = document.getElementById('wifi_pass').value;
        const ssidOk = ssid.length <= 32;
        const passOk = pass.length === 0 || (pass.length >= 8 && pass.length <= 63);
        if (!ssidOk || !passOk) {
            e.preventDefault();
            showFieldError('wifi-error-banner', 'Имя сети — до 32 символов, пароль — пусто (открытая сеть) или от 8 до 63 символов.');
        } else {
            clearFieldError('wifi-error-banner');
        }
    });
}

// Местное время этого устройства в формате поля datetime-local: 16 знаков — до минут, 19 — с секундами
function localIsoTime(length) {
    const now = new Date();
    const offset = now.getTimezoneOffset() * 60000;
    return (new Date(now - offset)).toISOString().slice(0, length);
}

function setCurrentTime() {
    document.getElementById('datetime').value = localIsoTime(16);
}

// Берёт время в момент нажатия и сразу отправляет. Раньше в плату уходило значение поля,
// заполненное при открытии страницы: провисела страница 5 минут — часы на 5 минут назад.
// Раньше страница после установки часов всегда писала "Настройки сохранены", даже если
// MCP7940N время не принял. Теперь ждём итог от платы (до ~20 с: запуск кварца бывает долгим).
const TIME_RESULT_TEXT = {
    pending: ['form-hint', 'Записываем время в часы платы…'],
    ok: ['form-ok', 'Время записано в часы и проверено.'],
    failed: ['form-error', 'Часы не приняли время. Повторите установку; если не поможет — причина в консоли (вкладка «Консоль»).'],
};
async function watchTimeSet(attempt = 0) {
    const banner = document.getElementById('time-result-banner');
    let result = 'pending';
    try {
        const res = await fetch('/api/status');
        if (res.ok) result = (await res.json()).time_set || 'pending';
    } catch (e) { /* Повторим ниже */ }
    if (result === 'pending' && attempt >= 20) result = 'failed';
    const [cls, text] = TIME_RESULT_TEXT[result] || TIME_RESULT_TEXT.pending;
    banner.className = cls;
    banner.textContent = text;
    banner.hidden = result === 'none';
    if (result === 'pending') setTimeout(() => watchTimeSet(attempt + 1), 1000);
    else if (result === 'ok') loadCurrentSettings(); // Обновить "Текущие дата и время на плате"
}

function syncTimeFromBrowser() {
    document.getElementById('datetime').value = localIsoTime(19);
    document.getElementById('time-form').requestSubmit();
}

async function loadCurrentSettings() {
    try {
        const res = await fetch('/api/settings');
        if (!res.ok) throw new Error("Ошибка ответа сервера");

        const data = await res.json();
        csrfToken = data.csrf_token;
        document.querySelectorAll('form[method="POST"]').forEach(form => {
            let input = form.querySelector('input[name="csrf_token"]');
            if (!input) {
                input = document.createElement('input');
                input.type = 'hidden'; input.name = 'csrf_token'; form.appendChild(input);
            }
            input.value = csrfToken;
        });



        document.getElementById('temp_target').value = data.temp_target;
        document.getElementById('temp_delta').value = data.temp_delta;
        document.getElementById('led_on_time').value = formatTime(data.led_on_hour, data.led_on_minute);
        document.getElementById('led_off_time').value = formatTime(data.led_off_hour, data.led_off_minute);
        document.getElementById('fan1_min_limit').value = data.fan1_min_limit;
        document.getElementById('fan1_max_limit').value = data.fan1_max_limit;
        document.getElementById('fan2_min_limit').value = data.fan2_min_limit;
        document.getElementById('fan2_max_limit').value = data.fan2_max_limit;
        document.getElementById('led_min_limit').value = data.led_min_limit;
        document.getElementById('led_max_limit').value = data.led_max_limit;
        document.getElementById('rtc-time-label').innerText = data.rtc_time;

        document.getElementById('watering_time').value = formatTime(data.watering_hour, data.watering_minute);
        document.getElementById('watering_duration').value = data.watering_duration;
        unpackWateringDays(data.watering_days);
        document.getElementById('water_sensor_checkbox').checked = !!data.water_sensor_enabled;
        document.getElementById('heater_mode').value = data.heater_mode;
        document.getElementById('temp_target_night').value = data.temp_target_night;
        document.getElementById('drying_temp_target').value = data.drying_temp_target;
        document.getElementById('drying_temp_delta').value = data.drying_temp_delta;
        document.getElementById('drying_hum_target').value = data.drying_hum_target;
        document.getElementById('drying_hum_band').value = data.drying_hum_band;
        document.getElementById('power_cal_pct').value = data.power_cal_pct;
        applySettingsMode(data.operation_mode);
        if (!activeTabRestored) {
            // После сохранения возвращаемся на вкладку, с которой сохраняли
            activeTabRestored = true;
            restoreActiveTab();
        }

        document.getElementById('wifi_ssid').value = data.wifi_ssid;
        document.getElementById('ap_ssid').value = data.ap_ssid;
        document.getElementById('telemetry_url').value = data.telemetry_url;
        document.getElementById('device_label').value = data.device_label;
        setSecretPlaceholder('wifi_pass', data.wifi_pass_set);
        setSecretPlaceholder('telemetry_token', data.telemetry_token_set);
        setSecretPlaceholder('ap_pass', data.ap_pass_set);

        // Плата хранит полночь выбранной даты без часового пояса, поэтому и разбираем её как UTC.
        // С местными getDate()/getMonth() в поясах западнее UTC дата съезжала на день назад.
        if (data.start_time > 0) {
            const d = new Date(data.start_time * 1000);
            const yyyy = d.getUTCFullYear();
            const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
            const dd = String(d.getUTCDate()).padStart(2, '0');
            document.getElementById('start_date').value = `${yyyy}-${mm}-${dd}`;
        } else {
            document.getElementById('start_date').value = '';
        }
        const today = new Date();
        document.getElementById('start_date').max =
            `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

        if (data.sdTotal !== undefined) {
            const usedPct = data.sdTotal > 0 ? Math.round((data.sdUsed / data.sdTotal) * 100) : 0;
            document.getElementById('sd-status').innerText = `${formatBytes(data.sdUsed)} / ${formatBytes(data.sdTotal)} (${usedPct}%)`;
            document.getElementById('sd-free').innerText = formatBytes(data.sdFree);
            document.getElementById('sd-bar-fill').style.width = Math.min(100, usedPct) + '%';
        }
    } catch (e) {
        console.error("Ошибка загрузки настроек:", e);
        document.getElementById('rtc-time-label').innerText = "Ошибка связи";
    }
}

function applySettingsMode(mode) {
    const drying = Number(mode) === 1;
    const modeName = drying ? 'drying' : 'growing';
    currentMode = drying ? '1' : '0';
    const modeSelect = document.getElementById('operation_mode');
    if (modeSelect) modeSelect.value = currentMode;

    document.querySelectorAll('[data-modes]').forEach(element => {
        if (!element.dataset || !element.dataset.modes) return;
        element.hidden = !element.dataset.modes.split(',').includes(modeName);
    });
    document.querySelectorAll('[data-mode-only]').forEach(element => {
        if (!element.dataset || !element.dataset.modeOnly) return;
        element.hidden = element.dataset.modeOnly !== modeName;
    });
}

// Смена режима применяется на плате сразу и меняет поведение всего бокса, поэтому
// спрашиваем подтверждение: список легко переключить случайно колесом мыши или свайпом.
function initOperationModeSelector() {
    const modeSelect = document.getElementById('operation_mode');
    const modeForm = document.getElementById('operation-mode-form');
    if (!modeSelect || !modeForm) return;
    modeSelect.addEventListener('change', () => {
        const question = modeSelect.value === '1'
            ? 'Переключить в режим сушки? Свет и автополив выключатся сразу.'
            : 'Переключить в режим выращивания? Свет и автополив снова пойдут по расписанию.';
        if (!confirm(question)) {
            modeSelect.value = currentMode;
            return;
        }
        modeForm.requestSubmit();
    });
}

// После сохранения открывает вкладку, с которой отправили форму. Если её нет в текущем
// режиме (например, «Полив» в сушке), остаётся вкладка по умолчанию — «Климат».
function restoreActiveTab() {
    if (!returnedFromSave) return;
    const tabId = readStoredTab();
    if (!tabId || typeof document.querySelector !== 'function') return;
    const button = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (button && !button.hidden) button.click();
}

// ---------- Обслуживание SD-карты ----------
function formatBytes(n) {
    if (n < 1024) return n + " Б";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " КБ";
    return (n / 1024 / 1024).toFixed(2) + " МБ";
}

function loadSdFiles() {
    const box = document.getElementById('sd-files-list');
    if (!box) return;
    box.innerText = "Запрос...";

    fetch('/api/files')
        .then(res => res.json())
        .then(data => {
            if (!data.files || data.files.length === 0) {
                box.innerHTML = '<div class="sd-empty">На карте нет файлов в корне.</div>';
                return;
            }
            let html = data.files.map(f =>
                `<div class="sd-file-row"><span class="sd-file-name">${f.name.replace(/</g, '&lt;')}</span>`
                + `<span class="sd-file-size">${formatBytes(f.size)}</span></div>`
            ).join('');
            if (data.total > data.files.length) {
                html += `<div class="sd-empty">…показаны последние ${data.files.length} из ${data.total}</div>`;
            }
            box.innerHTML = html;
        })
        .catch(err => {
            box.innerHTML = '<div class="sd-empty">Ошибка запроса /api/files</div>';
            console.error("Ошибка загрузки списка файлов:", err);
        });
}

function clearSDLogs() {
    if (!confirm("Уничтожить весь лог истории климата на SD-карте? Действие необратимо.")) return;
    if (!csrfToken) { alert("Сначала дождитесь загрузки настроек."); return; }
    fetch('/api/clearlogs', { method: 'POST', headers: { 'X-CSRF-Token': csrfToken } }).then(res => {
        if (res.ok) {
            alert("Удалено");
            loadCurrentSettings(); // Перечитываем занятость SD и обновляем прогресс-бар
            loadSdFiles();
        } else {
            alert("Удаление не выполнено. Обновите страницу и повторите.");
        }
    }).catch(() => alert("Ошибка связи при удалении."));
}

function initTabs() {
    const buttons = document.querySelectorAll('.tab-btn');
    const panels = document.querySelectorAll('.tab-panel');

    buttons.forEach(btn => {
        btn.addEventListener('click', () => {
            buttons.forEach(b => b.classList.remove('active'));
            panels.forEach(p => p.classList.remove('active'));

            btn.classList.add('active');
            document.getElementById(btn.dataset.tab).classList.add('active');
            storeTab(btn.dataset.tab);
            if (btn.dataset.tab === 'tab-console') updateConsole();
        });
    });
}

window.onload = () => {
    document.querySelectorAll('form').forEach(form => form.addEventListener('submit', e => {
        if (!csrfToken) { e.preventDefault(); alert("Сначала дождитесь загрузки настроек."); }
    }));
    setCurrentTime();
    loadCurrentSettings();
    loadSdFiles();
    document.getElementById('watering-form').addEventListener('submit', packWateringDays);
    document.getElementById('watering-form').addEventListener('submit', packWaterSensor);
    document.getElementById('watering-form').addEventListener('submit', packWateringTime);
    document.getElementById('light-form').addEventListener('submit', packLedTimes);
    initOperationModeSelector();
    initTabs();
    setInterval(updateConsole, 3000);
    showServerErrors();

    attachMinMaxValidation('light-form', 'led_min_limit', 'led_max_limit', 'light-error-banner', 'Мощность лампы (мин/макс)');
    attachWifiValidation();
    attachClearPassToggles();

    // Форма "Лимиты вентиляторов" содержит сразу 2 пары мин/макс (вентилятор 1 и 2) — проверяем обе одним обработчиком
    document.getElementById('fans-limits-form').addEventListener('submit', (e) => {
        const fan1Min = parseFloat(document.getElementById('fan1_min_limit').value);
        const fan1Max = parseFloat(document.getElementById('fan1_max_limit').value);
        const fan2Min = parseFloat(document.getElementById('fan2_min_limit').value);
        const fan2Max = parseFloat(document.getElementById('fan2_max_limit').value);
        if (!isNaN(fan1Min) && !isNaN(fan1Max) && fan1Min > fan1Max) {
            e.preventDefault();
            showFieldError('fans-limits-error-banner', 'Вентилятор 1 (мин/макс): минимальное значение не может быть больше максимального.');
            return;
        }
        if (!isNaN(fan2Min) && !isNaN(fan2Max) && fan2Min > fan2Max) {
            e.preventDefault();
            showFieldError('fans-limits-error-banner', 'Вентилятор 2 (мин/макс): минимальное значение не может быть больше максимального.');
            return;
        }
        clearFieldError('fans-limits-error-banner');
    });
};
