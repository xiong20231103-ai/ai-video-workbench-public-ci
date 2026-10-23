import fs from 'node:fs/promises';
import path from 'node:path';
import { writeFileDurable } from './durableWrite.js';

export class AtomicJsonFile {
  constructor(filePath) {
    this.filePath = filePath;
    this.queue = Promise.resolve();
    this.sequence = 0;
  }

  write(payload) {
    const operation = this.queue.then(() => this.#writeNow(payload));
    // A failed write must reject its caller but must not permanently poison
    // later status updates.
    this.queue = operation.catch(() => {});
    return operation;
  }

  async flush() {
    await this.queue;
  }

  async #writeNow(payload) {
    const serialized = `${JSON.stringify(payload, null, 2)}\n`;
    JSON.parse(serialized);
    await fs.mkdir(path.dirname(this.filePath), { recursive: true });

    const suffix = `${process.pid}.${Date.now()}.${this.sequence += 1}`;
    const temporary = `${this.filePath}.tmp.${suffix}`;
    const lastValid = `${this.filePath}.last-valid`;
    try {
      try {
        const previous = await fs.readFile(this.filePath, 'utf8');
        JSON.parse(previous.replace(/^\uFEFF/, ''));
        await writeFileDurable(lastValid, previous);
      } catch (error) {
        if (error?.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
      }

      await writeFileDurable(temporary, serialized);
      JSON.parse(await fs.readFile(temporary, 'utf8'));
      await fs.rename(temporary, this.filePath);
      JSON.parse((await fs.readFile(this.filePath, 'utf8')).replace(/^\uFEFF/, ''));
    } catch (error) {
      await fs.rm(temporary, { force: true }).catch(() => {});
      throw error;
    }
  }
}
