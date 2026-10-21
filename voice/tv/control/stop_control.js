'use strict';
// Stop-only current-attempt receiver. No URL, listener, device or start method.
const crypto = require('crypto');
const LIMIT = 384, BODY_LIMIT = 512, POLL_MS = 200, RESPONSE_MS = 500;
const GRACE_MS = 1500;
const DOMAIN = 'r2d2-stop-control-v1\n';
function need(v, c) { if (!v) throw Error(c); }
function message(kind, attempt, counter, state) {
  return DOMAIN + kind + '\n' + attempt + '\n' + counter + (state ? '\n' + ['released','cancelled','closed'].map(k => state[k] ? '1' : '0').join('') : '');
}
function create(secret, attempt, transport, clock = {now: () => Number(process.hrtime.bigint()/1000000n), setTimeout, clearTimeout}) {
  need(Buffer.isBuffer(secret) && secret.length === 32 && /^[a-f0-9]{48}$/.test(attempt), 'CONTROL_CONFIG');
  need(transport && typeof transport.request === 'function', 'CONTROL_CONFIG');
  const request = transport.request, key = Buffer.from(secret), started = clock.now();
  let last = started, counter = 0, at = null, pending = null, released = false, cancelled = false, closed = false, failure = null, firstMs = null;
  let authenticatedAt = null, missing = null;
  function fail(code) { if (failure === null) failure = code; cancelled = true; }
  function now() { const n = clock.now(); if (!Number.isSafeInteger(n) || n < last || n - started >= 45000) { fail('CONTROL_EXPIRED'); return null; } last = n; if (missing !== null && n - authenticatedAt >= GRACE_MS) { fail(missing); return null; } return n; }
  function snapshot() { return {released, cancelled, closed, reason: cancelled ? 'cancel' : null}; }
  // A missing poll may keep an active capture alive briefly, but cannot admit
  // READY or START through the existing remaining-budget checks.
  function remainingMs() { const n=now(); return n===null||closed||failure!==null||cancelled||released||missing!==null ? 0 : Math.max(0,45000-(n-started)); }
  async function refresh(force = false) {
    if (closed || failure !== null) return snapshot();
    const n = now();
    if (n === null) return snapshot();
    if (!force && at !== null && n - at < POLL_MS) return snapshot();
    if (pending !== null) { fail('CONTROL_CONCURRENT'); return snapshot(); }
    if (++counter > LIMIT) { fail('CONTROL_LIMIT'); return snapshot(); }
    let body = null, reply = null, call = null, timer = null, finish = null, stage = 'transport';
    const sentAt = n, number = counter;
    try {
      need(transport.request === request, 'CONTROL_BINDING');
      const row = {v:1, attempt, counter};
      row.mac = crypto.createHmac('sha256', key).update(message('request', attempt, counter)).digest('hex');
      body = Buffer.from(JSON.stringify(row));
      let done = false;
      const result = new Promise(resolve => {
        finish = value => { if (done) { if (Buffer.isBuffer(value)) value.fill(0); return; } done = true; resolve(value); };
        timer = clock.setTimeout(() => finish(null), missing === null ? RESPONSE_MS : Math.min(RESPONSE_MS, GRACE_MS - (n - authenticatedAt)));
      });
      pending = () => finish(null);
      // A source-bound adapter supplies this operation using the already owned
      // per-attempt handler. Its cancellation must close the request resources.
      call = request.call(transport, body, {timeoutMs:RESPONSE_MS, maxBytes:BODY_LIMIT});
      need(call && call.promise && typeof call.promise.then === 'function' && typeof call.cancel === 'function', 'CONTROL_TRANSPORT');
      call.promise.then(finish, () => finish(null));
      reply = await result;
      if (number === 1) { const t = clock.now(); firstMs = Number.isSafeInteger(t) && t >= sentAt ? Math.min(60000, t - sentAt) : null; }
      need(now() !== null && !closed && failure === null, 'CONTROL_EXPIRED');
      need(Buffer.isBuffer(reply) && reply.length > 0 && reply.length <= BODY_LIMIT, 'CONTROL_REPLY');
      stage = 'reply';
      const text = reply.toString('ascii');need(Buffer.from(text,'ascii').equals(reply),'CONTROL_REPLY');const v = JSON.parse(text);
      need(v && !Array.isArray(v) && Object.keys(v).sort().join(',') === 'attempt,cancelled,closed,counter,mac,released,v'
        && v.v === 1 && v.attempt === attempt && Number.isSafeInteger(v.counter) && v.counter === counter
        && ['released','cancelled','closed'].every(k => typeof v[k] === 'boolean')
        && typeof v.mac === 'string' && /^[a-f0-9]{64}$/.test(v.mac)
        && JSON.stringify(v) === text, 'CONTROL_REPLY');
      const expected = crypto.createHmac('sha256', key).update(message('response', attempt, counter, v)).digest();
      const supplied = Buffer.from(v.mac, 'hex');
      const authentic = crypto.timingSafeEqual(expected, supplied); expected.fill(0); supplied.fill(0);
      stage = 'auth';
      need(authentic, 'CONTROL_AUTH');
      stage = 'regression';
      need(!released || v.released, 'CONTROL_REGRESSION');
      need(!cancelled || v.cancelled, 'CONTROL_REGRESSION');
      released = released || v.released; cancelled = cancelled || v.cancelled || v.closed; at = last;
      authenticatedAt = last; missing = null;
    } catch (_) {
      const code = classify(number, stage, call);
      let d = null; try { d = call && typeof call.diagnostic === 'function' ? call.diagnostic() : null; } catch (_) {}
      // Only a positively classified transport loss after authentication may
      // retry. A malformed/replayed/authentication response is never a miss.
      if (authenticatedAt !== null && stage === 'transport' && !Buffer.isBuffer(reply)
          && d && (d.cause === 'timeout' || d.cause === 'socket')
          && !closed && !cancelled && !released && failure === null) {
        missing = code; at = sentAt;
        if (last - authenticatedAt >= GRACE_MS) fail(code);
      } else fail(code);
    }
    finally {
      if (timer !== null) clock.clearTimeout(timer);
      if (call && typeof call.cancel === 'function') { try { if (call.cancel() !== true) fail('CONTROL_CLEANUP'); } catch (_) { fail('CONTROL_CLEANUP'); } }
      if (Buffer.isBuffer(body)) body.fill(0);
      if (Buffer.isBuffer(reply)) reply.fill(0);
      pending = null;
    }
    return snapshot();
  }
  // Fixed per-stage codes ([A-Z_], <=40 chars, accepted by every receipt validator).
  function classify(number, stage, call) {
    if (closed) return 'CONTROL_UNCONFIRMED';  // local close during a request: unchanged code
    let kind = 'UNCONFIRMED';
    if (stage === 'reply') kind = 'REPLY'; else if (stage === 'auth') kind = 'AUTH'; else if (stage === 'regression') kind = 'REGRESSION';
    else {
      let d = null; try { d = call && typeof call.diagnostic === 'function' ? call.diagnostic() : null; } catch (_) { d = null; }
      if (d && d.cause === 'status') kind = d.status === 400 ? 'BAD_REQUEST' : 'HTTP_STATUS';
      else if (d && d.cause === 'socket') kind = 'SOCKET';
      else if (d && d.cause === 'size') kind = 'REPLY';
      else if (!d || d.cause === 'timeout' || d.cause === 'aborted' || d.cause === null) kind = 'TIMEOUT';
    }
    return (number === 1 ? 'CONTROL_FIRST_' : 'CONTROL_') + kind;
  }
  function close() {
    if (closed) return;
    closed = true; cancelled = true;
    if (pending !== null) pending();
    key.fill(0);
  }
  return {refresh, snapshot, remainingMs, close, summary: () => ({schema:1, requests:Math.min(counter,LIMIT), closed,
    keyCleared:closed && key.every(x => x === 0), failure, firstMs, nativeArmed:false})};
}
module.exports = {create, message, LIMIT, BODY_LIMIT, POLL_MS, RESPONSE_MS, GRACE_MS};
