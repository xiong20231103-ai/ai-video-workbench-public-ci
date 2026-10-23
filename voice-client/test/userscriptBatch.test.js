import { loadRetryCompletedItems } from '../src/retryCompletedItems.js';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { copyDownloadedFile, isSupportedExtensionVersion, runUserscriptBatch } from '../src/userscriptBatch.js';

test('Fish Audio 扩展桥接按领取和回报顺序完成任务并保存到项目文件夹', async (t) => {
  const port = 5198;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-bridge-'));
  const sourceDownload = path.join(tempRoot, 'source.mp3');
  const downloadDir = path.join(tempRoot, 'downloads');
  await fs.writeFile(sourceDownload, 'fake-mp3');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
  const events = [];
  const task = {
    id: 'T001',
    project: 'ProjectB',
    language: '英语',
    voice: 'default',
    text: 'hello',
    outputName: 'test-output',
    selectionMode: 'manual'
  };
  const logger = {
    info: async (message, meta = {}) => events.push({ level: 'info', message, meta }),
    error: async (message, meta = {}) => events.push({ level: 'error', message, meta })
  };
  const copySelector = {
    select: async (source, options) => {
      assert.equal(options.consume, true);
      return { ...source, selectionMode: 'manual' };
    }
  };

  const running = runUserscriptBatch({ tasks: [task], copySelector, logger, downloadDir, port, browserSessionId: 'test-session' });
  await waitForServer(port);
  const unrelated = await fetch(`http://127.0.0.1:${port}/api/bridge/status`).then((response) => response.json());
  assert.equal(unrelated.active, false);
  assert.equal(unrelated.sessionAccepted, false);
  await markBridgeReady(port);

  const status = await getJson(port, '/api/bridge/status');
  assert.equal(status.total, 1);
  assert.equal(status.done, false);

  const next = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(next.task.id, 'T001');
  assert.equal(next.task.voice, 'default');

  const result = await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify(validVoiceResult('T001', sourceDownload))
  });
  assert.equal(result.done, true);

  const summary = await running;
  assert.ok(events.some((event) => event.message === 'Fish Audio task finished'));
  assert.equal(summary.completedItems.length, 1);
  assert.equal(summary.completedItems[0].project, 'ProjectB');
  assert.equal(summary.completedItems[0].canonicalTaskId, '');
  assert.equal(summary.completedItems[0].audioPath, path.join(downloadDir, 'ProjectB', 'test-output.mp3'));
  assert.equal(await fs.readFile(path.join(downloadDir, 'ProjectB', 'test-output.mp3'), 'utf8'), 'fake-mp3');
  await assert.rejects(fs.access(sourceDownload));
});

test('Fish Audio 扩展桥接把项目音频保存到用户指定文件夹', async (t) => {
  const port = 5299;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-custom-output-'));
  const sourceDownload = path.join(tempRoot, 'source.mp3');
  const downloadDir = path.join(tempRoot, 'fallback-downloads');
  const customDir = path.join(tempRoot, 'my-entity-audio');
  await fs.writeFile(sourceDownload, 'custom-folder-mp3');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  const task = {
    id: 'T002', project: 'ProjectB', language: '英语', voice: 'default',
    text: 'hello', outputName: 'custom-output', selectionMode: 'manual'
  };
  const logger = { info: async () => {}, error: async () => {} };
  const copySelector = { select: async (source) => ({ ...source, selectionMode: 'manual' }) };
  const projectOutputDirs = new Map([['ProjectB', customDir]]);

  const running = runUserscriptBatch({
    tasks: [task], copySelector, logger, downloadDir, projectOutputDirs, port, browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port);
  const next = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify(validVoiceResult(next.task.id, sourceDownload))
  });
  const summary = await running;

  assert.equal(await fs.readFile(path.join(customDir, 'custom-output.mp3'), 'utf8'), 'custom-folder-mp3');
  assert.equal(summary.completedItems[0].audioPath, path.join(customDir, 'custom-output.mp3'));
  await assert.rejects(fs.access(path.join(downloadDir, 'ProjectB', 'custom-output.mp3')));
  await assert.rejects(fs.access(sourceDownload));
});

