"""No standalone server. Reads one bounded utterance at a time from owned pipes."""
import json
import os
import resource
import struct
import sys
import time


def exact(fd, length):
    data = bytearray()
    while len(data) < length:
        part = os.read(fd, min(8192, length - len(data)))
        if not part:
            raise ValueError()
        data.extend(part)
    return data


def main():
    if sys.platform == 'linux':
        # If the owned controller itself is killed, do not leave a detached GPU
        # worker alive. Close the race between checking the parent and prctl.
        import ctypes
        import signal
        parent = os.getppid()
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            return 1
    # Quieten all library writes before loading. The private output descriptor is
    # used only for framed protocol replies; no audio/transcript diagnostic logs.
    response_fd = os.dup(1)
    with open(os.devnull, 'wb', buffering=0) as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    if len(sys.argv) != 4:
        return 1
    def reply(row):
        raw = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()
        if len(raw) > 8192:
            raise ValueError()
        framed = struct.pack('>I', len(raw)) + raw
        while framed:
            written = os.write(response_fd, framed)
            if written <= 0:
                raise ValueError()
            framed = framed[written:]

    stage='bootstrap'
    try:
        if __package__:
            from .sealed_startup import bootstrap, current_seal
        else:
            from sealed_startup import bootstrap, current_seal
        context, public_bootstrap = bootstrap(response_fd, sys.argv[3])
        public_bootstrap = None
        if __package__:
            from .ingress import strict_json
            from .providers import NativeQwenASR, NativeQwenIntent
        else:
            from ingress import strict_json
            from providers import NativeQwenASR, NativeQwenIntent

        stage='model-hashes'
        if __package__:
            from .model_assets import verify
        else:
            from model_assets import verify
        context.resources=verify(sys.argv[1],sys.argv[2])
        asr, intent = NativeQwenASR(sys.argv[1]), NativeQwenIntent(sys.argv[2])
        stage='load-asr'
        asr.load()
        stage='load-intent'
        intent.load()
        stage='ready-seal'
        isolation = current_seal(context, response_fd)
        reply({'schema': 2, 'state': 'ready', 'isolation': isolation})
    except BaseException as error:
        if __package__:
            from .startup_diagnostic import load_diagnostic
        else:
            from startup_diagnostic import load_diagnostic
        reply({'schema':2,'state':'refused','stage':stage,'diagnostic':load_diagnostic(error)})
        return 1
    start = time.monotonic()
    for index in range(1, 257):
        pcm = transcript = data = None
        try:
            length = struct.unpack('>I', exact(0, 4))[0]
            if not 1 <= length <= 8192 or time.monotonic() - start >= 1800:
                return 1
            data = strict_json(bytes(exact(0, length)), 8192)
            if (type(data) is not dict or type(data.get('schema')) is not int or
                    data['schema'] != 1 or type(data.get('id')) is not int or data['id'] != index):
                return 1
            if data.get('action') == 'asr' and set(data) == {'schema', 'id', 'action', 'bytes'}:
                count = data['bytes']
                if type(count) is not int or not 640 <= count <= 320000 or count % 2:
                    return 1
                pcm = exact(0, count)
                transcript = asr.transcribe(pcm, rate=16000, timeout_s=8)
            elif data.get('action') == 'intent' and set(data) == {'schema', 'id', 'action', 'text'}:
                transcript = intent.interpret(data['text'], timeout_s=5)
            else:
                return 1
            current_seal(context,response_fd)
            reply({'schema': 1, 'id': index, 'action': data['action'], 'text': transcript})
        except Exception:
            return 1
        finally:
            if pcm is not None:
                pcm[:] = b'\0' * len(pcm)
                pcm.clear()
            pcm = transcript = data = None
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1) from None
