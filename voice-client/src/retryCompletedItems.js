import fs from 'node:fs/promises';
import { loadVerifiedCompletionLedger } from './verifiedCompletionLedger.js';

export async function loadRetryCompletedItems(handoffFile, ledgerDir, tasks, projectOutputDirs = new Map()) {
  let previous;
  try {
    previous = JSON.parse(await fs.readFile(handoffFile, 'utf8'));
  } catch (error) {
    if (error?.code === 'ENOENT' || error instanceof SyntaxError) return [];
    throw error;
  }
  const requested = new Map(tasks.map((task) => [String(task.id || ''), task]));
  const recoveredByTaskId = new Map(
    (await loadVerifiedCompletionLedger(ledgerDir, tasks))
      .map((item) => [String(item.taskId || ''), item])
  );
  for (const item of previous.completedItems || []) {
    if (!item || !requested.has(String(item.taskId || ''))) continue;
    const task = requested.get(String(item.taskId || ''));
    const expectedVoice = String(task.voice || '');
    const legacyDefaultVoice = expectedVoice.toLowerCase() === 'default';
    if ((!legacyDefaultVoice && item.voiceVerified !== true) ||
      (!legacyDefaultVoice && String(item.expectedVoice || '') !== expectedVoice) ||
      (!legacyDefaultVoice && String(item.actualActiveVoice || '') !== expectedVoice)) {
      continue;
    }
    const audioPath = String(item.audioPath || '');
    try {
      const stat = await fs.stat(audioPath);
      if (!stat.isFile() || stat.size <= 0) continue;
    } catch {
      continue;
    }
    recoveredByTaskId.set(String(item.taskId || ''), {
      ...item,
      canonicalTaskId: String(item.canonicalTaskId || task.canonicalTaskId || '')
    });
  }

  // Never infer completion from a same-named MP3 on disk.  An interrupted or
  // wrong-voice Fish attempt can leave such a file behind; only a handoff item
  // that was accepted by the bridge after active-voice verification is valid.
  return [...recoveredByTaskId.values()];
}

