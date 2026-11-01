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

test('почта: латиница, регистр не важен, лишние пробелы по краям отбрасываются', () => {
  assert.deepEqual(Lib.validateEmail('  Ivan.P+garden@Example.COM '), { ok: true, email: 'ivan.p+garden@example.com' });
  for (const good of ['a@b.co', 'x'.repeat(64) + '@example.com', 'a.b-c_d%e@sub.example.org']) assert.equal(Lib.validateEmail(good).ok, true, good);
  for (const bad of ['', 'ivan', 'ivan@', '@example.com', 'ivan@example', 'ivan@@example.com', 'iv an@example.com', 'ivan@exa_mple.com',
    '.ivan@example.com', 'ivan.@example.com', 'iv..an@example.com', 'ivan@example..com', 'ivan@-example.com', 'иван@example.com',
    'ivan@пример.рф', 'ivan@example.com\r\nBcc: x@example.org', '<ivan@example.com>', 'x'.repeat(65) + '@example.com', null, undefined]) {
    const check = Lib.validateEmail(bad);
    assert.equal(check.ok, false, JSON.stringify(bad));
    assert.match(check.error, /Почта/);
  }
});

test('регистрация с почтой: адрес проверяется после имени и до пароля, без адреса форма работает как раньше', () => {
  assert.deepEqual(Lib.validateRegistration('ivan', 'correct horse battery', 'correct horse battery', ' Ivan@Example.com '),
    { ok: true, username: 'ivan', password: 'correct horse battery', email: 'ivan@example.com' });
  assert.match(Lib.validateRegistration('iv', 'correct horse battery', 'correct horse battery', 'bad').error, /Имя/);
  assert.match(Lib.validateRegistration('ivan', 'short', 'short', 'bad').error, /Почта/);
  assert.match(Lib.validateRegistration('ivan', 'short', 'short', 'ivan@example.com').error, /Пароль/);
  assert.match(Lib.validateRegistration('ivan', 'correct horse battery', 'correct horse batterY', 'ivan@example.com').error, /не совпадают/);
  assert.equal('email' in Lib.validateRegistration('ivan', 'correct horse battery', 'correct horse battery'), false);
});

test('новый пароль по ссылке из письма и смена почты в аккаунте', () => {
  assert.deepEqual(Lib.validateNewPassword('brand new password', 'brand new password'), { ok: true, password: 'brand new password' });
  assert.match(Lib.validateNewPassword('short', 'short').error, /Пароль/);
  assert.match(Lib.validateNewPassword('brand new password', 'other new password').error, /не совпадают/);
  assert.deepEqual(Lib.validateEmailChange('New@Example.com', 'secret-password-1'), { ok: true, email: 'new@example.com', password: 'secret-password-1' });
  assert.match(Lib.validateEmailChange('bad', 'secret-password-1').error, /Почта/);
  assert.match(Lib.validateEmailChange('new@example.com', '').error, /пароль/);
});

test('ссылка из письма: значение берётся только из «#token=…» нужной длины и алфавита', () => {
  const token = 'abcdefghij0123456789_-ABCDEFGHIJ';
  assert.equal(Lib.tokenFromHash('#token=' + token), token);
  for (const bad of ['', '#', '#token=', '#token=short', '#token=' + 'x'.repeat(65), '#token=' + token + '&x=1', '?token=' + token,
    '#token=' + token.slice(0, 20) + '<script>', 'token=' + token, null, undefined]) {
    assert.equal(Lib.tokenFromHash(bad), '', JSON.stringify(bad));
  }
});

