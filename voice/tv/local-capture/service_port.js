/* Authored transport adapter for the ALREADY EXISTING keyfilter Service.
 * It grants no permission, creates no Service, and opens no audio socket.
 * The native route must qualify the caller, logging, ownership and watchdog
 * before invoking capture methods. This module is inert until explicitly used.
 */
function create(existingService, clock, outcome) {
    if (!existingService || typeof existingService.call !== "function" ||
        typeof existingService.cancel !== "function" || !existingService.callbackMap ||
        typeof clock !== "function" || typeof outcome !== "function") throw new Error("SERVICE_PORT_CONFIG");
    var URI = "luna://com.webos.service.voiceinput.hidraw";
    var created = clock(), last = created, end = created + 10000;
    var pending = {}, tokens = [], record = null, started = false, stopped = false, retired = false;
    var startToken = 0, stopDue = null, inventoryPending = false, inventoryCount = 0;
    function now() {
        var n = clock();
        if (typeof n !== "number" || !isFinite(n)) throw new Error("SERVICE_PORT_CLOCK");
        if (n < last) n = last;
        last = n; return n;
    }
    if (typeof created !== "number" || !isFinite(created)) throw new Error("SERVICE_PORT_CLOCK");
    function boundedPayload(value) {
        if(typeof value!=="string" || value.length>8192)return false;
        var bytes=0;
        for(var i=0;i<value.length;i++){
            var c=value.charCodeAt(i);
            if(c<128)bytes++;else if(c<2048)bytes+=2;
            else if(c>=0xd800 && c<=0xdbff){
                if(i+1>=value.length)return false;
                var next=value.charCodeAt(++i);if(next<0xdc00 || next>0xdfff)return false;bytes+=4;
            }else if(c>=0xdc00 && c<=0xdfff)return false;else bytes+=3;
            if(bytes>8192)return false;
        }
        return true;
    }
    function report(code) { try { outcome(code); } catch (_) {} }
    function remove(token) {
        var entry = pending[token];
        if (!entry) return;
        delete pending[token];
        // Cancel only entries whose callback object is still ours.
        if (existingService.callbackMap[token] === entry.binding) {
            try { existingService.cancel(token); } catch (_) { report("CANCEL_UNKNOWN"); }
            delete existingService.callbackMap[token];
        }
    }
    function cleanup() {
        if (retired) return;
        retired = true;
        for (var i=0;i<tokens.length;i++) remove(tokens[i]);
        tokens = []; record = null;
    }
    function invoke(method, body, subscription, callback, phase) {
        if (retired) throw new Error("SERVICE_PORT_RETIRED");
        var n=now(); if (phase !== "stop" && n >= end) throw new Error("SERVICE_PORT_EXPIRED");
        var token=existingService.call(URI,method,JSON.stringify(body));
        if (typeof token !== "number" || token <= 0 || token % 1 || pending[token] || existingService.callbackMap[token]) {
            report("CALL_UNKNOWN"); throw new Error("SERVICE_PORT_TOKEN");
        }
        var entry={binding:null,deadline:Math.min(phase === "stop" ? n+2000 : end,n+2000),phase:phase,initial:true};
        var binding={isSubscription:subscription,callback:function(payload) {
            if (pending[token] !== entry || retired) return;
            var t;
            try { t=now(); } catch (_) { cleanup();report("CLOCK_UNKNOWN");return; }
            if (t >= entry.deadline || !boundedPayload(payload)) {
                if(phase === "start") { entry.initial=false;entry.deadline=end; report("START_UNKNOWN");stop(); }
                else { if(phase === "inventory")inventoryPending=false;remove(token);if(phase === "stop")cleanup();report("REPLY_UNKNOWN"); }
                return;
            }
            if(!subscription) delete pending[token];
            else { entry.initial=false;entry.deadline=end; }
            try { callback(payload); } catch (_) { report("CALLBACK_FAILED"); }
            if(phase === "stop") { stopped=true;cleanup(); }
        }};
        entry.binding=binding;pending[token]=entry;tokens.push(token);existingService.callbackMap[token]=binding;
        return token;
    }
    function stop(callback) {
        if (retired || stopped || stopDue !== null) return false;
        if (!started || !record) { cleanup();return false; }
        var n;
        try { n=now(); } catch (_) { cleanup();report("STOP_UNKNOWN");return false; }
        stopDue=n+2000;
        try { invoke("/stopStreaming",{id:record.id,key:record.key},false,callback||function(){},"stop"); }
        catch (_) { cleanup();report("STOP_UNKNOWN");return false; }
        return true;
    }
    return {
        inventory:function(callback) {
            if(started || inventoryPending || inventoryCount>=12 || typeof callback!=="function")throw new Error("INVENTORY_SCOPE");
            inventoryPending=true;inventoryCount++;
            return invoke("/getDevices",{},false,function(payload){inventoryPending=false;callback(payload);},"inventory");
        },
        start:function(selected,callback) {
            if(started || !selected || typeof callback!=="function" ||
                typeof selected.id!=="string" || !/^remote[0-9]{1,8}$/.test(selected.id) ||
                typeof selected.key!=="string" || selected.key.length>128 || !/^[\x20-\x7e]*$/.test(selected.key)) throw new Error("CAPTURE_SCOPE");
            // selected must come from a separately bound actual physical remote.
            // This validation does not establish that selection or socket ownership.
            record={id:selected.id,key:selected.key};started=true;
            try { startToken=invoke("/startStreaming",{id:record.id,key:record.key,socketType:"socket",subscribe:true},true,callback,"start"); }
            catch (_) { report("START_UNKNOWN");stop();throw new Error("START_UNKNOWN"); }
            return startToken;
        },
        stop:stop,
        tick:function() {
            if(retired)return;
            var n;try{n=now();}catch(_){cleanup();report("STOP_UNKNOWN");return;}
            if(stopDue!==null && n>=stopDue){cleanup();report("STOP_UNKNOWN");return;}
            if(n>=end){stop();return;}
            for(var i=0;i<tokens.length;i++){
                var token=tokens[i],entry=pending[token];
                if(entry && n>=entry.deadline){
                    if(entry.phase==="start"){entry.initial=false;entry.deadline=end;report("START_UNKNOWN");stop();return;}
                    if(entry.phase==="inventory")inventoryPending=false;
                    remove(token);report("REPLY_UNKNOWN");
                }
            }
        },
        cancel:function(){return stop();},
        isRetired:function(){return retired;}
    };
}
if(typeof module!=="undefined")module.exports={create:create};
