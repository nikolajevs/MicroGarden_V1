// Страница аккаунта: кто вошёл, смена своего пароля и выход.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);
  let user = null;

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
    if (res.status === 401 && url !== '/api/login') { location.assign('/login?next=' + encodeURIComponent('/account')); }
    if (!res.ok) throw new Error((payload && payload.error) || 'ответ сервера ' + res.status);
    return payload;
  }

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  $('password-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('done').hidden = true;
    const check = Lib.validatePasswordChange($('old').value, $('new').value, $('repeat').value, user && user.username);
    if (!check.ok) { showError(check.error); return; }
    $('submit').disabled = true;
    showError('');
    try {
      await api('POST', '/api/account/password', { old: check.old, new: check.new });
      $('password-form').reset();
      $('done').hidden = false;
    } catch (e) {
      showError(e.message);
    } finally {
      $('submit').disabled = false;
    }
  });

  $('logout').addEventListener('click', async () => {
    try { await api('POST', '/api/logout'); } catch (e) { /* уже вышли или сервер не ответил */ }
    location.assign('/login');
  });

  api('GET', '/api/me').then((data) => {
    user = data.user;
    $('who').textContent = user.username + (user.role === 'admin' ? ' · администратор' : '');
    const admin = user.role === 'admin';
    $('password-form').hidden = admin;
    $('admin-note').hidden = !admin;
  }).catch((e) => { $('who').textContent = 'Не удалось загрузить: ' + e.message; });
})();
