/* Exact-build, fresh-process provider hook. This module is NOT a general webOS
 * patch. Parent must pin executable/module/ABI before exec, suppress stdout and
 * post-startup stderr, and own a finite process/recovery deadline. No constructors in normal
 * apps load this library. No filesystem patch or LS2 permission edit is made.
 */
#define _GNU_SOURCE 1
#include "native_handoff.hpp"
#include "ble_callback.hpp"
#include "native_retirement.hpp"
#include "startup_lock.hpp"
#include "ui_authority.hpp"
#include <memory>
#include <atomic>
#include <cerrno>
#include <cstdarg>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cstring>
#include <mutex>
#include <link.h>
#include <elf.h>
#include <dirent.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <string>
#include <ios>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <time.h>
#include <luna-service2/lunaservice.h>
namespace {
using namespace r2d2::local_capture;
using DeviceFn=bool(*)(void*,void*);
using GetId=std::string(*)(void*);
using CopyDevice=void(*)(void*,void*);
std::atomic<bool> enabled{false};
volatile sig_atomic_t graceful_stop=0;
void request_stop(int){graceful_stop=1;}
thread_local bool owner_call=false;
// Constant-initialized pointer: ELF constructor ordering must not reset a C++
// owning global after owner_init. This finite owned process retires then _exit.
Session *channel=nullptr;
DeviceFn native_start=nullptr,native_stop=nullptr;
GetId native_id=nullptr;CopyDevice native_copy=nullptr;
void *manager=nullptr,*device_copy=nullptr,*original_device=nullptr;
using NotifyFn=bool(*)(void*,char*,char*,int);
NotifyFn native_notify=nullptr;
BleFeCallback::Image native_image{};
std::mutex hardware_lock;
int control_fd=-1,status_fd=-1,owned_data_fd=-1;
std::atomic<LSHandle*> probe_handle{nullptr};
std::atomic<bool> probe_pending{false};unsigned probe_calls=0;std::atomic<uint64_t> probe_next{0};uint64_t process_deadline=0;
bool observed_services=false;bool capture_mode=false;uint32_t provider_mode=0;std::atomic<bool> stop_requested{false};
LSMethod guarded_methods[65]{};LSMethod ui_methods[4]{};LSMethod private_methods[7]{};LSHandle *private_handle=nullptr;
struct Config { uint32_t magic;uint32_t parent_pid;char device[32];uint32_t lease_ms;uint32_t mode;uint32_t backend_profile; };
static_assert(sizeof(Config)==52,"CONFIG_ABI");
uint64_t clock_ms(){timespec t{};if(clock_gettime(CLOCK_MONOTONIC,&t))return 0;return uint64_t(t.tv_sec)*1000+t.tv_nsec/1000000;}
void scalar(char c){if(status_fd>=0)(void)write(status_fd,&c,1);}
[[noreturn]] void fail(){scalar('F');_exit(76);}
void *symbol(const char *name){void*p=dlsym(RTLD_DEFAULT,name);if(!p)fail();return p;}
int main_image(dl_phdr_info *info,size_t,void *context){
 // The supervisor execs the pinned held image. Only the loader's main-image
 // mapping (not argv[0], a JSON base, or a similarly named DSO) can bind the
 // original notify symbol and BLE callsite. This also handles ASLR/ET_DYN.
 if(info->dlpi_name&&*info->dlpi_name)return 0;
 auto *found=static_cast<bool*>(context);if(*found)fail();
 BleFeCallback::Image image{uintptr_t(info->dlpi_addr),reinterpret_cast<uintptr_t>(native_notify)};
 if(!image.valid())fail();
 bool notify_code=false,callsite_code=false;
 for(ElfW(Half) i=0;i<info->dlpi_phnum;++i){const auto &segment=info->dlpi_phdr[i];
  if(segment.p_type!=PT_LOAD||!(segment.p_flags&PF_X))continue;
  if(segment.p_memsz>UINTPTR_MAX-segment.p_vaddr)fail();
  auto end=uintptr_t(segment.p_vaddr+segment.p_memsz);
  notify_code|=segment.p_vaddr<=0x60ba8&&0x60ba8<end;
  callsite_code|=segment.p_vaddr<=0x5771c&&0x5771c<end;
 }
 if(!notify_code||!callsite_code)fail();
 native_image=image;*found=true;return 0;
}
// Every slot is checked against the original exported function before changing
// private process memory; parent pins the complete original binary as well.
int original_protection(uintptr_t address){
 FILE *f=fopen("/proc/self/maps","r");if(!f)fail();char line[512];int result=-1;
 while(fgets(line,sizeof(line),f)){unsigned long a=0,b=0;char mode[5]={};
  if(sscanf(line,"%lx-%lx %4s",&a,&b,mode)==3&&address>=a&&address<b){
   if(mode[0]=='r'&&mode[2]=='-'&&mode[3]=='p'){result=PROT_READ|(mode[1]=='w'?PROT_WRITE:0);}break;}}
 fclose(f);if(result<0)fail();return result;
}
void replace(const char *table,size_t offset,const char *expected,void *replacement){
 auto slot=reinterpret_cast<void**>(static_cast<char*>(symbol(table))+offset);
 if(*slot!=symbol(expected))fail();
 long n=sysconf(_SC_PAGESIZE);if(n<=0||(n&(n-1)))fail();
 uintptr_t page=reinterpret_cast<uintptr_t>(slot)&~uintptr_t(n-1);
 int previous=original_protection(reinterpret_cast<uintptr_t>(slot));
 if(mprotect(reinterpret_cast<void*>(page),size_t(n),PROT_READ|PROT_WRITE))fail();
 *slot=replacement;
 if(mprotect(reinterpret_cast<void*>(page),size_t(n),previous))fail();
}
bool begin_hardware(){
 if(!manager||!device_copy)return false;
 bool result=channel->begin(native_id(device_copy));scalar(result?'S':'X');
 if(result){timespec ts{0,10000000};nanosleep(&ts,nullptr);}
 return result;
}
bool reserve(void*,std::string id,std::string caller,int type,std::string& uri){
 std::lock_guard<std::mutex> lock(hardware_lock);
 if(!capture_mode||!enabled.load()||!owner_call||type!=0||!channel||!channel->reserve(caller,id))return false;
 uri="owner-pipe://voice";scalar('R');
 if(manager&&device_copy&&!begin_hardware())return false;
 return true;
}
int owned_remove(void*,std::string,std::string){stop_requested.store(true);return 1;}
bool deny_send(void*,std::string,char*,int){return false;}
bool start(void *self,void *device){
 std::lock_guard<std::mutex> lock(hardware_lock);
 if(!enabled.load()||!channel||!device)return false;
 auto phase=channel->status().phase;
 if(phase!=Phase::Idle&&phase!=Phase::Reserved)return false;
 // CTask::~CTask deletes task->m_deviceInfo after handleDeviceStorage("insert");
 // clone CDeviceInfo into device_copy for both deferred START and STOP.
 if(!device_copy){device_copy=::operator new(0x16c);native_copy(device_copy,device);}
 manager=self;original_device=device_copy;
 return phase==Phase::Reserved?begin_hardware():true;
}
bool stop(void*,void*){
 if(!channel)return false;
 channel->release();bool confirmed=channel->status().stop_confirmed;
 scalar(confirmed?'T':'U');return confirmed;
}
bool no_restart(void*,void*){return false;}
void pcm(void*,std::string id,char *first,char *second,int bytes){
 if(!enabled.load()||stop_requested.load())return;
 if(channel&&!handoff_ble_pcm(*channel,id,first,second,bytes))scalar('F');
}
__attribute__((noinline)) bool qualified_notify(void *self,char *first,char *second,int bytes){
 auto address=reinterpret_cast<uintptr_t>(__builtin_extract_return_addr(__builtin_return_address(0)));
 bool result=false;
 if(!capture_mode||!channel||!BleFeCallback::dispatch(native_image,address,first,second,[&]{result=native_notify(self,first,second,bytes);})){if(channel)channel->cancel();return false;}
 if(!result)channel->cancel();
 return result;
}
void finish_capture(){
 if(capture_mode){timespec ts{0,30000000};nanosleep(&ts,nullptr);}
 enabled.store(false);
 if(channel){if(stop_requested.load())channel->release();auto status=finish_status(*channel,500);if(status.error!=Error::None||!status.complete)scalar('F');scalar(status.stop_confirmed?'T':status.stop_attempted?'U':'N');
  // Authored counters only, never PCM or a device identifier. The parent must
  // reconcile these actual sent bytes with accepted/drained bytes before it
  // can release the authenticated Spark utterance.
  char summary[19]{};int n=snprintf(summary,sizeof(summary),"Y%08x%08xZ",unsigned(status.bytes),unsigned(status.frames));
  if(n!=18||write(status_fd,summary,18)!=18)scalar('F');
 }
 else scalar('N');
}
bool probe_reply(LSHandle*,LSMessage *message,void*){
 const char *payload=LSMessageGetPayload(message);size_t n=payload?strnlen(payload,8193):8193;
 if(n==0||n>8192){scalar('F');probe_pending.store(false);return true;}
 // Private JSON remains on the inherited process-local pipe; the parent emits
 // counts only and erases its receive buffers. No path/socket/file is opened.
 unsigned char frame[8196];frame[0]=n&255;frame[1]=(n>>8)&255;frame[2]=frame[3]=0;
 memcpy(frame+4,payload,n);ssize_t sent=send(owned_data_fd,frame,n+4,MSG_DONTWAIT|MSG_NOSIGNAL);
 volatile unsigned char *erase=frame;for(size_t i=0;i<n+4;++i)erase[i]=0;
 scalar(sent==ssize_t(n+4)?'D':'F');probe_pending.store(false);return true;
}
void probe_tick(uint64_t now){
 if(capture_mode||!probe_handle.load()||probe_pending.load()||probe_calls>=3||now<probe_next)return;
 probe_pending.store(true);++probe_calls;probe_next=now+750;
 using Fn=bool(*)(LSHandle*,const char*,const char*,LSFilterFunc,void*,LSMessageToken*,LSError*);
 auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"LSCallOneReply"));LSMessageToken token=0;
 bool ok=real&&real(probe_handle.load(),"luna://com.webos.service.voiceinput.hidraw/getDevices","{}",probe_reply,nullptr,&token,nullptr);
 if(!ok){probe_pending.store(false);scalar('F');}
}
void *watch(void*){
 for(;;){
  uint64_t now=clock_ms();
  if(graceful_stop||!now||now>=process_deadline||(channel&&(channel->status().phase==Phase::Stopped||channel->status().phase==Phase::Failed)))break;
  pollfd p{control_fd,POLLIN|POLLHUP,0};int r=poll(&p,1,20);
  if(r<0&&errno==EINTR)continue;
  if(r<0)break;
  if(r>0){char c;ssize_t n=read(control_fd,&c,1);if(n!=1||c!='C')break;scalar('C');}
  probe_tick(now);
 }
 finish_capture();scalar('E');_exit(0);
}
LSMethodFunction originals[64]{};const char *names[64]{};size_t method_count=0;
bool gate(LSHandle *h,LSMessage *m,void *ctx){
 const char *sender=LSMessageGetSenderServiceName(m),*method=LSMessageGetMethod(m);
 bool keyfilter=sender&&!strcmp(sender,"com.webos.starfish.keyfilters");
 bool allowed=enabled.load()&&native_route_allowed(provider_mode,LSMessageGetCategory(m),sender,method);
 if(!allowed){(void)LSMessageRespond(m,"{\"returnValue\":false,\"errorCode\":-1,\"errorText\":\"Owner voice unavailable\"}",nullptr);return true;}
 if(!strcmp(method,"getDevices")&&!observed_services){
  // The normal /start API requires a real Activity Manager activityId. Do not
  // forge one. Its checked start task only runs this hardware observer method.
  auto initialized=static_cast<unsigned char*>(symbol("_ZGVZN12CTaskManager11getInstanceEvE8instance"));
  void *tasks=symbol("_ZZN12CTaskManager11getInstanceEvE8instance");
  if(!(*initialized&1)||!*(reinterpret_cast<void**>(static_cast<char*>(tasks)+0x30)))return false;
  auto prepare=reinterpret_cast<DeviceFn>(symbol("_ZN12CTaskManager18handleStartServiceEP5CTask"));
  if(!prepare(tasks,nullptr))return false;
  observed_services=true;scalar('O');
 }
 for(size_t i=0;i<method_count;++i)if(!strcmp(names[i],method)){
  scalar(keyfilter||provider_mode==2?'M':'P');owner_call=true;bool result=false;try{result=originals[i](h,m,ctx);}catch(...){owner_call=false;throw;}owner_call=false;return result;
 }
 return false;
}
void network_filter(){
 // This exact process must be a tracee of its direct supervisor, not merely
 // launched with an environment marker. Probe denied families before any LS2
 // handle, hardware observer, or native thread is created.
 const char *mode=getenv("OWNER_VOICE_TRACED");if(!mode||strcmp(mode,"1"))fail();
 FILE*f=fopen("/proc/self/status","r");if(!f)fail();char line[256];long tracer=0;
 while(fgets(line,sizeof(line),f))if(sscanf(line,"TracerPid:%ld",&tracer)==1)break;
 fclose(f);if(tracer!=getppid()||tracer<=1)fail();
 DIR *tasks=opendir("/proc/self/task");if(!tasks)fail();unsigned count=0;
 for(dirent *entry;(entry=readdir(tasks));)if(entry->d_name[0]!='.')++count;
 closedir(tasks);if(count!=1)fail();scalar('t');
 if(prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)!=1)fail();
 scalar('n');
 const int families[]={AF_INET,AF_INET6,AF_PACKET};
 for(int family:families){errno=0;int fd=int(syscall(__NR_socket,family,SOCK_STREAM,0));if(fd>=0){close(fd);fail();}if(errno!=EACCES)fail();}
 int unix_fd=int(syscall(__NR_socket,AF_UNIX,SOCK_STREAM,0));if(unix_fd<0)fail();close(unix_fd);
 scalar('K');
}
unsigned owner_parent(){
 const char*s=getenv("OWNER_VOICE_PARENT_PID");if(!s||!*s)fail();char*end=nullptr;long parent=strtol(s,&end,10);if(!end||*end||parent<=1)fail();
 char path[64];snprintf(path,sizeof(path),"/proc/%ld/status",long(getppid()));
 FILE*f=fopen(path,"r");if(!f)fail();char line[256];long actual=0;
 while(fgets(line,sizeof(line),f))if(sscanf(line,"PPid:%ld",&actual)==1)break;
 fclose(f);if(parent!=actual)fail();return unsigned(parent);
}
int inherited(const char *name){const char*s=getenv(name);if(!s||!*s)return -1;char*end=nullptr;long v=strtol(s,&end,10);return end&&!*end&&v>=3&&v<=1024?int(v):-1;}
}
namespace {
bool deny_private(LSHandle*,LSMessage *m,void*){(void)LSMessageRespond(m,"{\"returnValue\":false}",nullptr);return true;}
}
extern "C" bool LSRegisterCategory(LSHandle *h,const char *category,LSMethod *methods,LSSignal *signals,LSProperty *properties,LSError *error){
 using Fn=bool(*)(LSHandle*,const char*,LSMethod*,LSSignal*,LSProperty*,LSError*);
 auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"LSRegisterCategory"));if(!real||!enabled.load())return false;
 // The exact LS2 library registers this built-in category during LSRegister,
 // before the provider root table. Keep cancellation and scalar ping, deny
 // diagnostic payload enumeration; do not mistake it for the provider API.
 if(category&&!strcmp(category,"/com/palm/luna/private")){
  static const char *expected[]={"cancel","ping","subscriptions","mallinfo","malloc_trim","introspection"};
  if(private_handle||method_count||!methods)return false;
  for(size_t i=0;i<6;++i){if(!methods[i].name||strcmp(methods[i].name,expected[i])||!methods[i].function)return false;private_methods[i]=methods[i];if(i>=2)private_methods[i].function=deny_private;}
  if(methods[6].name)return false;
  bool ok=real(h,category,private_methods,signals,properties,error);if(ok)private_handle=h;return ok;
 }
 if(!category||strcmp(category,"/")||method_count||!methods||h!=private_handle)return false;
 LSMethod *guarded=guarded_methods;
 for(size_t i=0;i<65;++i){
  if(!methods[i].name){
   method_count=i;bool ok=real(h,category,guarded,signals,properties,error);
   if(ok&&provider_mode==2){
    size_t count=0;
    for(size_t n=0;n<i;++n)if(!strcmp(names[n],"getDevices")||!strcmp(names[n],"startStreaming")||!strcmp(names[n],"stopStreaming")){
     if(count>=3)return false;
     ui_methods[count++]=guarded[n];
    }
    ok=count==3&&real(h,kUiCategory,ui_methods,nullptr,nullptr,error);
   }
   scalar(ok?'L':'F');if(ok&&!capture_mode){probe_next=clock_ms()+250;probe_handle.store(h);}return ok;
  }
  if(i==64||!methods[i].function||strnlen(methods[i].name,65)>64)return false;
  originals[i]=methods[i].function;names[i]=methods[i].name;guarded[i]=methods[i];guarded[i].function=gate;
 }
 return false;
}
// Preserve the provider's genuine callback object when its root category or
// individual method data is installed. The UI route never invents a C++ ctx.
extern "C" bool LSCategorySetData(LSHandle *h,const char *category,void *data,LSError *error){
 using Fn=bool(*)(LSHandle*,const char*,void*,LSError*);
 auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"LSCategorySetData"));
 if(!real)return false;
 bool ok=real(h,category,data,error);
 if(ok&&provider_mode==2&&h==private_handle&&category&&!strcmp(category,"/")){
  ok=real(h,kUiCategory,data,error);
  if(!ok){enabled.store(false);if(channel)channel->cancel();scalar('F');}
 }
 return ok;
}
extern "C" bool LSMethodSetData(LSHandle *h,const char *category,const char *method,void *data,LSError *error){
 using Fn=bool(*)(LSHandle*,const char*,const char*,void*,LSError*);
 auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"LSMethodSetData"));
 if(!real)return false;
 bool ok=real(h,category,method,data,error);
 if(ok&&provider_mode==2&&h==private_handle&&category&&!strcmp(category,"/")&&method&&
    (!strcmp(method,"getDevices")||!strcmp(method,"startStreaming")||!strcmp(method,"stopStreaming"))){
  ok=real(h,kUiCategory,method,data,error);
  if(!ok){enabled.store(false);if(channel)channel->cancel();scalar('F');}
 }
 return ok;
}
// Whole-provider logging sink. It never evaluates/serializes variadic payloads.
extern "C" int _PmLogMsgKV(void*,int,const char*,int,...){return 0;}
extern "C" FILE *fopen(const char *path,const char *mode){
 if(!mode||strpbrk(mode,"wa+")){errno=EACCES;return nullptr;}
 using Fn=FILE*(*)(const char*,const char*);auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"fopen"));
 if(!real){errno=EACCES;return nullptr;}return real(path,mode);
}
extern "C" void LSErrorLogDefault(const char*,LSError*){}
namespace {
bool allowed_status_payload(const char *payload){
 static const char *services[]={"{\"serviceName\":\"com.palm.activitymanager\"}","{\"serviceName\":\"com.webos.service.bluetooth2\"}","{\"serviceName\":\"com.webos.service.config\"}","{\"serviceName\":\"com.webos.service.mrcu\"}"};
 if(!payload)return false;
 for(const char *entry:services)if(!strcmp(payload,entry))return true;
 return false;
}
bool allowed_uri(const char *uri,const char *payload=nullptr){
 static const char *list[]={"luna://com.palm.activitymanager/adopt","luna://com.palm.activitymanager/complete","luna://com.webos.service.bluetooth2/hid/internal/setReport","luna://com.webos.service.config/getConfigs","luna://com.webos.service.mrcu/enableAudioSinkMode","luna://com.webos.service.mrcu/isPaired"};
 if(!uri)return false;
 if(!strcmp(uri,"luna://com.webos.service.bus/signal/registerServerStatus"))return allowed_status_payload(payload);
 if(!capture_mode&&(!strcmp(uri,"luna://com.webos.service.mrcu/enableAudioSinkMode")||!strcmp(uri,"luna://com.webos.service.bluetooth2/hid/internal/setReport")))return false;
 for(const char *entry:list)if(!strcmp(uri,entry))return true;
 return false;
}
bool call(const char *symbol_name,LSHandle *h,const char *uri,const char *payload,LSFilterFunc callback,void *context,LSMessageToken *token,LSError *error){
 if(!allowed_uri(uri,payload))return false;
 using Fn=bool(*)(LSHandle*,const char*,const char*,LSFilterFunc,void*,LSMessageToken*,LSError*);
 auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,symbol_name));return real&&real(h,uri,payload,callback,context,token,error);
}
bool writing(int flags){return (flags&O_ACCMODE)!=O_RDONLY||(flags&(O_CREAT|O_TRUNC|O_APPEND));}
bool hid_path(const char *path){if(!path||strncmp(path,"/dev/hidraw",11)||!path[11])return false;for(size_t i=11;path[i];++i)if(path[i]<'0'||path[i]>'9')return false;return true;}
int own_open(const char *symbol_name,int dir,const char *path,int flags,mode_t mode,bool at){
 bool write=writing(flags);
 // Compatibility for the pinned original main's exact lock request. This never
 // creates or truncates a file; the helper admits only a stable existing empty
 // root-owned regular inode. Keep the current status protocol unchanged.
 if(!at&&owner_voice::lock_request(path,flags,mode)){
  auto real=reinterpret_cast<owner_voice::LockOpen>(dlsym(RTLD_NEXT,symbol_name));
  return owner_voice::existing_startup_lock(path,flags,mode,real);
 }
 if(write&&(!hid_path(path)||(flags&(O_CREAT|O_TRUNC|O_APPEND)))){errno=EACCES;return -1;}
 int fd=-1;
 if(at){using Fn=int(*)(int,const char*,int,...);auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,symbol_name));if(real)fd=real(dir,path,flags,mode);}
 else{using Fn=int(*)(const char*,int,...);auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,symbol_name));if(real)fd=real(path,flags,mode);}
 if(fd>=0&&write){struct stat st{};if(fstat(fd,&st)||!S_ISCHR(st.st_mode)){close(fd);errno=EACCES;return -1;}}
 return fd;
}
}
extern "C" bool LSCall(LSHandle*h,const char*u,const char*p,LSFilterFunc f,void*c,LSMessageToken*t,LSError*e){return call("LSCall",h,u,p,f,c,t,e);}
extern "C" bool LSCallOneReply(LSHandle*h,const char*u,const char*p,LSFilterFunc f,void*c,LSMessageToken*t,LSError*e){return call("LSCallOneReply",h,u,p,f,c,t,e);}
extern "C" int open(const char*p,int f,...){mode_t m=0;if(f&O_CREAT){va_list a;va_start(a,f);m=va_arg(a,int);va_end(a);}return own_open("open",AT_FDCWD,p,f,m,false);}
extern "C" int open64(const char*p,int f,...){mode_t m=0;if(f&O_CREAT){va_list a;va_start(a,f);m=va_arg(a,int);va_end(a);}return own_open("open64",AT_FDCWD,p,f,m,false);}
extern "C" int openat(int d,const char*p,int f,...){mode_t m=0;if(f&O_CREAT){va_list a;va_start(a,f);m=va_arg(a,int);va_end(a);}return own_open("openat",d,p,f,m,true);}
extern "C" int openat64(int d,const char*p,int f,...){mode_t m=0;if(f&O_CREAT){va_list a;va_start(a,f);m=va_arg(a,int);va_end(a);}return own_open("openat64",d,p,f,m,true);}
// The pinned provider imports this precise C++ filebuf entry as well as fopen.
// Interpose the entry before libstdc++ can use a hidden libc open alias.
extern "C" void *owner_filebuf_open(void*,const char*,std::ios_base::openmode)
 __asm__("_ZNSt13basic_filebufIcSt11char_traitsIcEE4openEPKcSt13_Ios_Openmode");
