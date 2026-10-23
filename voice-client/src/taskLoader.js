import fs from 'node:fs/promises';
import { parse } from 'csv-parse/sync';

const requiredColumns = ['id', 'language', 'voice', 'text', 'outputName'];
const legacyPlaceholderTexts = new Set([
  '这里填写中文语音文案。',
  'Put the English voice script here.'
]);

export async function loadTasks(taskFile) {
  const content = await fs.readFile(taskFile, 'utf8');
  const records = parse(content, {
    columns: true,
    skip_empty_lines: true,
    trim: true,
    bom: true,
    record_delimiter: ['\r\n', '\n', '\r']
  });

  validateColumns(records);

  return records
    .filter((record) => String(record.status ?? '').toLowerCase() !== 'done')
    .map((record, index) => {
      const project = record.project || record.majorCategory || record.category || '';
      const rawText = String(record.text ?? '').trim();
      const usesLegacyPlaceholder = legacyPlaceholderTexts.has(rawText);
      return {
        rowNumber: index + 2,
        id: record.id,
        project,
        language: record.language,
        material: String(record.material ?? '').trim(),
        voice: record.voice || 'default',
        text: usesLegacyPlaceholder ? '' : record.text,
        outputName: usesLegacyPlaceholder ? '' : record.outputName,
        canonicalTaskId: String(record.canonicalTaskId ?? '').trim(),
        runGroupId: String(record.runGroupId ?? '').trim(),
        sourceBatchId: String(record.sourceBatchId ?? '').trim(),
        manifestId: String(record.manifestId ?? '').trim(),
        productionDate: String(record.productionDate ?? '').trim(),
        audioBatch: String(record.audioBatch ?? '').trim(),
        copyId: String(record.copyId ?? '').trim(),
        copyTitle: String(record.copyTitle ?? '').trim(),
        copySource: String(record.copySource ?? '').trim(),
        copyLibraryVersion: String(record.copyLibraryVersion ?? '').trim(),
        copyTextHash: String(record.copyTextHash ?? '').trim()
      };
    });
}

function validateColumns(records) {
  if (records.length === 0) return;

  const columns = Object.keys(records[0]);
  const missing = requiredColumns.filter((column) => !columns.includes(column));
  const hasProject = ['project', 'majorCategory', 'category'].some((column) => columns.includes(column));
  if (!hasProject) missing.push('project');
  if (missing.length > 0) {
    throw new Error(`tasks.csv 缺少字段: ${missing.join(', ')}`);
  }
}
