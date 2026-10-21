#include "native_handoff.hpp"
#include "ui_authority.hpp"
#include <iostream>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>
using namespace r2d2::local_capture;
void need(bool value){if(!value)throw std::runtime_error("UI_AUTHORITY_ASSERTION");}
int main(){
 try{
  for(const char *method:{"getDevices","startStreaming","stopStreaming"}){
   need(native_route_allowed(2,kUiCategory,kUiCaller,method));
   for(const char *sender:{"com.webos.starfish.keyfilters","com.webos.service.voiceinput.hidraw","foreign.service",""})need(!native_route_allowed(2,kUiCategory,sender,method));
   need(!native_route_allowed(2,"/",kUiCaller,method));need(!native_route_allowed(1,kUiCategory,kUiCaller,method));
   need(native_route_allowed(1,"/",kCaller,method));need(!native_route_allowed(1,"/",kUiCaller,method));
  }
  need(native_route_allowed(0,"/","com.webos.service.voiceinput.hidraw","getDevices"));
  need(!native_route_allowed(0,"/","com.webos.service.voiceinput.hidraw","startStreaming"));
  need(!native_route_allowed(2,kUiCategory,kUiCaller,"start"));need(!native_route_allowed(3,kUiCategory,kUiCaller,"getDevices"));
  need(!native_route_allowed(2,nullptr,kUiCaller,"getDevices"));
  for(bool ui:{false,true})for(bool foreign:{false,true}){
   int sockets[2];need(socketpair(AF_UNIX,SOCK_STREAM,0,sockets)==0);int starts=0,stops=0;
   Profile profile{kBackend,kProviderSha256,"",getpid(),getuid(),1000,true,ui};
   Session session(sockets[0],profile,{[&]{++starts;return true;},[&]{++stops;return true;}});need(session.ready());
   const char *caller=ui?(foreign?kCaller:kUiCaller):(foreign?kUiCaller:kCaller);
   need(session.reserve(caller,"remote7")==!foreign);
   if(!foreign){need(session.begin("remote7"));unsigned char pcm[]={1,2,3,4};need(session.pcm("remote7",pcm,nullptr,4));session.release();need(session.status().complete&&session.status().stop_confirmed&&starts==1&&stops==1);}
   else{need(!session.begin("remote7"));need(starts==0&&stops==0&&session.status().transport_closed);}
   close(sockets[1]);
  }
  {
   int sockets[2];need(socketpair(AF_UNIX,SOCK_STREAM,0,sockets)==0);int starts=0,stops=0;
   Profile profile{kBackend,kProviderSha256,"",getpid(),getuid(),1000,true,true};
   Session session(sockets[0],profile,{[&]{++starts;return false;},[&]{++stops;return true;}});
   need(session.reserve(kUiCaller,"remote7"));need(!session.begin("remote7"));const auto result=session.status();
   need(result.start_attempted&&!result.start_confirmed&&result.stop_attempted&&result.stop_confirmed&&result.transport_closed&&!result.complete&&result.bytes==0);
   need(starts==1&&stops==1);close(sockets[1]);
  }
  std::cout<<"{\"groups\":6,\"deviceCalls\":false}\n";return 0;
 }catch(const std::exception&){return 1;}
}
