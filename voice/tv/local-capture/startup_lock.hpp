#ifndef OWNER_STARTUP_LOCK_HPP
#define OWNER_STARTUP_LOCK_HPP
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
namespace owner_voice {
using LockOpen=int(*)(const char*,int,...);
static bool lock_request(const char*path,int flags,mode_t mode){
 return path&&!strcmp(path,"/tmp/voiceinput_hidraw.lock")&&flags==(O_RDWR|O_CREAT)&&mode==0700;
}
static bool lock_file(const struct stat&s){
 return S_ISREG(s.st_mode)&&s.st_uid==0&&s.st_gid==0&&s.st_nlink==1&&s.st_size==0&&!(s.st_mode&06077);
}
static bool same_lock(const struct stat&a,const struct stat&b){
 return a.st_dev==b.st_dev&&a.st_ino==b.st_ino&&a.st_mode==b.st_mode&&a.st_uid==b.st_uid&&a.st_gid==b.st_gid&&a.st_nlink==b.st_nlink&&a.st_size==b.st_size&&a.st_mtime==b.st_mtime&&a.st_ctime==b.st_ctime;
}
static int existing_startup_lock(const char*path,int flags,mode_t mode,LockOpen open_file){
 if(!lock_request(path,flags,mode)||!open_file){errno=EACCES;return -1;}
 struct stat before{},opened{},after{};
 if(lstat(path,&before)||!lock_file(before)){errno=EACCES;return -1;}
 // The original main lockf(F_TLOCK)s this fd and closes it at shutdown. Existing bytes
 // are never written; creation/truncation and every other writable path stay
 // denied. No-follow also protects the gap between lstat and open.
 int fd=open_file(path,O_RDWR|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
 if(fd<0)return -1;
 if(fstat(fd,&opened)||lstat(path,&after)||!lock_file(opened)||!lock_file(after)||!same_lock(before,opened)||!same_lock(opened,after)){
  close(fd);errno=EACCES;return -1;
 }
 return fd;
}
}
#endif
