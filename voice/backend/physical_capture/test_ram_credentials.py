"""Actual loader with inert kernel/TLS boundaries; no PEM or native resources."""
import stat
from types import SimpleNamespace
import unittest

from .ram_credentials import Credentials


class Kernel:
    MFD_CLOEXEC = 1
    MFD_ALLOW_SEALING = 2
    SEEK_SET = 0
    F_SEAL_SEAL = 1
    F_SEAL_SHRINK = 2
    F_SEAL_GROW = 4
    F_SEAL_WRITE = 8
    F_ADD_SEALS = 1033
    F_GET_SEALS = 1034

    def __init__(self, failure=None):
        self.failure = failure
        self.rows = {}
        self.next_fd = 20
        self.close_calls = []
        self.buffers = []
        self.events = []

    def uname(self):
        return SimpleNamespace(sysname='Linux')

    def getuid(self):
        return 1234

    def memfd_create(self, name, flags):
        assert name in ('r2d2-voice-cert', 'r2d2-voice-key')
        assert flags == self.MFD_CLOEXEC | self.MFD_ALLOW_SEALING
        if self.failure == 'second-create' and self.rows:
            raise OSError('INERT_ERROR_TEXT')
        self.next_fd += 1
        self.rows[self.next_fd] = dict(data=bytearray(), mode=0o600, seals=0)
        self.events.append('create')
        return self.next_fd

    def fchmod(self, fd, mode):
        self.rows[fd]['mode'] = mode

    def get_inheritable(self, fd):
        return self.failure == 'inheritable'

    def fstat(self, fd):
        row = self.rows[fd]
        return SimpleNamespace(
            st_mode=(stat.S_IFSOCK if self.failure == 'socket' else stat.S_IFREG)
                    | (0o644 if self.failure == 'mode' else row['mode']),
            st_nlink=1 if self.failure == 'named' else 0,
            st_uid=4321 if self.failure == 'uid' else self.getuid(),
            st_gid=1234, st_dev=1, st_ino=fd, st_size=len(row['data']))

    def stat(self, path):
        assert path.startswith('/proc/self/fd/')
        row = self.fstat(int(path.rsplit('/', 1)[1]))
        if self.failure == 'alias':
            row.st_ino += 1
        return row

    def write(self, fd, view):
        self.buffers.append(view.obj)
        if self.failure == 'write':
            raise OSError('INERT_ERROR_TEXT')
        if self.failure == 'zero-write':
            return 0
        if self.failure == 'bool-write':
            return True
        if self.failure == 'oversized-write':
            return len(view) + 1
        n = min(1 if self.failure == 'slow-write' else 7, len(view))
        self.rows[fd]['data'].extend(view[:n])
        return n

    def fcntl(self, fd, command, value=None):
        if command == self.F_ADD_SEALS:
            self.rows[fd]['seals'] = value
            self.events.append('seal')
            return 0
        assert command == self.F_GET_SEALS
        return 0 if self.failure == 'seal' else self.rows[fd]['seals']

    def lseek(self, fd, offset, kind):
        assert (offset, kind) == (0, self.SEEK_SET)
        return 0

    def close(self, fd):
        self.close_calls.append(fd)
        self.events.append('close')
        if self.failure == 'close':
            raise OSError('INERT_ERROR_TEXT')
        self.rows[fd]['data'][:] = b'\0' * len(self.rows[fd]['data'])
        del self.rows[fd]


class TLS:
    PROTOCOL_TLS_SERVER = 1
    CERT_REQUIRED = 2
    OP_NO_COMPRESSION = 4
    TLSVersion = SimpleNamespace(TLSv1_3=13)

    def __init__(self, kernel, failure=None):
        self.kernel = kernel
        self.failure = failure
        self.contexts = []

    def SSLContext(self, kind):
        assert kind == self.PROTOCOL_TLS_SERVER
        outer = self

        class Context:
            options = 0

            def load_cert_chain(self, cert, key):
                assert cert.startswith('/proc/self/fd/')
                assert key.startswith('/proc/self/fd/')
                assert len(outer.kernel.rows) == 2
                assert all(row['seals'] == 15 for row in outer.kernel.rows.values())
                outer.kernel.events.append('load')
                if outer.failure == 'load':
                    raise ValueError('INERT_ERROR_TEXT')
                if outer.failure == 'identity':
                    outer.kernel.rows[int(key.rsplit('/', 1)[1])]['data'].append(1)

            def load_verify_locations(self, *, cadata):
                assert cadata == 'INERT_CA_TEXT'
                if outer.failure == 'ca-load':
                    raise ValueError('INERT_ERROR_TEXT')

        context = Context()
        self.contexts.append(context)
        return context


