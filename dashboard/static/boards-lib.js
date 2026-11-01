// Страница плат: чистые функции без DOM; их проверяет dashboard/tests/boards.test.cjs. Правила названия и имени
// платы такие же, как на сервере (server.py: clean_name, clean_device, DEVICE_RE).
(function () {
  'use strict';

  const NAME_MAX = 40;
  const DEVICE_RE = /^[A-Za-z0-9_.-]{1,32}$/;
  const IN_USE_SEC = 600; // токен, которым пользовались в последние 10 минут, считается «используется»

  // «не использовался», «только что», «5 мин назад», «3 ч назад», «2 дн назад»
  function agoText(ts, now) {
    if (typeof ts !== 'number' || !isFinite(ts)) return 'не использовался';
    const sec = Math.max(0, now - ts);
    if (sec < 60) return 'только что';
    if (sec < 3600) return Math.floor(sec / 60) + ' мин назад';
    if (sec < 86400) return Math.floor(sec / 3600) + ' ч назад';
    return Math.floor(sec / 86400) + ' дн назад';
  }

  // Связь платы для карточки: онлайн, давно молчит или ещё ни разу не присылала данные
  function boardStatus(board, now) {
    if (board.last_seen == null) return { status: 'never', text: 'Данных ещё не было' };
    if (board.online) return { status: 'online', text: 'Онлайн' };
    return { status: 'offline', text: 'Нет связи, данные были ' + agoText(board.last_seen, now) };
  }

  // Подписи одного токена. item — запись из /api/boards, now — секунды Unix.
  function describeToken(item, now) {
    const active = item.active === true;
    const used = typeof item.last_used_at === 'number';
    const inUse = active && used && now - item.last_used_at < IN_USE_SEC;
    return {
      status: !active ? 'revoked' : inUse ? 'in-use' : 'active',
      statusText: !active ? 'Отозван' : inUse ? 'Используется' : 'Действует',
      key: item.prefix + '…',
      used: used ? 'Использовался ' + agoText(item.last_used_at, now) + (item.last_ip ? ' (с ' + item.last_ip + ')' : '')
                 : 'Ни разу не использовался',
      inUse,
      canRevoke: active,
      canDelete: !active,
    };
  }

  // У платы нет ни одного действующего токена: данные она сможет присылать только с общим токеном из .env
  function hasActiveToken(board) {
    return board.tokens.some((token) => token.active === true);
  }

  const collapse = (value) => String(value == null ? '' : value).split(/\s+/).filter(Boolean).join(' ');
  const badName = (name) => name.length < 1 || name.length > NAME_MAX || /[\u0000-\u001f\u007f]/.test(name);

  // Название платы (и новое название при переименовании): пробелы схлопываются, как на сервере
  function validateName(value) {
    const name = collapse(value);
    if (badName(name)) return { ok: false, error: 'Название: от 1 до ' + NAME_MAX + ' символов' };
    return { ok: true, name };
  }

  // Форма «Добавить плату»: название и имя платы в системе (то, что плата присылает как device)
  function validateNewBoard(name, device) {
    const named = validateName(name);
    if (!named.ok) return named;
    const label = String(device == null ? '' : device).trim();
    if (!DEVICE_RE.test(label)) return { ok: false, error: 'Имя платы: от 1 до 32 символов, латиница, цифры, _ . -' };
    return { ok: true, name: named.name, device: label };
  }

  // Имя для новой платы: первое microgarden-N, которого ещё нет среди зарегистрированных
  function suggestDevice(taken) {
    const used = new Set(taken);
    for (let n = 2; n < 100; n++) {
      if (!used.has('microgarden-' + n)) return 'microgarden-' + n;
    }
    return '';
  }

  // Адрес, который вводится в настройках платы (поле «Адрес приёма»)
  function ingestUrl(origin) {
    return origin + '/api/ingest';
  }

  // Форма числительного: 1 плата, 2 платы, 5 плат
  function plural(n, one, few, many) {
    const mod100 = n % 100;
    const mod10 = n % 10;
    if (mod100 >= 11 && mod100 <= 14) return many;
    if (mod10 === 1) return one;
    return mod10 >= 2 && mod10 <= 4 ? few : many;
  }

  // Значок связи на карточке платы, коротко: «Онлайн», «Нет связи · 3 ч назад», «Нет данных»
  function boardChip(board, now) {
    if (board.last_seen == null) return { state: 'never', text: 'Нет данных', title: 'Плата ещё не присылала данные' };
    const ago = agoText(board.last_seen, now);
    if (board.online) return { state: 'online', text: 'Онлайн', title: 'Данные пришли ' + ago };
    return { state: 'offline', text: 'Нет связи · ' + ago, title: 'Последние данные пришли ' + ago };
  }

  // Строка под заголовком страницы: «3 платы · 2 онлайн»
  function summary(boards) {
    const n = boards.length;
    if (!n) return 'Плат пока нет';
    const online = boards.filter((board) => board.online).length;
    const state = online === 0 ? 'нет связи' : online === n ? (n === 1 ? 'онлайн' : 'все онлайн') : online + ' онлайн';
    return n + ' ' + plural(n, 'плата', 'платы', 'плат') + ' · ' + state;
  }

  // Токен в одной строке: как давно им пользовались. live — плата использует его прямо сейчас.
  function tokenUse(item, now) {
    if (item.active !== true) {
      return { text: typeof item.revoked_at === 'number' ? 'отозван ' + agoText(item.revoked_at, now) : 'отозван', live: false, title: '' };
    }
    const used = typeof item.last_used_at === 'number';
    return {
      text: used ? agoText(item.last_used_at, now) : 'не использовался',
      live: used && now - item.last_used_at < IN_USE_SEC,
      title: used && item.last_ip ? 'Последний раз с адреса ' + item.last_ip : '',
    };
  }

  const api = { NAME_MAX, DEVICE_RE, IN_USE_SEC, agoText, boardStatus, describeToken, hasActiveToken,
                validateName, validateNewBoard, suggestDevice, ingestUrl, plural, boardChip, summary, tokenUse };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.BoardsLib = api;
})();
