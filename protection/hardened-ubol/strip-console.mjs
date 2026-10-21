// Local build tool only. Acorn is not included in the deployed extension.
import { parse } from './audit-parser/package/dist/acorn.mjs';
import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2];
if (!root) throw new Error('OUTPUT_DIRECTORY_REQUIRED');
const changes = [];
function visit(node, edits) {
  if (!node || typeof node !== 'object') return;
  // Replace the extension's own console member expressions, not string literals,
  // filter text, or the page's global console object. Preserve argument evaluation.
  if (node.type === 'MemberExpression' && !node.computed &&
      node.object?.type === 'Identifier' && node.object.name === 'console' &&
      ['log', 'info', 'warn', 'error', 'debug', 'trace', 'assert'].includes(node.property?.name)) {
    edits.push([node.start, node.end, '(()=>{})']);
    return;
  }
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) for (const child of value) visit(child, edits);
    else if (value && typeof value === 'object') visit(value, edits);
  }
}
function walk(dir) {
  for (const entry of fs.readdirSync(dir, {withFileTypes:true})) {
    const file = path.join(dir,entry.name);
    if (entry.isDirectory()) { walk(file); continue; }
    if (!entry.isFile() || !file.endsWith('.js')) continue;
    const before = fs.readFileSync(file,'utf8');
    const ast = parse(before, {ecmaVersion:'latest',sourceType:'module',allowReturnOutsideFunction:true});
    const edits=[]; visit(ast,edits);
    if (!edits.length) continue;
    let after=before;
    for (const [start,end,replacement] of edits.sort((a,b)=>b[0]-a[0]))
      after=after.slice(0,start)+replacement+after.slice(end);
    parse(after,{ecmaVersion:'latest',sourceType:'module',allowReturnOutsideFunction:true});
    fs.writeFileSync(file,after);
    changes.push({path:path.relative(root,file),consoleExpressionsRemoved:edits.length});
  }
}
walk(root);
process.stdout.write(JSON.stringify(changes)+'\n');
