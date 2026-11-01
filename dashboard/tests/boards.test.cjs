// Тесты страницы плат: правила подписей и проверки формы (boards-lib.js) и согласованность страницы
// со скриптом и сервером. Запуск: node --test "dashboard/tests/*.test.cjs"
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Lib = require('../static/boards-lib.js');

const read = (...parts) => fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
const NOW = 1_800_000_000;

test('«использовался»: только что, минуты, часы, дни, ни разу', () => {
  assert.equal(Lib.agoText(undefined, NOW), 'не использовался');
  assert.equal(Lib.agoText(null, NOW), 'не использовался');
  assert.equal(Lib.agoText(NaN, NOW), 'не использовался');
  assert.equal(Lib.agoText(NOW, NOW), 'только что');
  assert.equal(Lib.agoText(NOW - 59, NOW), 'только что');
  assert.equal(Lib.agoText(NOW - 60, NOW), '1 мин назад');
  assert.equal(Lib.agoText(NOW - 3599, NOW), '59 мин назад');
  assert.equal(Lib.agoText(NOW - 3600, NOW), '1 ч назад');
  assert.equal(Lib.agoText(NOW - 86399, NOW), '23 ч назад');
  assert.equal(Lib.agoText(NOW - 86400, NOW), '1 дн назад');
  assert.equal(Lib.agoText(NOW - 5 * 86400, NOW), '5 дн назад');
  assert.equal(Lib.agoText(NOW + 30, NOW), 'только что', 'часы страницы отстают от сервера: не «минус минут назад»');
});

test('связь платы: онлайн, давно молчит, данных ещё не было', () => {
  assert.deepEqual(Lib.boardStatus({ last_seen: NOW - 30, online: true }, NOW), { status: 'online', text: 'Онлайн' });
  assert.deepEqual(Lib.boardStatus({ last_seen: NOW - 600, online: false }, NOW),
    { status: 'offline', text: 'Нет связи, данные были 10 мин назад' });
  assert.deepEqual(Lib.boardStatus({ last_seen: null, online: false }, NOW), { status: 'never', text: 'Данных ещё не было' });
});

test('токен платы: действует, используется, отозван', () => {
  const base = { id: 1, name: 'Основной', device: 'microgarden-2', prefix: 'mgd_Ab3d', created_at: NOW - 86400, last_ip: '10.0.0.5' };
  const idle = Lib.describeToken({ ...base, active: true, last_used_at: null, revoked_at: null }, NOW);
  assert.deepEqual([idle.status, idle.statusText, idle.used, idle.canRevoke, idle.canDelete],
    ['active', 'Действует', 'Ни разу не использовался', true, false]);
  assert.equal(idle.key, 'mgd_Ab3d…');

  const busy = Lib.describeToken({ ...base, active: true, last_used_at: NOW - 120, revoked_at: null }, NOW);
  assert.deepEqual([busy.status, busy.statusText, busy.inUse], ['in-use', 'Используется', true]);
  assert.equal(busy.used, 'Использовался 2 мин назад (с 10.0.0.5)');

  const old = Lib.describeToken({ ...base, active: true, last_used_at: NOW - Lib.IN_USE_SEC, revoked_at: null }, NOW);
  assert.equal(old.status, 'active', 'после 10 минут тишины токен уже не «используется»');

  const gone = Lib.describeToken({ ...base, active: false, last_used_at: NOW - 5, revoked_at: NOW - 1 }, NOW);
  assert.deepEqual([gone.status, gone.statusText, gone.canRevoke, gone.canDelete, gone.inUse], ['revoked', 'Отозван', false, true, false]);
});

test('у платы без действующего токена появляется предупреждение', () => {
  assert.equal(Lib.hasActiveToken({ tokens: [] }), false);
  assert.equal(Lib.hasActiveToken({ tokens: [{ active: false }] }), false);
  assert.equal(Lib.hasActiveToken({ tokens: [{ active: false }, { active: true }] }), true);
});

