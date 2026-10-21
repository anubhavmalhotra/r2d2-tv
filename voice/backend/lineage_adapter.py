"""Source-only adapter. No command-line dispatcher or native permit issuer.

Compose with the reviewed v4 native source closure, then review the parent result
schema and bounded transport before any execution. A diagnostic is not a seal.
"""
if __package__:
    from . import descriptor_lineage as lineage
else:
    import descriptor_lineage as lineage


def operations_class(base):
    class LineageOperations(base):
        def __init__(self, permit):
            super().__init__(permit)
            self.public_lineage = None
            self.lineage_attempted = False
            self.initial_sample = self.bootstrapped_sample = None

        def bootstrap_public_tensor(self):
            if self.lineage_attempted:
                lineage.refuse()
            self.lineage_attempted = True
            scope = (0, 1, 2, self.permit.response_fd)
            before = lineage.capture(scope)
            self.initial_sample = before
            result = super().bootstrap_public_tensor()
            after = lineage.capture(scope)
            self.bootstrapped_sample = after
            self.public_lineage = lineage.compare(before, after)
            return result
    return LineageOperations
