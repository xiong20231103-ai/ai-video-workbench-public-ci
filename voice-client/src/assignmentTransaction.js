import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import readline from 'node:readline';

// Only the lock owner may commit. No parent-side fallback writer is allowed.
export async function assignmentTransaction(file, usageFile, callback, consume = true) {
  if (process.platform !== 'win32') throw new Error('COPY_LOCK_PLATFORM_UNSUPPORTED');
  const host = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    fileURLToPath(new URL('./assignmentTransaction.ps1', import.meta.url)), '-AssignmentFile', file, '-UsageFile', usageFile],
  { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  const messages = []; const waiters = [];
  let ended = false; let failure; let stderr = '';
  function fail(error) { failure = error; while (waiters.length) waiters.shift().reject(error); }
  host.stderr.on('data', b => { stderr += b.toString(); });
  host.stdin.on('error', fail);
  host.once('error', fail);
  const exited = new Promise(resolve => host.once('exit', code => {
    ended = true; fail(new Error(`COPY_LOCK_HOST_EXITED: ${code} ${stderr}`)); resolve();
  }));
  readline.createInterface({ input: host.stdout }).on('line', line => {
    try {
      const item = JSON.parse(line.replace(/^\uFEFF/, ''));
      if (item.type === 'error') return fail(new Error(item.message));
      if (waiters.length) waiters.shift().resolve(item); else messages.push(item);
    } catch (error) { fail(error); }
  });
  function next() {
    if (messages.length) return Promise.resolve(messages.shift());
    if (failure) return Promise.reject(failure);
    return new Promise((resolve, reject) => waiters.push({ resolve, reject }));
  }
  const timer = setTimeout(() => { fail(new Error('COPY_TRANSACTION_TIMEOUT')); host.kill(); }, 30000);
  try {
    const ready = await next();
    if (ready.type !== 'ready') throw new Error('COPY_LOCK_NOT_ACQUIRED');
    const result = await callback(ready);
    if (ended || failure) throw failure || new Error('COPY_LOCK_LOST');
    host.stdin.end(JSON.stringify({ type: consume ? 'commit' : 'abort', assignments: ready.assignments, usage: ready.usage }) + '\n');
    if (consume && (await next()).type !== 'committed') throw new Error('COPY_COMMIT_UNCONFIRMED');
    return result;
  } finally {
    clearTimeout(timer);
    if (!ended) host.kill();
    await exited;
  }
}
