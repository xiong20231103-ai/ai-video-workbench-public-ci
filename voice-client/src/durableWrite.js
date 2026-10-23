import fs from 'node:fs';
import fsPromises from 'node:fs/promises';

// Write text and flush it to disk before returning.  Without the flush a
// power cut can leave the file at its full size but all zero bytes: that is
// how last_batch_handoff.json was lost on 2026-10-08.  Callers still write a
// temporary file first and rename it over the target.
export async function writeFileDurable(filePath, text) {
  const handle = await fsPromises.open(filePath, 'w');
  try {
    await handle.writeFile(text, 'utf8');
    await handle.sync();
  } finally {
    await handle.close();
  }
}

export function writeFileDurableSync(filePath, text) {
  const descriptor = fs.openSync(filePath, 'w');
  try {
    fs.writeFileSync(descriptor, text, 'utf8');
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}
