"""Remove only an identity-bound owned public startup-code tree after child exit."""
import os
from pathlib import Path
import stat

def identity(info):return(info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode))

def remove_public_tree(root,expected):
    root=Path(root);info=root.lstat()
    if identity(info)!=expected or not stat.S_ISDIR(info.st_mode)or info.st_uid!=os.getuid()or info.st_mode&0o077:
        raise ValueError('PUBLIC_RUNTIME_CLEANUP')
    rows=[];total=0;pending=[(root,0)]
    while pending:
        folder,depth=pending.pop()
        if depth>8:raise ValueError('PUBLIC_RUNTIME_CLEANUP')
        for path in folder.iterdir():
            info=path.lstat()
            if info.st_uid!=os.getuid()or info.st_nlink!=1 and not stat.S_ISDIR(info.st_mode):raise ValueError('PUBLIC_RUNTIME_CLEANUP')
            if stat.S_ISDIR(info.st_mode):pending.append((path,depth+1));kind='directory'
            elif stat.S_ISREG(info.st_mode):
                kind='file';total+=info.st_size
                if info.st_size>1048576 or total>8388608:raise ValueError('PUBLIC_RUNTIME_CLEANUP')
            else:raise ValueError('PUBLIC_RUNTIME_CLEANUP')
            rows.append((path,identity(info),kind))
            if len(rows)>128:raise ValueError('PUBLIC_RUNTIME_CLEANUP')
    # Validate the entire bounded tree before deleting anything; no symlink or
    # hardlink can lead cleanup into a model snapshot or unrelated user path.
    for path,expected_identity,kind in sorted(rows,key=lambda row:len(row[0].parts),reverse=True):
        if identity(path.lstat())!=expected_identity:raise ValueError('PUBLIC_RUNTIME_CLEANUP')
        if kind=='file':path.unlink()
        else:path.rmdir()
    if identity(root.lstat())!=expected:raise ValueError('PUBLIC_RUNTIME_CLEANUP')
    root.rmdir()
    return {'complete':True,'entriesRemoved':len(rows),'bytesRemoved':total,'payloadContentsRead':False}
