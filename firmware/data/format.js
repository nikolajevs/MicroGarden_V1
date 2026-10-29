// Мелкие чистые функции для главной страницы: время работы и адрес платы в сети роутера. Без DOM,
// их проверяет tests/format.test.cjs.
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

    const api = { formatWorkTime, routerAddress };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined') window.Format = api;
})();
