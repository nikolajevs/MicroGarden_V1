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

test('числительные: 1 плата, 2 платы, 5 плат, 11 плат, 21 плата', () => {
  const forms = [0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 111].map((n) => n + ' ' + Lib.plural(n, 'плата', 'платы', 'плат'));
  assert.deepEqual(forms, ['0 плат', '1 плата', '2 платы', '4 платы', '5 плат', '11 плат', '12 плат', '14 плат', '21 плата', '22 платы',
    '25 плат', '101 плата', '111 плат']);
});

test('значок связи на карточке: онлайн, нет связи с давностью, данных не было', () => {
  assert.deepEqual(Lib.boardChip({ last_seen: NOW - 30, online: true }, NOW),
    { state: 'online', text: 'Онлайн', title: 'Данные пришли только что' });
  const off = Lib.boardChip({ last_seen: NOW - 3 * 3600, online: false }, NOW);
  assert.deepEqual([off.state, off.text], ['offline', 'Нет связи · 3 ч назад']);
  assert.deepEqual(Lib.boardChip({ last_seen: null, online: false }, NOW),
    { state: 'never', text: 'Нет данных', title: 'Плата ещё не присылала данные' });
});

test('строка под заголовком: сколько плат и сколько из них онлайн', () => {
  assert.equal(Lib.summary([]), 'Плат пока нет');
  assert.equal(Lib.summary([{ online: true }]), '1 плата · онлайн');
  assert.equal(Lib.summary([{ online: false }]), '1 плата · нет связи');
  assert.equal(Lib.summary([{ online: true }, { online: true }]), '2 платы · все онлайн');
  assert.equal(Lib.summary([{ online: true }, { online: false }, { online: false }]), '3 платы · 1 онлайн');
  assert.equal(Lib.summary(Array(5).fill({ online: false })), '5 плат · нет связи');
});

test('токен в одной строке: когда использовался, «используется», отозван', () => {
  const base = { active: true, last_ip: '10.0.0.5', revoked_at: null };
  assert.deepEqual(Lib.tokenUse({ ...base, last_used_at: null }, NOW), { text: 'не использовался', live: false, title: '' });
  assert.deepEqual(Lib.tokenUse({ ...base, last_used_at: NOW - 120 }, NOW),
    { text: '2 мин назад', live: true, title: 'Последний раз с адреса 10.0.0.5' });
  assert.equal(Lib.tokenUse({ ...base, last_used_at: NOW - Lib.IN_USE_SEC }, NOW).live, false, 'после 10 минут тишины токен уже не «используется»');
  assert.deepEqual(Lib.tokenUse({ ...base, active: false, revoked_at: NOW - 7200, last_used_at: NOW - 9000 }, NOW),
    { text: 'отозван 2 ч назад', live: false, title: '' });
  assert.equal(Lib.tokenUse({ ...base, active: false, revoked_at: null }, NOW).text, 'отозван');
});

test('страница плат: мало текста, а опасные действия подтверждаются на самой странице', () => {
  const html = read('static', 'boards.html');
  const script = read('static', 'boards.js');
  const body = html.split('<body>')[1].replace(/<script[\s\S]*?<\/script>/g, '');
  const text = body.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  assert.ok(text.length < 600, 'пояснений на странице без плат не больше 600 знаков, сейчас ' + text.length);
  assert.ok(!/\bconfirm\(|\bprompt\(|\balert\(/.test(script), 'подтверждения и переименование идут не окнами браузера');
  for (const name of ['askRevoke', 'askRemoveToken', 'askDelete', 'startRename']) assert.match(script, new RegExp('function ' + name + '\\('));
  assert.match(script, /remove\.disabled = locked;/, 'плату с действующим токеном удалить нельзя');
  assert.match(script, /no\.focus\(\);/, 'фокус на «Отмене»: случайный Enter ничего не удалит');
  assert.match(script, /document\.querySelector\('\.confirm-bar, \.rename'\)/, 'пока открыто подтверждение, список не перерисовывается');
  assert.ok(!/innerHTML/.test(script), 'названия, имена и токены попадают на страницу только как текст');
});

test('все значки, которые просит страница плат, нарисованы', () => {
  const script = read('static', 'boards.js');
  const drawn = new Set([...script.match(/const ICONS = \{([\s\S]*?)\n  \};/)[1].matchAll(/^ {4}(\w+):/gm)].map((m) => m[1]));
  assert.ok(drawn.size >= 10);
  const asked = [
    ...script.matchAll(/\bicon\('(\w+)'/g),
    ...script.matchAll(/\b(?:tool|textButton|item)\('[^']*',\s*'(\w+)'/g),
    ...script.matchAll(/linkButton\('[^']*',\s*[^,]+,\s*'(\w+)'\)/g),
  ].map((m) => m[1]);
  assert.ok(asked.length >= 12, 'найдены обращения к значкам: ' + asked.length);
  for (const name of asked) assert.ok(drawn.has(name), 'значок «' + name + '» нарисован');
  for (const name of drawn) assert.ok(asked.includes(name), 'значок «' + name + '» нужен');
});

test('карточка ведёт на графики и настройки нужной платы, главная страница понимает адрес с ?device=', () => {
  assert.match(read('static', 'app.js'), /device: new URLSearchParams\(location\.search\)\.get\('device'\) \|\| null/);
  const script = read('static', 'boards.js');
  assert.match(script, /'\/\?device=' \+ encodeURIComponent\(board\.device\)/);
  assert.match(script, /'\/settings\?device=' \+ encodeURIComponent\(board\.device\)/);
});
