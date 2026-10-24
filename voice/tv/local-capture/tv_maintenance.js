'use strict';

const fs = require('fs');
const cp = require('child_process');
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

function buildIptablesCommands() {
  const cmds = [
    ['-N', 'R2D2_LG_GUARD'],
    ['-F', 'R2D2_LG_GUARD'],
    ['-D', 'OUTPUT', '-j', 'R2D2_LG_GUARD'],
    ['-I', 'OUTPUT', '1', '-j', 'R2D2_LG_GUARD']
  ];
  for (const ip of LG_PERMANENT_BLOCK_IPS) {
    cmds.push(['-A', 'R2D2_LG_GUARD', '-d', ip, '-j', 'REJECT']);
  }
  cmds.push(['-A', 'R2D2_LG_GUARD', '-j', 'RETURN']);
  return cmds;
}

function formatLocalDay(dateObj) {
  const y = dateObj.getFullYear();
  const m = String(dateObj.getMonth() + 1).padStart(2, '0');
  const d = String(dateObj.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function isForegroundIdle(foregroundAppId) {
  if (foregroundAppId === null || foregroundAppId === undefined) return true;
  return IDLE_FOREGROUND_APPS.has(String(foregroundAppId).trim());
}

function shouldRunScheduledUpdate(state, nowDate, context = {}) {
  const s = state && typeof state === 'object' ? state : {};
  const d = nowDate instanceof Date ? nowDate : new Date(nowDate);
  const today = formatLocalDay(d);
  const hour = d.getHours();
  const tvPower = context.tvPower || 'unknown';
  const idleMs = typeof context.idleDurationMs === 'number' ? context.idleDurationMs : 0;
  const thresholdMs =
    typeof context.idleThresholdMs === 'number' && context.idleThresholdMs > 0
      ? context.idleThresholdMs
      : DEFAULT_IDLE_THRESHOLD_MS;
  const fgIdle = isForegroundIdle(context.foregroundAppId);

  if (tvPower !== 'active') {
    return { run: false, reason: 'tv-not-active', today };
  }
  if (s.lastUpdateDay === today) {
    return { run: false, reason: 'already-updated-today', today };
  }
  if (hour === 3) {
    return { run: true, reason: 'nightly-3am', mode: 'update', today };
  }
  if (fgIdle && idleMs >= thresholdMs) {
    return { run: true, reason: 'idle-catchup', mode: 'update', today };
  }
  return { run: false, reason: 'waiting-for-window-or-idle', today };
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

  for (const item of Array.isArray(updateStatusApps) ? updateStatusApps : []) {
    if (!item) continue;
    const id = typeof item.id === 'string' ? item.id.trim() : typeof item.appId === 'string' ? item.appId.trim() : '';
    if (!id || seen.has(id) || NEVER_UPDATE_APPS.has(id)) continue;
    if (installedIds.size > 0 && !installedIds.has(id)) continue;
    if (item.updateYN === 'Y') {
      seen.add(id);
      if (foregroundAppId && id === foregroundAppId) {
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
    allowlistApps = []
  } = deps;

  const isUpdateMode = mode === 'update' || mode === 'update-standby';
  const returnToStandby = mode === 'update-standby';
  const startedAtMs = nowMs();

  const fgReply = await callLunaSend('luna://com.webos.applicationManager/getForegroundAppInfo', {}, 2500);
  const foregroundAppId = fgReply && typeof fgReply.appId === 'string' ? fgReply.appId : null;

  const listReply = await callLunaSendLarge('luna://com.webos.applicationManager/listApps', {}, 5000, 1048576);
  const allInstalledApps = listReply && Array.isArray(listReply.apps) ? listReply.apps : [];
  const installedMap = new Map();
  let homeAppPresent = false;
  let homeAppVersion = null;

  for (const app of allInstalledApps) {
    if (!app || typeof app.id !== 'string') continue;
    if (app.id === 'local.owner.applibrary') {
      homeAppPresent = true;
      homeAppVersion = typeof app.version === 'string' ? app.version : null;
    }
    if (!NEVER_UPDATE_APPS.has(app.id)) {
      installedMap.set(app.id, {
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
  const updatesSkippedForeground = [];
  let screenTurnedOff = false;
  let returnedToStandby = false;

  if (typeof setLgEgressWindow === 'function') {
    await setLgEgressWindow(isUpdateMode && tvPower === 'active');
  }

  try {
    if (returnToStandby && tvPower === 'active') {
      const offRes = await callLunaSend('luna://com.webos.service.tvpower/power/turnOffScreen', {}, 2500);
      screenTurnedOff = Boolean(offRes && offRes.returnValue === true);
    }

    updateReply = await callLunaSendLarge(
      'luna://com.webos.appUpdateService/statusAll',
      { subscribe: false },
      4000,
      262144
    );
    const updateApps =
      updateReply && updateReply.status && Array.isArray(updateReply.status.apps)
        ? updateReply.status.apps
        : [];
    for (const item of updateApps) {
      if (!item) continue;
      const id = typeof item.id === 'string' ? item.id : typeof item.appId === 'string' ? item.appId : null;
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
    const installApps =
      installReply && installReply.status && Array.isArray(installReply.status.apps)
        ? installReply.status.apps
        : [];
    for (const item of installApps) {
      if (!item) continue;
      const id = typeof item.id === 'string' ? item.id : typeof item.appId === 'string' ? item.appId : null;
      if (id && !NEVER_UPDATE_APPS.has(id)) {
        installMap.set(id, {
          state: typeof item.state === 'string' ? item.state : null,
          progress: typeof item.progress === 'number' ? item.progress : null,
          errorCode: typeof item.errorCode === 'number' ? item.errorCode : null
        });
      }
    }

    if (isUpdateMode && tvPower === 'active') {
      const effectiveFg = returnToStandby ? null : foregroundAppId;
      const { updatable, skippedForeground } = selectUpdatableApps(allInstalledApps, updateApps, effectiveFg);
      updatesSkippedForeground.push(...skippedForeground);

      for (const appId of updatable) {
        updatesAttempted.push(appId);
        const inst = await callLunaSend(
          'luna://com.webos.appInstallService/install',
          {
            id: appId,
            subscribe: false,
            silence: true
          },
          4000
        );
        if (inst && inst.returnValue === true) {
          const deadline = nowMs() + 45000;
          while (nowMs() < deadline) {
            await sleep(2000);
            const st = await callLunaSendLarge(
              'luna://com.webos.appInstallService/status',
              { subscribe: false },
              3500,
              262144
            );
            const jobs = st && st.status && Array.isArray(st.status.apps) ? st.status.apps : [];
            const job = jobs.find((j) => j && (j.id === appId || j.appId === appId));
            if (
              !job ||
              job.state === 'installed' ||
              job.state === 'install failed' ||
              job.state === 'download failed' ||
              job.state === 'canceled'
            ) {
              if (!job || job.state === 'installed') {
                updatesCompleted.push(appId);
              }
              break;
            }
          }
        }
      }

      if (updatesCompleted.length > 0) {
        const afterReply = await callLunaSendLarge(
          'luna://com.webos.applicationManager/listApps',
          {},
          5000,
          1048576
        );
        if (afterReply && Array.isArray(afterReply.apps)) {
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
      await setLgEgressWindow(false);
    }
  }

  if (returnToStandby && tvPower === 'active' && !hasUserActivitySince(startedAtMs)) {
    const standbyRes = await callLunaSend(
      'luna://com.webos.service.tvpower/power/turnOff',
      { reason: 'remoteKey' },
      3000
    );
    returnedToStandby = Boolean(standbyRes && standbyRes.returnValue === true);
  }

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
    homeApp: {
      present: homeAppPresent,
      version: homeAppVersion
    },
    installedCount: installedMap.size,
    updateCacheOk: Boolean(updateReply && updateReply.returnValue === true),
    updatesAttempted,
    updatesCompleted,
    updatesSkippedForeground,
    screenTurnedOff,
    returnedToStandby,
    allowlistedApps
  };
}

module.exports = {
  BLOCK_BEGIN,
  BLOCK_END,
  LG_PERMANENT_BLOCK_DOMAINS,
  LG_APPSTORE_DOMAINS,
  LG_PERMANENT_BLOCK_IPS,
  NEVER_UPDATE_APPS,
  IDLE_FOREGROUND_APPS,
  DEFAULT_IDLE_THRESHOLD_MS,
  stripManagedHostsBlock,
  renderBlockedHosts,
  buildIptablesCommands,
  formatLocalDay,
  isForegroundIdle,
  shouldRunScheduledUpdate,
  selectUpdatableApps,
  runUpdateWorkflow
};
