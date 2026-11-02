'use strict';
// Подвал со ссылками и страницы политики и условий. Подвал один и тот же на каждой странице, его ссылки ведут на существующие
// страницы и разделы, а правовые тексты не содержат скриптов и подставляют только те поля, которые знает сервер (server.py: legal_text).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = (...parts) => fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
const PAGES = fs.readdirSync(path.join(__dirname, '..', 'static')).filter((name) => name.endsWith('.html')).sort();
const LEGAL = ['privacy.html', 'terms.html'];
const FOOTER_RE = /<footer class="site-foot">[\s\S]*?<\/footer>/g;
const LINKS = [
  ['/privacy', 'Политика конфиденциальности'],
  ['/terms', 'Условия использования'],
  ['/privacy#cookies', 'Файлы cookie'],
  ['/account#delete', 'Удалить данные и аккаунт'],
  ['/privacy#contact', 'Контакты'],
];
const FIELDS = ['OPERATOR', 'CONTACT', 'RETENTION_DAYS', 'LOG_RETENTION_DAYS', 'SESSION_DAYS', 'SITE'];
const count = (text, part) => text.split(part).length - 1;

test('подвал: на каждой странице ровно один, с теми же пятью ссылками в том же порядке', () => {
  assert.ok(PAGES.length >= 12, 'страницы найдены: ' + PAGES.join(', '));
  for (const name of ['index.html', 'boards.html', 'account.html', 'settings.html', 'admin.html', 'login.html', 'register.html',
    'forgot.html', 'reset.html', 'verify.html', 'privacy.html', 'terms.html']) {
    assert.ok(PAGES.includes(name), name + ' есть среди страниц');
  }
  const same = new Set();
  for (const name of PAGES) {
    const footers = read('static', name).match(FOOTER_RE) || [];
    assert.equal(footers.length, 1, name + ': подвал со ссылками один');
    const links = [...footers[0].matchAll(/<a href="([^"]+)"[^>]*>([^<]+)<\/a>/g)].map((m) => [m[1], m[2]]);
    assert.deepEqual(links, LINKS, name + ': ссылки подвала');
    same.add(footers[0].replace(/^\s+/gm, ''));
    assert.match(footers[0], /<nav aria-label="Правовая информация и данные">/, name + ': ссылки подвала собраны в навигацию с подписью');
  }
  assert.equal(same.size, 1, 'разметка подвала одинакова на всех страницах (отличаться может только отступ)');
});

test('подвал стоит последним внутри страницы: после всего содержимого и до скриптов', () => {
  for (const name of PAGES) {
    const html = read('static', name);
    assert.match(html, /<\/footer>\s*<\/div>\s*(?:<script src=|<\/body>)/, name + ': за подвалом закрывается обёртка страницы');
    assert.equal((html.match(/<div\b/g) || []).length, (html.match(/<\/div>/g) || []).length, name + ': теги div закрыты');
    // (в настройках есть и свой короткий подвал с пояснением; подвал со ссылками идёт после него)
    assert.equal(html.lastIndexOf('<footer'), html.indexOf('<footer class="site-foot">'), name + ': подвал со ссылками последний');
  }
});

test('подвал ведёт только внутрь сайта, а об исходном коде и репозитории на страницах не говорится', () => {
  for (const name of PAGES) {
    const html = read('static', name);
    const footer = html.match(FOOTER_RE)[0];
    for (const [, href, rest] of footer.matchAll(/<a href="([^"]+)"([^>]*)>/g)) {
      assert.ok(href.startsWith('/') && !href.startsWith('//') && !/rel=|target=/.test(rest), name + ': ' + href + ' внутри сайта, в той же вкладке');
    }
    // владелец просил не упоминать исходный код: ни ссылки в подвале, ни фразы в политике
    assert.ok(!/github\.com|[Ии]сходный код|[Рр]епозитори/.test(html), name + ': нет упоминаний об исходном коде');
  }
});