extern "C" void *owner_filebuf_open(void*self,const char*path,std::ios_base::openmode mode){
 if(mode&(std::ios_base::out|std::ios_base::app|std::ios_base::trunc))return nullptr;
 using Fn=void*(*)(void*,const char*,std::ios_base::openmode);
 auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"_ZNSt13basic_filebufIcSt11char_traitsIcEE4openEPKcSt13_Ios_Openmode"));return real?real(self,path,mode):nullptr;
}
__attribute__((constructor)) static void owner_init(){
 // Establish the bounded status channel before any ABI/config refusal. These
 // authored scalar checkpoints contain no LS2 response, key, name or PCM.
 status_fd=inherited("OWNER_VOICE_STATUS_FD");
 int sf=status_fd<0?-1:fcntl(status_fd,F_GETFL);
 if(sf<0||fcntl(status_fd,F_SETFL,sf|O_NONBLOCK))_exit(76);
 scalar('a');
 struct sigaction retirement{};retirement.sa_handler=request_stop;
 sigset_t retirement_signal;
 if(sigemptyset(&retirement_signal)||sigaddset(&retirement_signal,SIGTERM)||
  sigprocmask(SIG_UNBLOCK,&retirement_signal,nullptr)||sigemptyset(&retirement.sa_mask)||sigaction(SIGTERM,&retirement,nullptr))fail();
 if(sizeof(void*)!=4||sizeof(std::string)!=24)fail();
 int data_fd=inherited("OWNER_VOICE_DATA_FD");control_fd=inherited("OWNER_VOICE_CONTROL_FD");
 if(data_fd<0||control_fd<0||status_fd<0||data_fd==control_fd||data_fd==status_fd||control_fd==status_fd)fail();
 scalar('b');
 Config cfg{};size_t got=0;uint64_t handshake=clock_ms();if(!handshake)fail();
 while(got<sizeof(cfg)){
  uint64_t t=clock_ms();if(t<handshake||t-handshake>=1000)fail();
  pollfd p{control_fd,POLLIN,0};int ready=poll(&p,1,int(1000-(t-handshake)));
  if(ready<0&&errno==EINTR)continue;
  if(ready!=1)fail();
  ssize_t count=read(control_fd,reinterpret_cast<char*>(&cfg)+got,sizeof(cfg)-got);
  if(count<0&&errno==EINTR)continue;
  if(count<=0)fail();
  got+=size_t(count);
 }
 if(cfg.magic!=0x31564f52||cfg.parent_pid!=owner_parent()||cfg.mode>2||cfg.lease_ms<100||cfg.lease_ms>10000||cfg.backend_profile!=(cfg.mode?1u:0u))fail();
 provider_mode=cfg.mode;capture_mode=cfg.mode!=0;
 if(strnlen(cfg.device,sizeof(cfg.device))==sizeof(cfg.device))fail();
 if(cfg.device[0]){if(strncmp(cfg.device,"remote",6)||!cfg.device[6])fail();for(size_t i=6;cfg.device[i];++i)if(cfg.device[i]<'0'||cfg.device[i]>'9')fail();}
 scalar('c');
 ucred peer{};socklen_t size=sizeof(peer);int type=0;socklen_t tsize=sizeof(type);
 if(getsockopt(data_fd,SOL_SOCKET,SO_TYPE,&type,&tsize)||type!=SOCK_STREAM||getsockopt(data_fd,SOL_SOCKET,SO_PEERCRED,&peer,&size)||unsigned(peer.pid)!=cfg.parent_pid||peer.uid!=geteuid())fail();
 scalar('d');
 int df=fcntl(data_fd,F_GETFL);
 if(df<0||prctl(PR_SET_DUMPABLE,0)||fcntl(data_fd,F_SETFL,df|O_NONBLOCK))fail();
 scalar('e');
 owned_data_fd=data_fd;network_filter();
 uint64_t now=clock_ms();if(!now)fail();process_deadline=now+cfg.lease_ms;

 native_start=reinterpret_cast<DeviceFn>(symbol("_ZN17CDeviceManagement12startCaptureEP11CDeviceInfo"));
 native_stop=reinterpret_cast<DeviceFn>(symbol("_ZN17CDeviceManagement11stopCaptureEP11CDeviceInfo"));
 native_id=reinterpret_cast<GetId>(symbol("_ZN11CDeviceInfo5getIDB5cxx11Ev"));
 native_copy=reinterpret_cast<CopyDevice>(symbol("_ZN11CDeviceInfoC1EPS_"));
 native_notify=reinterpret_cast<NotifyFn>(symbol("_ZN8CCapture18notifyDataCapturedEPcS0_i"));
 bool image_found=false;dl_iterate_phdr(main_image,&image_found);if(!image_found)fail();
 if(capture_mode){
  Profile profile{kBackend,kProviderSha256,cfg.device,pid_t(cfg.parent_pid),geteuid(),cfg.lease_ms,cfg.device[0]==0,cfg.mode==2};
  NativeOps native{[]{return manager&&original_device&&native_start(manager,original_device);},[]{return manager&&device_copy&&native_stop(manager,device_copy);}};
  channel=new Session(data_fd,std::move(profile),std::move(native));if(!channel->ready())fail();
 }
 scalar('f');
 replace("_ZTV17CSocketManagement",52,"_ZN17CSocketManagement13reserveClientENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEES5_iRS5_",reinterpret_cast<void*>(reserve));
 replace("_ZTV17CSocketManagement",56,"_ZN17CSocketManagement12removeClientENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEES5_",reinterpret_cast<void*>(owned_remove));
 replace("_ZTV17CSocketManagement",32,"_ZN17CSocketManagement8sendDataENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPci",reinterpret_cast<void*>(deny_send));
 replace("_ZTV17CDeviceManagement",40,"_ZN17CDeviceManagement12startCaptureEP11CDeviceInfo",reinterpret_cast<void*>(start));
 replace("_ZTV17CDeviceManagement",44,"_ZN17CDeviceManagement12restartTimerEP11CDeviceInfo",reinterpret_cast<void*>(no_restart));
 replace("_ZTV17CDeviceManagement",48,"_ZN17CDeviceManagement11stopCaptureEP11CDeviceInfo",reinterpret_cast<void*>(stop));
 replace("_ZTV17CDeviceManagement",20,"_ZN17CDeviceManagement12dataCapturedENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPcS6_i",reinterpret_cast<void*>(pcm));
 replace("_ZTV14CCaptureHidraw",72,"_ZN8CCapture18notifyDataCapturedEPcS0_i",reinterpret_cast<void*>(qualified_notify));
 scalar('g');enabled.store(true);pthread_t thread;if(pthread_create(&thread,nullptr,watch,nullptr))fail();pthread_detach(thread);scalar('B');
}
