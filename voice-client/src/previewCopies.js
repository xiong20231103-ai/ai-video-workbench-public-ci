import { config } from './config.js';
import { CopySelector } from './copySelector.js';
import { loadTasks } from './taskLoader.js';

const taskArgIndex = process.argv.indexOf('--task-base64');
const tasks = taskArgIndex >= 0
  ? [taskFromBase64(process.argv[taskArgIndex + 1])]
  : await loadTasks(config.taskFile);
const selector = await new CopySelector({
  libraryFile: config.copyLibraryFile,
  rulesFile: config.copyRulesFile,
  usageFile: config.copyUsageFile
}).init();

for (const task of tasks) {
  const resolved = await selector.select(task, { consume: false });
  if (resolved.selectionMode !== 'library') continue;
  const fields = [resolved.id, resolved.text, resolved.material, resolved.copyId].map(toBase64);
  console.log(fields.join('\t'));
}

function toBase64(value) {
  return Buffer.from(String(value ?? ''), 'utf8').toString('base64');
}

function taskFromBase64(value) {
  if (!value) throw new Error('--task-base64 缺少任务参数');
  const fields = Buffer.from(value, 'base64').toString('utf8').split('\x1f');
  if (fields.length < 4) throw new Error('--task-base64 任务参数无效');
  const [id, project, language, selectedMaterial] = fields;
  const material = selectedMaterial === '自动随机' ? '' : selectedMaterial;
  if (!id || !project || !language) throw new Error('--task-base64 缺少任务编号、项目或语言');
  return {
    id,
    project,
    language,
    material,
    voice: 'default',
    text: '',
    outputName: ''
  };
}
