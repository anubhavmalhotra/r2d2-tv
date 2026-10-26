'use strict';

const fs = require('fs');
const cp = require('child_process');
const crypto = require('crypto');
const path = require('path');

const BLOCK_BEGIN = '# BEGIN R2D2-LG-BLOCK';
const BLOCK_END = '# END R2D2-LG-BLOCK';

const LG_PERMANENT_BLOCK_DOMAINS = Object.freeze([
  'snu.lge.com',
  'snu-dev.lge.com',
  'su.lge.com',
  'su-dev.lge.com',
  'su-ssl.lge.com',
  'fota.lge.com',
  'redirect.lgsmartplatform.com',
  'lgsmartplatform.com',
  'us.lgsmartplatform.com',
  'aic.lgsmartplatform.com',
  'eic.lgsmartplatform.com',
  'kic.lgsmartplatform.com',
  'ad.lgsmartad.com',
  'us.ad.lgsmartad.com',
  'qt-us.ad.lgsmartad.com',
  'aic.ad.lgsmartad.com',
  'eic.ad.lgsmartad.com',
  'kic.ad.lgsmartad.com',
  'info.lgsmartad.com',
  'us.info.lgsmartad.com',
  'aic.info.lgsmartad.com',
  'eic.info.lgsmartad.com',
  'kic.info.lgsmartad.com',
  'cdpbeacon.lgtvcommon.com',
  'homeprv.lgtvcommon.com',
  'lgtvcommon.com',
  'rdx.lgtvsdp.com',
  'us.rdx.lgtvsdp.com',
  'aic.rdx.lgtvsdp.com',
  'eic.rdx.lgtvsdp.com',
  'kic.rdx.lgtvsdp.com',
  'rdx2.lgtvsdp.com',
  'us.rdx2.lgtvsdp.com',
  'aic.rdx2.lgtvsdp.com',
  'eic.rdx2.lgtvsdp.com',
  'kic.rdx2.lgtvsdp.com',
  'nudge.lgtvsdp.com',
  'us.nudge.lgtvsdp.com',
  'aic.nudge.lgtvsdp.com',
  'eic.nudge.lgtvsdp.com',
  'kic.nudge.lgtvsdp.com',
  'recommend.lgtvsdp.com',
  'us.recommend.lgtvsdp.com',
  'aic.recommend.lgtvsdp.com',
  'eic.recommend.lgtvsdp.com',
  'kic.recommend.lgtvsdp.com',
  'smartshare.lgtvsdp.com',
  'us.smartshare.lgtvsdp.com',
  'aic.smartshare.lgtvsdp.com',
  'eic.smartshare.lgtvsdp.com',
  'kic.smartshare.lgtvsdp.com',
  'common.lgappstv.com',
  'us.common.lgappstv.com',
  'aic.common.lgappstv.com',
  'eic.common.lgappstv.com',
  'kic.common.lgappstv.com',
  'ibs.lgappstv.com',
  'us.ibs.lgappstv.com',
  'aic.ibs.lgappstv.com',
  'eic.ibs.lgappstv.com',
  'kic.ibs.lgappstv.com',
  'ngfts.lge.com',
  'aic-ngfts.lge.com',
  'us-ngfts.lge.com',
  'eic-ngfts.lge.com',
  'nlpre.lge.com',
  'aic-nlpre.lge.com',
  'us-nlpre.lge.com',
  'voice.lge.com',
  'nlp.lge.com',
  'thinq.lge.com',
  'api.thinq.com',
  'yumenetworks.com'
]);

const LG_APPSTORE_DOMAINS = Object.freeze([
  'lgappstv.com',
  'www.lgappstv.com',
  'us.lgappstv.com',
  'aic.lgappstv.com',
  'eic.lgappstv.com',
  'kic.lgappstv.com',
  'lgtvsdp.com',
  'us.lgtvsdp.com',
  'aic.lgtvsdp.com',
  'eic.lgtvsdp.com',
  'kic.lgtvsdp.com'
]);

const LG_PERMANENT_BLOCK_IPS = Object.freeze(['156.147.69.32']);
const LG_PERMANENT_BLOCK_IPV6S = Object.freeze([
  '::ffff:156.147.69.32',
  '64:ff9b::9c93:4520'
]);

function isValidIpv4(ip) {
  return typeof ip === 'string' && /^(\d{1,3}\.){3}\d{1,3}$/.test(ip.trim());
}

function isValidIpv6(ip) {
  if (typeof ip !== 'string') return false;
  const t = ip.trim().toLowerCase();
  if (!t || t === '::' || t === '::1' || t === '0:0:0:0:0:0:0:0' || t === '0:0:0:0:0:0:0:1') {
    return false;
  }
  if (t.startsWith('fe80:')) return false;
  return t.includes(':') && /^[0-9a-f:.]+$/.test(t);
}

const NEVER_UPDATE_APPS = new Set([
  'local.owner.applibrary',
  'com.webos.app.adapp',
  'com.webos.app.acrcomponent',
  'com.webos.app.acrhdmi1',
  'com.webos.app.acrhdmi2',
  'com.webos.app.acrhdmi3',
  'com.webos.app.acrhdmi4',
  'com.webos.app.acroverlay',
  'com.webos.app.adhdmi1',
  'com.webos.app.adhdmi2',
  'com.webos.app.adhdmi3',
  'com.webos.app.adhdmi4',
  'com.webos.app.adoverlay',
  'com.webos.app.adoverlayex',
  'com.webos.app.renewupdate',
  'com.webos.app.softwareupdate',
  'com.webos.app.videoads'
]);

const IDLE_FOREGROUND_APPS = new Set([
  '',
  'local.owner.applibrary',
  'com.webos.app.screensaver',
  'com.webos.app.home'
]);

const DEFAULT_IDLE_THRESHOLD_MS = 30 * 60 * 1000;
const DEFAULT_INSTALL_TIMEOUT_MS = 45 * 1000;
const DEFAULT_MAX_INSTALL_TIMEOUT_MS = 180 * 1000;
const DEFAULT_VOICE_LEVELING_RECHECK_MS = 60 * 1000;
const DEFAULT_POST_SWITCH_RECHECK_DELAY_MS = 7 * 1000;

const PREFERRED_VOICE_SOUND_MODES = Object.freeze([
  'clearVoice',
  'clearVoicePro',
  'clearVoiceIII',
  'clearVoiceIV'
]);

// Strictly restrict to pure local hardware DSP Clear Voice modes (never AI/cloud/telemetry modes).
const FALLBACK_VOICE_SOUND_MODES = Object.freeze([]);

const VOICE_LEVELING_SOUND_MODES = Object.freeze([
  ...PREFERRED_VOICE_SOUND_MODES
]);

function isAutoVolumeEnabled(val) {
  return val === 'on' || val === true || val === 'true' || val === 1;
}

function isPreferredVoiceSoundMode(val) {
  return typeof val === 'string' && PREFERRED_VOICE_SOUND_MODES.includes(val.trim());
}

function isVoiceLevelingSoundMode(val) {
  return typeof val === 'string' && VOICE_LEVELING_SOUND_MODES.includes(val.trim());
}

