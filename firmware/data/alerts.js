// Оповещения главной страницы: активные неисправности платы, собранные из ответа /api/status.
// Это текущие состояния, а не журнал событий: оповещение исчезает, когда причина устранена.
// Прошлые события (перезагрузки, сбои, пропущенные запуски) остаются на вкладке «Консоль».
// Чистые функции без DOM; их проверяют тесты в Node (tests/alerts.test.cjs).
(function () {
    'use strict';

    const BAD = 'bad';   // что-то не работает: красная отметка
    const WARN = 'warn'; // работает, но с ограничением: жёлтая отметка

    // Список оповещений: сначала тяжёлые, внутри уровня — в порядке проверки.
    // Незнакомое или отсутствующее поле (старая прошивка) оповещения не создаёт.
    function collectAlerts(d) {
        if (!d) return [];
        const alerts = [];
        const drying = d.operation_mode === 1; // в сушке нет света и полива
        const add = (id, level, text) => alerts.push({ id, level, text });

        if (d.sht_online === false) {
            // Без датчика свет не гаснет, а идёт на минимальном уровне (если его не заблокировало питание или время)
            const lightNote = !drying && !d.led_supply_blocked && d.clock_usable !== false
                ? ', свет на минимальном уровне' : '';
            add('sht', BAD, 'Датчик SHT4x не отвечает: климат держится на безопасных уровнях, обогрев выключен' + lightNote);
        }

        if (d.clock_trusted === false) {
            const reason = d.rtc_status === 'no_response' ? 'Часы RTC не отвечают'
                : d.rtc_status === 'not_set' ? 'Часы не выставлены'
                : 'Проверка часов';
            if (d.clock_usable) {
                add('clock', WARN, reason + ': время идёт по программному таймеру до перезагрузки' +
                    (drying ? '' : ', свет и полив работают'));
            } else {
                // Без времени неизвестно, день сейчас или ночь: свет не включается (LiteControl.h, lightScheduleDay)
                add('clock', BAD, reason + (drying ? '' : ': свет и полив выключены, пока время не выставлено'));
            }
        }

        if (d.power_ok === true && d.led_supply_blocked) {
            add('power-low', BAD, drying
                ? 'Напряжение питания ниже 21,6 В'
                : 'Свет выключен: напряжение питания ниже 21,6 В, включение разрешится от 22,5 В');
        } else if (d.power_ok === false) {
            if (d.led_supply_blocked && !drying) {
                add('power-lost', BAD, 'Свет выключен: нет данных питания и нет времени (часы не выставлены)');
            } else {
                add('power-lost', WARN, drying
                    ? 'Измеритель питания не отвечает'
                    : d.sht_online === false
                        ? 'Измеритель питания не отвечает, датчик температуры тоже: свет работает на минимальном уровне, защиты от низкого напряжения нет'
                        : 'Измеритель питания не отвечает: свет работает без защиты от низкого напряжения');
            }
        }

        if (!drying && d.water_sensor_enabled && d.water_present === false) {
            add('water', WARN, 'В баке нет воды: автополив пропустит запуск');
        }
        if (!drying && d.last_water_skipped) {
            add('watering-skipped', WARN, 'Последний полив по расписанию пропущен');
        }
        if (d.sd_ok === false) {
            add('sd', WARN, 'SD-карта не обнаружена: журнал за день не записывается');
        }

        return alerts.filter(a => a.level === BAD).concat(alerts.filter(a => a.level === WARN));
    }

    // Что показать на колокольчике: число и цвет отметки (bad, если есть хоть одно тяжёлое).
    function summarizeAlerts(alerts) {
        const count = alerts.length;
        return { count, level: count === 0 ? 'none' : alerts.some(a => a.level === BAD) ? BAD : WARN };
    }

    // Нет связи с самой платой: показания на странице устарели, остальные оповещения недостоверны.
    const LINK_LOST_ALERT = { id: 'link', level: BAD, text: 'Нет связи с платой: показания на странице устарели' };

    const api = { collectAlerts, summarizeAlerts, LINK_LOST_ALERT };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined') window.Alerts = api;
})();
