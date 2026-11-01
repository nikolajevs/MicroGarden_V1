// Страница «Забыли пароль»: просит адрес почты и всегда отвечает одинаково, есть такой адрес в системе или нет.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  $('forgot-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const check = Lib.validateEmail($('email').value);
    if (!check.ok) { showError(check.error); return; }
    $('submit').disabled = true;
    showError('');
    try {
      const res = await fetch('/api/forgot', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Dash-Action': '1' },
        body: JSON.stringify({ email: check.email }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* ответ не JSON */ }
      if (!res.ok) throw new Error((data && data.error) || 'ответ сервера ' + res.status);
      $('forgot-form').hidden = true;   // повторно отправлять форму незачем: ссылка уже в пути
      $('done').hidden = false;
    } catch (e) {
      showError(e.message);
      $('submit').disabled = false;
    }
  });

  // Без настроенной почты форма бесполезна: честно говорим об этом
  fetch('/api/registration', { cache: 'no-store' })
    .then((res) => res.json())
    .then((data) => {
      $('forgot-form').hidden = !data.mail;
      $('off').hidden = !!data.mail;
    })
    .catch(() => { /* без ответа форма остаётся: сервер сам откажет */ });

  $('email').focus();
})();
