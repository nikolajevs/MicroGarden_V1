// Страница администратора: открыть или закрыть регистрацию, заблокировать, удалить человека, сбросить ему пароль.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30000;

  let data = { users: [], registration_open: true, max_users: 0 };
  let busy = false;

  // Запросы, которые что-то меняют, идут с заголовком X-Dash-Action: чужая страница не может его отправить
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
    if (res.status === 401) location.assign('/login?next=' + encodeURIComponent('/admin'));
    if (!res.ok) throw new Error((payload && payload.error) || 'ответ сервера ' + res.status);
    return payload;
  }

  function showError(text) {
    $('page-error').textContent = text || '';
    $('page-error').hidden = !text;
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

  function row(user, now) {
    const view = Lib.describeUser(user, now);
    const li = document.createElement('li');
    li.className = 'board';
    const head = document.createElement('div');
    head.className = 'board-head';
    const title = document.createElement('div');
    title.className = 'board-title';
    title.append(text('span', 'name', user.username), text('span', 'device', view.boards));
    head.append(title, text('span', 'badge ' + (view.status === 'blocked' ? 'offline' : view.status === 'admin' ? 'in-use' : 'online'), view.statusText));
    li.append(head);
    li.append(text('div', 'meta', 'Зарегистрирован ' + when(user.created_at) + ' · ' + view.lastLogin + (user.reg_ip ? ' · адрес регистрации ' + user.reg_ip : '')));
    const actions = document.createElement('div');
    actions.className = 'board-actions';
    if (view.canBlock) actions.append(button('Заблокировать', 'danger', () => block(user, true)));
    if (view.canUnblock) actions.append(button('Разблокировать', '', () => block(user, false)));
    if (view.canReset) actions.append(button('Сбросить пароль', 'ghost', () => reset(user)));
    if (view.canDelete) actions.append(button('Удалить', 'danger', () => remove(user)));
    if (actions.children.length) li.append(actions);
    return li;
  }

  function render() {
    const now = Math.floor(Date.now() / 1000);
    const list = $('user-list');
    list.textContent = '';
    data.users.forEach((user) => list.append(row(user, now)));
    $('counts').textContent = data.users.length + ' из ' + data.max_users;
    $('reg-state').textContent = data.registration_open ? 'Сейчас регистрация открыта: любой может зарегистрироваться.' : 'Сейчас регистрация закрыта: новые люди зарегистрироваться не могут.';
    $('reg-toggle').textContent = data.registration_open ? 'Закрыть регистрацию' : 'Открыть регистрацию';
    $('reg-toggle').disabled = false;
  }

  async function load() {
    try {
      data = await api('GET', '/api/admin/users');
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

  function block(user, disabled) {
    if (disabled && !confirm('Заблокировать «' + user.username + '»? Он не сможет войти, а его платы перестанут присылать данные.')) return;
    run(async () => { await api('POST', '/api/admin/users/' + user.id + (disabled ? '/disable' : '/enable')); await load(); });
  }

  function reset(user) {
    if (!confirm('Сбросить пароль «' + user.username + '»? Все его входы будут закрыты.')) return;
    run(async () => {
      const made = await api('POST', '/api/admin/users/' + user.id + '/reset-password');
      $('reset-title').textContent = 'Новый пароль для «' + user.username + '»';
      $('reset-password').textContent = made.password;
      $('reset-note').textContent = '';
      $('reset-box').hidden = false;
      await load();
    });
  }

  function remove(user) {
    if (!confirm('Удалить «' + user.username + '» вместе с его платами, показаниями и журналом? Это нельзя отменить.')) return;
    run(async () => { await api('DELETE', '/api/admin/users/' + user.id); await load(); });
  }

  $('reg-toggle').addEventListener('click', () => {
    const open = !data.registration_open;
    if (!open && !confirm('Закрыть регистрацию? Новые люди не смогут зарегистрироваться, уже зарегистрированные продолжат работать.')) return;
    run(async () => { await api('POST', '/api/admin/registration', { open }); await load(); });
  });

  $('reset-hide').addEventListener('click', () => {
    $('reset-password').textContent = ''; // после закрытия пароля на странице не остаётся
    $('reset-box').hidden = true;
  });
  $('reset-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('reset-password').textContent);
      $('reset-note').textContent = 'Скопировано';
    } catch (e) {
      const range = document.createRange();
      range.selectNodeContents($('reset-password'));
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      $('reset-note').textContent = 'Пароль выделен: нажмите Ctrl+C';
    }
  });

  load();
  setInterval(() => { if (!document.hidden && !busy) load(); }, REFRESH_MS);
})();
