import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { checkVoiceDependencies, requiredVoiceDependencies } from '../src/dependencyPreflight.js';

const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src');
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'voice-preflight-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(path.join(root, 'package.json'), JSON.stringify({ type: 'module' }));
  return root;
}
async function moduleAt(root, name, body = 'module.exports = {};') {
  const folder = path.join(root, 'node_modules', name);
  await fs.mkdir(folder, { recursive: true });
  await fs.writeFile(path.join(folder, 'package.json'), JSON.stringify({ name, main: 'index.cjs' }));
  await fs.writeFile(path.join(folder, 'index.cjs'), body);
}
async function entryAt(root) {
  await fs.mkdir(path.join(root, 'src'));
  for (const name of ['dependencyPreflight.js', 'runBatch.js']) {
    await fs.copyFile(path.join(source, name), path.join(root, 'src', name));
  }
  await fs.writeFile(path.join(root, 'src/runBatchImpl.js'),
    `import fs from 'node:fs'; fs.writeFileSync(new URL('../entered.json', import.meta.url), JSON.stringify(process.argv.slice(2)));`);
}

test('missing node_modules lists all required modules without writing state', async (t) => {
  const root = await fixture(t);
  assert.deepEqual(await checkVoiceDependencies(root), { ok: false, code: 'VOICE_DEPENDENCIES_MISSING', missing: [...requiredVoiceDependencies] });
  assert.deepEqual((await fs.readdir(root)).sort(), ['package.json']);
});
test('all three local modules must really import; a broken module is rejected', async (t) => {
  const root = await fixture(t);
  for (const name of requiredVoiceDependencies) await moduleAt(root, name);
  assert.equal((await checkVoiceDependencies(root)).ok, true);
  const broken = await fixture(t);
  for (const name of requiredVoiceDependencies) await moduleAt(broken, name, name === 'dotenv' ? `throw new Error('inert-detail-not-for-output');` : undefined);
  assert.deepEqual((await checkVoiceDependencies(broken)).missing, ['dotenv']);
});
test('parent/global modules cannot disguise empty local node_modules', async (t) => {
  const parent = await fixture(t);
  for (const name of requiredVoiceDependencies) await moduleAt(parent, name);
  const child = path.join(parent, 'child');
  await fs.mkdir(path.join(child, 'node_modules'), { recursive: true });
  assert.deepEqual((await checkVoiceDependencies(child)).missing, [...requiredVoiceDependencies]);
});
test('public batch entry blocks before importing any batch implementation', async (t) => {
  const root = await fixture(t); await entryAt(root);
  const result = spawnSync(process.execPath, [path.join(root, 'src/runBatch.js'), '--dry-run'], { cwd: root, encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, 1); assert.match(result.stderr, /VOICE_DEPENDENCIES_MISSING/);
  await assert.rejects(fs.stat(path.join(root, 'entered.json')), { code: 'ENOENT' });
});
test('healthy batch entry keeps the original arguments unchanged', async (t) => {
  const root = await fixture(t); await entryAt(root);
  for (const name of requiredVoiceDependencies) await moduleAt(root, name);
  const result = spawnSync(process.execPath, [path.join(root, 'src/runBatch.js'), '--dry-run', '--slow', '250'], { cwd: root, encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(root, 'entered.json'), 'utf8')), ['--dry-run', '--slow', '250']);
});
test('probe CLI prints only bounded module names, no exception/config details', async (t) => {
  const root = await fixture(t); await entryAt(root);
  for (const name of requiredVoiceDependencies) await moduleAt(root, name, name === 'playwright' ? `throw new Error('inert-detail-not-for-output');` : undefined);
  const result = spawnSync(process.execPath, [path.join(root, 'src/dependencyPreflight.js')], { cwd: root, encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, 1); assert.deepEqual(JSON.parse(result.stdout).missing, ['playwright']);
  assert.doesNotMatch(result.stdout + result.stderr, /inert-detail-not-for-output/);
});
