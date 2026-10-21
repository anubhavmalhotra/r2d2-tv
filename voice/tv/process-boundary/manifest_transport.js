'use strict';
const processBoundary = require('./joined_process');
// This is only transport. It does not establish manifest ownership or authority.
function manifest(config, hooks = {}) {
  if (!config || !['addOneManifest','removeOneManifest'].includes(config.method) || typeof config.manifestPath !== 'string' || !config.manifestPath.startsWith('/') || config.manifestPath.length > 4096 || config.manifestPath.includes('\0')) throw Error('MANIFEST_CONFIG');
  return processBoundary.run({executable:config.executable,args:['-n','1','-w','3000','luna://com.webos.service.bus/'+config.method,JSON.stringify({path:config.manifestPath,prefix:'/'})],env:config.env,input:null,deadlineMs:3500,termination:'kill'}, value => {
    if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.returnValue !== 'boolean') return null;
    return {returnValue:value.returnValue,errorCode:Number.isSafeInteger(value.errorCode) && Math.abs(value.errorCode) <= 2147483647 ? value.errorCode : null};
  },hooks).then(result => ({...result,ack:result.accepted && result.result.returnValue === true}));
}
module.exports = {manifest};
