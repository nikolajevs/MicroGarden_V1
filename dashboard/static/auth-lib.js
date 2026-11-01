// Страницы входа, регистрации, аккаунта и администратора: чистые функции без DOM; их проверяет
// dashboard/tests/auth.test.cjs. Правила имени и пароля такие же, как на сервере (server.py: clean_username,
// clean_password), сервер всё равно проверяет их ещё раз.
(function () {
  'use strict';

  const USERNAME_RE = /^[A-Za-z0-9_.-]{3,32}$/;
  const PASSWORD_MIN = 10;
  const PASSWORD_MAX = 128;
  const RESERVED = ['admin', 'administrator', 'root', 'system', 'support', 'api', 'null'];

  function validateUsername(value) {
    const name = String(value == null ? '' : value);
    if (!USERNAME_RE.test(name)) return { ok: false, error: 'Имя: от 3 до 32 символов, латиница, цифры, _ . -' };
    if (RESERVED.includes(name.toLowerCase())) return { ok: false, error: 'Это имя зарезервировано: выберите другое' };
    return { ok: true, username: name };
  }

  function validatePassword(value, username) {
    const password = String(value == null ? '' : value);
    if (password.length < PASSWORD_MIN || password.length > PASSWORD_MAX) {
      return { ok: false, error: 'Пароль: от ' + PASSWORD_MIN + ' до ' + PASSWORD_MAX + ' символов' };
    }
    if (!password.trim()) return { ok: false, error: 'Пароль не может состоять из одних пробелов' };
    if (username && password.toLowerCase() === String(username).toLowerCase()) {
      return { ok: false, error: 'Пароль не должен совпадать с именем' };
    }
    return { ok: true, password };
  }

  // Форма регистрации: имя, пароль и повтор пароля (повтор проверяется только на странице)
  function validateRegistration(username, password, repeat) {
    const name = validateUsername(username);
    if (!name.ok) return name;
    const pass = validatePassword(password, name.username);
    if (!pass.ok) return pass;
    if (password !== repeat) return { ok: false, error: 'Пароли не совпадают' };
    return { ok: true, username: name.username, password: pass.password };
  }

  // Смена пароля: новый пароль проверяется по тем же правилам, а повтор должен совпасть
  function validatePasswordChange(oldPassword, newPassword, repeat, username) {
    if (!oldPassword) return { ok: false, error: 'Введите текущий пароль' };
    const pass = validatePassword(newPassword, username);
    if (!pass.ok) return pass;
    if (newPassword !== repeat) return { ok: false, error: 'Новые пароли не совпадают' };
    return { ok: true, old: oldPassword, new: pass.password };
  }

  // Куда вести после входа: только путь на этом же сайте. Адрес чужого сайта, «//хост», обратный слэш,
  // управляющие символы и сами страницы входа заменяются главной (защита от перенаправления на чужой сайт).
  function safeNext(value) {
    if (typeof value !== 'string' || value.length > 300) return '/';
    if (!value.startsWith('/') || value.startsWith('//') || /[\\\u0000-\u001f\u007f]/.test(value)) return '/';
    const path = value.split(/[?#]/)[0];
    if (path === '/login' || path === '/register' || path === '/login.html' || path === '/register.html') return '/';
    return value;
  }

  // «никогда», «только что», «5 мин назад», «3 ч назад», «2 дн назад»
  function agoText(ts, now) {
    if (typeof ts !== 'number' || !isFinite(ts)) return 'никогда';
    const sec = Math.max(0, now - ts);
    if (sec < 60) return 'только что';
    if (sec < 3600) return Math.floor(sec / 60) + ' мин назад';
    if (sec < 86400) return Math.floor(sec / 3600) + ' ч назад';
    return Math.floor(sec / 86400) + ' дн назад';
  }

  function plural(n, one, few, many) {
    const m10 = n % 10;
    const m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  // Подписи строки человека на странице администратора. item — запись из /api/admin/users.
  function describeUser(item, now) {
    const admin = item.role === 'admin';
    return {
      status: item.disabled ? 'blocked' : admin ? 'admin' : 'active',
      statusText: item.disabled ? 'Заблокирован' : admin ? 'Администратор' : 'Активен',
      lastLogin: 'Вход: ' + agoText(item.last_login_at, now),
      boards: item.boards + ' ' + plural(item.boards, 'плата', 'платы', 'плат'),
      canBlock: !admin && !item.disabled,
      canUnblock: !admin && item.disabled,
      canReset: !admin,
      canDelete: !admin,
    };
  }

  const api = { USERNAME_RE, PASSWORD_MIN, PASSWORD_MAX, validateUsername, validatePassword, validateRegistration,
                validatePasswordChange, safeNext, agoText, plural, describeUser };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.AuthLib = api;
})();
