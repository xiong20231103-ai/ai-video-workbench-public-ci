import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { FishBridgeRuntime } from '../src/bridgeRuntime.js';

test('Fish bridge runtime records batch ownership and a terminal state', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-bridge-runtime-'));
  const filePath = path.join(directory, 'fish_bridge_runtime.json');
  const runtime = new FishBridgeRuntime({
    filePath,
    batchId: 'batch-test',
    taskIds: ['voice-1'],
    canonicalTaskIds: ['parent-1'],
    port: 5178,
    startedAt: '2026-07-23T12:00:00.000Z'
  });
  await runtime.write('listening');
  await runtime.markTerminal('failed', { message: 'EADDRINUSE test' });
  const saved = JSON.parse(await fs.readFile(filePath, 'utf8'));
  assert.equal(saved.batchId, 'batch-test');
  assert.deepEqual(saved.canonicalTaskIds, ['parent-1']);
  assert.equal(saved.port, 5178);
  assert.equal(saved.state, 'terminal');
  assert.equal(saved.terminalStatus, 'failed');
  await fs.rm(directory, { recursive: true, force: true });
});
