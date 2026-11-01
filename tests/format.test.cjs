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
  const header = html.slice(html.indexOf('<header class="main-header">'), html.indexOf('</header>'));
  assert.ok(header.includes('id="net-badge"') && header.includes('id="net-ip"'), 'значок лежит внутри шапки');
  assert.equal(html.split('id="net-badge"').length - 1, 1);
  assert.equal(html.split('id="net-ip"').length - 1, 1);
  assert.match(html, /id="net-badge"[^>]*\bhidden\b/, 'до первого ответа платы значок скрыт');
  assert.ok(html.indexOf('/format.js') < html.indexOf('/script.js'), 'format.js грузится раньше script.js');
  const script = fs.readFileSync(path.join(__dirname, '../firmware/data/script.js'), 'utf8');
  assert.ok(script.includes('Format.routerAddress(data)'), 'renderStatus берёт адрес из ответа платы');
  assert.match(script, /if \(lost\) \{[^}]*renderRouterAddress\(null\)/, 'без связи с платой значок убирается');
});

// ----- Шапка главной страницы в одну строку: значок цикла и кнопка-шестерёнка ----------------------
const {cycleView} = require('../firmware/data/format.js');
const dataFile = name => fs.readFileSync(path.join(__dirname, '../firmware/data', name), 'utf8');

test('значок цикла: фазу суток передаёт значок, текст — день цикла', () => {
  const grow = {operation_mode: 0, clock_usable: true, grow_day: 160};
  assert.deepEqual(cycleView({...grow, is_day: true}),
    {icon: '\u2600\uFE0F', label: 'Сейчас день', text: 'День 160', kind: 'day', hint: 'Сейчас день, день цикла 160'});
  assert.deepEqual(cycleView({...grow, is_day: false}),
    {icon: '\u{1F319}', label: 'Сейчас ночь', text: 'День 160', kind: 'night', hint: 'Сейчас ночь, день цикла 160'});
});

test('значок цикла: цикл не начат, сушка и нет времени', () => {
  const notStarted = cycleView({is_day: true, clock_usable: true, grow_day: 0});
  assert.equal(notStarted.text, 'Цикл не начат');
  assert.equal(notStarted.hint, 'Сейчас день, цикл не начат');
  const drying = cycleView({operation_mode: 1, is_day: true, grow_day: 40});
  assert.deepEqual([drying.icon, drying.text, drying.kind], ['\u{1F342}', 'Сушка', 'drying']);
  // без времени день и ночь неизвестны, поэтому номер дня цикла не показываем
  const noTime = cycleView({clock_usable: false, is_day: true, grow_day: 40});
  assert.deepEqual([noTime.icon, noTime.text, noTime.kind], ['\u{1F553}', 'Нет времени', 'night']);
  assert.equal(cycleView({operation_mode: 1, clock_usable: false}).kind, 'drying'); // сушка важнее
});

test('значок цикла: текст короткий, слова «цикла» в нём нет, а пустой ответ не ломает страницу', () => {
  const states = [
    {is_day: true, grow_day: 365}, {is_day: false, grow_day: 1}, {is_day: true, grow_day: 0},
    {operation_mode: 1}, {clock_usable: false}, {}
  ];
  for (const state of states) {
    const view = cycleView(state);
    assert.ok(view.text.length <= 13, view.text + ': в одну строку шапки на телефоне помещается до 13 символов');
    assert.ok(!/цикла/i.test(view.text), view.text);
    assert.ok(['day', 'night', 'drying'].includes(view.kind), view.kind);
    for (const field of ['icon', 'label', 'text', 'hint']) assert.ok(view[field], field);
  }
  assert.equal(cycleView({}).text, 'Цикл не начат'); // поля нет: цикл не начат, а без is_day считается ночь
});

test('шапка главной: время, значок цикла, адрес, колокольчик и шестерёнка идут по порядку, старых элементов нет', () => {
  const html = dataFile('index.html');
  assert.ok(html.includes('<header class="main-header">'), 'мобильные правила привязаны к классу main-header');
  const header = html.slice(html.indexOf('<header class="main-header">'), html.indexOf('</header>'));
  const order = ['id="box-time"', 'id="cycle-str"', 'id="cycle-icon"', 'id="cycle-text"', 'id="net-badge"',
                 'id="alerts-bell"', 'href="/settings" class="icon-btn"'].map(part => header.indexOf(part));
  assert.ok(order.every(position => position >= 0), 'все элементы на месте: ' + order);
  assert.deepEqual([...order].sort((a, b) => a - b), order, 'слева направо: время, цикл, адрес, колокольчик, настройки');
  // «Настройки» — только значок с подписью для чтения и подсказкой, видимого слова в шапке нет
  assert.match(header, /<a href="\/settings" class="icon-btn" aria-label="Настройки" title="Настройки">\s*<svg/);
  assert.doesNotMatch(header, />\s*Настройки\s*</);
  assert.ok(!header.includes('btn-nav'));
  // старые части значка цикла удалены, и на них никто не ссылается
  const script = dataFile('script.js');
  for (const removed of ['cycle-phase', 'cycle-divider', 'cycle-day']) {
    assert.ok(!html.includes(removed) && !script.includes(removed), removed);
  }
  assert.ok(script.includes('Format.cycleView(data)'), 'renderStatus берёт значок цикла из format.js');
});

test('стили шапки: цвета значка цикла описаны, на телефоне шапка не переносится, лишнее прячется', () => {
  const css = dataFile('style.css');
  for (const kind of ['day', 'night', 'drying']) assert.ok(css.includes('.status-badge.' + kind + '-badge'), kind);
  const mobile = (css.match(/@media \(max-width: 600px\) \{[\s\S]*?\n\}/g) || [])
    .find(block => /header\.main-header \{[^}]*flex-wrap: nowrap/.test(block));
  assert.ok(mobile, 'для телефона есть блок с header.main-header { flex-wrap: nowrap }');
  // у страницы настроек своя шапка («Параметры» и кнопка): общий тег header на телефоне не трогаем
  assert.doesNotMatch(mobile, /(^|\n)\s*header \{[^}]*nowrap/);
  assert.ok(dataFile('settings.html').includes('<header>'), 'у настроек обычная шапка без класса main-header');
  // контейнер значков высотой в одну строку: что не поместилось, уходит на скрытую вторую (первым адрес платы)
  assert.match(mobile, /\.header-status \{[^}]*flex-wrap: wrap[^}]*overflow: hidden/);
  assert.match(mobile, /\.net-icon \{ display: none; \}/);
  assert.doesNotMatch(css, /\.header-status[^{]*\{[^}]*flex-basis/, 'значки не уходят во вторую строку шапки');
});
