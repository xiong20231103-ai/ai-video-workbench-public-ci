import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { writeFileDurable, writeFileDurableSync } from '../src/durableWrite.js';

test('durable writes replace the whole file content (async and sync)', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-durable-write-'));
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  const file = path.join(root, 'state.json');

  await writeFileDurable(file, '{"long":"' + 'x'.repeat(200) + '"}\n');
  await writeFileDurable(file, '{"a":1}\n');
  assert.equal(await fs.readFile(file, 'utf8'), '{"a":1}\n');

  writeFileDurableSync(file, '{"中文":"配音"}\n');
  assert.deepEqual(JSON.parse(await fs.readFile(file, 'utf8')), { 中文: '配音' });
});

test('a failed durable write leaves no open handle behind', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-durable-write-'));
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  const missingDir = path.join(root, 'missing', 'state.json');
  await assert.rejects(writeFileDurable(missingDir, '{}'), { code: 'ENOENT' });
  assert.throws(() => writeFileDurableSync(missingDir, '{}'), { code: 'ENOENT' });
});