class RamCredentialsTests(unittest.TestCase):
    def fixture(self, failure=None):
        kernel = Kernel(failure)
        tls = TLS(kernel, failure)
        return Credentials(io=kernel, locks=kernel, tls=tls), kernel, tls

    def load(self, credentials):
        # Deliberately not valid certificates or keys: only the inert TLS
        # adapter accepts these tokens. They authorize no real connection.
        return credentials.context('INERT_CERT_TEXT', 'INERT_KEY_TEXT', 'INERT_CA_TEXT')

    def test_seal_load_and_close_before_return_with_tls13_client_auth(self):
        credentials, kernel, _ = self.fixture()
        context = self.load(credentials)
        self.assertEqual(context.minimum_version, 13)
        self.assertEqual(context.maximum_version, 13)
        self.assertEqual(context.verify_mode, 2)
        self.assertEqual(context.num_tickets, 0)
        self.assertEqual(context.options, 4)
        self.assertEqual(kernel.events, ['create', 'seal', 'create', 'seal', 'load', 'close', 'close'])
        self.assertEqual(kernel.rows, {})
        self.assertTrue(all(not raw for raw in kernel.buffers))
        self.assertEqual(credentials.summary(), dict(
            schema=1, anonymousOnly=True, created=2, sealed=2, closed=2,
            mutableBuffersCleared=True, closeConfirmed=True))

    def test_nonlinux_and_missing_memfd_refuse_before_allocation(self):
        for failure in ('platform', 'api', 'flags'):
            with self.subTest(failure=failure):
                credentials, kernel, _ = self.fixture()
                if failure == 'platform':
                    kernel.uname = lambda: SimpleNamespace(sysname='Other')
                elif failure == 'api':
                    kernel.memfd_create = None
                else:
                    kernel.MFD_ALLOW_SEALING = None
                with self.assertRaisesRegex(ValueError, '^RAM_CREDENTIALS$'):
                    self.load(credentials)
                self.assertEqual(kernel.events, [])
                self.assertTrue(credentials.close())

    def test_partial_allocation_metadata_and_tls_failures_close_owned_fds(self):
        for failure in ('second-create', 'write', 'seal', 'socket', 'named',
                        'mode', 'uid', 'inheritable', 'alias', 'load', 'identity', 'ca-load'):
            with self.subTest(failure=failure):
                credentials, kernel, _ = self.fixture(failure)
                with self.assertRaisesRegex(ValueError, '^RAM_CREDENTIALS$'):
                    self.load(credentials)
                self.assertEqual(kernel.rows, {})
                self.assertEqual(credentials.summary()['created'], credentials.summary()['closed'])
                self.assertTrue(all(not raw for raw in kernel.buffers))
                self.assertTrue(credentials.close())

    def test_write_result_and_iteration_bound_fail_closed(self):
        for failure in ('zero-write', 'bool-write', 'oversized-write', 'slow-write'):
            with self.subTest(failure=failure):
                credentials, kernel, _ = self.fixture(failure)
                with self.assertRaisesRegex(ValueError, '^RAM_CREDENTIALS$'):
                    credentials.context('X' * 65, 'INERT_KEY_TEXT', 'INERT_CA_TEXT')
                self.assertEqual(kernel.rows, {})
                self.assertLessEqual(len(kernel.buffers), 64)
                self.assertTrue(all(not raw for raw in kernel.buffers))

    def test_input_type_ascii_and_size_bounds(self):
        for invalid in ('', 'X' * 8193, '\u00e9', b'BYTES', ['TEXT'], True):
            for position in (0, 1, 2):
                with self.subTest(position=position, type=type(invalid).__name__):
                    credentials, kernel, _ = self.fixture()
                    args = ['INERT_CERT_TEXT', 'INERT_KEY_TEXT', 'INERT_CA_TEXT']
                    args[position] = invalid
                    with self.assertRaisesRegex(ValueError, '^RAM_CREDENTIALS$'):
                        credentials.context(*args)
                    self.assertEqual(kernel.rows, {})
                    self.assertTrue(all(not raw for raw in kernel.buffers))

    def test_close_failure_stays_false_without_retrying_descriptor_number(self):
        credentials, kernel, _ = self.fixture('close')
        with self.assertRaisesRegex(ValueError, '^RAM_CREDENTIALS$'):
            self.load(credentials)
        attempted = list(kernel.close_calls)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(credentials.fds, [])
        self.assertFalse(credentials.close())
        self.assertFalse(credentials.close())
        self.assertEqual(kernel.close_calls, attempted)
        self.assertEqual(credentials.summary()['closed'], 0)
        self.assertFalse(credentials.summary()['closeConfirmed'])
        self.assertTrue(all(not raw for raw in kernel.buffers))

    def test_completed_loader_cannot_be_reused_and_successful_close_is_idempotent(self):
        credentials, kernel, _ = self.fixture()
        self.load(credentials)
        before = list(kernel.events)
        with self.assertRaisesRegex(ValueError, '^RAM_CREDENTIALS$'):
            self.load(credentials)
        self.assertTrue(credentials.close())
        self.assertEqual(kernel.events, before)

    def test_summary_is_detached_and_contains_only_fixed_public_fields(self):
        credentials, _, _ = self.fixture()
        self.load(credentials)
        summary = credentials.summary()
        self.assertEqual(set(summary), {'schema', 'anonymousOnly', 'created', 'sealed',
                                       'closed', 'mutableBuffersCleared', 'closeConfirmed'})
        self.assertTrue(all(type(value) in (int, bool) for value in summary.values()))
        summary['created'] = -1
        self.assertEqual(credentials.summary()['created'], 2)


if __name__ == '__main__':
    unittest.main()
