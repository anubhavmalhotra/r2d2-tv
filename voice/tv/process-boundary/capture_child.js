'use strict';
const processBoundary = require('./joined_process');
// Only declared flat booleans, bounded integers, nulls, and fixed enums escape.
function schema(fields) {
  if (!fields || typeof fields !== 'object' || Array.isArray(fields) || Object.keys(fields).length < 1 || Object.keys(fields).length > 32) throw Error('RESULT_SCHEMA');
  const copy = {};
  for (const [name,rule] of Object.entries(fields)) {
    if (!/^[a-z][a-zA-Z0-9]{0,39}$/.test(name)) throw Error('RESULT_SCHEMA');
    if (rule === 'boolean' || rule === 'null') copy[name] = rule;
    else if (rule && Object.keys(rule).sort().join(',') === 'max,min' && Number.isSafeInteger(rule.min) && Number.isSafeInteger(rule.max) && rule.min >= -2147483647 && rule.max <= 2147483647 && rule.min <= rule.max) copy[name] = {min:rule.min,max:rule.max};
    else if (rule && Object.keys(rule).join(',') === 'enum' && Array.isArray(rule.enum) && rule.enum.length <= 32 && rule.enum.length > 0 && rule.enum.every(x => x === null || typeof x === 'string' && /^[A-Za-z0-9_-]{1,40}$/.test(x))) copy[name] = {enum:rule.enum.slice()};
    else throw Error('RESULT_SCHEMA');
  }
  return copy;
}
function captureChild(config, hooks = {}) {
  const fields = schema(config && config.resultFields), keys = Object.keys(fields).sort().join(',');
  if (hooks.validateResult !== undefined && typeof hooks.validateResult !== 'function') throw Error('RESULT_SCHEMA');
  return processBoundary.run({...config,termination:'term-kill'}, value => {
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== keys) return null;
    const projected = {};
    for (const [name,rule] of Object.entries(fields)) {
      const x = value[name];
      const valid = rule === 'boolean' ? typeof x === 'boolean' : rule === 'null' ? x === null : rule.enum ? rule.enum.includes(x) : Number.isSafeInteger(x) && x >= rule.min && x <= rule.max;
      if (!valid) return null;
      projected[name] = x;
    }
    Object.freeze(projected);
    if (hooks.validateResult && hooks.validateResult(projected) !== true) return null;
    return projected;
  },hooks);
}
module.exports = {captureChild};