test('ссылки подвала ведут на существующие страницы и на якоря, которые есть в разметке', () => {
  const server = read('server.py');
  const legal = server.match(/LEGAL_PAGES = \{([^}]*)\}/)[1];
  assert.ok(legal.includes('"/privacy": "privacy.html"') && legal.includes('"/terms": "terms.html"'));
  const pages = server.match(/\nPAGES = \{([\s\S]*?)\}/)[1];
  assert.ok(pages.includes('"/account": "account.html"'));
  const privacy = read('static', 'privacy.html');
  for (const anchor of ['cookies', 'contact']) assert.equal(count(privacy, ' id="' + anchor + '"'), 1, 'якорь #' + anchor + ' в политике');
  assert.equal(count(read('static', 'account.html'), ' id="delete"'), 1, 'якорь #delete на странице аккаунта');
  // каждый внутренний якорь политики и условий действительно есть на нужной странице
  for (const name of LEGAL) {
    const html = read('static', name);
    const own = name === 'terms.html' ? read('static', 'terms.html') : privacy;
    const links = [...html.matchAll(/href="(\/[a-z]*)?#([\w-]+)"/g)];
    assert.ok(links.length >= 3, name + ': внутренние ссылки найдены');
    for (const [, page, anchor] of links) {
      const target = page === '/account' ? read('static', 'account.html') : page === '/terms' ? read('static', 'terms.html')
        : page === '/privacy' ? privacy : page === undefined ? own : null;
      assert.ok(target, name + ': страница ' + page + ' известна');
      assert.equal(count(target, ' id="' + anchor + '"'), 1, name + ': якорь ' + (page || '') + '#' + anchor);
    }
  }
  // тот же набор разделов, на который ссылается оглавление политики
  const ids = [...privacy.matchAll(/ id="([\w-]+)"/g)].map((m) => m[1]);
  for (const id of ['operator', 'data', 'cookies', 'recipients', 'rights', 'after', 'security', 'children', 'changes', 'contact']) {
    assert.ok(ids.includes(id), 'раздел #' + id);
  }
  assert.equal(new Set(ids).size, ids.length, 'в политике нет двух одинаковых якорей');
});

test('политика и условия: без скриптов и встроенных стилей, подставляются только известные поля, оператор и контакт названы', () => {
  const server = read('server.py');
  for (const field of FIELDS) assert.ok(server.includes('"' + field + '"'), 'сервер знает поле ' + field);
  for (const name of LEGAL) {
    const html = read('static', name);
    assert.ok(!/<script|<style| style=|\son[a-z]+=|javascript:/i.test(html), name + ': CSP запрещает встроенный код, а ему здесь и нечего делать');
    assert.ok(!/<link rel="stylesheet" href="https?:|<img |<iframe|src="https?:/i.test(html), name + ': ничего не загружается с чужих адресов');
    const used = new Set([...html.matchAll(/\{\{([A-Z_]+)\}\}/g)].map((m) => m[1]));
    for (const field of used) assert.ok(FIELDS.includes(field), name + ': неизвестное поле {{' + field + '}}');
    assert.ok(used.has('OPERATOR') && used.has('CONTACT'), name + ': названы оператор и контакт');
    assert.match(html, /<html lang="ru">/);
    assert.match(html, /<meta name="viewport" content="width=device-width, initial-scale=1">/);
    assert.equal(count(html, '<h1'), 1, name + ': один заголовок первого уровня');
    assert.match(html, /<link rel="stylesheet" href="\/style\.css">/);
    assert.ok(!/\{\{|\}\}/.test(html.replace(/\{\{[A-Z_]+\}\}/g, '')), name + ': нет оборванных скобок подстановки');
  }
});

