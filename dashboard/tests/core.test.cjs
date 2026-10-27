'use strict';
// Чистые функции страницы: оси, метки времени, разрывы линий. node --test dashboard/tests
const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../static/app.js');

test('niceTicks даёт ровные деления, охватывающие данные', () => {
  assert.deepEqual(core.niceTicks(0, 100, 5).ticks, [0, 25, 50, 75, 100]);
  const t = core.niceTicks(18.3, 26.9, 5);
  assert.ok(t.min <= 18.3 && t.max >= 26.9);
  const steps = t.ticks.slice(1).map((v, i) => +(v - t.ticks[i]).toFixed(9));
  assert.ok(steps.every((s) => s === steps[0]), 'шаг постоянный');
  assert.ok(t.ticks.length >= 3 && t.ticks.length <= 8);
});

test('stepDigits: знаки после запятой для подписей осей', () => {
  assert.equal(core.stepDigits(25), 0);
  assert.equal(core.stepDigits(2.5), 1);
  assert.equal(core.stepDigits(0.5), 1);
  assert.equal(core.stepDigits(0.25), 2);
  assert.equal(core.stepDigits(0.1 + 0.2), 1);
});

test('niceTicks не ломается на одинаковых и неверных значениях', () => {
  const same = core.niceTicks(24, 24, 5);
  assert.ok(same.min < 24 && same.max > 24);
  assert.deepEqual(core.niceTicks(NaN, 1, 5).ticks, [0, 1]);
  const small = core.niceTicks(0, 0.3, 5);
  assert.ok(small.ticks.every((v) => Number.isFinite(v)));
});

test('timeTicks выравнивает метки по местному времени', () => {
  const from = 1700000000;
  for (const offset of [0, 120, -300, 330]) {
    const r = core.timeTicks(from, from + 86400, offset);
    assert.equal(r.step, 21600);
    assert.ok(r.ticks.length >= 3);
    for (const tick of r.ticks) {
      assert.ok(tick.t >= from && tick.t <= from + 86400);
      assert.equal((((tick.t + offset * 60) % r.step) + r.step) % r.step, 0, 'кратно шагу по местному времени');
    }
  }
});

test('timeTicks: подпись полуночи — дата, остальное — время', () => {
  const r = core.timeTicks(1700000000, 1700000000 + 86400, 0);
  const labels = r.ticks.map((t) => core.formatTick(t, r.step, r.offset));
  assert.ok(labels.includes('15.11'), labels.join());
  assert.ok(labels.includes('06:00'), labels.join());
  const week = core.timeTicks(1700000000, 1700000000 + 7 * 86400, 0);
  assert.ok(week.ticks.map((t) => core.formatTick(t, week.step, week.offset)).every((s) => /^\d\d\.\d\d$/.test(s)));
});

test('segments рвёт линию на пропусках и больших разрывах', () => {
  const ts = [0, 60, 120, 180, 1000, 1060];
  const vals = [1, 2, null, 4, 5, 6];
  const seg = core.segments(ts, vals, 150);
  assert.deepEqual(seg, [[[0, 1], [60, 2]], [[180, 4]], [[1000, 5], [1060, 6]]]);
  assert.deepEqual(core.segments([], [], 100), []);
  assert.deepEqual(core.segments([0], [NaN], 100), []);
});

test('flagRuns склеивает соседние интервалы', () => {
  assert.deepEqual(core.flagRuns([0, 60, 120, 300], [1, 1, 0, 1], 60), [[0, 120], [300, 360]]);
  assert.deepEqual(core.flagRuns([0, 60], [0, null], 60), []);
});

test('formatAgo и fmt по-русски', () => {
  assert.equal(core.formatAgo(12), '12 с');
  assert.equal(core.formatAgo(300), '5 мин');
  assert.equal(core.formatAgo(3600), '1 ч');
  assert.equal(core.formatAgo(7500), '2 ч 5 мин');
  assert.equal(core.formatAgo(90000), '1 д 1 ч');
  assert.equal(core.formatAgo(-5), '0 с');
  assert.equal(core.fmt(24.55, 1), '24,6');
  assert.equal(core.fmt(-3.2, 1), '−3,2');
  assert.equal(core.fmt(null, 1), '—');
  assert.equal(core.fmt(undefined, 1), '—');
});

test('nearestIndex находит ближайшую точку', () => {
  const ts = [0, 60, 120, 600];
  assert.equal(core.nearestIndex(ts, -50), 0);
  assert.equal(core.nearestIndex(ts, 80), 1);
  assert.equal(core.nearestIndex(ts, 95), 2);
  assert.equal(core.nearestIndex(ts, 5000), 3);
  assert.equal(core.nearestIndex([], 1), -1);
});
