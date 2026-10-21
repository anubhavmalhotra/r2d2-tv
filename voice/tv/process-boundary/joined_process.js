'use strict';
// No action at import. The caller supplies an already-authorized executable.
const cp = require('child_process');
function need(value) { if (!value) throw Error('PROCESS_CONFIG'); }
function command(config) {
  need(config && typeof config.executable === 'string' && config.executable.startsWith('/') && config.executable.length <= 4096 && !config.executable.includes('\0'));
  need(Array.isArray(config.args) && config.args.length <= 32 && config.args.every(x => typeof x === 'string' && x.length <= 8192 && !x.includes('\0')));
  need(config.cwd === undefined || typeof config.cwd === 'string' && config.cwd.startsWith('/') && config.cwd.length <= 4096 && !config.cwd.includes('\0'));
  const env = config.env || {LANG:'C', LC_ALL:'C'};
  need(typeof env === 'object' && !Array.isArray(env) && Object.keys(env).length <= 64 && Object.entries(env).every(([k,v]) => /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(k) && typeof v === 'string' && v.length <= 8192 && !v.includes('\0')));
  const extraFds = config.extraFds || [];
  need(Array.isArray(extraFds) && extraFds.length <= 16 && extraFds.every(x => Number.isInteger(x) && x >= 3 && x <= 1048576));
  need(Number.isInteger(config.deadlineMs) && config.deadlineMs >= 10 && config.deadlineMs <= 60000);
  need(config.input === null || Buffer.isBuffer(config.input) && config.input.length <= 16384);
  need(config.termination === 'kill' || config.termination === 'term-kill');
  return {executable:config.executable, args:config.args.slice(), env:{...env}, cwd:config.cwd, extraFds:extraFds.slice(), deadlineMs:config.deadlineMs, termination:config.termination};
}
function run(config, decode, hooks = {}) {
  const cmd = command(config);
  need(typeof decode === 'function' && (hooks.spawn === undefined || typeof hooks.spawn === 'function'));
  const spawn = hooks.spawn || cp.spawn;
  const input = config.input === null ? null : Buffer.from(config.input);
  return new Promise(resolve => {
    let child, timer, killTimer, finalTimer, done = false, failed = false, parts = [], used = 0;
    const out = {started:false, childClosed:false, stdoutClosed:false, stderrClosed:false, exitCode:null, signal:null, stdoutBytes:0, stderrBytes:0, timedOut:false, transportError:null, inputWiped:input === null, resultValid:false, accepted:false, result:null};
    function wipeInput() { if (input) input.fill(0); out.inputWiped = true; }
    function finish() {
      if (done) return;
      done = true;
      for (const t of [timer,killTimer,finalTimer]) clearTimeout(t);
      wipeInput();
      let bytes;
      try {
        bytes = Buffer.concat(parts);
        // Never decode partially closed, timed-out, or otherwise failed output.
        if (!failed && out.childClosed && out.stdoutClosed && out.stderrClosed && out.signal === null) {
          const result = decode(JSON.parse(bytes.toString('utf8')));
          if (result !== null) { out.result = result; out.resultValid = true; }
        }
      } catch (_) {} finally {
        if (bytes) bytes.fill(0);
        for (const part of parts) part.fill(0);
        parts = [];
      }
      if (!(out.childClosed && out.stdoutClosed && out.stderrClosed) && out.transportError === null) out.transportError = 'INCOMPLETE_CLOSE';
      out.accepted = !failed && out.started && out.childClosed && out.stdoutClosed && out.stderrClosed && out.exitCode === 0 && out.signal === null && out.resultValid && out.transportError === null;
      resolve(out);
    }
    function settled() { if (out.childClosed && out.stdoutClosed && out.stderrClosed) finish(); }
    function terminate(signal) { if (child && !out.childClosed) try { child.kill(signal); } catch (_) {} }
    function stop(reason) {
      if (done) return;
      failed = true;
      if (out.transportError === null) out.transportError = reason;
      if (reason === 'TIMEOUT') out.timedOut = true;
      terminate(cmd.termination === 'kill' ? 'SIGKILL' : 'SIGTERM');
      if (finalTimer || killTimer) return;
      if (cmd.termination === 'term-kill') killTimer = setTimeout(() => { terminate('SIGKILL'); finalTimer = setTimeout(finish,500); },250);
      else finalTimer = setTimeout(finish,500);
    }
    try {
      child = spawn(cmd.executable,cmd.args,{cwd:cmd.cwd,env:cmd.env,stdio:[input === null ? 'ignore' : 'pipe','pipe','pipe',...cmd.extraFds],shell:false});
      out.started = true;
      timer = setTimeout(() => stop('TIMEOUT'),cmd.deadlineMs);
      if (input) { child.stdin.on('error',() => stop('STDIN')); child.stdin.end(input,wipeInput); }
      child.stdout.on('data',chunk => {
        if (done) { chunk.fill(0); return; }
        used += chunk.length; out.stdoutBytes = Math.min(4097,used);
        if (used > 4096) { chunk.fill(0); stop('OUTPUT_LIMIT'); } else parts.push(chunk);
      });
      child.stderr.on('data',chunk => { if (done) { chunk.fill(0); return; } out.stderrBytes = Math.min(4097,out.stderrBytes + chunk.length); chunk.fill(0); });
      child.stdout.on('error',() => stop('STDOUT'));
      child.stderr.on('error',() => stop('STDERR'));
      child.stdout.on('close',() => { if (!done) { out.stdoutClosed = true; settled(); } });
      child.stderr.on('close',() => { if (!done) { out.stderrClosed = true; settled(); } });
      child.on('error',() => stop('CHILD'));
      child.on('close',(code,signal) => {
        if (done) return;
        out.childClosed = true;
        out.exitCode = Number.isInteger(code) && code >= 0 && code <= 255 ? code : null;
        out.signal = signal === null ? null : ['SIGTERM','SIGKILL'].includes(signal) ? signal : 'other';
        settled();
      });
    } catch (_) { stop('SPAWN'); }
  });
}
module.exports = {run};
