"""Explicit shared browser/voice owner assembly; inert until warm() and serve().

The host retains the HTTP server, Manager, configuration lock and weather owner.
This object reuses that exact rendered Manager and shuts down only its voice
listener/model worker. It creates no credential store or TV network client.
"""
import math
from pathlib import Path
import sys
import threading
import time

if __package__:
    from .controller import Refused as ProviderRefused
    from .model_worker import ModelWorker, WorkerASR, WorkerIntent
    from .sealed_startup import validate_ready
    from .service import VoiceService
else:
    from controller import Refused as ProviderRefused
    from model_worker import ModelWorker, WorkerASR, WorkerIntent
    from sealed_startup import validate_ready
    from service import VoiceService


def _load_pool_module():
    for module_name in ('voice_control.pool', 'pool', 'voice.runtime.pool'):
        try:
            return __import__(module_name, fromlist=['WorkerPool', 'WarmHook', 'create_warm_hook', 'pool_providers'])
        except ImportError:
            continue
    runtime_dir = Path(__file__).resolve().parents[1] / 'runtime'
    if runtime_dir.is_dir() and str(runtime_dir) not in sys.path:
        sys.path.insert(0, str(runtime_dir))
        return __import__('pool', fromlist=['WorkerPool', 'WarmHook', 'create_warm_hook', 'pool_providers'])
    raise ImportError('pool unavailable')


def _is_pool_instance(obj):
    return type(obj).__name__ == 'WorkerPool' and all(
        callable(getattr(obj, attr, None)) for attr in ('begin_warm', 'poll', 'unload', 'lease', 'give_back', 'summary'))


def _is_hook_instance(obj):
    return type(obj).__name__ == 'WarmHook' and hasattr(obj, 'pool') and all(
        callable(getattr(obj, attr, None)) for attr in ('tick', 'admit', 'release', 'lease', 'give_back', 'close'))


class AssemblyRefused(ValueError):
    pass


class _ASRBridge:
    def __init__(self, owner, refused, pool_asr=None):
        self.owner, self.refused = owner, refused
        self.provider = pool_asr if pool_asr is not None else WorkerASR(owner.worker)

    @property
    def qualified(self):
        return self.owner._worker_ready(reserve=30) and self.provider.qualified is True

    def transcribe(self, pcm, *, rate, timeout_s):
        if not self.qualified:
            raise self.refused('ASR_UNAVAILABLE')
        try:
            return self.provider.transcribe(pcm, rate=rate, timeout_s=timeout_s)
        except Exception:
            # Source worker and rendered controller have distinct Refused types.
            raise self.refused('ASR_UNAVAILABLE') from None


