// Страница по ссылке из письма «восстановление пароля»: проверяет ссылку и принимает новый пароль. Саму ссылку страница
// берёт из адреса после «#» (туда она не уходит ни в журнал сервера, ни в Referer) и сразу убирает из адресной строки.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);
  const token = Lib.tokenFromHash(location.hash);

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Dash-Action': '1' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* ответ не JSON */ }
    if (!res.ok) throw new Error((data && data.error) || 'ответ сервера ' + res.status);
    return data;
  }

  function rejected(text) {
    $('checking').hidden = true;
    $('reset-form').hidden = true;
    showError(text);
    $('again').hidden = false;
  }

  $('reset-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const check = Lib.validateNewPassword($('password').value, $('repeat').value);
    if (!check.ok) { showError(check.error); return; }
    $('submit').disabled = true;
    showError('');
    try {
      await post('/api/reset', { token, password: check.password });
      $('reset-form').hidden = true;
      $('again').hidden = true;
      $('done').hidden = false;
    } catch (e) {
      showError(e.message);   // слабый пароль не расходует ссылку: можно ввести другой
      $('submit').disabled = false;
    }
  });

  if (location.hash) history.replaceState(null, '', location.pathname);
  // Новую ссылку, вставленную в эту же вкладку, страница читает заново (иначе она осталась бы со старой)
  window.addEventListener('hashchange', () => location.reload());
  if (!token) {
    rejected('Ссылка неполная: откройте её из письма целиком.');
  } else {
    post('/api/reset/check', { token })
      .then(() => {
        $('checking').hidden = true;
        $('reset-form').hidden = false;
        $('again').hidden = false;
        $('password').focus();
      })
      .catch((e) => rejected(e.message));
  }
})();
