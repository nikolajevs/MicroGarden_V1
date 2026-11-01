// Тесты страницы «Настройки платы»: правила подписей и проверок (settings-lib.js) и согласованность страницы со
// скриптом и сервером. Запуск: node --test "dashboard/tests/*.test.cjs"
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Lib = require('../static/settings-lib.js');

const read = (...parts) => fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
const NOW = 1_800_000_000;

// Часть схемы, которую сервер присылает странице (server.py: CFG_SCHEMA)
const SCHEMA = [
  { id: 'climate', title: 'Климат', fields: [
    { type: 'number', param: 'temp_target', label: 'Дневная целевая температура', unit: '°C', step: 0.1, kind: 'float', lo: 0, hi: 40 },
    { type: 'number', param: 'led_min_limit', label: 'Минимальная мощность лампы', unit: '%', step: 1, kind: 'int', lo: 0, hi: 100 },
  ] },
  { id: 'misc', title: 'Прочее', fields: [
    { type: 'select', param: 'heater_mode', label: 'Режим обогревателя', kind: 'int', lo: 0, hi: 3,
      options: [[0, 'Только днём'], [1, 'Только ночью'], [2, 'Всегда'], [3, 'Никогда']] },
    { type: 'time', params: ['led_on_hour', 'led_on_minute'], label: 'Свет включается' },
    { type: 'days', param: 'watering_days', label: 'Дни полива', kind: 'int', lo: 0, hi: 127 },
    { type: 'date', param: 'start_time', label: 'Дата начала', kind: 'date', lo: 0, hi: 4102444800 },
    { type: 'flag', param: 'water_sensor_enabled', label: 'Датчик уровня воды' },
  ] },
];
const VALUES = { temp_target: 25, led_min_limit: 10, heater_mode: 3, led_on_hour: 6, led_on_minute: 5, watering_days: 0b0010101,
                 start_time: 1793491200, water_sensor_enabled: true };

test('время назад и остаток до отмены запроса', () => {
  assert.equal(Lib.ageText(5), 'только что');
  assert.equal(Lib.ageText(59.9), 'только что');
  assert.equal(Lib.ageText(60), '1 мин назад');
  assert.equal(Lib.ageText(3599), '59 мин назад');
  assert.equal(Lib.ageText(7199), '1 ч назад');
  assert.equal(Lib.ageText(3 * 86400), '3 дн назад');
  assert.equal(Lib.ageText(-5), 'только что', 'часы страницы отстают от сервера');
  assert.equal(Lib.remainingText(0), 'время вышло');
  assert.equal(Lib.remainingText(-3), 'время вышло');
  assert.equal(Lib.remainingText(30), 'меньше минуты');
  assert.equal(Lib.remainingText(61), 'ещё 2 мин');
  assert.equal(Lib.remainingText(600), 'ещё 10 мин');
});

test('время суток: 06:05 в два числа и обратно, неверное отклоняется', () => {
  assert.equal(Lib.timeText(6, 5), '06:05');
  assert.deepEqual(Lib.parseTime('06:05'), [6, 5]);
  assert.deepEqual(Lib.parseTime('23:59'), [23, 59]);
  for (const bad of ['', '24:00', '12:60', '7', 'ab:cd', null, undefined, '12:5']) assert.equal(Lib.parseTime(bad), null, String(bad));
});

test('дата начала цикла: полночь по UTC, как хранит плата; пустая — цикл не начат', () => {
  assert.equal(Lib.parseDate('2026-11-01'), 1793491200);
  assert.equal(Lib.dateText(1793491200), '2026-11-01');
  assert.equal(Lib.parseDate(''), 0);
  assert.equal(Lib.parseDate('  '), 0);
  assert.equal(Lib.dateText(0), '');
  assert.equal(Lib.dateText(undefined), '');
  for (const bad of ['2026-02-30', '2026-13-01', '01.11.2026', '2026-1-1', 'вчера']) assert.equal(Lib.parseDate(bad), null, bad);
  for (const date of ['2024-02-29', '2026-12-31', '2099-01-01']) assert.equal(Lib.dateText(Lib.parseDate(date)), date, 'туда и обратно: ' + date);
});

