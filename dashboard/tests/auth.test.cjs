// Тесты страниц входа, регистрации, аккаунта и администратора: правила проверок (auth-lib.js) и согласованность
// страниц со скриптами и сервером. Запуск: node --test "dashboard/tests/*.test.cjs"
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Lib = require('../static/auth-lib.js');

const read = (...parts) => fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
const NOW = 1_800_000_000;

test('имя: латиница, цифры и _ . - от 3 до 32 символов, зарезервированные имена отклоняются', () => {
  for (const good of ['ivan', 'Ivan_2', 'a.b-c', 'abc', 'x'.repeat(32)]) assert.equal(Lib.validateUsername(good).ok, true, good);
  for (const bad of ['', 'ab', 'x'.repeat(33), 'с кириллицей', 'bad name', 'a\nb', '<b>', null, undefined]) {
    assert.equal(Lib.validateUsername(bad).ok, false, JSON.stringify(bad));
  }
  for (const reserved of ['admin', 'Admin', 'ROOT', 'system', 'support']) {
    const check = Lib.validateUsername(reserved);
    assert.equal(check.ok, false, reserved);
    assert.match(check.error, /зарезервировано/);
  }
});

test('пароль: от 10 до 128 символов, не из одних пробелов и не равен имени', () => {
  assert.equal(Lib.validatePassword('x'.repeat(10)).ok, true);
  assert.equal(Lib.validatePassword('x'.repeat(128)).ok, true);
  assert.equal(Lib.validatePassword(' пароль с пробелами ', 'ivan').ok, true);
  for (const bad of ['x'.repeat(9), 'x'.repeat(129), ' '.repeat(12), '', null, undefined]) {
    assert.equal(Lib.validatePassword(bad).ok, false, JSON.stringify(bad));
  }
  assert.match(Lib.validatePassword('Ivan-Petrov-1', 'ivan-petrov-1').error, /совпадать/);
});

test('регистрация: имя, пароль и повтор пароля проверяются по порядку', () => {
  assert.deepEqual(Lib.validateRegistration('ivan', 'correct horse battery', 'correct horse battery'),
    { ok: true, username: 'ivan', password: 'correct horse battery' });
  assert.match(Lib.validateRegistration('iv', 'correct horse battery', 'correct horse battery').error, /Имя/);
  assert.match(Lib.validateRegistration('ivan', 'short', 'short').error, /Пароль/);
  assert.match(Lib.validateRegistration('ivan', 'correct horse battery', 'correct horse batterY').error, /не совпадают/);
});

test('смена пароля: нужен текущий, новый проверяется так же, повтор совпадает', () => {
  assert.deepEqual(Lib.validatePasswordChange('old-password-1', 'brand new password', 'brand new password', 'ivan'),
    { ok: true, old: 'old-password-1', new: 'brand new password' });
  assert.match(Lib.validatePasswordChange('', 'brand new password', 'brand new password', 'ivan').error, /текущий/);
  assert.match(Lib.validatePasswordChange('old-password-1', 'short', 'short', 'ivan').error, /Пароль/);
  assert.match(Lib.validatePasswordChange('old-password-1', 'brand new password', 'other new password', 'ivan').error, /не совпадают/);
});

test('после входа ведём только на путь этого сайта, чужой адрес заменяется главной', () => {
  for (const good of ['/', '/boards', '/boards?x=1', '/admin', '/account#top']) assert.equal(Lib.safeNext(good), good, good);
  for (const bad of ['https://evil.example/', '//evil.example', '/\\evil.example', 'javascript:alert(1)', 'boards', '', null, undefined,
    '/a\nb', '/a\u0000b', '/' + 'x'.repeat(400), '/login', '/login?next=/', '/register', '/login.html']) {
    assert.equal(Lib.safeNext(bad), '/', JSON.stringify(bad));
  }
});

test('«вход: …» и склонение платы', () => {
  assert.equal(Lib.agoText(null, NOW), 'никогда');
  assert.equal(Lib.agoText(NOW - 30, NOW), 'только что');
  assert.equal(Lib.agoText(NOW - 600, NOW), '10 мин назад');
  assert.equal(Lib.agoText(NOW - 7200, NOW), '2 ч назад');
  assert.equal(Lib.agoText(NOW - 3 * 86400, NOW), '3 дн назад');
  const forms = [0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25].map((n) => n + ' ' + Lib.plural(n, 'плата', 'платы', 'плат'));
  assert.deepEqual(forms, ['0 плат', '1 плата', '2 платы', '4 платы', '5 плат', '11 плат', '12 плат', '14 плат', '21 плата', '22 платы', '25 плат']);
});

test('строка человека на странице администратора: статусы и доступные действия', () => {
  const base = { id: 2, username: 'ivan', role: 'user', created_at: NOW - 86400, last_login_at: NOW - 120, disabled: false, boards: 1 };
  const active = Lib.describeUser(base, NOW);
  assert.deepEqual([active.status, active.statusText, active.lastLogin, active.boards], ['active', 'Активен', 'Вход: 2 мин назад', '1 плата']);
  assert.deepEqual([active.canBlock, active.canUnblock, active.canReset, active.canDelete], [true, false, true, true]);
  const blocked = Lib.describeUser({ ...base, disabled: true, last_login_at: null, boards: 3 }, NOW);
  assert.deepEqual([blocked.status, blocked.statusText, blocked.lastLogin, blocked.boards], ['blocked', 'Заблокирован', 'Вход: никогда', '3 платы']);
  assert.deepEqual([blocked.canBlock, blocked.canUnblock], [false, true]);
  const admin = Lib.describeUser({ ...base, role: 'admin' }, NOW);
  assert.deepEqual([admin.status, admin.statusText, admin.canBlock, admin.canUnblock, admin.canReset, admin.canDelete],
    ['admin', 'Администратор', false, false, false, false]);
});

