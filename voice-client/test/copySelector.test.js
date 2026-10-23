import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { CopySelector } from '../src/copySelector.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const libraryFile = path.join(root, 'data', 'copy_library.csv');
const rulesFile = path.join(root, 'data', 'project_copy_rules.csv');

async function selectorForTest(random = () => 0) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'copy-selector-'));
  const usageFile = path.join(directory, 'copy_usage.json');
  const weeklyDir = path.join(directory, 'weekly'); // empty: base library only
  const selector = await new CopySelector({ libraryFile, rulesFile, usageFile, random, weeklyDir }).init();
  return { selector, usageFile, directory };
}

function task(overrides = {}) {
  return {
    id: '001', project: 'ProjectB', language: '英语', voice: 'default', text: '', outputName: '', ...overrides
  };
}

test('文案库保留正式440条并增加ExampleBrand 200条（含MaterialPump）', async () => {
  const { selector } = await selectorForTest();
  assert.equal(selector.library.length, 640);
  const examplebrandMaterials = ['清淤泵', '潜污泵', '离心泵', '轴流泵', '粉碎格栅', '一体化预制泵站', '大型泵站', 'MaterialPump'];
  const examplebrandKeys = new Set(examplebrandMaterials.flatMap((material) => [
    `${material}|英语`, `${material}|西班牙语`
  ]));
  const formalPools = [...selector.pools.entries()].filter(([key]) => !examplebrandKeys.has(key));
  const examplebrandPools = [...selector.pools.entries()].filter(([key]) => examplebrandKeys.has(key));
  assert.equal(formalPools.length, 88);
  assert.ok(formalPools.every(([, pool]) => pool.length === 5));
  assert.equal(examplebrandPools.length, 16);
  assert.ok(examplebrandPools.every(([, pool]) => pool.length >= 1));
  const expectedExamplebrandPoolSizes = {
    'MaterialPump': 30, '粉碎格栅': 25, '清淤泵': 15, '潜污泵': 10,
    '离心泵': 8, '一体化预制泵站': 6, '大型泵站': 4, '轴流泵': 2
  };
  for (const [material, expected] of Object.entries(expectedExamplebrandPoolSizes)) {
    assert.equal(selector.pools.get(`${material}|英语`).length, expected);
    assert.equal(selector.pools.get(`${material}|西班牙语`).length, expected);
  }
});

