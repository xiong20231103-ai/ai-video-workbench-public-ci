import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { AtomicJsonFile } from '../src/atomicJsonFile.js';

test('concurrent status writes remain valid JSON and the last queued value wins', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-atomic-json-'));
  const file = path.join(root, 'handoff.json');
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  const writer = new AtomicJsonFile(file);

  await Promise.all(Array.from({ length: 30 }, (_, index) => writer.write({ index, values: [index, index + 1] })));
  await writer.flush();

  const parsed = JSON.parse(await fs.readFile(file, 'utf8'));
  assert.equal(parsed.index, 29);
  assert.deepEqual(parsed.values, [29, 30]);
  const backup = JSON.parse(await fs.readFile(`${file}.last-valid`, 'utf8'));
  assert.equal(backup.index, 28);
});
