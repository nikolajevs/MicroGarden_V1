// Страницы входа, регистрации, восстановления пароля, аккаунта и администратора: чистые функции без DOM; их проверяет
// dashboard/tests/auth.test.cjs. Правила имени, почты и пароля такие же, как на сервере (server.py: clean_username,
// clean_email, clean_password), сервер всё равно проверяет их ещё раз.
(function () {
  'use strict';

  const USERNAME_RE = /^[A-Za-z0-9_.-]{3,32}$/;
  const EMAIL_RE = /^[a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$/;
  const EMAIL_MAX = 254;
  const PASSWORD_MIN = 10;
  const PASSWORD_MAX = 128;
  const RESERVED = ['admin', 'administrator', 'root', 'system', 'support', 'api', 'null'];

  function validateUsername(value) {
    const name = String(value == null ? '' : value);
    if (!USERNAME_RE.test(name)) return { ok: false, error: 'Имя: от 3 до 32 символов, латиница, цифры, _ . -' };
    if (RESERVED.includes(name.toLowerCase())) return { ok: false, error: 'Это имя зарезервировано: выберите другое' };
    return { ok: true, username: name };
  }

  // Адрес почты хранится в нижнем регистре; кириллица в адресе пока не поддерживается
  function validateEmail(value) {
    const email = String(value == null ? '' : value).trim().toLowerCase();
    if (email.length > EMAIL_MAX || !EMAIL_RE.test(email) || email.includes('..') || email.split('@')[0].endsWith('.')) {
      return { ok: false, error: 'Почта: нужен адрес вида name@example.com (латиница)' };
    }
    return { ok: true, email };
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

  // Форма регистрации: имя, почта (только если сервер её просит: тогда email передан), пароль и повтор пароля
  // (повтор проверяется только на странице)
  function validateRegistration(username, password, repeat, email) {
    const name = validateUsername(username);
    if (!name.ok) return name;
    let mail = null;
    if (email !== undefined) {
      mail = validateEmail(email);
      if (!mail.ok) return mail;
    }
    const pass = validatePassword(password, name.username);
    if (!pass.ok) return pass;
    if (password !== repeat) return { ok: false, error: 'Пароли не совпадают' };
    const result = { ok: true, username: name.username, password: pass.password };
    if (mail) result.email = mail.email;
    return result;
  }

  // Смена пароля: новый пароль проверяется по тем же правилам, а повтор должен совпасть. У записи, созданной входом
  // через Google, пароля нет, и первый задаётся без текущего (needOld === false).
  function validatePasswordChange(oldPassword, newPassword, repeat, username, needOld) {
    const askOld = needOld !== false;
    if (askOld && !oldPassword) return { ok: false, error: 'Введите текущий пароль' };
    const pass = validatePassword(newPassword, username);
    if (!pass.ok) return pass;
    if (newPassword !== repeat) return { ok: false, error: 'Новые пароли не совпадают' };
    return askOld ? { ok: true, old: oldPassword, new: pass.password } : { ok: true, new: pass.password };
  }

  // Новый пароль по ссылке из письма: имени на странице нет, поэтому сравнивать с ним нечего
  function validateNewPassword(password, repeat) {
    const pass = validatePassword(password, '');
    if (!pass.ok) return pass;
    if (password !== repeat) return { ok: false, error: 'Пароли не совпадают' };
    return { ok: true, password: pass.password };
  }

  // Привязка или смена почты в аккаунте: адрес и текущий пароль (у записи из Google пароля нет, needPassword === false)
  function validateEmailChange(email, password, needPassword) {
    const mail = validateEmail(email);
    if (!mail.ok) return mail;
    if (needPassword === false) return { ok: true, email: mail.email };
    if (!password) return { ok: false, error: 'Введите пароль' };
    return { ok: true, email: mail.email, password };
  }

  // Почему вход через Google не удался: код приходит в адресе страницы входа (?google=код), см. server.py: _google_callback
  const GOOGLE_ERRORS = {
    denied: 'Вход через Google отменён.',
    state: 'Вход через Google не удался: время вышло или вход был начат в другом браузере. Попробуйте ещё раз.',
    error: 'Не удалось войти через Google. Попробуйте ещё раз или войдите по паролю.',
    unverified: 'Google не подтвердил адрес вашей почты, поэтому войти этим способом нельзя.',
    email: 'Адрес почты из Google не подошёл (нужна латиница). Войдите по паролю.',
    busy: 'Сейчас слишком много входов. Попробуйте через несколько минут.',
    limit: 'Слишком много попыток входа через Google с вашего адреса. Попробуйте позже.',
    closed: 'Регистрация закрыта: новую запись через Google создать нельзя.',
    full: 'На сервере нет места для новых записей.',
    blocked: 'Запись заблокирована администратором.',
    admin: 'Администратор входит только по имени и паролю.',
    other: 'Эта запись уже связана с другим аккаунтом Google. Войдите тем аккаунтом или по паролю.',
  };

  function googleError(code) {
    if (typeof code !== 'string' || !code) return '';
    return Object.prototype.hasOwnProperty.call(GOOGLE_ERRORS, code) ? GOOGLE_ERRORS[code] : GOOGLE_ERRORS.error;
  }

  // Адрес кнопки «Войти через Google»: после входа человека вернут туда, куда он шёл (только путь на этом сайте)
  function googleHref(next) {
    const path = safeNext(next);
    return path === '/' ? '/auth/google' : '/auth/google?next=' + encodeURIComponent(path);
  }

  // Ссылка из письма выглядит как https://сайт/reset#token=...; значение после «#» в журналы сервера и в Referer не попадает
  function tokenFromHash(hash) {
    const match = /^#token=([A-Za-z0-9_-]{20,64})$/.exec(String(hash == null ? '' : hash));
    return match ? match[1] : '';
  }

  // Куда вести после входа: только путь на этом же сайте. Адрес чужого сайта, «//хост», обратный слэш,
  // управляющие символы и сами страницы входа заменяются главной (защита от перенаправления на чужой сайт).
  function safeNext(value) {
    if (typeof value !== 'string' || value.length > 300) return '/';
    if (!value.startsWith('/') || value.startsWith('//') || /[\\\u0000-\u001f\u007f]/.test(value)) return '/';
    const path = value.split(/[?#]/)[0];
    if (['/login', '/register', '/forgot'].some((page) => path === page || path === page + '.html')) return '/';
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
      email: (item.email ? 'Почта: ' + item.email + (item.email_verified ? ' (подтверждена)' : ' (не подтверждена)') : 'Почта не указана')
        + (item.google ? ' · вход через Google' : ''),
      canBlock: !admin && !item.disabled,
      canUnblock: !admin && item.disabled,
      canReset: !admin,
      canDelete: !admin,
    };
  }

  // Что показать про почту на странице «Аккаунт». state — раздел email из /api/me: {address, verified, pending}:
  // адрес записи, подтверждён ли он, и адрес, который ждёт подтверждения по ссылке из письма.
  function describeEmail(state) {
    const address = state && state.address;
    const pending = state && state.pending;
    if (!address && !pending) {
      return { status: 'none', text: 'Почта не указана', note: 'Укажите адрес: на него придёт ссылка для подтверждения, и по нему можно будет восстановить пароль.', canResend: false };
    }
    if (state.verified) {
      const note = pending && pending !== address ? 'Ждём подтверждения нового адреса ' + pending + ': откройте ссылку из письма.' : '';
      return { status: 'ok', text: address + ' · подтверждена', note, canResend: !!note };
    }
    return { status: 'wait', text: (address || pending) + ' · не подтверждена',
      note: 'Откройте ссылку из письма, чтобы подтвердить адрес. Пока он не подтверждён, добавлять платы нельзя.', canResend: true };
  }

  const api = { USERNAME_RE, EMAIL_RE, EMAIL_MAX, PASSWORD_MIN, PASSWORD_MAX, GOOGLE_ERRORS, validateUsername, validateEmail,
                validatePassword, validateRegistration, validatePasswordChange, validateNewPassword, validateEmailChange,
                tokenFromHash, googleError, googleHref, safeNext, agoText, plural, describeUser, describeEmail };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.AuthLib = api;
})();
