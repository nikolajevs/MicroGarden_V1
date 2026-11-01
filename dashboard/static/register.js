// Страница регистрации: новая запись сразу входит в панель и ведёт на страницу «Платы», где человек добавляет свою плату.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  $('register-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const check = Lib.validateRegistration($('username').value.trim(), $('password').value, $('repeat').value);
    if (!check.ok) { showError(check.error); return; }
    $('submit').disabled = true;
    showError('');
    try {
      const res = await fetch('/api/register', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Dash-Action': '1' },
        body: JSON.stringify({ username: check.username, password: check.password }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* ответ не JSON */ }
      if (!res.ok) throw new Error((data && data.error) || 'ответ сервера ' + res.status);
      location.assign('/boards');
    } catch (e) {
      showError(e.message);
      $('submit').disabled = false;
    }
  });

  fetch('/api/registration', { cache: 'no-store' })
    .then((res) => res.json())
    .then((data) => {
      $('register-form').hidden = !data.open;
      $('closed').hidden = data.open;
    })
    .catch(() => { /* без ответа форма остаётся: сервер сам откажет, если регистрация закрыта */ });

  $('username').focus();
})();