test('目标目录写入失败时保留浏览器原始文件', async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-copy-failure-'));
  const sourceDownload = path.join(tempRoot, 'source.mp3');
  const blockedDirectory = path.join(tempRoot, 'not-a-directory');
  await fs.writeFile(sourceDownload, 'must-survive');
  await fs.writeFile(blockedDirectory, 'blocking-file');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  await assert.rejects(copyDownloadedFile({
    source: sourceDownload,
    downloadDir: path.join(tempRoot, 'downloads'),
    outputName: 'test-output',
    project: 'ProjectB',
    projectOutputDirs: new Map([['ProjectB', blockedDirectory]])
  }));
  assert.equal(await fs.readFile(sourceDownload, 'utf8'), 'must-survive');
});

test('项目材料目录优先于项目通用目录', async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-material-output-'));
  const sourceDownload = path.join(tempRoot, 'source.mp3');
  const projectDir = path.join(tempRoot, 'entity');
  const materialDir = path.join(tempRoot, 'entity-metal');
  await fs.writeFile(sourceDownload, 'material-mp3');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  const saved = await copyDownloadedFile({
    source: sourceDownload,
    downloadDir: path.join(tempRoot, 'downloads'),
    outputName: 'metal-output',
    project: 'ProjectB',
    material: '金属板',
    projectOutputDirs: new Map([
      ['ProjectB', projectDir],
      ['ProjectB\u0000金属板', materialDir]
    ])
  });
  assert.equal(saved, path.join(materialDir, 'metal-output.mp3'));
  assert.equal(await fs.readFile(saved, 'utf8'), 'material-mp3');
});

test('下载超时后保留同一任务并自动重试且不重复抽取文案', async (t) => {
  const port = 5200;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-download-retry-'));
  const sourceDownload = path.join(tempRoot, 'retry.mp3');
  const downloadDir = path.join(tempRoot, 'downloads');
  await fs.writeFile(sourceDownload, 'retry-success');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  let selections = 0;
  const task = {
    id: 'T003', project: 'ProjectA', language: '俄语', material: '碳岩板',
    voice: 'default', text: 'hello', outputName: 'retry-output', selectionMode: 'manual'
  };
  const logger = { info: async () => {}, error: async () => {} };
  const copySelector = {
    select: async (source) => {
      selections += 1;
      return { ...source, selectionMode: 'manual' };
    }
  };

  const running = runUserscriptBatch({ tasks: [task], copySelector, logger, downloadDir, port, browserSessionId: 'test-session' });
  await waitForServer(port);
  await markBridgeReady(port);
  const first = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  const retry = await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify({ taskId: first.task.id, ok: false, error: '等待浏览器下载完成超时' })
  });
  assert.equal(retry.retry, true);
  assert.equal(retry.retryAttempt, 1);

  const resumed = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(resumed.resumed, true);
  assert.equal(resumed.task.id, first.task.id);
  await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify(validVoiceResult(resumed.task.id, sourceDownload))
  });
  const summary = await running;
  assert.equal(selections, 1);
  assert.equal(summary.completed, 1);
  assert.equal(summary.failed, 0);
  assert.equal(await fs.readFile(path.join(downloadDir, 'ProjectA', 'retry-output.mp3'), 'utf8'), 'retry-success');
});

test('结构化音色错误不会因错误文字包含“下载”而误走下载重试', async (t) => {
  const port = 5312;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-voice-error-'));
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
  const task = {
    id: 'VOICE-ERROR-1', project: 'SPC地板', language: '德语', material: '塑木地板',
    voice: 'Ben', text: 'hallo', outputName: 'voice-error', selectionMode: 'manual'
  };
  const running = runUserscriptBatch({
    tasks: [task],
    copySelector: { select: async (source) => source },
    logger: { info: async () => {}, error: async () => {} },
    downloadDir: path.join(tempRoot, 'downloads'),
    port,
    browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port);
  await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  const result = await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify({
      taskId: task.id,
      ok: false,
      errorCode: 'VOICE_EVIDENCE_CONFLICT',
      phase: 'verifying_voice',
      error: '音色证据冲突，已阻止生成和下载'
    })
  });
  assert.equal(result.retry, undefined);
  assert.equal(result.done, true);
  const summary = await running;
  assert.equal(summary.failed, 1);
  assert.equal(summary.failedItems[0].errorCode, 'VOICE_EVIDENCE_CONFLICT');
  assert.equal(summary.failedItems[0].phase, 'verifying_voice');
});

