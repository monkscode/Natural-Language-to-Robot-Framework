// Guards against a silently empty coverage map.
//
// `coverage.include` in vite.config.ts can be edited into matching nothing
// (a typo'd extension, a moved directory) and vitest still exits 0: it just
// reports "All files | 0 | 0 | 0 | 0" and every threshold trivially "passes"
// because there is nothing to fail it against. `npm run test:coverage` would
// then be green in CI while measuring zero files. This script reads the
// json-summary reporter's output (vite.config.ts's coverage.reporter, added
// for exactly this purpose - see its comment) and fails loudly whenever the
// map is empty, too small to be believable, or the totals are not real
// numbers.
//
// Referenced by: package.json's `test:coverage` script (chained with `&&`).
// Depends on: coverage/coverage-summary.json, produced by the `json-summary`
// vitest coverage reporter. Nothing else reads this file.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Resolved relative to this script's own location (not process.cwd()) so the
// check works whether invoked from src/frontend-react or anywhere else.
const SUMMARY_PATH = path.resolve(__dirname, '..', 'coverage', 'coverage-summary.json');

// A gross-collapse tripwire, not a coverage target. The real count is 41 as
// of 2026-09-06; 20 leaves deliberate headroom for refactors that legitimately
// shrink the file list, while still catching an `include` pattern that
// matches nothing (or nearly nothing).
const MIN_COVERED_FILES = 20;

// The four metrics vite.config.ts actually gates on (coverage.thresholds).
// branchesTrue is deliberately NOT checked here: a healthy report legitimately
// carries `branchesTrue: {total: 0, covered: 0, pct: 100}`, so checking it
// would fail every good run.
const GATED_METRICS = ['lines', 'statements', 'functions', 'branches'];

function fail(message) {
  console.error(`assert-coverage-not-empty: ${message}`);
  process.exit(1);
}

let raw;
try {
  raw = readFileSync(SUMMARY_PATH, 'utf-8');
} catch (err) {
  fail(
    `${SUMMARY_PATH} does not exist (${err.code}). Check that vite.config.ts's ` +
      "coverage.reporter still includes 'json-summary' and that vitest ran " +
      'with --coverage.'
  );
}

let summary;
try {
  summary = JSON.parse(raw);
} catch (err) {
  fail(`${SUMMARY_PATH} is not valid JSON (${err.message}).`);
}

if (summary === null || typeof summary !== 'object') {
  fail(
    `${SUMMARY_PATH} parsed to ${JSON.stringify(summary)}, not an object. ` +
      "Check that vite.config.ts's coverage.reporter still includes " +
      "'json-summary' and that vitest ran with --coverage."
  );
}

const fileEntries = Object.keys(summary).filter((key) => key !== 'total');

if (fileEntries.length === 0) {
  fail(
    'coverage-summary.json has zero file entries. This usually means ' +
      "vite.config.ts's coverage.include matches no source file - check " +
      'that pattern and the include/exclude globs against src/.'
  );
}

if (fileEntries.length < MIN_COVERED_FILES) {
  fail(
    `coverage-summary.json only covers ${fileEntries.length} file(s), below ` +
      `MIN_COVERED_FILES=${MIN_COVERED_FILES}. Check vite.config.ts's ` +
      'coverage.include/exclude globs for an accidental narrowing.'
  );
}

const total = summary.total ?? {};

if (!(typeof total.lines?.total === 'number' && total.lines.total > 0)) {
  fail(
    'coverage-summary.json total.lines.total is not greater than 0 - no ' +
      'lines were measured. Check coverage.include in vite.config.ts.'
  );
}

for (const metric of GATED_METRICS) {
  const pct = total[metric]?.pct;
  if (!Number.isFinite(pct)) {
    fail(
      `coverage-summary.json total.${metric}.pct is not a finite number ` +
        `(got ${JSON.stringify(pct)}). Check coverage.include in ` +
        'vite.config.ts and that vitest ran with --coverage.'
    );
  }
}

console.log(
  `assert-coverage-not-empty: OK - ${fileEntries.length} file(s) covered, ` +
    `total.lines.total=${total.lines.total}`
);
