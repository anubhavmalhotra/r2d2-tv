"""Parent-owned temporary public import code, removed only after worker exit.

No audio, transcript, URL, account or model output is written here. The child
can create import-time public runtime code before its filesystem seal; after
that seal it cannot create or change persistent files.
"""
import os
from pathlib import Path
import re
import stat
import tempfile
if __package__:
    from .runtime_cleanup import identity, remove_public_tree
else:
    from runtime_cleanup import identity,remove_public_tree

class Runtime:
    def __init__(self,attempt):
        if type(attempt)is not str or not re.fullmatch('[a-f0-9]{32}',attempt):raise ValueError('WORKER_RUNTIME')
        self.root=Path(tempfile.mkdtemp(prefix='r2d2-voice-worker-'))
        os.chmod(self.root,0o700)
        self.root_identity=identity(self.root.lstat())
        self.claim=self.root/'attempt.json'
        fd=os.open(self.claim,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:os.write(fd,b'{"kind":"public-startup-denial-probe"}\n')
        finally:os.close(fd)
        self.claim_identity=identity(self.claim.lstat())
        self.temp=self.root/'startup-public-runtime';self.temp.mkdir(mode=0o700)
        self.temp_identity=identity(self.temp.lstat());self.temp_removed=False;self.complete=False

    def cleanup(self):
        if self.complete:return True
        root=self.root.lstat()
        if identity(root)!=self.root_identity or root.st_uid!=os.getuid()or stat.S_IMODE(root.st_mode)!=0o700:
            raise ValueError('WORKER_RUNTIME_CLEANUP')
        if not self.temp_removed:
            remove_public_tree(self.temp,self.temp_identity);self.temp_removed=True
        if set(p.name for p in self.root.iterdir())!={'attempt.json'}or identity(self.claim.lstat())!=self.claim_identity:
            raise ValueError('WORKER_RUNTIME_CLEANUP')
        self.claim.unlink();self.root.rmdir();self.complete=True
        return True
