import fs from 'node:fs/promises';
import path from 'node:path';
import { parse } from 'csv-parse/sync';

export async function loadProjectOutputDirs(file, { now = new Date() } = {}) {
  try {
    const content = await fs.readFile(file, 'utf8');
    const rows = parse(content, { columns: true, skip_empty_lines: true, trim: true, bom: true });
    const result = new Map();
    for (const row of rows) {
      const project = String(row.project ?? '').trim();
      const material = String(row.material ?? '').trim();
      const outputDir = String(row.outputDir ?? '').trim();
      const baseDir = String(row.baseDir ?? '').trim();
      const folderName = safeSegment(row.folderName);
      const dateMode = String(row.dateMode ?? '').trim().toLowerCase();
      const manualDate = safeSegment(row.dateValue);
      if (!project) continue;
      if (baseDir) {
        const parts = [path.resolve(baseDir)];
        if (folderName) parts.push(folderName);
        if (dateMode === 'auto') parts.push(formatDateFolder(now));
        else if (dateMode === 'manual' && manualDate) parts.push(manualDate);
        result.set(outputDirectoryKey(project, material), path.join(...parts));
      } else if (outputDir) {
        result.set(outputDirectoryKey(project, material), path.resolve(outputDir));
      }
    }
    return result;
  } catch (error) {
    if (error?.code === 'ENOENT') return new Map();
    throw error;
  }
}

export function outputDirectoryKey(project, material = '') {
  const cleanProject = String(project ?? '').trim();
  const cleanMaterial = String(material ?? '').trim();
  return cleanMaterial ? `${cleanProject}\u0000${cleanMaterial}` : cleanProject;
}

function formatDateFolder(date) {
  return `${date.getMonth() + 1}.${date.getDate()}`;
}

function safeSegment(value) {
  const safe = String(value ?? '').replace(/[<>:"/\\|?*\x00-\x1F]/g, '_').replace(/^\.+$/, '_').trim();
  return safe;
}
