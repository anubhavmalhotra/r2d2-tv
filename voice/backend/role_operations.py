"""Role metadata composition for the fixed public tensor only."""
if __package__:
    from .lineage_adapter import operations_class
    from .diagnostic_inventory import DiagnosticOperations, reason
    from .role_guard import RoleGuard
    from .child_probe import execute
    from .gpu_bootstrap_readback import InventoryRefused
    from . import socket_scope
    from . import filesystem_seal
    from . import runtime_preload
else:
    from lineage_adapter import operations_class
    from diagnostic_inventory import DiagnosticOperations, reason
    from role_guard import RoleGuard
    from child_probe import execute
    from gpu_bootstrap_readback import InventoryRefused
    import socket_scope
    import filesystem_seal
    import runtime_preload
from pathlib import Path
import os


class RoleOperations(operations_class(DiagnosticOperations)):
    def __init__(self, permit):
        super().__init__(permit)
        self.socket_proofs = {}
        self.role_observation = None
        self.filesystem_observation = None
        self.runtime_preparation = None
        self.denial_probe=Path(os.environ['TMPDIR']).parent/'attempt.json'

    def configure_owned_process(self):
        if super().configure_owned_process() is not True: return False
        self.runtime_preparation = runtime_preload.new()
        runtime_preload.prepare(self.runtime_preparation)
        self.filesystem_observation = filesystem_seal.install()
        filesystem_seal.check_denials(self.denial_probe, self.filesystem_observation)
        return True

    def bootstrap_public_tensor(self):
        result = super().bootstrap_public_tensor()
        for fp, kind, properties in self.bootstrapped_sample.details:
            if kind == 'socket':
                self.socket_proofs[fp[0]] = socket_scope.query(fp[0])
        return result


def run(ops, attempt):
    class ReportingGuard(RoleGuard):
        def capture_inventory(self, fds, threads):
            ops.inventory_observation['stage'] = 'guard'
            try:
                return super().capture_inventory(fds, threads)
            except InventoryRefused as error:
                reason(ops.inventory_observation, error)
                raise
    return execute(ops, attempt, guard_factory=lambda: ReportingGuard(ops))
