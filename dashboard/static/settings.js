// Страница «Настройки платы»: показывает, что плата сообщила о своих настройках, и отправляет ей изменения через
// сервер. Плата забирает их с очередной отправкой данных и применяет, если это разрешено на ней самой. Пределы и
// подписи приходят от сервера (/api/settings), правила разбора значений лежат в settings-lib.js.
(function () {
  'use strict';

  const Lib = window.SettingsLib;
  const $ = (id) => document.getElementById(id);
  const FAST_MS = 4000;   // запрос ждёт плату: смотрим, не применила ли
  const SLOW_MS = 20000;
  const HISTORY_ROWS = 10;

  const state = {
    device: new URLSearchParams(location.search).get('device') || '',
    data: null,           // последний ответ /api/settings
    builtFor: null,       // для какой платы построена форма
    controls: [],         // поля формы
    sections: [],         // группы формы: { group, section, controls }
    edits: {},            // настройка -> значение, которое человек поставил в форме
    watching: '',         // номер запроса, итог которого ждём, чтобы сказать, чем он кончился
  };
  let timer = 0;

  // Запросы, которые что-то меняют, идут с заголовком X-Dash-Action: чужая страница не может его отправить
  async function api(method, url, body) {
    const headers = {};
    const options = { method, headers, cache: 'no-store', credentials: 'same-origin' };
    if (method !== 'GET') headers['X-Dash-Action'] = '1';
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const res = await fetch(url, options);
    let payload = null;
    try { payload = await res.json(); } catch (e) { /* ответ не JSON */ }
    if (res.status === 401) location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search)); // сессия закончилась
    if (!res.ok) throw new Error((payload && payload.error) || 'ответ сервера ' + res.status);
    return payload;
  }

  function showError(message) {
    const el = $('page-error');
    el.textContent = message || '';
    el.hidden = !message;
  }

  function text(tag, className, value) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = value;
    return el;
  }

  const when = (ts) => new Date(ts * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });

  // ----- поля формы ------------------------------------------------------------------------
  // У каждого поля: params (какие настройки оно меняет), read() -> { values } или { error }, write(values), enable(bool)
  function makeControl(field, id) {
    const params = Lib.paramsOf(field);
    const control = { field, params, touched: false, invalid: '', inputs: [] };

    function input(type) {
      const el = document.createElement('input');
      el.type = type;
      el.id = id;
      control.inputs.push(el);
      return el;
    }

    if (field.type === 'number') {
      const el = input('number');
      el.step = String(field.step || 1);
      el.min = String(field.lo);
      el.max = String(field.hi);
      el.inputMode = 'decimal';
      control.nodes = [el];
      control.read = () => {
        const checked = Lib.checkNumber(field, el.value);
        return checked.ok ? { values: { [field.param]: checked.value } } : { error: checked.error };
      };
      control.write = (values) => { el.value = values[field.param] === undefined ? '' : String(values[field.param]); };
    } else if (field.type === 'select') {
      const el = document.createElement('select');
      el.id = id;
      control.inputs.push(el);
      for (const [value, label] of field.options) el.append(Object.assign(document.createElement('option'), { value: String(value), textContent: label }));
      control.nodes = [el];
      control.read = () => ({ values: { [field.param]: Number(el.value) } });
      control.write = (values) => { el.value = values[field.param] === undefined ? '' : String(values[field.param]); };
    } else if (field.type === 'time') {
      const el = input('time');
      control.nodes = [el];
      control.read = () => {
        const parsed = Lib.parseTime(el.value);
        return parsed ? { values: { [params[0]]: parsed[0], [params[1]]: parsed[1] } } : { error: '«' + field.label + '»: выберите время' };
      };
      control.write = (values) => {
        const [h, m] = params.map((p) => values[p]);
        el.value = typeof h === 'number' && typeof m === 'number' ? Lib.timeText(h, m) : '';
      };
    } else if (field.type === 'date') {
      const el = input('date');
      control.nodes = [el];
      control.read = () => {
        const parsed = Lib.parseDate(el.value);
        return parsed === null ? { error: '«' + field.label + '»: неверная дата' } : { values: { [field.param]: parsed } };
      };
      control.write = (values) => { el.value = Lib.dateText(values[field.param]); };
    } else if (field.type === 'days') {
      const row = document.createElement('div');
      row.className = 'days';
      row.id = id;
      const boxes = Lib.DAYS.map((name) => {
        const box = document.createElement('input');
        box.type = 'checkbox';
        const label = document.createElement('label');
        label.className = 'day';
        label.append(box, document.createTextNode(name));
        row.append(label);
        control.inputs.push(box);
        return box;
      });
      control.nodes = [row];
      control.read = () => ({ values: { [field.param]: boxes.reduce((mask, box, i) => mask | (box.checked ? 1 << i : 0), 0) } });
      control.write = (values) => { boxes.forEach((box, i) => { box.checked = !!((values[field.param] || 0) & (1 << i)); }); };
    } else {
      // flag: плата сообщает, менять нельзя
      const el = text('span', 'readonly', '—');
      control.nodes = [el];
      control.readOnly = true;
      control.params = [];
      control.read = () => ({ values: {} });
      control.write = (values) => { el.textContent = Lib.formatValue(field, values); };
    }
    return control;
  }

  function buildForm() {
    const groups = $('groups');
    groups.textContent = '';
    state.controls = [];
    state.sections = [];
    state.edits = {};
    for (const group of state.data.schema) {
      const section = document.createElement('section');
      section.className = 'panel';
      section.append(text('h2', '', group.title));
      const entry = { group, section, controls: [] };
      state.sections.push(entry);
      const grid = document.createElement('div');
      grid.className = 'settings-grid';
      for (const field of group.fields) {
        const id = 'f-' + (field.param || field.params[0]);
        const control = makeControl(field, id);
        const wrap = document.createElement('div');
        wrap.className = 'setting';
        const head = document.createElement('label');
        head.className = 'setting-label';
        head.htmlFor = id;
        head.append(text('span', 'label', field.label));
        if (field.unit) head.append(text('span', 'unit', field.unit));
        wrap.append(head, ...control.nodes);
        control.now = text('span', 'setting-now', '');
        control.problem = text('span', 'setting-problem', '');
        wrap.append(control.now, control.problem);
        if (field.hint) wrap.append(text('span', 'hint', field.hint));
        control.wrap = wrap;
        for (const el of control.inputs) {
          const handler = () => onEdit(control);
          el.addEventListener('input', handler);
          el.addEventListener('change', handler);
        }
        state.controls.push(control);
        entry.controls.push(control);
        grid.append(wrap);
      }
      section.append(grid);
      groups.append(section);
    }
    state.builtFor = state.device;
  }

  // Значения, от которых считаются правки: то, что на плате, и то, что уже ждёт её
  function baseValues() {
    const data = state.data;
    return Object.assign({}, data.values, data.pending ? data.pending.set : {});
  }

  // Режим, по которому показываются группы: что человек выбрал в форме, иначе то, что на плате (и ждёт её)
  function currentMode() {
    return 'operation_mode' in state.edits ? state.edits.operation_mode : baseValues().operation_mode;
  }

  // Прячет группы, которые в этом режиме не работают (в сушке нет света и полива), и пишет, что спрятано. Правки
  // спрятанных полей остаются в памяти, но не отправляются: что не видно, то не меняется.
  function applyMode() {
    const mode = currentMode();
    for (const entry of state.sections) {
      const visible = Lib.groupVisible(entry.group, mode);
      entry.section.hidden = !visible;
      for (const control of entry.controls) control.hidden = !visible;
    }
    const note = Lib.hiddenNote(state.data.schema, mode);
    $('hidden-note').textContent = note;
    $('hidden-note').hidden = !note;
  }

  // Что уйдёт по кнопке «Применить»: правки видимых полей, отличающиеся от значений на плате
  function visibleChanges() {
    return Lib.changesOf(Lib.visibleEdits(state.data.schema, state.edits, currentMode()), baseValues());
  }

  function onEdit(control) {
    const read = control.read();
    const base = baseValues();
    if (read.error) {
      control.invalid = read.error;
      control.touched = true;
    } else {
      control.invalid = '';
      for (const param of control.params) {
        const value = read.values[param];
        if (Math.abs(value - base[param]) < 0.0005) delete state.edits[param];
        else state.edits[param] = value;
      }
      control.touched = control.params.some((p) => p in state.edits);
    }
    refreshControl(control, base);
    applyMode();
    refreshActions();
  }

  function refreshControl(control, base) {
    const changed = control.params.some((p) => p in state.edits);
    const pending = !!state.data.pending && control.params.some((p) => p in state.data.pending.set);
    control.wrap.classList.toggle('changed', changed);
    control.wrap.classList.toggle('pending', pending && !changed);
    control.wrap.classList.toggle('invalid', !!control.invalid);
    control.problem.textContent = control.invalid;
    const reported = Lib.formatValue(control.field, state.data.values);
    control.now.textContent = changed || pending ? 'на плате сейчас: ' + reported + (pending && !changed ? ' · ждёт плату' : '') : '';
    for (const el of control.inputs) el.setAttribute('aria-invalid', control.invalid ? 'true' : 'false');
  }

  // Подставляет в поля то, что на плате (и ждёт её), кроме полей, которые человек сейчас правит
  function syncControls(editable) {
    const base = baseValues();
    for (const control of state.controls) {
      if (!control.touched) control.write(base);
      if (control.readOnly) control.write(state.data.values);
      for (const el of control.inputs) el.disabled = !editable;
      refreshControl(control, base);
    }
    applyMode();
  }

  function refreshActions() {
    const count = Object.keys(visibleChanges()).length;
    const bad = state.controls.find((c) => c.invalid && !c.hidden);
    $('apply').disabled = count === 0 || !!bad || state.busy;
    $('apply').textContent = count ? 'Применить (' + count + ')' : 'Применить';
    $('reset').disabled = !(count || bad) || state.busy;
  }

  // ----- вывод -------------------------------------------------------------------------------
  function renderPending() {
    const data = state.data;
    const panel = $('pending-panel');
    panel.hidden = !data.pending;
    if (!data.pending) return;
    const list = $('pending-list');
    list.textContent = '';
    for (const line of Lib.describeChanges(data.schema, data.pending.set, data.values)) {
      const li = document.createElement('li');
      li.append(text('span', 'name', line.label), text('span', 'delta', line.from + ' → ' + line.to));
      list.append(li);
    }
    const left = data.pending.expires_at - data.now;
    $('pending-note').textContent = 'Запросил ' + data.pending.by + ' в ' + when(data.pending.created_at) + '. Плата заберёт его при очередной отправке данных (раз в минуту), ' +
      Lib.remainingText(left) + ' до отмены.';
  }

  function renderHistory() {
    const data = state.data;
    $('history-panel').hidden = data.history.length === 0;
    const list = $('history');
    list.textContent = '';
    for (const item of data.history.slice(0, HISTORY_ROWS)) {
      const status = Lib.requestStatus(data.schema, item, data.now, data.ttl);
      const li = document.createElement('li');
      li.className = 'history-row';
      const head = document.createElement('div');
      head.className = 'history-head';
      head.append(text('span', 'when', when(item.created_at)), text('span', 'who', item.by || '—'), text('span', 'badge ' + status.kind, status.text));
      li.append(head);
      const to = Object.assign({}, data.values, item.set);
      const what = Lib.describeChanges(data.schema, item.set, to).map((line) => line.label + ': ' + line.to).join('; ');
      li.append(text('div', 'meta', what));
      list.append(li);
    }
  }

  // Чем кончился запрос, который мы ждали
  function reportOutcome() {
    const data = state.data;
    if (!state.watching || data.pending) return;
    const item = data.history.find((h) => h.id === state.watching);
    state.watching = '';
    if (!item) return;
    const status = Lib.requestStatus(data.schema, item, data.now, data.ttl);
    const note = $('apply-note');
    note.textContent = item.status === 'applied' ? '✓ Плата применила изменения.' : status.text + '.';
    note.classList.toggle('bad', item.status !== 'applied');
  }

  function render() {
    const data = state.data;
    if (state.builtFor !== state.device || !state.controls.length) buildForm();
    const link = Lib.linkState(data);
    $('sub').textContent = data.name + ' · ' + data.device;
    document.title = 'MicroGarden · Настройки: ' + data.name;
    $('link-pill').className = 'badge ' + link.kind;
    $('link-pill').textContent = link.pill;
    $('link-text').textContent = link.text;
    $('groups').hidden = !link.form;
    $('form-actions').hidden = !link.editable;
    syncControls(link.editable);
    refreshActions();
    renderPending();
    renderHistory();
    reportOutcome();
  }

  // ----- загрузка ---------------------------------------------------------------------------
  async function loadDevices() {
    const data = await api('GET', '/api/devices');
    const list = (data && data.devices) || [];
    const select = $('device');
    select.textContent = '';
    for (const d of list) select.append(Object.assign(document.createElement('option'), { value: d.device, textContent: d.name || d.device }));
    select.hidden = list.length < 2;
    if (!list.length) return false;
    if (!state.device || !list.some((d) => d.device === state.device)) state.device = list[0].device;
    select.value = state.device;
    return true;
  }

  async function load() {
    state.data = await api('GET', '/api/settings?device=' + encodeURIComponent(state.device));
    state.device = state.data.device;
    showError('');
    render();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(tick, state.data && state.data.pending ? FAST_MS : SLOW_MS);
  }

  async function tick() {
    if (!document.hidden) {
      try { await load(); } catch (err) { showError('Не удалось обновить: ' + err.message); }
    }
    schedule();
  }

  // ----- действия ---------------------------------------------------------------------------
  async function apply(evt) {
    evt.preventDefault();
    const base = baseValues();
    const patch = visibleChanges();
    if (!Object.keys(patch).length) return;
    const bad = state.controls.find((c) => c.invalid && !c.hidden);
    const note = $('apply-note');
    note.classList.remove('bad');
    if (bad) { note.textContent = bad.invalid; note.classList.add('bad'); return; }
    const orderError = Lib.checkOrder(Object.assign({}, base, patch));
    if (orderError) { note.textContent = orderError; note.classList.add('bad'); return; }
    state.busy = true;
    refreshActions();
    try {
      const result = await api('POST', '/api/settings', { device: state.device, set: patch });
      state.edits = {};
      for (const control of state.controls) control.touched = false;
      state.watching = result.pending ? result.pending.id : '';
      note.textContent = result.pending ? 'Отправлено. Плата применит изменения при очередной отправке данных (в течение минуты).' : 'Запрос больше не нужен: на плате уже так.';
      await load();
    } catch (err) {
      note.textContent = err.message;
      note.classList.add('bad');
    } finally {
      state.busy = false;
      refreshActions();
      schedule();
    }
  }

  function reset() {
    state.edits = {};
    for (const control of state.controls) { control.touched = false; control.invalid = ''; }
    $('apply-note').textContent = '';
    syncControls(true);
    refreshActions();
  }

  async function cancel() {
    const button = $('cancel');
    button.disabled = true;
    try {
      await api('POST', '/api/settings/cancel', { device: state.device });
      state.watching = '';
      $('apply-note').textContent = 'Запрос отменён.';
      await load();
    } catch (err) {
      showError(err.message);
    } finally {
      button.disabled = false;
      schedule();
    }
  }

  async function start() {
    $('settings-form').addEventListener('submit', apply);
    $('reset').addEventListener('click', reset);
    $('cancel').addEventListener('click', cancel);
    $('device').addEventListener('change', (evt) => {
      state.device = evt.target.value;
      history.replaceState(null, '', '/settings?device=' + encodeURIComponent(state.device));
      state.builtFor = null;
      tick();
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
    try {
      if (!(await loadDevices())) {
        $('sub').textContent = 'Нет плат';
        $('link-pill').textContent = 'Нет данных';
        $('link-text').textContent = 'Плата ещё не присылала данные. Добавьте её на странице «Платы» и введите выданные адрес, имя и токен в её настройках.';
        return;
      }
      await load();
    } catch (err) {
      showError('Не удалось загрузить настройки: ' + err.message);
    }
    schedule();
  }

  start();
})();
