/* Fixed ARM syscall policy. No runtime allowlist update or detach operation. */
#ifndef OWNER_SUPERVISED_POLICY_H
#define OWNER_SUPERVISED_POLICY_H
#include <errno.h>
#include <stdint.h>
#ifndef __NR_clone3
#define __NR_clone3 435
#endif
#ifndef __NR_execveat
#define __NR_execveat 387
#endif
/* Pure decision function; every task, including the initial tracee, uses it. */
static int owner_denial(unsigned long nr,unsigned long first,int first_exec) {
 if(nr==__NR_socket && first!=AF_UNIX && first!=16 /* AF_NETLINK */)return EACCES;
 if(nr==__NR_socketpair && first!=AF_UNIX)return EACCES;
 if(nr==__NR_clone && (first&CLONE_UNTRACED))return EPERM;
 if(nr==__NR_clone3 || nr==__NR_execveat)return EPERM;
 if(nr==__NR_execve && !first_exec)return EPERM;
 /* The provider has no legitimate reason to become a tracer or change its
  * namespace. Deny these even though EXITKILL covers supervisor death. */
 if(nr==__NR_ptrace || nr==__NR_unshare || nr==__NR_setns)return EPERM;
 /* ARM EABI numbers; deny alternate socket/FD acquisition paths. recvmsg is
  * separately substituted with readv by the tracer (no ancillary facility). */
 if(nr==102 /* socketcall */ || nr==365 /* recvmmsg */ ||
    nr==425 /* io_uring_setup */ || nr==438 /* pidfd_getfd */ ||
    nr==386 /* bpf */)return EPERM;
 return 0;
}
#endif