test('подпись про почту на странице «Аккаунт»', () => {
  const none = Lib.describeEmail({ address: null, verified: false, pending: null });
  assert.deepEqual([none.status, none.text, none.canResend], ['none', 'Почта не указана', false]);
  assert.match(none.note, /Укажите адрес/);
  const waiting = Lib.describeEmail({ address: 'ivan@example.com', verified: false, pending: 'ivan@example.com' });
  assert.deepEqual([waiting.status, waiting.text, waiting.canResend], ['wait', 'ivan@example.com · не подтверждена', true]);
  assert.match(waiting.note, /добавлять платы нельзя/);
  const ok = Lib.describeEmail({ address: 'ivan@example.com', verified: true, pending: null });
  assert.deepEqual([ok.status, ok.text, ok.note, ok.canResend], ['ok', 'ivan@example.com · подтверждена', '', false]);
  const change = Lib.describeEmail({ address: 'old@example.com', verified: true, pending: 'new@example.com' });
  assert.deepEqual([change.status, change.text, change.canResend], ['ok', 'old@example.com · подтверждена', true]);
  assert.match(change.note, /new@example\.com/);
  assert.equal(Lib.describeEmail(null).status, 'none');
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
    '/a\nb', '/a\u0000b', '/' + 'x'.repeat(400), '/login', '/login?next=/', '/register', '/login.html', '/forgot', '/forgot.html']) {
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
  assert.equal(active.email, 'Почта не указана');
  assert.equal(Lib.describeUser({ ...base, email: 'ivan@example.com', email_verified: true }, NOW).email, 'Почта: ivan@example.com (подтверждена)');
  assert.equal(Lib.describeUser({ ...base, email: 'ivan@example.com', email_verified: false }, NOW).email, 'Почта: ivan@example.com (не подтверждена)');
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

test('правила почты и ссылки из письма совпадают с сервером', () => {
  const server = read('server.py');
  const emailRe = server.match(/EMAIL_RE = re\.compile\(r"([^"]+)"\)/)[1];
  assert.equal(emailRe, Lib.EMAIL_RE.source.replace(/^\^|\$$/g, ''));
  assert.match(server, new RegExp('EMAIL_MAX = ' + Lib.EMAIL_MAX + '\\b'));
  // страница читает ссылку из «#token=…» тем же алфавитом и длиной, какие сервер принимает (MAIL_TOKEN_RE)
  const tokenRe = server.match(/MAIL_TOKEN_RE = re\.compile\(r"([^"]+)"\)/)[1];
  assert.equal(tokenRe, '[A-Za-z0-9_-]{20,64}');
  assert.equal(Lib.tokenFromHash('#token=' + 'a'.repeat(20)), 'a'.repeat(20));
  assert.equal(Lib.tokenFromHash('#token=' + 'a'.repeat(19)), '');
  // ссылка в письме: адрес сайта, путь страницы и «#token=»: именно так её разбирает страница
  assert.match(server, /"%s%s#token=%s" % \(self\.cfg\.public_url, MAIL_PATHS\[kind\], token\)/);
  assert.match(server, /MAIL_PATHS = \{"email": "\/verify", "reset": "\/reset"\}/);
});

const PAGES = {
  'login': ['login.html', 'login.js'],
  'register': ['register.html', 'register.js'],
  'forgot': ['forgot.html', 'forgot.js'],
  'reset': ['reset.html', 'reset.js'],
  'verify': ['verify.html', 'verify.js'],
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
  assert.ok(publicPages.includes('"/forgot": "forgot.html"'), '«Забыли пароль» открыта без входа');
  // страницы по ссылкам из писем открываются и вошедшему: без перенаправления на главную ссылка бы пропала
  const openPages = server.match(/OPEN_PAGES = \{([^}]*)\}/)[1];
  assert.ok(openPages.includes('"/verify": "verify.html"') && openPages.includes('"/reset": "reset.html"'));
  assert.ok(!publicPages.includes('verify') && !publicPages.includes('reset'), 'подтверждение и сброс не попали в страницы с перенаправлением');
  assert.ok(pages.includes('"/account": "account.html"') && pages.includes('"/boards": "boards.html"') && pages.includes('"/": "index.html"'));
  assert.ok(adminPages.includes('"/admin": "admin.html"'));
  for (const file of ['auth-lib.js', 'login.js', 'register.js', 'forgot.js', 'reset.js', 'verify.js', 'account.js', 'admin.js', 'style.css',
    'boards.js', 'boards-lib.js']) {
    assert.ok(assets.includes('"/' + file + '": ("' + file + '"'), file + ' раздаётся открыто');
  }
  for (const file of fs.readdirSync(path.join(__dirname, '..', 'static'))) {
    assert.ok(server.includes('"' + file + '"'), file + ' из static/ есть в server.py');
  }
});