test('Fish页面失联后心跳看门狗结束挂起批次并释放桥接端口', async (t) => {
  const port = 5313;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-heartbeat-timeout-'));
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
  const running = runUserscriptBatch({
    tasks: [{
      id: 'HEARTBEAT-1', project: 'SPC地板', language: '英语', material: '塑木地板',
      voice: 'chand', text: 'hello', outputName: 'heartbeat', selectionMode: 'manual'
    }],
    copySelector: { select: async (source) => source },
    logger: { info: async () => {}, error: async () => {} },
    downloadDir: path.join(tempRoot, 'downloads'),
    port,
    browserSessionId: 'test-session',
    heartbeatTimeoutMs: 600
  });
  await waitForServer(port);
  await markBridgeReady(port);
  await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  await assert.rejects(running, /页面已失联/);
  await assert.rejects(fetch(`http://127.0.0.1:${port}/api/bridge/status`));
});

test('首次切换音色窗口暂未打开时只重试当前任务一次', async (t) => {
  const port = 5202;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-voice-dialog-retry-'));
  const sourceDownload = path.join(tempRoot, 'retry.mp3');
  await fs.writeFile(sourceDownload, 'voice-dialog-success');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  let selections = 0;
  const task = { id: 'T005', project: 'ProjectC', language: '意大利语', material: '碳岩板', voice: 'default', text: 'hello', outputName: 'voice-retry' };
  const running = runUserscriptBatch({
    tasks: [task],
    copySelector: { select: async (source) => { selections += 1; return { ...source, selectionMode: 'manual' }; } },
    logger: { info: async () => {}, error: async () => {} },
    downloadDir: path.join(tempRoot, 'downloads'), port, browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port);
  const first = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  const retry = await getJson(port, '/api/bridge/result', {
    method: 'POST', body: JSON.stringify({ taskId: first.task.id, ok: false, error: '右侧切换音色按钮没有打开“选择语音”窗口' })
  });
  assert.equal(retry.retry, true);
  assert.equal(retry.retryReason, 'voice-selection');
  const resumed = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(resumed.resumed, true);
  await getJson(port, '/api/bridge/result', {
    method: 'POST', body: JSON.stringify(validVoiceResult(resumed.task.id, sourceDownload))
  });
  const summary = await running;
  assert.equal(selections, 1);
  assert.equal(summary.completed, 1);
  assert.equal(summary.failed, 0);
});

test('音色库标签延迟挂载时按结构化错误重试同一任务且不推进索引', async (t) => {
  const port = 5203;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-catalog-tabs-retry-'));
  const sourceDownload = path.join(tempRoot, 'catalog-tabs.mp3');
  await fs.writeFile(sourceDownload, 'catalog-tabs-success');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  let selections = 0;
  const task = {
    id: 'T006', project: 'ProjectD', language: '西班牙语', material: '碳岩板',
    voice: 'Farid Dieck', text: 'hola', outputName: 'catalog-tabs'
  };
  const running = runUserscriptBatch({
    tasks: [task],
    copySelector: { select: async (source) => { selections += 1; return { ...source, selectionMode: 'manual' }; } },
    logger: { info: async () => {}, error: async () => {} },
    downloadDir: path.join(tempRoot, 'downloads'), port, browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port);
  const first = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  const retry = await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify({
      taskId: first.task.id,
      ok: false,
      errorCode: 'VOICE_SAVED_TAB_TIMEOUT',
      phase: 'waiting_for_catalog_tabs',
      error: '完整音色库标签尚未加载完成'
    })
  });
  assert.equal(retry.retry, true);
  assert.equal(retry.retryReason, 'voice-selection');
  assert.equal(retry.maxVoiceDialogRetries, 1);
  const resumed = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(resumed.resumed, true);
  assert.equal(resumed.task.id, first.task.id);
  await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify(validVoiceResult(resumed.task.id, sourceDownload, 'Farid Dieck'))
  });
  const summary = await running;
  assert.equal(selections, 1);
  assert.equal(summary.completed, 1);
  assert.equal(summary.failed, 0);
});

