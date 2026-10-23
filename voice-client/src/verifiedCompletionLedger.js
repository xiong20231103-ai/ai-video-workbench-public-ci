import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { writeFileDurable } from './durableWrite.js';

function ledgerName(canonicalTaskId) {
  const readable = String(canonicalTaskId || '').trim().replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 96);
  const digest = crypto.createHash('sha256').update(String(canonicalTaskId || '')).digest('hex').slice(0, 12);
  return `${readable || 'unknown'}-${digest}.json`;
}

function canonicalIdFor(item, task) {
  return String(item?.canonicalTaskId || task?.canonicalTaskId || '').trim();
}

export function isVerifiedCompletion(item, task) {
  if (!item || !task || String(item.taskId || '') !== String(task.id || '')) return false;
  const expectedVoice = String(task.voice || '');
  const legacyDefaultVoice = expectedVoice.toLowerCase() === 'default';
  if (!legacyDefaultVoice && item.voiceVerified !== true) return false;
  if (!legacyDefaultVoice && String(item.expectedVoice || '') !== expectedVoice) return false;
  if (!legacyDefaultVoice && String(item.actualActiveVoice || '') !== expectedVoice) return false;
  const itemCanonical = canonicalIdFor(item, task);
  const taskCanonical = String(task.canonicalTaskId || '').trim();
  return Boolean(itemCanonical && taskCanonical && itemCanonical === taskCanonical);
}

async function validAudioFile(audioPath) {
  try {
    const stat = await fs.stat(String(audioPath || ''));
    return stat.isFile() && stat.size > 0;
  } catch {
    return false;
  }
}

async function readLedger(filePath) {
  try {
    const text = await fs.readFile(filePath, 'utf8');
    const parsed = JSON.parse(text.replace(/^\uFEFF/, ''));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (error) {
    if (error?.code === 'ENOENT' || error instanceof SyntaxError) return {};
    throw error;
  }
}

async function atomicWriteJson(filePath, payload) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const temp = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  await writeFileDurable(temp, `${JSON.stringify(payload, null, 2)}\n`);
  await fs.rename(temp, filePath);
}

export async function loadVerifiedCompletionLedger(ledgerDir, tasks) {
  const requested = new Map(tasks.map((task) => [String(task.id || ''), task]));
  const canonicalIds = [...new Set(tasks.map((task) => String(task.canonicalTaskId || '').trim()).filter(Boolean))];
  const recovered = new Map();
  for (const canonicalTaskId of canonicalIds) {
    const ledger = await readLedger(path.join(ledgerDir, ledgerName(canonicalTaskId)));
    if (String(ledger.canonicalTaskId || '') !== canonicalTaskId) continue;
    for (const item of ledger.completedItems || []) {
      const task = requested.get(String(item?.taskId || ''));
      if (!isVerifiedCompletion(item, task)) continue;
      if (!(await validAudioFile(item.audioPath))) continue;
      recovered.set(String(item.taskId), { ...item, canonicalTaskId });
    }
  }
  return [...recovered.values()];
}

export async function persistVerifiedCompletionLedger(ledgerDir, payload) {
  const grouped = new Map();
  for (const item of payload?.completedItems || []) {
    const canonicalTaskId = String(item?.canonicalTaskId || '').trim();
    if (!canonicalTaskId || item?.voiceVerified !== true) continue;
    if (String(item.expectedVoice || '') !== String(item.actualActiveVoice || '')) continue;
    if (!(await validAudioFile(item.audioPath))) continue;
    if (!grouped.has(canonicalTaskId)) grouped.set(canonicalTaskId, []);
    grouped.get(canonicalTaskId).push(item);
  }
  for (const [canonicalTaskId, items] of grouped) {
    const filePath = path.join(ledgerDir, ledgerName(canonicalTaskId));
    const previous = await readLedger(filePath);
    const merged = new Map();
    for (const item of previous.completedItems || []) {
      if (item?.taskId) merged.set(String(item.taskId), item);
    }
    for (const item of items) merged.set(String(item.taskId), item);
    await atomicWriteJson(filePath, {
      version: 1,
      canonicalTaskId,
      updatedAt: new Date().toISOString(),
      sourceBatchId: String(payload.batchId || ''),
      completedItems: [...merged.values()]
    });
  }
}
