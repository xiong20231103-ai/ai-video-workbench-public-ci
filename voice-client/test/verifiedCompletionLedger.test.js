import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  loadVerifiedCompletionLedger,
  persistVerifiedCompletionLedger
} from '../src/verifiedCompletionLedger.js';

test('persists verified completions per canonical task and reuses only exact task items', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'voice-ledger-'));
  const audio = path.join(root, 'de.mp3');
  await fs.writeFile(audio, 'audio');
  const item = {
    taskId: 'remote-current-02', canonicalTaskId: 'current', audioPath: audio,
    expectedVoice: 'Ben', actualActiveVoice: 'Ben', voiceVerified: true
  };
  await persistVerifiedCompletionLedger(root, { batchId: 'batch-1', completedItems: [item] });
  const recovered = await loadVerifiedCompletionLedger(root, [
    { id: 'remote-current-02', canonicalTaskId: 'current', voice: 'Ben' },
    { id: 'remote-other-02', canonicalTaskId: 'other', voice: 'Ben' }
  ]);
  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].taskId, 'remote-current-02');
});

test('rejects wrong active voice, missing files and a different canonical task', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'voice-ledger-reject-'));
  const audio = path.join(root, 'bad.mp3');
  await fs.writeFile(audio, 'audio');
  await persistVerifiedCompletionLedger(root, {
    batchId: 'batch-2',
    completedItems: [{
      taskId: 'remote-current-02', canonicalTaskId: 'current', audioPath: audio,
      expectedVoice: 'Ben', actualActiveVoice: 'wrong', voiceVerified: true
    }]
  });
  const recovered = await loadVerifiedCompletionLedger(root, [
    { id: 'remote-current-02', canonicalTaskId: 'current', voice: 'Ben' }
  ]);
  assert.deepEqual(recovered, []);
});

test('reads a Windows UTF-8 BOM ledger written during state migration', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'voice-ledger-bom-'));
  const audio = path.join(root, 'it.mp3');
  await fs.writeFile(audio, 'audio');
  const canonicalTaskId = 'current';
  const digest = crypto.createHash('sha256').update(canonicalTaskId).digest('hex').slice(0, 12);
  await fs.writeFile(path.join(root, `${canonicalTaskId}-${digest}.json`), `\uFEFF${JSON.stringify({
    version: 1,
    canonicalTaskId,
    completedItems: [{
      taskId: 'remote-current-03', canonicalTaskId, audioPath: audio,
      expectedVoice: 'italiano', actualActiveVoice: 'italiano', voiceVerified: true
    }]
  })}`, 'utf8');
  const recovered = await loadVerifiedCompletionLedger(root, [
    { id: 'remote-current-03', canonicalTaskId, voice: 'italiano' }
  ]);
  assert.equal(recovered.length, 1);
});
