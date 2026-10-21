'use strict';
// The legacy loader optionally calls mod.run(name). Export no run function;
// start only when this package is loaded by the genuine standalone bootstrap.
module.exports={};
const BOOT='/usr/palm/services/jsservicelauncher/bootstrap-node.js';
if(require.main&&require.main.filename===BOOT){
 if(!module.parent||module.parent.filename!==BOOT)throw Error('LOCAL_CAPTURE_BUS_CONTEXT');
 const args=require('./bus_context').entryArguments();
 // main.run owns the inherited descriptor through final native cleanup.
 Promise.resolve(require('./main').run(args.configPath,args.heldOriginalFd)).catch(()=>{process.exitCode=1;});
}else if(require.main===module){
 process.exitCode=1;
}
