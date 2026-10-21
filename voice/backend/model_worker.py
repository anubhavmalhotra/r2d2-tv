"""Owned persistent child with hard pipe deadlines and finite request budget.

Never includes speech/text in argv, environment, files or logs. Child stdout is
an anonymous bounded response pipe. Only explicitly configured local weights.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import signal
import struct
import subprocess
import threading
import time

if __package__:
    from .controller import Refused, bounded_text
    from .ingress import strict_json
    from .local_qwen import prompt_for_phase
    from .sealed_startup import validate_ready
    from .worker_runtime import Runtime
    from .startup_diagnostic import validate_diagnostic, load_diagnostic
else:
    from controller import Refused, bounded_text
    from ingress import strict_json
    from local_qwen import prompt_for_phase
    from sealed_startup import validate_ready
    from worker_runtime import Runtime
    from startup_diagnostic import validate_diagnostic,load_diagnostic


def validate_startup_failure(value):
    if(type(value)is not dict or set(value)!={'schema','state','stage','diagnostic'}
       or type(value['schema'])is not int or value['schema']!=2 or value['state']!='refused'
       or value['stage']not in('parent-startup','bootstrap','model-hashes','load-asr','load-intent','ready-seal')
       or value['diagnostic']is None):raise ValueError('STARTUP_FAILURE_SCHEMA')
    validate_diagnostic(value['diagnostic'])
    return dict(value)


class ModelWorker:
    def __init__(self, python, asr_path, intent_path, *, qualified=False,
                 clock=time.monotonic, spawn=subprocess.Popen, prompt_phase=None):
        for path in (python, asr_path, intent_path):
            if not Path(path).is_absolute():
                raise Refused('MODEL_CONFIG')
        self.python, self.asr_path, self.intent_path = map(str, (python, asr_path, intent_path))
        self.qualified = qualified is True
        self.clock, self.spawn = clock, spawn
        self.process = None
        self.lock = threading.Lock()
        self.counter = 0
        self.created = None
        self.cleanup_complete = True
        self.attempt = None
        self.sealed = None
        self.runtime = None
        self.startup_failure = None
        self.prompt_phase = None
        self.prompt_sha256 = None
        self.configure_prompt(prompt_for_phase(prompt_phase), phase=prompt_phase)

    def configure_prompt(self, system_prompt, *, phase=None):
        if self.process is not None or self.cleanup_complete is not True:
            raise Refused('MODEL_CONFIG')
        expected = prompt_for_phase(phase)
        if type(system_prompt) is not str or system_prompt != expected:
            raise Refused('MODEL_CONFIG')
        self.prompt_phase = phase
        self.prompt_sha256 = hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()

    def start(self, *, timeout_s=120):
        if self.process is not None or self.cleanup_complete is not True or not 0 < timeout_s <= 120:
            raise Refused('MODEL_CONFIG')
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        env.update({'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                    'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                    'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
                    'TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1',
                    'MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1','CUDA_CACHE_DISABLE':'1'})
        if self.prompt_phase is not None:
            env['R2D2_PROMPT_PHASE'] = self.prompt_phase
        self.attempt = secrets.token_hex(16)
        self.sealed = None
        self.startup_failure = None
        try:
            self.runtime=Runtime(self.attempt)
            self.cleanup_complete=False
            env['TMPDIR']=str(self.runtime.temp)
            self.process = self.spawn([self.python, '-s', '-B', str(Path(__file__).with_name('worker_child.py')),
                                       self.asr_path, self.intent_path, self.attempt],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                                      close_fds=True)
            # Each exact fresh child starts its protocol sequence at one. Never
            # carry a previous child's consumed request budget across a clean restart.
            self.counter = 0
            self.created = self.clock()
            self.cleanup_complete = False
            # The child keeps its normal blocking pipe ends. Only parent ends
            # become nonblocking: readiness permits a partial operation and does
            # not guarantee enough capacity for a whole 8192-byte write.
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            value = strict_json(self._read_frame(self.clock() + timeout_s, 8192), 8192)
            if type(value)is dict and value.get('state')=='refused':
                self.startup_failure=validate_startup_failure(value)
                raise Refused('MODEL_UNAVAILABLE')
            if (type(value) is not dict or set(value) != {'schema', 'state', 'isolation'} or
                    type(value['schema']) is not int or value['schema'] != 2 or
                    value['state'] != 'ready'):
                raise Refused('MODEL_UNAVAILABLE')
            self.sealed = validate_ready(value['isolation'], self.attempt)
        except BaseException as error:
            if self.startup_failure is None:
                self.startup_failure={'schema':2,'state':'refused','stage':'parent-startup','diagnostic':load_diagnostic(error)}
            self.close()
            if not isinstance(error, Exception):
                raise  # Preserve Ctrl-C/SystemExit after owned child cleanup.
            raise Refused('MODEL_UNAVAILABLE') from None

    def _io(self, fd, data_or_count, deadline, write=False):
        result = bytearray()
        offset = 0
        count = len(data_or_count) if write else data_or_count
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE if write else selectors.EVENT_READ)
            while offset < count:
                remaining = deadline - self.clock()
                if remaining <= 0 or not selector.select(remaining):
                    raise Refused('MODEL_UNAVAILABLE')
                if write:
                    try:
                        size = os.write(fd, memoryview(data_or_count)[offset:offset + 8192])
                    except BlockingIOError:
                        continue
                    if size <= 0:
                        raise Refused('MODEL_UNAVAILABLE')
                    offset += size
                else:
                    try:
                        chunk = os.read(fd, min(8192, count - offset))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise Refused('MODEL_UNAVAILABLE')
                    result.extend(chunk)
                    offset += len(chunk)
        return bytes(result) if not write else None

    def _read_frame(self, deadline, maximum=8192):
        fd = self.process.stdout.fileno()
        length = struct.unpack('>I', self._io(fd, 4, deadline))[0]
        if not 1 <= length <= maximum:
            raise Refused('MODEL_UNAVAILABLE')
        return self._io(fd, length, deadline)

    def call(self, action, value, *, timeout_s):
        if (action not in ('asr', 'intent') or self.process is None or self.sealed is None or
                not 0 < timeout_s <= (8 if action == 'asr' else 5) or
                not self.lock.acquire(blocking=False)):
            raise Refused('MODEL_UNAVAILABLE')
        payload = raw = row = None
        try:
            if action == 'asr':
                if type(value) not in (bytes, bytearray) or not 640 <= len(value) <= 320000 or len(value) % 2:
                    raise Refused('MODEL_SCHEMA')
            else:
                if not bounded_text(value, 1024, 4096):
                    raise Refused('MODEL_SCHEMA')
            try:
                validate_ready(self.sealed, self.attempt)
                if (self.process.poll() is not None or self.counter >= 256 or
                        self.clock() - self.created >= 1800):
                    raise Refused('MODEL_UNAVAILABLE')
                self.counter += 1
                header = {'schema': 1, 'id': self.counter, 'action': action}
                if action == 'asr':
                    header['bytes'] = len(value)
                else:
                    header['text'] = value
                payload = json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode()
                deadline = self.clock() + timeout_s
                self._io(self.process.stdin.fileno(), struct.pack('>I', len(payload)) + payload, deadline, True)
                if action == 'asr':
                    self._io(self.process.stdin.fileno(), value, deadline, True)
                raw = self._read_frame(deadline)
                row = strict_json(raw, 8192)
                if (type(row) is not dict or set(row) != {'schema', 'id', 'action', 'text'} or
                        type(row['schema']) is not int or row['schema'] != 1 or
                        type(row['id']) is not int or row['id'] != self.counter or row['action'] != action):
                    raise Refused('MODEL_UNAVAILABLE')
            except Exception:
                # Timeout/invalid reply poisons the owned process. Never replay the
                # unknown request or keep its delayed response for a future utterance.
                self.close()
                raise Refused('MODEL_UNAVAILABLE') from None
            # An in-sync empty transcript (silence) is a valid answer; other text is bounded.
            if action == 'asr' and row['text'] != '' and not bounded_text(row['text'], 1024, 4096):
                raise Refused('MODEL_SCHEMA')
            if action == 'intent' and (type(row['text']) is not str or len(row['text'].encode()) > 2048):
                raise Refused('MODEL_SCHEMA')
            return row['text']
        finally:
            value = payload = raw = row = None
            self.lock.release()

    def close(self):
        self.sealed = None
        process = self.process
        if process is None:
            return self._cleanup_runtime()
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except Exception:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except Exception:
                pass
        self.cleanup_complete = process.poll() is not None
        # Preserve the owned handle if reaping failed; a detached live child is
        # not an empty/clean state and cannot qualify or be replaced implicitly.
        if self.cleanup_complete:
            self.process = None
        else:
            self.qualified = False
        if self.cleanup_complete:self._cleanup_runtime()
        return self.cleanup_complete

    def _cleanup_runtime(self):
        if self.runtime is not None:
            try:
                self.runtime.cleanup();self.runtime=None;self.cleanup_complete=True
            except Exception:self.cleanup_complete=False;self.qualified=False
        return self.cleanup_complete


class WorkerASR:
    def __init__(self, worker):
        self.worker = worker

    @property
    def qualified(self):
        return self.worker.qualified and self.worker.process is not None and self.worker.sealed is not None

    def transcribe(self, pcm, *, rate, timeout_s):
        if rate != 16000:
            raise Refused('ASR_UNAVAILABLE')
        try:
            return self.worker.call('asr', pcm, timeout_s=timeout_s)
        except Refused:
            raise Refused('ASR_UNAVAILABLE') from None


class WorkerIntent:
    def __init__(self, worker):
        self.worker = worker

    @property
    def prompt_sha256(self):
        return getattr(self.worker, 'prompt_sha256', None)

    def configure_prompt(self, system_prompt, *, phase=None):
        return self.worker.configure_prompt(system_prompt, phase=phase)

    def interpret(self, transcript, *, timeout_s):
        return self.worker.call('intent', transcript, timeout_s=timeout_s)