class SharedVoiceOwner:
    def __init__(self, manager, worker, viewer, manager_key, address, port, context,
                certificate_sha256, device_key, *, enabled=False,
                 max_connections=128, lifetime_s=1500, agent_mode=False, tv_gateway=None,
                 power_source=None, tv_power_reader=None, audit=None, killswitch=None):
        # Import the exact existing rendered package; do not create a second
        # Manager or a second copy of its controller/ingress observation types.
        from manager import Manager, SPARK, PORT, handler_for
        from voice_control.browser_spawn import ControlledSpawn
        from voice_control.channel_adapter import ChannelBrowserAdapter
        from voice_control.controller import Controller, Refused
        from voice_control.ingress import Device, VoiceIngress
        from voice_control.model_adapter import LocalIntentAdapter
        from voice_control.viewer_handoff import TVViewerHandoff
        is_single_worker = type(worker) is ModelWorker or (
            type(worker).__name__ == 'ModelWorker' and hasattr(worker, 'start') and hasattr(worker, 'cleanup_complete'))
        is_pool = _is_pool_instance(worker)
        is_hook = _is_hook_instance(worker)
        if (type(manager) is not Manager or type(manager.control_spawn) is not ControlledSpawn or
                manager.control_spawn.manager is not manager or
                not (is_single_worker or is_pool or is_hook) or
                type(viewer) is not TVViewerHandoff or type(enabled) is not bool or
                type(agent_mode) is not bool or
                type(max_connections) is not int or not 1 <= max_connections <= 128 or
                type(lifetime_s) not in (int, float) or not math.isfinite(lifetime_s) or not 0 < lifetime_s <= 1800):
            raise AssemblyRefused('ASSEMBLY_CONFIG')
        self.pool = None
        self.warm_hook = None
        self.tv_power_reader = tv_power_reader
        self._lease_coordinator = None
        self._close_budget_s = 5.0
        if is_single_worker:
            if worker.process is not None or worker.cleanup_complete is not True:
                raise AssemblyRefused('ASSEMBLY_WORKER_OWNERSHIP')
        else:
            pool_mod = _load_pool_module()
            if is_hook:
                self.warm_hook = worker
                self.pool = worker.pool
                if (not _is_pool_instance(self.pool) or self.warm_hook.stopping.is_set() or
                        self.warm_hook.controller.state != 'unloaded' or not self.warm_hook.controller.clean):
                    raise AssemblyRefused('ASSEMBLY_WORKER_OWNERSHIP')
            else:
                self.pool = worker
                if any(x is not None for x in (power_source, tv_power_reader, audit, killswitch)):
                    self.warm_hook = pool_mod.create_warm_hook(
                        self.pool, clock=self.pool.clock, ssap=tv_power_reader,
                        source=power_source, audit=audit, killswitch=killswitch)
            summary = self.pool.summary()
            if (summary.get('state') != 'empty' or summary.get('leased') is not False or
                    summary.get('cleanupFailures', 0) != 0 or
                    self.pool.active is not None or self.pool.standby is not None):
                raise AssemblyRefused('ASSEMBLY_WORKER_OWNERSHIP')
        if viewer.open_url != 'http://' + SPARK + ':' + str(PORT) + '/' + manager_key + '/open':
            raise AssemblyRefused('ASSEMBLY_MANAGER_BINDING')
        self.manager, self.worker, self.viewer = manager, worker, viewer
        self.enabled = enabled
        self.agent_mode = agent_mode
        self.max_connections, self.lifetime_s = max_connections, lifetime_s
        self.state = 'new'
        self.lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.quiet = threading.Event(); self.quiet.set()
        self.closing = threading.Event()
        self.owner_thread = None
        self.cleanup_complete = False
        self.started_worker = False
        self.runtime = ChannelBrowserAdapter(manager, manager.control_spawn, viewer=viewer, qualified=enabled)
        if self.pool is not None:
            pool_mod = _load_pool_module()
            target = self.warm_hook if self.warm_hook is not None else self.pool
            self._lease_coordinator, pool_asr, intent_provider = pool_mod.pool_providers(
                target, asr_refused=Refused, intent_refused=ProviderRefused)
            asr_bridge = _ASRBridge(self, Refused, pool_asr=pool_asr)
        else:
            intent_provider = WorkerIntent(worker)
            asr_bridge = _ASRBridge(self, Refused)
        model_adapter = LocalIntentAdapter(intent_provider, ProviderRefused)
        if agent_mode:
            if __package__:
                from .agent_controller import AgentController
            else:
                from agent_controller import AgentController
            self.controller = AgentController(model_adapter, self.runtime, tv_gateway=tv_gateway)
        else:
            self.controller = Controller(model_adapter, self.runtime)
        if self._lease_coordinator is not None:
            orig_arm = self.controller.arm
            orig_cancel = self.controller.cancel
            coord = self._lease_coordinator
            def _arm_with_activity():
                lease = orig_arm()
                coord.note_activity()
                return lease
            def _cancel_with_release(lease=None):
                try:
                    return orig_cancel(lease)
                finally:
                    coord.release()
            self.controller.arm = _arm_with_activity
            self.controller.cancel = _cancel_with_release
        self.ingress = VoiceIngress(self.controller, Device(certificate_sha256, device_key), asr_bridge)
        # Constructor validates TLS and listener scope but does not open a socket.
        self.service = VoiceService(address, port, context, self.ingress, qualified=enabled)
        # The host creates its ordinary existing Server with THIS handler. Every
        # HTTP session and voice operation now reaches the same Manager object.
        self.http_handler = handler_for(manager, manager_key)

    def _selected_pool_worker(self):
        pool = self.pool
        with pool.lock:
            if pool.standby is not None and pool._servable(pool.standby) and pool.leased is None:
                return pool.standby
            return pool.active

    def _worker_ready(self, *, reserve=0):
        if self.pool is not None:
            try:
                if not self.started_worker or self.closing.is_set() or not self.enabled:
                    return False
                if self.warm_hook is not None and (
                        self.warm_hook.stopping.is_set() or
                        self.warm_hook.controller.state in ('degraded', 'paused')):
                    return False
                worker = self._selected_pool_worker()
                return (worker is not None and getattr(worker, 'qualified', False) is True and
                        worker.process is not None and worker.process.poll() is None and
                        getattr(worker, 'cleanup_complete', False) is False and worker.counter <= 254 and
                        worker.created is not None and 0 <= self.pool.clock() - worker.created < 1800 - reserve and
                        validate_ready(worker.sealed, worker.attempt)['sealed'] is True)
            except Exception:
                return False
        worker = self.worker
        try:
            return (self.started_worker and not self.closing.is_set() and self.enabled and worker.qualified is True and
                    worker.process is not None and worker.process.poll() is None and
                    worker.cleanup_complete is False and worker.counter <= 254 and
                    worker.created is not None and 0 <= worker.clock()-worker.created < 1800-reserve and
                    validate_ready(worker.sealed, worker.attempt)['sealed'] is True)
        except Exception:
            return False

    def _warm_pool(self, timeout_s):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise AssemblyRefused('ASSEMBLY_STARTUP_UNQUALIFIED')
        deadline = time.monotonic() + timeout_s
        if self.tv_power_reader is not None and callable(getattr(self.tv_power_reader, 'start', None)):
            self.tv_power_reader.start()
        if self.warm_hook is not None:
            self.warm_hook.note_tv_activity()
            self.warm_hook.tick()
        else:
            self.pool.begin_warm()
        while True:
            state = self.warm_hook.tick() if self.warm_hook is not None else self.pool.poll()
            pool_state = self.pool.poll()
            if pool_state == 'ready' and (self.warm_hook is None or state == 'ready'):
                break
            if (pool_state == 'failed' or state in ('degraded', 'paused') or
                    self.closing.is_set() or time.monotonic() >= deadline):
                break
            job = getattr(self.pool, '_warm', None)
            wait_s = min(0.02, max(0.0, deadline - time.monotonic()))
            if job is not None and hasattr(job, 'finished'):
                job.finished.wait(wait_s)
            elif wait_s > 0:
                time.sleep(wait_s)
        active = self._selected_pool_worker()
        if (self.pool.poll() != 'ready' or not self._worker_ready(reserve=120) or
                active is None or active.counter != 0):
            raise AssemblyRefused('ASSEMBLY_STARTUP_UNQUALIFIED')
        if self.warm_hook is not None:
            self.warm_hook.start()

    def warm(self, *, timeout_s=120):
        admitted = False
        try:
            with self.lock:
                if self.pool is not None:
                    factory_qualified = getattr(self.pool.factory, 'qualified', True) is True
                    if (not self.enabled or self.state != 'new' or self.closing.is_set() or
                            not factory_qualified or self.viewer.enabled is not True):
                        raise AssemblyRefused('ASSEMBLY_UNQUALIFIED')
                    summary = self.pool.summary()
                    if (summary.get('state') != 'empty' or summary.get('leased') is not False or
                            summary.get('cleanupFailures', 0) != 0 or self.pool.active is not None):
                        raise AssemblyRefused('ASSEMBLY_WORKER_OWNERSHIP')
                else:
                    if (not self.enabled or self.state != 'new' or self.closing.is_set() or
                            self.worker.qualified is not True or self.viewer.enabled is not True):
                        raise AssemblyRefused('ASSEMBLY_UNQUALIFIED')
                    if self.worker.process is not None or self.worker.cleanup_complete is not True:
                        raise AssemblyRefused('ASSEMBLY_WORKER_OWNERSHIP')
                admitted = True
                self.started_worker = True
                self.state = 'warming'; self.owner_thread = threading.get_ident(); self.quiet.clear()
            # This performs the actual mandatory startup handshake. Boolean
            # provider admission alone cannot open the listener or accept PCM.
            if self.pool is not None:
                self._warm_pool(timeout_s)
            else:
                self.worker.start(timeout_s=timeout_s)
                if not self._worker_ready(reserve=120) or self.worker.counter != 0:
                    raise AssemblyRefused('ASSEMBLY_STARTUP_UNQUALIFIED')
            with self.lock:
                if self.closing.is_set():
                    raise AssemblyRefused('ASSEMBLY_CLOSED')
                self.state = 'ready'; self.owner_thread = None
                self.quiet.set()
        except BaseException as error:
            if not admitted:
                raise
            self._finish()
            if not isinstance(error, Exception):
                raise  # Preserve Ctrl-C/SystemExit after publishing cleanup state.
            raise AssemblyRefused('ASSEMBLY_STARTUP_UNQUALIFIED') from None

    def serve(self):
        with self.lock:
            if self.state != 'ready' or not self._worker_ready(reserve=120):
                raise AssemblyRefused('ASSEMBLY_NOT_READY')
            if self.pool is not None:
                lifetime = self.lifetime_s
            else:
                remaining = 1800 - (self.worker.clock()-self.worker.created) - 120
                lifetime = min(self.lifetime_s, remaining)
            self.state = 'serving'; self.owner_thread = threading.get_ident(); self.quiet.clear()
        try:
            # At most two model calls per authenticated command: ASR and intent.
            # Reserve 120s for final capture/ASR/intent, the controller's
            # 90s browser window and owned worker cleanup after acceptance.
            self.service.serve(max_connections=self.max_connections, lifetime_s=lifetime)
        finally:
            self._finish()

    def _finish(self):
        with self.cleanup_lock:
            self.closing.set()
            self.runtime.browser_enabled = False
            self.service.close()
            if self._lease_coordinator is not None:
                self._lease_coordinator.release()
            reader_closed = True
            if self.tv_power_reader is not None and callable(getattr(self.tv_power_reader, 'close', None)):
                try:
                    reader_closed = self.tv_power_reader.close(timeout_s=min(1.0, self._close_budget_s)) is True
                except Exception:
                    reader_closed = False
            if self.pool is not None:
                if self.started_worker:
                    if self.warm_hook is not None:
                        clean = self.warm_hook.close(timeout_s=self._close_budget_s)
                    else:
                        clean = self.pool.unload(timeout_s=self._close_budget_s)
                else:
                    summary = self.pool.summary()
                    clean = summary.get('state') == 'empty' and summary.get('cleanupFailures', 0) == 0
            else:
                clean = (self.worker.close() if self.started_worker else
                         self.worker.process is None and self.worker.cleanup_complete is True)
            with self.lock:
                self.cleanup_complete = clean is True and reader_closed
                self.state = 'closed' if self.cleanup_complete else 'cleanup-pending'
                self.owner_thread = None
                self.quiet.set()

    def close(self, *, timeout_s=5):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 15:
            raise AssemblyRefused('ASSEMBLY_TIMEOUT')
        self._close_budget_s = float(timeout_s)
        self.closing.set()
        self.runtime.browser_enabled = False
        self.service.close()  # Cancels controller and interrupts owned TLS reads.
        if self.pool is not None:
            cancel = getattr(self.pool, 'cancel_warm', None)
            if callable(cancel):
                try:
                    cancel(escalate=False)
                except Exception:
                    pass
        with self.lock:
            active = self.state in ('warming', 'serving')
            same_thread = self.owner_thread == threading.get_ident()
        if active:
            if same_thread or not self.quiet.wait(timeout_s):
                return False  # No fabricated cleanup proof while work is active.
        else:
            self._finish()
        return self.cleanup_complete
