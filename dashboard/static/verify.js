// Страница по ссылке из письма «подтвердите почту». Подтверждает только нажатие кнопки: программы, которые сами открывают
// ссылки из писем (проверка на вирусы, предпросмотр), кнопку не нажимают и чужой адрес «подтвердить» не могут. Ссылку
// страница берёт из адреса после «#» и убирает её из адресной строки.
(function () {
  'use strict';

  const Lib = window.AuthLib;
  const $ = (id) => document.getElementById(id);
  const token = Lib.tokenFromHash(location.hash);

  function showError(text) {
    $('error').textContent = text || '';
    $('error').hidden = !text;
  }

  if (location.hash) history.replaceState(null, '', location.pathname);
  // Новую ссылку, вставленную в эту же вкладку, страница читает заново (иначе она осталась бы со старой)
  window.addEventListener('hashchange', () => location.reload());

  if (!token) {
    showError('Ссылка неполная: откройте её из письма целиком или запросите новое письмо на странице «Аккаунт».');
  } else {
    $('ask').hidden = false;
    $('confirm').hidden = false;
  }

  $('confirm').addEventListener('click', async () => {
    $('confirm').disabled = true;
    showError('');
    try {
      const res = await fetch('/api/verify', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Dash-Action': '1' },
        body: JSON.stringify({ token }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* ответ не JSON */ }
      if (!res.ok) throw new Error((data && data.error) || 'ответ сервера ' + res.status);
      $('address').textContent = data.email;
      $('ask').hidden = true;
      $('confirm').hidden = true;
      $('done').hidden = false;
    } catch (e) {
      showError(e.message);
      $('ask').hidden = true;
      $('confirm').hidden = true;
    }
  });
})();