test('ExampleBrand新旧文案库按任务版本隔离并复用已绑定copyId', async () => {
  const { selector } = await selectorForTest(() => 0);
  const base = { project: 'ExampleBrand', language: '英语', material: '清淤泵', voice: 'chand', text: '' };
  const legacy = await selector.select({ ...base, id: 'legacy' });
  assert.equal(legacy.copyLibraryVersion, 'legacy');
  assert.equal(legacy.copySource, 'ExampleBrand-reels-rule-v0.4-hookmix');

  const modern = await selector.select({
    ...base,
    id: 'modern',
    copyLibraryVersion: 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1'
  });
  assert.equal(modern.copyLibraryVersion, 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1');
  assert.equal(modern.copySource, 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1');

  const persisted = await selector.select({ ...base, id: 'persisted', copyId: legacy.copyId });
  assert.equal(persisted.selectionMode, 'persisted');
  assert.equal(persisted.copyId, legacy.copyId);
  assert.equal(persisted.text, legacy.text);
});

test('旧递交器无版本的MaterialPump首次绑定新版，双语分别持久化且重启不重选', async () => {
  const { selector, usageFile } = await selectorForTest(() => 0);
  const base = { project: 'ExampleBrand', material: 'MaterialPump', canonicalTaskId: 'old-submitter-mobile',
    productionDate: '9.11', text: '', copyLibraryVersion: '' };
  for (const [id, language] of [['mobile-en', '英语'], ['mobile-es', '西班牙语']]) {
    const input = { ...base, id, language };
    const first = await selector.select(input);
    assert.equal(first.copyLibraryVersion, 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1');
    assert.ok(first.text.length > 0);
    const restarted = await new CopySelector({ libraryFile, rulesFile, usageFile, random: () => 0.99 }).init();
    const again = await restarted.select(input);
    assert.equal(again.copyId, first.copyId);
    assert.equal(again.text, first.text);
    assert.equal(again.copyTextHash, first.copyTextHash);
  }
});

test('MaterialPump明确旧版或已有文案身份不得静默换库', async () => {
  const { selector } = await selectorForTest();
  const base = { id: 'explicit-old-mobile', project: 'ExampleBrand', material: 'MaterialPump', language: '英语', text: '' };
  await assert.rejects(selector.select({ ...base, copyLibraryVersion: 'legacy' }), /找不到/);
  await assert.rejects(selector.select({ ...base, copyId: 'existing-old-id' }), /文案|copy/i);
});

test('ExampleBrand同一稳定任务重启后复用文案正文、版本和哈希', async () => {
  const { selector, usageFile } = await selectorForTest(() => 0);
  const taskInput = {
    id: 'examplebrand-child-01', canonicalTaskId: 'examplebrand-stable-01', project: 'ExampleBrand',
    language: '英语', material: 'MaterialPump', voice: 'chand', text: '',
    copyLibraryVersion: 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1'
  };
  const first = await selector.select(taskInput);
  const restarted = await new CopySelector({ libraryFile, rulesFile, usageFile, random: () => 0.99 }).init();
  const second = await restarted.select(taskInput);
  assert.equal(second.selectionMode, 'persisted');
  assert.equal(second.copyId, first.copyId);
  assert.equal(second.copyTextHash, first.copyTextHash);
  assert.equal(second.text, first.text);
});

test('共享父级任务身份时不同语言不会继承兄弟文案分配', async () => {
  const { selector, usageFile } = await selectorForTest(() => 0);
  const base = {
    project: 'ProjectD', material: '碳岩板', canonicalTaskId: 'formal-parent-01',
    productionDate: '9.11', runGroupId: 'run-01', batchId: 'batch-01', text: ''
  };
  const spanish = await selector.select({
    ...base, id: 'formal-parent-01-01', language: '西班牙语'
  });
  const portuguese = await selector.select({
    ...base, id: 'formal-parent-01-02', language: '葡萄牙语'
  });
  assert.equal(spanish.language, '西班牙语');
  assert.equal(portuguese.language, '葡萄牙语');
  assert.notEqual(portuguese.copyId, spanish.copyId);
  const saved = JSON.parse(await fs.readFile(path.join(path.dirname(usageFile), 'copy_assignments.json'), 'utf8'));
  const rows = Object.values(saved.assignments).filter((item) => item.canonicalTaskId === 'formal-parent-01');
  assert.equal(rows.length, 2);
  assert.deepEqual(new Set(rows.map((item) => item.language)), new Set(['西班牙语', '葡萄牙语']));
  assert.ok(rows.every((item) => item.audioTaskKey && item.audioTaskId));
});

test('同一稳定任务并发首次选择只提交一个文案身份', async () => {
  const { selector, usageFile, directory } = await selectorForTest(() => 0);
  const other = await new CopySelector({ libraryFile, rulesFile, usageFile, random: () => 0.99 }).init();
  const input = {
    id: 'examplebrand-child-01', canonicalTaskId: 'examplebrand-stable-concurrent', project: 'ExampleBrand',
    language: '英语', material: 'MaterialPump', voice: 'chand', text: '',
    copyLibraryVersion: 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1'
  };
  const [first, second] = await Promise.all([selector.select(input), other.select(input)]);
  assert.equal(first.copyId, second.copyId);
  assert.equal(first.copyTextHash, second.copyTextHash);
  const saved = JSON.parse(await fs.readFile(path.join(directory, 'copy_assignments.json'), 'utf8'));
  assert.equal(Object.keys(saved.assignments).length, 1);
});

test('旧任务带正文时保留既有文案身份，不冒充新版', async () => {
  const { selector } = await selectorForTest();
  const result = await selector.select(task({
    project: 'ExampleBrand', material: '清淤泵', language: '英语',
    text: '历史任务正文', copyId: 'legacy-copy-01',
    copySource: 'ExampleBrand-reels-rule-v0.4-hookmix', copyLibraryVersion: 'legacy'
  }));
  assert.equal(result.copyId, 'legacy-copy-01');
  assert.equal(result.copySource, 'ExampleBrand-reels-rule-v0.4-hookmix');
  assert.equal(result.copyLibraryVersion, 'legacy');
  assert.match(result.copyTextHash, /^[a-f0-9]{64}$/);
});

test('已绑定正文被修改或版本冲突时拒绝复用', async () => {
  const { selector, usageFile } = await selectorForTest(() => 0);
  const input = {
    id: 'identity-01', canonicalTaskId: 'stable-identity-01', project: 'ExampleBrand',
    material: 'MaterialPump', language: '英语', voice: 'chand', text: '',
    copyLibraryVersion: 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1'
  };
  const first = await selector.select(input);
  await assert.rejects(
    new CopySelector({ libraryFile, rulesFile, usageFile, random: () => 0 }).init()
      .then((next) => next.select({ ...input, copyLibraryVersion: 'legacy' })),
    /版本冲突/
  );
  const assignmentPath = path.join(path.dirname(usageFile), 'copy_assignments.json');
  const saved = JSON.parse(await fs.readFile(assignmentPath, 'utf8'));
  const key = Object.keys(saved.assignments)[0];
  saved.assignments[key].text = `${first.text}被改写`;
  await fs.writeFile(assignmentPath, `${JSON.stringify(saved)}\n`, 'utf8');
  await assert.rejects(
    new CopySelector({ libraryFile, rulesFile, usageFile, random: () => 0 }).init()
      .then((next) => next.select(input)),
    /保存正文已被修改/
  );
});

test('韩语只允许ProjectD并从七类材料池抽取', async () => {
  const { selector } = await selectorForTest();
  await assert.rejects(
    selector.select(task({ project: 'ProjectB', language: '韩语' })),
    /仅允许用于ProjectD项目/
  );
  const selections = [
    await selector.select(task({ id: 'KR-1', project: 'ProjectD', language: '韩语' })),
    await selector.select(task({ id: 'KR-2', project: 'ProjectD', language: '韩语' }))
  ];
  assert.equal(new Set(selections.map((item) => item.material)).size, 2);
  for (const selection of selections) {
    assert.match(selection.copyId, /-韩语-[1-5]$/);
    assert.ok(['净白板', '碳岩板', '金属板', '镜面板', '木饰面', 'PU石', 'PU软石'].includes(selection.material));
  }
});

test('方案A韩语任务只生成一条音频并复用到两条成片', async () => {
  const { selector } = await selectorForTest();
  const resolved = await selector.select(task({
    id: 'KR-A', project: 'ProjectD', language: '韩语', material: '碳岩板',
    audioCount: 1, outputCount: 2
  }));
  assert.equal(resolved.audioCount, 1);
  assert.equal(resolved.outputCount, 2);
  assert.equal(resolved.material, '碳岩板');
  assert.match(resolved.copyId, /^碳岩板-韩语-[1-5]$/);
});

test('七类项目连续七次材料不重复', async () => {
  const { selector } = await selectorForTest();
  const selections = [];
  for (let index = 0; index < 7; index += 1) {
    selections.push(await selector.select(task({ id: String(index + 1) })));
  }
  assert.equal(new Set(selections.map((item) => item.material)).size, 7);
  const eighth = await selector.select(task({ id: '008' }));
  assert.ok(eighth.material);
});

test('SPC 地板固定塑木地板且五条文案抽完前不重复', async () => {
  const { selector } = await selectorForTest();
  const selections = [];
  for (let index = 0; index < 5; index += 1) {
    selections.push(await selector.select(task({ id: String(index + 1), project: 'SPC地板' })));
  }
  assert.ok(selections.every((item) => item.material === '塑木地板'));
  assert.equal(new Set(selections.map((item) => item.copyId)).size, 5);
});

test('SPC 墙板只使用晶岩墙板', async () => {
  const { selector } = await selectorForTest();
  const selection = await selector.select(task({ project: 'SPC墙板', language: '俄语' }));
  assert.equal(selection.material, '晶岩墙板');
  assert.match(selection.copyId, /^晶岩墙板-俄语-/);
});

test('指定材料后只从该材料抽取且五条文案不重复', async () => {
  const { selector } = await selectorForTest();
  const selections = [];
  for (let index = 0; index < 5; index += 1) {
    selections.push(await selector.select(task({ id: String(index + 1), material: '木饰面' })));
  }
  assert.ok(selections.every((item) => item.material === '木饰面'));
  assert.equal(new Set(selections.map((item) => item.copyId)).size, 5);
});

test('项目不能选择映射之外的材料', async () => {
  const { selector } = await selectorForTest();
  await assert.rejects(
    selector.select(task({ project: 'SPC地板', material: '晶岩墙板' })),
    /不能使用材料/
  );
});

test('手工文案不被覆盖且不产生使用记录', async () => {
  const { selector, usageFile } = await selectorForTest();
  const selection = await selector.select(task({ text: '人工指定内容' }));
  assert.equal(selection.text, '人工指定内容');
  assert.equal(selection.selectionMode, 'manual');
  await assert.rejects(fs.access(usageFile));
});

test('手工文案保留用户指定材料用于后续目录和拼接交接', async () => {
  const { selector } = await selectorForTest();
  const selected = await selector.select(task({
    project: 'ProjectB',
    material: '镜面板',
    text: '这是一条手工文案'
  }));
  assert.equal(selected.selectionMode, 'manual');
  assert.equal(selected.material, '镜面板');
  assert.equal(selected.text, '这是一条手工文案');
});

test('空身份字段不会覆盖已保存的手工文案身份', async () => {
  const { selector } = await selectorForTest();
  const first = await selector.select(task({
    canonicalTaskId: 'stable-manual-identity',
    project: 'ProjectB',
    material: '镜面板',
    language: '英语',
    text: '固定正文',
    copyId: 'manual-copy-01',
    copySource: 'manual-source',
    copyLibraryVersion: 'legacy_unverified',
  }));
  const restarted = await new CopySelector({
    libraryFile, rulesFile, usageFile: selector.usageFile,
  }).init();
  const second = await restarted.select({
    canonicalTaskId: 'stable-manual-identity',
    project: 'ProjectB',
    material: '',
    language: '英语',
    text: '固定正文',
    copyId: '',
    copySource: '',
    copyLibraryVersion: '',
  });
  assert.equal(second.material, first.material);
  assert.equal(second.copyId, first.copyId);
  assert.equal(second.copySource, first.copySource);
  assert.equal(second.copyLibraryVersion, first.copyLibraryVersion);
});

test('预览抽取不写入使用记录', async () => {
  const { selector, usageFile } = await selectorForTest();
  await selector.select(task(), { consume: false });
  await assert.rejects(fs.access(usageFile));
});

test('重新启动后延续文案防重复状态', async () => {
  const { selector, usageFile } = await selectorForTest();
  const first = await selector.select(task({ project: 'SPC地板' }));
  const restarted = await new CopySelector({ libraryFile, rulesFile, usageFile, random: () => 0 }).init();
  const second = await restarted.select(task({ id: '002', project: 'SPC地板' }));
  assert.notEqual(first.copyId, second.copyId);
});

test('项目和语言使用记录相互独立', async () => {
  const { selector, usageFile } = await selectorForTest();
  await selector.select(task({ project: 'ProjectB', language: '英语' }));
  await selector.select(task({ project: 'ProjectA', language: '英语' }));
  await selector.select(task({ project: 'ProjectB', language: '德语' }));
  const state = JSON.parse(await fs.readFile(usageFile, 'utf8'));
  assert.ok(state.materialDecks['ProjectB|英语']);
  assert.ok(state.materialDecks['ProjectA|英语']);
  assert.ok(state.materialDecks['ProjectB|德语']);
});
