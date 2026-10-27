const {test} = require('node:test');
const assert = require('node:assert/strict');
const {formatWorkTime} = require('../firmware/data/format.js');

test('время работы: секунды, минуты, часы', () => {
  assert.equal(formatWorkTime(0), 'не включался');
  assert.equal(formatWorkTime(0.4), 'не включался');
  assert.equal(formatWorkTime(1), '1 с');
  assert.equal(formatWorkTime(59), '59 с');
  assert.equal(formatWorkTime(60), '1 мин');
  assert.equal(formatWorkTime(754), '12 мин');
  assert.equal(formatWorkTime(3599), '59 мин');
  assert.equal(formatWorkTime(3600), '1 ч');
  assert.equal(formatWorkTime(7500), '2 ч 05 мин');
  assert.equal(formatWorkTime(86399), '23 ч 59 мин');
});

test('время работы: неверные значения не ломают страницу', () => {
  for (const bad of [undefined, null, 'x', NaN, Infinity, -5]) assert.equal(formatWorkTime(bad), '--');
});