for (const [port, errorCode] of [[5204, 'FISH_GENERATE_NOT_STARTED'], [5205, 'FISH_PAGE_NOT_STABLE']]) {
  test(`${errorCode} 只重试当前任务一次，再失败才记为失败`, async (t) => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-page-retry-'));
    t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
    const task = {
      id: 'T007', project: 'ProjectA', language: '英语', material: '碳岩板',
      voice: 'default', text: 'hello', outputName: 'page-retry', selectionMode: 'manual'
    };
    const running = runUserscriptBatch({
      tasks: [task],
      copySelector: { select: async (source) => ({ ...source, selectionMode: 'manual' }) },
      logger: { info: async () => {}, error: async () => {} },
      downloadDir: path.join(tempRoot, 'downloads'), port, browserSessionId: 'test-session'
    });
    await waitForServer(port);
    await markBridgeReady(port);
    const failure = (id) => JSON.stringify({ taskId: id, ok: false, errorCode, phase: 'generating', error: errorCode });
    const first = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
    const retry = await getJson(port, '/api/bridge/result', { method: 'POST', body: failure(first.task.id) });
    assert.equal(retry.retry, true);
    assert.equal(retry.retryReason, 'page');
    const resumed = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
    assert.equal(resumed.resumed, true);
    assert.equal(resumed.task.id, first.task.id);
    const final = await getJson(port, '/api/bridge/result', { method: 'POST', body: failure(resumed.task.id) });
    assert.equal(final.retry, undefined);
    const summary = await running;
    assert.equal(summary.completed, 0);
    assert.equal(summary.failed, 1);
  });
}

test('重试批次复用已有有效音频，只领取之前失败的任务', async (t) => {
  const port = 5201;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-partial-retry-'));
  const existingAudio = path.join(tempRoot, 'existing.mp3');
  const newAudio = path.join(tempRoot, 'new.mp3');
  const downloadDir = path.join(tempRoot, 'downloads');
  await fs.writeFile(existingAudio, 'previous-success');
  await fs.writeFile(newAudio, 'new-success');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));

  const tasks = [
    { id: 'T004-1', project: 'ProjectA', language: '英语', voice: 'default', text: 'one', outputName: 'one', canonicalTaskId: 'parent-4' },
    { id: 'T004-2', project: 'ProjectA', language: '德语', voice: 'default', text: 'two', outputName: 'two', canonicalTaskId: 'parent-4' }
  ];
  const selections = [];
  const logger = { info: async () => {}, error: async () => {} };
  const copySelector = {
    select: async (task) => {
      selections.push(task.id);
      return { ...task, selectionMode: 'manual' };
    }
  };
  const running = runUserscriptBatch({
    tasks: [tasks[1]],
    copySelector,
    logger,
    downloadDir,
    port,
    totalExpected: 2,
    initialCompletedItems: [{
      taskId: 'T004-1', project: 'ProjectA', language: '英语', canonicalTaskId: 'parent-4', audioPath: existingAudio
    }],
    browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port);
  const status = await getJson(port, '/api/bridge/status');
  assert.equal(status.total, 2);
  assert.equal(status.completed, 1);
  const next = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(next.task.id, 'T004-2');
  await getJson(port, '/api/bridge/result', {
    method: 'POST', body: JSON.stringify(validVoiceResult('T004-2', newAudio))
  });
  const summary = await running;
  assert.deepEqual(selections, ['T004-2']);
  assert.equal(summary.total, 2);
  assert.equal(summary.completed, 2);
  assert.equal(summary.failed, 0);
  assert.equal(summary.completedItems.length, 2);
  assert.deepEqual(new Set(summary.completedItems.map((item) => item.canonicalTaskId)), new Set(['parent-4']));
});

