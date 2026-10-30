'use strict';
// Оповещения на панели (dashboard/static/alerts.js) и на главной странице платы (firmware/data/alerts.js)
// описывают одни и те же неисправности. Здесь проверяется, что для любого сочетания состояний они дают одни и те же
// оповещения: те же номера, уровни и (кроме часов) тексты. Это не даёт двум файлам разойтись при правках.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const board = require('../firmware/data/alerts.js');
const panel = require('../dashboard/static/alerts.js');

// Как прошивка превращает состояние платы в образец телеметрии (см. buildTelemetryPayload в firmware.ino).
function toSample(d) {
  return {
    sht_ok: d.sht_online, rtc_ok: d.rtc_online, clock_trusted: d.clock_trusted, clock_usable: d.clock_usable,
    sd_ok: d.sd_ok, power_ok: d.power_ok, led_blocked: d.led_supply_blocked, water_sensor: d.water_sensor_enabled,
    water_present: d.water_present, water_skipped: d.last_water_skipped, op_mode: d.operation_mode
  };
}

test('для всех сочетаний состояний панель и плата показывают одни и те же оповещения', () => {
  const flags = ['sht_online', 'rtc_trusted', 'clock_usable', 'power_ok', 'led_supply_blocked',
    'water_sensor_enabled', 'water_present', 'last_water_skipped', 'sd_ok', 'drying'];
  let checked = 0;
  for (let mask = 0; mask < (1 << flags.length); mask++) {
    const bit = name => Boolean(mask & (1 << flags.indexOf(name)));
    const trusted = bit('rtc_trusted');
    const d = {
      operation_mode: bit('drying') ? 1 : 0,
      sht_online: bit('sht_online'),
      // часы доверенные — RTC отвечает; недоверенные — RTC не отвечает (прошивка в этом случае ставит rtc_online = false)
      rtc_online: trusted, clock_trusted: trusted, rtc_status: trusted ? 'ok' : 'no_response',
      clock_usable: trusted || bit('clock_usable'),
      power_ok: bit('power_ok'), led_supply_blocked: bit('led_supply_blocked'),
      water_sensor_enabled: bit('water_sensor_enabled'), water_present: bit('water_present'),
      last_water_skipped: bit('last_water_skipped'), sd_ok: bit('sd_ok')
    };
    const fromBoard = board.collectAlerts(d);
    const fromPanel = panel.stateAlerts(toSample(d));
    const brief = alerts => alerts.map(a => `${a.id}:${a.level}`);
    assert.deepEqual(brief(panel.collectAlerts({online_sec: 180, sample: toSample(d), errors_1h: 0}, 10)), brief(fromBoard),
      `маска ${mask}: collectAlerts панели расходится с платой`);
    // порядок: плата сортирует «тяжёлые первыми», панель делает то же при сборке полного списка
    const sortedPanel = fromPanel.filter(a => a.level === 'bad').concat(fromPanel.filter(a => a.level === 'warn'));
    assert.deepEqual(brief(sortedPanel), brief(fromBoard), `маска ${mask}: правила расходятся`);
    for (const a of fromBoard) {
      const twin = fromPanel.find(b => b.id === a.id);
      if (a.id !== 'clock') {
        // тексты про часы различаются: плата знает причину («не отвечают» или «не выставлены»), панель нет
        assert.equal(twin.text, a.text, `маска ${mask}: текст оповещения ${a.id} расходится`);
      }
    }
    checked++;
  }
  assert.equal(checked, 1024);
});

test('тексты про часы совпадают по смыслу: оба называют последствия для света и полива', () => {
  const d = {operation_mode: 0, clock_trusted: false, rtc_online: false, rtc_status: 'no_response', clock_usable: false};
  const a = board.collectAlerts(d).find(x => x.id === 'clock');
  const b = panel.stateAlerts(toSample(d)).find(x => x.id === 'clock');
  for (const text of [a.text, b.text]) assert.match(text, /RTC не отвечают.*свет и полив выключены, пока время не выставлено/);
});
