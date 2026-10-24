'use strict';

const assert = require('assert');
const maint = require('./tv_maintenance');

let groups = 0;

(async () => {
  // 1. 24/7 LG server hosts sinkhole vs temporary App Store window
  {
    const baseHosts = '127.0.0.1\tlocalhost\n::1\tlocalhost\n';
    const fullBlock = maint.renderBlockedHosts(baseHosts, { allowAppStore: false });
    for (const d of maint.LG_PERMANENT_BLOCK_DOMAINS) {
      assert(fullBlock.includes('0.0.0.0 ' + d), 'Missing permanent block for ' + d);
      assert(fullBlock.includes(':: ' + d), 'Missing IPv6 permanent block for ' + d);
    }
    for (const d of maint.LG_APPSTORE_DOMAINS) {
      assert(fullBlock.includes('0.0.0.0 ' + d), 'Missing 24/7 appstore block for ' + d);
    }

    const windowOpen = maint.renderBlockedHosts(fullBlock, { allowAppStore: true });
    for (const d of maint.LG_PERMANENT_BLOCK_DOMAINS) {
      assert(windowOpen.includes('0.0.0.0 ' + d), 'Permanent block must remain during appstore window: ' + d);
    }
    for (const d of maint.LG_APPSTORE_DOMAINS) {
      assert(!windowOpen.includes('0.0.0.0 ' + d), 'Appstore domain must be unblocked during window: ' + d);
    }

    const stripped = maint.stripManagedHostsBlock(fullBlock);
    assert.strictEqual(stripped, baseHosts);
    groups++;
  }

  // 2. iptables rules block 156.147.69.32 permanently without blocking consumer CDNs
  {
    const cmds = maint.buildIptablesCommands().map((c) => c.join(' '));
    assert(cmds.includes('-A R2D2_LG_GUARD -d 156.147.69.32 -j REJECT'));
    assert(cmds.includes('-A R2D2_LG_GUARD -j RETURN'));
    groups++;
  }

  // 3. Scheduler triggers at 3:00 AM and catches up after 30 minutes of idle if 3:00 AM was missed
  {
    const at3am = new Date(2026, 9, 25, 3, 5, 0);
    const r1 = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-24' },
      at3am,
      { tvPower: 'active', foregroundAppId: 'netflix', idleDurationMs: 0 }
    );
    assert.strictEqual(r1.run, true);
    assert.strictEqual(r1.reason, 'nightly-3am');

    const r1Done = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-25' },
      at3am,
      { tvPower: 'active', foregroundAppId: 'local.owner.applibrary', idleDurationMs: 3600000 }
    );
    assert.strictEqual(r1Done.run, false);
    assert.strictEqual(r1Done.reason, 'already-updated-today');

    const offAt3am = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-24' },
      at3am,
      { tvPower: 'standby', foregroundAppId: 'local.owner.applibrary', idleDurationMs: 3600000 }
    );
    assert.strictEqual(offAt3am.run, false);
    assert.strictEqual(offAt3am.reason, 'tv-not-active');

    const afternoonShortIdle = new Date(2026, 9, 25, 14, 0, 0);
    const r2 = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-24' },
      afternoonShortIdle,
      { tvPower: 'active', foregroundAppId: 'local.owner.applibrary', idleDurationMs: 10 * 60 * 1000 }
    );
    assert.strictEqual(r2.run, false);

    const r3 = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-24' },
      afternoonShortIdle,
      { tvPower: 'active', foregroundAppId: 'local.owner.applibrary', idleDurationMs: 30 * 60 * 1000 }
    );
    assert.strictEqual(r3.run, true);
    assert.strictEqual(r3.reason, 'idle-catchup');
    groups++;
  }

  // 4. All installed apps with updateYN === 'Y' are updated; foreground app and Home/retired apps are protected
  {
    const windowTransitions = [];
    const lunaCalls = [];
    const res = await maint.runUpdateWorkflow({
      mode: 'update',
      tvPower: 'active',
      allowlistApps: ['netflix', 'youtube.leanback.v4'],
      setLgEgressWindow: async (open) => {
        windowTransitions.push(open);
      },
      callLunaSend: async (uri, payload) => {
        lunaCalls.push({ uri, payload });
        if (uri === 'luna://com.webos.applicationManager/getForegroundAppInfo') {
          return { returnValue: true, appId: 'netflix' };
        }
        if (uri === 'luna://com.webos.appInstallService/install') {
          return { returnValue: true };
        }
        return { returnValue: true };
      },
      callLunaSendLarge: async (uri) => {
        if (uri === 'luna://com.webos.applicationManager/listApps') {
          return {
            returnValue: true,
            apps: [
              { id: 'local.owner.applibrary', version: '1.0.0' },
              { id: 'netflix', version: '2.0.0' },
              { id: 'youtube.leanback.v4', version: '3.0.0' },
              { id: 'plex', version: '4.1.0' },
              { id: 'com.webos.app.adapp', version: '1.0.0' }
            ]
          };
        }
        if (uri === 'luna://com.webos.appUpdateService/statusAll') {
          return {
            returnValue: true,
            status: {
              apps: [
                { id: 'local.owner.applibrary', updateYN: 'Y', version: '9.9.9' },
                { id: 'netflix', updateYN: 'Y', version: '2.1.0' },
                { id: 'youtube.leanback.v4', updateYN: 'Y', version: '3.1.0' },
                { id: 'plex', updateYN: 'Y', version: '4.2.0' },
                { id: 'com.webos.app.adapp', updateYN: 'Y', version: '2.0.0' }
              ]
            }
          };
        }
        if (uri === 'luna://com.webos.appInstallService/status') {
          return {
            returnValue: true,
            status: {
              apps: [
                { id: 'youtube.leanback.v4', state: 'installed' },
                { id: 'plex', state: 'installed' }
              ]
            }
          };
        }
        return { returnValue: true };
      }
    });

    assert.deepStrictEqual(windowTransitions, [true, false]);
    assert.deepStrictEqual(res.updatesSkippedForeground, ['netflix']);
    assert.deepStrictEqual(res.updatesAttempted, ['youtube.leanback.v4', 'plex']);
    assert.deepStrictEqual(res.updatesCompleted, ['youtube.leanback.v4', 'plex']);
    assert.strictEqual(res.screenTurnedOff, false);
    assert.strictEqual(res.returnedToStandby, false);
    groups++;
  }

  // 5. Woken-from-standby 3 AM mode ('update-standby') turns off screen, updates all apps, and returns to standby
  {
    const windowTransitions = [];
    const powerCalls = [];
    const res = await maint.runUpdateWorkflow({
      mode: 'update-standby',
      tvPower: 'active',
      setLgEgressWindow: async (open) => {
        windowTransitions.push(open);
      },
      callLunaSend: async (uri, payload) => {
        if (uri.includes('tvpower')) {
          powerCalls.push({ uri, payload });
          return { returnValue: true };
        }
        if (uri === 'luna://com.webos.applicationManager/getForegroundAppInfo') {
          return { returnValue: true, appId: 'netflix' };
        }
        if (uri === 'luna://com.webos.appInstallService/install') {
          return { returnValue: true };
        }
        return { returnValue: true };
      },
      callLunaSendLarge: async (uri) => {
        if (uri === 'luna://com.webos.applicationManager/listApps') {
          return {
            returnValue: true,
            apps: [{ id: 'netflix', version: '2.0.0' }]
          };
        }
        if (uri === 'luna://com.webos.appUpdateService/statusAll') {
          return {
            returnValue: true,
            status: {
              apps: [{ id: 'netflix', updateYN: 'Y', version: '2.1.0' }]
            }
          };
        }
        if (uri === 'luna://com.webos.appInstallService/status') {
          return {
            returnValue: true,
            status: {
              apps: [{ id: 'netflix', state: 'installed' }]
            }
          };
        }
        return { returnValue: true };
      }
    });

    assert.deepStrictEqual(windowTransitions, [true, false]);
    assert.deepStrictEqual(res.updatesCompleted, ['netflix']);
    assert.strictEqual(res.screenTurnedOff, true);
    assert.strictEqual(res.returnedToStandby, true);
    assert.strictEqual(powerCalls.length, 2);
    assert.strictEqual(powerCalls[0].uri, 'luna://com.webos.service.tvpower/power/turnOffScreen');
    assert.strictEqual(powerCalls[1].uri, 'luna://com.webos.service.tvpower/power/turnOff');
    groups++;
  }

  // 6. If user activity occurs during a 3 AM 'update-standby' run, TV remains on instead of powering off
  {
    const powerCalls = [];
    const res = await maint.runUpdateWorkflow({
      mode: 'update-standby',
      tvPower: 'active',
      hasUserActivitySince: () => true,
      callLunaSend: async (uri, payload) => {
        if (uri.includes('tvpower')) {
          powerCalls.push({ uri, payload });
        }
        return { returnValue: true };
      },
      callLunaSendLarge: async () => ({ returnValue: true, apps: [], status: { apps: [] } })
    });
    assert.strictEqual(res.screenTurnedOff, true);
    assert.strictEqual(res.returnedToStandby, false);
    assert.strictEqual(powerCalls.length, 1);
    assert.strictEqual(powerCalls[0].uri, 'luna://com.webos.service.tvpower/power/turnOffScreen');
    groups++;
  }

  // 7. Even if an exception occurs mid-update, setLgEgressWindow(false) is guaranteed to run in finally
  {
    const windowTransitions = [];
    let caught = false;
    try {
      await maint.runUpdateWorkflow({
        mode: 'update',
        tvPower: 'active',
        setLgEgressWindow: async (open) => {
          windowTransitions.push(open);
        },
        callLunaSend: async () => ({ returnValue: true, appId: 'local.owner.applibrary' }),
        callLunaSendLarge: async (uri) => {
          if (uri.includes('statusAll')) {
            throw new Error('simulated bus timeout');
          }
          return { returnValue: true, apps: [] };
        }
      });
    } catch (_) {
      caught = true;
    }
    assert.strictEqual(caught, true);
    assert.deepStrictEqual(windowTransitions, [true, false]);
    groups++;
  }

  process.stdout.write(JSON.stringify({ groups, nativeCalls: 0, modelCalls: 0 }) + '\n');
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
