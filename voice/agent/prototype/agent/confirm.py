"""At most one PendingConfirmation. The plan is frozen at validation
and executed byte-for-byte; its digest is re-checked at execution time."""
from dataclasses import dataclass
import threading

from .policy import CONFIRM_TTL_S


@dataclass(frozen=True, repr=False)
class Pending:
    plan: object
    digest: str
    expires: float
    surface: str
    rule: str
    audit_args: dict  # already sanitized (free text as length only)


class ConfirmationSlot:
    def __init__(self, clock):
        self.clock = clock
        self.pending = None
        self.lock = threading.Lock()

    def hold(self, plan, surface, rule, audit_args):
        with self.lock:
            self.pending = Pending(plan, plan.digest(), self.clock() + CONFIRM_TTL_S, surface, rule,
                                   audit_args)
            return self.pending

    def take(self):
        """Remove and return (pending|None, expired: bool). Always empties the slot."""
        with self.lock:
            pending, self.pending = self.pending, None
        if pending is None:
            return None, False
        return pending, self.clock() >= pending.expires

    def occupied(self):
        with self.lock:
            return self.pending is not None
