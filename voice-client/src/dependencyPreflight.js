// Builtins only: this file must load even if node_modules is missing/broken.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const requiredVoiceDependencies = Object.freeze(['csv-parse', 'dotenv', 'playwright']);
export const voiceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export async function checkVoiceDependencies(root = voiceRoot) {
  const missing = [];
  const require = createRequire(path.join(path.resolve(root), 'package.json'));
  let modules;
  try {
    const local = path.join(root, 'node_modules');
    if ((await fs.lstat(local)).isSymbolicLink()) throw new Error('nonlocal modules');
    modules = await fs.realpath(local);
  } catch {
    return { ok: false, code: 'VOICE_DEPENDENCIES_MISSING', missing: [...requiredVoiceDependencies] };
  }
  for (const name of requiredVoiceDependencies) {
    try {
      const entry = await fs.realpath(require.resolve(name));
      const relative = path.relative(modules, entry);
      if (!relative || relative.startsWith('..' + path.sep) || relative === '..' || path.isAbsolute(relative)) {
        throw new Error('parent/global fallback refused');
      }
      await import(pathToFileURL(entry).href);
    } catch {
      missing.push(name);
    }
  }
  return { ok: missing.length === 0, code: missing.length ? 'VOICE_DEPENDENCIES_MISSING' : '', missing };
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const result = await checkVoiceDependencies();
  process.stdout.write(JSON.stringify(result) + '\n');
  process.exitCode = result.ok ? 0 : 1;
}
