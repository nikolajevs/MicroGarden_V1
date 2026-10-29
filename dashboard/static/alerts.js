// Оповещения панели: что требует внимания прямо сейчас. Это текущие состояния, а не журнал: оповещение
// исчезает, когда причина устранена. История сообщений платы остаётся в разделе «Сообщения платы».
// Чистые функции без DOM; их проверяет dashboard/tests/alerts.test.cjs, а tests/dashboard-alerts-parity.test.cjs
// сверяет правила с главной страницей платы (firmware/data/alerts.js): одна и та же неисправность должна
// выглядеть одинаково и там, и здесь. Меняя правила, меняйте оба файла.
(function () {
  'use strict';

  const BAD = 'bad';   // что-то не работает: красная отметка
  const WARN = 'warn'; // работает, но с ограничением или есть свежие ошибки: жёлтая отметка

  const isFalse = (v) => v === false || v === 0;

  // Неисправности платы по последнему присланному образцу показаний (поля — как в dashboard/README.md).
  // Незнакомое или отсутствующее поле (старая прошивка) оповещения не создаёт.
  function stateAlerts(s) {
    if (!s) return [];
    const alerts = [];
    const drying = s.op_mode === 1; // в сушке нет света и полива
    const add = (id, level, text) => alerts.push({ id, level, text });

    if (isFalse(s.sht_ok)) {
      // Без датчика свет не гаснет, а идёт на минимальном уровне (если его не заблокировало питание или время)
      const lightNote = !drying && !s.led_blocked && s.clock_usable !== false ? ', свет на минимальном уровне' : '';
      add('sht', BAD, 'Датчик SHT4x не отвечает: климат держится на безопасных уровнях, обогрев выключен' + lightNote);
    }

    if (isFalse(s.clock_trusted)) {
      // Плата присылает только «RTC отвечает или нет»: «не отвечает» и «не выставлен» здесь не различить.
      const reason = isFalse(s.rtc_ok) ? 'Часы RTC не отвечают или не выставлены' : 'Проверка часов';
      if (s.clock_usable) {
        add('clock', WARN, reason + ': время идёт по программному таймеру до перезагрузки' +
          (drying ? '' : ', свет и полив работают'));
      } else {
        // Без времени неизвестно, день сейчас или ночь: свет не включается
        add('clock', BAD, reason + (drying ? '' : ': свет и полив выключены, пока время не выставлено'));
      }
    }

    if (s.power_ok === true && s.led_blocked) {
      add('power-low', BAD, drying
        ? 'Напряжение питания ниже 21,6 В'
        : 'Свет выключен: напряжение питания ниже 21,6 В, включение разрешится от 22,5 В');
    } else if (isFalse(s.power_ok)) {
      if (s.led_blocked && !drying) {
        add('power-lost', BAD, 'Свет выключен: нет данных питания и нет времени (часы не выставлены)');
      } else {
        add('power-lost', WARN, drying
          ? 'Измеритель питания не отвечает'
          : isFalse(s.sht_ok)
            ? 'Измеритель питания не отвечает, датчик температуры тоже: свет работает на минимальном уровне, защиты от низкого напряжения нет'
            : 'Измеритель питания не отвечает: свет работает без защиты от низкого напряжения');
      }
    }

    if (!drying && s.water_sensor && isFalse(s.water_present)) {
      add('water', WARN, 'В баке нет воды: автополив пропустит запуск');
    }
    if (!drying && s.water_skipped) {
      add('watering-skipped', WARN, 'Последний полив по расписанию пропущен');
    }
    if (isFalse(s.sd_ok)) {
      add('sd', WARN, 'SD-карта не обнаружена: журнал за день не записывается');
    }
    return alerts;
  }

  // Полный список для колокольчика: связь с платой, неисправности по последним показаниям и свежие ошибки
  // журнала. status — ответ /api/status, ageSec — сколько секунд назад плата присылала данные (страница
  // досчитывает его по своим часам), fmtAgo — форматирование длительности страницы.
  function collectAlerts(status, ageSec, fmtAgo) {
    if (!status) return []; // плата ещё не присылала данных: об этом говорит значок связи
    const ago = fmtAgo || ((sec) => Math.max(1, Math.round(sec / 60)) + ' мин');
    const online = ageSec <= (status.online_sec || 180);
    const items = [];
    if (!online) {
      items.push({ id: 'link', level: ageSec < 3600 ? WARN : BAD, text: 'Нет связи с платой: данные не приходят ' + ago(ageSec) });
    }
    for (const alert of stateAlerts(status.sample)) {
      // Пока плата молчит, её последние показания могли устареть: говорим об этом прямо
      items.push(online ? alert : Object.assign({}, alert, { text: alert.text + ' (по последним данным)' }));
    }
    const errors = status.errors_1h || 0;
    if (errors > 0) {
      items.push({ id: 'log-errors', level: WARN, action: 'log',
        text: 'Ошибки в журнале платы за последний час: ' + errors });
    }
    return items.filter((a) => a.level === BAD).concat(items.filter((a) => a.level === WARN));
  }

  // Что показать на колокольчике: число и цвет отметки (bad, если есть хоть одно тяжёлое).
  function summarizeAlerts(alerts) {
    const count = alerts.length;
    return { count, level: count === 0 ? 'none' : alerts.some((a) => a.level === BAD) ? BAD : WARN };
  }

  // Нет связи с самим сервером панели: показания на странице устарели, остальным оповещениям верить нельзя.
  const SERVER_LOST_ALERT = { id: 'server', level: BAD, text: 'Нет связи с сервером панели: данные на странице устарели' };

  const api = { stateAlerts, collectAlerts, summarizeAlerts, SERVER_LOST_ALERT };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.Alerts = api;
})();
