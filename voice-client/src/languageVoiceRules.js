import fs from 'node:fs/promises';
import { parse } from 'csv-parse/sync';

export async function loadLanguageVoiceRules(file) {
  const content = await fs.readFile(file, 'utf8');
  const rows = parse(content.replace(/\r\n?/g, '\n'), {
    columns: true,
    skip_empty_lines: true,
    trim: true,
    bom: true
  });
  return new Map(rows
    .map((row) => [String(row.language ?? '').trim(), String(row.voice ?? '').trim()])
    .filter(([language, voice]) => language && voice));
}

export function applyLanguageVoiceRules(tasks, rules) {
  return tasks.map((task) => {
    const current = String(task.voice ?? '').trim();
    const mapped = rules.get(String(task.language ?? '').trim());
    return current && current.toLowerCase() !== 'default' ? task : { ...task, voice: mapped || current || 'default' };
  });
}
