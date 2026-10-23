import fs from 'node:fs/promises';
import path from 'node:path';

let consolePipeBroken = false;
// stdout errors are emitted asynchronously, so a try/catch around write alone
// cannot prevent Node from terminating when a detached monitor closes its pipe.
process.stdout?.on?.('error', (error) => {
  if (error?.code === 'EPIPE') {
    consolePipeBroken = true;
    return;
  }
  // Batch logs are persisted to disk before stdout is touched.  Do not let a
  // nonessential console sink make an unattended production job fail.
  consolePipeBroken = true;
});

export class BatchLogger {
  constructor(logDir) {
    this.logDir = logDir;
    this.logFile = path.join(logDir, `batch-${timestampForFile()}.log`);
  }

  async init() {
    await fs.mkdir(this.logDir, { recursive: true });
    await this.write('INFO', 'batch started');
  }

  async info(message, meta = {}) {
    await this.write('INFO', message, meta);
  }

  async error(message, meta = {}) {
    await this.write('ERROR', message, meta);
  }

  async cleanupSuccessfulBatchLogs() {
    let entries;
    try {
      entries = await fs.readdir(this.logDir, { withFileTypes: true });
    } catch (error) {
      if (error?.code === 'ENOENT') return 0;
      throw error;
    }

    const batchLogs = entries
      .filter((entry) => entry.isFile() && /^batch-.*\.log$/i.test(entry.name))
      .map((entry) => path.join(this.logDir, entry.name));
    await Promise.all(batchLogs.map((file) => fs.unlink(file)));
    return batchLogs.length;
  }

  async write(level, message, meta = {}) {
    const line = JSON.stringify({
      time: new Date().toISOString(),
      level,
      message,
      ...meta
    });
    await fs.appendFile(this.logFile, `${line}\n`, 'utf8');
    // The unattended client is launched without a durable console.  A parent
    // process (or a test monitor) may close stdout while Fish is still
    // working; logging must never make the production batch crash with EPIPE.
    try {
      if (!consolePipeBroken && process.stdout?.writable && !process.stdout.destroyed) {
        process.stdout.write(`[${level}] ${message}\n`);
      }
    } catch (error) {
      if (error?.code !== 'EPIPE') throw error;
    }
  }
}

function timestampForFile() {
  return new Date().toISOString().replace(/[:.]/g, '-');
}
