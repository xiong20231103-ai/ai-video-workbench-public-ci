import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('单任务预览使用当前材料且不消耗正式使用记录', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'copy-preview-'));
  const usageFile = path.join(directory, 'copy_usage.json');
  try {
    const payload = Buffer.from(['PREVIEW-001', 'ProjectB', '英语', 'PU石'].join('\x1f'), 'utf8').toString('base64');
    const result = spawnSync(
      process.execPath,
      [path.join(root, 'src', 'previewCopies.js'), '--task-base64', payload],
      {
        cwd: root,
        env: { ...process.env, COPY_USAGE_FILE: usageFile },
        encoding: 'utf8'
      }
    );

    assert.equal(result.status, 0, result.stderr);
    const fields = result.stdout.trim().split('\t').map((value) => Buffer.from(value, 'base64').toString('utf8'));
    assert.equal(fields[0], 'PREVIEW-001');
    assert.ok(fields[1]);
    assert.equal(fields[2], 'PU石');
    assert.match(fields[3], /^PU石-英语-/);
    await assert.rejects(fs.access(usageFile));
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
