// Форматирование длительностей для главной страницы. Чистые функции без DOM: их проверяет
// tests/format.test.cjs.
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

    const api = { formatWorkTime };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined') window.Format = api;
})();
