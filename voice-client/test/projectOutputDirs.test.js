import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { loadProjectOutputDirs, outputDirectoryKey } from '../src/projectOutputDirs.js';

test('自动日期使用当天的 M.d 文件夹', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'project-dirs-'));
  const file = path.join(root, 'rules.csv');
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(file, 'project,baseDir,folderName,dateMode,dateValue,outputDir\nProjectB,"E:\\音频",ProjectB音频,auto,,\n', 'utf8');
  const rules = await loadProjectOutputDirs(file, { now: new Date(2026, 6, 15) });
  assert.equal(rules.get('ProjectB'), path.resolve('E:\\音频', 'ProjectB音频', '7.15'));
});

test('手动日期使用用户指定的日期文件夹', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'project-dirs-'));
  const file = path.join(root, 'rules.csv');
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(file, 'project,baseDir,folderName,dateMode,dateValue,outputDir\nProjectA,"E:\\音频",ProjectA音频,manual,7.20,\n', 'utf8');
  const rules = await loadProjectOutputDirs(file, { now: new Date(2026, 6, 15) });
  assert.equal(rules.get('ProjectA'), path.resolve('E:\\音频', 'ProjectA音频', '7.20'));
});

test('旧版 outputDir 设置仍然有效', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'project-dirs-'));
  const file = path.join(root, 'rules.csv');
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(file, 'project,outputDir\nProjectC,"E:\\音频\\ProjectC音频"\n', 'utf8');
  const rules = await loadProjectOutputDirs(file);
  assert.equal(rules.get('ProjectC'), path.resolve('E:\\音频\\ProjectC音频'));
});

test('同一项目可按材料分别进入当天目录', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'project-dirs-'));
  const file = path.join(root, 'rules.csv');
  t.after(async () => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(
    file,
    'project,material,baseDir,folderName,dateMode,dateValue,outputDir\nProjectB,碳岩板,"E:\\音频\\ProjectB音频\\石纹",,auto,,\nProjectB,金属板,"E:\\音频\\ProjectB音频\\金属",,auto,,\n',
    'utf8'
  );
  const rules = await loadProjectOutputDirs(file, { now: new Date(2026, 6, 17) });
  assert.equal(rules.get(outputDirectoryKey('ProjectB', '碳岩板')), path.resolve('E:\\音频\\ProjectB音频\\石纹', '7.17'));
  assert.equal(rules.get(outputDirectoryKey('ProjectB', '金属板')), path.resolve('E:\\音频\\ProjectB音频\\金属', '7.17'));
});
