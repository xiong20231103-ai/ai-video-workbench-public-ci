import http from 'node:http';
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';
import { outputDirectoryKey } from './projectOutputDirs.js';

// The workbench and content script must agree on the navigation flow used to
// activate a Fish voice.  Older scripts can connect, but must never receive a
// task because they can silently choose the wrong speaker.
export const REQUIRED_EXTENSION_VERSION = '1.3.47';

export function isSupportedExtensionVersion(version, minimum = REQUIRED_EXTENSION_VERSION) {
  const parse = (value) => String(value ?? '').trim().split('.').map((part) => {
    const parsed = Number.parseInt(part, 10);
    return Number.isFinite(parsed) ? parsed : Number.NaN;
  });
  const actual = parse(version);
  const required = parse(minimum);
  if (actual.length < 3 || required.length < 3 || actual.slice(0, 3).some(Number.isNaN) || required.slice(0, 3).some(Number.isNaN)) {
    return false;
  }
  for (let index = 0; index < 3; index += 1) {
    if (actual[index] !== required[index]) return actual[index] > required[index];
  }
  return true;
}

export async function runUserscriptBatch({
  tasks,
  copySelector,
  logger,
  downloadDir,
  audioOutputDir = '',
  projectOutputDirs = new Map(),
  port = 5178,
  browserSessionId = '',
  onProgress = null,
  onDiagnostics = null,
  onBridgeListening = null,
  onBridgeClosed = null,
  totalExpected = tasks.length,
  initialCompletedItems = [],
  // Chrome deliberately throttles timers for a backgrounded Fish tab.  The
  // content script emits every four seconds while foregrounded, but give a
  // live browser enough time to resume after a Fish catalogue redraw before
  // treating the page as disconnected.
  heartbeatTimeoutMs = 90_000
}) {
  let index = 0;
  let current = null;
  let currentRetryCount = 0;
  let currentUiRetryCount = 0;
  const maxDownloadRetries = 2;
  const maxVoiceDialogRetries = 1;
  const expectedTotal = Math.max(Number(totalExpected) || 0, tasks.length);
  const completedItems = Array.isArray(initialCompletedItems) ? [...initialCompletedItems] : [];
  let completed = completedItems.length;
  let failed = 0;
  const failedItems = [];
  let lastHeartbeat = null;
  let lastHeartbeatAt = 0;
  let lastTaskResultAt = 0;
  let lastTaskResult = null;
  let bridgeReady = null;
  let bridgeReadyAt = 0;
  let bridgeControlsReady = false;
  let loginRequired = false;
  let extensionConnected = false;
  let extensionVersion = '';
  let extensionVersionOk = false;
  let lastDiagnostics = {};
  let blockedError = null;
  let finish;
  const finished = new Promise((resolve) => {
    finish = resolve;
  });

  if (!browserSessionId) throw new Error('Fish Audio browser session was not created');

  const server = http.createServer(async (req, res) => {
    setCors(res);
    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      return res.end();
    }

    try {
      const url = new URL(req.url ?? '/', `http://${req.headers.host ?? '127.0.0.1'}`);
      if (url.searchParams.get('browserSessionId') !== browserSessionId) {
        if (req.method === 'GET' && url.pathname === '/api/bridge/status') {
          return sendJson(res, 200, { ok: true, active: false, sessionAccepted: false });
        }
        return sendJson(res, 403, { ok: false, error: 'Fish Audio browser session is not active' });
      }

      if (req.method === 'GET' && url.pathname === '/api/bridge/status') {
        lastHeartbeat = new Date().toISOString();
        lastHeartbeatAt = Date.now();
        extensionConnected = true;
        return sendJson(res, 200, {
          ok: true,
          active: true,
          total: expectedTotal,
          completed,
          failed,
          pending: Math.max(0, expectedTotal - completed - failed - (current ? 1 : 0)),
          current: current ? publicTask(current) : null,
          done: index >= tasks.length && !current,
          ready: bridgeReady === true,
          loginRequired,
          extensionVersion,
          requiredExtensionVersion: REQUIRED_EXTENSION_VERSION,
          extensionVersionOk,
          versionMessage: extensionVersionOk
            ? ''
            : `Fish插件版本${extensionVersion || '未知'}，需要重新加载${REQUIRED_EXTENSION_VERSION}`,
          sessionAccepted: true,
          blocked: Boolean(blockedError),
          primaryError: blockedError ? publicBridgeError(blockedError) : null
        });
      }

      if (req.method === 'POST' && url.pathname === '/api/bridge/next') {
        lastHeartbeat = new Date().toISOString();
        lastHeartbeatAt = Date.now();
        extensionConnected = true;
        if (blockedError) {
          return sendJson(res, 409, {
            ok: false,
            ...publicBridgeError(blockedError)
          });
        }
        if (!extensionVersionOk) {
          return sendJson(res, 200, {
            ok: true,
            waiting: true,
            versionMismatch: true,
            requiredExtensionVersion: REQUIRED_EXTENSION_VERSION,
            extensionVersion,
            message: `Fish插件版本${extensionVersion || '未知'}，需要重新加载${REQUIRED_EXTENSION_VERSION}`
          });
        }
        if (bridgeReady !== true) {
          return sendJson(res, 200, {
            ok: true,
            waiting: true,
            loginRequired,
            message: loginRequired ? 'Fish Audio 登录已失效，等待重新登录' : '等待 Fish Audio 页面就绪'
          });
        }
        // Fish reports a usable editor a moment before its right-hand voice
        // controls finish hydrating.  Dispatching the very first language at
        // that instant caused an avoidable “switch voice did not open” error.
        // Hold only the first dispatch briefly; no task, copy, or audio file
        // is changed while waiting.
        if (bridgeControlsReady && bridgeReadyAt && Date.now() - bridgeReadyAt < 5_000) {
          return sendJson(res, 200, {
            ok: true,
            waiting: true,
            message: 'Fish Audio 页面正在稳定音色控件'
          });
        }
        if (current) return sendJson(res, 200, { ok: true, task: publicTask(current), resumed: true });
        if (index >= tasks.length) {
          scheduleFinish(server, finish);
          return sendJson(res, 200, { ok: true, done: true });
        }

        const sourceTask = tasks[index];
        try {
          current = await copySelector.select(sourceTask, { consume: true });
        } catch (error) {
          if (!isDeterministicCopyIdentityError(error)) throw error;
          blockedError = makeBridgeError(error, sourceTask);
          lastDiagnostics = {
            ...lastDiagnostics,
            primaryError: publicBridgeError(blockedError),
            taskId: String(sourceTask.id || ''),
            phase: 'copy_assignment'
          };
          await logger.error('Fish Audio bridge blocked by copy identity', {
            requestMethod: req.method,
            requestPath: url.pathname,
            taskId: String(sourceTask.id || ''),
            canonicalTaskId: String(sourceTask.canonicalTaskId || ''),
            project: String(sourceTask.project || ''),
            material: String(sourceTask.material || ''),
            language: String(sourceTask.language || ''),
            errorCode: blockedError.code,
            phase: blockedError.phase,
            retryable: false,
            message: blockedError.message,
            stack: error instanceof Error ? error.stack : ''
          });
          if (typeof onDiagnostics === 'function') await onDiagnostics(lastDiagnostics);
          const payload = publicBridgeError(blockedError);
          sendJson(res, 409, { ok: false, ...payload });
          setTimeout(() => {
            server.closeAllConnections?.();
            finish();
          }, 0);
          return;
        }
        currentRetryCount = 0;
        currentUiRetryCount = 0;
        await logSelection(logger, current);
        await logger.info('Fish Audio bridge dispatched task', {
          taskId: current.id,
          index: index + 1,
          total: tasks.length
        });
        if (onProgress) await onProgress({
          total: expectedTotal, completed, failed,
          current: publicTask(current), completedItems, failedItems
        });
        return sendJson(res, 200, { ok: true, task: publicTask(current), resumed: false });
      }

      if (req.method === 'POST' && url.pathname === '/api/bridge/result') {
        const body = await readJson(req);
        if (!current || String(body.taskId ?? '') !== String(current.id)) {
          return sendJson(res, 409, { ok: false, error: '任务编号与当前任务不一致' });
        }

        if (body.ok) {
          const verification = verifyActiveVoiceResult(current, body);
          if (!verification.ok) {
            body.ok = false;
            body.error = verification.error;
            await logger.error('Fish Audio voice verification rejected result', {
              taskId: current.id,
              expectedVoice: current.voice || '',
              actualActiveVoice: String(body.actualActiveVoice || ''),
              voiceVerification: body.voiceVerification || null,
              error: verification.error
            });
          }
        }

        if (body.ok) {
          const savedDownload = await copyDownloadedFile({
            source: String(body.download ?? ''),
            downloadDir,
            outputName: current.outputName,
            project: current.project,
            material: current.material,
            audioOutputDir,
            projectOutputDirs
          });
          completed += 1;
          const audioPath = savedDownload || body.download || '';
          const audioSha256 = await sha256File(audioPath);
          completedItems.push({
            taskId: current.id,
            project: current.project,
            material: current.material || '',
            language: current.language || '',
            canonicalTaskId: current.canonicalTaskId || '',
            outputName: current.outputName || '',
            copyId: current.copyId || '',
            copyLibraryVersion: current.copyLibraryVersion || '',
            copyTextHash: current.copyTextHash || '',
            audioPath,
            expectedVoice: current.voice || '',
            actualActiveVoice: String(body.actualActiveVoice || ''),
            voiceVerified: true,
            voiceVerification: body.voiceVerification || null,
            audioSha256,
            languageContractVerified: true
          });
          await logger.info('Fish Audio task finished', {
            taskId: current.id,
            outputName: current.outputName,
            download: savedDownload || body.download || ''
          });
        } else {
          const errorMessage = String(body.error ?? '未知错误');
          const errorCode = String(body.errorCode ?? '').trim();
          const errorPhase = String(body.phase ?? '').trim();
          if (errorCode === 'LOGIN_REQUIRED' || (!errorCode && isLoginRequiredError(errorMessage))) {
            bridgeReady = false;
            loginRequired = true;
            await logger.info('Fish Audio login required; task will resume after login', {
              taskId: current.id,
              message: errorMessage
            });
            return sendJson(res, 200, {
              ok: true,
              waiting: true,
              loginRequired: true,
              message: 'Fish Audio 登录已失效，已保留当前任务等待重新登录'
            });
          }
          if (
            (errorCode === 'DOWNLOAD_FAILED' || (!errorCode && isRetryableDownloadError(errorMessage)))
            && currentRetryCount < maxDownloadRetries
          ) {
            currentRetryCount += 1;
            await logger.info('Fish Audio download retry scheduled', {
              taskId: current.id,
              retryAttempt: currentRetryCount,
              maxDownloadRetries,
              message: errorMessage
            });
            return sendJson(res, 200, {
              ok: true,
              done: false,
              retry: true,
              retryAttempt: currentRetryCount,
              maxDownloadRetries,
              retryReason: 'download'
            });
          }
          if (
            (isRetryableVoiceErrorCode(errorCode) || (!errorCode && isRetryableVoiceDialogError(errorMessage)))
            && currentUiRetryCount < maxVoiceDialogRetries
          ) {
            currentUiRetryCount += 1;
            lastTaskResultAt = Date.now();
            lastTaskResult = {
              taskId: current.id,
              ok: false,
              retry: true,
              errorCode,
              phase: errorPhase
            };
            await logger.info('Fish Audio voice-dialog retry scheduled', {
              taskId: current.id,
              retryAttempt: currentUiRetryCount,
              maxVoiceDialogRetries,
              message: errorMessage
            });
            return sendJson(res, 200, {
              ok: true,
              done: false,
              retry: true,
              retryAttempt: currentUiRetryCount,
              maxVoiceDialogRetries,
              // Keep the legacy field for older content scripts; new scripts
              // use the phase-specific name above.
              maxDownloadRetries: maxVoiceDialogRetries,
              retryReason: isPageRetryErrorCode(errorCode) ? 'page' : 'voice-selection'
            });
          }
          failed += 1;
          failedItems.push({
            taskId: current.id,
            project: current.project,
            material: current.material || '',
            language: current.language || '',
            canonicalTaskId: current.canonicalTaskId || '',
            copyId: current.copyId || '',
            copyLibraryVersion: current.copyLibraryVersion || '',
            copyTextHash: current.copyTextHash || '',
            error: errorMessage,
            errorCode,
            phase: errorPhase,
            voiceEvidence: body.voiceEvidence || null
          });
          await logger.error('Fish Audio task failed', {
            taskId: current.id,
            message: errorMessage
          });
        }

        lastTaskResultAt = Date.now();
        lastTaskResult = {
          taskId: current.id,
          ok: body.ok === true,
          errorCode: body.ok === true ? '' : String(body.errorCode ?? '').trim(),
          phase: String(body.phase ?? '').trim()
        };
        index += 1;
        current = null;
        currentRetryCount = 0;
        currentUiRetryCount = 0;
        if (onProgress) await onProgress({
          total: expectedTotal, completed, failed,
          current: null, completedItems, failedItems
        });
        const done = index >= tasks.length;
        sendJson(res, 200, { ok: true, done, completed, failed });
        if (done) scheduleFinish(server, finish);
        return;
      }

      if (req.method === 'POST' && url.pathname === '/api/bridge/heartbeat') {
        const body = await readJson(req);
        extensionConnected = true;
        lastHeartbeat = new Date().toISOString();
        lastHeartbeatAt = Date.now();
        const heartbeatVersion = String(body.scriptVersion ?? extensionVersion ?? '').trim();
        lastDiagnostics = {
          ...lastDiagnostics,
          extensionVersion: heartbeatVersion,
          requiredExtensionVersion: REQUIRED_EXTENSION_VERSION,
          extensionVersionOk: isSupportedExtensionVersion(heartbeatVersion),
          browserSessionId,
          taskId: String(body.taskId ?? ''),
          phase: String(body.phase ?? ''),
          busy: body.busy === true,
          expectedVoice: String(body.expectedVoice ?? ''),
          voiceEvidence: body.voiceEvidence || null,
          voiceNavigation: body.voiceNavigation || null,
          lastHeartbeat
        };
        if (typeof onDiagnostics === 'function') await onDiagnostics(lastDiagnostics);
        return sendJson(res, 200, { ok: true });
      }

      if (req.method === 'POST' && url.pathname === '/api/bridge/diagnostics') {
        const body = await readJson(req);
        extensionConnected = true;
        lastHeartbeat = new Date().toISOString();
        lastHeartbeatAt = Date.now();
        extensionVersion = String(body.scriptVersion ?? '').trim();
        extensionVersionOk = isSupportedExtensionVersion(extensionVersion);
        const wasReady = bridgeReady === true;
        bridgeReady = body.loggedIn === true && extensionVersionOk;
        bridgeControlsReady = Number(body.editorCount ?? 0) > 0 && Number(body.generateButtonCount ?? 0) > 0;
        if (bridgeReady && bridgeControlsReady && (!wasReady || !bridgeReadyAt)) bridgeReadyAt = Date.now();
        if (!bridgeReady || !bridgeControlsReady) bridgeReadyAt = 0;
        loginRequired = body.loggedIn === false;
        lastDiagnostics = {
          extensionVersion,
          requiredExtensionVersion: REQUIRED_EXTENSION_VERSION,
          extensionVersionOk,
          extensionVersionMessage: extensionVersionOk
            ? ''
            : `Fish插件版本${extensionVersion || '未知'}，需要重新加载${REQUIRED_EXTENSION_VERSION}`,
          loggedIn: body.loggedIn === true,
          editorCount: Number(body.editorCount ?? 0),
          generateButtonCount: Number(body.generateButtonCount ?? 0),
          windowState: String(body.windowState ?? ''),
          innerWidth: Number(body.innerWidth ?? 0),
          innerHeight: Number(body.innerHeight ?? 0),
          phase: String(body.phase ?? ''),
          busy: body.busy === true,
          taskId: String(body.taskId ?? ''),
          expectedVoice: String(body.expectedVoice ?? ''),
          actualActiveVoice: String(body.actualActiveVoice ?? ''),
          headerVoice: String(body.headerVoice ?? ''),
          editorVoice: String(body.editorVoice ?? ''),
          rightCardVoice: String(body.rightCardVoice ?? ''),
          verificationCount: Number(body.verificationCount ?? 0),
          voiceNavigation: body.voiceNavigation || null,
          lastHeartbeat
        };
        await logger.info('Fish Audio bridge diagnostics', {
          scriptVersion: String(body.scriptVersion ?? ''),
          browserSessionId,
          url: String(body.url ?? ''),
          loggedIn: body.loggedIn === true,
          editorCount: Number(body.editorCount ?? 0),
          generateButtonCount: Number(body.generateButtonCount ?? 0),
          downloadCandidateCount: Number(body.downloadCandidateCount ?? 0),
          windowState: String(body.windowState ?? ''),
          innerWidth: Number(body.innerWidth ?? 0),
          innerHeight: Number(body.innerHeight ?? 0),
          expectedVoice: String(body.expectedVoice ?? ''),
          actualActiveVoice: String(body.actualActiveVoice ?? ''),
          primarySettingVoice: String(body.primarySettingVoice ?? ''),
          headerVoice: String(body.headerVoice ?? ''),
          availableVoices: Array.isArray(body.availableVoices) ? body.availableVoices : [],
          editorCandidates: Array.isArray(body.editorCandidates) ? body.editorCandidates : []
        });
        if (typeof onDiagnostics === 'function') await onDiagnostics(lastDiagnostics);
        return sendJson(res, 200, { ok: true });
      }

      return sendJson(res, 404, { ok: false, error: 'not found' });
    } catch (error) {
      await logger.error('Fish Audio bridge request failed', {
        requestMethod: req.method,
        requestPath: req.url || '',
        message: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : ''
      });
      return sendJson(res, 500, { ok: false, error: error instanceof Error ? error.message : String(error) });
    }
  });

  server.listen(port, '127.0.0.1');
  await new Promise((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  if (typeof onBridgeListening === 'function') await onBridgeListening();

  await logger.info('Fish Audio bridge waiting for Chrome extension', {
    endpoint: `http://127.0.0.1:${port}`,
    tasks: tasks.length,
    browserSessionId
  });
  console.log(`[INFO] 请保持已登录的 Fish Audio 标签页打开；浏览器扩展将自动领取 ${tasks.length} 条任务`);

  try {
    await waitForExtensionConnection({ getConnected: () => extensionConnected, server, finish, logger });
    await waitForFinishedWithHeartbeat({
      finished,
      getLastHeartbeatAt: () => lastHeartbeatAt,
      getLastTaskResultAt: () => lastTaskResultAt,
      timeoutMs: heartbeatTimeoutMs,
      logger
    });
    if (blockedError) throw blockedError;
    await logger.info('Fish Audio bridge batch finished', { completed, failed, lastHeartbeat });
    return { total: expectedTotal, completed, failed, lastHeartbeat, completedItems, failedItems, loginRequired, ...lastDiagnostics };
  } finally {
    server.closeAllConnections?.();
    await closeBridgeServer(server);
    if (typeof onBridgeClosed === 'function') await onBridgeClosed();
  }
}

async function sha256File(filePath) {
  if (!filePath) return '';
  const content = await fs.readFile(filePath);
  return crypto.createHash('sha256').update(content).digest('hex');
}

function isRetryableDownloadError(message) {
  return /下载|download/i.test(String(message ?? ''));
}

function isRetryableVoiceDialogError(message) {
  return /切换音色.*(?:没有打开|未打开)|switch voice.*(?:did not open|not open)/i.test(String(message ?? ''));
}

function isRetryableVoiceErrorCode(code) {
  return new Set([
    'PRIMARY_VOICE_CARD_NOT_FOUND',
    'VOICE_SWITCH_CONTROL_NOT_FOUND',
    'VOICE_CATALOG_NOT_OPENED',
    'VOICE_EVIDENCE_INCOMPLETE',
    'VOICE_CATALOG_TABS_TIMEOUT',
    'VOICE_SAVED_TAB_TIMEOUT',
    'VOICE_SAVED_TAB_NOT_SELECTED',
    'VOICE_COLLECTION_TIMEOUT',
    'VOICE_TARGET_CARD_TIMEOUT',
    'VOICE_TARGET_ACTIVATION_FAILED',
    'VOICE_CATALOG_REOPEN_FAILED',
    // Neither failure has started a generation, so one retry is safe.
    'FISH_PAGE_NOT_STABLE',
    'FISH_GENERATE_NOT_STARTED'
  ]).has(String(code ?? '').trim());
}

function isPageRetryErrorCode(code) {
  return ['FISH_PAGE_NOT_STABLE', 'FISH_GENERATE_NOT_STARTED'].includes(String(code ?? '').trim());
}

function verifyActiveVoiceResult(task, body) {
  const expected = normalizeVoice(task?.voice);
  const actual = normalizeVoice(body?.actualActiveVoice);
  const evidence = body?.voiceVerification ?? {};
  const evidenceExpected = normalizeVoice(evidence.expected);
  const matchingSources = Array.isArray(evidence.matchingSources) ? evidence.matchingSources : [];
  const conflictingSources = Array.isArray(evidence.conflictingSources) ? evidence.conflictingSources : [];
  if (!expected) return { ok: false, error: 'Fish Audio 音色校验失败：任务没有期望音色' };
  if (
    actual !== expected
    || evidenceExpected !== expected
    || matchingSources.length < 2
    || conflictingSources.length > 0
  ) {
    return {
      ok: false,
      error: `Fish Audio 音色校验失败：要求“${expected}”，实际主音色“${actual || '未识别'}”`
    };
  }
  if (Number(evidence.editorCount) !== 1) {
    return { ok: false, error: `Fish Audio 音色校验失败：正文编辑区数量为 ${evidence.editorCount ?? '未知'}，必须为 1` };
  }
  return { ok: true };
}

function normalizeVoice(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
}

function isLoginRequiredError(message) {
  return /Fish Audio.*(?:尚未登录|登录)|登录.*Fish Audio/i.test(String(message ?? ''));
}

async function waitForExtensionConnection({ getConnected, server, finish, logger }) {
  const deadline = Date.now() + 3 * 60_000;
  while (!getConnected() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  if (getConnected()) return;
  await logger.error('Fish Audio extension did not connect within 3 minutes', {});
  server.close(() => finish());
  throw new Error('Fish Audio 扩展未连接：已自动打开 Chrome，请检查扩展是否加载或网络是否可用');
}

async function waitForFinishedWithHeartbeat({ finished, getLastHeartbeatAt, getLastTaskResultAt = () => 0, timeoutMs, logger }) {
  let interval;
  const watchdog = new Promise((_, reject) => {
    interval = setInterval(async () => {
      const lastSeen = Number(getLastHeartbeatAt() || 0);
      if (!lastSeen || Date.now() - lastSeen <= timeoutMs) return;
      const lastResult = Number(getLastTaskResultAt() || 0);
      // A task result is acknowledged by the bridge before the next browser
      // poll.  Give that hand-off a short grace period so a transient page
      // redraw cannot overwrite the real task error with a heartbeat error.
      if (lastResult && Date.now() - lastResult <= Math.min(30_000, timeoutMs)) return;
      clearInterval(interval);
      const error = new Error(`Fish Audio 页面已失联超过 ${Math.round(timeoutMs / 1000)} 秒，已停止本批次`);
      error.code = 'BRIDGE_HEARTBEAT_TIMEOUT';
      try {
        await logger.error('Fish Audio bridge heartbeat timed out', {
          timeoutMs,
          lastHeartbeatAt: new Date(lastSeen).toISOString()
        });
      } finally {
        reject(error);
      }
    }, Math.min(5_000, Math.max(250, Math.floor(timeoutMs / 3))));
  });
  try {
    return await Promise.race([finished, watchdog]);
  } finally {
    clearInterval(interval);
  }
}

export async function copyDownloadedFile({ source, downloadDir, outputName, project, material, audioOutputDir = '', projectOutputDirs }) {
  if (!source || !downloadDir) return source;
  const sourceInfo = await fs.stat(source);
  if (!sourceInfo.isFile()) throw new Error('Chrome 返回的下载路径不是文件');
  const configuredDirectory =
    projectOutputDirs?.get(outputDirectoryKey(project, material)) ??
    projectOutputDirs?.get(outputDirectoryKey(project));
  const projectDirectory = audioOutputDir || configuredDirectory || path.join(downloadDir, safePathSegment(project || '未分类'));
  await fs.mkdir(projectDirectory, { recursive: true });
  const extension = path.extname(source) || '.mp3';
  const destination = path.join(projectDirectory, `${outputName}${extension}`);
  if (path.resolve(source) !== path.resolve(destination)) {
    await fs.copyFile(source, destination);
    await fs.unlink(source);
  }
  return destination;
}

function safePathSegment(value) {
  const safe = String(value).replace(/[<>:"/\\|?*\x00-\x1F]/g, '_').replace(/^\.+$/, '_').trim();
  return safe || '未分类';
}

function publicTask(task) {
  return {
    id: task.id,
    project: task.project,
    language: task.language,
    voice: task.voice,
    text: task.text,
    outputName: task.outputName,
    material: task.material,
    copyId: task.copyId,
    copyTitle: task.copyTitle,
    copySource: task.copySource || '',
    copyLibraryVersion: task.copyLibraryVersion || '',
    copyTextHash: task.copyTextHash || '',
    canonicalTaskId: task.canonicalTaskId || ''
  };
}

async function logSelection(logger, task) {
  if (task.selectionMode === 'library' || task.selectionMode === 'persisted') {
    await logger.info('copy selected', {
      taskId: task.id,
      project: task.project,
      language: task.language,
      material: task.material,
      copyId: task.copyId,
      title: task.copyTitle,
      source: task.copySource,
      copyLibraryVersion: task.copyLibraryVersion,
      copyTextHash: task.copyTextHash || ''
    });
  } else {
    await logger.info('manual copy selected', {
      taskId: task.id,
      project: task.project,
      language: task.language
    });
  }
}

function scheduleFinish(server, finish) {
  setTimeout(() => {
    // Chromium may keep the polling fetch alive after it has received the
    // final result.  Close those sockets before closing the server so a
    // completed or failed batch cannot remain as a ghost Node process.
    server.closeAllConnections?.();
    server.close(() => finish());
    setTimeout(finish, 1000);
  }, 1200);
}

function isDeterministicCopyIdentityError(error) {
  const message = error instanceof Error ? error.message : String(error ?? '');
  return /^(COPY_ASSIGNMENT_CONFLICT|COPY_LEGACY_IDENTITY_UNVERIFIED|COPY_SAVED_TEXT_HASH_MISMATCH|COPY_TEXT_HASH_CONFLICT|稳定任务文案|已绑定文案)/.test(message);
}

function makeBridgeError(error, task) {
  const message = error instanceof Error ? error.message : String(error ?? '文案身份冲突');
  const code = message.startsWith('COPY_ASSIGNMENT_CONFLICT')
    ? 'COPY_ASSIGNMENT_CONFLICT'
    : message.startsWith('COPY_LEGACY_IDENTITY_UNVERIFIED')
      ? 'COPY_LEGACY_IDENTITY_UNVERIFIED'
      : message.startsWith('COPY_SAVED_TEXT_HASH_MISMATCH')
        ? 'COPY_SAVED_TEXT_HASH_MISMATCH'
        : message.startsWith('COPY_TEXT_HASH_CONFLICT')
          ? 'COPY_TEXT_HASH_CONFLICT'
          : 'COPY_IDENTITY_CONFLICT';
  const blocked = new Error(message);
  blocked.code = code;
  blocked.phase = 'copy_assignment';
  blocked.retryable = false;
  blocked.taskId = String(task?.id || '');
  blocked.details = {
    taskId: String(task?.id || ''),
    canonicalTaskId: String(task?.canonicalTaskId || ''),
    project: String(task?.project || ''),
    material: String(task?.material || ''),
    language: String(task?.language || '')
  };
  return blocked;
}

function publicBridgeError(error) {
  return {
    errorCode: String(error?.code || 'BRIDGE_BLOCKED'),
    error: String(error?.message || 'Fish Audio 桥接已停止'),
    message: String(error?.message || 'Fish Audio 桥接已停止'),
    phase: String(error?.phase || 'bridge'),
    retryable: error?.retryable === true,
    taskId: String(error?.taskId || error?.details?.taskId || ''),
    details: error?.details || null
  };
}

function closeBridgeServer(server) {
  return new Promise((resolve) => {
    if (!server.listening) return resolve();
    server.close(() => resolve());
  });
}

function setCors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Cache-Control', 'no-store');
}

function sendJson(res, status, payload) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString('utf8');
  return raw ? JSON.parse(raw) : {};
}