test('форма: название и имя платы проверяются как на сервере', () => {
  assert.deepEqual(Lib.validateNewBoard('  Плата   в теплице ', ' microgarden-2 '),
    { ok: true, name: 'Плата в теплице', device: 'microgarden-2' });
  for (const name of ['', '   ', null, undefined, 'x'.repeat(Lib.NAME_MAX + 1), 'a\u0000b']) {
    assert.equal(Lib.validateNewBoard(name, 'ok-m').ok, false, JSON.stringify(name));
  }
  assert.equal(Lib.validateNewBoard('x'.repeat(Lib.NAME_MAX), 'ok-m').ok, true);
  for (const device of ['', '   ', null, '../x', 'bad name', 'x'.repeat(33), 'плата']) {
    assert.equal(Lib.validateNewBoard('ok', device).ok, false, JSON.stringify(device));
  }
  assert.match(Lib.validateNewBoard('', 'ok-m').error, /Название/);
  assert.match(Lib.validateNewBoard('ok', '../x').error, /Имя платы/);
  assert.deepEqual(Lib.validateName('  Новое  имя '), { ok: true, name: 'Новое имя' });
  assert.equal(Lib.validateName('').ok, false);
});

test('подсказка имени для новой платы пропускает занятые', () => {
  assert.equal(Lib.suggestDevice([]), 'microgarden-2');
  assert.equal(Lib.suggestDevice(['microgarden-v1']), 'microgarden-2');
  assert.equal(Lib.suggestDevice(['microgarden-2', 'microgarden-3']), 'microgarden-4');
  assert.ok(Lib.DEVICE_RE.test(Lib.suggestDevice([])), 'подсказка сама подходит под правило имени');
});

test('адрес приёма для настроек платы', () => {
  assert.equal(Lib.ingestUrl('https://dash.dvinsk.lat'), 'https://dash.dvinsk.lat/api/ingest');
});

test('страница и сервер используют одни и те же ограничения', () => {
  const server = read('server.py');
  assert.match(server, new RegExp('NAME_MAX = ' + Lib.NAME_MAX + '\\b'));
  const serverRe = server.match(/DEVICE_RE = re\.compile\(r"([^"]+)"\)/)[1];
  assert.equal(serverRe, '^' + Lib.DEVICE_RE.source.replace(/^\^|\$$/g, '') + '$');
});

test('все элементы, которые ищет boards.js, есть в boards.html, а скрипты подключены по порядку', () => {
  const html = read('static', 'boards.html');
  const script = read('static', 'boards.js');
  const ids = new Set([...script.matchAll(/\$\('([\w-]+)'\)/g)].map((m) => m[1]));
  assert.ok(ids.size >= 15, 'найдены обращения к элементам страницы');
  for (const id of ids) {
    assert.equal(html.split('id="' + id + '"').length - 1, 1, 'элемент #' + id + ' есть на странице ровно один раз');
  }
  assert.ok(!/<script>/.test(html) && !/ style=/.test(html), 'CSP запрещает встроенные скрипты и стили');
  const sources = [...html.matchAll(/<script src="([^"]+)">/g)].map((m) => m[1]);
  assert.deepEqual(sources, ['/boards-lib.js', '/boards.js'], 'библиотека подключена до скрипта страницы');
  const served = read('server.py');
  for (const src of [...sources, '/boards', '/boards.html', '/style.css']) {
    assert.ok(served.includes('"' + src + '":'), src + ' раздаётся сервером');
  }
  assert.match(html, /id="created" class="created" role="status" hidden/, 'токен скрыт, пока плату не добавили');
});

test('со страницы графиков есть ссылка на платы, а запросы, меняющие данные, несут заголовок страницы', () => {
  assert.match(read('static', 'index.html'), /<a href="\/boards" class="icon-btn" aria-label="Платы"/);
  const script = read('static', 'boards.js');
  assert.match(script, /'X-Dash-Action': '1'/);
  assert.match(script, /'Content-Type'\] = 'application\/json'/);
  assert.ok(!/localStorage|sessionStorage/.test(script), 'созданный токен нигде не сохраняется');
  assert.match(script, /\$\('created-token'\)\.textContent = ''/, 'при закрытии окна токен стирается со страницы');
  // страница графиков показывает название платы, а не только её имя в системе
  assert.match(read('static', 'app.js'), /d\.name \|\| d\.device/);
});
