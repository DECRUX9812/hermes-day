// node:vm harness for the pure view-model block inside desktop/plugin.js.
//
// The bundle imports the desktop SDK (`@hermes/plugin-sdk`, `react`), so it can
// never be imported directly from a bare Node process. Instead we extract the
// marked `hday:viewmodels` block VERBATIM from the shipped source and evaluate
// it under node:vm — the exact bytes the desktop runs are the bytes under test.
//
// Usage: node tests/node_ledger_driver.cjs <plugin.js> <expr-file>
//   <expr-file> holds a JS expression evaluated with the view-model functions
//   in scope; its value is JSON.stringify'd to stdout (null when undefined).
//
// Exit 2 = the marked block is missing from the bundle (i.e. feature absent).

const vm = require('node:vm')
const fs = require('node:fs')

const src = fs.readFileSync(process.argv[2], 'utf8')
const m = src.match(/\/\* hday:viewmodels-begin \*\/([\s\S]*?)\/\* hday:viewmodels-end \*\//)
if (!m) {
  console.error('hday:viewmodels block not found in ' + process.argv[2])
  process.exit(2)
}
const expr = fs.readFileSync(process.argv[3], 'utf8')
const out = vm.runInNewContext(m[1] + '\n;(' + expr + '\n)', Object.create(null))
process.stdout.write(JSON.stringify(out === undefined ? null : out))
