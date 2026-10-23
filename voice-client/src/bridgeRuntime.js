import fs from 'node:fs';
import fsPromises from 'node:fs/promises';
import path from 'node:path';
import { writeFileDurable, writeFileDurableSync } from './durableWrite.js';

/**
 * Durable, local-only ownership record for the Fish extension bridge.
 *
 * The Workbench uses this file together with the listener command line before
 * it ever terminates a process on port 5178.  It is deliberately separate
 * from the batch handoff: the handoff describes audio results, while this file
 * describes only the short-lived Node bridge process.
 */
export class FishBridgeRuntime {
  constructor({ filePath, batchId, taskIds, canonicalTaskIds, port, startedAt }) {
    this.filePath = filePath;
    this.base = {
      version: 1,
      batchId,
      taskIds: Array.isArray(taskIds) ? taskIds : [],
      canonicalTaskIds: Array.isArray(canonicalTaskIds) ? canonicalTaskIds : [],
      pid: process.pid,
      port: Number(port),
      startedAt: startedAt || new Date().toISOString(),
      command: process.argv.join(' ')
    };
  }

  payload(state, extra = {}) {
    return {
      ...this.base,
      state,
      updatedAt: new Date().toISOString(),
      ...extra
    };
  }

  async write(state, extra = {}) {
    const payload = this.payload(state, extra);
    await fsPromises.mkdir(path.dirname(this.filePath), { recursive: true });
    const temporary = `${this.filePath}.${process.pid}.tmp`;
    await writeFileDurable(temporary, `${JSON.stringify(payload, null, 2)}\n`);
    await fsPromises.rename(temporary, this.filePath);
    return payload;
  }

  async markTerminal(status, extra = {}) {
    return this.write('terminal', {
      terminalStatus: status,
      completedAt: new Date().toISOString(),
      ...extra
    });
  }

  markTerminalSync(status, extra = {}) {
    try {
      const payload = this.payload('terminal', {
        terminalStatus: status,
        completedAt: new Date().toISOString(),
        ...extra
      });
      fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
      const temporary = `${this.filePath}.${process.pid}.tmp`;
      writeFileDurableSync(temporary, `${JSON.stringify(payload, null, 2)}\n`);
      fs.renameSync(temporary, this.filePath);
    } catch {
      // A signal handler must never turn an orderly Node shutdown into a hang.
    }
  }
}
