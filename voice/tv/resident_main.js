'use strict';
// webOS bootstrap-node.js loads the application's main file as a dependency.
// This entry intentionally starts it; importing resident_mic alone stays inert.
require('./resident_mic').run().catch(()=>{process.exitCode=1;});