test('каждый запрос, который что-то меняет, идёт с заголовком страницы и не прячет токен или пароль в хранилище', () => {
  for (const file of ['login.js', 'register.js', 'forgot.js', 'reset.js', 'verify.js', 'account.js', 'admin.js', 'boards.js']) {
    const script = read('static', file);
    assert.match(script, /'X-Dash-Action': '1'/, file);
    assert.ok(!/localStorage|sessionStorage/.test(script), file + ': пароли и токены нигде не сохраняются');
  }
  assert.match(read('static', 'login.js'), /Lib\.safeNext\(/, 'после входа адрес возврата проверяется');
  assert.match(read('static', 'register.js'), /Lib\.validateRegistration\(/);
  assert.match(read('static', 'account.js'), /Lib\.validatePasswordChange\(/);
  assert.match(read('static', 'admin.js'), /\$\('reset-password'\)\.textContent = ''/, 'при закрытии окна новый пароль стирается со страницы');
});

test('страницы по ссылкам из писем: ссылка берётся из «#», сразу убирается из адреса, подтверждает только нажатие кнопки', () => {
  for (const file of ['reset.js', 'verify.js']) {
    const script = read('static', file);
    assert.match(script, /Lib\.tokenFromHash\(location\.hash\)/, file + ': ссылка читается только из части после «#»');
    assert.match(script, /history\.replaceState\(null, '', location\.pathname\)/, file + ': ссылка не остаётся в адресной строке и истории');
    assert.ok(!/location\.search|document\.referrer/.test(script), file + ': ссылка не берётся из запроса, который попал бы в журнал сервера');
  }
  const verify = read('static', 'verify.js');
  const post = verify.indexOf("fetch('/api/verify'");
  const click = verify.indexOf("$('confirm').addEventListener('click'");
  assert.ok(click > 0 && post > click, 'запрос на подтверждение уходит только из обработчика нажатия: сама загрузка страницы адрес не подтверждает');
  const reset = read('static', 'reset.js');
  assert.ok(reset.indexOf("'/api/reset/check'") > 0 && reset.indexOf("post('/api/reset', {") > 0, 'ссылка проверяется до показа формы');
  assert.match(reset, /Lib\.validateNewPassword\(/);
  assert.match(read('static', 'forgot.js'), /Lib\.validateEmail\(/);
  assert.match(read('static', 'forgot.html'), /Ссылка придёт на адрес, указанный в записи/);
  // страница не должна подсказывать, есть ли адрес в системе: ответ на «Забыли пароль» один и тот же
  assert.ok(!/не найден|нет такого|не зарегистрирован/i.test(read('static', 'forgot.js') + read('static', 'forgot.html')));
});

test('вход, регистрация, аккаунт и платы знают про почту', () => {
  const register = read('static', 'register.js');
  assert.match(register, /mailRequired = !!data\.mail;/, 'поле почты показывается, только когда сервер её настроил');
  assert.match(register, /mailRequired \? \$\('email'\)\.value : undefined/, 'без почты на сервере адрес не отправляется и не проверяется');
  assert.match(read('static', 'login.js'), /\$\('forgot-note'\)\.hidden = !data\.mail;/, '«Забыли пароль?» только при настроенной почте');
  assert.match(read('static', 'login.html'), /<span class="label">Имя или почта<\/span>/);
  assert.match(read('static', 'account.js'), /api\('POST', '\/api\/account\/email'/);
  assert.match(read('static', 'account.js'), /api\('POST', '\/api\/account\/email\/resend'/);
  const boards = read('static', 'boards.js');
  assert.match(boards, /answer\.mail_enabled && answer\.user\.role !== 'admin'/, 'предупреждение не показывается администратору и без почты');
  assert.match(read('static', 'boards.html'), /id="verify-banner" class="notice" role="status" hidden/);
  assert.match(read('static', 'style.css'), /\.notice\[hidden\] \{ display: none; \}/, 'правило display не должно перебивать hidden');
  const css = read('static', 'style.css');
  assert.match(css, /\.field\[hidden\]/, 'поле формы с hidden действительно прячется');
  // У кнопки задан display, поэтому атрибут hidden сам её не скроет: «Подтвердить почту» оставалась на странице после подтверждения,
  // а «Отправить письмо ещё раз» у человека с подтверждённым адресом
  assert.match(css, /\.btn \{[^}]*display: inline-block/);
  assert.match(css, /\.btn\[hidden\][^{]*\{ display: none; \}/);
  const order = (re) => css.search(re);
  assert.ok(order(/\.btn\.with-icon \{ display: inline-flex/) < order(/\.btn\[hidden\]/), 'правило hidden стоит после правил display у кнопок');
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

test('главная без данных: остаются шапка и подсказка, а дашборд (показания, графики, журнал) спрятан', () => {
  const html = read('static', 'index.html');
  const app = read('static', 'app.js');
  const at = (id) => html.indexOf('id="' + id + '"');
  const start = html.indexOf('<div id="dash">');
  const end = html.indexOf('<script src="/alerts.js">');
  assert.ok(start > 0 && end > start, 'дашборд обёрнут в #dash');
  assert.equal((html.match(/<div\b/g) || []).length, (html.match(/<\/div>/g) || []).length, 'теги div закрыты');
  for (const id of ['range-group', 'csv', 'board-settings', 'cards', 'chart-climate', 'chart-light', 'chart-power', 'log-section', 'level-group', 'more']) {
    assert.ok(at(id) > start && at(id) < end, '#' + id + ' лежит внутри дашборда');
  }
  for (const id of ['start-hint', 'pill', 'bell', 'account', 'device', 'alerts-panel']) {
    assert.ok(at(id) > 0 && at(id) < start, '#' + id + ' остаётся вне дашборда');
  }
  assert.match(html, /<footer class="foot">[^<]*<\/footer>\s*<\/div>\s*<\/div>/, 'подвал тоже внутри дашборда');
  assert.match(html, /<p id="start-hint" class="start-hint" hidden>Здесь появятся показания вашей платы\. <a href="\/boards">Добавьте плату<\/a>, введите выданные адрес, имя и токен в её настройках \(вкладка «WiFi», раздел «Телеметрия»\), и через минуту она появится на графиках\.<\/p>/);
  assert.match(app, /\$\('start-hint'\)\.hidden = !!st;\s*\$\('dash'\)\.hidden = !st;/, 'подсказка и дашборд переключаются вместе');
  // Дашборд открывается раньше, чем на нём рисуются графики: у скрытого блока нулевая ширина
  assert.match(app, /state\.status = status;\s*state\.fetchedAt = Date\.now\(\);\s*renderStatus\(\);\s*csvLink\(\);\s*if \(series\) for \(const key in CHARTS\) drawChart/);
});

test('число оповещений на колокольчике прячется атрибутом hidden: правило display не должно его перебивать', () => {
  const css = read('static', 'style.css');
  assert.match(css, /\.bell-count \{[^}]*display: inline-flex/, 'у значка задан display, поэтому атрибут hidden сам его не скроет');
  assert.match(css, /\.bell-count\[hidden\] \{ display: none; \}/, 'без этого правила при нуле оповещений на колокольчике висел красный «0»');
  assert.match(read('static', 'app.js'), /badge\.hidden = count === 0;/);
});
