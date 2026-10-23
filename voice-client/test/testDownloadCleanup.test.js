import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { verifyAndDeleteTestDownload } from '../src/testDownloadCleanup.js';

test('deletes an existing prefixed test download', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-test-download-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const filename = path.join(root, 'FishAudio_voice_test_01.mp3');
  await fs.writeFile(filename, 'test-audio');

  const result = await verifyAndDeleteTestDownload(filename, 'FishAudio_voice_test_01', Date.now());

  assert.equal(result.deleted, true);
  await assert.rejects(fs.stat(filename), { code: 'ENOENT' });
});

test('renames and deletes a recent Firefox download', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-test-download-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const tempDir = path.join(root, 'BatchVoiceClientTemp');
  await fs.mkdir(tempDir);
  const filename = path.join(tempDir, 'chand-default-name.mp3');
  const duplicate = path.join(root, 'chand-2026-07-16-This-is-an-English-voice-test.mp3');
  await fs.writeFile(filename, 'test-audio');
  await fs.writeFile(duplicate, 'duplicate-audio');

  const result = await verifyAndDeleteTestDownload(
    filename,
    'FishAudio_voice_test_01',
    Date.now(),
    'This is an English voice test.',
    50
  );

  assert.equal(result.deleted, true);
  assert.equal(result.deletedCount, 2);
  assert.deepEqual(await fs.readdir(tempDir), []);
  assert.deepEqual(await fs.readdir(root), ['BatchVoiceClientTemp']);
});

test('refuses to rename or delete an old unprefixed file', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-test-download-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const filename = path.join(root, 'unrelated.mp3');
  await fs.writeFile(filename, 'keep-me');
  const oldTime = new Date(Date.now() - 60_000);
  await fs.utimes(filename, oldTime, oldTime);

  await assert.rejects(
    verifyAndDeleteTestDownload(filename, 'FishAudio_voice_test_01', Date.now()),
    /拒绝重命名或删除非本次测试文件/
  );
  assert.equal(await fs.readFile(filename, 'utf8'), 'keep-me');
});
