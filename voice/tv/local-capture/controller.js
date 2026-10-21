/* QML-compatible ES5. Called only by the earliest protected owner Voice signal.
 * Reuses the existing keyfilter Service; no new identity or vendor fallback. */
// The hold-to-talk limit: a held press is released (and submitted) HOLD_MS after
// key-down. The owned provider's lease (provider.maxMs, at most 10000 natively)
// starts later and must outlast it: 10000 - 9200 keeps the same 800 ms native
// margin the 5200/6000 pair had. PRESS_MS cancels a press that never started.
var HOLD_MS=9200,PRESS_MS=13000;
function create(existingService,voiceKey,clock,options){
 options=options||{};
 var portFactory=options.portFactory||(typeof require==='function'?require('./service_port').create:null);
 if(!existingService||typeof existingService.call!=='function'||typeof existingService.cancel!=='function'||!existingService.callbackMap||typeof voiceKey!=='number'||!isFinite(voiceKey)||typeof clock!=='function'||typeof portFactory!=='function')throw Error('LOCAL_KEY_CONFIG');
 var URI='luna://local.owner.r2d2mic',current=null,counter=0,last=-Infinity,closed=false,pending={};
 function state(value){if(typeof options.onState==='function')try{options.onState(value);}catch(_){}}
 function now(){var n=clock();if(typeof n!=='number'||!isFinite(n))throw Error('LOCAL_KEY_CLOCK');if(n<last)n=last;last=n;return n;}
 function device(v){return typeof v==='string'&&v.length>0&&v.length<=128&&/^[\x20-\x7e]+$/.test(v)||typeof v==='number'&&isFinite(v)&&Math.floor(v)===v&&v>=0&&v<=2147483647;}
 function parse(raw){if(typeof raw!=='string'||raw.length>8192)throw Error('LOCAL_REPLY');var row=JSON.parse(raw);if(!row||row.returnValue!==true)throw Error('LOCAL_REPLY');return row;}
 function remove(token){var entry=pending[token];if(!entry)return;delete pending[token];if(existingService.callbackMap[token]===entry){try{existingService.cancel(token);}catch(_){}delete existingService.callbackMap[token];}}
 function ownerCall(job,method,body,callback){
  body.issuedAt=now();
  var token=existingService.call(URI,'/capture/'+method,JSON.stringify(body));
  if(typeof token!=='number'||token<=0||Math.floor(token)!==token||pending[token]||existingService.callbackMap[token])throw Error('LOCAL_TOKEN');
  var entry={isSubscription:false,method:method,deadline:Math.min(job.end+2000,now()+(method==='begin'?10000:2000)),callback:function(raw){
   if(pending[token]!==entry)return;remove(token);
   if(current!==job)return;
   try{if(now()>=entry.deadline)throw Error('LOCAL_REPLY_TIMEOUT');var value=parse(raw);if(value.request!==job.id)throw Error('LOCAL_REPLY');callback(value);}
   catch(_){if(method==='begin')finish(job,true);else{job.ownerAck=true;complete(job);}}
  }};pending[token]=entry;existingService.callbackMap[token]=entry;return token;
 }
 function complete(job){
  if(current!==job||!job.ending||!job.ownerAck||job.port&&!job.port.isRetired())return;
  for(var token in pending)if(pending.hasOwnProperty(token))remove(token);
  current=null;state(closed?'closed':'idle');
 }
 function finish(job,cancel){
  if(current!==job)return;
  job.held=false;
  function tellOwner(cancelled){
   if(current!==job||!cancelled&&job.cancelled)return;
   var method=cancelled?'cancel':'release';if(job.ownerSent===method)return;job.ownerSent=method;
   try{ownerCall(job,method,{request:job.id},function(){job.ownerAck=true;complete(job);});}
   catch(_){job.ownerAck=true;complete(job);}
  }
  if(job.ending&&(!cancel||job.cancelled)){if(cancel&&!job.ownerSent)tellOwner(true);return;}
  job.ending=true;job.cancelled=cancel;state('stopping');
  if(cancel){
   if(job.port){
    try{if(job.started&&job.port.stop(function(){tellOwner(true);complete(job);}))return;}catch(_){}
    try{job.port.cancel();}catch(_){}
   }
   tellOwner(true);return;
  }
  try{
   if(!job.port||!job.port.stop(function(raw){try{parse(raw);tellOwner(false);}catch(_){finish(job,true);}complete(job);}))finish(job,true);
  }catch(_){finish(job,true);}
 }
 function pollInventory(job){
  if(current!==job||!job.held||job.ending||job.started)return;
  job.retryAt=null;job.inventoryTries=(job.inventoryTries||0)+1;
  try{
   job.port.inventory(function(raw){
    if(current!==job||!job.held||job.ending)return;
    var selected=null;
    try{
     var inventory=parse(raw),seen={},choice=null;
     if(!Array.isArray(inventory.devices)||inventory.devices.length>32)throw Error('LOCAL_INVENTORY');
     for(var i=0;i<inventory.devices.length;i++){
      var d=inventory.devices[i];if(!d||typeof d!=='object'||typeof d.id!=='string'||seen[d.id])throw Error('LOCAL_INVENTORY');seen[d.id]=true;
      if(d.deviceType!=='remote')continue;
      if(choice||!/^remote[0-9]{1,8}$/.test(d.id)||typeof d.key!=='string'||d.key.length>128||!/^[\x20-\x7e]*$/.test(d.key))throw Error('LOCAL_REMOTE_AMBIGUOUS');
      choice={id:d.id,key:d.key};
     }
     if(!choice){
      if(job.inventoryTries<12&&now()+150<job.end){job.retryAt=now()+150;return;}
      throw Error('LOCAL_REMOTE_MISSING');
     }
     selected=choice;job.remoteId=choice.id;job.started=true;
     job.port.start(selected,function(startRaw){
      if(current!==job||job.ending)return;
      try{
       var value=parse(startRaw);
       if(value.socketPath!==undefined){if(value.socketPath!=='owner-pipe://voice')throw Error('LOCAL_START');job.socketConfirmed=true;}
       if(value.id!==undefined||value.state!==undefined){if(!job.socketConfirmed||value.id!==job.remoteId||value.state!=='voice start')throw Error('LOCAL_START');}
       if(!job.socketConfirmed)throw Error('LOCAL_START');
       state('listening');
      }
      catch(_){finish(job,true);}
     });
    }catch(_){finish(job,true);}finally{if(selected){selected.id='';selected.key='';}}
   });
  }catch(_){finish(job,true);}
 }
 function begin(deviceId){
  if(closed)return;
  if(current){
   var prev=current;current=null;
   if(prev.port){try{prev.port.cancel();}catch(_){}}
   for(var token in pending)if(pending.hasOwnProperty(token))remove(token);
  }
  counter=counter>=1000000000?1:counter+1;
  var job={id:counter,device:deviceId,held:true,releaseAt:now()+HOLD_MS,end:now()+PRESS_MS,port:null,started:false,socketConfirmed:false,ending:false,cancelled:false,ownerAck:false,inventoryTries:0,retryAt:null};current=job;state('preparing');
  try{ownerCall(job,'begin',{request:job.id,deviceId:deviceId},function(reply){
   if(!job.held||job.ending)return;
   if(reply.providerReady!==true)return finish(job,true);
   job.port=portFactory(existingService,clock,function(){if(current===job)finish(job,true);});
   pollInventory(job);
  });}catch(_){finish(job,true);}
 }
 return {
  key:function(key,pressed,autoRepeat,deviceId){
   if(key!==voiceKey)return false; // all other controls retain their original route
   if(closed)return true;
   try{
    if(typeof pressed!=='boolean'||typeof autoRepeat!=='boolean'||!device(deviceId))throw Error('LOCAL_KEY_EVENT');
    if(autoRepeat)return true;
    if(pressed)begin(deviceId);
    else if(current&&current.held&&current.device===deviceId)finish(current,!current.started);
   }catch(_){if(current)finish(current,true);}
   return true;
  },
  tick:function(){
   try{
    var t=now(),job=current;
    if(job&&job.port)job.port.tick();
    if(job&&current===job&&job.retryAt!==null&&t>=job.retryAt&&job.held&&!job.ending&&!job.started)pollInventory(job);
    if(job&&current===job&&t>=job.releaseAt&&job.held&&!job.ending&&job.started&&job.socketConfirmed)finish(job,false);
    if(job&&current===job&&t>=job.end&&!job.ending)finish(job,true);
    for(var token in pending)if(pending.hasOwnProperty(token)&&t>=pending[token].deadline){var isBegin=pending[token].method==='begin';remove(token);if(job&&current===job){if(isBegin)finish(job,true);else{job.ownerAck=true;complete(job);}}}
    if(job&&current===job){complete(job);if(current===job&&t>=job.end+2500){for(var n in pending)if(pending.hasOwnProperty(n))remove(n);current=null;state(closed?'closed':'unavailable');}}
   }catch(_){if(current)finish(current,true);}
  },
  cancel:function(){if(current)finish(current,true);},
  close:function(){closed=true;if(current)finish(current,true);state('closed');},
  status:function(){return current?(current.ending?'stopping':'preparing'):closed?'closed':'idle';}
 };
}
if(typeof module!=='undefined')module.exports={create:create,HOLD_MS:HOLD_MS,PRESS_MS:PRESS_MS};
