// Страница регистрации: новая запись сразу входит в панель и ведёт на страницу «Платы». Если на сервере настроена почта,
// просим адрес: на него уходит ссылка для подтверждения, а подтверждённая почта нужна, чтобы добавлять платы.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);
  let mailRequired = false;

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  $('register-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const check = Lib.validateRegistration($('username').value.trim(), $('password').value, $('repeat').value,
      mailRequired ? $('email').value : undefined);
    if (!check.ok) { showError(check.error); return; }
    $('submit').disabled = true;
    showError('');
    try {
      const body = { username: check.username, password: check.password };
      if (check.email) body.email = check.email;
      const res = await fetch('/api/register', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Dash-Action': '1' },
        body: JSON.stringify(body),
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
      mailRequired = !!data.mail;
      $('email-field').hidden = !mailRequired;
      $('note-mail').hidden = !mailRequired;
      $('note-nomail').hidden = mailRequired;
    })
    .catch(() => { /* без ответа форма остаётся: сервер сам откажет, если регистрация закрыта */ });

  $('username').focus();
})();
