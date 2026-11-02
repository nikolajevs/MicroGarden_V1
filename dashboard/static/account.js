// Страница аккаунта: кто вошёл, почта (привязать, сменить, получить ссылку ещё раз), смена своего пароля и выход.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);
  let user = null;
  let hasPassword = true;    // у записи, созданной входом через Google, пароля нет: первый задаётся без текущего

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

  function showEmailError(text) {
    $('email-error').textContent = text || '';
    $('email-error').hidden = !text;
  }

  // Состояние почты: подпись, пояснение и кнопка «ещё раз» берутся из AuthLib.describeEmail
  function showEmail(state) {
    const view = Lib.describeEmail(state);
    $('email-state').textContent = view.text;
    $('email-note').textContent = view.note;
    $('email-note').hidden = !view.note;
    $('email-resend').hidden = !view.canResend;
  }

  $('password-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('done').hidden = true;
    const check = Lib.validatePasswordChange($('old').value, $('new').value, $('repeat').value, user && user.username, hasPassword);
    if (!check.ok) { showError(check.error); return; }
    $('submit').disabled = true;
    showError('');
    try {
      await api('POST', '/api/account/password', hasPassword ? { old: check.old, new: check.new } : { new: check.new });
      $('password-form').reset();
      $('done').hidden = false;
      if (!hasPassword) showPasswordMode(true);   // теперь пароль есть, и менять его дальше нужно с текущим
    } catch (e) {
      showError(e.message);
    } finally {
      $('submit').disabled = false;
    }
  });

  // Пароль есть или его ещё нет (запись из Google): от этого зависят подпись, поле «Текущий пароль» и запросы
  function showPasswordMode(has) {
    hasPassword = has;
    $('old-field').hidden = !has;
    $('no-password').hidden = has;
    $('password-title').textContent = has ? 'Сменить пароль' : 'Задать пароль';
    $('submit').textContent = has ? 'Сменить пароль' : 'Задать пароль';
    $('email-password-field').hidden = !has;
  }

  $('email-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('email-done').hidden = true;
    const check = Lib.validateEmailChange($('email-new').value, $('email-password').value, hasPassword);
    if (!check.ok) { showEmailError(check.error); return; }
    $('email-submit').disabled = true;
    showEmailError('');
    try {
      const answer = await api('POST', '/api/account/email', hasPassword ? { email: check.email, password: check.password } : { email: check.email });
      $('email-form').reset();
      showEmail(answer.email);
      $('email-done').hidden = !answer.mail_sent;
      if (!answer.mail_sent) showEmailError('Письмо не отправлено: попробуйте позже.');
    } catch (e) {
      showEmailError(e.message);
    } finally {
      $('email-submit').disabled = false;
    }
  });

  $('email-resend').addEventListener('click', async () => {
    $('email-done').hidden = true;
    $('email-resend').disabled = true;
    showEmailError('');
    try {
      const answer = await api('POST', '/api/account/email/resend', {});
      showEmail(answer.email);
      $('email-done').hidden = !answer.mail_sent;
      if (!answer.mail_sent) showEmailError('Письмо не отправлено: попробуйте позже.');
    } catch (e) {
      showEmailError(e.message);
    } finally {
      $('email-resend').disabled = false;
    }
  });

  // ----- удаление данных и записи ----------------------------------------------------------
  function showDataMessage(error, done) {
    $('data-error').textContent = error || '';
    $('data-error').hidden = !error;
    $('data-done').textContent = done || '';
    $('data-done').hidden = !done;
  }

  function showDeleteError(text) {
    $('delete-error').textContent = text || '';
    $('delete-error').hidden = !text;
  }

  // Подтверждение стирания данных стоит на самой странице (не окном браузера); фокус на «Отмене»: случайный Enter ничего не сотрёт
  $('data-ask').addEventListener('click', () => {
    showDataMessage('', '');
    $('data-confirm').hidden = false;
    $('data-no').focus();
  });
  $('data-no').addEventListener('click', () => { $('data-confirm').hidden = true; });
  $('data-yes').addEventListener('click', async () => {
    $('data-yes').disabled = true;
    try {
      const answer = await api('POST', '/api/account/data/delete', { confirm: true });
      showDataMessage('', 'Стёрто: ' + Lib.eraseSummary(answer));
    } catch (e) {
      showDataMessage(e.message, '');
    } finally {
      $('data-confirm').hidden = true;
      $('data-yes').disabled = false;
    }
  });

  $('delete-ask').addEventListener('click', () => {
    showDeleteError('');
    $('delete-form').hidden = false;
    $('delete-ask').hidden = true;
    $('delete-confirm').focus();
  });
  $('delete-cancel').addEventListener('click', () => {
    $('delete-form').reset();
    $('delete-form').hidden = true;
    $('delete-ask').hidden = false;
    showDeleteError('');
  });
  $('delete-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const check = Lib.validateDeletion($('delete-confirm').value, user && user.username, $('delete-password').value, hasPassword);
    if (!check.ok) { showDeleteError(check.error); return; }
    $('delete-submit').disabled = true;
    showDeleteError('');
    try {
      await api('POST', '/api/account/delete', hasPassword ? { confirm: check.confirm, password: check.password } : { confirm: check.confirm });
      location.assign('/login?deleted=1');
    } catch (e) {
      showDeleteError(e.message);
      $('delete-submit').disabled = false;
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
    $('google-state').hidden = !data.google;
    showPasswordMode(data.has_password !== false);
    // Администратор задан в .env и сам себя удалить не может; запись из Google подтверждает удаление свежим входом, а не паролем
    $('delete-box').hidden = admin;
    $('delete-admin').hidden = !admin;
    $('delete-name').textContent = user.username;
    $('delete-password-field').hidden = !hasPassword;
    $('delete-need-login').hidden = hasPassword;
    $('email-box').hidden = !data.mail_enabled;
    $('email-off').hidden = !!data.mail_enabled;
    if (data.mail_enabled) showEmail(data.email);
  }).catch((e) => { $('who').textContent = 'Не удалось загрузить: ' + e.message; });
})();
