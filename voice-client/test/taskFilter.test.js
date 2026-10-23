import assert from 'node:assert/strict';
import test from 'node:test';
import {
  filterTasksByIds,
  filterTasksByProjects,
  selectedProjectsFromEnvironment,
  selectedTaskIdsFromEnvironment
} from '../src/taskFilter.js';

const tasks = [
  { id: '1', project: 'ProjectA' },
  { id: '2', project: 'ProjectB' },
  { id: '3', project: 'ProjectC' }
];

test('只保留本次勾选的项目任务', () => {
  const projects = selectedProjectsFromEnvironment('ProjectA|ProjectC');
  assert.deepEqual(filterTasksByProjects(tasks, projects).map((task) => task.id), ['1', '3']);
});

test('没有项目筛选时兼容旧行为并执行全部任务', () => {
  assert.equal(filterTasksByProjects(tasks, selectedProjectsFromEnvironment('')).length, 3);
});

test('只保留本次勾选的任务编号', () => {
  const ids = selectedTaskIdsFromEnvironment('1|3');
  assert.deepEqual(filterTasksByIds(tasks, ids).map((task) => task.id), ['1', '3']);
});
