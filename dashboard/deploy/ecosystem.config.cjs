'use strict';
// Запуск сервера телеметрии под pm2 (он уже поднимает остальные приложения на VPS).
//   pm2 startOrReload ~/dash/deploy/ecosystem.config.cjs && pm2 save
const path = require('path');

module.exports = {
  apps: [{
    name: 'dash',
    script: 'server.py',
    interpreter: 'python3',
    cwd: path.resolve(__dirname, '..'),
    autorestart: true,
    max_restarts: 20,
    min_uptime: '10s',
    restart_delay: 3000,
    max_memory_restart: '200M',
    time: true,
    // Секреты читает сам server.py из ~/dash/.env (права 600), в конфигурацию pm2 они не попадают.
  }],
};