test('дни полива: бит 0 — понедельник, как на плате', () => {
  assert.deepEqual(Lib.DAYS, ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']);
  assert.equal(Lib.daysText(0), 'не поливать');
  assert.equal(Lib.daysText(127), 'каждый день');
  assert.equal(Lib.daysText(0b0010101), 'Пн, Ср, Пт');
  assert.equal(Lib.daysText(0b1000000), 'Вс');
});

test('значение настройки текстом по её виду', () => {
  const field = (param) => Lib.allFields(SCHEMA).find((f) => (f.param || f.params[0]) === param);
  assert.equal(Lib.formatValue(field('temp_target'), VALUES), '25 °C');
  assert.equal(Lib.formatValue(field('heater_mode'), VALUES), 'Никогда');
  assert.equal(Lib.formatValue(field('led_on_hour'), VALUES), '06:05');
  assert.equal(Lib.formatValue(field('led_on_hour'), { led_on_hour: 6 }), '—', 'часа без минут недостаточно');
  assert.equal(Lib.formatValue(field('watering_days'), VALUES), 'Пн, Ср, Пт');
  assert.equal(Lib.formatValue(field('start_time'), VALUES), '2026-11-01');
  assert.equal(Lib.formatValue(field('start_time'), { start_time: 0 }), 'не начат');
  assert.equal(Lib.formatValue(field('water_sensor_enabled'), VALUES), 'включён');
  assert.equal(Lib.formatValue(field('water_sensor_enabled'), { water_sensor_enabled: false }), 'выключен');
  assert.equal(Lib.formatValue(field('temp_target'), {}), '—');
});

test('правки: только то, что отличается от значений на плате, числа с разницей меньше сотых не считаются', () => {
  assert.deepEqual(Lib.changesOf({ temp_target: 25, led_min_limit: 20 }, VALUES), { led_min_limit: 20 });
  assert.deepEqual(Lib.changesOf({ temp_target: 25.0001 }, VALUES), {});
  assert.deepEqual(Lib.changesOf({ temp_target: 25.1 }, VALUES), { temp_target: 25.1 });
  assert.deepEqual(Lib.changesOf({ start_time: 0 }, VALUES), { start_time: 0 });
  assert.deepEqual(Lib.changesOf({}, VALUES), {});
  assert.deepEqual(Lib.changesOf({ new_param: 1 }, VALUES), { new_param: 1 }, 'настройки, которой платa не сообщала, тоже правка');
});

test('числовое поле проверяется как на сервере: число, целое там где нужно, в пределах', () => {
  const temp = SCHEMA[0].fields[0];
  const limit = SCHEMA[0].fields[1];
  assert.deepEqual(Lib.checkNumber(temp, '24.5'), { ok: true, value: 24.5 });
  assert.deepEqual(Lib.checkNumber(temp, ' 24,55 '), { ok: true, value: 24.55 }, 'запятая вместо точки');
  assert.deepEqual(Lib.checkNumber(temp, '24.567'), { ok: true, value: 24.57 }, 'плата хранит сотые');
  assert.deepEqual(Lib.checkNumber(temp, '0'), { ok: true, value: 0 });
  assert.deepEqual(Lib.checkNumber(temp, '40'), { ok: true, value: 40 });
  assert.match(Lib.checkNumber(temp, '40.01').error, /«Дневная целевая температура»: от 0 до 40/);
  assert.match(Lib.checkNumber(temp, '-1').error, /от 0 до 40/);
  for (const bad of ['', '  ', 'abc', '1e3', '--1', '1.', '.5', '24.5.1', null, undefined]) {
    assert.match(Lib.checkNumber(temp, bad).error, /введите число/, String(bad));
  }
  assert.match(Lib.checkNumber(limit, '12.5').error, /нужно целое число/);
  assert.deepEqual(Lib.checkNumber(limit, '100'), { ok: true, value: 100 });
  assert.match(Lib.checkNumber(limit, '101').error, /от 0 до 100/);
});

test('минимум не больше максимума, пары те же, что на сервере', () => {
  assert.equal(Lib.checkOrder({ led_min_limit: 10, led_max_limit: 100 }), '');
  assert.equal(Lib.checkOrder({ led_min_limit: 100, led_max_limit: 100 }), '');
  assert.equal(Lib.checkOrder({ led_min_limit: 90, led_max_limit: 80 }), 'Мощность лампы: минимум не может быть больше максимума');
  assert.match(Lib.checkOrder({ fan1_min_limit: 30, fan1_max_limit: 20 }), /^Вентилятор 1/);
  assert.match(Lib.checkOrder({ fan2_min_limit: 30, fan2_max_limit: 20 }), /^Вентилятор 2/);
  assert.equal(Lib.checkOrder({ led_min_limit: 90 }), '', 'второй границы в данных нет: проверять нечего');
  const server = read('server.py');
  const pairs = server.match(/CFG_PAIRS = \(([\s\S]*?)\)\s*# /)[1];
  for (const [low, high, title] of [['led_min_limit', 'led_max_limit', 'Мощность лампы'], ['fan1_min_limit', 'fan1_max_limit', 'Вентилятор 1'],
    ['fan2_min_limit', 'fan2_max_limit', 'Вентилятор 2']]) {
    assert.ok(pairs.includes('("' + low + '", "' + high + '", "' + title + '")'), title + ' есть в server.py');
  }
});

test('список изменений: подпись, было и станет; пара «час, минута» — одной строкой', () => {
  const lines = Lib.describeChanges(SCHEMA, { temp_target: 26, led_on_hour: 7, heater_mode: 0 }, VALUES);
  assert.deepEqual(lines, [
    { label: 'Климат · Дневная целевая температура', from: '25 °C', to: '26 °C' },
    { label: 'Прочее · Режим обогревателя', from: 'Никогда', to: 'Только днём' },
    { label: 'Прочее · Свет включается', from: '06:05', to: '07:05' },
  ]);
  assert.deepEqual(Lib.describeChanges(SCHEMA, { mystery: 5 }, VALUES), [{ label: 'mystery', from: '—', to: '5' }]);
});

test('причины отказа: понятным языком, по коду из журнала запроса', () => {
  assert.match(Lib.rejectionText(SCHEMA, 'range:temp_target'), /вне допустимых пределов: «Климат · Дневная целевая температура»/);
  assert.match(Lib.rejectionText(SCHEMA, 'param:wifi_pass'), /нельзя менять из панели: wifi_pass/);
  assert.match(Lib.rejectionText(SCHEMA, 'order:led'), /минимум больше максимума/);
  assert.match(Lib.rejectionText(SCHEMA, 'off'), /выключено разрешение/);
  assert.equal(Lib.rejectionText(SCHEMA, 'weird'), 'код weird');
  assert.equal(Lib.rejectionText(SCHEMA, ''), 'причина не указана');
});

test('состояние запроса: ждёт, применён, отклонён, просрочен, отменён', () => {
  const at = (extra) => Lib.requestStatus(SCHEMA, Object.assign({ created_at: NOW - 100, expires_at: NOW + 500, note: '' }, extra), NOW);
  assert.deepEqual(at({ status: 'pending' }), { text: 'Ждёт плату, ещё 9 мин', kind: 'in-use' });
  // В истории у запроса нет срока годности: он считается от времени запроса и срока ожидания сервера
  assert.equal(Lib.requestStatus(SCHEMA, { status: 'pending', created_at: NOW - 100 }, NOW, 600).text, 'Ждёт плату, ещё 9 мин');
  assert.equal(Lib.requestStatus(SCHEMA, { status: 'pending', created_at: NOW - 100 }, NOW).text, 'Ждёт плату, ещё 9 мин', 'по умолчанию 10 минут');
  assert.deepEqual(at({ status: 'applied' }), { text: 'Применено платой', kind: 'online' });
  assert.equal(at({ status: 'rejected', note: 'range:temp_target' }).kind, 'offline');
  assert.match(at({ status: 'rejected', note: 'range:temp_target' }).text, /^Плата не применила: значение вне допустимых пределов/);
  assert.match(at({ status: 'expired' }).text, /Не доставлено/);
  assert.deepEqual(at({ status: 'cancelled' }), { text: 'Отменено', kind: 'never' });
});

test('связь с платой: нет настроек, правка выключена на плате, нет связи, всё в порядке', () => {
  const state = { supported: true, cloud: true, online: true, now: NOW, reported_at: NOW - 30, ttl: 600 };
  const ok = Lib.linkState(state);
  assert.deepEqual([ok.form, ok.editable, ok.kind, ok.pill], [true, true, 'online', 'Онлайн']);
  assert.match(ok.text, /Настройки получены только что/);
  const off = Lib.linkState(Object.assign({}, state, { cloud: false }));
  assert.deepEqual([off.form, off.editable, off.kind], [true, false, 'offline']);
  assert.match(off.text, /Разрешить менять настройки платы из панели/, 'сказано, где это включить');
  const gone = Lib.linkState(Object.assign({}, state, { online: false, reported_at: NOW - 7200 }));
  assert.deepEqual([gone.form, gone.editable, gone.pill], [true, true, 'Нет связи']);
  assert.match(gone.text, /2 ч назад/);
  assert.match(gone.text, /10 мин/);
  const old = Lib.linkState({ supported: false, cloud: false, online: true, now: NOW });
  assert.deepEqual([old.form, old.editable], [false, false]);
  assert.match(old.text, /прошивка/);
});

const PAGE = ['settings.html', 'settings.js'];

test('все элементы, которые ищет скрипт страницы, есть в разметке ровно один раз, встроенного кода нет', () => {
  const html = read('static', PAGE[0]);
  const script = read('static', PAGE[1]);
  const ids = new Set([...script.matchAll(/\$\('([\w-]+)'\)/g)].map((m) => m[1]));
  assert.ok(ids.size >= 10);
  for (const id of ids) assert.equal(html.split('id="' + id + '"').length - 1, 1, 'элемент #' + id);
  assert.ok(!/<script>/.test(html) && !/ style=/.test(html), 'CSP запрещает встроенные скрипты и стили');
  assert.deepEqual([...html.matchAll(/<script src="([^"]+)">/g)].map((m) => m[1]), ['/settings-lib.js', '/settings.js']);
});

test('страница и её скрипты отдаются только вошедшим, ссылка есть на главной', () => {
  const server = read('server.py');
  const pages = server.match(/\nPAGES = \{([\s\S]*?)\}/)[1];
  assert.ok(pages.includes('"/settings": "settings.html"'));
  const assets = server.match(/ASSETS = \{([\s\S]*?)\n\}/)[1];
  for (const file of ['settings-lib.js', 'settings.js']) assert.ok(assets.includes('"/' + file + '": ("' + file + '"'), file);
  const index = read('static', 'index.html');
  assert.equal(index.split('id="board-settings"').length - 1, 1);
  assert.match(index, /id="board-settings" href="\/settings"/);
  assert.match(read('static', 'app.js'), /\$\('board-settings'\)\.href = '\/settings\?'/, 'ссылка ведёт на выбранную плату');
});

test('запросы страницы идут с заголовком X-Dash-Action, сессия и токены нигде не сохраняются', () => {
  const script = read('static', 'settings.js');
  assert.match(script, /method !== 'GET'\) headers\['X-Dash-Action'\] = '1'/);
  assert.ok(!/localStorage|sessionStorage|innerHTML/.test(script), 'значения с платы попадают на страницу только через textContent');
  assert.match(script, /res\.status === 401\) location\.assign\('\/login\?next='/, 'сессия закончилась: на страницу входа');
  for (const call of script.matchAll(/api\('(\w+)', '([^']+)'/g)) {
    assert.ok(['GET', 'POST'].includes(call[1]) && call[2].startsWith('/api/'), call[0]);
  }
});

test('схема страницы: каждое поле выбора и числа несёт пределы, а время и дни разбираются библиотекой', () => {
  const script = read('static', 'settings.js');
  for (const type of ['number', 'select', 'time', 'date', 'days']) assert.ok(script.includes("field.type === '" + type + "'"), 'поле ' + type);
  const server = read('server.py');
  for (const type of ['number', 'select', 'time', 'date', 'days', 'flag']) assert.ok(server.includes('"type": "' + type + '"'), 'сервер присылает ' + type);
});
