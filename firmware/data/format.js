// Мелкие чистые функции для главной страницы: время работы, адрес платы в сети роутера и значок цикла.
// Без DOM, их проверяет tests/format.test.cjs.
(function () {
    'use strict';

    // Время работы: «не включался», «45 с», «12 мин», «2 ч», «2 ч 05 мин». Секунды показываем
    // только до минуты, дальше они только шумят.
    function formatWorkTime(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '--';
        const total = Math.floor(seconds);
        if (total < 1) return 'не включался';
        if (total < 60) return total + ' с';
        const minutes = Math.floor(total / 60);
        if (minutes < 60) return minutes + ' мин';
        const hours = Math.floor(minutes / 60);
        const rest = minutes % 60;
        return hours + ' ч' + (rest ? ' ' + String(rest).padStart(2, '0') + ' мин' : '');
    }

    // Адрес платы в сети роутера для шапки: только пока плата подключена и адрес похож на IPv4
    // (четыре числа через точки). Иначе null, и значок в шапке скрыт.
    function routerAddress(data) {
        if (!data || data.wifi_state !== 'connected') return null;
        const ip = data.wifi_ip;
        return typeof ip === 'string' && /^\d{1,3}(\.\d{1,3}){3}$/.test(ip) ? ip : null;
    }

    // Значок цикла в шапке: значок фазы, текст, цвет (kind) и подсказки. День или ночь передаёт сам значок
    // (☀️ или 🌙), поэтому слово «День» в тексте всегда значит день цикла: «День 160». Так значок короткий
    // и помещается в одну строку шапки на телефоне.
    function cycleView(data) {
        if (data.operation_mode === 1) {
            return { icon: '🍂', label: 'Сушка', text: 'Сушка', kind: 'drying', hint: 'Режим сушки: дня цикла нет' };
        }
        // Без времени плата не знает, день сейчас или ночь, а день цикла не считается
        if (data.clock_usable === false) {
            return { icon: '🕓', label: 'Нет времени', text: 'Нет времени', kind: 'night',
                     hint: 'Время не выставлено: день и ночь неизвестны' };
        }
        const started = data.grow_day > 0;
        const text = started ? 'День ' + data.grow_day : 'Цикл не начат';
        const cycle = started ? 'день цикла ' + data.grow_day : 'цикл не начат';
        return data.is_day
            ? { icon: '☀️', label: 'Сейчас день', text, kind: 'day', hint: 'Сейчас день, ' + cycle }
            : { icon: '🌙', label: 'Сейчас ночь', text, kind: 'night', hint: 'Сейчас ночь, ' + cycle };
    }

    const api = { formatWorkTime, routerAddress, cycleView };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined') window.Format = api;
})();
