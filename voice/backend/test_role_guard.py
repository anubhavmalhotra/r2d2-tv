import copy
import ctypes
import os
import stat
import types
import unittest

import descriptor_lineage as lineage
from gpu_bootstrap_readback import InventoryRefused
from lineage_fixtures import Context, info, threads
from role_guard import RoleGuard, roles, validate_observation
import socket_scope
import gpu_bootstrap_tsync


def fixture():
    initial = Context(scope=(0, 1, 2, 44))
    later = Context(scope=initial.scope, extra={
        5: info(stat.S_IFSOCK, inode=501), 6: info(stat.S_IFIFO, inode=601),
        7: info(stat.S_IFIFO, inode=601), 8: info(0, inode=801),
        9: info(stat.S_IFCHR, inode=901, rdev=os.makedev(195, 0))})
    later.flags[6] = (os.O_RDONLY, 1); later.flags[7] = (os.O_WRONLY, 1)
    def sample(ctx, ids):
        return lineage.capture(ctx.scope, inventory=lambda *a, **k: ctx.snapshot(read_domains=True, socket_domain=lambda _: 1),
             tasks=lambda: threads(ids=ids), readlink=lambda _: 'anon_inode:[eventfd]',
             socket_properties=lambda _: {'family': 1, 'type': 5, 'accepting': 1})
    ops = types.SimpleNamespace(initial_sample=sample(initial, (10,)),
          bootstrapped_sample=sample(later, (10, 11)),
          socket_proofs={5: {'notConnected': True, 'peerErrno': 107, 'localAddressClass': 'abstract'}},
          role_observation=None)
    return initial, later, ops


def guard(ops):
    value = RoleGuard(ops)
    value.bootstrap_complete(torch_version_matches=True, cuda_version_matches=True,
                             driver_init_code=0, tensor_matched=True)
    value.capture_inventory(ops.bootstrapped_sample.fds, ops.bootstrapped_sample.tasks)
    return value


class RoleTests(unittest.TestCase):
    def test_known_public_roles_allow_only_public_seal_and_private_gate_stays_closed(self):
        _, ctx, ops = fixture(); value = guard(ops)
        value.seal_complete(tsync_return=0, filter_sha256_matches=True)
        value.after_inventory(ctx.snapshot(read_domains=False), threads(2))
        value.fixed_tensor_completed(matched=True, network_denials_verified=True,
              swap_bytes=0, locked_bytes=4096, fds=ctx.snapshot(read_domains=False), threads=threads(2))
        self.assertEqual(value.stage, 'functional-diagnostic-complete')
        self.assertFalse(value.private_input_allowed())
        self.assertFalse(ops.role_observation['privateInputQualified'])

    def test_unknown_socket_role_connected_peer_extra_proof_or_unpaired_pipe_refuse(self):
        for mutate in (lambda o: o.socket_proofs[5].update(notConnected=False),
                       lambda o: o.socket_proofs[5].update(peerErrno=0),
                       lambda o: o.socket_proofs.update({12: o.socket_proofs[5]}),
                       lambda o: o.socket_proofs[5].update(rawEndpoint='PRIVATE')):
            _, _, ops = fixture(); mutate(ops)
            with self.assertRaises(InventoryRefused): guard(ops)
        initial, later, ops = fixture()
        later.flags[6] = (os.O_WRONLY, 1)
        old = ops.bootstrapped_sample
        changed = []
        for fp, kind, props in old.details:
            if fp[0] == 6: fp = (*fp[:5], os.O_WRONLY, fp[6])
            changed.append((fp, kind, props))
        ops.bootstrapped_sample = lineage.Sample(later.snapshot(read_domains=True, socket_domain=lambda _: 1), old.tasks, changed)
        with self.assertRaises(InventoryRefused): guard(ops)

    def test_socket_shape_eventfd_tag_and_preexisting_nonstdio_never_generalized(self):
        for bad_kind, bad_props in [('unknown', None), ('eventpoll', None), ('socket', (1, 1, 0)), ('socket', (2, 5, 1))]:
            _, _, ops = fixture(); old = ops.bootstrapped_sample
            values = [(fp, bad_kind, bad_props) if fp[0] == 5 else (fp, kind, prop) for fp, kind, prop in old.details]
            ops.bootstrapped_sample = lineage.Sample(old.fds, old.tasks, values)
            with self.assertRaises(InventoryRefused): guard(ops)
        _, _, ops = fixture(); ops.initial_sample = ops.bootstrapped_sample
        with self.assertRaises(InventoryRefused): guard(ops)

    def test_postseal_descriptor_replacement_or_new_thread_refuses(self):
        for mutation in ('inode', 'thread'):
            _, ctx, ops = fixture(); value = guard(ops)
            value.seal_complete(tsync_return=0, filter_sha256_matches=True)
            if mutation == 'inode': ctx.rows[5].st_ino += 1
            with self.assertRaises(InventoryRefused):
                value.after_inventory(ctx.snapshot(read_domains=False), threads(2, ids=(10, 11, 12) if mutation == 'thread' else (10, 11)))

    def test_unchanged_policy_denies_all_listener_and_external_fd_escape_calls(self):
        self.assertEqual(gpu_bootstrap_tsync.policy_hash(), '9f38390e5ea6847aaa3ba3ff2263d32710c186d700b9f271d8cc287f9de436c4')
        for nr in (198, 199, 200, 201, 202, 203, 206, 207, 208, 210, 211, 212, 242, 243, 269, 425, 426, 427, 438):
            self.assertEqual(gpu_bootstrap_tsync.TARGET_DENIED[nr], 1)

    def test_role_projection_rejects_private_or_inflated_proof(self):
        _, _, ops = fixture(); guard(ops); value = validate_observation(ops.role_observation)
        for key, replacement in [('privateInputQualified', True), ('listenerCount', True), ('rawEndpoint', 'PRIVATE')]:
            row = copy.deepcopy(value); row[key] = replacement
            with self.assertRaises(ValueError): validate_observation(row)


class SocketTests(unittest.TestCase):
    def test_address_parser_never_returns_endpoint_and_rejects_wrong_family(self):
        for raw, expected in [(b'\1\0', 'unnamed'), (b'\1\0\0PRIVATE', 'abstract'), (b'\1\0/PRIVATE', 'pathname')]:
            self.assertEqual(socket_scope.classify_address(raw, len(raw)), expected)
        for raw in (b'', b'\2\0', b'\1\0' + b'x' * 109):
            with self.assertRaises(ValueError): socket_scope.classify_address(raw, len(raw))

    def test_getpeername_requires_enotconn_and_does_not_read_payload(self):
        class Function:
            def __init__(self, fn): self.fn = fn
            def __call__(self, *args): return self.fn(*args)
        def name(fd, buffer, length):
            raw = b'\1\0\0PRIVATE'; ctypes.memmove(buffer, raw, len(raw))
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint)).contents.value = len(raw); return 0
        for connected in (False, True):
            def peer(*_): ctypes.set_errno(0 if connected else 107); return 0 if connected else -1
            library = types.SimpleNamespace(getpeername=Function(peer), getsockname=Function(name))
            if connected:
                with self.assertRaises(ValueError): socket_scope.query(5, library=library)
            else:
                self.assertEqual(socket_scope.query(5, library=library),
                    {'notConnected': True, 'peerErrno': 107, 'localAddressClass': 'abstract'})


if __name__ == '__main__': unittest.main()
