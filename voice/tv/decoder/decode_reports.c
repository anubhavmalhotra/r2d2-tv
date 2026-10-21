#define _POSIX_C_SOURCE 200809L
#include "capture_core.h"
#include <unistd.h>
#include <errno.h>
#include <sys/resource.h>
#include <signal.h>
/* Fixed stdin report stream to stdout PCM. No device, path, network or logs. */
#define MAX_REPORTS 400
static unsigned char input[MAX_REPORTS*124+1],pcm[MAX_REPORTS*480];
int main(void){struct rlimit core={0,0};if(setrlimit(RLIMIT_CORE,&core))return 64;alarm(2);size_t used=0,out=0;int rc=65;struct owner_decoder d={0};struct owner_packet packet={0};
 while(used<sizeof(input)){ssize_t n=read(0,input+used,sizeof(input)-used);if(n<0){if(errno==EINTR)continue;goto finish;}if(!n)break;used+=(size_t)n;}
 if(used<248||used>MAX_REPORTS*124||used%124||owner_decoder_init(&d))goto finish;
 for(size_t i=0;i<used;i+=124){if(owner_parse(input+i,124,&packet)||owner_decode(&d,&packet,pcm+out))goto finish;out+=480;owner_clear(&packet,sizeof(packet));}
 for(size_t sent=0;sent<out;){ssize_t n=write(1,pcm+sent,out-sent);if(n<0){if(errno==EINTR)continue;goto finish;}if(n==0)goto finish;sent+=(size_t)n;}rc=0;
 finish:owner_decoder_finish(&d);owner_clear(&packet,sizeof(packet));owner_clear(input,sizeof(input));owner_clear(pcm,sizeof(pcm));return rc;
}
