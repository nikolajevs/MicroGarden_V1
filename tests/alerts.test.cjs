const {test} = require('node:test');
const assert = require('node:assert/strict');
const {collectAlerts, summarizeAlerts, LINK_LOST_ALERT} = require('../firmware/data/alerts.js');

// Ответ /api/status здоровой платы в режиме выращивания.
function healthy(extra = {}) {
  return {
    operation_mode: 0, sht_online: true, rtc_online: true, clock_trusted: true, clock_usable: true,
    rtc_status: 'ok', power_ok: true, led_supply_blocked: false, water_sensor_enabled: true,
    water_present: true, last_water_skipped: false, sd_ok: true, ...extra
  };
}
const ids = alerts => alerts.map(a => a.id);

test('здоровая плата и пустой ответ не дают оповещений', () => {
  assert.deepEqual(collectAlerts(healthy()), []);
  assert.deepEqual(collectAlerts(null), []);
  assert.deepEqual(collectAlerts({}), []); // старая прошивка без этих полей: ничего не выдумываем
  assert.deepEqual(summarizeAlerts([]), {count: 0, level: 'none'});
});

test('датчик SHT4x: тяжёлое оповещение', () => {
  const alerts = collectAlerts(healthy({sht_online: false}));
  assert.deepEqual(ids(alerts), ['sht']);
  assert.equal(alerts[0].level, 'bad');
});

test('часы: без времени — тяжёлое, на программном таймере — предупреждение', () => {
  const lost = collectAlerts(healthy({clock_trusted: false, clock_usable: false, rtc_status: 'no_response'}));
  assert.equal(lost[0].level, 'bad');
  assert.match(lost[0].text, /RTC не отвечают.*полив выключен/);
  const timer = collectAlerts(healthy({clock_trusted: false, clock_usable: true, rtc_status: 'no_response'}));
  assert.equal(timer[0].level, 'warn');
  assert.match(timer[0].text, /программному таймеру.*полив работает/);
  const unset = collectAlerts(healthy({clock_trusted: false, clock_usable: false, rtc_status: 'not_set'}));
  assert.match(unset[0].text, /не выставлены/);
});

test('питание: низкое напряжение, потеря измерителя со светом и без света', () => {
  assert.deepEqual(ids(collectAlerts(healthy({led_supply_blocked: true}))), ['power-low']);
  const meterSilent = collectAlerts(healthy({power_ok: false, led_supply_blocked: false}));
  assert.deepEqual(ids(meterSilent), ['power-lost']);
  assert.equal(meterSilent[0].level, 'warn');
  assert.match(meterSilent[0].text, /без защиты/);
  const dark = collectAlerts(healthy({power_ok: false, led_supply_blocked: true}));
  assert.equal(dark[0].level, 'bad');
  assert.match(dark[0].text, /Свет выключен/);
});

test('вода, пропущенный полив и SD-карта: предупреждения', () => {
  assert.deepEqual(ids(collectAlerts(healthy({water_present: false}))), ['water']);
  // датчик воды выключен в настройках: пустой бак оповещением не считается
  assert.deepEqual(collectAlerts(healthy({water_sensor_enabled: false, water_present: false})), []);
  assert.deepEqual(ids(collectAlerts(healthy({last_water_skipped: true}))), ['watering-skipped']);
  assert.deepEqual(ids(collectAlerts(healthy({sd_ok: false}))), ['sd']);
});

test('в режиме сушки нет оповещений про свет и полив', () => {
  const dry = healthy({operation_mode: 1, water_present: false, last_water_skipped: true,
    power_ok: false, led_supply_blocked: true, clock_trusted: false, clock_usable: false, rtc_status: 'no_response'});
  const alerts = collectAlerts(dry);
  assert.deepEqual(ids(alerts), ['clock', 'power-lost']);
  for (const a of alerts) assert.doesNotMatch(a.text, /[Сс]вет|полив/);
});

test('тяжёлые оповещения идут первыми, сводка берёт самый тяжёлый уровень', () => {
  const alerts = collectAlerts(healthy({sd_ok: false, water_present: false, sht_online: false, led_supply_blocked: true}));
  assert.deepEqual(ids(alerts), ['sht', 'power-low', 'water', 'sd']);
  assert.deepEqual(summarizeAlerts(alerts), {count: 4, level: 'bad'});
  assert.deepEqual(summarizeAlerts(collectAlerts(healthy({sd_ok: false}))), {count: 1, level: 'warn'});
});

test('потеря связи с платой — отдельное тяжёлое оповещение', () => {
  assert.equal(LINK_LOST_ALERT.level, 'bad');
  assert.deepEqual(summarizeAlerts([LINK_LOST_ALERT]), {count: 1, level: 'bad'});
});
