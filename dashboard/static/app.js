(function () {
  'use strict';

  // ===================================================================================
  // Чистые функции (без DOM): их проверяют тесты в Node, см. dashboard/tests/core.test.cjs
  // ===================================================================================

  // «Красивые» числа для осей: шаг 1, 2, 2,5, 5 × 10^n (2,5 нужен, чтобы проценты шли 0-25-50-75-100).
  function niceNumber(x, round) {
    const exp = Math.floor(Math.log10(x));
    const f = x / Math.pow(10, exp);
    let nf;
    if (round) nf = f < 1.5 ? 1 : f < 2.25 ? 2 : f < 3.5 ? 2.5 : f < 7.5 ? 5 : 10;
    else nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return +(nf * Math.pow(10, exp)).toPrecision(12);
  }

  // Сколько знаков после запятой нужно для подписей оси с таким шагом.
  function stepDigits(step) {
    const s = String(+step.toPrecision(12));
    const dot = s.indexOf('.');
    return dot < 0 ? 0 : s.length - dot - 1;
  }

  function niceTicks(min, max, count) {
    if (!isFinite(min) || !isFinite(max)) return { min: 0, max: 1, step: 1, ticks: [0, 1] };
    if (min === max) {
      const pad = Math.abs(min) * 0.1 || 1;
      min -= pad;
      max += pad;
    }
    const range = niceNumber(max - min, false);
    const step = niceNumber(range / (count - 1), true);
    const lo = Math.floor(min / step + 1e-9) * step;
    const hi = Math.ceil(max / step - 1e-9) * step;
    const ticks = [];
    for (let i = 0; lo + i * step <= hi + step / 2; i++) ticks.push(+(lo + i * step).toFixed(10));
    return { min: ticks[0], max: ticks[ticks.length - 1], step, ticks };
  }

  const TIME_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];

  // Метки времени на оси X, выровненные по местной полуночи. offsetMin — смещение часового
  // пояса в минутах к востоку от UTC (по умолчанию — пояс браузера).
  function timeTicks(from, to, offsetMin) {
    const off = (offsetMin === undefined ? -new Date(from * 1000).getTimezoneOffset() : offsetMin) * 60;
    const span = to - from;
    let step = TIME_STEPS[TIME_STEPS.length - 1];
    for (const s of TIME_STEPS) {
      if (span / s <= 7) { step = s; break; }
    }
    const base = Math.floor((from + off) / 86400) * 86400 - off;
    const ticks = [];
    for (let t = base + Math.ceil((from - base) / step) * step; t <= to; t += step) {
      ticks.push({ t, midnight: (((t + off) % 86400) + 86400) % 86400 === 0 });
    }
    return { step, offset: off, ticks };
  }

  function two(n) { return n < 10 ? '0' + n : '' + n; }

  function formatTick(tick, step, offset) {
    const d = new Date((tick.t + offset) * 1000); // getUTC* даёт местные компоненты
    if (step >= 86400 || tick.midnight) return two(d.getUTCDate()) + '.' + two(d.getUTCMonth() + 1);
    return two(d.getUTCHours()) + ':' + two(d.getUTCMinutes());
  }

  // Делит ряд на непрерывные отрезки: пропуск значения или разрыв по времени больше maxGap
  // рвёт линию, чтобы простой платы не рисовался ровной линией.
  function segments(ts, vals, maxGap) {
    const out = [];
    let cur = [];
    for (let i = 0; i < ts.length; i++) {
      const v = vals[i];
      if (v === null || v === undefined || !isFinite(v)) {
        if (cur.length) out.push(cur);
        cur = [];
        continue;
      }
      if (cur.length && ts[i] - cur[cur.length - 1][0] > maxGap) {
        out.push(cur);
        cur = [];
      }
      cur.push([ts[i], v]);
    }
    if (cur.length) out.push(cur);
    return out;
  }

  // Интервалы, где флаг равен 1: [[начало, конец], ...]; соседние склеиваются.
  function flagRuns(ts, flags, bucket) {
    const runs = [];
    for (let i = 0; i < ts.length; i++) {
      if (!flags[i]) continue;
      const last = runs[runs.length - 1];
      if (last && ts[i] - last[1] <= bucket) last[1] = ts[i] + bucket;
      else runs.push([ts[i], ts[i] + bucket]);
    }
    return runs;
  }

  function formatAgo(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + ' с';
    if (sec < 3600) return Math.floor(sec / 60) + ' мин';
    if (sec < 86400) {
      const m = Math.floor((sec % 3600) / 60);
      return Math.floor(sec / 3600) + ' ч' + (m ? ' ' + m + ' мин' : '');
    }
    const h = Math.floor((sec % 86400) / 3600);
    return Math.floor(sec / 86400) + ' д' + (h ? ' ' + h + ' ч' : '');
  }

  function fmt(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    return v.toFixed(digits).replace('.', ',').replace('-', '−');
  }

  // Ближайший к t индекс во возрастающем массиве ts.
  function nearestIndex(ts, t) {
    let lo = 0, hi = ts.length - 1;
    if (hi < 0) return -1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(ts[lo - 1] - t) <= Math.abs(ts[lo] - t)) lo--;
    return lo;
  }

  const core = { niceNumber, niceTicks, stepDigits, timeTicks, formatTick, segments, flagRuns, formatAgo, fmt, nearestIndex };
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  if (typeof document === 'undefined') return;

  // ===================================================================================
  // Страница
  // ===================================================================================
  const POLL_MS = 30000;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const W = 900, H = 250, PAD_T = 10, PAD_B = 26;
  const $ = (id) => document.getElementById(id);

  const state = { device: null, range: '24h', level: 'error', status: null, fetchedAt: 0, logRows: [], logHasMore: false };

  const CHARTS = {
    climate: {
      el: 'chart-climate',
      series: [
        { key: 'temp', cls: 'temp', label: 'Температура', unit: '°C', axis: 'l', digits: 1 },
        { key: 'hum', cls: 'hum', label: 'Влажность', unit: '%', axis: 'r', digits: 1 },
      ],
      left: { unit: '°C' }, right: { unit: '%' },
    },
    light: {
      el: 'chart-light',
      series: [
        { key: 'led', cls: 'led', label: 'Свет', unit: '%', axis: 'l', digits: 0, scale: (v) => v / 2.55 },
        { key: 'fan1', cls: 'fan1', label: 'Вентилятор 1', unit: '%', axis: 'l', digits: 0, scale: (v) => v / 2.55 },
        { key: 'fan2', cls: 'fan2', label: 'Вентилятор 2', unit: '%', axis: 'l', digits: 0, scale: (v) => v / 2.55 },
      ],
      left: { unit: '%', min: 0, max: 100 },
      bands: [{ key: 'pump', cls: 'band-pump', label: 'Помпа' }, { key: 'heater', cls: 'band-heater', label: 'Нагреватель' }],
    },
    power: {
      el: 'chart-power',
      series: [
        { key: 'power_w', cls: 'power', label: 'Мощность', unit: 'Вт', axis: 'l', digits: 1 },
        { key: 'bus_v', cls: 'volt', label: 'Напряжение шины', unit: 'В', axis: 'r', digits: 2 },
      ],
      left: { unit: 'Вт', min: 0 }, right: { unit: 'В', include: [21, 26] },
      limit: { axis: 'r', value: 21.6, label: '21,6 В — свет отключается' },
    },
  };

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function axisRange(spec, values) {
    let min = spec.min !== undefined ? spec.min : Infinity;
    let max = spec.max !== undefined ? spec.max : -Infinity;
    if (spec.min === undefined || spec.max === undefined) {
      for (const v of values) {
        if (spec.min === undefined && v < min) min = v;
        if (spec.max === undefined && v > max) max = v;
      }
      for (const v of spec.include || []) {
        if (spec.min === undefined && v < min) min = v;
        if (spec.max === undefined && v > max) max = v;
      }
      if (!isFinite(min) || !isFinite(max)) { min = 0; max = 1; }
      const pad = (max - min) * 0.08 || 1;
      if (spec.min === undefined) min -= pad;
      if (spec.max === undefined) max += pad;
      if (spec.min === undefined && min < 0 && values.every((v) => v >= 0)) min = 0;
    }
    return niceTicks(min, max, 5);
  }

  function drawChart(chart, data) {
    const host = $(chart.el);
    host.textContent = '';
    const bucket = data.bucket;
    const series = chart.series.map((s) => ({
      s, vals: (data[s.key] || []).map((v) => (v === null || v === undefined ? null : (s.scale ? s.scale(v) : v))),
    }));
    if (!series.some((x) => x.vals.some((v) => v !== null))) {
      const p = document.createElement('p');
      p.className = 'msg';
      p.textContent = 'Нет данных за выбранный период';
      host.appendChild(p);
      return;
    }

    const hasRight = chart.series.some((s) => s.axis === 'r');
    const padL = 46, padR = hasRight ? 46 : 12;
    const plotW = W - padL - padR, plotH = H - PAD_T - PAD_B;
    const t0 = data.from, t1 = data.to;
    const x = (t) => padL + ((t - t0) / (t1 - t0)) * plotW;

    const axes = {};
    for (const side of ['l', 'r']) {
      const spec = side === 'l' ? chart.left : chart.right;
      if (!spec) continue;
      const values = [];
      for (const { s, vals } of series) if (s.axis === side) for (const v of vals) if (v !== null) values.push(v);
      axes[side] = axisRange(spec, values);
    }
    const y = (side, v) => {
      const a = axes[side];
      return PAD_T + plotH - ((v - a.min) / (a.max - a.min)) * plotH;
    };

    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': chart.series.map((s) => s.label).join(', ') });

    // сетка и подписи осей
    for (const v of axes.l.ticks) {
      const yy = y('l', v).toFixed(1);
      root.appendChild(svg('line', { class: 'grid', x1: padL, x2: W - padR, y1: yy, y2: yy }));
      const t = svg('text', { class: 'axis-l', x: padL - 6, y: +yy + 4 });
      t.textContent = fmt(v, stepDigits(axes.l.step));
      root.appendChild(t);
    }
    if (axes.r) {
      const cls = 't-' + chart.series.find((s) => s.axis === 'r').cls;
      for (const v of axes.r.ticks) {
        const t = svg('text', { class: 'axis-r ' + cls, x: W - padR + 6, y: y('r', v) + 4 });
        t.textContent = fmt(v, stepDigits(axes.r.step));
        root.appendChild(t);
      }
    }
    const tt = timeTicks(t0, t1);
    for (const tick of tt.ticks) {
      const xx = x(tick.t).toFixed(1);
      root.appendChild(svg('line', { class: 'grid', x1: xx, x2: xx, y1: PAD_T, y2: PAD_T + plotH }));
      const t = svg('text', { class: 'axis-x', x: xx, y: H - 8 });
      t.textContent = formatTick(tick, tt.step, tt.offset);
      root.appendChild(t);
    }

    // полосы «помпа» и «нагреватель»
    for (const band of chart.bands || []) {
      for (const run of flagRuns(data.t, data[band.key] || [], bucket)) {
        const xa = Math.max(padL, x(run[0])), xb = Math.min(W - padR, x(run[1]));
        root.appendChild(svg('rect', { class: band.cls, x: xa.toFixed(1), y: PAD_T, width: Math.max(2, xb - xa).toFixed(1), height: plotH }));
      }
    }

    // порог
    if (chart.limit && axes[chart.limit.axis]) {
      const a = axes[chart.limit.axis];
      if (chart.limit.value >= a.min && chart.limit.value <= a.max) {
        const yy = y(chart.limit.axis, chart.limit.value).toFixed(1);
        root.appendChild(svg('line', { class: 'limit', x1: padL, x2: W - padR, y1: yy, y2: yy }));
        const t = svg('text', { class: 'limit-label axis-l', x: W - padR - 4, y: +yy - 4 });
        t.textContent = chart.limit.label;
        root.appendChild(t);
      }
    }

    // линии
    const maxGap = Math.max(bucket, 60) * 2.5;
    for (const { s, vals } of series) {
      for (const seg of segments(data.t, vals, maxGap)) {
        if (seg.length === 1) {
          root.appendChild(svg('circle', { class: 'dot d-' + s.cls, cx: x(seg[0][0]).toFixed(1), cy: y(s.axis, seg[0][1]).toFixed(1), r: 2.5 }));
          continue;
        }
        const d = seg.map((p, i) => (i ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(s.axis, p[1]).toFixed(1)).join('');
        root.appendChild(svg('path', { class: 'line s-' + s.cls, d }));
      }
    }

    const cursor = svg('line', { class: 'cursor', y1: PAD_T, y2: PAD_T + plotH, visibility: 'hidden' });
    root.appendChild(cursor);
    const hit = svg('rect', { x: padL, y: PAD_T, width: plotW, height: plotH, fill: 'transparent' });
    root.appendChild(hit);
    host.appendChild(root);

    const tip = document.createElement('div');
    tip.className = 'tip';
    tip.hidden = true;
    host.appendChild(tip);

    const legend = document.createElement('div');
    legend.className = 'legend';
    for (const s of chart.series) {
      const item = document.createElement('span');
      const key = document.createElement('i');
      key.className = 'dot-key k-' + s.cls;
      item.appendChild(key);
      item.appendChild(document.createTextNode(s.label + ', ' + s.unit));
      legend.appendChild(item);
    }
    host.appendChild(legend);

    function hide() { tip.hidden = true; cursor.setAttribute('visibility', 'hidden'); }
    function move(evt) {
      const box = root.getBoundingClientRect();
      const px = ((evt.clientX - box.left) / box.width) * W;
      const t = t0 + ((px - padL) / plotW) * (t1 - t0);
      const i = nearestIndex(data.t, t);
      if (i < 0 || Math.abs(data.t[i] - t) > Math.max(bucket, 60) * 3) { hide(); return; }
      const xx = x(data.t[i]);
      cursor.setAttribute('x1', xx.toFixed(1));
      cursor.setAttribute('x2', xx.toFixed(1));
      cursor.setAttribute('visibility', 'visible');
      tip.textContent = '';
      const when = document.createElement('div');
      when.className = 'when';
      when.textContent = new Date(data.t[i] * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      tip.appendChild(when);
      for (const { s, vals } of series) {
        const row = document.createElement('div');
        row.className = 'row';
        const key = document.createElement('i');
        key.className = 'dot-key k-' + s.cls;
        row.appendChild(key);
        row.appendChild(document.createTextNode(s.label + ': ' + fmt(vals[i], s.digits) + (vals[i] === null ? '' : ' ' + s.unit)));
        tip.appendChild(row);
      }
      tip.hidden = false;
      const shown = (xx / W) * box.width;
      const width = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(box.width - width, shown + 12 + width > box.width ? shown - width - 12 : shown + 12)) + 'px';
    }
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerdown', move);
    hit.addEventListener('pointerleave', hide);
  }

  // ----- запросы ------------------------------------------------------------------------
  async function getJson(url) {
    const res = await fetch(url, { cache: 'no-store', credentials: 'same-origin' });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }
  const q = (params) => new URLSearchParams(params).toString();

  async function loadDevices() {
    const data = await getJson('/api/devices');
    const list = (data && data.devices) || [];
    const select = $('device');
    select.textContent = '';
    for (const d of list) {
      const o = document.createElement('option');
      o.value = o.textContent = d.device;
      select.appendChild(o);
    }
    select.hidden = list.length < 2;
    if (!list.length) return;
    if (!state.device || !list.some((d) => d.device === state.device)) state.device = list[0].device;
    select.value = state.device;
  }

  // ----- карточки и состояние ----------------------------------------------------------
  function setText(id, text) { $(id).textContent = text; }
  function setNote(id, text, bad) {
    const el = $(id);
    el.textContent = text || ' ';
    el.classList.toggle('bad', !!bad);
  }

  function badge(text, kind) {
    const b = document.createElement('span');
    b.className = 'badge' + (kind ? ' ' + kind : '');
    b.textContent = text;
    return b;
  }

  function renderStatus() {
    const st = state.status;
    const pill = $('pill');
    const cards = $('cards');
    const health = $('health');
    health.textContent = '';
    if (!st) {
      pill.className = 'pill pill-wait';
      pill.textContent = 'Плата ещё не присылала данные';
      cards.classList.add('stale');
      $('sub').textContent = 'Телеметрия платы';
      return;
    }
    const age = st.age + (Date.now() - state.fetchedAt) / 1000;
    const online = age <= (st.online_sec || 180);
    pill.className = 'pill ' + (online ? 'pill-ok' : age < 3600 ? 'pill-warn' : 'pill-bad');
    pill.textContent = online ? 'На связи · данные ' + formatAgo(age) + ' назад' : 'Нет связи с платой · ' + formatAgo(age);
    cards.classList.toggle('stale', !online);
    $('sub').textContent = 'Плата ' + st.device + (st.sample && st.sample.fw ? ' · прошивка ' + st.sample.fw : '');

    const s = st.sample || {};
    const shtBad = s.sht_ok === false;
    setText('v-temp', shtBad ? '—' : fmt(s.temp, 1));
    setText('v-hum', shtBad ? '—' : fmt(s.hum, 1));
    setNote('n-temp', shtBad ? 'датчик не отвечает' : '', shtBad);
    setText('v-led', s.led === undefined ? '—' : fmt(s.led / 2.55, 0));
    setNote('n-led', s.led_blocked ? 'выключен: низкое питание' : '', !!s.led_blocked);
    setText('v-fan', s.fan1 === undefined ? '—' : fmt(s.fan1 / 2.55, 0) + ' / ' + fmt(s.fan2 / 2.55, 0));
    setText('v-power', s.power_w === undefined ? '—' : fmt(s.power_w, s.power_w < 10 ? 1 : 0));
    setNote('n-power', s.bus_v === undefined ? 'нет данных измерителя' : fmt(s.bus_v, 1) + ' В · ' + fmt(s.current_a, 2) + ' А', s.bus_v === undefined);
    setText('v-energy', s.energy_kwh === undefined ? '—' : fmt(s.energy_kwh, 2));
    setText('v-pump', s.pump === undefined ? '—' : s.pump ? 'Включена' : 'Выключена');
    setText('v-heater', s.heater === undefined ? '—' : s.heater ? 'Включён' : 'Выключен');
    if (s.water_sensor === false) setNote('n-water', 'датчик воды выключен');
    else if (s.water_present === false) setNote('n-water', 'в баке нет воды', true);
    else setNote('n-water', s.water_present ? 'вода в баке есть' : '');

    if (s.sht_ok !== undefined) health.appendChild(badge(s.sht_ok ? 'Датчик SHT4x' : 'Датчик SHT4x: нет ответа', s.sht_ok ? 'ok' : 'bad'));
    if (s.rtc_ok !== undefined) {
      if (!s.rtc_ok) health.appendChild(badge('Часы RTC: нет ответа', 'bad'));
      else if (s.clock_trusted === false) health.appendChild(badge('Часы не выставлены', 'warn'));
      else health.appendChild(badge('Часы RTC', 'ok'));
    }
    if (s.sd_ok !== undefined) health.appendChild(badge(s.sd_ok ? 'SD-карта' : 'SD-карта: нет', s.sd_ok ? 'ok' : 'warn'));
    if (s.power_ok !== undefined) health.appendChild(badge(s.power_ok ? 'Измеритель INA238' : 'INA238: нет данных', s.power_ok ? 'ok' : 'warn'));
    if (s.rssi !== undefined) health.appendChild(badge('Wi-Fi ' + fmt(s.rssi, 0) + ' дБм'));
    if (s.uptime_s !== undefined) health.appendChild(badge('Работает ' + formatAgo(s.uptime_s)));
    setText('cnt-error', st.errors_24h ? String(st.errors_24h) : '');
  }

  // ----- журнал --------------------------------------------------------------------------
  function renderLog() {
    const list = $('log');
    list.textContent = '';
    const today = new Date().toDateString();
    for (const row of state.logRows) {
      const li = document.createElement('li');
      li.className = row.level;
      const time = document.createElement('time');
      const d = new Date(row.ts * 1000);
      time.dateTime = d.toISOString();
      time.textContent = (d.toDateString() === today ? '' : d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ') +
        d.toLocaleTimeString('ru-RU');
      const msg = document.createElement('span');
      msg.className = 'msg';
      msg.textContent = row.line;
      li.appendChild(time);
      li.appendChild(msg);
      list.appendChild(li);
    }
    const empty = $('log-empty');
    empty.hidden = state.logRows.length > 0;
    empty.className = state.level === 'error' ? 'empty' : 'empty plain';
    empty.textContent = state.level === 'error' ? 'Ошибок нет' : 'Сообщений нет';
    $('more').hidden = !state.logHasMore;
  }

  async function loadLog(append) {
    if (!state.device) return;
    const params = { device: state.device, level: state.level, limit: 100 };
    if (append && state.logRows.length) params.before = state.logRows[state.logRows.length - 1].id;
    const data = await getJson('/api/logs?' + q(params));
    const rows = (data && data.rows) || [];
    state.logRows = append ? state.logRows.concat(rows) : rows;
    state.logHasMore = rows.length === 100;
    renderLog();
  }

  // ----- обновление ---------------------------------------------------------------------
  function csvLink() {
    const span = { '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000 }[state.range];
    const now = Math.floor(Date.now() / 1000);
    $('csv').href = '/api/export.csv?' + q({ device: state.device || '', from: now - span, to: now + 1 });
  }

  async function refresh() {
    if (document.hidden && state.fetchedAt) return;
    try {
      await loadDevices();
      if (!state.device) {
        state.status = null;
        renderStatus();
        return;
      }
      const [status, series] = await Promise.all([
        getJson('/api/status?' + q({ device: state.device })),
        getJson('/api/series?' + q({ device: state.device, range: state.range })),
      ]);
      state.status = status;
      state.fetchedAt = Date.now();
      renderStatus();
      csvLink();
      if (series) for (const key in CHARTS) drawChart(CHARTS[key], series);
      await loadLog(false);
    } catch (err) {
      const pill = $('pill');
      pill.className = 'pill pill-bad';
      pill.textContent = 'Нет связи с сервером';
      console.error(err);
    }
  }

  function remember(key, value) {
    try { localStorage.setItem('dash.' + key, value); } catch (e) { /* приватный режим */ }
  }
  function recall(key, allowed, fallback) {
    try {
      const v = localStorage.getItem('dash.' + key);
      return allowed.indexOf(v) >= 0 ? v : fallback;
    } catch (e) { return fallback; }
  }

  function bindGroup(id, attr, onPick) {
    const group = $(id);
    group.addEventListener('click', (evt) => {
      const btn = evt.target.closest('button');
      if (!btn || !group.contains(btn)) return;
      for (const b of group.querySelectorAll('button')) b.classList.toggle('on', b === btn);
      onPick(btn.getAttribute(attr));
    });
  }
  function markGroup(id, attr, value) {
    for (const b of $(id).querySelectorAll('button')) b.classList.toggle('on', b.getAttribute(attr) === value);
  }

  function start() {
    state.range = recall('range', ['6h', '24h', '7d', '30d'], '24h');
    state.level = recall('level', ['error', 'warn', 'all'], 'error');
    markGroup('range-group', 'data-range', state.range);
    markGroup('level-group', 'data-level', state.level);
    bindGroup('range-group', 'data-range', (v) => { state.range = v; remember('range', v); refresh(); });
    bindGroup('level-group', 'data-level', (v) => { state.level = v; remember('level', v); loadLog(false).catch(console.error); });
    $('device').addEventListener('change', (evt) => { state.device = evt.target.value; refresh(); });
    $('more').addEventListener('click', () => loadLog(true).catch(console.error));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    refresh();
    setInterval(refresh, POLL_MS);
    setInterval(() => { if (state.status) renderStatus(); }, 5000);
  }

  start();
})();