test('bridge rejects a download when the active Fish voice differs from the task voice', async (t) => {
  const port = 5300;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-voice-mismatch-'));
  const sourceDownload = path.join(tempRoot, 'wrong-voice.mp3');
  await fs.writeFile(sourceDownload, 'wrong-voice');
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
  const events = [];
  const running = runUserscriptBatch({
    tasks: [{ id: 'VOICE-MISMATCH', project: 'test', language: 'German', voice: 'Ben', text: 'hello', outputName: 'wrong' }],
    copySelector: { select: async (task) => ({ ...task }) },
    logger: { info: async () => {}, error: async (message, meta) => events.push({ message, meta }) },
    downloadDir: path.join(tempRoot, 'downloads'), port, browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port);
  const next = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  const result = await getJson(port, '/api/bridge/result', {
    method: 'POST',
    body: JSON.stringify(validVoiceResult(next.task.id, sourceDownload, 'Молодой Аналитик'))
  });
  assert.equal(result.done, true);
  const summary = await running;
  assert.equal(summary.completed, 0);
  assert.equal(summary.failed, 1);
  assert.equal(events.some((event) => event.message === 'Fish Audio voice verification rejected result'), true);
  await assert.rejects(fs.access(path.join(tempRoot, 'downloads', 'test', 'wrong.mp3')));
  assert.equal(await fs.readFile(sourceDownload, 'utf8'), 'wrong-voice');
});

async function waitForServer(port) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      await fetch(`http://127.0.0.1:${port}/api/bridge/status?browserSessionId=test-session`);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }
  throw new Error('bridge server did not start');
}

test('copy identity conflict is returned as a structured non-retryable block', async (t) => {
  const port = 5310;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-copy-conflict-'));
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
  const running = runUserscriptBatch({
    tasks: [{ id: 'COPY-PT', canonicalTaskId: 'COPY-PARENT', project: 'ProjectD', material: '碳岩板', language: '葡萄牙语' }],
    copySelector: {
      select: async () => {
        const error = new Error('COPY_ASSIGNMENT_CONFLICT: language');
        error.code = 'COPY_ASSIGNMENT_CONFLICT';
        throw error;
      }
    },
    logger: { info: async () => {}, error: async () => {} },
    downloadDir: path.join(tempRoot, 'downloads'),
    port,
    browserSessionId: 'test-session'
  });
  const runningOutcome = running.then(() => null, (error) => error);
  await waitForServer(port);
  await markBridgeReady(port, '1.3.47');
  await new Promise((resolve) => setTimeout(resolve, 50));
  const response = await fetch(`http://127.0.0.1:${port}/api/bridge/next?browserSessionId=test-session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  });
  const payload = await response.json();
  assert.equal(response.status, 409);
  assert.equal(payload.errorCode, 'COPY_ASSIGNMENT_CONFLICT');
  assert.equal(payload.retryable, false);
  assert.equal(payload.phase, 'copy_assignment');
  assert.equal(payload.taskId, 'COPY-PT');
  const batchError = await runningOutcome;
  assert.equal(batchError.code, 'COPY_ASSIGNMENT_CONFLICT');
});

async function getJson(port, pathname, options = {}) {
  const url = new URL(`http://127.0.0.1:${port}${pathname}`);
  url.searchParams.set('browserSessionId', 'test-session');
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  });
  assert.equal(response.ok, true);
  return response.json();
}

test('old Fish extension can connect but cannot claim a voice task', async (t) => {
  const port = 5311;
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-extension-version-'));
  const task = { id: 'VERSION-1', project: 'demo', language: 'de', voice: 'Ben', text: 'hello', outputName: 'voice' };
  t.after(async () => fs.rm(tempRoot, { recursive: true, force: true }));
  const running = runUserscriptBatch({
    tasks: [task],
    copySelector: { select: async (source) => source },
    logger: { info: async () => {}, error: async () => {} },
    downloadDir: path.join(tempRoot, 'downloads'),
    port,
    browserSessionId: 'test-session'
  });
  await waitForServer(port);
  await markBridgeReady(port, '1.3.31');
  const status = await getJson(port, '/api/bridge/status');
  assert.equal(status.ready, false);
  assert.equal(status.extensionVersionOk, false);
  const blocked = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(blocked.waiting, true);
  assert.equal(blocked.versionMismatch, true);

  await markBridgeReady(port, '1.3.47');
  const next = await getJson(port, '/api/bridge/next', { method: 'POST', body: '{}' });
  assert.equal(next.task.id, 'VERSION-1');
  const source = path.join(tempRoot, 'voice.mp3');
  await fs.writeFile(source, 'voice');
  await getJson(port, '/api/bridge/result', { method: 'POST', body: JSON.stringify(validVoiceResult('VERSION-1', source, 'Ben')) });
  await running;
});

