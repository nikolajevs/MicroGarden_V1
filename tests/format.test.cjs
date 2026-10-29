const {test} = require('node:test');
const assert = require('node:assert/strict');
const {formatWorkTime} = require('../firmware/data/format.js');

test('время работы: секунды, минуты, часы', () => {
  assert.equal(formatWorkTime(0), 'не включался');
  assert.equal(formatWorkTime(0.4), 'не включался');
  assert.equal(formatWorkTime(1), '1 с');
  assert.equal(formatWorkTime(59), '59 с');
  assert.equal(formatWorkTime(60), '1 мин');
  assert.equal(formatWorkTime(754), '12 мин');
  assert.equal(formatWorkTime(3599), '59 мин');
  assert.equal(formatWorkTime(3600), '1 ч');
  assert.equal(formatWorkTime(7500), '2 ч 05 мин');
  assert.equal(formatWorkTime(86399), '23 ч 59 мин');
});

test('время работы: неверные значения не ломают страницу', () => {
  for (const bad of [undefined, null, 'x', NaN, Infinity, -5]) assert.equal(formatWorkTime(bad), '--');
});

// ----- Адрес платы в сети роутера (значок в шапке главной страницы) ---------------------------------
const fs = require('node:fs');
const path = require('node:path');
const {routerAddress} = require('../firmware/data/format.js');

test('адрес в шапке: показываем только у подключённой платы', () => {
  assert.equal(routerAddress({wifi_state: 'connected', wifi_ip: '192.168.1.57'}), '192.168.1.57');
  for (const state of ['connecting', 'disconnected', 'not_configured', undefined, 'unknown']) {
    assert.equal(routerAddress({wifi_state: state, wifi_ip: '192.168.1.57'}), null, String(state));
  }
});

test('адрес в шапке: не IPv4, пустой ответ и старая прошивка дают null', () => {
  for (const bad of ['javascript:alert(1)', '<b>x</b>', '192.168.1', '1.2.3.4/evil', '1.2.3.4 ', '', null, undefined, 4, {}]) {
    assert.equal(routerAddress({wifi_state: 'connected', wifi_ip: bad}), null, String(bad));
  }
  assert.equal(routerAddress({wifi_state: 'connected'}), null);
  assert.equal(routerAddress({}), null);
  assert.equal(routerAddress(null), null);
  assert.equal(routerAddress(undefined), null);
});

test('значок адреса стоит в шапке главной, скрыт до ответа платы и подключён к скриптам', () => {
  const html = fs.readFileSync(path.join(__dirname, '../firmware/data/index.html'), 'utf8');
  const header = html.slice(html.indexOf('<header>'), html.indexOf('</header>'));
  assert.ok(header.includes('id="net-badge"') && header.includes('id="net-ip"'), 'значок лежит внутри шапки');
  assert.equal(html.split('id="net-badge"').length - 1, 1);
  assert.equal(html.split('id="net-ip"').length - 1, 1);
  assert.match(html, /id="net-badge"[^>]*\bhidden\b/, 'до первого ответа платы значок скрыт');
  assert.ok(html.indexOf('/format.js') < html.indexOf('/script.js'), 'format.js грузится раньше script.js');
  const script = fs.readFileSync(path.join(__dirname, '../firmware/data/script.js'), 'utf8');
  assert.ok(script.includes('Format.routerAddress(data)'), 'renderStatus берёт адрес из ответа платы');
  assert.match(script, /if \(lost\) \{[^}]*renderRouterAddress\(null\)/, 'без связи с платой значок убирается');
});
