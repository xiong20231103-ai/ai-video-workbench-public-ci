import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { parse } from 'csv-parse/sync';

// Weekly copy written on the company PC and imported by weekly-copy/import_week.py.
// The importer runs the full Python checker and records the accepted file's
// SHA-256; here we only confirm that the accepted bytes are unchanged and that
// every pool has exactly the agreed number of rows. Anything else falls back
// to the base library, so a bad week never stops production.

export function isoWeekTag(date = new Date()) {
  const day = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const weekday = day.getUTCDay() || 7;
  day.setUTCDate(day.getUTCDate() + 4 - weekday);
  const yearStart = new Date(Date.UTC(day.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((day - yearStart) / 86400000 + 1) / 7);
  return `${day.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
}

export async function loadWeeklyLibrary({ weeklyDir, rulesFile, week }) {
  const csvFile = path.join(weeklyDir, `weekly-${week}.csv`);
  const acceptedFile = path.join(weeklyDir, `weekly-${week}.accepted.json`);
  let raw;
  let accepted;
  try {
    [raw, accepted] = await Promise.all([fs.readFile(csvFile), fs.readFile(acceptedFile, 'utf8')]);
  } catch (error) {
    if (error?.code === 'ENOENT') return { week, active: false, reason: '本周没有已导入的周文案，使用基础文案库' };
    throw error;
  }
  try {
    accepted = JSON.parse(accepted);
  } catch {
    return { week, active: false, reason: `周文案导入记录无法读取：${acceptedFile}` };
  }
  const sha256 = createHash('sha256').update(raw).digest('hex');
  if (accepted?.week !== week || accepted?.sha256 !== sha256) {
    return { week, active: false, reason: '周文案文件与导入记录不一致（可能被改动），使用基础文案库' };
  }
  const rules = JSON.parse(await fs.readFile(rulesFile, 'utf8'));
  let rows;
  try {
    rows = parse(raw.toString('utf8').replace(/\r\n?/g, '\n'), { columns: true, skip_empty_lines: true, bom: true });
  } catch (error) {
    return { week, active: false, reason: `周文案 CSV 无法解析：${error.message}` };
  }
  const problem = structureProblem(rows, rules, week);
  if (problem) return { week, active: false, reason: problem };
  return { week, active: true, rows, sha256, reason: '' };
}

function structureProblem(rows, rules, week) {
  const per = Number(rules.perWeekPerMaterialLanguage);
  const source = `weekly-${week}`;
  const expected = new Map();
  for (const material of Object.keys(rules.materials)) {
    const languages = [...rules.baseLanguages];
    for (const [language, materials] of Object.entries(rules.scopedLanguages)) {
      if (materials.includes(material)) languages.push(language);
    }
    for (const language of languages) expected.set(`${material}|${language}`, 0);
  }
  const ids = new Set();
  for (const row of rows) {
    const key = `${row.material}|${row.language}`;
    if (!expected.has(key)) return `周文案含有不在规则里的材料/语言：${key}`;
    if (row.source !== source) return `周文案来源应为 ${source}，实际为 ${row.source}`;
    if (!String(row.copyId ?? '').trim() || ids.has(row.copyId)) return `周文案编号缺失或重复：${row.copyId}`;
    if (!String(row.title ?? '').trim() || !String(row.text ?? '').trim()) return `周文案标题或正文为空：${row.copyId}`;
    ids.add(row.copyId);
    expected.set(key, expected.get(key) + 1);
  }
  for (const [key, count] of expected) {
    if (count !== per) return `周文案 ${key} 应有 ${per} 条，实际为 ${count} 条`;
  }
  return '';
}
