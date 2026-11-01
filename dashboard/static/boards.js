// Страница плат: карточки плат с токенами, добавление платы и показ её токена, замена и отзыв токенов,
// переименование и удаление. Правила подписей и проверок лежат в boards-lib.js, сервер — в server.py.
(function () {
  'use strict';

  const Lib = window.BoardsLib;
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30000;
  const SVG_NS = 'http://www.w3.org/2000/svg';

  // Значки в одном стиле со значками шапки панели: контурные, 24×24
  const ICONS = {
    plus: ['M12 5v14', 'M5 12h14'],
    close: ['M6 6l12 12', 'M18 6L6 18'],
    check: ['M5 12.5l4.5 4.5L19 7'],
    copy: ['M9 9h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1z', 'M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1'],
    dots: ['M5 12h.01', 'M12 12h.01', 'M19 12h.01'],
    key: ['M12 15a4 4 0 1 1-8 0 4 4 0 0 1 8 0z', 'M10.8 12.2L20 3', 'M16 7l3 3', 'M13.5 9.5l2 2'],
    sliders: ['M4 7h9', 'M17 7h3', 'M4 17h3', 'M11 17h9', 'M17 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0z', 'M11 17a2 2 0 1 1-4 0 2 2 0 0 1 4 0z'],
    chart: ['M4 4v16h16', 'M8 15l3.5-4.5 3 2.5L20 7'],
    user: ['M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2', 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z'],
    pencil: ['M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19z'],
    trash: ['M4 7h16', 'M10 11v6', 'M14 11v6', 'M6 7l1 13h10l1-13', 'M9 7V4h6v3'],
    ban: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M5.6 5.6l12.8 12.8'],
  };

  let data = { boards: [], shared_tokens: [] };
  let me = null;             // кто вошёл: у администратора на чужих платах стоит имя владельца
  let busy = false;
  let deviceTouched = false; // имя платы вписал владелец: подсказку не перезаписываем
  let autoOpened = false;    // у человека без плат форма добавления открывается сама, но один раз

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

  function showError(message) {
    const node = $('page-error');
    node.textContent = message || '';
    node.hidden = !message;
  }

  // ----- мелкие кирпичики --------------------------------------------------------------------
  // Дети — узлы или строки; строки попадают на страницу как текст, а не как разметка
  function el(tag, className, ...children) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    for (const child of children) if (child != null) node.append(child);
    return node;
  }

  function icon(name, size) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    const attrs = { viewBox: '0 0 24 24', width: size || 18, height: size || 18, fill: 'none', stroke: 'currentColor',
      'stroke-width': name === 'dots' ? 3.4 : 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' };
    for (const key of Object.keys(attrs)) svg.setAttribute(key, attrs[key]);
    for (const d of ICONS[name]) {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.append(path);
    }
    return svg;
  }

  const when = (ts) => new Date(ts * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });

  // Значок-кнопка без подписи; подпись для экрана и подсказки — label
  function tool(label, iconName, handler, extra) {
    const node = el('button', 'tool' + (extra ? ' ' + extra : ''), icon(iconName));
    node.type = 'button';
    node.title = label;
    node.setAttribute('aria-label', label);
    if (handler) node.addEventListener('click', handler);
    return node;
  }

  function textButton(label, iconName, handler, extra) {
    const node = el('button', 'btn small with-icon' + (extra ? ' ' + extra : ''), iconName ? icon(iconName, 16) : null, label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }

  function linkButton(label, href, iconName) {
    const node = el('a', 'btn small with-icon', icon(iconName, 16), label);
    node.href = href;
    return node;
  }

  function selectNode(node) {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  // ----- подтверждение и меню ---------------------------------------------------------------
  function clearConfirm() {
    document.querySelectorAll('.confirm-bar').forEach((bar) => bar.remove());
  }

  // Полоса подтверждения: под строкой токена (after) или внизу карточки. Фокус на «Отмене», чтобы случайный Enter
  // ничего не удалил.
  function askConfirm(card, text, label, action, after) {
    clearConfirm();
    const yes = textButton(label, null, () => { clearConfirm(); run(action); }, 'danger');
    const no = textButton('Отмена', null, clearConfirm);
    const bar = el(after ? 'li' : 'div', 'confirm-bar', el('span', null, text), yes, no);
    if (after) after.after(bar);
    else card.append(bar);
    no.focus();
  }

  function closeMenus(except) {
    document.querySelectorAll('details.menu[open]').forEach((menu) => { if (menu !== except) menu.open = false; });
  }

  function boardMenu(board, card, titleBox) {
    const menu = el('details', 'menu');
    const trigger = el('summary', 'tool', icon('dots'));
    trigger.title = 'Ещё';
    trigger.setAttribute('aria-label', 'Меню платы «' + board.name + '»');
    const list = el('div', 'menu-list');
    const item = (label, iconName, handler, extra) => {
      const node = el('button', 'menu-item' + (extra ? ' ' + extra : ''), icon(iconName, 16), label);
      node.type = 'button';
      node.addEventListener('click', () => { menu.open = false; handler(); });
      return node;
    };
    const remove = item('Удалить плату', 'trash', () => askDelete(card, board), 'danger');
    const locked = Lib.hasActiveToken(board); // пока токен действует, плату не удалить: сначала отзывают доступ
    remove.disabled = locked;
    list.append(item('Переименовать', 'pencil', () => startRename(titleBox, board)), remove);
    if (locked) list.append(el('span', 'menu-hint', 'Сначала отзовите токены'));
    menu.append(trigger, list);
    menu.addEventListener('toggle', () => { if (menu.open) closeMenus(menu); });
    return menu;
  }

  // ----- карточки ------------------------------------------------------------------------------
  function keyRow(item, now, board) {
    const view = Lib.describeToken(item, now);
    const use = Lib.tokenUse(item, now);
    const row = el('li', 'key' + (use.live ? ' live' : '') + (item.active ? '' : ' revoked'));
    const keyIcon = icon('key', 18);
    keyIcon.classList.add('key-icon');
    const line = el('div', 'key-line', el('span', 'key-prefix', view.key), el('span', 'key-use', use.text));
    if (use.title) line.title = use.title;
    row.title = 'Создан ' + when(item.created_at);
    row.append(keyIcon, el('div', 'key-main', el('span', 'key-name', item.name), line));
    row.append(item.active
      ? tool('Отозвать токен', 'ban', () => askRevoke(row, item, view, board), 'danger')
      : tool('Удалить запись', 'trash', () => askRemoveToken(row, item), 'danger'));
    return row;
  }

  function revokedBlock(items, now, board) {
    const box = el('details', 'old-keys', el('summary', null, 'Отозванные: ' + items.length));
    const list = el('ul', 'keys');
    items.forEach((item) => list.append(keyRow(item, now, board)));
    box.append(list);
    return box;
  }

  function boardCard(board, now) {
    const chip = Lib.boardChip(board, now);
    const card = el('li', 'plate');
    card.dataset.state = chip.state;

    const titleBox = el('div', 'plate-title', el('span', 'plate-name', board.name), el('span', 'plate-id', board.device));
    card.append(el('div', 'plate-top', titleBox, boardMenu(board, card, titleBox)));

    const meta = el('div', 'plate-meta', el('span', 'chip dot ' + chip.state, chip.text));
    meta.firstChild.title = chip.title;
    if (board.fw) meta.append(el('span', 'chip', board.fw));
    // Владельца знает и показывает только администратору сервер; на своих платах имя не нужно
    if (board.owner && !(me && board.owner === me.username)) meta.append(el('span', 'chip', icon('user', 13), board.owner));
    card.append(meta);

    const active = board.tokens.filter((item) => item.active);
    const revoked = board.tokens.filter((item) => !item.active);
    if (active.length) {
      const keys = el('ul', 'keys');
      active.forEach((item) => keys.append(keyRow(item, now, board)));
      card.append(keys);
    } else {
      card.append(el('p', 'plate-warn', 'Нет токена: плата не сможет отправлять данные'));
    }
    if (revoked.length) card.append(revokedBlock(revoked, now, board));

    const newKey = textButton('Токен', 'plus', () => newToken(board), active.length ? '' : 'primary');
    newKey.title = 'Новый токен';
    card.append(el('div', 'plate-actions', newKey,
      linkButton('Настройки', '/settings?device=' + encodeURIComponent(board.device), 'sliders'),
      linkButton('Графики', '/?device=' + encodeURIComponent(board.device), 'chart')));
    return card;
  }

  function renderShared(now) {
    const list = $('shared-list');
    list.textContent = '';
    const active = data.shared_tokens.filter((item) => item.active);
    const revoked = data.shared_tokens.filter((item) => !item.active);
    active.forEach((item) => list.append(keyRow(item, now, null)));
    if (revoked.length) list.append(el('li', 'revoked-item', revokedBlock(revoked, now, null)));
    $('shared-section').hidden = data.shared_tokens.length === 0;
  }

  function render() {
    const now = Math.floor(Date.now() / 1000);
    $('summary').textContent = Lib.summary(data.boards);
    const list = $('board-list');
    list.textContent = '';
    data.boards.forEach((board) => list.append(boardCard(board, now)));
    renderShared(now);
    if (!data.boards.length && !autoOpened) { autoOpened = true; setAddOpen(true, false); }
    syncEmpty();
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

  // ----- добавление платы и показ токена ---------------------------------------------------
  function syncEmpty() {
    $('board-empty').hidden = data.boards.length > 0 || !$('add-panel').hidden;
  }

  function setAddOpen(open, focus) {
    $('add-panel').hidden = !open;
    $('add-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    syncEmpty();
    if (open && focus !== false) $('new-name').focus();
  }

  function showCreated(title, device, token) {
    $('created-title').textContent = title;
    $('created-url').textContent = Lib.ingestUrl(location.origin);
    $('created-device').textContent = device;
    $('created-token').textContent = token;
    $('created').hidden = false;
  }

  function hideCreated() {
    $('created-token').textContent = ''; // после закрытия токена на странице не остаётся
    $('created').hidden = true;
  }

  function revealCreated() {
    $('created').scrollIntoView({ block: 'nearest' });
    selectNode($('created-token'));
  }

  // Копирует значение; значок на секунду сменяется галочкой. Без доступа к буферу значение выделяется для Ctrl+C.
  async function copyFrom(id, button) {
    try {
      await navigator.clipboard.writeText($(id).textContent);
      button.replaceChildren(icon('check'));
      button.title = 'Скопировано';
    } catch (e) {
      selectNode($(id));
      button.title = 'Выделено: нажмите Ctrl+C';
    }
    setTimeout(() => { button.replaceChildren(icon('copy')); button.title = 'Копировать'; }, 1500);
  }

  function newToken(board) {
    run(async () => {
      const made = await api('POST', '/api/boards/' + board.device + '/tokens', {});
      showCreated('Новый токен для «' + board.name + '»', board.device, made.token);
      await load();
      revealCreated();
    });
  }

  // ----- переименование, отзыв, удаление -------------------------------------------------------
  function startRename(titleBox, board) {
    const nameNode = titleBox.querySelector('.plate-name');
    if (!nameNode || titleBox.querySelector('.rename')) return;
    const input = el('input', 'input');
    input.type = 'text';
    input.maxLength = Lib.NAME_MAX;
    input.value = board.name;
    input.setAttribute('aria-label', 'Название платы');
    const save = tool('Сохранить', 'check', null, 'ok');
    save.type = 'submit';
    const form = el('form', 'rename', input, save, tool('Отмена', 'close', () => form.replaceWith(nameNode)));
    nameNode.replaceWith(form);
    input.focus();
    input.select();
    input.addEventListener('keydown', (event) => { if (event.key === 'Escape') form.replaceWith(nameNode); });
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const check = Lib.validateName(input.value);
      if (!check.ok) { showError(check.error); return; }
      if (check.name === board.name) { form.replaceWith(nameNode); return; }
      run(async () => { await api('POST', '/api/boards/' + board.device + '/rename', { name: check.name }); await load(); });
    });
  }

  function askRevoke(row, item, view, board) {
    const card = row.closest('.plate, .shared');
    const text = view.inUse
      ? 'Плата использует токен «' + item.name + '» прямо сейчас: после отзыва данные перестанут приходить, пока не введёте другой.'
      : 'Отозвать токен «' + item.name + '»' + (board ? ' платы «' + board.name + '»' : '') + '?';
    askConfirm(card, text, 'Отозвать', async () => { await api('POST', '/api/tokens/' + item.id + '/revoke'); await load(); }, row);
  }

  function askRemoveToken(row, item) {
    askConfirm(row.closest('.plate, .shared'), 'Удалить запись об отозванном токене «' + item.name + '»?', 'Удалить',
      async () => { await api('DELETE', '/api/tokens/' + item.id); await load(); }, row);
  }

  function askDelete(card, board) {
    askConfirm(card, 'Удалить «' + board.name + '» вместе с показаниями и журналом? Это нельзя отменить.', 'Удалить',
      async () => { await api('DELETE', '/api/boards/' + board.device); await load(); });
  }

  // ----- запуск ----------------------------------------------------------------------------------
  $('new-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const check = Lib.validateNewBoard($('new-name').value, $('new-device').value);
    if (!check.ok) { showError(check.error); return; }
    run(async () => {
      const made = await api('POST', '/api/boards', { name: check.name, device: check.device });
      showCreated('Плата «' + made.board.name + '» добавлена', made.board.device, made.token);
      $('new-name').value = '';
      deviceTouched = false;
      setAddOpen(false);
      await load();
      revealCreated();
    });
  });

  $('add-toggle').prepend(icon('plus', 16));
  $('hide-created').append(icon('close'));
  for (const id of ['copy-url', 'copy-device', 'copy-token']) {
    const button = $(id);
    button.append(icon('copy'));
    button.addEventListener('click', () => copyFrom('created-' + id.slice(5), button));
  }
  $('add-toggle').addEventListener('click', () => setAddOpen($('add-panel').hidden));
  $('add-cancel').addEventListener('click', () => setAddOpen(false));
  $('empty-add').addEventListener('click', () => setAddOpen(true));
  $('new-device').addEventListener('input', () => { deviceTouched = true; });
  $('hide-created').addEventListener('click', hideCreated);
  document.addEventListener('click', (event) => {
    document.querySelectorAll('details.menu[open]').forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenus(null); });

  api('GET', '/api/me').then((answer) => { me = answer.user; }).catch(() => { /* без имени покажем владельцев всех плат */ }).then(load);
  // Пока открыто подтверждение или идёт переименование, список не перерисовывается: он сбросил бы их
  setInterval(() => { if (!document.hidden && !busy && !document.querySelector('.confirm-bar, .rename')) load(); }, REFRESH_MS);
})();