function sha256Hex(input) {
  const buf = Buffer.isBuffer(input) ? input : Buffer.from(String(input || ''), 'utf8');
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function stripManagedHostsBlock(rawText) {
  const lines = String(rawText || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let inside = false;
  for (const line of lines) {
    if (line.trim() === BLOCK_BEGIN) {
      inside = true;
      continue;
    }
    if (line.trim() === BLOCK_END) {
      inside = false;
      continue;
    }
    if (!inside) {
      out.push(line);
    }
  }
  while (out.length > 0 && out[out.length - 1] === '') {
    out.pop();
  }
  return out.join('\n') + '\n';
}

function renderBlockedHosts(baseHostsText, options = {}) {
  const allowAppStore = Boolean(options && options.allowAppStore === true);
  const cleanBase = stripManagedHostsBlock(baseHostsText || '127.0.0.1\tlocalhost\n::1\tlocalhost\n');
  const domains = allowAppStore
    ? [...LG_PERMANENT_BLOCK_DOMAINS]
    : [...LG_PERMANENT_BLOCK_DOMAINS, ...LG_APPSTORE_DOMAINS];
  const blockLines = [BLOCK_BEGIN];
  for (const domain of domains) {
    blockLines.push('0.0.0.0 ' + domain);
    blockLines.push(':: ' + domain);
  }
  blockLines.push(BLOCK_END);
  return cleanBase + blockLines.join('\n') + '\n';
}

function buildIptablesCommands(options = {}) {
  const allowAppStore = Boolean(options && options.allowAppStore === true);
  const extraAppStoreIps = Array.isArray(options && options.appStoreIps) ? options.appStoreIps : [];
  const cmds = [
    ['-N', 'R2D2_LG_GUARD'],
    ['-F', 'R2D2_LG_GUARD'],
    ['-D', 'OUTPUT', '-j', 'R2D2_LG_GUARD'],
    ['-I', 'OUTPUT', '1', '-j', 'R2D2_LG_GUARD']
  ];
  const blockedIps = new Set(LG_PERMANENT_BLOCK_IPS);
  if (!allowAppStore) {
    for (const ip of extraAppStoreIps) {
      if (isValidIpv4(ip)) {
        blockedIps.add(ip.trim());
      }
    }
  }
  for (const ip of blockedIps) {
    cmds.push(['-A', 'R2D2_LG_GUARD', '-d', ip, '-j', 'REJECT']);
  }
  cmds.push(['-A', 'R2D2_LG_GUARD', '-j', 'RETURN']);
  return cmds;
}

function buildIp6tablesCommands(options = {}) {
  const allowAppStore = Boolean(options && options.allowAppStore === true);
  const extraAppStoreIps = Array.isArray(options && options.appStoreIps) ? options.appStoreIps : [];
  const cmds = [
    ['-N', 'R2D2_LG_GUARD6'],
    ['-F', 'R2D2_LG_GUARD6'],
    ['-D', 'OUTPUT', '-j', 'R2D2_LG_GUARD6'],
    ['-I', 'OUTPUT', '1', '-j', 'R2D2_LG_GUARD6']
  ];
  const blockedIpv6s = new Set(LG_PERMANENT_BLOCK_IPV6S);
  if (!allowAppStore) {
    for (const rawIp of extraAppStoreIps) {
      if (typeof rawIp !== 'string') continue;
      const trimmed = rawIp.trim();
      if (isValidIpv4(trimmed)) {
        blockedIpv6s.add('::ffff:' + trimmed);
      } else if (isValidIpv6(trimmed)) {
        blockedIpv6s.add(trimmed.toLowerCase());
      }
    }
  }
  for (const ip6 of blockedIpv6s) {
    cmds.push(['-A', 'R2D2_LG_GUARD6', '-d', ip6, '-j', 'REJECT']);
  }
  cmds.push(['-A', 'R2D2_LG_GUARD6', '-j', 'RETURN']);
  return cmds;
}

function isValidForegroundReply(reply) {
  return Boolean(
    reply &&
      reply.returnValue !== false &&
      typeof reply.appId === 'string'
  );
}

function formatLocalDay(dateObj) {
  const d = dateObj instanceof Date ? dateObj : new Date(dateObj);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function previousLocalDay(dateObj) {
  const d = dateObj instanceof Date ? dateObj : new Date(dateObj);
  const prev = new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1, 12, 0, 0);
  return formatLocalDay(prev);
}

function isForegroundIdle(foregroundAppId) {
  if (foregroundAppId === null || foregroundAppId === undefined) return true;
  return IDLE_FOREGROUND_APPS.has(String(foregroundAppId).trim());
}

function extractInterruptDigest(rawInterruptsText) {
  if (typeof rawInterruptsText !== 'string' || !rawInterruptsText) return '';
  const rel = rawInterruptsText
    .split('\n')
    .filter((l) => /usb|gpio|input|uart|bt|ir|key/i.test(l) && !/xhci|ehci|ohci|uhci/i.test(l))
    .join('|');
  return sha256Hex(rel).slice(0, 16);
}

function createActivityTracker(deps = {}) {
  const nowMs = typeof deps.nowMs === 'function' ? deps.nowMs : () => Date.now();
  const readInterrupts =
    typeof deps.readInterrupts === 'function'
      ? deps.readInterrupts
      : () => {
          try {
            return fs.readFileSync('/proc/interrupts', 'utf8');
          } catch (_) {
            return '';
          }
        };
  const queryForegroundSync =
    typeof deps.queryForegroundSync === 'function' ? deps.queryForegroundSync : () => '';

  let idleSinceMs = nowMs();
  let lastActivityAtMs = idleSinceMs;
  let lastInputDigest = '';
  let lastFgAppId = null;

  function recordObservation(fgAppIdRaw, rawIrqText) {
    const now = nowMs();
    const fgAppId = typeof fgAppIdRaw === 'string' ? fgAppIdRaw.trim() : '';
    const irqDigest = extractInterruptDigest(rawIrqText);
    const fgIdle = isForegroundIdle(fgAppId);
    const digest = fgAppId + ':' + irqDigest;
    const fgChanged = lastFgAppId !== null && fgAppId !== lastFgAppId;
    const digestChanged = lastInputDigest !== '' && digest !== lastInputDigest;
    if (!fgIdle || fgChanged || digestChanged) {
      idleSinceMs = now;
      lastActivityAtMs = now;
    }
    lastFgAppId = fgAppId;
    lastInputDigest = digest;
    return {
      fgAppId,
      fgIdle,
      irqDigest,
      digest,
      idleSinceMs,
      lastActivityAtMs,
      idleDurationMs: Math.max(0, now - idleSinceMs)
    };
  }

  function sampleSync() {
    const fgAppId = queryForegroundSync();
    const rawIrq = readInterrupts();
    return recordObservation(fgAppId, rawIrq);
  }

  async function sampleAsync(callLunaSend) {
    let fgAppId = lastFgAppId !== null ? lastFgAppId : '';
    if (typeof callLunaSend === 'function') {
      try {
        const reply = await callLunaSend(
          'luna://com.webos.applicationManager/getForegroundAppInfo',
          {},
          2500
        );
        if (reply && typeof reply.appId === 'string') {
          fgAppId = reply.appId.trim();
        }
      } catch (_) {}
    } else {
      fgAppId = queryForegroundSync();
    }
    const rawIrq = readInterrupts();
    return recordObservation(fgAppId, rawIrq);
  }

  function markActive() {
    const now = nowMs();
    idleSinceMs = now;
    lastActivityAtMs = now;
  }

  function hasActivitySince(sinceMs) {
    const snap = sampleSync();
    return Boolean(!snap.fgIdle || snap.idleSinceMs > sinceMs || snap.lastActivityAtMs > sinceMs);
  }

  function getState() {
    const now = nowMs();
    return {
      fgAppId: lastFgAppId || '',
      fgIdle: isForegroundIdle(lastFgAppId),
      idleSinceMs,
      lastActivityAtMs,
      idleDurationMs: Math.max(0, now - idleSinceMs)
    };
  }

  return {
    sampleSync,
    sampleAsync,
    markActive,
    hasActivitySince,
    getState,
    recordObservation
  };
}

function consumeUpdaterTrigger(triggerObj, nowDate, currentState = {}) {
  const s = currentState && typeof currentState === 'object' ? { ...currentState } : { schema: 1 };
  const d = nowDate instanceof Date ? nowDate : new Date(nowDate);
  const today = formatLocalDay(d);
  const yesterday = previousLocalDay(d);
  const hour = d.getHours();
  const reqId =
    triggerObj && typeof triggerObj.reqId === 'string' && triggerObj.reqId.trim()
      ? triggerObj.reqId.trim()
      : 'tv-timer-' + d.getTime();
  const rawMode = triggerObj && typeof triggerObj.mode === 'string' ? triggerObj.mode.trim() : 'update';

  if (rawMode === 'check') {
    return { immediate: true, mode: 'check', reqId, targetDay: today, state: s };
  }
  const alreadyRequiresIdleToday = Boolean(s.requiresIdle && s.requiresIdleDay === today);
  if (rawMode === 'update-standby' && hour === 3 && !alreadyRequiresIdleToday) {
    return { immediate: true, mode: 'update-standby', reqId, targetDay: today, state: s };
  }
  if (rawMode === 'update' && hour === 3 && !alreadyRequiresIdleToday) {
    return { immediate: true, mode: 'update', reqId, targetDay: today, state: s };
  }

  // Trigger arrived outside the 3:00 AM window (e.g., Persistent=true systemd timer on wake,
  // delayed header, or maintenance-update-idle): persist missed-window state and require
  // 30-min verified idle before running.
  const deferredDay =
    hour < 3
      ? (s.pendingRetryDay && s.pendingRetryDay < today && s.pendingRetryDay) ||
        (s.missedWindowDay && s.missedWindowDay < today && s.missedWindowDay) ||
        yesterday
      : today;
  s.schema = 1;
  s.missedWindowDay = s.missedWindowDay || deferredDay;
  s.requiresIdle = true;
  s.requiresIdleDay = deferredDay;
  s.pendingReqId = reqId;
  return {
    immediate: false,
    deferredToIdle: true,
    mode: 'update',
    reqId,
    targetDay: deferredDay,
    state: s
  };
}

function shouldRunScheduledUpdate(state, nowDate, context = {}) {
  const s = state && typeof state === 'object' ? state : {};
  const d = nowDate instanceof Date ? nowDate : new Date(nowDate);
  const today = formatLocalDay(d);
  const yesterday = previousLocalDay(d);
  const hour = d.getHours();
  const tvPower = context.tvPower || 'unknown';
  const idleMs = typeof context.idleDurationMs === 'number' ? context.idleDurationMs : 0;
  const thresholdMs =
    typeof context.idleThresholdMs === 'number' && context.idleThresholdMs > 0
      ? context.idleThresholdMs
      : DEFAULT_IDLE_THRESHOLD_MS;
  const fgIdle = isForegroundIdle(context.foregroundAppId);

  const todayComplete =
    s.lastUpdateDay === today &&
    !s.pendingRetryDay &&
    !(Array.isArray(s.pendingApps) && s.pendingApps.length > 0) &&
    !(Array.isArray(s.inProgressApps) && s.inProgressApps.length > 0);

  if (tvPower !== 'active') {
    if (hour === 3 && !todayComplete) {
      return {
        run: false,
        reason: 'tv-not-active',
        today,
        recordMissedDay: today
      };
    }
    return { run: false, reason: 'tv-not-active', today };
  }

  if (todayComplete) {
    return { run: false, reason: 'already-updated-today', today };
  }

  const requiresIdleForToday = Boolean(s.requiresIdle && s.requiresIdleDay === today);

  if (hour === 3 && !requiresIdleForToday) {
    return {
      run: true,
      reason: 'nightly-3am',
      mode: 'update',
      today,
      targetDay: today,
      reqId: s.pendingReqId || 'tv-nightly-' + today
    };
  }

  if (hour < 3) {
    // Before 03:00 AM, today's 3:00 AM window has NOT occurred yet.
    // Catch-up is only eligible for a prior missed/pending day, and targetDay MUST be that prior day.
    const hasPriorDayPending =
      Boolean(s.missedWindowDay && s.missedWindowDay < today) ||
      Boolean(s.pendingRetryDay && s.pendingRetryDay < today) ||
      Boolean(s.requiresIdleDay && s.requiresIdleDay < today) ||
      Boolean(s.lastUpdateDay && s.lastUpdateDay < yesterday) ||
      (s.lastUpdateDay !== yesterday &&
        (Boolean(s.pendingReqId) ||
          (Array.isArray(s.pendingApps) && s.pendingApps.length > 0) ||
          (Array.isArray(s.inProgressApps) && s.inProgressApps.length > 0)));
    if (!hasPriorDayPending) {
      return { run: false, reason: 'waiting-for-window-or-idle', today };
    }
    const priorTargetDay =
      (s.pendingRetryDay && s.pendingRetryDay < today && s.pendingRetryDay) ||
      (s.missedWindowDay && s.missedWindowDay < today && s.missedWindowDay) ||
      (s.requiresIdleDay && s.requiresIdleDay < today && s.requiresIdleDay) ||
      yesterday;
    if (fgIdle && idleMs >= thresholdMs) {
      return {
        run: true,
        reason: 'idle-catchup',
        mode: 'update',
        today,
        targetDay: priorTargetDay,
        reqId: s.pendingReqId || 'tv-idle-' + priorTargetDay
      };
    }
    return {
      run: false,
      reason: 'waiting-for-window-or-idle',
      today,
      recordMissedDay: s.missedWindowDay || priorTargetDay
    };
  }

  // hour >= 3 (either hour === 3 with requiresIdleForToday === true, or hour > 3):
  const eligibleAfterOrAt3am =
    requiresIdleForToday ||
    s.missedWindowDay === today ||
    s.pendingRetryDay === today ||
    Boolean(s.pendingReqId) ||
    (Array.isArray(s.pendingApps) && s.pendingApps.length > 0) ||
    (Array.isArray(s.inProgressApps) && s.inProgressApps.length > 0) ||
    (hour > 3 && s.lastUpdateDay !== today) ||
    Boolean(s.missedWindowDay && s.missedWindowDay < today) ||
    Boolean(s.pendingRetryDay && s.pendingRetryDay < today);

  if (!eligibleAfterOrAt3am) {
    return { run: false, reason: 'waiting-for-window-or-idle', today };
  }

  if (fgIdle && idleMs >= thresholdMs) {
    return {
      run: true,
      reason: 'idle-catchup',
      mode: 'update',
      today,
      targetDay: today,
      reqId: s.pendingReqId || 'tv-idle-' + today
    };
  }

  return {
    run: false,
    reason: 'waiting-for-window-or-idle',
    today,
    recordMissedDay: !s.missedWindowDay ? today : s.missedWindowDay
  };
}

function computeNextUpdaterState(prevState, workflowOutcome, context = {}) {
  const prev = prevState && typeof prevState === 'object' ? { ...prevState } : { schema: 1 };
  const nowDate = context.nowDate instanceof Date ? context.nowDate : new Date(context.nowDate || Date.now());
  const today = formatLocalDay(nowDate);
  const targetDay = typeof context.targetDay === 'string' && context.targetDay ? context.targetDay : today;
  const reqId = context.reqId || null;

  const complete = Boolean(workflowOutcome && workflowOutcome.maintenanceComplete === true);
  const pendingApps =
    workflowOutcome && Array.isArray(workflowOutcome.pendingApps) ? workflowOutcome.pendingApps : [];
  const inProgressApps =
    workflowOutcome && Array.isArray(workflowOutcome.updatesInProgress)
      ? [...new Set(workflowOutcome.updatesInProgress)]
      : [];

  if (complete) {
    return {
      schema: 1,
      lastUpdateDay: targetDay,
      lastUpdateAt: nowDate.toISOString(),
      lastReason: reqId,
      lastAttemptComplete: true,
      missedWindowDay:
        prev.missedWindowDay && prev.missedWindowDay > targetDay ? prev.missedWindowDay : null,
      requiresIdle: false,
      requiresIdleDay: null,
      pendingRetryDay: null,
      pendingReqId: null,
      pendingApps: [],
      inProgressApps: [],
      updatesAttempted: workflowOutcome.updatesAttempted || [],
      updatesCompleted: workflowOutcome.updatesCompleted || []
    };
  }

  return {
    schema: 1,
    lastUpdateDay: prev.lastUpdateDay || null,
    lastUpdateAt: prev.lastUpdateAt || null,
    lastAttemptAt: nowDate.toISOString(),
    lastReason: reqId,
    lastAttemptComplete: false,
    missedWindowDay: prev.missedWindowDay || targetDay,
    requiresIdle: true,
    requiresIdleDay: targetDay,
    pendingRetryDay: targetDay,
    pendingReqId: prev.pendingReqId || reqId || null,
    pendingApps,
    inProgressApps,
    updatesAttempted: (workflowOutcome && workflowOutcome.updatesAttempted) || [],
    updatesCompleted: (workflowOutcome && workflowOutcome.updatesCompleted) || [],
    updatesFailed: (workflowOutcome && workflowOutcome.updatesFailed) || [],
    updatesTimedOut: (workflowOutcome && workflowOutcome.updatesTimedOut) || [],
    updatesInProgress: inProgressApps,
    updatesSkippedForeground: (workflowOutcome && workflowOutcome.updatesSkippedForeground) || []
  };
}

function selectUpdatableApps(installedApps, updateStatusApps, foregroundAppId) {
  const installedIds = new Set();
  for (const app of Array.isArray(installedApps) ? installedApps : []) {
    if (!app || typeof app.id !== 'string') continue;
    const id = app.id.trim();
    if (!id || NEVER_UPDATE_APPS.has(id)) continue;
    installedIds.add(id);
  }

  const updatable = [];
  const skippedForeground = [];
  const seen = new Set();
  const fgNorm = typeof foregroundAppId === 'string' ? foregroundAppId.trim() : '';

  if (installedIds.size === 0) {
    return { updatable, skippedForeground };
  }

  for (const item of Array.isArray(updateStatusApps) ? updateStatusApps : []) {
    if (!item) continue;
    const id =
      typeof item.id === 'string'
        ? item.id.trim()
        : typeof item.appId === 'string'
        ? item.appId.trim()
        : '';
    if (!id || seen.has(id) || NEVER_UPDATE_APPS.has(id)) continue;
    if (!installedIds.has(id)) continue;
    if (item.updateYN === 'Y') {
      seen.add(id);
      if (fgNorm && id === fgNorm) {
        skippedForeground.push(id);
      } else {
        updatable.push(id);
      }
    }
  }

  return { updatable, skippedForeground };
}

async function runUpdateWorkflow(deps) {
  const {
    mode = 'update',
    tvPower = 'active',
    callLunaSend,
    callLunaSendLarge,
    setLgEgressWindow,
    sleep = () => Promise.resolve(),
    nowMs = () => Date.now(),
    hasUserActivitySince = () => false,
    sampleActivity = null,
    allowlistApps = [],
    knownInProgressApps = [],
    wakeOwnedByMaintenance = true,
    installTimeoutMs = DEFAULT_INSTALL_TIMEOUT_MS,
    maxInstallTimeoutMs = DEFAULT_MAX_INSTALL_TIMEOUT_MS,
    pollIntervalMs = 2000
  } = deps;

  const isUpdateMode = mode === 'update' || mode === 'update-standby';
  const returnToStandby = mode === 'update-standby';
  const wakeOwnedOk = wakeOwnedByMaintenance !== false;
  const startedAtMs = nowMs();

  let userActivityDetected = false;
  let foregroundObservationFailed = false;

  async function checkActivity(initialFg) {
    if (typeof sampleActivity === 'function') {
      try {
        const snap = await sampleActivity();
        if (snap) {
          if (!snap.fgIdle || snap.idleSinceMs > startedAtMs) {
            userActivityDetected = true;
          }
          if (
            initialFg !== undefined &&
            typeof snap.fgAppId === 'string' &&
            snap.fgAppId !== initialFg &&
            snap.fgAppId !== ''
          ) {
            userActivityDetected = true;
          }
        }
      } catch (_) {}
    }
    try {
      if (hasUserActivitySince(startedAtMs)) {
        userActivityDetected = true;
      }
    } catch (_) {}
    return userActivityDetected;
  }

  const fgReply = await callLunaSend(
    'luna://com.webos.applicationManager/getForegroundAppInfo',
    {},
    2500
  );
  const initialFgOk = isValidForegroundReply(fgReply);
  if (!initialFgOk) {
    foregroundObservationFailed = true;
  }
  const foregroundAppId = initialFgOk ? fgReply.appId.trim() : null;
  if (foregroundAppId && !isForegroundIdle(foregroundAppId)) {
    userActivityDetected = true;
  }
  await checkActivity(foregroundAppId !== null ? foregroundAppId : undefined);

  const listReply = await callLunaSendLarge(
    'luna://com.webos.applicationManager/listApps',
    {},
    5000,
    1048576
  );
  const listAppsOk = Boolean(
    listReply &&
      listReply.returnValue !== false &&
      Array.isArray(listReply.apps) &&
      listReply.apps.length > 0
  );
  const allInstalledApps = listReply && Array.isArray(listReply.apps) ? listReply.apps : [];
  const installedMap = new Map();
  let homeAppPresent = false;
  let homeAppVersion = null;

  for (const app of allInstalledApps) {
    if (!app || typeof app.id !== 'string') continue;
    const id = app.id.trim();
    if (!id) continue;
    if (id === 'local.owner.applibrary') {
      homeAppPresent = true;
      homeAppVersion = typeof app.version === 'string' ? app.version : null;
    }
    if (!NEVER_UPDATE_APPS.has(id)) {
      installedMap.set(id, {
        version: typeof app.version === 'string' ? app.version : null
      });
    }
  }

  let updateReply = null;
  let installReply = null;
  const updateMap = new Map();
  const installMap = new Map();
  const updatesAttempted = [];
  const updatesCompleted = [];
  const updatesFailed = [];
  const updatesTimedOut = [];
  const updatesInProgress = [];
  const updatesSkippedForeground = [];
  let screenTurnedOff = false;
  let returnedToStandby = false;
  let windowClosedOk = true;

  function isJobTerminalSuccess(stateLower) {
    return (
      stateLower === 'installed' ||
      stateLower === 'complete' ||
      stateLower === 'completed'
    );
  }

  function isJobTerminalFailure(stateLower, errorCode) {
    return (
      stateLower === 'install failed' ||
      stateLower === 'download failed' ||
      stateLower === 'canceled' ||
      stateLower === 'cancelled' ||
      stateLower === 'failed' ||
      stateLower === 'error' ||
      (typeof errorCode === 'number' && errorCode !== 0)
    );
  }

  async function cancelAndReconcileInFlightJob(appId) {
    const cancelRes = await callLunaSend(
      'luna://com.webos.appInstallService/cancel',
      { id: appId },
      2500
    );
    if (!cancelRes || cancelRes.returnValue !== true) {
      if (!updatesInProgress.includes(appId)) {
        updatesInProgress.push(appId);
      }
      return false;
    }
    const postSt = await callLunaSendLarge(
      'luna://com.webos.appInstallService/status',
      { subscribe: false },
      3500,
      262144
    );
    const postStOk = Boolean(
      postSt &&
        postSt.returnValue !== false &&
        postSt.status &&
        Array.isArray(postSt.status.apps)
    );
    if (!postStOk) {
      if (!updatesInProgress.includes(appId)) {
        updatesInProgress.push(appId);
      }
      return false;
    }
    const remainingJob = postSt.status.apps.find(
      (j) => j && (j.id === appId || j.appId === appId)
    );
    if (remainingJob) {
      const remState =
        typeof remainingJob.state === 'string' ? remainingJob.state.trim().toLowerCase() : '';
      if (
        !isJobTerminalFailure(remState, remainingJob.errorCode) &&
        !isJobTerminalSuccess(remState)
      ) {
        if (!updatesInProgress.includes(appId)) {
          updatesInProgress.push(appId);
        }
        return false;
      }
    }
    return true;
  }

  async function pollAndReconcileJob(appId, beforeVersion, targetVersion) {
    let deadline = nowMs() + installTimeoutMs;
    const maxDeadline = nowMs() + Math.max(installTimeoutMs, maxInstallTimeoutMs);
    let lastProgress = -1;
    let terminalOutcome = null;

    while (nowMs() < deadline) {
      await sleep(pollIntervalMs);
      await checkActivity(foregroundAppId !== null ? foregroundAppId : undefined);

      const st = await callLunaSendLarge(
        'luna://com.webos.appInstallService/status',
        { subscribe: false },
        3500,
        262144
      );
      if (
        !st ||
        st.returnValue === false ||
        !st.status ||
        !Array.isArray(st.status.apps)
      ) {
        continue;
      }

      const jobs = st.status.apps;
      const job = jobs.find((j) => j && (j.id === appId || j.appId === appId));
      if (job) {
        const rawState = typeof job.state === 'string' ? job.state.trim() : '';
        const stateLower = rawState.toLowerCase();
        installMap.set(appId, {
          state: rawState || null,
          progress: typeof job.progress === 'number' ? job.progress : null,
          errorCode: typeof job.errorCode === 'number' ? job.errorCode : null
        });

        if (isJobTerminalSuccess(stateLower)) {
          terminalOutcome = 'installed';
          break;
        }
        if (isJobTerminalFailure(stateLower, job.errorCode)) {
          terminalOutcome = 'failed';
          break;
        }

        if (typeof job.progress === 'number' && job.progress > lastProgress) {
          lastProgress = job.progress;
          deadline = Math.min(maxDeadline, Math.max(deadline, nowMs() + installTimeoutMs));
        }
        continue;
      }

      const afterList = await callLunaSendLarge(
        'luna://com.webos.applicationManager/listApps',
        {},
        4000,
        1048576
      );
      const afterApps =
        afterList && afterList.returnValue !== false && Array.isArray(afterList.apps)
          ? afterList.apps
          : null;
      const afterApp = afterApps ? afterApps.find((a) => a && a.id === appId) : null;
      const afterVer = afterApp && typeof afterApp.version === 'string' ? afterApp.version : null;
      const versionAdvanced = Boolean(
        afterVer &&
          ((beforeVersion && afterVer !== beforeVersion) ||
            (targetVersion && afterVer === targetVersion))
      );

      let updateFlagCleared = false;
      if (!versionAdvanced) {
        const afterUpd = await callLunaSendLarge(
          'luna://com.webos.appUpdateService/statusAll',
          { subscribe: false },
          3500,
          262144
        );
        const afterUpdApps =
          afterUpd &&
          afterUpd.returnValue === true &&
          afterUpd.status &&
          Array.isArray(afterUpd.status.apps)
            ? afterUpd.status.apps
            : null;
        const afterUpdItem = afterUpdApps
          ? afterUpdApps.find((u) => u && (u.id === appId || u.appId === appId))
          : null;
        if (afterUpdItem && afterUpdItem.updateYN === 'N') {
          updateFlagCleared = true;
        }
      }

      if (versionAdvanced || updateFlagCleared) {
        if (afterVer) {
          installedMap.set(appId, { version: afterVer });
        }
        terminalOutcome = 'installed';
        break;
      }
    }

    if (terminalOutcome === 'installed') {
      if (!updatesCompleted.includes(appId)) {
        updatesCompleted.push(appId);
      }
    } else if (terminalOutcome === 'failed') {
      if (!updatesFailed.includes(appId)) {
        updatesFailed.push(appId);
      }
    } else {
      if (!updatesTimedOut.includes(appId)) {
        updatesTimedOut.push(appId);
      }
      await cancelAndReconcileInFlightJob(appId);
    }
  }

  try {
    if (typeof setLgEgressWindow === 'function') {
      const opened = await setLgEgressWindow(isUpdateMode && tvPower === 'active');
      if (isUpdateMode && tvPower === 'active' && opened === false) {
        throw new Error('LG_EGRESS_WINDOW_OPEN_FAILED');
      }
    }

    if (returnToStandby && tvPower === 'active') {
      await checkActivity(foregroundAppId !== null ? foregroundAppId : undefined);
      const preScreenFg = await callLunaSend(
        'luna://com.webos.applicationManager/getForegroundAppInfo',
        {},
        2500
      );
      const preScreenFgOk = isValidForegroundReply(preScreenFg);
      if (!preScreenFgOk) {
        foregroundObservationFailed = true;
      } else {
        const preScreenAppId = preScreenFg.appId.trim();
        if (preScreenAppId !== (foregroundAppId || '') || !isForegroundIdle(preScreenAppId)) {
          userActivityDetected = true;
        }
      }
      const liveWakeOwnedOk =
        typeof deps.isWakeOwned === 'function' ? Boolean(deps.isWakeOwned()) : wakeOwnedOk;
      if (
        liveWakeOwnedOk &&
        listAppsOk &&
        initialFgOk &&
        preScreenFgOk &&
        !foregroundObservationFailed &&
        isForegroundIdle(foregroundAppId) &&
        !userActivityDetected
      ) {
        const offRes = await callLunaSend(
          'luna://com.webos.service.tvpower/power/turnOffScreen',
          {},
          2500
        );
        screenTurnedOff = Boolean(offRes && offRes.returnValue === true);
      }
    }

    updateReply = await callLunaSendLarge(
      'luna://com.webos.appUpdateService/statusAll',
      { subscribe: false },
      4000,
      262144
    );
    const updateApps =
      updateReply &&
      updateReply.returnValue !== false &&
      updateReply.status &&
      Array.isArray(updateReply.status.apps)
        ? updateReply.status.apps
        : [];
    for (const item of updateApps) {
      if (!item) continue;
      const id =
        typeof item.id === 'string'
          ? item.id.trim()
          : typeof item.appId === 'string'
          ? item.appId.trim()
          : null;
      if (id && !NEVER_UPDATE_APPS.has(id)) {
        updateMap.set(id, {
          updateYN: item.updateYN === 'Y' || item.updateYN === 'N' ? item.updateYN : null,
          storeVersion: typeof item.version === 'string' ? item.version : null,
          status: item.status !== undefined ? item.status : null,
          state: typeof item.state === 'string' ? item.state : null
        });
      }
    }

    installReply = await callLunaSendLarge(
      'luna://com.webos.appInstallService/status',
      { subscribe: false },
      4000,
      262144
    );
    const installStatusOk = Boolean(
      installReply &&
        installReply.returnValue !== false &&
        installReply.status &&
        Array.isArray(installReply.status.apps)
    );
    const installApps = installStatusOk ? installReply.status.apps : [];
    for (const item of installApps) {
      if (!item) continue;
      const id =
        typeof item.id === 'string'
          ? item.id.trim()
          : typeof item.appId === 'string'
          ? item.appId.trim()
          : null;
      if (id && !NEVER_UPDATE_APPS.has(id)) {
        installMap.set(id, {
          state: typeof item.state === 'string' ? item.state : null,
          progress: typeof item.progress === 'number' ? item.progress : null,
          errorCode: typeof item.errorCode === 'number' ? item.errorCode : null
        });
      }
    }

    // Reconcile every prior or currently active in-progress job BEFORE starting any new installation
    const priorInProgressSet = new Set(
      (Array.isArray(knownInProgressApps) ? knownInProgressApps : [])
        .map((id) => (typeof id === 'string' ? id.trim() : ''))
        .filter((id) => id && !NEVER_UPDATE_APPS.has(id))
    );
    const reconciledPriorIds = new Set();
    const allPriorAndActiveIds = [
      ...new Set([...priorInProgressSet, ...installMap.keys()])
    ];
    for (const priorId of allPriorAndActiveIds) {
      if (!installStatusOk) {
        if (priorInProgressSet.has(priorId)) {
          await cancelAndReconcileInFlightJob(priorId);
          reconciledPriorIds.add(priorId);
        }
        continue;
      }
      const job = installMap.get(priorId);
      if (job) {
        const stLower = typeof job.state === 'string' ? job.state.trim().toLowerCase() : '';
        if (!isJobTerminalSuccess(stLower) && !isJobTerminalFailure(stLower, job.errorCode)) {
          reconciledPriorIds.add(priorId);
          if (!updatesAttempted.includes(priorId)) {
            updatesAttempted.push(priorId);
          }
          const bVer = installedMap.get(priorId) ? installedMap.get(priorId).version : null;
          const tVer = updateMap.get(priorId) ? updateMap.get(priorId).storeVersion : null;
          await pollAndReconcileJob(priorId, bVer, tVer);
        }
      }
    }

    if (isUpdateMode && tvPower === 'active' && listAppsOk) {
      // Preserve foreground exclusion in ALL modes and recheck immediately before each install.
      const { updatable, skippedForeground } = selectUpdatableApps(
        allInstalledApps,
        updateApps,
        foregroundAppId
      );
      for (const skippedId of skippedForeground) {
        if (!updatesSkippedForeground.includes(skippedId)) {
          updatesSkippedForeground.push(skippedId);
        }
      }

      for (const appId of updatable) {
        if (reconciledPriorIds.has(appId)) {
          continue;
        }
        await checkActivity(foregroundAppId !== null ? foregroundAppId : undefined);
        if (!initialFgOk) {
          foregroundObservationFailed = true;
          if (!updatesSkippedForeground.includes(appId)) {
            updatesSkippedForeground.push(appId);
          }
          continue;
        }

        const liveFgReply = await callLunaSend(
          'luna://com.webos.applicationManager/getForegroundAppInfo',
          {},
          2500
        );
        if (!isValidForegroundReply(liveFgReply)) {
          foregroundObservationFailed = true;
          if (!updatesSkippedForeground.includes(appId)) {
            updatesSkippedForeground.push(appId);
          }
          continue;
        }

        const liveFgAppId = liveFgReply.appId.trim();
        if (liveFgAppId !== (foregroundAppId || '')) {
          userActivityDetected = true;
        }
        if (liveFgAppId && !isForegroundIdle(liveFgAppId)) {
          userActivityDetected = true;
        }
        if (returnToStandby && userActivityDetected) {
          if (!updatesSkippedForeground.includes(appId)) {
            updatesSkippedForeground.push(appId);
          }
          continue;
        }
        if (liveFgAppId && liveFgAppId === appId) {
          if (!updatesSkippedForeground.includes(appId)) {
            updatesSkippedForeground.push(appId);
          }
          continue;
        }

        updatesAttempted.push(appId);
        const beforeVersion = installedMap.get(appId) ? installedMap.get(appId).version : null;
        const targetVersion = updateMap.get(appId) ? updateMap.get(appId).storeVersion : null;

        const inst = await callLunaSend(
          'luna://com.webos.appInstallService/install',
          {
            id: appId,
            subscribe: false,
            silence: true
          },
          4000
        );
        if (!inst || inst.returnValue !== true) {
          updatesFailed.push(appId);
          continue;
        }

        await pollAndReconcileJob(appId, beforeVersion, targetVersion);
      }

      if (updatesCompleted.length > 0) {
        const afterReply = await callLunaSendLarge(
          'luna://com.webos.applicationManager/listApps',
          {},
          5000,
          1048576
        );
        if (afterReply && afterReply.returnValue !== false && Array.isArray(afterReply.apps)) {
          for (const app of afterReply.apps) {
            if (app && typeof app.id === 'string' && !NEVER_UPDATE_APPS.has(app.id)) {
              installedMap.set(app.id, {
                version: typeof app.version === 'string' ? app.version : null
              });
            }
          }
        }
      }
    }
  } finally {
    if (typeof setLgEgressWindow === 'function') {
      const closed = await setLgEgressWindow(false);
      if (closed === false) {
        windowClosedOk = false;
      }
    }
  }

  await checkActivity(foregroundAppId !== null ? foregroundAppId : undefined);
  const postFgReply = await callLunaSend(
    'luna://com.webos.applicationManager/getForegroundAppInfo',
    {},
    2500
  );
  const postFgOk = isValidForegroundReply(postFgReply);
  if (!postFgOk) {
    foregroundObservationFailed = true;
  }
  const postFgAppId = postFgOk ? postFgReply.appId.trim() : null;
  if (postFgAppId !== null && postFgAppId !== (foregroundAppId || '')) {
    userActivityDetected = true;
  }
  if (postFgAppId && !isForegroundIdle(postFgAppId)) {
    userActivityDetected = true;
  }

  const postWakeOwnedOk =
    typeof deps.isWakeOwned === 'function' ? Boolean(deps.isWakeOwned()) : wakeOwnedOk;

  if (
    returnToStandby &&
    tvPower === 'active' &&
    postWakeOwnedOk &&
    listAppsOk &&
    initialFgOk &&
    postFgOk &&
    !foregroundObservationFailed &&
    isForegroundIdle(foregroundAppId) &&
    isForegroundIdle(postFgAppId) &&
    !userActivityDetected &&
    updatesTimedOut.length === 0 &&
    updatesInProgress.length === 0
  ) {
    const standbyRes = await callLunaSend(
      'luna://com.webos.service.tvpower/power/turnOff',
      { reason: 'remoteKey' },
      3000
    );
    returnedToStandby = Boolean(standbyRes && standbyRes.returnValue === true);
  }

  const updateCacheOk = Boolean(
    updateReply &&
      updateReply.returnValue === true &&
      updateReply.status &&
      Array.isArray(updateReply.status.apps)
  );
  const pendingApps = [
    ...new Set([
      ...updatesFailed,
      ...updatesTimedOut,
      ...updatesInProgress,
      ...updatesSkippedForeground
    ])
  ];
  const maintenanceComplete = Boolean(
    isUpdateMode &&
      tvPower === 'active' &&
      initialFgOk &&
      !foregroundObservationFailed &&
      listAppsOk &&
      updateCacheOk &&
      windowClosedOk &&
      updatesInProgress.length === 0 &&
      pendingApps.length === 0 &&
      updatesCompleted.length === updatesAttempted.length
  );

  const allowlistedApps = allowlistApps.map((appId) => {
    const inst = installedMap.get(appId);
    const upd = updateMap.get(appId);
    const job = installMap.get(appId);
    return {
      id: appId,
      installed: Boolean(inst),
      version: inst ? inst.version : null,
      updateYN: upd ? upd.updateYN : null,
      state: job && job.state ? job.state : upd ? upd.state : null,
      errorCode: job ? job.errorCode : null
    };
  });

  return {
    foregroundAppId,
    foregroundObservationFailed,
    homeApp: {
      present: homeAppPresent,
      version: homeAppVersion
    },
    listAppsOk,
    installedCount: installedMap.size,
    updateCacheOk,
    updatesAttempted,
    updatesCompleted,
    updatesFailed,
    updatesTimedOut,
    updatesInProgress,
    updatesSkippedForeground,
    pendingApps,
    maintenanceComplete,
    windowClosedOk,
    userActivityDetected,
    screenTurnedOff,
    returnedToStandby,
    allowlistedApps
  };
}

function createLgNetworkGuard(deps = {}) {
  const io = deps.fs || fs;
  const spawnSync = deps.spawnSync || cp.spawnSync;
  const getMountedTargets = deps.getMountedTargets || (() => new Map());
  const writeAtomic = deps.writeAtomic;
  const env = deps.env || process.env;
  const hostsOrigPath = deps.hostsOrigPath || '/tmp/r2d2-hosts.orig';
  const hostsBoundPath = deps.hostsBoundPath || '/tmp/r2d2-hosts';
  const appStoreLeasePath = deps.appStoreLeasePath || '/tmp/r2d2-appstore-window.json';
  const appStoreIpsPath = deps.appStoreIpsPath || '/tmp/r2d2-appstore-ips.json';
  const nowMs = deps.nowMs || (() => Date.now());
  const log = deps.log || (() => {});

  let currentAppStoreOpen = null;
  let lgServerBlocked = false;
  let unverifiedWindowHistory = false;

  function fileSha(p) {
    try {
      return sha256Hex(io.readFileSync(p));
    } catch (_) {
      return null;
    }
  }

  function loadRecordedAppStoreIps() {
    try {
      if (io.existsSync(appStoreIpsPath)) {
        const parsed = JSON.parse(io.readFileSync(appStoreIpsPath, 'utf8'));
        if (parsed && parsed.unverifiedOpen === true) {
          unverifiedWindowHistory = true;
        }
        if (parsed && Array.isArray(parsed.ips)) {
          return parsed.ips.filter(
            (ip) => isValidIpv4(ip) || isValidIpv6(ip)
          );
        }
      }
    } catch (_) {}
    return [];
  }

  function persistAppStoreIpState(ips, unverifiedOpen) {
    if (typeof writeAtomic !== 'function') return;
    try {
      writeAtomic(
        appStoreIpsPath,
        Buffer.from(
          JSON.stringify({
            schema: 1,
            ips: Array.isArray(ips) ? ips : [],
            unverifiedOpen: Boolean(unverifiedOpen)
          }) + '\n',
          'utf8'
        ),
        0o600
      );
    } catch (_) {}
  }

  function harvestActiveAppStoreIps() {
    const known = new Set(loadRecordedAppStoreIps());
    for (const domain of LG_APPSTORE_DOMAINS) {
      try {
        const r = spawnSync('/usr/bin/getent', ['ahosts', domain], { env, timeout: 1000 });
        if (r && r.status === 0 && r.stdout) {
          const text = r.stdout.toString('utf8');
          for (const line of text.split('\n')) {
            const tok = line.trim().split(/\s+/)[0] || '';
            if (isValidIpv4(tok) && tok !== '0.0.0.0' && tok !== '127.0.0.1') {
              known.add(tok);
            } else if (isValidIpv6(tok)) {
              known.add(tok.toLowerCase());
            }
          }
        }
      } catch (_) {}
    }
    const list = [...known];
    if (list.length > 0) {
      persistAppStoreIpState(list, false);
    }
    return list;
  }

  function revokeCachedConnections(blockedIps) {
    for (const ssBin of ['/usr/sbin/ss', '/sbin/ss', '/bin/ss']) {
      try {
        if (io.existsSync(ssBin)) {
          for (const ip of blockedIps) {
            spawnSync(ssBin, ['-K', 'dst', ip], { env, timeout: 1000 });
          }
          break;
        }
      } catch (_) {}
    }
    for (const ipBin of ['/sbin/ip', '/usr/sbin/ip', '/bin/ip']) {
      try {
        if (io.existsSync(ipBin)) {
          spawnSync(ipBin, ['route', 'flush', 'cache'], { env, timeout: 1000 });
          spawnSync(ipBin, ['-6', 'route', 'flush', 'cache'], { env, timeout: 1000 });
          break;
        }
      } catch (_) {}
    }
  }

  function mountHostsForState(baseHosts, allowOpen) {
    const expectedHostsText = renderBlockedHosts(baseHosts, { allowAppStore: allowOpen });
    const expectedHostsSha = sha256Hex(expectedHostsText);
    const alreadyMounted = getMountedTargets().has('/etc/hosts');
    const liveSha = fileSha('/etc/hosts');

    if (alreadyMounted && liveSha !== expectedHostsSha) {
      const umRes = spawnSync('/bin/umount', ['-l', '/etc/hosts'], { env, timeout: 2000 });
      if (!umRes || umRes.status !== 0) {
        return false;
      }
    }

    if (!getMountedTargets().has('/etc/hosts') || fileSha('/etc/hosts') !== expectedHostsSha) {
      writeAtomic(hostsBoundPath, Buffer.from(expectedHostsText, 'utf8'), 0o644);
      if (fileSha(hostsBoundPath) !== expectedHostsSha) {
        return false;
      }
      const bindRes = spawnSync('/bin/mount', ['--bind', hostsBoundPath, '/etc/hosts'], {
        env,
        timeout: 2000
      });
      const roRes = spawnSync(
        '/bin/mount',
        ['-o', 'remount,bind,ro', hostsBoundPath, '/etc/hosts'],
        { env, timeout: 2000 }
      );
      if (!bindRes || bindRes.status !== 0 || !roRes || roRes.status !== 0) {
        return false;
      }
    }

    return getMountedTargets().has('/etc/hosts') && fileSha('/etc/hosts') === expectedHostsSha;
  }

  function runFirewallCommands(binPath, cmds) {
    for (const args of cmds) {
      const res = spawnSync(binPath, args, { env, timeout: 1500 });
      const op = args[0];
      if (op !== '-N' && op !== '-D') {
        if (!res || res.status !== 0) {
          return false;
        }
      }
    }
    return true;
  }

  function apply(allowAppStore = false) {
    try {
      const wantOpen = Boolean(allowAppStore === true);

      if (!io.existsSync(hostsOrigPath)) {
        const mounted0 = getMountedTargets();
        if (mounted0.has('/etc/hosts')) {
          const u0 = spawnSync('/bin/umount', ['-l', '/etc/hosts'], { env, timeout: 2000 });
          if (!u0 || u0.status !== 0) {
            currentAppStoreOpen = null;
            lgServerBlocked = false;
            return false;
          }
        }
        let baseText = '127.0.0.1\tlocalhost\n::1\tlocalhost\n';
        try {
          baseText = io.readFileSync('/etc/hosts', 'utf8');
        } catch (_) {}
        const cleanOrig = stripManagedHostsBlock(baseText);
        writeAtomic(hostsOrigPath, Buffer.from(cleanOrig, 'utf8'), 0o644);
      }

      const baseHosts = io.readFileSync(hostsOrigPath, 'utf8');
      if (!mountHostsForState(baseHosts, wantOpen)) {
        currentAppStoreOpen = null;
        lgServerBlocked = false;
        return false;
      }

      let iptBin = null;
      for (const candidate of ['/usr/sbin/iptables', '/sbin/iptables']) {
        if (io.existsSync(candidate)) {
          iptBin = candidate;
          break;
        }
      }
      if (!iptBin) {
        currentAppStoreOpen = null;
        lgServerBlocked = false;
        return false;
      }

      let ip6tBin = null;
      for (const candidate of ['/usr/sbin/ip6tables', '/sbin/ip6tables']) {
        if (io.existsSync(candidate)) {
          ip6tBin = candidate;
          break;
        }
      }
      if (!ip6tBin) {
        ip6tBin = iptBin.replace(/iptables$/, 'ip6tables');
      }

      const appStoreIps = wantOpen ? harvestActiveAppStoreIps() : loadRecordedAppStoreIps();

      if (wantOpen && appStoreIps.length === 0) {
        // Refusing to leave window open when DNS harvesting fails and no App Store IPs are known
        unverifiedWindowHistory = true;
        persistAppStoreIpState([], true);
        mountHostsForState(baseHosts, false);
        runFirewallCommands(iptBin, buildIptablesCommands({ allowAppStore: false, appStoreIps: [] }));
        runFirewallCommands(ip6tBin, buildIp6tablesCommands({ allowAppStore: false, appStoreIps: [] }));
        currentAppStoreOpen = false;
        lgServerBlocked = false;
        return false;
      }

      if (wantOpen && appStoreIps.length > 0) {
        unverifiedWindowHistory = false;
        persistAppStoreIpState(appStoreIps, false);
      }

      const iptCmds = buildIptablesCommands({ allowAppStore: wantOpen, appStoreIps });
      const ip6tCmds = buildIp6tablesCommands({ allowAppStore: wantOpen, appStoreIps });

      if (!runFirewallCommands(iptBin, iptCmds) || !runFirewallCommands(ip6tBin, ip6tCmds)) {
        currentAppStoreOpen = null;
        lgServerBlocked = false;
        return false;
      }

      if (wantOpen) {
        try {
          writeAtomic(
            appStoreLeasePath,
            Buffer.from(
              JSON.stringify({
                schema: 1,
                pid: process.pid,
                openedAtMs: nowMs(),
                expiresAtMs: nowMs() + 10 * 60 * 1000
              }) + '\n',
              'utf8'
            ),
            0o600
          );
        } catch (_) {}
      } else {
        try {
          if (io.existsSync(appStoreLeasePath)) {
            io.unlinkSync(appStoreLeasePath);
          }
        } catch (_) {}
        revokeCachedConnections([
          ...LG_PERMANENT_BLOCK_IPS,
          ...LG_PERMANENT_BLOCK_IPV6S,
          ...appStoreIps
        ]);
        if (unverifiedWindowHistory && appStoreIps.length === 0) {
          currentAppStoreOpen = false;
          lgServerBlocked = false;
          return false;
        }
      }

      currentAppStoreOpen = wantOpen;
      lgServerBlocked = !wantOpen;
      return true;
    } catch (e) {
      currentAppStoreOpen = null;
      lgServerBlocked = false;
      log('lg_block_error', { error: String(e && (e.code || e.message || e)) });
      return false;
    }
  }

  function enforceLease() {
    try {
      if (!io.existsSync(appStoreLeasePath)) {
        const ok = apply(false);
        return { leasePresent: false, expired: false, blocked: ok && lgServerBlocked };
      }
      const rawLease = io.readFileSync(appStoreLeasePath, 'utf8');
      const lease = JSON.parse(rawLease);
      const now = nowMs();
      const expired =
        !lease ||
        typeof lease.expiresAtMs !== 'number' ||
        now >= lease.expiresAtMs;
      let ownerDead = false;
      if (lease && typeof lease.pid === 'number' && lease.pid > 0 && lease.pid !== process.pid) {
        try {
          process.kill(lease.pid, 0);
        } catch (err) {
          if (err && err.code === 'ESRCH') {
            ownerDead = true;
          }
        }
      }
      if (expired || ownerDead) {
        const ok = apply(false);
        return { leasePresent: true, expired: true, ownerDead, blocked: ok && lgServerBlocked };
      }
      return { leasePresent: true, expired: false, ownerDead: false, blocked: lgServerBlocked };
    } catch (_) {
      const ok = apply(false);
      return { leasePresent: true, expired: true, blocked: ok && lgServerBlocked };
    }
  }

  return {
    apply,
    enforceLease,
    isBlocked: () => lgServerBlocked,
    isWindowOpen: () => currentAppStoreOpen === true,
    resetCache: () => {
      currentAppStoreOpen = null;
      lgServerBlocked = false;
      unverifiedWindowHistory = false;
    }
  };
}

function createPrivacyRetirementEnforcer(deps = {}) {
  const io = deps.fs || fs;
  const spawnSync = deps.spawnSync || cp.spawnSync;
  const getAllMountPoints = deps.getAllMountPoints || (() => new Set());
  const retirePrivacyProcesses = deps.retirePrivacyProcesses || (() => 0);
  const env = deps.env || process.env;
  const emptyDir = deps.emptyDir || '/var/lib/local.owner.r2d2mic/privacy-empty';
  const stubBin = deps.stubBin || '/bin/true';
  const systemdServices = deps.systemdServices || [];
  const ls2Manifests = deps.ls2Manifests || [];
  const binTargets = deps.binTargets || [];
  const dirTargets = deps.dirTargets || [];
  const log = deps.log || (() => {});

  const verifiedRemovedManifests = new Set();
  let privacyStatus = 'init';
  let manifestsRemovedCount = 0;

  function isManifestRemovalAccepted(res) {
    if (!res || res.status !== 0 || !res.stdout) return false;
    const text = Buffer.isBuffer(res.stdout)
      ? res.stdout.toString('utf8').trim()
      : String(res.stdout).trim();
    if (!text) return false;
    try {
      const parsed = JSON.parse(text);
      return Boolean(parsed && parsed.returnValue === true);
    } catch (_) {
      return false;
    }
  }

  function enforce() {
    try {
      try {
        io.mkdirSync(emptyDir, 0o555);
      } catch (_) {}
      try {
        io.chmodSync(emptyDir, 0o555);
      } catch (_) {}

      const presentBins = binTargets.filter((t) => {
        try {
          return io.existsSync(t);
        } catch (_) {
          return false;
        }
      });
      const presentDirs = dirTargets.filter((t) => {
        try {
          return io.existsSync(t);
        } catch (_) {
          return false;
        }
      });
      const presentManifests = ls2Manifests.filter((m) => {
        try {
          return io.existsSync(m);
        } catch (_) {
          return false;
        }
      });
      const totalPresent = presentBins.length + presentDirs.length;

      for (const m of [...verifiedRemovedManifests]) {
        if (!presentManifests.includes(m)) {
          verifiedRemovedManifests.delete(m);
        }
      }

      let points = getAllMountPoints();
      let mountedCount =
        presentBins.filter((t) => points.has(t)).length +
        presentDirs.filter((t) => points.has(t)).length;

      if (
        mountedCount < totalPresent ||
        verifiedRemovedManifests.size < presentManifests.length
      ) {
        for (const svc of systemdServices) {
          try {
            spawnSync('/bin/systemctl', ['stop', svc], {
              env,
              timeout: 2500
            });
          } catch (_) {}
        }

        for (const manifestPath of presentManifests) {
          if (verifiedRemovedManifests.has(manifestPath)) continue;
          try {
            const res = spawnSync(
              '/usr/bin/luna-send',
              [
                '-n',
                '1',
                '-w',
                '1200',
                'luna://com.webos.service.bus/removeOneManifest',
                JSON.stringify({ path: manifestPath, prefix: '/' })
              ],
              { env, timeout: 1500 }
            );
            if (isManifestRemovalAccepted(res)) {
              verifiedRemovedManifests.add(manifestPath);
            }
          } catch (_) {}
        }
        manifestsRemovedCount = verifiedRemovedManifests.size;
        retirePrivacyProcesses();

        for (const binPath of presentBins) {
          if (!points.has(binPath)) {
            try {
              const b1 = spawnSync('/bin/mount', ['-o', 'bind', stubBin, binPath], {
                env,
                timeout: 2000
              });
              if (b1 && b1.status === 0) {
                spawnSync('/bin/mount', ['-o', 'remount,ro,bind', stubBin, binPath], {
                  env,
                  timeout: 2000
                });
              }
            } catch (_) {}
          }
        }

        for (const dirPath of presentDirs) {
          if (!points.has(dirPath)) {
            try {
              const d1 = spawnSync('/bin/mount', ['-o', 'bind', emptyDir, dirPath], {
                env,
                timeout: 2000
              });
              if (d1 && d1.status === 0) {
                spawnSync('/bin/mount', ['-o', 'remount,ro,bind', emptyDir, dirPath], {
                  env,
                  timeout: 2000
                });
              }
            } catch (_) {}
          }
        }

        retirePrivacyProcesses();
        points = getAllMountPoints();
        mountedCount =
          presentBins.filter((t) => points.has(t)).length +
          presentDirs.filter((t) => points.has(t)).length;
        log('privacy_retirements_applied', {
          mounted: mountedCount,
          totalPresent,
          manifestsRemoved: manifestsRemovedCount
        });
      } else {
        retirePrivacyProcesses();
      }
      manifestsRemovedCount = verifiedRemovedManifests.size;
      privacyStatus = mountedCount + '/' + totalPresent;
      return {
        ok:
          mountedCount === totalPresent &&
          verifiedRemovedManifests.size === presentManifests.length,
        privacyStatus,
        manifestsRemovedCount,
        mountedCount,
        totalPresent
      };
    } catch (e) {
      privacyStatus = 'err';
      log('privacy_retirement_error', { error: String(e && (e.code || e.message || e)) });
      return {
        ok: false,
        privacyStatus: 'err',
        manifestsRemovedCount: verifiedRemovedManifests.size,
        mountedCount: 0,
        totalPresent: 0
      };
    }
  }

  return {
    enforce,
    getStatus: () => privacyStatus,
    getManifestsRemovedCount: () => verifiedRemovedManifests.size
  };
}

function createAutoVoiceLevelingEnforcer(deps = {}) {
  const io = deps.fs || fs;
  const spawnSync = deps.spawnSync || cp.spawnSync;
  const callLunaSend = typeof deps.callLunaSend === 'function' ? deps.callLunaSend : null;
  const writeAtomic = typeof deps.writeAtomic === 'function' ? deps.writeAtomic : null;
  const env = deps.env || process.env;
  const statePath = deps.statePath || '/var/lib/local.owner.r2d2mic/voice-leveling-state.json';
  const nowMs = typeof deps.nowMs === 'function' ? deps.nowMs : () => Date.now();
  const recheckIntervalMs =
    typeof deps.recheckIntervalMs === 'number' && deps.recheckIntervalMs > 0
      ? deps.recheckIntervalMs
      : DEFAULT_VOICE_LEVELING_RECHECK_MS;
  const postSwitchRecheckDelayMs =
    typeof deps.postSwitchRecheckDelayMs === 'number' && deps.postSwitchRecheckDelayMs >= 0
      ? deps.postSwitchRecheckDelayMs
      : DEFAULT_POST_SWITCH_RECHECK_DELAY_MS;
  const isLgServerBlocked =
    typeof deps.isLgServerBlocked === 'function' ? deps.isLgServerBlocked : () => true;
  const log = typeof deps.log === 'function' ? deps.log : () => {};

  let hydratedFromDisk = false;
  let verifiedForCurrentCycle = false;
  let currentStatus = 'init';
  let currentAutoVolume = null;
  let currentSoundMode = null;
  let currentSoundOutput = null;
  let resolvedAutoVolumeValue = null;
  let resolvedSoundMode = null;
  let preferredProbed = false;
  let lastVerifiedFgAppId = null;
  let lastObservedFgAppId = null;
  let lastFgSwitchAtMs = 0;
  let postSwitchRecheckPending = false;
  let lastVerifiedAtMs = 0;
  let lastVerifiedAtIso = null;
  let lastEnforcedAtIso = null;
  let lastPower = 'unknown';
  let enforceCount = 0;

  function loadPersistedStateOnce() {
    if (hydratedFromDisk) return;
    hydratedFromDisk = true;
    try {
      if (io && typeof io.existsSync === 'function' && io.existsSync(statePath)) {
        const raw = io.readFileSync(statePath, 'utf8');
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          if (isAutoVolumeEnabled(parsed.resolvedAutoVolumeValue)) {
            resolvedAutoVolumeValue = parsed.resolvedAutoVolumeValue === true ? true : 'on';
          }
          if (isVoiceLevelingSoundMode(parsed.resolvedSoundMode)) {
            resolvedSoundMode = parsed.resolvedSoundMode.trim();
            if (isPreferredVoiceSoundMode(resolvedSoundMode)) {
              preferredProbed = true;
            }
          }
          if (typeof parsed.preferredProbed === 'boolean') {
            preferredProbed = parsed.preferredProbed;
          }
          if (typeof parsed.enforceCount === 'number' && parsed.enforceCount >= 0) {
            enforceCount = parsed.enforceCount;
          }
          if (typeof parsed.lastEnforcedAt === 'string') {
            lastEnforcedAtIso = parsed.lastEnforcedAt;
          }
          if (typeof parsed.lastVerifiedAt === 'string') {
            lastVerifiedAtIso = parsed.lastVerifiedAt;
          }
          if (parsed.autoVolume !== undefined) {
            currentAutoVolume = parsed.autoVolume;
          }
          if (typeof parsed.soundMode === 'string') {
            currentSoundMode = parsed.soundMode;
          }
          if (typeof parsed.soundOutput === 'string') {
            currentSoundOutput = parsed.soundOutput;
          }
        }
      }
    } catch (_) {}
  }

  function persistState() {
    if (typeof writeAtomic !== 'function') return;
    try {
      const payload = {
        schema: 1,
        enabled: true,
        status: currentStatus,
        autoVolume: currentAutoVolume,
        soundMode: currentSoundMode,
        soundOutput: currentSoundOutput,
        resolvedAutoVolumeValue,
        resolvedSoundMode,
        preferredProbed,
        lastForegroundAppId: lastVerifiedFgAppId,
        lastEnforcedAt: lastEnforcedAtIso,
        lastVerifiedAt: lastVerifiedAtIso,
        enforceCount
      };
      writeAtomic(statePath, Buffer.from(JSON.stringify(payload, null, 2) + '\n', 'utf8'), 0o600);
    } catch (_) {}
  }

  function invokeLunaSync(uri, payload, timeoutMs = 1800) {
    try {
      const res = spawnSync(
        '/usr/bin/luna-send',
        ['-n', '1', '-w', String(timeoutMs), uri, JSON.stringify(payload || {})],
        { env, timeout: timeoutMs + 400 }
      );
      if (!res || res.status !== 0 || !res.stdout) return null;
      const text = Buffer.isBuffer(res.stdout)
        ? res.stdout.toString('utf8').trim()
        : String(res.stdout).trim();
      if (!text) return null;
      return JSON.parse(text);
    } catch (_) {
      return null;
    }
  }

  async function invokeLunaAsync(uri, payload, timeoutMs = 1800) {
    if (callLunaSend) {
      try {
        return await callLunaSend(uri, payload || {}, timeoutMs);
      } catch (_) {
        return null;
      }
    }
    return invokeLunaSync(uri, payload, timeoutMs);
  }

  function extractValidSettings(reply) {
    if (
      reply &&
      reply.returnValue === true &&
      reply.settings &&
      typeof reply.settings === 'object'
    ) {
      return reply.settings;
    }
    return null;
  }

  function querySoundSettingsSync() {
    const batch = invokeLunaSync(
      'luna://com.webos.settingsservice/getSystemSettings',
      { category: 'sound', keys: ['autoVolume', 'soundMode', 'soundOutput'] },
      1800
    );
    const batchSettings = extractValidSettings(batch);
    if (
      batchSettings &&
      ('autoVolume' in batchSettings || 'soundMode' in batchSettings)
    ) {
      return { ok: true, settings: { ...batchSettings } };
    }
    const merged = {};
    let anyKeyOk = false;
    for (const k of ['autoVolume', 'soundMode', 'soundOutput']) {
      const single = invokeLunaSync(
        'luna://com.webos.settingsservice/getSystemSettings',
        { category: 'sound', keys: [k] },
        1500
      );
      const s = extractValidSettings(single);
      if (s && k in s) {
        merged[k] = s[k];
        if (k === 'autoVolume' || k === 'soundMode') {
          anyKeyOk = true;
        }
      }
    }
    return { ok: anyKeyOk, settings: merged };
  }

  async function querySoundSettingsAsync() {
    const batch = await invokeLunaAsync(
      'luna://com.webos.settingsservice/getSystemSettings',
      { category: 'sound', keys: ['autoVolume', 'soundMode', 'soundOutput'] },
      1800
    );
    const batchSettings = extractValidSettings(batch);
    if (
      batchSettings &&
      ('autoVolume' in batchSettings || 'soundMode' in batchSettings)
    ) {
      return { ok: true, settings: { ...batchSettings } };
    }
    const merged = {};
    let anyKeyOk = false;
    for (const k of ['autoVolume', 'soundMode', 'soundOutput']) {
      const single = await invokeLunaAsync(
        'luna://com.webos.settingsservice/getSystemSettings',
        { category: 'sound', keys: [k] },
        1500
      );
      const s = extractValidSettings(single);
      if (s && k in s) {
        merged[k] = s[k];
        if (k === 'autoVolume' || k === 'soundMode') {
          anyKeyOk = true;
        }
      }
    }
    return { ok: anyKeyOk, settings: merged };
  }

  function buildSnapshot(extra = {}) {
    const autoVolumeActive = isAutoVolumeEnabled(currentAutoVolume);
    const soundModeActive = isVoiceLevelingSoundMode(currentSoundMode);
    return {
      enabled: true,
      ok: currentStatus === 'on',
      status: currentStatus,
      autoVolume: currentAutoVolume,
      autoVolumeActive,
      soundMode: currentSoundMode,
      soundModeActive,
      soundOutput: currentSoundOutput,
      resolvedSoundMode,
      foregroundAppId: lastVerifiedFgAppId,
      lastEnforcedAt: lastEnforcedAtIso,
      lastVerifiedAt: lastVerifiedAtIso,
      enforceCount,
      cached: Boolean(extra.cached),
      tvPower: extra.tvPower || lastPower
    };
  }

  function prepareCycleAndCheckCache(options = {}) {
    loadPersistedStateOnce();
    const now = nowMs();
    const tvPower = typeof options.tvPower === 'string' && options.tvPower ? options.tvPower : 'active';
    const fgAppId = typeof options.fgAppId === 'string' ? options.fgAppId.trim() : '';
    const force = Boolean(options.force === true);

    if (tvPower !== 'active') {
      verifiedForCurrentCycle = false;
      postSwitchRecheckPending = false;
      lastPower = tvPower;
      return {
        skip: true,
        snapshot: {
          ...buildSnapshot({ cached: true, tvPower }),
          status: currentStatus === 'on' ? 'standby' : currentStatus
        }
      };
    }

    if (!isLgServerBlocked()) {
      verifiedForCurrentCycle = false;
      lastPower = tvPower;
      return {
        skip: true,
        snapshot: {
          ...buildSnapshot({ cached: false, tvPower }),
          ok: false,
          status: 'deferred-until-lg-blocked'
        }
      };
    }

    if (lastObservedFgAppId !== null && fgAppId !== lastObservedFgAppId) {
      lastFgSwitchAtMs = now;
      postSwitchRecheckPending = true;
    }
    lastObservedFgAppId = fgAppId;

    const dueForPostSwitchRecheck =
      postSwitchRecheckPending && now - lastFgSwitchAtMs >= postSwitchRecheckDelayMs;

    if (
      !force &&
      !dueForPostSwitchRecheck &&
      verifiedForCurrentCycle &&
      currentStatus === 'on' &&
      lastPower === 'active' &&
      fgAppId === lastVerifiedFgAppId &&
      now - lastVerifiedAtMs < recheckIntervalMs
    ) {
      return {
        skip: true,
        snapshot: buildSnapshot({ cached: true, tvPower })
      };
    }

    return {
      skip: false,
      now,
      tvPower,
      fgAppId,
      dueForPostSwitchRecheck
    };
  }

  function buildAutoVolumeCandidates(currentVal) {
    const cands = [];
    if (resolvedAutoVolumeValue !== null) {
      cands.push(resolvedAutoVolumeValue);
    }
    if (typeof currentVal === 'boolean') {
      if (!cands.includes(true)) cands.push(true);
      if (!cands.includes('on')) cands.push('on');
    } else {
      if (!cands.includes('on')) cands.push('on');
      if (!cands.includes(true)) cands.push(true);
    }
    return cands;
  }

  function buildSoundModeCandidates() {
    const cands = [];
    if (resolvedSoundMode && isPreferredVoiceSoundMode(resolvedSoundMode)) {
      cands.push(resolvedSoundMode);
    }
    for (const m of PREFERRED_VOICE_SOUND_MODES) {
      if (!cands.includes(m)) cands.push(m);
    }
    if (resolvedSoundMode && !cands.includes(resolvedSoundMode)) {
      cands.push(resolvedSoundMode);
    }
    for (const m of FALLBACK_VOICE_SOUND_MODES) {
      if (!cands.includes(m)) cands.push(m);
    }
    return cands;
  }

  function isSoundModeAlreadySatisfied(modeVal) {
    if (isPreferredVoiceSoundMode(modeVal)) {
      resolvedSoundMode = modeVal.trim();
      preferredProbed = true;
      return true;
    }
    if (preferredProbed && isVoiceLevelingSoundMode(modeVal)) {
      resolvedSoundMode = modeVal.trim();
      return true;
    }
    return false;
  }

  function finalizeCycle(settings, changedAny, meta) {
    const { tvPower, fgAppId, dueForPostSwitchRecheck } = meta;
    const now = nowMs();
    const autoVolumeActive = isAutoVolumeEnabled(settings.autoVolume);
    const soundModeActive = isVoiceLevelingSoundMode(settings.soundMode);

    if (settings.autoVolume !== undefined) {
      currentAutoVolume = settings.autoVolume;
    }
    if (typeof settings.soundMode === 'string' && settings.soundMode.trim()) {
      currentSoundMode = settings.soundMode.trim();
    }
    if (typeof settings.soundOutput === 'string' && settings.soundOutput.trim()) {
      currentSoundOutput = settings.soundOutput.trim();
    }

    lastPower = tvPower;
    if (autoVolumeActive && soundModeActive) {
      currentStatus = 'on';
      verifiedForCurrentCycle = true;
      lastVerifiedFgAppId = fgAppId;
      lastVerifiedAtMs = now;
      lastVerifiedAtIso = new Date(now).toISOString();
      if (dueForPostSwitchRecheck) {
        postSwitchRecheckPending = false;
      }
    } else if (autoVolumeActive || soundModeActive) {
      currentStatus = 'partial';
      verifiedForCurrentCycle = false;
      lastVerifiedAtIso = new Date(now).toISOString();
    } else {
      currentStatus = 'off';
      verifiedForCurrentCycle = false;
    }

    if (changedAny) {
      enforceCount += 1;
      lastEnforcedAtIso = new Date(now).toISOString();
      log('voice_leveling_enforced', {
        status: currentStatus,
        autoVolume: currentAutoVolume,
        soundMode: currentSoundMode,
        soundOutput: currentSoundOutput,
        fgAppId,
        enforceCount
      });
    }

    if (currentStatus === 'on' || changedAny) {
      persistState();
    }

    return buildSnapshot({ cached: false, tvPower });
  }

  function enforceSync(options = {}) {
    const prep = prepareCycleAndCheckCache(options);
    if (prep.skip) return prep.snapshot;

    const initialRead = querySoundSettingsSync();
    if (!initialRead.ok) {
      verifiedForCurrentCycle = false;
      currentStatus = 'err';
      lastPower = prep.tvPower;
      return buildSnapshot({ cached: false, tvPower: prep.tvPower });
    }

    let settings = { ...initialRead.settings };
    let changedAny = false;

    if (isAutoVolumeEnabled(settings.autoVolume)) {
      resolvedAutoVolumeValue = settings.autoVolume === true ? true : 'on';
    } else {
      for (const avCandidate of buildAutoVolumeCandidates(settings.autoVolume)) {
        const setRes = invokeLunaSync(
          'luna://com.webos.settingsservice/setSystemSettings',
          { category: 'sound', settings: { autoVolume: avCandidate } },
          1800
        );
        if (setRes && setRes.returnValue === true) {
          const rb = querySoundSettingsSync();
          if (rb.ok) {
            settings = { ...settings, ...rb.settings };
            if (isAutoVolumeEnabled(settings.autoVolume)) {
              resolvedAutoVolumeValue = settings.autoVolume === true ? true : 'on';
              changedAny = true;
              break;
            }
          }
        }
      }
    }

    if (!isSoundModeAlreadySatisfied(settings.soundMode)) {
      const modeCandidates = buildSoundModeCandidates();
      for (const modeCandidate of modeCandidates) {
        if (
          FALLBACK_VOICE_SOUND_MODES.includes(modeCandidate) &&
          typeof settings.soundMode === 'string' &&
          settings.soundMode.trim() === modeCandidate
        ) {
          preferredProbed = true;
          resolvedSoundMode = modeCandidate;
          break;
        }
        const setRes = invokeLunaSync(
          'luna://com.webos.settingsservice/setSystemSettings',
          { category: 'sound', settings: { soundMode: modeCandidate } },
          1800
        );
        if (setRes && setRes.returnValue === true) {
          const rb = querySoundSettingsSync();
          if (rb.ok) {
            settings = { ...settings, ...rb.settings };
            const activeMode =
              typeof settings.soundMode === 'string' ? settings.soundMode.trim() : '';
            if (activeMode === modeCandidate || isPreferredVoiceSoundMode(activeMode)) {
              resolvedSoundMode = activeMode;
              if (isPreferredVoiceSoundMode(activeMode)) {
                preferredProbed = true;
              }
              changedAny = true;
              break;
            }
          }
        }
        if (modeCandidate === PREFERRED_VOICE_SOUND_MODES[PREFERRED_VOICE_SOUND_MODES.length - 1]) {
          preferredProbed = true;
        }
      }
    }

    return finalizeCycle(settings, changedAny, prep);
  }

  async function enforceAsync(options = {}) {
    const prep = prepareCycleAndCheckCache(options);
    if (prep.skip) return prep.snapshot;

    const initialRead = await querySoundSettingsAsync();
    if (!initialRead.ok) {
      verifiedForCurrentCycle = false;
      currentStatus = 'err';
      lastPower = prep.tvPower;
      return buildSnapshot({ cached: false, tvPower: prep.tvPower });
    }

    let settings = { ...initialRead.settings };
    let changedAny = false;

    if (isAutoVolumeEnabled(settings.autoVolume)) {
      resolvedAutoVolumeValue = settings.autoVolume === true ? true : 'on';
    } else {
      for (const avCandidate of buildAutoVolumeCandidates(settings.autoVolume)) {
        const setRes = await invokeLunaAsync(
          'luna://com.webos.settingsservice/setSystemSettings',
          { category: 'sound', settings: { autoVolume: avCandidate } },
          1800
        );
        if (setRes && setRes.returnValue === true) {
          const rb = await querySoundSettingsAsync();
          if (rb.ok) {
            settings = { ...settings, ...rb.settings };
            if (isAutoVolumeEnabled(settings.autoVolume)) {
              resolvedAutoVolumeValue = settings.autoVolume === true ? true : 'on';
              changedAny = true;
              break;
            }
          }
        }
      }
    }

    if (!isSoundModeAlreadySatisfied(settings.soundMode)) {
      const modeCandidates = buildSoundModeCandidates();
      for (const modeCandidate of modeCandidates) {
        if (
          FALLBACK_VOICE_SOUND_MODES.includes(modeCandidate) &&
          typeof settings.soundMode === 'string' &&
          settings.soundMode.trim() === modeCandidate
        ) {
          preferredProbed = true;
          resolvedSoundMode = modeCandidate;
          break;
        }
        const setRes = await invokeLunaAsync(
          'luna://com.webos.settingsservice/setSystemSettings',
          { category: 'sound', settings: { soundMode: modeCandidate } },
          1800
        );
        if (setRes && setRes.returnValue === true) {
          const rb = await querySoundSettingsAsync();
          if (rb.ok) {
            settings = { ...settings, ...rb.settings };
            const activeMode =
              typeof settings.soundMode === 'string' ? settings.soundMode.trim() : '';
            if (activeMode === modeCandidate || isPreferredVoiceSoundMode(activeMode)) {
              resolvedSoundMode = activeMode;
              if (isPreferredVoiceSoundMode(activeMode)) {
                preferredProbed = true;
              }
              changedAny = true;
              break;
            }
          }
        }
        if (modeCandidate === PREFERRED_VOICE_SOUND_MODES[PREFERRED_VOICE_SOUND_MODES.length - 1]) {
          preferredProbed = true;
        }
      }
    }

    return finalizeCycle(settings, changedAny, prep);
  }

  return {
    enforceSync,
    enforceAsync,
    getStatus: () => currentStatus,
    getSnapshot: () => buildSnapshot({ cached: true, tvPower: lastPower }),
    resetCache: () => {
      verifiedForCurrentCycle = false;
      postSwitchRecheckPending = false;
    }
  };
}

function createResidentMaintenanceController(deps = {}) {
  const io = deps.fs || fs;
  const nowMs = typeof deps.nowMs === 'function' ? deps.nowMs : () => Date.now();
  const sleep = typeof deps.sleep === 'function' ? deps.sleep : () => Promise.resolve();
  const getTvPower = typeof deps.getTvPower === 'function' ? deps.getTvPower : () => 'active';
  const callLunaSend = deps.callLunaSend;
  const callLunaSendLarge = deps.callLunaSendLarge;
  const postMaintenanceReport =
    typeof deps.postMaintenanceReport === 'function'
      ? deps.postMaintenanceReport
      : async () => true;
  const ensurePrivacyRetirements =
    typeof deps.ensurePrivacyRetirements === 'function'
      ? deps.ensurePrivacyRetirements
      : () => ({ ok: true });
  const ensureWakeOnLanEnabled =
    typeof deps.ensureWakeOnLanEnabled === 'function' ? deps.ensureWakeOnLanEnabled : () => {};
  const ensureAutoVoiceLeveling =
    typeof deps.ensureAutoVoiceLeveling === 'function'
      ? deps.ensureAutoVoiceLeveling
      : async () => ({
          enabled: true,
          ok: true,
          status: 'on',
          autoVolume: 'on',
          soundMode: 'clearVoice'
        });
  const applyLgNetworkBlock =
    typeof deps.applyLgNetworkBlock === 'function' ? deps.applyLgNetworkBlock : () => true;
  const isLgServerBlocked =
    typeof deps.isLgServerBlocked === 'function' ? deps.isLgServerBlocked : () => true;
  const activityTracker = deps.activityTracker || createActivityTracker({ nowMs });
  const readUpdaterState =
    typeof deps.readUpdaterState === 'function'
      ? deps.readUpdaterState
      : () => ({ schema: 1, lastUpdateDay: null, lastUpdateAt: null, lastReason: null });
  const writeUpdaterState =
    typeof deps.writeUpdaterState === 'function' ? deps.writeUpdaterState : () => {};
  const updaterTriggerPath = deps.updaterTriggerPath || '/tmp/r2d2-trigger-updater.json';
  const allowlistApps = Array.isArray(deps.allowlistApps) ? deps.allowlistApps : [];
  const idleThresholdMs =
    typeof deps.idleThresholdMs === 'number' && deps.idleThresholdMs > 0
      ? deps.idleThresholdMs
      : DEFAULT_IDLE_THRESHOLD_MS;
  const collectDiagnostics =
    typeof deps.collectDiagnostics === 'function' ? deps.collectDiagnostics : () => ({});
  const scheduleImmediate =
    typeof deps.scheduleImmediate === 'function'
      ? deps.scheduleImmediate
      : (fn) => setImmediate(fn);
  const log = typeof deps.log === 'function' ? deps.log : () => {};

  let maintenanceRunning = false;
  let lastMaintenanceReqId = null;

  function sampleActivity() {
    return activityTracker.sampleSync();
  }

  async function executeTvMaintenance(voiceKey, reqId, mode, targetDay) {
    if (maintenanceRunning) return null;
    maintenanceRunning = true;
    const isUpdateMode = mode === 'update' || mode === 'update-standby';
    const currentPower = getTvPower();
    const priorState = readUpdaterState() || {};
    try {
      log('tv_maintenance_start', { reqId, mode });
      ensurePrivacyRetirements();
      ensureWakeOnLanEnabled();

      const wf = await runUpdateWorkflow({
        mode,
        tvPower: currentPower,
        callLunaSend,
        callLunaSendLarge,
        setLgEgressWindow: async (open) => applyLgNetworkBlock(Boolean(open)),
        sleep,
        nowMs,
        hasUserActivitySince: (sinceMs) => activityTracker.hasActivitySince(sinceMs),
        sampleActivity: async () => activityTracker.sampleAsync(callLunaSend),
        allowlistApps,
        knownInProgressApps: Array.isArray(priorState.inProgressApps)
          ? priorState.inProgressApps
          : [],
        wakeOwnedByMaintenance:
          typeof deps.isWakeOwned === 'function' ? Boolean(deps.isWakeOwned()) : true,
        installTimeoutMs: deps.installTimeoutMs,
        maxInstallTimeoutMs: deps.maxInstallTimeoutMs,
        pollIntervalMs: deps.pollIntervalMs
      });

      if (isUpdateMode && currentPower === 'active') {
        const now = new Date(nowMs());
        const resolvedTargetDay =
          targetDay ||
          (now.getHours() < 3 ? previousLocalDay(now) : formatLocalDay(now));
        const nextState = computeNextUpdaterState(readUpdaterState(), wf, {
          nowDate: now,
          targetDay: resolvedTargetDay,
          reqId
        });
        writeUpdaterState(nextState);
      }

      applyLgNetworkBlock(false);
      let voiceLevelingReport = null;
      if (isLgServerBlocked()) {
        try {
          voiceLevelingReport = await Promise.resolve(
            ensureAutoVoiceLeveling({
              tvPower: currentPower,
              fgAppId: wf.foregroundAppId || '',
              force: true
            })
          );
        } catch (_) {}
      }

      const diag = collectDiagnostics() || {};
      const report = {
        schema: 1,
        reqId,
        mode,
        ts: new Date(nowMs()).toISOString(),
        tvPower: currentPower,
        tvMacs: diag.tvMacs || [],
        foregroundAppId: wf.foregroundAppId,
        fences: diag.fences || '4/4',
        privacy: diag.privacyStatus || 'ok',
        privacyRetirements: diag.privacyStatus || 'ok',
        lgServerBlocked: isLgServerBlocked(),
        voiceLeveling: voiceLevelingReport,
        privacyManifestsRemoved:
          typeof diag.privacyManifestsRemovedCount === 'number'
            ? diag.privacyManifestsRemovedCount
            : 0,
        privacyProcsKilled:
          typeof diag.lastRetiredProcs === 'number' ? diag.lastRetiredProcs : 0,
        manifest: diag.manifestStatus || 'ok',
        activationSummaryOk:
          diag.activationSummaryOk !== undefined ? diag.activationSummaryOk : true,
        recentLogs: diag.recentLogs || [],
        legacyLogs: diag.legacyLogs || [],
        homeApp: wf.homeApp,
        listAppsOk: wf.listAppsOk,
        installedCount: wf.installedCount,
        updateCacheOk: wf.updateCacheOk,
        updatesAttempted: wf.updatesAttempted,
        updatesCompleted: wf.updatesCompleted,
        updatesFailed: wf.updatesFailed,
        updatesTimedOut: wf.updatesTimedOut,
        updatesInProgress: wf.updatesInProgress,
        updatesSkippedForeground: wf.updatesSkippedForeground,
        pendingApps: wf.pendingApps,
        maintenanceComplete: wf.maintenanceComplete,
        screenTurnedOff: wf.screenTurnedOff,
        returnedToStandby: wf.returnedToStandby,
        allowlistedApps: wf.allowlistedApps
      };

      const posted = await postMaintenanceReport(voiceKey, report);
      log('tv_maintenance_complete', {
        reqId,
        mode,
        posted,
        installedCount: wf.installedCount,
        updatesAttempted: wf.updatesAttempted.length,
        updatesCompleted: wf.updatesCompleted.length,
        maintenanceComplete: wf.maintenanceComplete,
        lgServerBlocked: report.lgServerBlocked,
        returnedToStandby: wf.returnedToStandby
      });
      return report;
    } catch (e) {
      log('tv_maintenance_error', {
        reqId,
        mode,
        error: String(e && (e.code || e.message || e))
      });
      return null;
    } finally {
      applyLgNetworkBlock(false);
      maintenanceRunning = false;
    }
  }

  function maybeTriggerScheduledOrIdleUpdate(voiceKey) {
    const now = new Date(nowMs());
    const currentPower = getTvPower();
    // Always sample user activity on active power, even while maintenance is running!
    const act =
      currentPower === 'active'
        ? activityTracker.sampleSync()
        : activityTracker.getState();

    if (currentPower !== 'active') {
      const stOff = readUpdaterState();
      const decOff = shouldRunScheduledUpdate(stOff, now, {
        tvPower: currentPower,
        foregroundAppId: act.fgAppId,
        idleDurationMs: act.idleDurationMs,
        idleThresholdMs
      });
      if (decOff.recordMissedDay && stOff.missedWindowDay !== decOff.recordMissedDay) {
        const nextOff = { ...stOff, schema: 1, missedWindowDay: decOff.recordMissedDay };
        writeUpdaterState(nextOff);
      }
      return { triggered: false, reason: 'tv-not-active', activity: act };
    }

    if (maintenanceRunning) {
      return { triggered: false, reason: 'maintenance-running', activity: act };
    }

    try {
      if (io.existsSync(updaterTriggerPath)) {
        const raw = JSON.parse(io.readFileSync(updaterTriggerPath, 'utf8'));
        try {
          io.unlinkSync(updaterTriggerPath);
        } catch (_) {}
        const consumed = consumeUpdaterTrigger(raw, now, readUpdaterState());
        if (consumed.immediate) {
          const targetDay = consumed.targetDay || formatLocalDay(now);
          scheduleImmediate(() =>
            executeTvMaintenance(voiceKey, consumed.reqId, consumed.mode, targetDay).catch(
              () => {}
            )
          );
          return {
            triggered: true,
            reason: 'trigger-immediate',
            reqId: consumed.reqId,
            mode: consumed.mode,
            targetDay,
            activity: act
          };
        }
        if (consumed.deferredToIdle) {
          writeUpdaterState(consumed.state);
        }
      }
    } catch (_) {
      try {
        io.unlinkSync(updaterTriggerPath);
      } catch (_) {}
    }

    const st = readUpdaterState();
    const dec = shouldRunScheduledUpdate(st, now, {
      tvPower: currentPower,
      foregroundAppId: act.fgAppId,
      idleDurationMs: act.idleDurationMs,
      idleThresholdMs
    });
    if (dec.recordMissedDay && st.missedWindowDay !== dec.recordMissedDay) {
      writeUpdaterState({ ...st, schema: 1, missedWindowDay: dec.recordMissedDay });
    }
    if (dec.run) {
      const reqId = dec.reqId || 'tv-' + dec.reason + '-' + dec.today;
      const targetDay = dec.targetDay || dec.today;
      scheduleImmediate(() =>
        executeTvMaintenance(voiceKey, reqId, dec.mode || 'update', targetDay).catch(() => {})
      );
      return {
        triggered: true,
        reason: dec.reason,
        reqId,
        mode: dec.mode || 'update',
        targetDay,
        activity: act
      };
    }
    return { triggered: false, reason: dec.reason, activity: act };
  }

  function handleMaintenanceHeader(voiceKey, maintHdr) {
    if (typeof maintHdr !== 'string' || !maintHdr.includes(':')) {
      return { accepted: false };
    }
    const [reqId, rawMode] = maintHdr.split(':', 2);
    if (!reqId || reqId === lastMaintenanceReqId) {
      return { accepted: false };
    }
    const now = new Date(nowMs());
    const consumed = consumeUpdaterTrigger({ reqId, mode: rawMode }, now, readUpdaterState());
    if (!consumed.immediate) {
      lastMaintenanceReqId = reqId;
      writeUpdaterState(consumed.state);
      const schedRes = maybeTriggerScheduledOrIdleUpdate(voiceKey);
      return {
        accepted: true,
        deferredToIdle: !schedRes.triggered,
        triggered: schedRes.triggered,
        reqId
      };
    }
    if (maintenanceRunning) {
      return { accepted: false, reason: 'maintenance-running' };
    }
    const mode = consumed.mode;
    const targetDay = consumed.targetDay || formatLocalDay(now);
    lastMaintenanceReqId = reqId;
    scheduleImmediate(() =>
      executeTvMaintenance(voiceKey, reqId, mode, targetDay).catch(() => {})
    );
    return { accepted: true, deferredToIdle: false, triggered: true, reqId, mode, targetDay };
  }

  function shutdown() {
    try {
      return applyLgNetworkBlock(false);
    } catch (_) {
      return false;
    }
  }

  return {
    sampleActivity,
    executeTvMaintenance,
    maybeTriggerScheduledOrIdleUpdate,
    handleMaintenanceHeader,
    shutdown,
    isMaintenanceRunning: () => maintenanceRunning,
    getLastMaintenanceReqId: () => lastMaintenanceReqId
  };
}

module.exports = {
  BLOCK_BEGIN,
  BLOCK_END,
  LG_PERMANENT_BLOCK_DOMAINS,
  LG_APPSTORE_DOMAINS,
  LG_PERMANENT_BLOCK_IPS,
  LG_PERMANENT_BLOCK_IPV6S,
  NEVER_UPDATE_APPS,
  IDLE_FOREGROUND_APPS,
  DEFAULT_IDLE_THRESHOLD_MS,
  DEFAULT_INSTALL_TIMEOUT_MS,
  DEFAULT_MAX_INSTALL_TIMEOUT_MS,
  DEFAULT_VOICE_LEVELING_RECHECK_MS,
  DEFAULT_POST_SWITCH_RECHECK_DELAY_MS,
  PREFERRED_VOICE_SOUND_MODES,
  FALLBACK_VOICE_SOUND_MODES,
  VOICE_LEVELING_SOUND_MODES,
  isAutoVolumeEnabled,
  isPreferredVoiceSoundMode,
  isVoiceLevelingSoundMode,
  sha256Hex,
  stripManagedHostsBlock,
  renderBlockedHosts,
  buildIptablesCommands,
  buildIp6tablesCommands,
  formatLocalDay,
  previousLocalDay,
  isForegroundIdle,
  isValidForegroundReply,
  extractInterruptDigest,
  createActivityTracker,
  consumeUpdaterTrigger,
  shouldRunScheduledUpdate,
  computeNextUpdaterState,
  selectUpdatableApps,
  runUpdateWorkflow,
  createLgNetworkGuard,
  createPrivacyRetirementEnforcer,
  createAutoVoiceLevelingEnforcer,
  createResidentMaintenanceController
};
