// Prints the raw and gzipped size of dist/widget.js and fails the build if over budget.
import { readFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';

const BUDGET_GZIP_KB = 30;
const FILE = new URL('../dist/widget.js', import.meta.url);

const raw = readFileSync(FILE);
const gzipKb = gzipSync(raw, { level: 9 }).length / 1024;

console.log(`widget.js: ${(raw.length / 1024).toFixed(1)} KB raw, ${gzipKb.toFixed(1)} KB gzipped (budget ${BUDGET_GZIP_KB} KB)`);

if (gzipKb > BUDGET_GZIP_KB) {
  console.error('Over budget!');
  process.exit(1);
}
