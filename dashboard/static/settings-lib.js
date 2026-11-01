// Страница «Настройки платы»: чистые функции без DOM; их проверяет dashboard/tests/settings.test.cjs. Пределы и
// подписи приходят от сервера (server.py: CFG_SPECS, CFG_GROUPS), здесь только разбор значений и подписи.
(function () {
  'use strict';

  const DAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']; // бит 0 — понедельник, как на плате
  const SAME = 0.0005; // числа с платы приходят с двумя знаками; меньшее различие считается одинаковым значением

  const pad = (n) => String(n).padStart(2, '0');
  const same = (a, b) => (typeof a === 'number' && typeof b === 'number' ? Math.abs(a - b) < SAME : a === b);

  // «5 мин назад», «только что», «3 ч назад»
  function ageText(seconds) {
    const sec = Math.max(0, Math.floor(seconds));
    if (sec < 60) return 'только что';
    if (sec < 3600) return Math.floor(sec / 60) + ' мин назад';
    if (sec < 86400) return Math.floor(sec / 3600) + ' ч назад';
    return Math.floor(sec / 86400) + ' дн назад';
  }

  // «ещё 7 мин», «меньше минуты», «время вышло»
  function remainingText(seconds) {
    if (seconds <= 0) return 'время вышло';
    if (seconds < 60) return 'меньше минуты';
    return 'ещё ' + Math.ceil(seconds / 60) + ' мин';
  }

  function timeText(hour, minute) {
    return pad(hour) + ':' + pad(minute);
  }

  // "06:30" -> [6, 30]; пустая или неверная строка -> null
  function parseTime(text) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(text == null ? '' : text));
    if (!m) return null;
    const hour = Number(m[1]);
    const minute = Number(m[2]);
    return hour <= 23 && minute <= 59 ? [hour, minute] : null;
  }

  // Дата начала цикла: плата хранит полночь выбранной даты как unixtime без часового пояса, поэтому и читаем как UTC
  function dateText(start) {
    if (typeof start !== 'number' || !(start > 0)) return '';
    const d = new Date(start * 1000);
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());
  }

  // "2026-11-01" -> unixtime полуночи; пустая строка -> 0 (цикл не начат); неверная дата -> null
  function parseDate(text) {
    const s = String(text == null ? '' : text).trim();
    if (s === '') return 0;
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!m) return null;
    const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    const back = new Date(ms);
    if (back.getUTCFullYear() !== Number(m[1]) || back.getUTCMonth() !== Number(m[2]) - 1 || back.getUTCDate() !== Number(m[3])) return null;
    return ms / 1000;
  }

  // Маска дней полива -> «Пн, Ср, Пт», «каждый день», «не поливать»
  function daysText(mask) {
    const bits = DAYS.filter((_, i) => mask & (1 << i));
    if (bits.length === 0) return 'не поливать';
    return bits.length === 7 ? 'каждый день' : bits.join(', ');
  }

  // Пары (метка, поле) схемы, в том порядке, как их показывает страница
  function allFields(schema) {
    const out = [];
    for (const group of schema) for (const field of group.fields) out.push(field);
    return out;
  }

  const paramsOf = (field) => field.params || [field.param];

  // Подпись настройки по её имени с названием группы («Климат сушки · Целевая температура»): одни и те же слова
  // встречаются в разных группах. У пары «час, минута» подпись общая.
  function labelOf(schema, param) {
    for (const group of schema) {
      for (const field of group.fields) if (paramsOf(field).includes(param)) return group.title + ' · ' + field.label;
    }
    return param;
  }

  // Значение настройки текстом: «25 °C», «06:30», «Пн, Ср», «Выключен»
  function formatValue(field, values) {
    if (field.type === 'flag') return values[field.param] === true ? 'включён' : values[field.param] === false ? 'выключен' : '—';
    if (field.type === 'time') {
      const [h, m] = field.params.map((p) => values[p]);
      return typeof h === 'number' && typeof m === 'number' ? timeText(h, m) : '—';
    }
    const v = values[field.param];
    if (v === undefined) return '—';
    if (field.type === 'days') return daysText(v);
    if (field.type === 'date') return v > 0 ? dateText(v) : 'не начат';
    if (field.type === 'select') {
      const option = (field.options || []).find((o) => o[0] === v);
      return option ? option[1] : String(v);
    }
    return String(v) + (field.unit ? ' ' + field.unit : '');
  }

  // Что изменилось: edits — значения, которые человек поставил в форме, values — что сейчас на плате.
  // Возвращает только отличающиеся настройки.
  function changesOf(edits, values) {
    const out = {};
    for (const param of Object.keys(edits)) {
      if (!same(edits[param], values[param])) out[param] = edits[param];
    }
    return out;
  }

  // Проверка числового поля до отправки: те же правила, что на сервере (server.py: clean_cfg_value)
  function checkNumber(field, text) {
    const s = String(text == null ? '' : text).trim().replace(',', '.');
    if (s === '' || !/^-?\d+(\.\d+)?$/.test(s)) return { ok: false, error: '«' + field.label + '»: введите число' };
    const value = Number(s);
    if (field.kind === 'int' && !Number.isInteger(value)) return { ok: false, error: '«' + field.label + '»: нужно целое число' };
    if (value < field.lo || value > field.hi) {
      return { ok: false, error: '«' + field.label + '»: от ' + field.lo + ' до ' + field.hi };
    }
    return { ok: true, value: field.kind === 'float' ? Math.round(value * 100) / 100 : value };
  }

  // Минимум не больше максимума (лампа, вентиляторы): плата отклонила бы такой запрос, поэтому проверяем заранее
  const PAIRS = [['led_min_limit', 'led_max_limit', 'Мощность лампы'], ['fan1_min_limit', 'fan1_max_limit', 'Вентилятор 1'],
    ['fan2_min_limit', 'fan2_max_limit', 'Вентилятор 2']];

  function checkOrder(values) {
    for (const [low, high, title] of PAIRS) {
      if (typeof values[low] === 'number' && typeof values[high] === 'number' && values[low] > values[high]) {
        return title + ': минимум не может быть больше максимума';
      }
    }
    return '';
  }

  // Строки «Подпись: было → станет» для списка изменений
  function describeChanges(schema, patch, values) {
    const lines = [];
    const done = new Set();
    const to = Object.assign({}, values, patch);
    for (const group of schema) {
      for (const field of group.fields) {
        const params = paramsOf(field);
        if (!params.some((p) => p in patch)) continue;
        lines.push({ label: group.title + ' · ' + field.label, from: formatValue(field, values), to: formatValue(field, to) });
        params.forEach((p) => done.add(p));
      }
    }
    Object.keys(patch).filter((p) => !done.has(p)).forEach((p) => lines.push({ label: p, from: '—', to: String(patch[p]) }));
    return lines;
  }

  // Причина отказа платы или сервера по коду из журнала запроса (note)
  function rejectionText(schema, note) {
    const [kind, arg] = String(note || '').split(':');
    if (kind === 'off') return 'на плате выключено разрешение менять настройки из панели';
    if (kind === 'range') return 'значение вне допустимых пределов: «' + labelOf(schema, arg) + '»';
    if (kind === 'param') return 'такую настройку нельзя менять из панели: ' + arg;
    if (kind === 'order') return 'минимум больше максимума';
    return note ? 'код ' + note : 'причина не указана';
  }

  // Состояние одного запроса из истории: подпись и вид значка. ttl — сколько запрос ждёт плату (у строк истории
  // срока годности нет, он считается от времени запроса).
  function requestStatus(schema, item, now, ttl) {
    switch (item.status) {
      case 'pending':
        return { text: 'Ждёт плату, ' + remainingText((item.expires_at || item.created_at + (ttl || 600)) - now), kind: 'in-use' };
      case 'applied': return { text: 'Применено платой', kind: 'online' };
      case 'rejected': return { text: 'Плата не применила: ' + rejectionText(schema, item.note), kind: 'offline' };
      case 'expired': return { text: 'Не доставлено: плата не вышла на связь за 10 минут', kind: 'offline' };
      case 'cancelled': return { text: 'Отменено', kind: 'never' };
      default: return { text: item.status, kind: 'never' };
    }
  }

  // Что сказать о связи с платой и можно ли менять настройки: { form: показывать ли форму, editable: разрешены ли правки, text }
  function linkState(state) {
    if (!state.supported) {
      return { form: false, editable: false, kind: 'never', pill: 'Настроек нет',
        text: 'Эта плата ещё не сообщала свои настройки. Нужна прошивка, которая умеет менять их из панели; после обновления настройки появятся здесь в течение минуты.' };
    }
    const age = state.now - state.reported_at;
    const known = 'Настройки получены ' + ageText(age) + '.';
    if (!state.cloud) {
      return { form: true, editable: false, kind: 'offline', pill: 'Правка выключена',
        text: 'Менять настройки из панели не разрешено на самой плате. Откройте её страницу настроек, вкладка «WiFi», раздел «Телеметрия», поставьте галочку «Разрешить менять настройки платы из панели» и нажмите «Сохранить телеметрию». ' + known + ' Пока значения только для просмотра.' };
    }
    if (!state.online) {
      return { form: true, editable: true, kind: 'offline', pill: 'Нет связи',
        text: 'Плата не в сети. ' + known + ' Изменение ждёт плату ' + Math.round(state.ttl / 60) + ' мин, потом отменяется: устаревшие правки плата не применяет.' };
    }
    return { form: true, editable: true, kind: 'online', pill: 'Онлайн',
      text: known + ' Плата забирает изменения при очередной отправке данных, то есть в течение минуты.' };
  }

  const api = { DAYS, ageText, remainingText, timeText, parseTime, dateText, parseDate, daysText, allFields, paramsOf,
                labelOf, formatValue, changesOf, checkNumber, checkOrder, describeChanges, rejectionText, requestStatus, linkState };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.SettingsLib = api;
})();
