import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { CopySelector } from '../src/copySelector.js';
import { isoWeekTag } from '../src/weeklyCopy.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const libraryFile = path.join(root, 'data', 'copy_library.csv');
const rulesFile = path.join(root, 'data', 'project_copy_rules.csv');
const weeklyRulesFile = path.join(root, 'weekly-copy', 'rules.json');
const MONDAY_W44 = new Date(2026, 9, 26, 9, 0, 0);

async function weekCsv(week, mutate = (rows) => rows) {
  const rules = JSON.parse(await fs.readFile(weeklyRulesFile, 'utf8'));
  const tag = week.replace('-', '');
  let rows = [];
  for (const material of Object.keys(rules.materials)) {
    const languages = [...rules.baseLanguages];
    for (const [language, materials] of Object.entries(rules.scopedLanguages)) {
      if (materials.includes(material)) languages.push(language);
    }
    for (const language of languages) {
      for (let n = 1; n <= rules.perWeekPerMaterialLanguage; n += 1) {
        rows.push([`weekly-${week}`, material, language, `${material}-${language}-${tag}-${n}`, `title ${n}`, `weekly text ${material} ${language} ${n}`]);
      }
    }
  }
  rows = mutate(rows);
  return [rules.columns.join(','), ...rows.map((row) => row.map((v) => `"${v}"`).join(','))].join('\n') + '\n';
}

async function setup({ week = '2026-W44', csv, sidecar } = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'weekly-copy-'));
  const weeklyDir = path.join(directory, 'weekly');
  await fs.mkdir(weeklyDir);
  if (csv !== null) {
    const body = csv ?? await weekCsv(week);
    await fs.writeFile(path.join(weeklyDir, `weekly-${week}.csv`), body);
    const sha256 = createHash('sha256').update(body).digest('hex');
    if (sidecar !== null) {
      await fs.writeFile(path.join(weeklyDir, `weekly-${week}.accepted.json`), JSON.stringify(sidecar ?? { week, sha256 }));
    }
  }
  return { directory, weeklyDir, usageFile: path.join(directory, 'copy_usage.json') };
}

function selector({ weeklyDir, usageFile }, now = MONDAY_W44) {
  return new CopySelector({ libraryFile, rulesFile, usageFile, weeklyDir, weeklyRulesFile, now: () => now, random: () => 0 }).init();
}

test('ISO week tags match the calendar the company PC uses', () => {
  assert.equal(isoWeekTag(new Date(2026, 9, 20)), '2026-W43');
  assert.equal(isoWeekTag(new Date(2026, 9, 26)), '2026-W44');
  assert.equal(isoWeekTag(new Date(2026, 10, 1, 23, 59)), '2026-W44');
  assert.equal(isoWeekTag(new Date(2027, 0, 1)), '2026-W53');
});

test('an accepted week replaces the regular pools but never ExampleBrand', async () => {
  const copies = await selector(await setup());
  assert.equal(copies.weekly.active, true);
  assert.equal(copies.pools.get('净白板|英语').length, 6);
  assert.ok(copies.pools.get('塑木地板|俄语').every((row) => row.source === 'weekly-2026-W44'));
  assert.ok(copies.pools.get('清淤泵|英语').every((row) => row.source !== 'weekly-2026-W44'));
  const chosen = await copies.select({ id: '001', project: 'ProjectB', language: '英语', text: '', outputName: '' });
  assert.equal(chosen.copySource, 'weekly-2026-W44');
  assert.match(chosen.copyId, /-2026W44-/);
  assert.equal(chosen.copyLibraryVersion, '');
});

test('another week, a missing record or edited bytes fall back to the base library', async () => {
  const before = await selector(await setup(), new Date(2026, 9, 25, 23, 59)); // Sunday of W43
  assert.equal(before.weekly.active, false);
  assert.equal(before.pools.get('净白板|英语').length, 5);
  const noRecord = await selector(await setup({ sidecar: null }));
  assert.equal(noRecord.weekly.active, false);
  const edited = await setup();
  await fs.appendFile(path.join(edited.weeklyDir, 'weekly-2026-W44.csv'), '"x"\n');
  const changed = await selector(edited);
  assert.equal(changed.weekly.active, false);
  assert.match(changed.weekly.reason, /不一致/);
  assert.equal(changed.pools.get('净白板|英语').length, 5);
});

test('a structurally incomplete week is refused', async () => {
  const short = await selector(await setup({ csv: await weekCsv('2026-W44', (rows) => rows.slice(1)) }));
  assert.equal(short.weekly.active, false);
  assert.match(short.weekly.reason, /应有 6 条/);
  const wrongSource = await selector(await setup({ csv: await weekCsv('2026-W44', (rows) => rows.map((r) => ['weekly-2026-W43', ...r.slice(1)])) }));
  assert.equal(wrongSource.weekly.active, false);
});

test('copy bound before Monday keeps its saved text after the switch', async () => {
  const paths = await setup();
  const task = { id: 'spc-01', canonicalTaskId: 'stable-spc-01', project: 'SPC地板', material: '塑木地板', language: '英语', text: '', outputName: '' };
  const sunday = await selector(paths, new Date(2026, 9, 25, 23, 0));
  const first = await sunday.select(task);
  assert.notEqual(first.copySource, 'weekly-2026-W44');
  const monday = await selector(paths);
  const again = await monday.select(task);
  assert.equal(again.selectionMode, 'persisted');
  assert.equal(again.text, first.text);
  assert.equal(again.copyId, first.copyId);
});