test('правила имени и пароля совпадают с сервером', () => {
  const server = read('server.py');
  const usernameRe = server.match(/USERNAME_RE = re\.compile\(r"([^"]+)"\)/)[1];
  assert.equal(usernameRe, Lib.USERNAME_RE.source.replace(/^\^|\$$/g, ''));
  assert.match(server, new RegExp('PASSWORD_MIN, PASSWORD_MAX = ' + Lib.PASSWORD_MIN + ', ' + Lib.PASSWORD_MAX + '\\b'));
  const reserved = [...server.match(/RESERVED_USERNAMES = frozenset\(\{([^}]+)\}\)/)[1].matchAll(/"(\w+)"/g)].map((m) => m[1]).sort();
  for (const name of reserved) assert.equal(Lib.validateUsername(name).ok, false, 'на странице зарезервировано: ' + name);
  assert.ok(reserved.length >= 5);
});

const PAGES = {
  'login': ['login.html', 'login.js'],
  'register': ['register.html', 'register.js'],
  'account': ['account.html', 'account.js'],
  'admin': ['admin.html', 'admin.js'],
};

test('все элементы, которые ищут скрипты страниц, есть в их разметке, без встроенного кода', () => {
  for (const [name, [htmlFile, jsFile]] of Object.entries(PAGES)) {
    const html = read('static', htmlFile);
    const script = read('static', jsFile);
    const ids = new Set([...script.matchAll(/\$\('([\w-]+)'\)/g)].map((m) => m[1]));
    assert.ok(ids.size >= 4, name + ': найдены обращения к элементам');
    for (const id of ids) assert.equal(html.split('id="' + id + '"').length - 1, 1, name + ': элемент #' + id + ' есть ровно один раз');
    assert.ok(!/<script>/.test(html) && !/ style=/.test(html), name + ': CSP запрещает встроенные скрипты и стили');
    assert.deepEqual([...html.matchAll(/<script src="([^"]+)">/g)].map((m) => m[1]), ['/auth-lib.js', '/' + jsFile],
      name + ': библиотека подключена до скрипта страницы');
  }
});

test('сервер отдаёт страницы и скрипты входа открыто, остальные страницы только после входа', () => {
  const server = read('server.py');
  const publicPages = server.match(/PUBLIC_PAGES = \{([\s\S]*?)\}/)[1];
  const pages = server.match(/\nPAGES = \{([\s\S]*?)\}/)[1];
  const adminPages = server.match(/ADMIN_PAGES = \{([^}]*)\}/)[1];
  const assets = server.match(/ASSETS = \{([\s\S]*?)\n\}/)[1];
  assert.ok(publicPages.includes('"/login": "login.html"') && publicPages.includes('"/register": "register.html"'));
  assert.ok(pages.includes('"/account": "account.html"') && pages.includes('"/boards": "boards.html"') && pages.includes('"/": "index.html"'));
  assert.ok(adminPages.includes('"/admin": "admin.html"'));
  for (const file of ['auth-lib.js', 'login.js', 'register.js', 'account.js', 'admin.js', 'style.css', 'boards.js', 'boards-lib.js']) {
    assert.ok(assets.includes('"/' + file + '": ("' + file + '"'), file + ' раздаётся открыто');
  }
  for (const file of fs.readdirSync(path.join(__dirname, '..', 'static'))) {
    assert.ok(server.includes('"' + file + '"'), file + ' из static/ есть в server.py');
  }
});

test('каждый запрос, который что-то меняет, идёт с заголовком страницы и не прячет токен или пароль в хранилище', () => {
  for (const file of ['login.js', 'register.js', 'account.js', 'admin.js', 'boards.js']) {
    const script = read('static', file);
    assert.match(script, /'X-Dash-Action': '1'/, file);
    assert.ok(!/localStorage|sessionStorage/.test(script), file + ': пароли и токены нигде не сохраняются');
  }
  assert.match(read('static', 'login.js'), /Lib\.safeNext\(/, 'после входа адрес возврата проверяется');
  assert.match(read('static', 'register.js'), /Lib\.validateRegistration\(/);
  assert.match(read('static', 'account.js'), /Lib\.validatePasswordChange\(/);
  assert.match(read('static', 'admin.js'), /\$\('reset-password'\)\.textContent = ''/, 'при закрытии окна новый пароль стирается со страницы');
});

test('страница графиков и страница плат уходят на вход, когда сессия закончилась', () => {
  const app = read('static', 'app.js');
  assert.match(app, /res\.status === 401[\s\S]{0,200}location\.assign\('\/login\?next='/);
  assert.match(read('static', 'boards.js'), /res\.status === 401\) location\.assign\('\/login\?next='/);
  const html = read('static', 'index.html');
  for (const id of ['account', 'people', 'start-hint']) assert.equal(html.split('id="' + id + '"').length - 1, 1, '#' + id);
  assert.match(html, /id="people" href="\/admin"[^>]*\bhidden\b/, 'кнопка «Люди» скрыта, пока сервер не подтвердит администратора');
  assert.match(app, /me\.user\.role !== 'admin'/);
});
