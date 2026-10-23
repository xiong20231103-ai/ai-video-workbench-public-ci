import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { BatchLogger } from '../src/logger.js';

test('成功批次清理批处理日志但保留其他文件', async () => {
  const logDir = await fs.mkdtemp(path.join(os.tmpdir(), 'batch-voice-logs-'));
  try {
    const logger = new BatchLogger(logDir);
    await logger.init();
    await fs.writeFile(path.join(logDir, 'batch-old.log'), 'old\n', 'utf8');
    await fs.writeFile(path.join(logDir, 'notes.txt'), 'keep\n', 'utf8');

    const removed = await logger.cleanupSuccessfulBatchLogs();
    const remaining = await fs.readdir(logDir);

    assert.equal(removed, 2);
    assert.deepEqual(remaining, ['notes.txt']);
  } finally {
    await fs.rm(logDir, { recursive: true, force: true });
  }
});
