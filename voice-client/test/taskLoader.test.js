import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { loadTasks } from '../src/taskLoader.js';

test('任务表正确读取逗号、双引号和多行文案', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'task-loader-'));
  const file = path.join(directory, 'tasks.csv');
  const text = [
    'id,project,language,voice,text,outputName,status',
    '001,ProjectB,英语,default,"第一行，带逗号和""引号""。',
    '第二行。",,',
  ].join('\r\n');
  await fs.writeFile(file, text, 'utf8');
  const tasks = await loadTasks(file);
  assert.equal(tasks.length, 1);
  assert.equal(tasks[0].text, '第一行，带逗号和"引号"。\r\n第二行。');
});

test('旧版示例占位文案自动进入随机模式', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'task-loader-'));
  const file = path.join(directory, 'tasks.csv');
  await fs.writeFile(
    file,
    'id,project,language,voice,text,outputName,status\r\n001,ProjectA,英语,default,这里填写中文语音文案。,beauty_zh_001,\r\n',
    'utf8'
  );
  const tasks = await loadTasks(file);
  assert.equal(tasks[0].text, '');
  assert.equal(tasks[0].outputName, '');
});

test('任务表读取用户指定材料', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'task-loader-'));
  const file = path.join(directory, 'tasks.csv');
  await fs.writeFile(
    file,
    'id,project,language,material,voice,text,outputName,status\r\n001,ProjectB,英语,木饰面,default,,,\r\n',
    'utf8'
  );
  const tasks = await loadTasks(file);
  assert.equal(tasks[0].material, '木饰面');
});
