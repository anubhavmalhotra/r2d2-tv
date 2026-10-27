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
      assert(
        windowOpen.includes('0.0.0.0 ' + d),
        'Permanent block must remain during appstore window: ' + d
      );
    }
    for (const d of maint.LG_APPSTORE_DOMAINS) {
      assert(
        !windowOpen.includes('0.0.0.0 ' + d),
        'Appstore domain must be unblocked during window: ' + d
      );
    }

    const stripped = maint.stripManagedHostsBlock(fullBlock);
    assert.strictEqual(stripped, baseHosts);
    groups++;
  }

  // 2. iptables rules block 156.147.69.32 permanently and block harvested App Store IPs when window closes
  {
    const openCmds = maint
      .buildIptablesCommands({ allowAppStore: true, appStoreIps: ['203.0.113.50'] })
      .map((c) => c.join(' '));
    assert(openCmds.includes('-A R2D2_LG_GUARD -d 156.147.69.32 -j REJECT'));
    assert(!openCmds.includes('-A R2D2_LG_GUARD -d 203.0.113.50 -j REJECT'));

    const closedCmds = maint
      .buildIptablesCommands({ allowAppStore: false, appStoreIps: ['203.0.113.50'] })
      .map((c) => c.join(' '));
    assert(closedCmds.includes('-A R2D2_LG_GUARD -d 156.147.69.32 -j REJECT'));
    assert(closedCmds.includes('-A R2D2_LG_GUARD -d 203.0.113.50 -j REJECT'));
    assert(closedCmds.includes('-A R2D2_LG_GUARD -j RETURN'));
    groups++;
  }

  // 3. Scheduler: 3 AM window, missed-window persistence, no pre-3-AM consumption, and 30-min idle requirement
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
    assert.strictEqual(offAt3am.recordMissedDay, '2026-10-25');

    // Before 3:00 AM (e.g. 00:40 AM on Oct 25) when Oct 24 already updated: must NOT run early and consume Oct 25
    const before3am = new Date(2026, 9, 25, 0, 40, 0);
    const rBefore3am = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-24' },
      before3am,
      {
        tvPower: 'active',
        foregroundAppId: 'local.owner.applibrary',
        idleDurationMs: 45 * 60 * 1000
      }
    );
    assert.strictEqual(rBefore3am.run, false);

    // Before 3:00 AM when yesterday (Oct 24) was missed: catches up Oct 24 without consuming Oct 25
    const rMissedYesterdayBefore3am = maint.shouldRunScheduledUpdate(
      { lastUpdateDay: '2026-10-23', missedWindowDay: '2026-10-24' },
      before3am,
      {
        tvPower: 'active',
        foregroundAppId: 'local.owner.applibrary',
        idleDurationMs: 45 * 60 * 1000
      }
    );
    assert.strictEqual(rMissedYesterdayBefore3am.run, true);
    assert.strictEqual(rMissedYesterdayBefore3am.targetDay, '2026-10-24');

    // Persistent=true timer trigger firing at 10:00 AM after missed 3:00 AM must defer to 30-min idle
    const at10am = new Date(2026, 9, 25, 10, 0, 0);
    const consumed = maint.consumeUpdaterTrigger(
      { reqId: 'tv-timer-123', mode: 'update' },
      at10am,
      { lastUpdateDay: '2026-10-24' }
    );
    assert.strictEqual(consumed.immediate, false);
    assert.strictEqual(consumed.deferredToIdle, true);
    assert.strictEqual(consumed.state.missedWindowDay, '2026-10-25');

    const rShortIdle = maint.shouldRunScheduledUpdate(consumed.state, at10am, {
      tvPower: 'active',
      foregroundAppId: 'local.owner.applibrary',
      idleDurationMs: 10 * 60 * 1000
    });
    assert.strictEqual(rShortIdle.run, false);

    const r30MinIdle = maint.shouldRunScheduledUpdate(consumed.state, at10am, {
      tvPower: 'active',
      foregroundAppId: 'local.owner.applibrary',
      idleDurationMs: 30 * 60 * 1000
    });
    assert.strictEqual(r30MinIdle.run, true);
    assert.strictEqual(r30MinIdle.reason, 'idle-catchup');
    assert.strictEqual(r30MinIdle.reqId, 'tv-timer-123');
    groups++;
  }

  // 4. Empty installed-app inventory never broadens eligibility; Home Input resets idle timer
  {
    const emptyRes = maint.selectUpdatableApps(
      [],
      [{ id: 'netflix', updateYN: 'Y', version: '2.1.0' }],
      null
    );
    assert.deepStrictEqual(emptyRes.updatable, []);

    let clock = 1000;
    let irqText = ' 42: 100 gpio-keys\n';
    let fg = 'local.owner.applibrary';
    const tracker = maint.createActivityTracker({
      nowMs: () => clock,
      readInterrupts: () => irqText,
      queryForegroundSync: () => fg
    });
    tracker.sampleSync();
    clock += 20 * 60 * 1000;
    let snap = tracker.sampleSync();
    assert.strictEqual(snap.idleDurationMs, 20 * 60 * 1000);

    // Input on Home (local.owner.applibrary) must reset the idle timer
    clock += 1000;
    irqText = ' 42: 101 gpio-keys\n';
    snap = tracker.sampleSync();
    assert.strictEqual(snap.idleDurationMs, 0);
    groups++;
  }

  // 5. Foreground recheck before every install & foreground change during update prevents standby
  {
    const windowTransitions = [];
    const installCalls = [];
    const powerCalls = [];
    let fgApp = 'local.owner.applibrary';
    let clock = 10000;

    const res = await maint.runUpdateWorkflow({
      mode: 'update-standby',
      tvPower: 'active',
      nowMs: () => clock,
      sleep: async (ms) => {
        clock += ms;
      },
      allowlistApps: ['youtube.leanback.v4', 'netflix'],
      setLgEgressWindow: async (open) => {
        windowTransitions.push(open);
        return true;
      },
      callLunaSend: async (uri, payload) => {
        if (uri.includes('tvpower')) {
          powerCalls.push({ uri, payload });
          return { returnValue: true };
        }
        if (uri === 'luna://com.webos.applicationManager/getForegroundAppInfo') {
          return { returnValue: true, appId: fgApp };
        }
        if (uri === 'luna://com.webos.appInstallService/install') {
          installCalls.push(payload.id);
          // While youtube is installing, user switches foreground to netflix!
          if (payload.id === 'youtube.leanback.v4') {
            fgApp = 'netflix';
          }
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
              { id: 'youtube.leanback.v4', version: '3.0.0' },
              { id: 'netflix', version: '2.0.0' }
            ]
          };
        }
        if (uri === 'luna://com.webos.appUpdateService/statusAll') {
          return {
            returnValue: true,
            status: {
              apps: [
                { id: 'youtube.leanback.v4', updateYN: 'Y', version: '3.1.0' },
                { id: 'netflix', updateYN: 'Y', version: '2.1.0' }
              ]
            }
          };
        }
        if (uri === 'luna://com.webos.appInstallService/status') {
          return {
            returnValue: true,
            status: {
              apps: [{ id: 'youtube.leanback.v4', state: 'installed' }]
            }
          };
        }
        return { returnValue: true };
      }
    });

    assert.deepStrictEqual(windowTransitions, [true, false]);
    assert.deepStrictEqual(installCalls, ['youtube.leanback.v4']);
    assert.deepStrictEqual(res.updatesCompleted, ['youtube.leanback.v4']);
    assert.deepStrictEqual(res.updatesSkippedForeground, ['netflix']);
    assert.strictEqual(res.userActivityDetected, true);
    assert.strictEqual(res.returnedToStandby, false);
    assert.strictEqual(res.maintenanceComplete, false);
    assert.deepStrictEqual(res.pendingApps, ['netflix']);

    const nextState = maint.computeNextUpdaterState(
      { lastUpdateDay: '2026-10-24' },
      res,
      { nowDate: new Date(2026, 9, 25, 3, 10, 0), targetDay: '2026-10-25', reqId: 'req-1' }
    );
    assert.strictEqual(nextState.lastUpdateDay, '2026-10-24');
    assert.strictEqual(nextState.pendingRetryDay, '2026-10-25');
    assert.deepStrictEqual(nextState.pendingApps, ['netflix']);
    groups++;
  }

  // 6. Woken-from-standby 3 AM mode ('update-standby') with idle foreground completes all updates and returns to standby
  {
    const windowTransitions = [];
    const powerCalls = [];
    let clock = 5000;
    const res = await maint.runUpdateWorkflow({
      mode: 'update-standby',
      tvPower: 'active',
      nowMs: () => clock,
      sleep: async (ms) => {
        clock += ms;
      },
      setLgEgressWindow: async (open) => {
        windowTransitions.push(open);
        return true;
      },
      callLunaSend: async (uri, payload) => {
        if (uri.includes('tvpower')) {
          powerCalls.push({ uri, payload });
          return { returnValue: true };
        }
        if (uri === 'luna://com.webos.applicationManager/getForegroundAppInfo') {
          return { returnValue: true, appId: 'local.owner.applibrary' };
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
    assert.strictEqual(res.maintenanceComplete, true);
    assert.strictEqual(res.screenTurnedOff, true);
    assert.strictEqual(res.returnedToStandby, true);
    assert.strictEqual(powerCalls.length, 2);
    assert.strictEqual(powerCalls[0].uri, 'luna://com.webos.service.tvpower/power/turnOffScreen');
    assert.strictEqual(powerCalls[1].uri, 'luna://com.webos.service.tvpower/power/turnOff');

    const nextState = maint.computeNextUpdaterState(
      { lastUpdateDay: '2026-10-24', missedWindowDay: '2026-10-25' },
      res,
      { nowDate: new Date(2026, 9, 25, 3, 10, 0), targetDay: '2026-10-25', reqId: 'req-2' }
    );
    assert.strictEqual(nextState.lastUpdateDay, '2026-10-25');
    assert.strictEqual(nextState.pendingRetryDay, null);
    assert.deepStrictEqual(nextState.pendingApps, []);
    groups++;
  }

  // 7. Null/error status responses and stalled 45s downloads are NOT treated as completed and prevent standby
  {
    let clock = 1000;
    const cancelCalls = [];
    const powerCalls = [];
    let statusCallCount = 0;

    const res = await maint.runUpdateWorkflow({
      mode: 'update-standby',
      tvPower: 'active',
      nowMs: () => clock,
      sleep: async (ms) => {
        clock += ms;
      },
      installTimeoutMs: 10000,
      maxInstallTimeoutMs: 12000,
      pollIntervalMs: 3000,
      setLgEgressWindow: async () => true,
      callLunaSend: async (uri, payload) => {
        if (uri.includes('tvpower')) {
          powerCalls.push(uri);
          return { returnValue: true };
        }
        if (uri === 'luna://com.webos.appInstallService/cancel') {
          cancelCalls.push(payload.id);
          return { returnValue: true };
        }
        if (uri === 'luna://com.webos.applicationManager/getForegroundAppInfo') {
          return { returnValue: true, appId: 'local.owner.applibrary' };
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
          statusCallCount++;
          if (statusCallCount === 1) {
            return null; // Null/error status response must NOT be treated as completed!
          }
          // Ongoing stalled download at deadline
          return {
            returnValue: true,
            status: {
              apps: [{ id: 'netflix', state: 'downloading', progress: 15 }]
            }
          };
        }
        return { returnValue: true };
      }
    });

    assert.deepStrictEqual(res.updatesCompleted, []);
    assert.deepStrictEqual(res.updatesTimedOut, ['netflix']);
    assert.deepStrictEqual(cancelCalls, ['netflix']);
    assert.strictEqual(res.returnedToStandby, false);
    assert.strictEqual(res.maintenanceComplete, false);
    assert(!powerCalls.includes('luna://com.webos.service.tvpower/power/turnOff'));
    groups++;
  }

  // 8. Even if setLgEgressWindow(true) throws after partial change, finally calls setLgEgressWindow(false)
  {
    const windowTransitions = [];
    let caught = false;
    try {
      await maint.runUpdateWorkflow({
        mode: 'update',
        tvPower: 'active',
        setLgEgressWindow: async (open) => {
          windowTransitions.push(open);
          if (open === true) {
            throw new Error('simulated opener failure after partial mutation');
          }
          return true;
        },
        callLunaSend: async () => ({ returnValue: true, appId: 'local.owner.applibrary' }),
        callLunaSendLarge: async () => ({
          returnValue: true,
          apps: [{ id: 'netflix', version: '1.0.0' }]
        })
      });
    } catch (_) {
      caught = true;
    }
    assert.strictEqual(caught, true);
    assert.deepStrictEqual(windowTransitions, [true, false]);
    groups++;
  }

  // 9. Network guard & manifest retirement verify command results, effective file hashes, and retry on failure
  {
    const files = new Map([
      ['/etc/hosts', Buffer.from('127.0.0.1\tlocalhost\n', 'utf8')],
      ['/usr/sbin/iptables', Buffer.from('', 'utf8')]
    ]);
    const mounted = new Map();
    let failAllSubprocesses = false;

    const fakeFs = {
      existsSync: (p) => files.has(p),
      readFileSync: (p, enc) => {
        if (!files.has(p)) throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
        const b = files.get(p);
        return enc ? b.toString(enc) : b;
      },
      unlinkSync: (p) => files.delete(p)
    };

    const guard = maint.createLgNetworkGuard({
      fs: fakeFs,
      getMountedTargets: () => mounted,
      writeAtomic: (p, buf) => files.set(p, Buffer.from(buf)),
      spawnSync: (cmd, args) => {
        if (failAllSubprocesses) {
          return { status: 1, stdout: Buffer.alloc(0) };
        }
        if (cmd.endsWith('/getent')) {
          return {
            status: 0,
            stdout: Buffer.from('203.0.113.50 STREAM store\n2001:db8::50 STREAM store\n', 'utf8')
          };
        }
        if (cmd === '/bin/umount') {
          mounted.delete('/etc/hosts');
          return { status: 0 };
        }
        if (cmd === '/bin/mount' && args.includes('--bind')) {
          files.set('/etc/hosts', Buffer.from(files.get('/tmp/r2d2-hosts')));
          mounted.set('/etc/hosts', { mountId: '1', opts: 'rw' });
          return { status: 0 };
        }
        if (cmd === '/bin/mount' && args.some((a) => a.includes('remount'))) {
          mounted.set('/etc/hosts', { mountId: '1', opts: 'ro' });
          return { status: 0 };
        }
        return { status: 0, stdout: Buffer.alloc(0) };
      }
    });

    // Open App Store window succeeds
    assert.strictEqual(guard.apply(true), true);
    assert.strictEqual(guard.isWindowOpen(), true);
    assert.strictEqual(guard.isBlocked(), false);

    // Simulate restoration with previous open-window mount retained and all subprocesses failing:
    // MUST report false (not blocked) and MUST retry on the next call!
    failAllSubprocesses = true;
    assert.strictEqual(guard.apply(false), false);
    assert.strictEqual(guard.isBlocked(), false);

    // Next call when subprocesses recover must retry and succeed
    failAllSubprocesses = false;
    assert.strictEqual(guard.apply(false), true);
    assert.strictEqual(guard.isBlocked(), true);
    assert.strictEqual(guard.isWindowOpen(), false);

    // Manifest retirement enforcer checks command status and retries failed removals
    let lunaShouldFail = true;
    const enforcer = maint.createPrivacyRetirementEnforcer({
      fs: {
        mkdirSync() {},
        chmodSync() {},
        existsSync: (p) => p === '/usr/share/luna-service2/manifests.d/ad.manifest.json'
      },
      getAllMountPoints: () => new Set(),
      retirePrivacyProcesses: () => 0,
      ls2Manifests: ['/usr/share/luna-service2/manifests.d/ad.manifest.json'],
      binTargets: [],
      dirTargets: [],
      spawnSync: () => ({
        status: lunaShouldFail ? 1 : 0,
        stdout: Buffer.from(JSON.stringify({ returnValue: !lunaShouldFail }), 'utf8')
      })
    });

    const rFail = enforcer.enforce();
    assert.strictEqual(rFail.ok, false);
    assert.strictEqual(rFail.manifestsRemovedCount, 0);

    lunaShouldFail = false;
    const rOk = enforcer.enforce();
    assert.strictEqual(rOk.ok, true);
    assert.strictEqual(rOk.manifestsRemovedCount, 1);
    groups++;
  }

  // 10. ResidentMaintenanceController: activity sampling while maintenanceRunning, deferred triggers, and shutdown block restoration
  {
    let clock = new Date(2026, 9, 25, 10, 0, 0).getTime();
    let tvPower = 'active';
    let fgApp = 'local.owner.applibrary';
    let irqText = ' 42: 10 gpio-keys\n';
    let persistedState = { schema: 1, lastUpdateDay: '2026-10-24' };
    let triggerFileContent = JSON.stringify({ reqId: 'timer-post-3am', mode: 'update' });
    const windowHistory = [];
    const scheduledFns = [];

    const tracker = maint.createActivityTracker({
      nowMs: () => clock,
      readInterrupts: () => irqText,
      queryForegroundSync: () => fgApp
    });

    const ctrl = maint.createResidentMaintenanceController({
      fs: {
        existsSync: (p) => p === '/tmp/r2d2-trigger-updater.json' && triggerFileContent !== null,
        readFileSync: () => triggerFileContent,
        unlinkSync: () => {
          triggerFileContent = null;
        }
      },
      nowMs: () => clock,
      sleep: async (ms) => {
        clock += ms;
      },
      getTvPower: () => tvPower,
      activityTracker: tracker,
      readUpdaterState: () => persistedState,
      writeUpdaterState: (st) => {
        persistedState = st;
      },
      applyLgNetworkBlock: (open) => {
        windowHistory.push(Boolean(open));
        return true;
      },
      scheduleImmediate: (fn) => scheduledFns.push(fn),
      callLunaSend: async (uri) => {
        if (uri === 'luna://com.webos.applicationManager/getForegroundAppInfo') {
          return { returnValue: true, appId: fgApp };
        }
        return { returnValue: true };
      },
      callLunaSendLarge: async (uri) => {
        if (uri === 'luna://com.webos.applicationManager/listApps') {
          return { returnValue: true, apps: [{ id: 'netflix', version: '2.0.0' }] };
        }
        if (uri === 'luna://com.webos.appUpdateService/statusAll') {
          return {
            returnValue: true,
            status: { apps: [{ id: 'netflix', updateYN: 'Y', version: '2.1.0' }] }
          };
        }
        if (uri === 'luna://com.webos.appInstallService/status') {
          return {
            returnValue: true,
            status: { apps: [{ id: 'netflix', state: 'installed' }] }
          };
        }
        return { returnValue: true };
      }
    });

    // At 10:00 AM with 0 ms idle, consuming /tmp/r2d2-trigger-updater.json must defer to idle, NOT run immediately!
    const t1 = ctrl.maybeTriggerScheduledOrIdleUpdate('vk');
    assert.strictEqual(t1.triggered, false);
    assert.strictEqual(triggerFileContent, null);
    assert.strictEqual(persistedState.missedWindowDay, '2026-10-25');
    assert.strictEqual(persistedState.pendingReqId, 'timer-post-3am');
    assert.strictEqual(scheduledFns.length, 0);

    // After 30 minutes of verified idle on Home, maybeTriggerScheduledOrIdleUpdate triggers catch-up
    clock += 30 * 60 * 1000;
    const t2 = ctrl.maybeTriggerScheduledOrIdleUpdate('vk');
    assert.strictEqual(t2.triggered, true);
    assert.strictEqual(t2.reason, 'idle-catchup');
    assert.strictEqual(t2.reqId, 'timer-post-3am');
    assert.strictEqual(scheduledFns.length, 1);

    // Run the scheduled maintenance job
    await scheduledFns.shift()();
    assert.strictEqual(persistedState.lastUpdateDay, '2026-10-25');
    assert.strictEqual(persistedState.pendingRetryDay, null);

    // Shutdown always closes the LG egress window
    assert.strictEqual(ctrl.shutdown(), true);
    assert.strictEqual(windowHistory[windowHistory.length - 1], false);
    groups++;
  }

  // 11. Adversarial edge cases: failed foreground queries, active viewer screen-off gate, unobservable install cancel+reconcile, pre-3am retry, 03:20 update-idle, 10:00 delayed header, firewall loss repair, DNS harvest failure + IPv6 rules, empty manifest reply, and lease expiry
  {
    // 11a. Failed foreground reads must NOT install or return to standby
    {
      let clock = 1000;
      const calls = [];
      const out = await maint.runUpdateWorkflow({
        mode: 'update-standby',
        tvPower: 'active',
        nowMs: () => clock,
        sleep: async (ms) => {
          clock += ms;
        },
        setLgEgressWindow: async () => true,
        callLunaSend: async (uri, payload) => {
          calls.push({ uri, payload });
          if (uri.endsWith('/getForegroundAppInfo')) return null;
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [{ id: 'netflix', version: '1.0' }] };
          }
          if (uri.endsWith('/statusAll')) {
            return {
              returnValue: true,
              status: { apps: [{ id: 'netflix', updateYN: 'Y', version: '2.0' }] }
            };
          }
          if (uri.endsWith('/status')) {
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });
      assert.deepStrictEqual(out.updatesAttempted, []);
      assert.strictEqual(out.screenTurnedOff, false);
      assert.strictEqual(out.returnedToStandby, false);
      assert.strictEqual(out.maintenanceComplete, false);
    }

    // 11b. Known active viewer (e.g. Netflix foreground) must NOT turn off screen or return to standby
    {
      let clock = 1000;
      const out = await maint.runUpdateWorkflow({
        mode: 'update-standby',
        tvPower: 'active',
        nowMs: () => clock,
        sleep: async (ms) => {
          clock += ms;
        },
        setLgEgressWindow: async () => true,
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'netflix' };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [{ id: 'netflix', version: '1.0' }] };
          }
          if (uri.endsWith('/statusAll')) {
            return {
              returnValue: true,
              status: { apps: [{ id: 'netflix', updateYN: 'Y', version: '2.0' }] }
            };
          }
          return { returnValue: true, status: { apps: [] } };
        }
      });
      assert.strictEqual(out.userActivityDetected, true);
      assert.strictEqual(out.screenTurnedOff, false);
      assert.strictEqual(out.returnedToStandby, false);
    }

    // 11c. Unobservable install (status returns null) calls cancel and records updatesInProgress
    {
      let clock = 1000;
      const calls = [];
      const out = await maint.runUpdateWorkflow({
        mode: 'update-standby',
        tvPower: 'active',
        nowMs: () => clock,
        sleep: async (ms) => {
          clock += ms;
        },
        installTimeoutMs: 6000,
        maxInstallTimeoutMs: 6000,
        pollIntervalMs: 2000,
        setLgEgressWindow: async () => true,
        callLunaSend: async (uri, payload) => {
          calls.push({ uri, payload });
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/cancel')) return { returnValue: false };
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [{ id: 'netflix', version: '1.0' }] };
          }
          if (uri.endsWith('/statusAll')) {
            return {
              returnValue: true,
              status: { apps: [{ id: 'netflix', updateYN: 'Y', version: '2.0' }] }
            };
          }
          if (uri.endsWith('/status')) return null;
          return { returnValue: true };
        }
      });
      assert.strictEqual(out.maintenanceComplete, false);
      assert.deepStrictEqual(out.updatesTimedOut, ['netflix']);
      assert.strictEqual(calls.filter((x) => x.uri.endsWith('/cancel')).length, 1);
      assert.deepStrictEqual(out.updatesInProgress, ['netflix']);
      assert.strictEqual(out.returnedToStandby, false);
    }

    // 11d. Pre-3am retry targets the prior missed day and does NOT suppress today's 3:00 AM run
    {
      const at = new Date(2026, 9, 25, 0, 40);
      const state = {
        lastUpdateDay: '2026-10-23',
        missedWindowDay: '2026-10-24',
        pendingRetryDay: '2026-10-24',
        pendingReqId: 'old-retry',
        pendingApps: ['netflix']
      };
      const decision = maint.shouldRunScheduledUpdate(state, at, {
        tvPower: 'active',
        foregroundAppId: 'local.owner.applibrary',
        idleDurationMs: 1800000
      });
      assert.strictEqual(decision.run, true);
      assert.strictEqual(decision.targetDay, '2026-10-24');
      const next = maint.computeNextUpdaterState(
        state,
        { maintenanceComplete: true },
        { nowDate: at, targetDay: decision.targetDay }
      );
      const three = maint.shouldRunScheduledUpdate(next, new Date(2026, 9, 25, 3, 0), {
        tvPower: 'active'
      });
      assert.strictEqual(three.run, true);
      assert.strictEqual(three.reason, 'nightly-3am');
    }

    // 11e. update-idle at 03:20 with 0 idle time defers to 30-min idle, and delayed update header at 10:00 also defers
    {
      let state = { lastUpdateDay: '2026-10-24' };
      const scheduled = [];
      const clock320 = new Date(2026, 9, 25, 3, 20).getTime();
      const ctrl320 = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => clock320,
        readUpdaterState: () => state,
        writeUpdaterState: (s) => {
          state = s;
        },
        scheduleImmediate: (fn) => scheduled.push(fn)
      });
      const res320 = ctrl320.handleMaintenanceHeader('key', 'failed-wake:update-idle');
      assert.strictEqual(res320.triggered, false);
      assert.strictEqual(res320.deferredToIdle, true);
      assert.strictEqual(scheduled.length, 0);

      let state10 = { lastUpdateDay: '2026-10-24' };
      const scheduled10 = [];
      const ctrl10 = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => new Date(2026, 9, 25, 10, 0).getTime(),
        readUpdaterState: () => state10,
        writeUpdaterState: (s) => {
          state10 = s;
        },
        scheduleImmediate: (fn) => scheduled10.push(fn)
      });
      const res10 = ctrl10.handleMaintenanceHeader('key', 'delayed-request:update');
      assert.strictEqual(res10.triggered, false);
      assert.strictEqual(res10.deferredToIdle, true);
      assert.strictEqual(scheduled10.length, 0);
    }

    // 11f. Firewall rule loss repair, DNS harvest failure refusal, IPv6 coverage, lease expiry, and non-empty manifest reply
    {
      function makeGuard({ dnsFail = false } = {}) {
        const files = new Map([
          ['/etc/hosts', Buffer.from('127.0.0.1 localhost\n')],
          ['/usr/sbin/iptables', Buffer.alloc(0)]
        ]);
        const mounted = new Map();
        const commands = [];
        let rules = [];
        let clock = 1000;
        const io = {
          existsSync: (p) => files.has(p),
          readFileSync: (p, enc) => {
            if (!files.has(p)) throw new Error('missing ' + p);
            const b = files.get(p);
            return enc ? b.toString(enc) : b;
          },
          unlinkSync: (p) => files.delete(p)
        };
        const guard = maint.createLgNetworkGuard({
          fs: io,
          nowMs: () => clock,
          getMountedTargets: () => mounted,
          writeAtomic: (p, b) => files.set(p, Buffer.from(b)),
          spawnSync: (cmd, args) => {
            commands.push([cmd, ...args]);
            if (cmd === '/bin/umount') {
              mounted.delete('/etc/hosts');
              return { status: 0 };
            }
            if (cmd === '/bin/mount' && args.includes('--bind')) {
              files.set('/etc/hosts', Buffer.from(files.get('/tmp/r2d2-hosts')));
              mounted.set('/etc/hosts', {});
              return { status: 0 };
            }
            if (cmd.endsWith('/getent')) {
              return dnsFail
                ? { status: 127 }
                : { status: 0, stdout: Buffer.from('203.0.113.50 STREAM store\n') };
            }
            if (cmd.endsWith('/iptables')) {
              if (args[0] === '-F') rules = [];
              if (args[0] === '-A') rules.push(args.join(' '));
            }
            return { status: 0, stdout: Buffer.alloc(0) };
          }
        });
        return {
          guard,
          commands,
          files,
          advanceClock: (ms) => {
            clock += ms;
          },
          clearRules: () => {
            rules = [];
          },
          getRules: () => rules
        };
      }

      const fx1 = makeGuard();
      assert.strictEqual(fx1.guard.apply(false), true);
      fx1.clearRules();
      const before = fx1.commands.length;
      assert.strictEqual(fx1.guard.apply(false), true);
      assert(fx1.commands.length > before);
      assert(fx1.getRules().length > 0);
      assert(fx1.commands.some((x) => x[0].endsWith('ip6tables')));

      // Open window writes lease; advancing clock past lease expiry and calling enforceLease closes window
      assert.strictEqual(fx1.guard.apply(true), true);
      assert(fx1.files.has('/tmp/r2d2-appstore-window.json'));
      fx1.advanceClock(11 * 60 * 1000);
      const leaseRes = fx1.guard.enforceLease();
      assert.strictEqual(leaseRes.expired, true);
      assert.strictEqual(leaseRes.blocked, true);
      assert.strictEqual(fx1.guard.isBlocked(), true);
      assert(!fx1.files.has('/tmp/r2d2-appstore-window.json'));

      // DNS failure at open refuses to open window and refuses to report blocked without harvested App Store IPs
      const fxDns = makeGuard({ dnsFail: true });
      assert.strictEqual(fxDns.guard.apply(true), false);
      assert.strictEqual(fxDns.guard.isWindowOpen(), false);
      assert.strictEqual(fxDns.guard.apply(false), false);
      assert.strictEqual(fxDns.guard.isBlocked(), false);

      // Empty manifest reply is rejected and retried on next enforcement
      let manifestCalls = 0;
      const enforcer = maint.createPrivacyRetirementEnforcer({
        fs: { existsSync: () => true, mkdirSync() {}, chmodSync() {} },
        getAllMountPoints: () => new Set(),
        ls2Manifests: ['/manifest'],
        spawnSync: () => {
          manifestCalls++;
          return { status: 0, stdout: Buffer.alloc(0) };
        }
      });
      const first = enforcer.enforce();
      enforcer.enforce();
      assert.strictEqual(first.ok, false);
      assert.strictEqual(first.manifestsRemovedCount, 0);
      assert.strictEqual(manifestCalls, 2);
    }

    groups++;
  }

  // 12. Auto Voice Leveling Enforcer: boot enforcement, steady-state cache, app switch + post-splash recheck, standby/wake, enum & key fallbacks, readback verification, disk hydration, and maintenance report integration
  {
    // 12a. Boot enforcement, steady-state cache, app switch + 7s post-splash recheck, and standby/wake re-enforcement
    {
      let clock = 10000;
      const files = new Map();
      const lunaCalls = [];
      let tvSettings = {
        autoVolume: 'off',
        soundMode: 'standard',
        soundOutput: 'tv_speaker'
      };

      const enforcer = maint.createAutoVoiceLevelingEnforcer({
        fs: {
          existsSync: (p) => files.has(p),
          readFileSync: (p, enc) => {
            const b = files.get(p);
            return enc ? b.toString(enc) : b;
          }
        },
        writeAtomic: (p, b) => files.set(p, Buffer.from(b)),
        nowMs: () => clock,
        recheckIntervalMs: 60000,
        postSwitchRecheckDelayMs: 7000,
        spawnSync: (_cmd, args) => {
          const uri = args[4];
          const payload = JSON.parse(args[5]);
          lunaCalls.push({ uri, payload });
          if (uri.endsWith('/getSystemSettings')) {
            return {
              status: 0,
              stdout: Buffer.from(
                JSON.stringify({ returnValue: true, settings: { ...tvSettings } }),
                'utf8'
              )
            };
          }
          if (uri.endsWith('/setSystemSettings')) {
            tvSettings = { ...tvSettings, ...(payload.settings || {}) };
            return {
              status: 0,
              stdout: Buffer.from(JSON.stringify({ returnValue: true }), 'utf8')
            };
          }
          return { status: 1, stdout: Buffer.alloc(0) };
        }
      });

      // Boot enforcement on Home
      const rBoot = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'local.owner.applibrary'
      });
      assert.strictEqual(rBoot.ok, true);
      assert.strictEqual(rBoot.status, 'on');
      assert.strictEqual(rBoot.autoVolume, 'on');
      assert.strictEqual(rBoot.soundMode, 'clearVoice');
      assert.strictEqual(rBoot.enforceCount, 1);
      assert.strictEqual(rBoot.cached, false);
      assert(files.has('/var/lib/local.owner.r2d2mic/voice-leveling-state.json'));

      // Steady-state poll 8s later on same app: 0 Luna calls, cached=true
      const callsAfterBoot = lunaCalls.length;
      clock += 8000;
      const rCached = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'local.owner.applibrary'
      });
      assert.strictEqual(rCached.ok, true);
      assert.strictEqual(rCached.cached, true);
      assert.strictEqual(lunaCalls.length, callsAfterBoot);

      // User switches to Netflix; Netflix resets soundMode to 'cinema' and autoVolume to 'off'
      clock += 2000;
      tvSettings.autoVolume = 'off';
      tvSettings.soundMode = 'cinema';
      const rSwitch = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rSwitch.ok, true);
      assert.strictEqual(rSwitch.status, 'on');
      assert.strictEqual(rSwitch.autoVolume, 'on');
      assert.strictEqual(rSwitch.soundMode, 'clearVoice');
      assert.strictEqual(rSwitch.enforceCount, 2);
      assert.strictEqual(rSwitch.cached, false);

      // 3s after switch (before 7s post-switch delay): uses cache
      const callsAfterSwitch = lunaCalls.length;
      clock += 3000;
      const rPreSplash = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rPreSplash.cached, true);
      assert.strictEqual(lunaCalls.length, callsAfterSwitch);

      // 8s after switch (>= 7s post-switch delay): Netflix media pipeline overrides soundMode to 'cinema' after splash
      clock += 5000;
      tvSettings.soundMode = 'cinema';
      const rPostSplash = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rPostSplash.ok, true);
      assert.strictEqual(rPostSplash.cached, false);
      assert.strictEqual(rPostSplash.soundMode, 'clearVoice');
      assert.strictEqual(rPostSplash.enforceCount, 3);

      // Subsequent poll 8s later: post-switch recheck already completed, so cached=true and 0 Luna calls
      const callsAfterPostSplash = lunaCalls.length;
      clock += 8000;
      const rSteadyNetflix = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rSteadyNetflix.cached, true);
      assert.strictEqual(lunaCalls.length, callsAfterPostSplash);

      // Standby transition makes 0 Luna calls and reports standby; waking back up re-verifies immediately
      const rStandby = enforcer.enforceSync({
        tvPower: 'standby',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rStandby.status, 'standby');
      assert.strictEqual(lunaCalls.length, callsAfterPostSplash);

      tvSettings.autoVolume = 'off';
      const rWake = enforcer.enforceSync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rWake.ok, true);
      assert.strictEqual(rWake.status, 'on');
      assert.strictEqual(rWake.autoVolume, 'on');
      assert.strictEqual(rWake.cached, false);
      assert.strictEqual(rWake.enforceCount, 4);

      // New enforcer instance hydrates resolvedSoundMode='clearVoice' and enforceCount=4 from disk
      const enforcerRebooted = maint.createAutoVoiceLevelingEnforcer({
        fs: {
          existsSync: (p) => files.has(p),
          readFileSync: (p, enc) => {
            const b = files.get(p);
            return enc ? b.toString(enc) : b;
          }
        },
        writeAtomic: (p, b) => files.set(p, Buffer.from(b)),
        nowMs: () => clock,
        spawnSync: (_cmd, args) => {
          const uri = args[4];
          if (uri.endsWith('/getSystemSettings')) {
            return {
              status: 0,
              stdout: Buffer.from(
                JSON.stringify({ returnValue: true, settings: { ...tvSettings } }),
                'utf8'
              )
            };
          }
          return { status: 0, stdout: Buffer.from(JSON.stringify({ returnValue: true }), 'utf8') };
        }
      });
      const rHydrated = enforcerRebooted.enforceSync({
        tvPower: 'active',
        fgAppId: 'local.owner.applibrary'
      });
      assert.strictEqual(rHydrated.ok, true);
      assert.strictEqual(rHydrated.status, 'on');
      assert.strictEqual(rHydrated.resolvedSoundMode, 'clearVoice');
      assert.strictEqual(rHydrated.enforceCount, 4);
    }

    // 12b. Async enforcement with individual-key query fallback, enum fallback (clearVoicePro / aiSoundPro), boolean autoVolume, and partial/error states
    {
      let clock = 5000;
      let tvSettings = {
        autoVolume: false,
        soundMode: 'standard'
      };
      let failAllReads = false;
      let ignoreSoundModeWrites = false;
      const setAttempts = [];

      const asyncEnforcer = maint.createAutoVoiceLevelingEnforcer({
        nowMs: () => clock,
        callLunaSend: async (uri, payload) => {
          if (failAllReads) return null;
          if (uri.endsWith('/getSystemSettings')) {
            // Simulate firmware that rejects batch query containing 'soundOutput'
            if (Array.isArray(payload.keys) && payload.keys.includes('soundOutput')) {
              return { returnValue: false, errorText: 'There is no matched result' };
            }
            const k = payload.keys && payload.keys[0];
            if (k && k in tvSettings) {
              return { returnValue: true, settings: { [k]: tvSettings[k] } };
            }
            return { returnValue: false };
          }
          if (uri.endsWith('/setSystemSettings')) {
            setAttempts.push(payload.settings);
            const s = payload.settings || {};
            if ('autoVolume' in s) {
              // Only accept boolean true on this simulated firmware
              if (s.autoVolume === true) {
                tvSettings.autoVolume = true;
                return { returnValue: true };
              }
              return { returnValue: false };
            }
            if ('soundMode' in s) {
              if (ignoreSoundModeWrites) {
                // Simulate firmware returning returnValue:true while ignoring the write
                return { returnValue: true };
              }
              // Only accept 'clearVoicePro' on this simulated firmware
              if (s.soundMode === 'clearVoicePro') {
                tvSettings.soundMode = 'clearVoicePro';
                return { returnValue: true };
              }
              return { returnValue: true }; // Returned true for clearVoice, but readback stays 'standard'!
            }
          }
          return { returnValue: false };
        }
      });

      const rAsync = await asyncEnforcer.enforceAsync({
        tvPower: 'active',
        fgAppId: 'youtube.leanback.v4'
      });
      assert.strictEqual(rAsync.ok, true);
      assert.strictEqual(rAsync.status, 'on');
      assert.strictEqual(rAsync.autoVolume, true);
      assert.strictEqual(rAsync.soundMode, 'clearVoicePro');
      assert.strictEqual(rAsync.resolvedSoundMode, 'clearVoicePro');

      // When soundMode writes are ignored on readback, status is 'partial' (ok=false) and NOT cached as 'on'
      tvSettings.soundMode = 'sports';
      ignoreSoundModeWrites = true;
      const rPartial = await asyncEnforcer.enforceAsync({
        tvPower: 'active',
        fgAppId: 'cdp-30'
      });
      assert.strictEqual(rPartial.ok, false);
      assert.strictEqual(rPartial.status, 'partial');
      assert.strictEqual(rPartial.autoVolumeActive, true);
      assert.strictEqual(rPartial.soundModeActive, false);

      // When Luna read fails completely, status is 'err' (ok=false)
      failAllReads = true;
      const rErr = await asyncEnforcer.enforceAsync({
        tvPower: 'active',
        fgAppId: 'cdp-30'
      });
      assert.strictEqual(rErr.ok, false);
      assert.strictEqual(rErr.status, 'err');

      // Hard anti-telemetry gate: refuses to call settingsservice if LG network block is not verified active
      let gatedCalls = 0;
      let lgBlocked = false;
      const gatedEnforcer = maint.createAutoVoiceLevelingEnforcer({
        isLgServerBlocked: () => lgBlocked,
        callLunaSend: async () => {
          gatedCalls++;
          return {
            returnValue: true,
            settings: { autoVolume: 'on', soundMode: 'clearVoice', soundOutput: 'tv_speaker' }
          };
        }
      });
      const rDeferred = await gatedEnforcer.enforceAsync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rDeferred.ok, false);
      assert.strictEqual(rDeferred.status, 'deferred-until-lg-blocked');
      assert.strictEqual(gatedCalls, 0);

      lgBlocked = true;
      const rAllowed = await gatedEnforcer.enforceAsync({
        tvPower: 'active',
        fgAppId: 'netflix'
      });
      assert.strictEqual(rAllowed.ok, true);
      assert.strictEqual(rAllowed.status, 'on');
      assert.strictEqual(gatedCalls, 1);
    }

    // 12c. ResidentMaintenanceController includes voiceLeveling in maintenance reports
    {
      let postedReport = null;
      const ctrl = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        getTvPower: () => 'active',
        ensureAutoVoiceLeveling: async (opts) => ({
          enabled: true,
          ok: true,
          status: 'on',
          autoVolume: 'on',
          soundMode: 'clearVoice',
          foregroundAppId: opts.fgAppId,
          forced: opts.force
        }),
        postMaintenanceReport: async (_vk, rep) => {
          postedReport = rep;
          return true;
        },
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [{ id: 'local.owner.applibrary', version: '1.0.0' }] };
          }
          if (uri.endsWith('/statusAll') || uri.endsWith('/status')) {
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });

      const rep = await ctrl.executeTvMaintenance('vk', 'req-vl', 'check');
      assert(rep && rep.voiceLeveling);
      assert.strictEqual(rep.voiceLeveling.status, 'on');
      assert.strictEqual(rep.voiceLeveling.soundMode, 'clearVoice');
      assert.strictEqual(rep.voiceLeveling.foregroundAppId, 'local.owner.applibrary');
      assert.strictEqual(rep.voiceLeveling.forced, true);
      assert.strictEqual(postedReport.voiceLeveling.status, 'on');
    }

    groups++;
  }

  // 13. Seasonal Sports App Suggestions & Season-End Cleanup: evaluateSeasonalSports, multi-sport carrier safety, verified background removal of seasonalInstalls, foreground/activity deferral, and controller integration
  {
    const utc = (y, m) => new Date(Date.UTC(y, m - 1, 15));
    const amazonApp = {
      id: 'amazon',
      title: 'Prime Video',
      type: 'web',
      visible: true,
      removable: true,
      systemApp: false
    };
    const appleApp = {
      id: 'com.apple.appletv',
      title: 'Apple TV',
      type: 'web',
      visible: true,
      removable: true,
      systemApp: false
    };

    // 13a. In-season missing carrier produces suggestion; off-season seasonalInstall produces exit + autoRemoveIds
    const oct2026 = utc(2026, 10);
    const evInSeason = maint.evaluateSeasonalSports([], { followed: ['nfl'] }, oct2026);
    assert.strictEqual(evInSeason.suggestions.length, 1);
    assert.strictEqual(evInSeason.suggestions[0].appId, 'amazon');
    assert.deepStrictEqual(evInSeason.suggestions[0].sports, ['nfl']);
    assert.deepStrictEqual(evInSeason.autoRemoveIds, []);

    const mar2027 = utc(2027, 3);
    // Seasonal install WITHOUT affirmative autoRemove consent produces exit prompt for Home, but NOT unattended autoRemoveIds
    const evOffSeasonNoConsent = maint.evaluateSeasonalSports(
      [amazonApp],
      { followed: ['nfl'], seasonalInstalls: { amazon: true } },
      mar2027
    );
    assert.strictEqual(evOffSeasonNoConsent.exits.length, 1);
    assert.strictEqual(evOffSeasonNoConsent.exits[0].seasonalInstall, true);
    assert.strictEqual(evOffSeasonNoConsent.exits[0].autoRemove, false);
    assert.deepStrictEqual(evOffSeasonNoConsent.autoRemoveIds, []);

    const evOffSeasonSeasonal = maint.evaluateSeasonalSports(
      [amazonApp],
      { followed: ['nfl'], seasonalInstalls: { amazon: true }, autoRemove: { amazon: true } },
      mar2027
    );
    assert.strictEqual(evOffSeasonSeasonal.exits.length, 1);
    assert.strictEqual(evOffSeasonSeasonal.exits[0].appId, 'amazon');
    assert.strictEqual(evOffSeasonSeasonal.exits[0].seasonalInstall, true);
    assert.strictEqual(evOffSeasonSeasonal.exits[0].autoRemove, true);
    assert.deepStrictEqual(evOffSeasonSeasonal.autoRemoveIds, ['amazon']);

    // Pre-existing install (not in seasonalInstalls) produces exit prompt but is NOT in autoRemoveIds
    const evOffSeasonManual = maint.evaluateSeasonalSports(
      [amazonApp],
      { followed: ['nfl'], seasonalInstalls: {} },
      mar2027
    );
    assert.strictEqual(evOffSeasonManual.exits.length, 1);
    assert.strictEqual(evOffSeasonManual.exits[0].seasonalInstall, false);
    assert.deepStrictEqual(evOffSeasonManual.autoRemoveIds, []);

    // Shared carrier (Apple TV carries MLB Mar-Nov and MLS Feb-Dec): no exit/auto-remove in December while MLS is active
    const evDecShared = maint.evaluateSeasonalSports(
      [appleApp],
      {
        followed: ['mlb', 'mls'],
        seasonalInstalls: { 'com.apple.appletv': true },
        autoRemove: { 'com.apple.appletv': true }
      },
      utc(2026, 12)
    );
    assert.strictEqual(evDecShared.exits.length, 0);
    assert.deepStrictEqual(evDecShared.autoRemoveIds, []);

    const evJanShared = maint.evaluateSeasonalSports(
      [appleApp],
      {
        followed: ['mlb', 'mls'],
        seasonalInstalls: { 'com.apple.appletv': true },
        autoRemove: { 'com.apple.appletv': true }
      },
      utc(2027, 1)
    );
    assert.strictEqual(evJanShared.exits.length, 1);
    assert.strictEqual(evJanShared.exits[0].endMonthName, 'December');
    assert.deepStrictEqual(evJanShared.autoRemoveIds, ['com.apple.appletv']);

    const evDecF1Shared = maint.evaluateSeasonalSports(
      [appleApp],
      {
        followed: ['mlb', 'f1'],
        seasonalInstalls: { 'com.apple.appletv': true },
        autoRemove: { 'com.apple.appletv': true }
      },
      utc(2026, 12)
    );
    assert.strictEqual(evDecF1Shared.exits.length, 0);
    assert.deepStrictEqual(evDecF1Shared.autoRemoveIds, []);

    const evMarPrimeShared = maint.evaluateSeasonalSports(
      [amazonApp],
      {
        followed: ['nfl', 'nba'],
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true }
      },
      mar2027
    );
    assert.strictEqual(evMarPrimeShared.exits.length, 0);
    assert.deepStrictEqual(evMarPrimeShared.autoRemoveIds, []);

    const evYearRound = maint.evaluateSeasonalSports(
      [],
      { followed: ['cricket', 'soccer'] },
      oct2026
    );
    assert.strictEqual(evYearRound.suggestions.length, 1);
    assert.strictEqual(evYearRound.suggestions[0].yearRound, true);
    assert.strictEqual(evYearRound.suggestions[0].endMonthName, null);

    // 13b. runSeasonalAppCleanup removes off-season seasonal install when idle on Home and updates state
    {
      let seasonalState = {
        followed: ['nfl'],
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true }
      };
      let installed = [amazonApp];
      let removeCalled = false;
      let listPollsAfterRemove = 0;

      const res = await maint.runSeasonalAppCleanup({
        tvPower: 'active',
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        readSeasonalState: () => seasonalState,
        writeSeasonalState: (next) => {
          seasonalState = next;
        },
        callLunaSend: async (uri, payload) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri === 'luna://com.webos.appInstallService/remove' && payload.id === 'amazon') {
            removeCalled = true;
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            if (removeCalled && ++listPollsAfterRemove >= 2) {
              installed = [];
            }
            return { returnValue: true, apps: installed };
          }
          return { returnValue: true };
        }
      });

      assert.strictEqual(res.evaluated, true);
      assert.deepStrictEqual(res.autoRemoveCandidates, ['amazon']);
      assert.deepStrictEqual(res.removalsAttempted, ['amazon']);
      assert.deepStrictEqual(res.removalsCompleted, ['amazon']);
      assert.deepStrictEqual(res.removalsFailed, []);
      assert.strictEqual(seasonalState.seasonalInstalls.amazon, undefined);
      assert.strictEqual(seasonalState.autoRemove.amazon, undefined);
    }

    // 13c. runSeasonalAppCleanup skips removal when viewer is watching an active app
    {
      const resFg = await maint.runSeasonalAppCleanup({
        tvPower: 'active',
        nowMs: () => mar2027.getTime(),
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'netflix' };
          }
          throw new Error('Must not call remove while viewer is active');
        },
        callLunaSendLarge: async () => ({ returnValue: true, apps: [amazonApp] })
      });
      assert.deepStrictEqual(resFg.removalsAttempted, []);
      assert.deepStrictEqual(resFg.removalsSkippedForeground, ['amazon']);
    }

    // 13d. runSeasonalAppCleanup defers removal when getForegroundAppInfo fails, even if foregroundAppId was passed
    {
      const resFgFail = await maint.runSeasonalAppCleanup({
        tvPower: 'active',
        nowMs: () => mar2027.getTime(),
        foregroundAppId: 'local.owner.applibrary',
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: false };
          }
          throw new Error('Must not call remove when foreground query fails');
        },
        callLunaSendLarge: async () => ({ returnValue: true, apps: [amazonApp] })
      });
      assert.deepStrictEqual(resFgFail.removalsAttempted, []);
      assert.deepStrictEqual(resFgFail.removalsSkippedForeground, ['amazon']);
    }

    // 13e. runSeasonalAppCleanup rechecks activity and foreground after the pre-removal listApps query before calling remove
    {
      let listCalls = 0;
      let activityOccurred = false;
      const resPostVerify = await maint.runSeasonalAppCleanup({
        tvPower: 'active',
        nowMs: () => mar2027.getTime(),
        hasUserActivitySince: () => activityOccurred,
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          throw new Error('Must not call remove after user activity during pre-removal listApps');
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            listCalls++;
            if (listCalls === 2) {
              // Simulate remote activity arriving during the slow pre-removal listApps call
              activityOccurred = true;
            }
            return { returnValue: true, apps: [amazonApp] };
          }
          return { returnValue: true };
        }
      });
      assert.strictEqual(listCalls, 2);
      assert.deepStrictEqual(resPostVerify.removalsAttempted, []);
      assert.deepStrictEqual(resPostVerify.removalsSkippedForeground, ['amazon']);
    }

    // 13f. createSeasonalSportsStateStore and createResidentMaintenanceController bridge Home's LevelDB baaz.sports.v1 state by default
    {
      const levelDbDir = '/var/luna/preferences/webos_webappmgr/Local Storage/leveldb';
      const stateFile = '/var/lib/local.owner.r2d2mic/seasonal-sports-state.json';
      const files = new Map();
      // Simulate Chromium LevelDB .log file with an older UTF-8 record followed by a newer UTF-16LE record
      const oldRecord = Buffer.from(
        '_http://home.test\x00\x01baaz.sports.v1\x00\x01' +
          JSON.stringify({
            followed: ['nfl'],
            seasonalInstalls: { amazon: true },
            autoRemove: { amazon: true },
            kept: { 'nfl:off:2027': true }
          }),
        'utf8'
      );
      const newRecord = Buffer.concat([
        Buffer.from('_http://home.test\x00', 'utf8'),
        Buffer.from('baaz.sports.v1', 'utf16le'),
        Buffer.from('\x00\x00', 'utf8'),
        Buffer.from(
          JSON.stringify({
            followed: ['nfl'],
            seasonalInstalls: { amazon: true },
            autoRemove: { amazon: true },
            kept: {}
          }),
          'utf16le'
        )
      ]);
      files.set(levelDbDir + '/000003.log', Buffer.concat([oldRecord, newRecord]));

      const mockFs = {
        existsSync: (p) => files.has(p) || p === levelDbDir,
        readdirSync: (d) => (d === levelDbDir ? ['000003.log'] : []),
        statSync: () => ({ mtimeMs: 1000 }),
        readFileSync: (p, enc) => {
          const buf = files.get(p);
          if (!buf) throw new Error('ENOENT: ' + p);
          return enc ? buf.toString(enc) : buf;
        },
        mkdirSync: () => {},
        writeFileSync: (p, data) => {
          files.set(p, Buffer.isBuffer(data) ? data : Buffer.from(String(data), 'utf8'));
        },
        chmodSync: () => {},
        renameSync: (from, to) => {
          files.set(to, files.get(from));
          files.delete(from);
        }
      };

      let installed = [
        { id: 'local.owner.applibrary', version: '1.0.0', visible: true, removable: false, systemApp: true },
        amazonApp
      ];
      let removeCalled = false;

      const ctrl = maint.createResidentMaintenanceController({
        fs: mockFs,
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        getTvPower: () => 'active',
        callLunaSend: async (uri, payload) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri === 'luna://com.webos.appInstallService/remove' && payload.id === 'amazon') {
            removeCalled = true;
            installed = installed.filter((a) => a.id !== 'amazon');
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: installed };
          }
          if (uri.endsWith('/statusAll') || uri.endsWith('/status')) {
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });

      const rep = await ctrl.executeTvMaintenance('vk', 'req-seasonal-bridge', 'update', '2027-03-15');
      assert(rep && rep.seasonalCleanup);
      assert.strictEqual(removeCalled, true);
      assert.deepStrictEqual(rep.seasonalCleanup.removalsCompleted, ['amazon']);
      assert(files.has(stateFile));
      const persisted = JSON.parse(files.get(stateFile).toString('utf8'));
      assert.strictEqual(persisted.state.seasonalInstalls.amazon, undefined);
    }

    // 13g. Foreign origin rejection, concurrent Home edit preservation during removal, and unresolved updatesInProgress exclusion
    {
      const foreignBuf = Buffer.from(
        '_http://unrelated.test\x00\x01baaz.sports.v1\x01' +
          JSON.stringify({ followed: ['nfl'], seasonalInstalls: { amazon: true } }),
        'utf8'
      );
      assert.strictEqual(maint.extractBaazSportsFromBuffer(foreignBuf).found, false);

      // Unauthenticated /tmp bridge files are rejected; root-owned 0600 bridge files in non-world-writable dirs are accepted
      const untrustedStore = maint.createSeasonalSportsStateStore({
        fs: {
          existsSync: () => true,
          lstatSync: () => ({
            isSymbolicLink: () => false,
            isDirectory: () => true,
            isFile: () => true,
            uid: 0,
            mode: 0o600
          }),
          readFileSync: () => Buffer.from(JSON.stringify({ followed: ['nfl'], seasonalInstalls: { amazon: true }, autoRemove: { amazon: true } }), 'utf8')
        },
        homeStateBridgePath: '/tmp/r2d2-home-sports-state.json',
        localStorageDirs: []
      });
      assert.deepStrictEqual(untrustedStore.read().followed, []);

      // Concurrent Home edit while removal is polling preserves Keep on the second app
      const bridgeFiles = new Map();
      const homeBridge = '/var/lib/local.owner.r2d2mic/home-bridge.json';
      const persistedBridge = '/var/lib/local.owner.r2d2mic/persisted-state.json';
      let homeObj = {
        followed: ['nfl', 'nhl'],
        dismissed: {},
        kept: {},
        seasonalInstalls: { amazon: true, hulu: true },
        autoRemove: { amazon: true, hulu: true }
      };
      bridgeFiles.set(homeBridge, Buffer.from(JSON.stringify(homeObj), 'utf8'));
      const store = maint.createSeasonalSportsStateStore({
        fs: {
          existsSync: (p) => bridgeFiles.has(p) || p === '/var/lib/local.owner.r2d2mic',
          lstatSync: (p) => ({
            isSymbolicLink: () => false,
            isDirectory: () => p === '/var/lib/local.owner.r2d2mic',
            isFile: () => p !== '/var/lib/local.owner.r2d2mic',
            uid: 0,
            mode: p === '/var/lib/local.owner.r2d2mic' ? 0o700 : 0o600
          }),
          readFileSync: (p, e) => (e ? bridgeFiles.get(p).toString(e) : bridgeFiles.get(p))
        },
        homeStateBridgePath: homeBridge,
        seasonalStatePath: persistedBridge,
        localStorageDirs: [],
        writeAtomic: (p, b) => bridgeFiles.set(p, b)
      });
      let installedBoth = [
        amazonApp,
        { id: 'hulu', title: 'Hulu', type: 'web', visible: true, removable: true, systemApp: false }
      ];
      let becameActive = false;
      const resConcurrent = await maint.runSeasonalAppCleanup({
        nowMs: () => utc(2027, 7).getTime(),
        readSeasonalState: store.read,
        writeSeasonalState: store.write,
        hasUserActivitySince: () => becameActive,
        callLunaSend: async (uri, payload) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/remove')) {
            installedBoth = installedBoth.filter((a) => a.id !== payload.id);
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async () => ({ returnValue: true, apps: installedBoth }),
        sleep: async () => {
          becameActive = true;
          homeObj.kept['nhl:off:2027'] = true;
          delete homeObj.seasonalInstalls.hulu;
          delete homeObj.autoRemove.hulu;
          bridgeFiles.set(homeBridge, Buffer.from(JSON.stringify(homeObj), 'utf8'));
        }
      });
      assert.deepStrictEqual(resConcurrent.removalsCompleted, ['amazon']);
      assert.deepStrictEqual(resConcurrent.removalsSkippedForeground, ['hulu']);
      assert.deepStrictEqual(store.read().kept, { 'nhl:off:2027': true });
      assert.deepStrictEqual(store.read().seasonalInstalls, {});

      // Unrelated preference change in Home after removal does not resurrect stale seasonalInstalls
      homeObj = {
        followed: ['nfl', 'nhl', 'mlb'],
        dismissed: {},
        kept: { 'nhl:off:2027': true },
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true }
      };
      bridgeFiles.set(homeBridge, Buffer.from(JSON.stringify(homeObj), 'utf8'));
      const afterUnrelatedEdit = store.read();
      assert.deepStrictEqual(afterUnrelatedEdit.followed, ['nfl', 'nhl', 'mlb']);
      assert.deepStrictEqual(afterUnrelatedEdit.seasonalInstalls, {});
      assert.deepStrictEqual(afterUnrelatedEdit.autoRemove, {});

      // A genuine new seasonal install with a fresh installToken is recognized
      homeObj = {
        followed: ['nfl', 'nhl', 'mlb'],
        dismissed: {},
        kept: { 'nhl:off:2027': true },
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true },
        installTokens: { amazon: 'tok-fresh-2028' }
      };
      bridgeFiles.set(homeBridge, Buffer.from(JSON.stringify(homeObj), 'utf8'));
      const afterFreshInstall = store.read();
      assert.deepStrictEqual(afterFreshInstall.seasonalInstalls, { amazon: true });
      assert.deepStrictEqual(afterFreshInstall.autoRemove, { amazon: true });

      // Unresolved update job in executeTvMaintenance prevents seasonal removal from firing in the same run
      let clockInProg = mar2027.getTime();
      const mutations = [];
      const ctrlInProg = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => clockInProg,
        sleep: async (ms) => {
          clockInProg += ms;
        },
        installTimeoutMs: 10,
        maxInstallTimeoutMs: 10,
        pollIntervalMs: 5,
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri, payload) => {
          if (
            uri.endsWith('/install') ||
            uri.endsWith('/cancel') ||
            uri.endsWith('/remove')
          ) {
            mutations.push({ uri, payload });
          }
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/cancel')) {
            return { returnValue: false };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [{ ...amazonApp, version: '1.0.0' }] };
          }
          if (uri.endsWith('/statusAll')) {
            return {
              returnValue: true,
              status: { apps: [{ id: 'amazon', updateYN: 'Y', version: '2.0.0' }] }
            };
          }
          if (uri.endsWith('/status')) {
            return {
              returnValue: true,
              status: { apps: [{ id: 'amazon', state: 'downloading', progress: 10 }] }
            };
          }
          return { returnValue: true };
        }
      });
      const repInProg = await ctrlInProg.executeTvMaintenance('vk', 'req-inprog', 'update', '2027-03-15');
      assert.deepStrictEqual(repInProg.updatesInProgress, ['amazon']);
      assert.strictEqual(repInProg.seasonalCleanup, null);
      assert.strictEqual(mutations.some((m) => m.uri.endsWith('/remove')), false);
    }

    // 13h. Authoritative LevelDB selection across multiple directories and fail-closed behavior after Home storage is cleared/compacted
    {
      function encodeVarint(n) {
        const out = [];
        do {
          let b = n & 0x7f;
          n >>>= 7;
          out.push(n ? b | 0x80 : b);
        } while (n);
        return Buffer.from(out);
      }
      function walRecord(seq, op, key, valObj) {
        const k = Buffer.from('_file://\x00\x01' + key, 'utf8');
        const v = valObj !== undefined ? Buffer.from('\x01' + JSON.stringify(valObj), 'utf8') : Buffer.alloc(0);
        const hdr = Buffer.alloc(12);
        hdr.writeBigUInt64LE(BigInt(seq), 0);
        hdr.writeUInt32LE(1, 8);
        const batch = Buffer.concat([
          hdr,
          Buffer.from([op]),
          encodeVarint(k.length),
          k,
          ...(op === 1 ? [encodeVarint(v.length), v] : [])
        ]);
        const recHdr = Buffer.alloc(7);
        recHdr.writeUInt16LE(batch.length, 4);
        recHdr[6] = 1;
        return Buffer.concat([recHdr, batch]);
      }

      const dbFiles = new Map([
        [
          '/old/000003.log',
          walRecord(900, 1, 'baaz.sports.v1', {
            followed: ['nfl'],
            seasonalInstalls: { amazon: true },
            autoRemove: { amazon: true }
          })
        ],
        [
          '/old/000004.log',
          Buffer.from('_https://other.test\x00\x01unrelated\x00\x011', 'utf8')
        ],
        [
          '/new/000003.log',
          walRecord(10, 1, 'baaz.sports.v1', {
            followed: ['nfl'],
            kept: { 'nfl:off:2027': true },
            seasonalInstalls: {}
          })
        ]
      ]);
      const multiStore = maint.createSeasonalSportsStateStore({
        fs: {
          existsSync: (p) => dbFiles.has(p) || p === '/old' || p === '/new',
          readdirSync: (d) => (d === '/old' ? ['000003.log', '000004.log'] : ['000003.log']),
          statSync: (p) => ({
            mtimeMs: p === '/old/000004.log' ? 5000 : p.startsWith('/new') ? 2000 : 1000
          }),
          readFileSync: (p, enc) => {
            if (!dbFiles.has(p)) throw new Error('ENOENT');
            return enc ? dbFiles.get(p).toString(enc) : dbFiles.get(p);
          }
        },
        localStorageDirs: ['/old', '/new'],
        homeStateBridgePath: '/absent-bridge',
        seasonalStatePath: '/saved-state',
        writeAtomic: (p, b) => dbFiles.set(p, b)
      });
      // When both /old and /new contain conflicting Home records, source authority is ambiguous -> fail closed
      const ambiguousRead = multiStore.read();
      assert.deepStrictEqual(ambiguousRead.followed, []);
      assert.deepStrictEqual(ambiguousRead.seasonalInstalls, {});
      assert.deepStrictEqual(ambiguousRead.autoRemove, {});

      // When /old only contains unrelated origins and /new contains Home records, /new is unambiguously selected
      dbFiles.delete('/old/000003.log');
      const multiOnlyNew = maint.createSeasonalSportsStateStore({
        fs: {
          existsSync: (p) => dbFiles.has(p) || p === '/old' || p === '/new',
          readdirSync: (d) => (d === '/old' ? ['000004.log'] : ['000003.log']),
          statSync: () => ({ mtimeMs: 5000 }),
          readFileSync: (p, enc) => {
            if (!dbFiles.has(p)) throw new Error('ENOENT');
            return enc ? dbFiles.get(p).toString(enc) : dbFiles.get(p);
          }
        },
        localStorageDirs: ['/old', '/new'],
        homeStateBridgePath: '/absent-bridge',
        seasonalStatePath: '/saved-state',
        writeAtomic: (p, b) => dbFiles.set(p, b)
      });
      const multiRead = multiOnlyNew.read();
      assert.deepStrictEqual(multiRead.kept, { 'nfl:off:2027': true });
      assert.deepStrictEqual(multiRead.seasonalInstalls, {});

      // Truncated newer WAL record after an older consent record fails closed instead of retaining older consent
      const oldOptInRec = walRecord(10, 1, 'baaz.sports.v1', {
        followed: ['nfl'],
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true }
      });
      const newKeepRec = walRecord(11, 1, 'baaz.sports.v1', {
        followed: ['nfl'],
        kept: { 'nfl:off:2027': true },
        seasonalInstalls: {},
        autoRemove: {}
      });
      const truncatedWalBuf = Buffer.concat([
        oldOptInRec,
        newKeepRec.subarray(0, newKeepRec.length - 8)
      ]);
      const truncatedExtracted = maint.extractBaazSportsFromBuffer(truncatedWalBuf);
      assert.strictEqual(truncatedExtracted.found, false);
      assert.strictEqual(truncatedExtracted.corrupt, true);

      // Revalidating cleanup eligibility after appInstallService/status prevents removal if Keep is chosen during status await
      let statusAwaitState = {
        followed: ['nfl'],
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true }
      };
      let removeAfterStatusKeep = false;
      const resStatusRace = await maint.runSeasonalAppCleanup({
        tvPower: 'active',
        requireInstallStatus: true,
        nowMs: () => mar2027.getTime(),
        readSeasonalState: () => statusAwaitState,
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/remove')) {
            removeAfterStatusKeep = true;
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [amazonApp] };
          }
          if (uri.endsWith('/status')) {
            // User chooses Keep while appInstallService/status is in flight
            statusAwaitState = {
              followed: ['nfl'],
              kept: { 'nfl:off:2027': true },
              seasonalInstalls: {},
              autoRemove: {}
            };
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });
      assert.strictEqual(removeAfterStatusKeep, false);
      assert.deepStrictEqual(resStatusRace.removalsAttempted, []);
      assert.deepStrictEqual(resStatusRace.removalsSkippedForeground, ['amazon']);

      // Seed persisted state, then simulate Home storage cleared and tombstone compacted away
      dbFiles.set(
        '/new/000003.log',
        walRecord(11, 1, 'baaz.sports.v1', {
          followed: ['nba', 'mlb'],
          seasonalInstalls: { 'com.apple.appletv': true },
          autoRemove: { 'com.apple.appletv': true }
        })
      );
      multiOnlyNew.write(multiOnlyNew.read());
      dbFiles.set('/new/000003.log', walRecord(12, 1, 'baaz.calm.v1', true));
      const afterClearAndCompact = multiOnlyNew.read();
      assert.deepStrictEqual(afterClearAndCompact, {
        followed: [],
        dismissed: {},
        kept: {},
        seasonalInstalls: {},
        autoRemove: {}
      });

      // Unconfirmed seasonal removal in update-standby mode defers power/turnOff and marks maintenanceComplete=false
      let turnOffCalled = false;
      const ctrlStandbyDefer = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        pollIntervalMs: 1,
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/remove')) {
            return { returnValue: false };
          }
          if (uri.endsWith('/turnOff')) {
            turnOffCalled = true;
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [amazonApp] };
          }
          if (uri.endsWith('/statusAll') || uri.endsWith('/status')) {
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });
      const repStandbyDefer = await ctrlStandbyDefer.executeTvMaintenance(
        'vk',
        'req-standby-defer',
        'update-standby',
        '2027-03-15'
      );
      assert.strictEqual(turnOffCalled, false);
      assert.strictEqual(repStandbyDefer.returnedToStandby, false);
      assert.strictEqual(repStandbyDefer.maintenanceComplete, false);
      assert.deepStrictEqual(repStandbyDefer.seasonalCleanup.removalsFailed, ['amazon']);

      // Failed appInstallService/status prevents seasonal removal from firing and sets maintenanceComplete=false
      let removeOnFailedStatus = false;
      const ctrlStatusFail = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/remove')) {
            removeOnFailedStatus = true;
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [amazonApp] };
          }
          if (uri.endsWith('/statusAll')) {
            return { returnValue: true, status: { apps: [] } };
          }
          if (uri.endsWith('/status')) {
            return { returnValue: false };
          }
          return { returnValue: true };
        }
      });
      const repStatusFail = await ctrlStatusFail.executeTvMaintenance(
        'vk',
        'req-status-fail',
        'update',
        '2027-03-15'
      );
      assert.strictEqual(removeOnFailedStatus, false);
      assert.strictEqual(repStatusFail.seasonalCleanup, null);
      assert.strictEqual(repStatusFail.maintenanceComplete, false);

      // Failed foreground observation during pre-standby cleanup prevents power/turnOff and marks maintenanceComplete=false
      let listCallsForFgTest = 0;
      let turnOffOnFgFail = false;
      const ctrlStandbyFgFail = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            // Succeed during runUpdateWorkflow; fail once runSeasonalAppCleanup starts (after its listApps call)
            if (listCallsForFgTest >= 2) {
              return { returnValue: false };
            }
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/turnOff')) {
            turnOffOnFgFail = true;
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            listCallsForFgTest++;
            return { returnValue: true, apps: [amazonApp] };
          }
          if (uri.endsWith('/statusAll') || uri.endsWith('/status')) {
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });
      const repStandbyFgFail = await ctrlStandbyFgFail.executeTvMaintenance(
        'vk',
        'req-standby-fg-fail',
        'update-standby',
        '2027-03-15'
      );
      assert.strictEqual(turnOffOnFgFail, false);
      assert.strictEqual(repStandbyFgFail.returnedToStandby, false);
      assert.strictEqual(repStandbyFgFail.maintenanceComplete, false);
      assert.strictEqual(repStandbyFgFail.seasonalCleanup.foregroundObservationFailed, true);

      // Wake ownership revoked during beforeStandbyTurnOff cleanup prevents power/turnOff
      let wakeOwnedLive = true;
      let turnOffOnRevokedWake = false;
      const ctrlWakeRevoked = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        isWakeOwned: () => wakeOwnedLive,
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          if (uri.endsWith('/remove')) {
            wakeOwnedLive = false;
            return { returnValue: true };
          }
          if (uri.endsWith('/turnOff')) {
            turnOffOnRevokedWake = true;
            return { returnValue: true };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: wakeOwnedLive ? [amazonApp] : [] };
          }
          if (uri.endsWith('/statusAll') || uri.endsWith('/status')) {
            return { returnValue: true, status: { apps: [] } };
          }
          return { returnValue: true };
        }
      });
      const repWakeRevoked = await ctrlWakeRevoked.executeTvMaintenance(
        'vk',
        'req-wake-revoked',
        'update-standby',
        '2027-03-15'
      );
      assert.strictEqual(turnOffOnRevokedWake, false);
      assert.strictEqual(repWakeRevoked.returnedToStandby, false);
      assert.strictEqual(repWakeRevoked.maintenanceComplete, false);

      // Deferred seasonal cleanup (e.g. cleanup-time install-status failure) marks maintenanceComplete=false and preserves retry state
      let statusCallIdx = 0;
      let savedUpdaterState = { schema: 1, lastUpdateDay: '2027-03-14' };
      const ctrlCleanupDeferred = maint.createResidentMaintenanceController({
        fs: { existsSync: () => false },
        nowMs: () => mar2027.getTime(),
        sleep: async () => {},
        readUpdaterState: () => savedUpdaterState,
        writeUpdaterState: (next) => {
          savedUpdaterState = next;
        },
        readSeasonalState: () => ({
          followed: ['nfl'],
          seasonalInstalls: { amazon: true },
          autoRemove: { amazon: true }
        }),
        callLunaSend: async (uri) => {
          if (uri.endsWith('/getForegroundAppInfo')) {
            return { returnValue: true, appId: 'local.owner.applibrary' };
          }
          return { returnValue: true };
        },
        callLunaSendLarge: async (uri) => {
          if (uri.endsWith('/listApps')) {
            return { returnValue: true, apps: [amazonApp] };
          }
          if (uri.endsWith('/statusAll')) {
            return { returnValue: true, status: { apps: [] } };
          }
          if (uri.endsWith('/status')) {
            statusCallIdx++;
            if (statusCallIdx === 1) {
              return { returnValue: true, status: { apps: [] } };
            }
            return { returnValue: false };
          }
          return { returnValue: true };
        }
      });
      const repCleanupDeferred = await ctrlCleanupDeferred.executeTvMaintenance(
        'vk',
        'req-cleanup-deferred',
        'update',
        '2027-03-15'
      );
      assert.strictEqual(repCleanupDeferred.maintenanceComplete, false);
      assert.deepStrictEqual(repCleanupDeferred.pendingApps, ['amazon']);
      assert.strictEqual(savedUpdaterState.pendingRetryDay, '2027-03-15');

      // Replacement install token published while removal confirmation is in flight is preserved and not marked removed
      let repHome = {
        followed: ['nfl'],
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true },
        installTokens: { amazon: 'old' }
      };
      const repFiles = new Map();
      const repStore = maint.createSeasonalSportsStateStore({
        localStorageDirs: [],
        seasonalStatePath: '/fixture/rep-state.json',
        getHomeLocalStorageItem: () => JSON.stringify(repHome),
        fs: {
          existsSync: (p) => repFiles.has(p),
          readFileSync: (p, e) => (e ? repFiles.get(p).toString(e) : repFiles.get(p))
        },
        writeAtomic: (p, b) => repFiles.set(p, b)
      });
      let repRemoved = false;
      await maint.runSeasonalAppCleanup({
        nowMs: () => mar2027.getTime(),
        readSeasonalState: repStore.read,
        writeSeasonalState: repStore.write,
        callLunaSend: async (uri) => {
          if (uri.endsWith('/remove')) {
            repRemoved = true;
            return { returnValue: true };
          }
          return { returnValue: true, appId: 'local.owner.applibrary' };
        },
        callLunaSendLarge: async () => {
          if (repRemoved) {
            repHome = {
              followed: ['nfl'],
              seasonalInstalls: { amazon: true },
              autoRemove: { amazon: true },
              installTokens: { amazon: 'replacement' }
            };
            return { returnValue: true, apps: [] };
          }
          return { returnValue: true, apps: [amazonApp] };
        }
      });
      assert.deepStrictEqual(repStore.read().seasonalInstalls, { amazon: true });
      assert.deepStrictEqual(repStore.read().installTokens, { amazon: 'replacement' });

      // Partial database read (EIO on a newer LevelDB file) fails closed instead of using an older opt-in record
      const eioStore = maint.createSeasonalSportsStateStore({
        fs: {
          existsSync: (p) => p === '/eiodb' || p === '/eiodb/000001.log' || p === '/eiodb/000002.log',
          readdirSync: () => ['000001.log', '000002.log'],
          statSync: (p) => ({ mtimeMs: p.endsWith('000002.log') ? 2000 : 1000 }),
          readFileSync: (p) => {
            if (p.endsWith('000002.log')) {
              const err = new Error('EIO');
              err.code = 'EIO';
              throw err;
            }
            return walRecord(1, 1, 'baaz.sports.v1', {
              followed: ['nfl'],
              seasonalInstalls: { amazon: true },
              autoRemove: { amazon: true }
            });
          }
        },
        localStorageDirs: ['/eiodb']
      });
      assert.deepStrictEqual(eioStore.read().followed, []);
      assert.deepStrictEqual(eioStore.read().seasonalInstalls, {});

      // Concurrent new installation in Home between read() and write() is preserved, not marked as removed
      const concFiles = new Map();
      let concHome = JSON.stringify({
        followed: ['nfl'],
        seasonalInstalls: { amazon: true },
        autoRemove: { amazon: true },
        installTokens: { amazon: 'old' }
      });
      const concStore = maint.createSeasonalSportsStateStore({
        fs: {
          existsSync: (p) => concFiles.has(p),
          readFileSync: (p, e) => (e ? concFiles.get(p).toString(e) : concFiles.get(p))
        },
        localStorageDirs: [],
        getHomeLocalStorageItem: () => concHome,
        seasonalStatePath: '/conc-state',
        writeAtomic: (p, b) => concFiles.set(p, b)
      });
      const nextSnap = concStore.read();
      delete nextSnap.seasonalInstalls.amazon;
      delete nextSnap.autoRemove.amazon;
      delete nextSnap.installTokens.amazon;
      concHome = JSON.stringify({
        followed: ['nfl', 'mls'],
        seasonalInstalls: { amazon: true, 'com.apple.appletv': true },
        autoRemove: { amazon: true, 'com.apple.appletv': true },
        installTokens: { amazon: 'old', 'com.apple.appletv': 'new' }
      });
      concStore.write(nextSnap);
      const savedConc = JSON.parse(concFiles.get('/conc-state').toString('utf8'));
      assert.deepStrictEqual(savedConc.removedApps, { amazon: 'tok:old' });
      const concAfter = concStore.read();
      assert.deepStrictEqual(concAfter.followed, ['nfl', 'mls']);
      assert.deepStrictEqual(concAfter.seasonalInstalls, { 'com.apple.appletv': true });
      assert.deepStrictEqual(concAfter.autoRemove, { 'com.apple.appletv': true });
    }

    groups++;
  }

  process.stdout.write(JSON.stringify({ groups, nativeCalls: 0, modelCalls: 0 }) + '\n');
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
