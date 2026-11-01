// Страница плат: регистрация платы и её токена, замена и отзыв токенов, переименование и удаление.
// Правила подписей и проверок лежат в boards-lib.js, сервер — в server.py.
(function () {
  'use strict';

  const Lib = window.BoardsLib;
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30000;

  let data = { boards: [], shared_tokens: [] };
  let busy = false;
  let deviceTouched = false; // имя платы вписал владелец: подсказку не перезаписываем

  // Запросы, которые что-то меняют, идут с заголовком X-Dash-Action: чужая страница не может его отправить
  // (сервер не разрешает CORS), поэтому браузер не даст ей зарегистрировать или удалить плату от имени владельца.
  async function api(method, url, body) {
    const headers = { 'X-Dash-Action': '1' };
    const options = { method, headers, cache: 'no-store', credentials: 'same-origin' };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const res = await fetch(url, options);
    let payload = null;
    try { payload = await res.json(); } catch (e) { /* ответ не JSON */ }
    if (res.status === 401) location.assign('/login?next=' + encodeURIComponent('/boards')); // сессия закончилась
    if (!res.ok) throw new Error((payload && payload.error) || 'ответ сервера ' + res.status);
    return payload;
  }

  function showError(text) {
    const el = $('page-error');
    el.textContent = text || '';
    el.hidden = !text;
  }

  function when(ts) {
    return new Date(ts * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
  }

  function text(tag, className, value) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = value;
    return el;
  }

  function button(label, className, handler) {
    const el = text('button', 'btn ' + className, label);
    el.type = 'button';
    el.addEventListener('click', handler);
    return el;
  }

  function tokenRow(item, now, board) {
    const view = Lib.describeToken(item, now);
    const li = document.createElement('li');
    li.className = 'token-row' + (item.active ? '' : ' revoked');
    const main = document.createElement('div');
    main.className = 'token-main';
    const head = document.createElement('div');
    head.className = 'token-head';
    head.append(text('span', 'name', item.name), text('span', 'badge ' + view.status, view.statusText));
    main.append(
      head,
      text('div', 'meta', view.key + ' · создан ' + when(item.created_at) + (item.revoked_at ? ' · отозван ' + when(item.revoked_at) : '')),
      text('div', 'meta', view.used));
    li.append(main);
    const actions = document.createElement('div');
    actions.className = 'token-actions';
    if (view.canRevoke) actions.append(button('Отозвать', 'danger', () => revoke(item, view, board)));
    if (view.canDelete) actions.append(button('Удалить запись', 'ghost', () => removeToken(item)));
    li.append(actions);
    return li;
  }

  function boardCard(board, now, showRevoked) {
    const status = Lib.boardStatus(board, now);
    const li = document.createElement('li');
    li.className = 'board';

    const head = document.createElement('div');
    head.className = 'board-head';
    const title = document.createElement('div');
    title.className = 'board-title';
    title.append(text('span', 'name', board.name), text('span', 'device', board.device));
    head.append(title, text('span', 'badge ' + status.status, status.text));
    li.append(head);
    li.append(text('div', 'meta', 'Зарегистрирована ' + when(board.created_at) + (board.fw ? ' · прошивка ' + board.fw : '')
      + (board.owner ? ' · владелец ' + board.owner : '')));  // владельца знает и показывает только администратору сервер

    const tokens = document.createElement('ul');
    tokens.className = 'token-list';
    board.tokens.filter((item) => showRevoked || item.active).forEach((item) => tokens.append(tokenRow(item, now, board)));
    li.append(tokens);
    if (!Lib.hasActiveToken(board)) {
      li.append(text('p', 'warn-text', 'Нет действующего токена: плата не сможет присылать данные, пока вы не создадите новый (или она использует общий токен из .env).'));
    }

    const actions = document.createElement('div');
    actions.className = 'board-actions';
    actions.append(
      button('Новый токен', '', () => newToken(board)),
      button('Переименовать', 'ghost', () => rename(board)));
    const remove = button('Удалить плату', 'danger', () => removeBoard(board));
    if (Lib.hasActiveToken(board)) {
      remove.disabled = true;
      remove.title = 'Сначала отзовите токены платы';
    }
    actions.append(remove);
    li.append(actions);
    return li;
  }

  function render() {
    const showRevoked = $('show-revoked').checked;
    const now = Math.floor(Date.now() / 1000);
    const list = $('board-list');
    list.textContent = '';
    data.boards.forEach((board) => list.append(boardCard(board, now, showRevoked)));
    $('board-empty').hidden = data.boards.length > 0;

    const shared = data.shared_tokens.filter((item) => showRevoked || item.active);
    const sharedList = $('shared-list');
    sharedList.textContent = '';
    shared.forEach((item) => sharedList.append(tokenRow(item, now, null)));
    $('shared-section').hidden = shared.length === 0;

    if (!deviceTouched && $('new-device') !== document.activeElement) {
      $('new-device').value = Lib.suggestDevice(data.boards.map((board) => board.device));
    }
  }

  async function load() {
    try {
      data = await api('GET', '/api/boards');
      render();
      showError('');
    } catch (e) {
      showError('Не удалось загрузить список: ' + e.message);
    }
  }

  // Одно действие за раз: двойной щелчок не должен отправить запрос дважды
  function run(action) {
    if (busy) return Promise.resolve();
    busy = true;
    showError('');
    return action().catch((e) => showError(e.message)).finally(() => { busy = false; });
  }

  function showCreated(title, device, token) {
    $('created-title').textContent = title;
    $('created-url').textContent = Lib.ingestUrl(location.origin);
    $('created-device').textContent = device;
    $('created-token').textContent = token;
    $('copy-note').textContent = '';
    $('created').hidden = false;
  }

  function hideCreated() {
    $('created-token').textContent = ''; // после закрытия токена на странице не остаётся
    $('created').hidden = true;
    $('copy-note').textContent = '';
  }

  function selectToken() {
    const range = document.createRange();
    range.selectNodeContents($('created-token'));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function newToken(board) {
    run(async () => {
      const made = await api('POST', '/api/boards/' + board.device + '/tokens', {});
      showCreated('Новый токен для платы «' + board.name + '»', board.device, made.token);
      await load();
      $('created').scrollIntoView({ block: 'nearest' });
      selectToken();
    });
  }

  function rename(board) {
    const answer = prompt('Новое название платы', board.name);
    if (answer === null) return;
    const check = Lib.validateName(answer);
    if (!check.ok) { showError(check.error); return; }
    run(async () => { await api('POST', '/api/boards/' + board.device + '/rename', { name: check.name }); await load(); });
  }

  function revoke(item, view, board) {
    const warning = view.inUse ? '\n\nЭтим токеном плата пользовалась только что: после отзыва она перестанет отправлять данные, пока в её настройки не введён другой.' : '';
    const owner = board ? ' платы «' + board.name + '»' : '';
    if (!confirm('Отозвать токен «' + item.name + '»' + owner + '?' + warning)) return;
    run(async () => { await api('POST', '/api/tokens/' + item.id + '/revoke'); await load(); });
  }

  function removeToken(item) {
    if (!confirm('Удалить запись об отозванном токене «' + item.name + '»?')) return;
    run(async () => { await api('DELETE', '/api/tokens/' + item.id); await load(); });
  }

  function removeBoard(board) {
    if (!confirm('Удалить плату «' + board.name + '» (' + board.device + ')?\n\nВместе с ней удалятся все её показания и журнал. Это нельзя отменить.')) return;
    run(async () => { await api('DELETE', '/api/boards/' + board.device); await load(); });
  }

  $('new-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const check = Lib.validateNewBoard($('new-name').value, $('new-device').value);
    if (!check.ok) { showError(check.error); return; }
    run(async () => {
      const made = await api('POST', '/api/boards', { name: check.name, device: check.device });
      showCreated('Плата «' + made.board.name + '» добавлена', made.board.device, made.token);
      $('new-name').value = '';
      deviceTouched = false;
      await load();
      $('created').scrollIntoView({ block: 'nearest' });
      selectToken();
    });
  });

  $('new-device').addEventListener('input', () => { deviceTouched = true; });
  $('copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('created-token').textContent);
      $('copy-note').textContent = 'Скопировано';
    } catch (e) {
      selectToken();
      $('copy-note').textContent = 'Токен выделен: нажмите Ctrl+C';
    }
  });
  $('hide-created').addEventListener('click', hideCreated);
  $('show-revoked').addEventListener('change', render);

  load();
  setInterval(() => { if (!document.hidden && !busy) load(); }, REFRESH_MS);
})();
