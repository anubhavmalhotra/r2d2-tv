"""Offline prepared PTT boundary, NOT a TV microphone or transport driver.

Trusted host owns the controller lease and supplies closures. Wire messages,
ASR/model output and page content cannot construct that lease. Input is already
normalized 16 kHz mono signed little-endian PCM; the LG socket format is unproved.
No sockets, files, logging, model loading or device calls exist in this module.
"""
import math
import re
import threading
import time

RATE = 16000
MAX_SECONDS = 20
MAX_BYTES = RATE * 2 * MAX_SECONDS
MAX_CHUNK = 8192


class Ingress:
    """One owner-authorized utterance; construct a new instance for the next PTT.

    transcribe(readonly_pcm_view, RATE, monotonic_deadline) must be a qualified
    local, no-retention ASR adapter. This class cannot forcibly stop a model call;
    late results are discarded. The capture owner must run its release/watchdog
    independently and call cancel on disconnect/suspend. on_transcript closes
    over the controller's *actual* lease and executes it once. Its return value
    is discarded so arbitrary model/controller data cannot leak into status.
    """
    def __init__(self, request_id, expires_at, transcribe, on_transcript,
                 cancel_authority, clock=time.monotonic):
        now = clock()
        if (type(request_id) is not str or not re.fullmatch(r'[a-f0-9]{32}', request_id)
                or type(expires_at) not in (int, float) or not math.isfinite(expires_at)
                or not now < expires_at <= now + 30
                or not all(callable(x) for x in (transcribe, on_transcript, cancel_authority))):
            raise ValueError('OWNER_BINDING')
        self._clock, self._deadline = clock, expires_at
        self._capture_deadline = min(expires_at, now + MAX_SECONDS)
        self._asr, self._deliver, self._cancel_authority = transcribe, on_transcript, cancel_authority
        self._lock = threading.RLock()
        self._state, self._reason = 'capturing', None
        self._audio = bytearray()
        self._processing = None
        self._sequence = self._samples = 0
        self._authority_cancelled = False

    @staticmethod
    def _wipe(buf):
        if buf is not None:
            buf[:] = b'\0' * len(buf)

    def _cancel_lease(self):
        if not self._authority_cancelled:
            self._authority_cancelled = True
            try:
                self._cancel_authority()
            except Exception:
                self._reason = 'AUTHORITY_CANCEL_FAILED'

    def _stop(self, state, reason):
        self._state, self._reason = state, reason
        self._wipe(self._audio)
        self._audio = bytearray()
        self._wipe(self._processing)
        self._cancel_lease()

    def feed(self, sequence, pcm):
        with self._lock:
            if self._state != 'capturing':
                return False
            if self._clock() >= self._capture_deadline:
                self._stop('expired', 'CAPTURE_DEADLINE')
                return False
            if (type(sequence) is not int or sequence != self._sequence
                    or type(pcm) not in (bytes, bytearray) or not 0 < len(pcm) <= MAX_CHUNK
                    or len(pcm) % 2 or len(self._audio) + len(pcm) > MAX_BYTES):
                self._stop('rejected', 'AUDIO_SHAPE_OR_ORDER')
                return False
            self._audio.extend(pcm)
            self._sequence += 1
            self._samples += len(pcm) // 2
            return True

    def finish(self):
        with self._lock:
            if self._state != 'capturing':
                return self.snapshot()
            if self._clock() >= self._capture_deadline or not self._audio:
                self._stop('expired' if self._audio else 'rejected', 'EMPTY_OR_EXPIRED')
                return self.snapshot()
            self._state = 'transcribing'
            audio, self._audio = self._audio, bytearray()
            self._processing = audio
        transcript = None
        try:
            transcript = self._asr(memoryview(audio).toreadonly(), RATE, self._deadline)
        except Exception:
            with self._lock:
                if self._state == 'transcribing':
                    self._stop('failed', 'ASR_FAILED')
        finally:
            self._wipe(audio)
            with self._lock:
                self._processing = None
        with self._lock:
            if self._state != 'transcribing':
                return self.snapshot()
            if self._clock() >= self._deadline:
                self._stop('expired', 'RESULT_DEADLINE')
                return self.snapshot()
            try:
                valid = (type(transcript) is str and 0 < len(transcript) <= 1024
                         and len(transcript.encode('utf-8')) <= 4096
                         and bool(transcript.strip())
                         and all(ord(c) >= 32 or c in '\t\n' for c in transcript))
            except UnicodeError:
                valid = False
            if not valid:
                self._stop('rejected', 'TRANSCRIPT_SHAPE')
                return self.snapshot()
            # Consume before external callback. An uncertain callback is never retried.
            self._state = 'delivering'
            try:
                self._deliver(transcript)
                self._state, self._reason = 'delivered', None
            except Exception:
                self._stop('unknown', 'DELIVERY_UNCERTAIN')
            finally:
                transcript = None
            return self.snapshot()

    def cancel(self):
        with self._lock:
            if self._state in ('capturing', 'transcribing'):
                self._stop('cancelled', 'OWNER_CANCELLED')
            return self.snapshot()

    def tick(self):
        with self._lock:
            limit = self._capture_deadline if self._state == 'capturing' else self._deadline
            if self._state in ('capturing', 'transcribing') and self._clock() >= limit:
                self._stop('expired', 'OWNER_DEADLINE')
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {'state': self._state, 'reason': self._reason,
                    'chunks': self._sequence, 'samples': self._samples}
