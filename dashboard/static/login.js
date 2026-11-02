// Страница входа. Запрос идёт с заголовком X-Dash-Action: чужая страница не может его отправить
// (сервер не разрешает CORS), поэтому чужой сайт не может войти от имени человека (защита от CSRF).
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const username = $('username').value.trim();
    const password = $('password').value;
    if (!username || !password) { showError('Введите имя (или почту) и пароль'); return; }
    $('submit').disabled = true;
    showError('');
    try {
      const res = await fetch('/api/login', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Dash-Action': '1' },
        body: JSON.stringify({ username, password }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* ответ не JSON */ }
      if (!res.ok) throw new Error((data && data.error) || 'ответ сервера ' + res.status);
      location.assign(Lib.safeNext(new URLSearchParams(location.search).get('next')));
    } catch (e) {
      showError(e.message);
      $('submit').disabled = false;
      $('password').select();
    }
  });

  // Если регистрация закрыта, ссылки на неё не показываем; «Забыли пароль?» только там, где настроена почта, а кнопка
  // «Войти через Google» только там, где настроен вход через Google
  fetch('/api/registration', { cache: 'no-store' })
    .then((res) => res.json())
    .then((data) => {
      $('register-note').hidden = !data.open;
      $('forgot-note').hidden = !data.mail;
      $('google-box').hidden = !data.google;
    })
    .catch(() => { /* без ответа ссылка остаётся: страница регистрации сама скажет, закрыта ли она */ });

  const params = new URLSearchParams(location.search);
  // После смены пароля по ссылке из письма сюда ведёт /login?reset=1, а после неудачного входа через Google /login?google=код
  $('notice').hidden = params.get('reset') !== '1';
  $('google-link').href = Lib.googleHref(params.get('next'));
  if (params.get('google')) showError(Lib.googleError(params.get('google')));

  $('username').focus();
})();
