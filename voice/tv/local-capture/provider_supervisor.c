#define _GNU_SOURCE 1
/* Finite, owned-process ARM tracer. Never attaches an existing process. Parent
 * passes already hash-verified held executable/module FDs 7/8. Child uses only
 * those bytes and an explicit environment. No audio or LS2 payload is read. */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <sched.h>
#include <dirent.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <asm/ptrace.h>
#include "supervised_policy.h"
#include "owner_retirement.h"
#ifndef PTRACE_O_EXITKILL
#define PTRACE_O_EXITKILL 0x00100000
#endif
#define MAX_TASKS 64
#define EXE_FD 7
#define MODULE_FD 8
#define REPORT_FD 6
struct arm_regs {unsigned long r[18];};
struct task {pid_t pid;int alive,pending,options;unsigned long message,control,receive_flags;};
static struct task tasks[MAX_TASKS];
static unsigned tasks_n=0,sockets_denied=0,escapes_denied=0,returns_replaced=0,recvmsg_replaced=0;
static int first_exec=0,image_verified=0,fds_verified=0,all_reaped=0,error_no=0,expired=0;
static int root_exit=-1,root_signal=0;
static const char *stage="preflight";
static volatile sig_atomic_t owner_lost=0;
static void owner_died(int signal_number){(void)signal_number;owner_lost=1;}
static struct stat exe_stat,module_stat;
static uint64_t millis(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return 0;return (uint64_t)t.tv_sec*1000+t.tv_nsec/1000000;}
static int same(const struct stat *a,const struct stat *b){return a->st_dev==b->st_dev&&a->st_ino==b->st_ino&&a->st_size==b->st_size&&a->st_mtime==b->st_mtime&&a->st_ctime==b->st_ctime;}
static int stable_file(int fd,struct stat *st){int flags=fcntl(fd,F_GETFL);return flags>=0&&(flags&O_ACCMODE)==O_RDONLY&&fstat(fd,st)==0&&S_ISREG(st->st_mode)&&st->st_uid==0&&!(st->st_mode&06022)&&st->st_size>0&&st->st_size<=1048576;}
static int close_extra_fds(void){
 DIR*d=opendir("/proc/self/fd");if(!d)return 0;
 int extras[1024];unsigned used=0;int okay=1;struct dirent*e;
 while((e=readdir(d))){
  if(!strcmp(e->d_name,".")||!strcmp(e->d_name,".."))continue;
  char *end;errno=0;long fd=strtol(e->d_name,&end,10);
  if(errno||*end||fd<0||fd>1048576){okay=0;break;}
  if(fd>8&&fd!=dirfd(d)){if(used>=1024){okay=0;break;}extras[used++]=(int)fd;}
 }
 if(closedir(d))okay=0;
 if(!okay)return 0;
 for(unsigned i=0;i<used;++i){
  if(close(extras[i])<0){if(fcntl(extras[i],F_GETFD)!=-1||errno!=EBADF)return 0;}
 }
 return 1;
}
static int finite_fds(pid_t owner){
 struct stat st; if(!stable_file(EXE_FD,&exe_stat)||!stable_file(MODULE_FD,&module_stat))return 0;
 for(int fd=0;fd<2;++fd)if(fstat(fd,&st)||!S_ISCHR(st.st_mode))return 0;
 if(fstat(2,&st)||!(S_ISFIFO(st.st_mode)||S_ISSOCK(st.st_mode)))return 0;
 for(int fd=3;fd<=6;++fd){int type=0;struct ucred peer;socklen_t n=sizeof(type),p=sizeof(peer);
  if(getsockopt(fd,SOL_SOCKET,SO_TYPE,&type,&n)||type!=SOCK_STREAM||getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&p)||peer.pid!=owner||peer.uid!=0)return 0;}
 DIR*d=opendir("/proc/self/fd");if(!d)return 0;int okay=1;struct dirent*e;
 while((e=readdir(d))){char *end;long fd=strtol(e->d_name,&end,10);if(*end)continue;if(fd<0||(fd>8&&fd!=dirfd(d)))okay=0;}
 closedir(d);return okay;
}
static struct task *lookup(pid_t pid){for(unsigned i=0;i<tasks_n;++i)if(tasks[i].pid==pid&&tasks[i].alive)return &tasks[i];return NULL;}
static struct task *add(pid_t pid){struct task*t=lookup(pid);if(t)return t;if(pid<=0||tasks_n>=MAX_TASKS)return NULL;t=&tasks[tasks_n++];t->pid=pid;t->alive=1;return t;}
static int options(struct task*t){if(ptrace(PTRACE_SETOPTIONS,t->pid,0,(void *)(long)(PTRACE_O_TRACESYSGOOD|PTRACE_O_TRACEFORK|PTRACE_O_TRACEVFORK|PTRACE_O_TRACECLONE|PTRACE_O_TRACEEXEC|PTRACE_O_EXITKILL)))return 0;t->options=1;return 1;}
static int resume(pid_t pid,int sig){return ptrace(PTRACE_SYSCALL,pid,0,(void *)(long)sig)==0;}
static int fixed_path(pid_t pid,unsigned long address){
 const char expected[]="/proc/self/fd/7";
 for(size_t i=0;i<sizeof(expected);i+=sizeof(long)){errno=0;long value=ptrace(PTRACE_PEEKDATA,pid,(void*)(address+i),0);if(value==-1&&errno)return 0;size_t n=sizeof(expected)-i;if(n>sizeof(long))n=sizeof(long);if(memcmp(&value,expected+i,n))return 0;}
 return 1;
}
static int nonblocking_fd(pid_t pid,unsigned long fd){
 if(fd>1048576)return 0;
 char path[80],line[128];snprintf(path,sizeof(path),"/proc/%ld/fdinfo/%lu",(long)pid,fd);
 FILE*f=fopen(path,"r");if(!f)return 0;unsigned long flags=0;int found=0;
 while(fgets(line,sizeof(line),f))if(sscanf(line,"flags:%lo",&flags)==1){found=1;break;}
 fclose(f);return found&&(flags&O_NONBLOCK);
}
static int socket_fd(pid_t pid,unsigned long fd){
 if(fd<3||fd>1024)return 0;
 char path[64];struct stat st;snprintf(path,sizeof(path),"/proc/%ld/fd/%lu",(long)pid,fd);
 return stat(path,&st)==0&&S_ISSOCK(st.st_mode);
}
static int receive_as_readv(struct task*t,struct arm_regs*r){
 unsigned long address=r->r[1],flags=r->r[2];
 if(!address||address>UINT32_MAX-28||(flags&~(MSG_DONTWAIT|MSG_CMSG_CLOEXEC)))return 0;
 uint32_t header[7];
 for(unsigned i=0;i<7;++i){errno=0;long v=ptrace(PTRACE_PEEKDATA,t->pid,(void*)(address+4*i),0);if(v==-1&&errno)return -1;header[i]=(uint32_t)v;}
 if(header[0]||!header[2]||header[3]<1||header[3]>64)return 0;
 if((flags&MSG_DONTWAIT)&&!nonblocking_fd(t->pid,r->r[0]))return 0;
 if(!header[1]&&header[3]==1&&header[4]&&header[4]<=UINT32_MAX-16&&header[5]==16&&header[2]<=UINT32_MAX-8&&socket_fd(t->pid,r->r[0])){
  uint32_t iov[2];
  for(unsigned i=0;i<2;++i){errno=0;long v=ptrace(PTRACE_PEEKDATA,t->pid,(void*)((unsigned long)header[2]+4*i),0);if(v==-1&&errno)return -1;iov[i]=(uint32_t)v;}
  if(iov[0]&&iov[1]==1){
   t->message=address;t->control=header[4];t->receive_flags=flags;r->r[2]=flags|MSG_CMSG_CLOEXEC;
   if(ptrace(PTRACE_SETREGS,t->pid,0,r))return -1;
   ++recvmsg_replaced;return 1;
  }
 }
 if(header[4]||header[5])return 0;
 t->message=address;t->control=0;t->receive_flags=flags;r->r[1]=header[2];r->r[2]=header[3];r->r[7]=__NR_readv;
 if(ptrace(PTRACE_SETREGS,t->pid,0,r)||ptrace(PTRACE_SET_SYSCALL,t->pid,0,(void*)(long)__NR_readv))return -1;
 ++recvmsg_replaced;return 1;
}
static int syscall_stop(struct task*t,pid_t root){
 struct arm_regs r;if(!t->options||ptrace(PTRACE_GETREGS,t->pid,0,&r))return 0;
 if(r.r[12]==0){
  if(t->pending){errno=EPROTO;return 0;}
  unsigned long nr=r.r[7];int initial=nr==__NR_execve&&t->pid==root&&!first_exec;
  int denial=owner_denial(nr,r.r[0],initial);
  if(nr==__NR_recvmsg){
   int replaced=receive_as_readv(t,&r);if(replaced<0)return 0;if(!replaced)denial=EACCES;
  }
  if(initial){if(!fixed_path(t->pid,r.r[0])){errno=EPROTO;return 0;}first_exec=1;}
  if(denial){if(ptrace(PTRACE_SET_SYSCALL,t->pid,0,(void*)-1L))return 0;t->pending=denial;if(denial==EACCES)++sockets_denied;else ++escapes_denied;}
 }else if(r.r[12]==1){
  if(t->message&&t->control){
   unsigned long msg=t->message,ctrl=t->control,orig_flags=t->receive_flags;
   t->message=0;t->control=0;t->receive_flags=0;r.r[2]=orig_flags;
   if(ptrace(PTRACE_SETREGS,t->pid,0,&r))return 0;
   if((long)r.r[0]>0){
    errno=0;long clen=ptrace(PTRACE_PEEKDATA,t->pid,(void*)(msg+20),0);if(clen==-1&&errno)return 0;
    errno=0;long mflags=ptrace(PTRACE_PEEKDATA,t->pid,(void*)(msg+24),0);if(mflags==-1&&errno)return 0;
    if((uint32_t)mflags&MSG_CTRUNC){errno=EPROTO;return 0;}
    if(!(orig_flags&MSG_CMSG_CLOEXEC)&&((uint32_t)mflags&MSG_CMSG_CLOEXEC)){
     if(ptrace(PTRACE_POKEDATA,t->pid,(void*)(msg+24),(void*)(unsigned long)((uint32_t)mflags&~(uint32_t)MSG_CMSG_CLOEXEC)))return 0;
    }
    if((uint32_t)clen==16){
     uint32_t cmsg[4];
     for(unsigned i=0;i<4;++i){errno=0;long v=ptrace(PTRACE_PEEKDATA,t->pid,(void*)(ctrl+4*i),0);if(v==-1&&errno)return 0;cmsg[i]=(uint32_t)v;}
     if(cmsg[0]!=16||cmsg[1]!=SOL_SOCKET||cmsg[2]!=SCM_RIGHTS||!socket_fd(t->pid,cmsg[3])){errno=EPROTO;return 0;}
    }else if((uint32_t)clen!=0){errno=EPROTO;return 0;}
   }
  }else if(t->message){
   if(ptrace(PTRACE_POKEDATA,t->pid,(void*)(t->message+20),0)||ptrace(PTRACE_POKEDATA,t->pid,(void*)(t->message+24),0))return 0;
   r.r[1]=t->message;r.r[2]=t->receive_flags;r.r[7]=__NR_recvmsg;t->message=0;t->receive_flags=0;
   if(ptrace(PTRACE_SETREGS,t->pid,0,&r))return 0;
  }
  if(t->pending){r.r[0]=(unsigned long)-(long)t->pending;if(ptrace(PTRACE_SETREGS,t->pid,0,&r))return 0;t->pending=0;++returns_replaced;}
 }else{errno=EPROTO;return 0;}
 return 1;
}
static int verify_exec(pid_t pid){
 char path[64];struct stat a,b,c;snprintf(path,sizeof(path),"/proc/%ld/exe",(long)pid);
 return stat(path,&a)==0&&fstat(EXE_FD,&b)==0&&fstat(MODULE_FD,&c)==0&&same(&a,&exe_stat)&&same(&b,&exe_stat)&&same(&c,&module_stat);
}
static void kill_owned(void){for(unsigned i=0;i<tasks_n;++i)if(tasks[i].alive)(void)kill(tasks[i].pid,SIGKILL);}
static int scalar(char value){return write(REPORT_FD,&value,1)==1;}
static void wake_deadline(int signal_number){(void)signal_number;}
static void child(pid_t parent,pid_t owner){
 if(prctl(PR_SET_PDEATHSIG,SIGKILL)||getppid()!=parent||prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0))_exit(70);
 close(REPORT_FD);
 if(ptrace(PTRACE_TRACEME,0,0,0))_exit(71);
 raise(SIGSTOP);
 char owner_env[64];snprintf(owner_env,sizeof(owner_env),"OWNER_VOICE_PARENT_PID=%ld",(long)owner);
 char *env[]={"PATH=/usr/bin:/bin","LANG=C","LC_ALL=C","LD_PRELOAD=/proc/self/fd/8","OWNER_VOICE_DATA_FD=3","OWNER_VOICE_CONTROL_FD=4","OWNER_VOICE_STATUS_FD=5","OWNER_VOICE_TRACED=1",owner_env,NULL};
 char *args[]={"/usr/sbin/voiceinput_hidraw",NULL};
 execve("/proc/self/fd/7",args,env);_exit(72);
}
int main(int argc,char **argv){
 if(argc!=3||strcmp(argv[1],"--owned")||getuid()!=0||geteuid()!=0||sizeof(unsigned long)!=4)return 64;
 char*end;long budget=strtol(argv[2],&end,10);if(*end||budget<100||budget>12000)return 64;
 struct sigaction owner_action;memset(&owner_action,0,sizeof(owner_action));owner_action.sa_handler=owner_died;
 sigset_t owner_signal;if(sigemptyset(&owner_signal)||sigaddset(&owner_signal,SIGUSR1)||
  sigprocmask(SIG_UNBLOCK,&owner_signal,NULL)||sigemptyset(&owner_action.sa_mask)||sigaction(SIGUSR1,&owner_action,NULL))return 65;
 /* The supervisor must outlive a dead Node owner long enough for the owned
  * provider's native STOP. Its tracee still dies if this tracer itself dies. */
 pid_t owner=getppid();if(owner<=1||prctl(PR_SET_PDEATHSIG,SIGUSR1)||getppid()!=owner||prctl(PR_SET_DUMPABLE,0))return 65;
 if(!close_extra_fds()||!finite_fds(owner))return 66;
 fds_verified=1;
 int flags=fcntl(REPORT_FD,F_GETFL),set_result;if(flags<0)return 67;
 do{set_result=fcntl(REPORT_FD,F_SETFL,flags|O_NONBLOCK);}while(set_result<0&&errno==EINTR);if(set_result<0)return 67;
 /* Node normally sets close-on-exec on passed descriptors; these two must be
  * present for the loader's immutable held-fd paths. They are not sockets. */
 if(fcntl(EXE_FD,F_SETFD,0)||fcntl(MODULE_FD,F_SETFD,0))return 68;
 uint64_t start=millis();if(!start)return 69;uint64_t deadline=start+(uint64_t)budget;
 pid_t parent=getpid(),pid=fork();if(pid<0)return 69;if(pid==0)child(parent,owner);
 for(int fd=0;fd<=5;++fd)close(fd);
 add(pid);int okay=1;struct owner_retirement retirement={0,0};int owner_grace_expired=0;
 struct sigaction wake;memset(&wake,0,sizeof(wake));wake.sa_handler=wake_deadline;
 struct itimerval timer={{0,10000},{0,10000}};
 if(sigaction(SIGALRM,&wake,NULL)||setitimer(ITIMER_REAL,&timer,NULL)){error_no=errno;okay=0;}
 while(okay){
  uint64_t now=millis();unsigned retirement_action=owner_retirement_step(&retirement,now,owner_lost);
  if(retirement_action&OWNER_REQUEST_STOP){stage="owner-retirement";if(kill(pid,SIGTERM)&&errno!=ESRCH){error_no=errno;okay=0;break;}}
  if(retirement_action&OWNER_GRACE_EXPIRED){owner_grace_expired=1;okay=0;break;}
  if(!now||now<start||(!retirement.requested&&now>=deadline)){expired=1;okay=0;break;}
  /* A 10 ms alarm interrupts a genuinely idle wait; syscall-heavy startup
   * does not incur a millisecond sleep on every entry/exit. */
  int st=0;pid_t got=waitpid(-1,&st,__WALL);
  if(got<0){if(errno==EINTR)continue;if(errno==ECHILD){all_reaped=1;break;}error_no=errno;okay=0;break;}
  if(!got){error_no=EPROTO;okay=0;break;}
  /* A traced child's first stop may be delivered before its parent's fork
   * event. waitpid(__WALL) can only return this supervisor's owned children. */
  struct task*t=add(got);if(!t){error_no=E2BIG;okay=0;break;}
  if(WIFEXITED(st)||WIFSIGNALED(st)){t->alive=0;if(got==pid){root_exit=WIFEXITED(st)?WEXITSTATUS(st):-1;root_signal=WIFSIGNALED(st)?WTERMSIG(st):0;}if(WIFSIGNALED(st)||(WIFEXITED(st)&&WEXITSTATUS(st))){okay=0;break;}continue;}
  if(!WIFSTOPPED(st)){error_no=EPROTO;okay=0;break;}
  int sig=WSTOPSIG(st);unsigned event=(unsigned)st>>16;
  if(event==PTRACE_EVENT_FORK||event==PTRACE_EVENT_VFORK||event==PTRACE_EVENT_CLONE){
   unsigned long born=0;stage="descendant";if(ptrace(PTRACE_GETEVENTMSG,got,0,&born)||!add((pid_t)born)||!resume(got,0)){error_no=errno?errno:E2BIG;okay=0;break;}
  }else if(event==PTRACE_EVENT_EXEC){
   stage="exec-identity";if(got!=pid||image_verified||!first_exec||!verify_exec(got)||!scalar('X')){error_no=errno?errno:EPROTO;okay=0;break;}
   image_verified=1;t->pending=0;if(!resume(got,0)){error_no=errno;okay=0;break;}
  }else if(sig==(SIGTRAP|0x80)){
   stage="syscall";if(!syscall_stop(t,pid)||!resume(got,0)){error_no=errno;okay=0;break;}
  }else if(sig==SIGSTOP&&!t->options){
   stage="options";if(!options(t)||(got==pid&&!scalar('I'))||!resume(got,0)){error_no=errno;okay=0;break;}
  }else if(!event&&(sig==SIGCHLD||sig==SIGTERM||sig==SIGINT||sig==SIGHUP||sig==SIGPIPE||sig==SIGALRM||sig==SIGUSR1||sig==SIGUSR2)){
   if(!resume(got,sig)){error_no=errno;okay=0;break;}
  }else{error_no=EPROTO;okay=0;break;}
 }
 if(!all_reaped){kill_owned();uint64_t cleanup=millis();
  for(unsigned n=0;n<750;++n){uint64_t now=millis();if(!cleanup||!now||now<cleanup||now-cleanup>=750)break;int st;pid_t got=waitpid(-1,&st,__WALL|WNOHANG);if(got<0&&errno==ECHILD){all_reaped=1;break;}if(got>0){struct task*t=lookup(got);if(WIFEXITED(st)||WIFSIGNALED(st)){if(t)t->alive=0;if(got==pid){root_exit=WIFEXITED(st)?WEXITSTATUS(st):-1;root_signal=WIFSIGNALED(st)?WTERMSIG(st):0;}}else(void)kill(got,SIGKILL);}else usleep(1000);}
 }
 char report[1024];int n=snprintf(report,sizeof(report),"{\"schema\":1,\"stage\":\"%s\",\"errorNumber\":%d,\"fdsVerified\":%s,\"imageVerified\":%s,\"deadlineExpired\":%s,\"ownerLost\":%s,\"gracefulStopRequested\":%s,\"ownerGraceExpired\":%s,\"tasksObserved\":%u,\"deniedSockets\":%u,\"deniedEscape\":%u,\"replacedReturns\":%u,\"recvmsgReplaced\":%u,\"allReaped\":%s,\"rootExit\":%d,\"rootSignal\":%d,\"supervisionComplete\":%s}\n",stage,error_no,fds_verified?"true":"false",image_verified?"true":"false",expired?"true":"false",owner_lost?"true":"false",retirement.requested?"true":"false",owner_grace_expired?"true":"false",tasks_n,sockets_denied,escapes_denied,returns_replaced,recvmsg_replaced,all_reaped?"true":"false",root_exit,root_signal,okay&&!retirement.requested&&all_reaped&&image_verified&&fds_verified?"true":"false");
 if(n<=0||n>=(int)sizeof(report)||write(REPORT_FD,report,(size_t)n)!=n)return 74;
 return all_reaped?0:75;
}