test('политика называет то, что страницы действительно сохраняют в браузере, и ссылается на удаление', () => {
  const privacy = read('static', 'privacy.html');
  const server = read('server.py');
  assert.match(server, /SESSION_COOKIE = "dash_session"/);
  assert.match(server, /OAUTH_COOKIE = "dash_oauth"/);
  assert.ok(privacy.includes('dash_session') && privacy.includes('dash_oauth'), 'оба cookie названы');
  // всё, что скрипты кладут в localStorage, названо в политике: ключи собираются как 'dash.' + имя
  const keys = new Set();
  for (const file of fs.readdirSync(path.join(__dirname, '..', 'static')).filter((name) => name.endsWith('.js'))) {
    const script = read('static', file);
    if (/sessionStorage|indexedDB|document\.cookie/.test(script)) assert.fail(file + ': хранение, о котором политика молчит');
    for (const [, key] of script.matchAll(/remember\('([\w-]+)'/g)) keys.add('dash.' + key);
  }
  assert.deepEqual([...keys].sort(), ['dash.level', 'dash.range']);
  for (const key of keys) assert.ok(privacy.includes('<code>' + key + '</code>'), 'в политике назван ключ ' + key);
  assert.ok(privacy.includes('href="/account#delete"'), 'политика ведёт к удалению данных и записи');
  assert.ok(privacy.includes('dvi.gov.lv'), 'названа организация, куда можно пожаловаться');
  // сторонние сервисы названы по именам: их не подключают незаметно
  assert.ok(privacy.includes('Resend') && privacy.includes('Google'));
  assert.ok(!/google-analytics|googletagmanager|fonts\.googleapis|gstatic\.com|cdn\./i.test(
    fs.readdirSync(path.join(__dirname, '..', 'static')).map((name) => read('static', name)).join('\n')), 'страницы не подключают чужие счётчики, шрифты и скрипты');
});

test('стили подвала, правовых страниц и подтверждения удаления есть, а атрибут hidden их не перебивается', () => {
  const css = read('static', 'style.css');
  for (const rule of [/\.site-foot \{/, /\.site-foot nav \{/, /\.legal-wrap \{/, /\.legal \{/, /\.legal h2 \{/, /\.legal table \{/, /\.table-wrap \{/,
    /\.todo \{/, /\.erase \{/, /\.erase\.danger \{/, /\.erase-body \{/, /\.confirm-bar \{/]) {
    assert.match(css, rule, String(rule));
  }
  // у блока задан display, поэтому hidden сам его не скроет: без этих правил подтверждение висело бы на странице всегда
  assert.match(css, /\.erase\[hidden\], \.erase-body\[hidden\], \.confirm-bar\[hidden\] \{ display: none; \}/);
  assert.match(css, /\.confirm-bar \{[^}]*display: flex/);
  assert.match(css, /\.erase-body \{ display: grid; gap: 10px; \}/, 'у блока удаления тот же отступ между абзацем и формой, что у блока стирания данных');
  assert.match(css, /\.label \.typed-name \{[^}]*text-transform: none/, 'имя для подтверждения показано как написано, а не заглавными: так его и набирают');
  const account = read('static', 'account.html');
  assert.match(account, /<div id="delete-box" class="erase-body">/);
  assert.match(account, /<span id="delete-name" class="typed-name"><\/span>/);
  const order = (re) => css.search(re);
  assert.ok(order(/\.confirm-bar \{/) < order(/\.confirm-bar\[hidden\]/), 'правило hidden стоит после правила display');
  for (const name of LEGAL) assert.match(read('static', name), /<div class="wrap legal-wrap">/, name + ': обёртка читаемой ширины');
  // таблица данных на узком экране прокручивается внутри своей рамки: рамка доступна с клавиатуры и названа для чтения с экрана
  assert.match(read('static', 'privacy.html'), /<div class="table-wrap" role="region" aria-label="[^"]+" tabindex="0">/);
  assert.match(css, /\.table-wrap:focus-visible \{ outline: 2px solid var\(--accent\)/);
});

test('сервер отдаёт политику и условия всем и не кэширует их без проверки', () => {
  const server = read('server.py');
  const route = server.match(/if path in LEGAL_PAGES and reading:[\s\S]{0,260}/)[0];
  assert.match(route, /legal_text\(self\.server\.cfg, LEGAL_PAGES\[path\]\)/);
  assert.match(route, /"Cache-Control": "no-cache"/, 'изменение DASH_OPERATOR_NAME подхватывается без очистки кэша браузера');
  // маршрут стоит до проверки входа: прочитать условия можно, не регистрируясь
  assert.ok(server.indexOf('if path in LEGAL_PAGES and reading:') < server.indexOf('who = self._principal()\n            except (AuthFailed, RateLimited):'),
    'правовые страницы отдаются до проверки входа: устаревший вход в браузере не уводит человека со страницы условий');
  // значения настроек вставляются с экранированием
  assert.match(server, /html\.escape\(cfg\.operator_name\)/);
  assert.match(server, /html\.escape\(cfg\.public_url or "адрес этого сайта"\)/);
  for (const name of LEGAL) assert.ok(server.includes('"' + name + '"'), name + ' перечислена в server.py');
});
