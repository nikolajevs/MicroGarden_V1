const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function page(statusPayload) {
  const fields = new Map();
  let modeChangeHandler;
  let modeFormSubmits = 0;
  const modeSelect = {
    value: '',
    addEventListener(type, handler) { if (type === 'change') modeChangeHandler = handler; }
  };
  const modeForm = {requestSubmit() { modeFormSubmits++; }};
  fields.set('operation_mode', modeSelect);
  const forms = Array.from({length: 8}, () => ({
    inputs: [], querySelector() { return this.inputs[0]; },
    appendChild(input) { this.inputs.push(input); }
  }));
  const modeElements = [
    {dataset: {modes: 'growing,drying'}, hidden: false},
    {dataset: {modes: 'growing'}, hidden: false},
    {dataset: {modeOnly: 'growing'}, hidden: false},
    {dataset: {modeOnly: 'drying'}, hidden: false}
  ];
  const calls = [], alerts = [];
  const context = vm.createContext({
    console, URLSearchParams, Date,
    window: {},
    document: {
      getElementById(id) {
        if (id === 'operation-mode-form') return modeForm;
        if (!fields.has(id)) fields.set(id, {value: 'previous', style: {}});
        return fields.get(id);
      },
      querySelectorAll(selector) {
        if (selector === '[data-modes]') return modeElements.slice(0, 2);
        if (selector === '[data-mode-only]') return modeElements.slice(2);
        return forms;
      },
      createElement() { return {}; }
    },
    confirm: () => true,
    alert: text => alerts.push(text),
    fetch: async (url, options) => {
      calls.push({url, options});
      if (url === '/api/status') {
        if (statusPayload === 'down') throw new Error('плата не отвечает');
        if (statusPayload) return {ok: true, json: async () => statusPayload};
      }
      return {ok: true, json: async () => ({csrf_token: 'random-per-boot-token',
        operation_mode: 0, drying_temp_target: 16, drying_temp_delta: 0.5,
        drying_hum_target: 55, drying_hum_band: 5, hum_target_night: 55, hum_band_night: 10,
        rgb_brightness: 19, rgb_night_mode: 1,
        wifi_ssid: 'Box"\\Lab', ap_ssid: 'Private AP',
        telemetry_url: 'https://dash.example.lat/api/ingest', device_label: 'box-1',
        led_on_hour: 6, led_on_minute: 0, led_off_hour: 18, led_off_minute: 0,
        watering_days: 0, start_time: 0})};
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../firmware/data/settings.js'), 'utf8'), context);
  return {context, fields, forms, modeElements, calls, alerts,
    changeOperationMode() { modeChangeHandler(); },
    get modeFormSubmits() { return modeFormSubmits; }};
}

test('settings keep secrets empty, preserve special characters and attach CSRF to every form', async () => {
  const p = page();
  await vm.runInContext('loadCurrentSettings()', p.context);
  assert.equal(p.fields.get('wifi_ssid').value, 'Box"\\Lab');
  assert.equal(p.fields.get('rgb_brightness').value, 19);
  assert.equal(p.fields.get('rgb_night_mode').value, 1);
  assert.equal(p.fields.get('operation_mode').value, '0');
  assert.equal(p.fields.get('drying_temp_target').value, 16);
  assert.equal(p.fields.get('drying_hum_target').value, 55);
  assert.equal(p.fields.get('hum_target_night').value, 55);
  assert.equal(p.fields.get('hum_band_night').value, 10);
  assert.equal(p.fields.get('telemetry_url').value, 'https://dash.example.lat/api/ingest');
  assert.equal(p.fields.get('device_label').value, 'box-1');
  for (const name of ['wifi_pass', 'ap_pass', 'telemetry_token']) assert.equal(p.fields.get(name).value, '');
  for (const form of p.forms) {
    assert.equal(form.inputs[0].name, 'csrf_token');
    assert.equal(form.inputs[0].value, 'random-per-boot-token');
    assert.equal(form.inputs[0].type, 'hidden');
  }
  await vm.runInContext('loadCurrentSettings()', p.context);
  for (const form of p.forms) assert.equal(form.inputs.length, 1);
});

test('clear logs refuses to run before token is loaded', () => {
  const p = page();
  vm.runInContext('clearSDLogs()', p.context);
  assert.equal(p.calls.length, 0);
  assert.equal(p.alerts.length, 1);
});

test('clear logs uses authenticated same-origin POST with CSRF', async () => {
  const p = page();
  await vm.runInContext('loadCurrentSettings()', p.context);
  vm.runInContext('clearSDLogs()', p.context);
  const request = p.calls.find(c => c.url === '/api/clearlogs');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['X-CSRF-Token'], 'random-per-boot-token');
});

test('Drying settings show the drying profile and hide Growing-only controls', () => {
  const p = page();
  vm.runInContext('applySettingsMode(1)', p.context);
  assert.equal(p.fields.get('operation_mode').value, '1');
  assert.equal(p.modeElements[0].hidden, false);
  assert.equal(p.modeElements[1].hidden, true);
  assert.equal(p.modeElements[2].hidden, true);
  assert.equal(p.modeElements[3].hidden, false);
});

test('changing operation mode asks for confirmation and then submits', () => {
  const p = page();
  const questions = [];
  p.context.confirm = text => { questions.push(text); return true; };
  vm.runInContext('applySettingsMode(0); initOperationModeSelector()', p.context);
  p.fields.get('operation_mode').value = '1';
  p.changeOperationMode();
  assert.equal(p.modeFormSubmits, 1);
  assert.equal(questions.length, 1);
  assert.match(questions[0], /сушк/);
});

test('cancelling the mode change restores the saved mode and does not submit', () => {
  const p = page();
  p.context.confirm = () => false;
  vm.runInContext('applySettingsMode(0); initOperationModeSelector()', p.context);
  p.fields.get('operation_mode').value = '1';
  p.changeOperationMode();
  assert.equal(p.modeFormSubmits, 0);
  assert.equal(p.fields.get('operation_mode').value, '0');
});

test('console refreshes when the text changes even if its length stays the same', async () => {
  const p = page();
  let body = 'aaaa';
  p.fields.set('tab-console', {classList: {contains: () => true}});
  p.fields.set('serial-console', {scrollHeight: 100, scrollTop: 100, clientHeight: 100});
  p.fields.set('console-autoscroll', {checked: true});
  p.context.fetch = async () => ({ok: true, text: async () => body});

  await vm.runInContext('updateConsole()', p.context);
  body = 'bbbb';
  await vm.runInContext('updateConsole()', p.context);
  assert.equal(p.fields.get('serial-console').textContent, 'bbbb');
});

test('start date is shown as stored on the board, independent of the browser time zone', async () => {
  const p = page();
  const original = p.context.fetch;
  p.context.fetch = async (url, options) => {
    const res = await original(url, options);
    const data = await res.json();
    // 2026-10-01 00:00:00 — полночь даты, как её хранит плата
    return {ok: true, json: async () => ({...data, start_time: 1790812800})};
  };
  await vm.runInContext('loadCurrentSettings()', p.context);
  assert.equal(p.fields.get('start_date').value, '2026-10-01');
});

test('watering time is one time field that still submits hour and minute', async () => {
  const p = page();
  const original = p.context.fetch;
  p.context.fetch = async (url, options) => {
    const data = await (await original(url, options)).json();
    return {ok: true, json: async () => ({...data, watering_hour: 7, watering_minute: 5})};
  };
  await vm.runInContext('loadCurrentSettings()', p.context);
  assert.equal(p.fields.get('watering_time').value, '07:05');

  p.fields.get('watering_time').value = '21:30';
  vm.runInContext('packWateringTime()', p.context);
  assert.equal(p.fields.get('watering_hour').value, '21');
  assert.equal(p.fields.get('watering_minute').value, '30');
});

test('serial console polls only while its settings tab is active', async () => {
  const p = page();
  let active = false;
  let requests = 0;
  p.fields.set('tab-console', {classList: {contains: () => active}});
  p.fields.set('serial-console', {scrollHeight: 100, scrollTop: 100, clientHeight: 100});
  p.fields.set('console-autoscroll', {checked: true});
  p.context.fetch = async () => {
    requests++;
    return {ok: true, text: async () => 'board log'};
  };

  await vm.runInContext('updateConsole()', p.context);
  assert.equal(requests, 0);

  active = true;
  await vm.runInContext('updateConsole()', p.context);
  assert.equal(requests, 1);
  assert.equal(p.fields.get('serial-console').textContent, 'board log');
});

test('RGB legend lists exactly the status colours of the firmware', () => {
  const html = fs.readFileSync(path.join(__dirname, '../firmware/data/settings.html'), 'utf8');
  const header = fs.readFileSync(path.join(__dirname, '../firmware/LiteControl.h'), 'utf8');
  const body = header.match(/enum class StatusColor[^{]*\{([^}]*)\}/)[1];
  const firmwareColours = body.split(',').map(s => s.trim().toLowerCase()).filter(Boolean).sort();
  const legendColours = [...html.matchAll(/class="rgb-dot rgb-(\w+)"/g)].map(m => m[1]).sort();
  assert.deepEqual(legendColours, firmwareColours,
    'a colour added to or removed from StatusColor must be added to or removed from the legend');
});

test('every settings form field is a firmware setting and every setting has a form field', () => {
  const html = fs.readFileSync(path.join(__dirname, '../firmware/data/settings.html'), 'utf8');
  const sketch = fs.readFileSync(path.join(__dirname, '../firmware/firmware.ino'), 'utf8');
  // Поля вне таблицы SETTING_DEFS: время и дата разбираются отдельно, *_clear стирают пароль/токен.
  const special = new Set(['viewport', 'datetime', 'start_date']);
  const formNames = new Set([...html.matchAll(/name="([a-z0-9_]+)"/g)].map(m => m[1])
    .filter(name => !special.has(name) && !name.endsWith('_clear')));
  const firmware = [...sketch.matchAll(/^\s*SET_([A-Z0-9_]+)\s*\("([a-z0-9_]+)"/gm)];
  const formParams = new Set(firmware.filter(m => !m[1].endsWith('_NOFORM')).map(m => m[2]));
  assert.ok(formParams.has('hum_target_night') && formParams.has('hum_band_night'));
  assert.deepEqual([...formNames].sort(), [...formParams].sort(),
    'a field on the settings page without a SETTING_DEFS row is silently ignored by the board, and the reverse is unreachable');
});

test('temperature targets on the page and in the firmware stop at the heater hard limit', () => {
  const html = fs.readFileSync(path.join(__dirname, '../firmware/data/settings.html'), 'utf8');
  const sketch = fs.readFileSync(path.join(__dirname, '../firmware/firmware.ino'), 'utf8');
  const board = fs.readFileSync(path.join(__dirname, '../firmware/Board.h'), 'utf8');
  const limit = Number(board.match(/HEATER_HARD_LIMIT_C\s*=\s*([0-9.]+)f/)[1]);
  assert.equal(limit, 40);
  for (const id of ['temp_target', 'temp_target_night', 'drying_temp_target']) {
    const input = html.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`))[0];
    assert.equal(Number(input.match(/max="([0-9.]+)"/)[1]), limit, `${id}: page maximum`);
    const row = sketch.match(new RegExp(`SET_FLOAT\\("${id}",[^\\n]*`))[0];
    assert.match(row, /,\s*HEATER_HARD_LIMIT_C,/, `${id}: firmware maximum`);
  }
});

test('climate defaults are the values the owner chose', () => {
  const sketch = fs.readFileSync(path.join(__dirname, '../firmware/firmware.ino'), 'utf8');
  const defaultOf = id => Number(sketch.match(new RegExp(`SET_FLOAT\\("${id}",[^\\n]*,\\s*([0-9.]+)\\)`))[1]);
  // Решения владельца от 28.10.2026; на платах с уже сохранённым значением остаётся сохранённое.
  assert.deepEqual({
    temp_target: defaultOf('temp_target'), temp_target_night: defaultOf('temp_target_night'),
    temp_delta: defaultOf('temp_delta'), hum_target_night: defaultOf('hum_target_night'),
    hum_band_night: defaultOf('hum_band_night'), drying_temp_target: defaultOf('drying_temp_target'),
    drying_temp_delta: defaultOf('drying_temp_delta')
  }, {temp_target: 25, temp_target_night: 17, temp_delta: 2, hum_target_night: 55, hum_band_night: 10,
      drying_temp_target: 16, drying_temp_delta: 0.5});
});

// ----- WiFi роутера: состояние и IP-адрес платы -----------------------------------------------------
function wifiView(p, data) {
  return JSON.parse(vm.runInContext('JSON.stringify(wifiStatusView(' + JSON.stringify(data) + '))', p.context));
}

test('WiFi роутера: подключено — показываем IP-адрес и уровень сигнала', () => {
  const p = page();
  const view = wifiView(p, {wifi_state: 'connected', wifi_ip: '192.168.1.57', wifi_rssi: -58, ap_ip: '192.168.4.1'});
  assert.equal(view.level, 'ok');
  assert.equal(view.ip, '192.168.1.57');
  assert.match(view.extra, /−58 дБм \(отличный\)/);
});

test('WiFi роутера: сигнал называется по уровню', () => {
  const p = page();
  const label = rssi => wifiView(p, {wifi_state: 'connected', wifi_ip: '10.0.0.2', wifi_rssi: rssi}).extra;
  assert.match(label(-60), /отличный/);
  assert.match(label(-67), /хороший/);
  assert.match(label(-75), /слабый/);
  assert.match(label(-90), /плохой/);
  assert.equal(wifiView(p, {wifi_state: 'connected', wifi_ip: '10.0.0.2', wifi_rssi: null}).extra, '');
});

test('WiFi роутера: остальные состояния адреса не показывают', () => {
  const p = page();
  assert.deepEqual(
    ['connecting', 'disconnected', 'not_configured'].map(s => wifiView(p, {wifi_state: s, wifi_ip: '192.168.1.57'}).ip),
    [null, null, null]);
  assert.equal(wifiView(p, {wifi_state: 'connecting'}).level, 'wait');
  assert.equal(wifiView(p, {wifi_state: 'disconnected'}).level, 'bad');
  assert.equal(wifiView(p, {wifi_state: 'disconnected'}).text, 'Нет подключения к роутеру');
  assert.match(wifiView(p, {wifi_state: 'disconnected'}).extra, /раз в 2 минуты/);
  assert.equal(wifiView(p, {wifi_state: 'not_configured'}).level, 'off');
  // старая прошивка без этих полей и пустой ответ
  assert.match(wifiView(p, {}).text, /неизвестно/);
  assert.match(wifiView(p, null).text, /неизвестно/);
});

test('WiFi роутера: адрес, не похожий на IPv4, в ссылку не попадает', () => {
  const p = page();
  for (const bad of ['javascript:alert(1)', '<b>x</b>', '192.168.1', '1.2.3.4/evil', '']) {
    assert.equal(wifiView(p, {wifi_state: 'connected', wifi_ip: bad, wifi_rssi: -50}).ip, null, bad);
  }
});

// Вкладка «WiFi» открыта (active) или нет: только на открытой вкладке страница опрашивает плату
function wifiTab(p, active = true) {
  p.fields.set('tab-network', {classList: {contains: name => name === 'active' && active}});
}

test('WiFi роутера: плату опрашивают, только пока открыта вкладка «WiFi»', async () => {
  const p = page({wifi_state: 'connected', wifi_ip: '192.168.1.57', wifi_rssi: -50, ap_ip: '192.168.4.1'});
  wifiTab(p, false);
  await vm.runInContext('updateWifiStatus()', p.context);
  assert.equal(p.calls.length, 0, 'вкладка закрыта — запроса нет');
  wifiTab(p, true);
  await vm.runInContext('updateWifiStatus()', p.context);
  assert.deepEqual(p.calls.map(c => c.url), ['/api/status']);
});

test('WiFi роутера: пока запрос не вернулся, второй не отправляется', async () => {
  const p = page({wifi_state: 'connecting'});
  wifiTab(p);
  const first = vm.runInContext('updateWifiStatus()', p.context);
  const second = vm.runInContext('updateWifiStatus()', p.context);
  await Promise.all([first, second]);
  assert.equal(p.calls.length, 1);
  await vm.runInContext('updateWifiStatus()', p.context);
  assert.equal(p.calls.length, 2, 'после ответа опрос снова возможен');
});

test('WiFi роутера: страница обновляет текст, адрес и ссылку', async () => {
  const p = page({wifi_state: 'connected', wifi_ip: '192.168.1.57', wifi_rssi: -71, ap_ip: '192.168.4.1'});
  wifiTab(p);
  await vm.runInContext('updateWifiStatus()', p.context);
  assert.equal(p.calls.at(-1).url, '/api/status');
  assert.equal(p.fields.get('wifi-state-text').textContent, 'Подключено к роутеру');
  assert.equal(p.fields.get('wifi-ip').textContent, '192.168.1.57');
  assert.equal(p.fields.get('wifi-open').href, 'http://192.168.1.57/');
  assert.equal(p.fields.get('wifi-ip-line').hidden, false);
  assert.equal(p.fields.get('wifi-dot').className, 'wifi-dot ok');
  assert.match(p.fields.get('wifi-extra').textContent, /слабый/);
  assert.equal(p.fields.get('ap-ip').textContent, '192.168.4.1');
});

test('WiFi роутера: пока плата подключается, адреса нет; когда подключилась, он появляется', async () => {
  const p = page({wifi_state: 'connecting'});
  wifiTab(p);
  await vm.runInContext('updateWifiStatus()', p.context);
  assert.equal(p.fields.get('wifi-ip-line').hidden, true);
  assert.equal(p.fields.get('wifi-state-text').textContent, 'Подключаюсь к роутеру…');
  assert.equal(p.fields.get('wifi-dot').className, 'wifi-dot wait');
});

test('WiFi роутера: плата не отвечает — страница не падает и пишет «неизвестно»', async () => {
  const p = page('down');
  wifiTab(p);
  await vm.runInContext('updateWifiStatus()', p.context);
  assert.match(p.fields.get('wifi-state-text').textContent, /неизвестно/);
  assert.equal(p.fields.get('wifi-ip-line').hidden, true);
});

test('WiFi роутера: все элементы, которые заполняет скрипт, есть на вкладке «WiFi»', () => {
  const html = fs.readFileSync(path.join(__dirname, '../firmware/data/settings.html'), 'utf8');
  const start = html.indexOf('id="tab-network"');
  const end = html.indexOf('class="tab-panel', start);
  assert.ok(start > 0 && end > start, 'вкладка tab-network найдена');
  const panel = html.slice(start, end);
  for (const id of ['wifi-status', 'wifi-dot', 'wifi-state-text', 'wifi-ip-line', 'wifi-ip', 'wifi-open', 'wifi-extra', 'ap-ip']) {
    assert.equal(html.split('id="' + id + '"').length - 1, 1, id + ' встречается один раз на странице');
    assert.ok(panel.includes('id="' + id + '"'), id + ' лежит внутри вкладки «WiFi»');
  }
  // Адрес сначала скрыт: до ответа платы страница не показывает пустую строку «IP-адрес: —»
  assert.match(html, /id="wifi-ip-line" hidden/);
});
