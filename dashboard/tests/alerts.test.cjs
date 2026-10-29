'use strict';
// Оповещения панели: связь с платой, неисправности по последним показаниям, свежие ошибки журнала.
// node --test "dashboard/tests/*.test.cjs"
const test = require('node:test');
const assert = require('node:assert/strict');
const {stateAlerts, collectAlerts, summarizeAlerts, SERVER_LOST_ALERT} = require('../static/alerts.js');

// Образец показаний здоровой платы в режиме выращивания (поля — как в dashboard/README.md).
function healthy(extra = {}) {
  return {
    sht_ok: true, rtc_ok: true, clock_trusted: true, clock_usable: true, sd_ok: true, power_ok: true,
    led_blocked: false, water_sensor: true, water_present: true, water_skipped: false, op_mode: 0, ...extra
  };
}
// Ответ /api/status: плата на связи 30 секунд назад, ошибок нет.
function status(sample, extra = {}) {
  return {device: 'm', online_sec: 180, age: 30, sample, errors_1h: 0, errors_24h: 0, ...extra};
}
const ids = alerts => alerts.map(a => a.id);

test('здоровая плата, плата без данных и пустой образец не дают оповещений', () => {
  assert.deepEqual(collectAlerts(status(healthy()), 30), []);
  assert.deepEqual(collectAlerts(null, 0), []);               // плата ещё не присылала данных
  assert.deepEqual(stateAlerts({}), []);                      // старая прошивка без этих полей: ничего не выдумываем
  assert.deepEqual(stateAlerts(null), []);
  assert.deepEqual(summarizeAlerts([]), {count: 0, level: 'none'});
});

test('связь: короткая потеря — предупреждение, долгая — тяжёлое, показания помечаются устаревшими', () => {
  const short = collectAlerts(status(healthy({water_present: false})), 600, sec => Math.round(sec / 60) + ' мин');
  assert.deepEqual(ids(short), ['link', 'water']);
  assert.equal(short[0].level, 'warn');
  assert.match(short[0].text, /Нет связи с платой.*10 мин/);
  assert.match(short[1].text, /\(по последним данным\)$/);
  const long = collectAlerts(status(healthy()), 7200);
  assert.equal(long[0].level, 'bad');
  // пока плата на связи, пометки «по последним данным» нет
  const live = collectAlerts(status(healthy({water_present: false})), 30);
  assert.doesNotMatch(live[0].text, /по последним данным/);
});

test('порог связи берётся из ответа сервера', () => {
  assert.deepEqual(ids(collectAlerts(status(healthy(), {online_sec: 600}), 500)), []);
  assert.deepEqual(ids(collectAlerts(status(healthy(), {online_sec: 600}), 700)), ['link']);
});

test('свежие ошибки журнала: предупреждение с пунктом «Показать»', () => {
  const alerts = collectAlerts(status(healthy(), {errors_1h: 3}), 30);
  assert.deepEqual(ids(alerts), ['log-errors']);
  assert.equal(alerts[0].level, 'warn');
  assert.equal(alerts[0].action, 'log');
  assert.match(alerts[0].text, /за последний час: 3/);
});

test('неисправности: датчик, часы, питание, вода, SD', () => {
  assert.deepEqual(ids(stateAlerts(healthy({sht_ok: false}))), ['sht']);
  assert.deepEqual(ids(stateAlerts(healthy({sd_ok: false}))), ['sd']);
  assert.deepEqual(ids(stateAlerts(healthy({water_present: false}))), ['water']);
  assert.deepEqual(ids(stateAlerts(healthy({water_skipped: true}))), ['watering-skipped']);
  // датчик воды выключен в настройках: пустой бак оповещением не считается
  assert.deepEqual(stateAlerts(healthy({water_sensor: false, water_present: false})), []);
  const lowSupply = stateAlerts(healthy({led_blocked: true}));
  assert.deepEqual(ids(lowSupply), ['power-low']);
  assert.equal(lowSupply[0].level, 'bad');
  const meter = stateAlerts(healthy({power_ok: false}));
  assert.deepEqual(ids(meter), ['power-lost']);
  assert.equal(meter[0].level, 'warn');
});

test('часы: без времени тяжёлое, на программном таймере — предупреждение', () => {
  const lost = stateAlerts(healthy({rtc_ok: false, clock_trusted: false, clock_usable: false}));
  assert.equal(lost[0].level, 'bad');
  assert.match(lost[0].text, /RTC не отвечают.*свет и полив выключены/);
  const timer = stateAlerts(healthy({rtc_ok: false, clock_trusted: false, clock_usable: true}));
  assert.equal(timer[0].level, 'warn');
  assert.match(timer[0].text, /программному таймеру.*свет и полив работают/);
  // RTC отвечает, но доверия ещё нет — идёт проверка
  assert.match(stateAlerts(healthy({clock_trusted: false, clock_usable: false}))[0].text, /Проверка часов/);
  // поле clock_usable старая прошивка не присылает: считаем, что времени нет
  const noField = healthy({rtc_ok: false, clock_trusted: false});
  delete noField.clock_usable;
  assert.equal(stateAlerts(noField)[0].level, 'bad');
});

test('измеритель и датчик молчат: свет на минимуме, это предупреждение, а не «свет выключен»', () => {
  const alerts = stateAlerts(healthy({power_ok: false, sht_ok: false}));
  assert.deepEqual(ids(alerts), ['sht', 'power-lost']);
  assert.match(alerts.find(a => a.id === 'power-lost').text, /минимальном уровне/);
  assert.match(alerts.find(a => a.id === 'sht').text, /свет на минимальном уровне/);
  const dark = stateAlerts(healthy({power_ok: false, led_blocked: true}));
  assert.equal(dark[0].level, 'bad');
  assert.match(dark[0].text, /Свет выключен: нет данных питания и нет времени/);
  // свет заблокирован: про минимум не пишем
  assert.doesNotMatch(stateAlerts(healthy({sht_ok: false, led_blocked: true, power_ok: false}))
    .find(a => a.id === 'sht').text, /свет на минимальном/);
});

test('в режиме сушки нет оповещений про свет и полив', () => {
  const dry = healthy({op_mode: 1, water_present: false, water_skipped: true, power_ok: false, led_blocked: true,
    rtc_ok: false, clock_trusted: false, clock_usable: false});
  const alerts = stateAlerts(dry);
  assert.deepEqual(ids(alerts), ['clock', 'power-lost']);
  for (const a of alerts) assert.doesNotMatch(a.text, /[Сс]вет|полив/);
});

test('порядок и сводка: тяжёлые первыми, цвет по самому тяжёлому', () => {
  const alerts = collectAlerts(status(healthy({sd_ok: false, water_present: false, sht_ok: false, led_blocked: true}),
    {errors_1h: 2}), 30);
  assert.deepEqual(ids(alerts), ['sht', 'power-low', 'water', 'sd', 'log-errors']);
  assert.deepEqual(summarizeAlerts(alerts), {count: 5, level: 'bad'});
  assert.deepEqual(summarizeAlerts(collectAlerts(status(healthy({sd_ok: false})), 30)), {count: 1, level: 'warn'});
});

test('потеря связи с сервером панели — отдельное тяжёлое оповещение', () => {
  assert.equal(SERVER_LOST_ALERT.level, 'bad');
  assert.deepEqual(summarizeAlerts([SERVER_LOST_ALERT]), {count: 1, level: 'bad'});
});