test('Fish extension version gate accepts current and future compatible versions', () => {
  assert.equal(isSupportedExtensionVersion('1.3.31'), false);
  assert.equal(isSupportedExtensionVersion('1.3.32'), false);
  assert.equal(isSupportedExtensionVersion('1.3.33'), false);
  assert.equal(isSupportedExtensionVersion('1.3.34'), false);
  assert.equal(isSupportedExtensionVersion('1.3.35'), false);
  assert.equal(isSupportedExtensionVersion('1.3.36'), false);
  assert.equal(isSupportedExtensionVersion('1.3.38'), false);
  assert.equal(isSupportedExtensionVersion('1.3.40'), false);
  assert.equal(isSupportedExtensionVersion('1.3.41'), false);
  assert.equal(isSupportedExtensionVersion('1.3.42'), false);
  assert.equal(isSupportedExtensionVersion('1.3.44'), false);
  assert.equal(isSupportedExtensionVersion('1.3.46'), false);
  assert.equal(isSupportedExtensionVersion('1.3.47'), true);
  assert.equal(isSupportedExtensionVersion('1.4.0'), true);
  assert.equal(isSupportedExtensionVersion('bad-version'), false);
});

async function markBridgeReady(port, scriptVersion = '1.3.47') {
  await getJson(port, '/api/bridge/diagnostics', {
    method: 'POST',
    body: JSON.stringify({ scriptVersion, url: 'https://fish.audio/zh-CN/app/text-to-speech/', loggedIn: true })
  });
}

function validVoiceResult(taskId, download, voice = 'default') {
  return {
    taskId,
    ok: true,
    download,
    expectedVoice: voice,
    actualActiveVoice: voice,
    voiceVerification: {
      expected: voice,
      header: voice,
      rightCard: voice,
      editorCount: 1,
      matchingSources: ['header', 'rightCard'],
      conflictingSources: []
    }
  };
}


test('8/9 partial handoff resumes only missing German and retains eight completions', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fish-partial-resume-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const tasks = Array.from({length:9}, (_,i) => ({id:`partial-${i}`,canonicalTaskId:'parent',
    project:'fixture', language:i===8?'德语':`language-${i}`,voice:'Ben',text:'fixture',outputName:`audio-${i}`,selectionMode:'manual'}));
  const completedItems=[];
  for (const task of tasks.slice(0,8)) {
    const audioPath=path.join(root,`${task.id}.mp3`);await fs.writeFile(audioPath,'existing audio');
    completedItems.push({taskId:task.id,canonicalTaskId:'parent',voiceVerified:true,
      expectedVoice:'Ben',actualActiveVoice:'Ben',audioPath});
  }
  const handoff=path.join(root,'handoff.json');
  await fs.writeFile(handoff,JSON.stringify({status:'completed_with_errors',total:9,completed:8,failed:1,completedItems}));
  const recovered=await loadRetryCompletedItems(handoff,path.join(root,'ledger'),tasks);
  assert.equal(recovered.length,8);
  const ids=new Set(recovered.map(i=>i.taskId));
  const remaining=tasks.filter(t=>!ids.has(t.id));
  assert.deepEqual(remaining.map(t=>t.id),['partial-8']);
  const port=5336;
  const running=runUserscriptBatch({tasks:remaining,totalExpected:9,initialCompletedItems:recovered,
    copySelector:{select:async t=>t},logger:{info:async()=>{},error:async()=>{}},
    downloadDir:path.join(root,'downloads'),port,browserSessionId:'test-session'});
  await waitForServer(port);await markBridgeReady(port);
  const next=await getJson(port,'/api/bridge/next',{method:'POST',body:'{}'});
  assert.equal(next.task.id,'partial-8');
  const audio=path.join(root,'new.mp3');await fs.writeFile(audio,'new audio');
  const receipt=await getJson(port,'/api/bridge/result',{method:'POST',body:JSON.stringify(validVoiceResult('partial-8',audio,'Ben'))});
  assert.equal(receipt.done,true);
  const result=await running;
  assert.equal(result.completed,9);assert.equal(result.failed,0);
  assert.deepEqual(result.completedItems.slice(0,8),recovered);
});
