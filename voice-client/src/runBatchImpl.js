import { loadRetryCompletedItems } from './retryCompletedItems.js';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { chromium } from 'playwright';
import { config } from './config.js';
import { BatchLogger } from './logger.js';
import { CopySelector } from './copySelector.js';
import { VoiceSiteAdapter } from './siteAdapter.js';
import { loadTasks } from './taskLoader.js';
import { runUserscriptBatch } from './userscriptBatch.js';
import { launchExtensionBrowser } from './extensionBrowser.js';
import { loadProjectOutputDirs, outputDirectoryKey } from './projectOutputDirs.js';
import {
  filterTasksByIds,
  filterTasksByProjects,
  selectedProjectsFromEnvironment,
  selectedTaskIdsFromEnvironment
} from './taskFilter.js';
import { applyLanguageVoiceRules, loadLanguageVoiceRules } from './languageVoiceRules.js';
import { FishBridgeRuntime } from './bridgeRuntime.js';
import { AtomicJsonFile } from './atomicJsonFile.js';
import {
  persistVerifiedCompletionLedger
} from './verifiedCompletionLedger.js';

const args = new Set(process.argv.slice(2));
const dryRun = args.has('--dry-run');
const headed = args.has('--headed');
const slowArgIndex = process.argv.indexOf('--slow');
const slowMo = slowArgIndex >= 0 ? Number.parseInt(process.argv[slowArgIndex + 1] ?? '250', 10) : config.slowMoMs;
const batchHandoffFile = new AtomicJsonFile(config.batchHandoffFile);

async function main() {
  await fs.mkdir(config.downloadDir, { recursive: true });
  await fs.mkdir(config.logDir, { recursive: true });

  const logger = new BatchLogger(config.logDir);
  await logger.init();

  const languageVoiceRules = await loadLanguageVoiceRules(config.languageVoiceRulesFile);
  const loadedTasks = applyLanguageVoiceRules(await loadTasks(config.taskFile), languageVoiceRules);
  const selectedProjects = selectedProjectsFromEnvironment(process.env.RUN_PROJECTS);
  const selectedTaskIds = selectedTaskIdsFromEnvironment(process.env.RUN_TASK_IDS);
  const tasks = filterTasksByIds(filterTasksByProjects(loadedTasks, selectedProjects), selectedTaskIds);
  const batchId = `${Date.now()}-${process.pid}`;
  const batchIdentity = {
    runGroupId: String(process.env.RUN_GROUP_ID || '').trim(),
    sourceBatchId: String(process.env.SOURCE_BATCH_ID || '').trim(),
    canonicalTaskId: String(process.env.CANONICAL_TASK_ID || '').trim(),
    manifestId: String(process.env.MANIFEST_ID || '').trim(),
    productionDate: String(process.env.PRODUCTION_DATE || '').trim(),
    audioBatch: String(process.env.AUDIO_BATCH || '').trim()
  };
  await logger.info(`loaded ${tasks.length} task(s)`, {
    taskFile: config.taskFile,
    selectedProjects: [...selectedProjects],
    selectedTaskIds: [...selectedTaskIds]
  });
  if (tasks.length === 0) throw new Error('本次选择的项目没有可执行任务');
  const missingVoiceLanguages = [...new Set(tasks
    .filter((task) => !String(task.voice ?? '').trim() || String(task.voice ?? '').trim().toLowerCase() === 'default')
    .map((task) => String(task.language ?? '').trim())
    .filter(Boolean))];
  if (missingVoiceLanguages.length > 0) {
    throw new Error(`以下语言缺少已确认的 Fish 音色映射，已阻止整批配音：${missingVoiceLanguages.join('、')}`);
  }
  const copySelector = await new CopySelector({
    libraryFile: config.copyLibraryFile,
    rulesFile: config.copyRulesFile,
    usageFile: config.copyUsageFile
  }).init();
  await logger.info(copySelector.weekly.active
    ? `copy library: weekly ${copySelector.weekly.week}`
    : `copy library: base (${copySelector.weekly.reason})`, { weeklyCopy: { week: copySelector.weekly.week, active: copySelector.weekly.active, sha256: copySelector.weekly.sha256 || '' } });

  if (dryRun) {
    for (const task of tasks) {
      const resolved = await copySelector.select(task, { consume: false });
      const copyLabel = resolved.selectionMode === 'manual'
        ? '手工文案'
        : `${resolved.material} / ${resolved.copyId}`;
      console.log(`${resolved.id}: ${resolved.project} / ${resolved.language} / ${copyLabel} -> ${resolved.outputName}`);
    }
    await logger.cleanupSuccessfulBatchLogs();
    return;
  }

  if (config.browserMode === 'extension') {
    const projectOutputDirs = await loadProjectOutputDirs(config.projectOutputDirsFile);
    const startedAt = new Date().toISOString();
    const browserSessionId = crypto.randomUUID();
    const priorCompletedItems = await loadRetryCompletedItems(
      config.batchHandoffFile,
      config.verifiedCompletionLedgerDir,
      tasks,
      projectOutputDirs
    );
    const completedTaskIds = new Set(priorCompletedItems.map((item) => String(item.taskId || '')).filter(Boolean));
    const pendingTasks = tasks.filter((task) => !completedTaskIds.has(String(task.id || '')));
    const taskIds = tasks.map((task) => String(task.id || '')).filter(Boolean);
    const canonicalTaskIds = [...new Set(tasks.map((task) => String(task.canonicalTaskId || '')).filter(Boolean))];
    const bridgeRuntime = new FishBridgeRuntime({
      filePath: path.join(config.projectRoot, 'data', 'fish_bridge_runtime.json'),
      batchId,
      taskIds,
      canonicalTaskIds,
      port: config.bridgePort,
      startedAt
    });
    await bridgeRuntime.write('starting');
    const markSignalTerminal = (signal) => {
      bridgeRuntime.markTerminalSync('cancelled', { message: `Node received ${signal}` });
      process.exit(signal === 'SIGINT' ? 130 : 143);
    };
    process.once('SIGINT', () => markSignalTerminal('SIGINT'));
    process.once('SIGTERM', () => markSignalTerminal('SIGTERM'));
    await writeBatchHandoff({
      version: 3, batchId, ...batchIdentity, status: 'running', startedAt,
      selectedProjects: [...selectedProjects], taskIds, canonicalTaskIds,
      total: tasks.length, completed: priorCompletedItems.length, failed: 0,
      pending: pendingTasks.length, completedItems: priorCompletedItems, failedItems: []
    });
    let acceptingRunningUpdates = true;
    let latestProgress = {
      total: tasks.length,
      completed: priorCompletedItems.length,
      failed: 0,
      completedItems: priorCompletedItems,
      failedItems: []
    };
    let latestBridgeDiagnostics = {};
    try {
      if (pendingTasks.length === 0) {
        await writeBatchHandoff({
          version: 3, batchId, ...batchIdentity, status: 'completed', startedAt, completedAt: new Date().toISOString(),
          selectedProjects: [...selectedProjects], taskIds, canonicalTaskIds,
          total: tasks.length, completed: priorCompletedItems.length, failed: 0,
          pending: 0, completedItems: priorCompletedItems, failedItems: [], loginRequired: false
        });
        await bridgeRuntime.markTerminal('completed', { message: 'All requested audio files were already verified' });
        await logger.info('Fish Audio retry reused all previously verified audio files', { total: tasks.length });
        return;
      }
      await launchExtensionBrowser(config, logger, browserSessionId);
      const writeRunningHandoff = async () => writeBatchHandoff({
        version: 3,
        batchId,
        ...batchIdentity,
        status: 'running',
        startedAt,
        updatedAt: new Date().toISOString(),
        selectedProjects: [...selectedProjects],
        taskIds,
        canonicalTaskIds,
        pending: Math.max(0, tasks.length - Number(latestProgress.completed || 0) - Number(latestProgress.failed || 0)),
        ...latestProgress,
        bridge: latestBridgeDiagnostics
      });
      const summary = await runUserscriptBatch({
        tasks: pendingTasks,
        copySelector,
        logger,
        downloadDir: config.downloadDir,
        audioOutputDir: config.audioOutputDir,
        projectOutputDirs,
        port: config.bridgePort,
        browserSessionId,
        totalExpected: tasks.length,
        initialCompletedItems: priorCompletedItems,
        onBridgeListening: async () => bridgeRuntime.write('listening'),
        onBridgeClosed: async () => bridgeRuntime.write('closing'),
        onProgress: async (progress) => {
          if (!acceptingRunningUpdates) return;
          latestProgress = { ...latestProgress, ...progress };
          await writeRunningHandoff();
        },
        onDiagnostics: async (diagnostics) => {
          if (!acceptingRunningUpdates) return;
          latestBridgeDiagnostics = { ...diagnostics };
          await writeRunningHandoff();
        }
      });
      acceptingRunningUpdates = false;
      await batchHandoffFile.flush();
      await writeBatchHandoff({
        version: 3,
        batchId,
        ...batchIdentity,
        status: summary.failed === 0 ? 'completed' : 'completed_with_errors',
        startedAt,
        completedAt: new Date().toISOString(),
        selectedProjects: [...selectedProjects],
        taskIds,
        canonicalTaskIds,
        total: tasks.length,
        ...summary
      });
      await bridgeRuntime.markTerminal(summary.failed === 0 ? 'completed' : 'completed_with_errors', {
        completed: Number(summary.completed || 0),
        failed: Number(summary.failed || 0)
      });
      if (summary.failed === 0) await logger.cleanupSuccessfulBatchLogs();
      return;
    } catch (error) {
      acceptingRunningUpdates = false;
      await batchHandoffFile.flush();
      const transportErrorCode = String(error?.code || '');
      const transportErrorMessage = error instanceof Error ? error.message : String(error);
      const acknowledgedTaskFailure = Array.isArray(latestProgress.failedItems)
        ? latestProgress.failedItems.at(-1)
        : null;
      const primaryErrorCode = String(acknowledgedTaskFailure?.errorCode || transportErrorCode);
      const primaryErrorMessage = String(acknowledgedTaskFailure?.error || transportErrorMessage);
      await writeBatchHandoff({
        version: 3, batchId, ...batchIdentity, status: 'failed', startedAt, completedAt: new Date().toISOString(),
        selectedProjects: [...selectedProjects], total: tasks.length,
        taskIds,
        canonicalTaskIds,
        pending: Math.max(0, tasks.length - Number(latestProgress.completed || 0) - Number(latestProgress.failed || 0)),
        ...latestProgress,
        bridge: latestBridgeDiagnostics,
        errorCode: primaryErrorCode,
        error: primaryErrorMessage,
        transportErrorCode: transportErrorCode && transportErrorCode !== primaryErrorCode ? transportErrorCode : '',
        transportError: transportErrorMessage !== primaryErrorMessage ? transportErrorMessage : ''
      });
      await bridgeRuntime.markTerminal('failed', {
        message: primaryErrorMessage,
        errorCode: primaryErrorCode,
        transportErrorCode
      });
      if (acknowledgedTaskFailure && transportErrorCode === 'BRIDGE_HEARTBEAT_TIMEOUT') {
        const surfaced = new Error(primaryErrorMessage);
        surfaced.code = primaryErrorCode || 'VOICE_TASK_FAILED';
        surfaced.details = {
          transportErrorCode,
          transportError: transportErrorMessage,
          failedTask: acknowledgedTaskFailure
        };
        throw surfaced;
      }
      throw error;
    }
  }

  const context = await chromium.launchPersistentContext(config.browserUserDataDir, {
    executablePath: config.browserExecutable,
    headless: headed ? false : config.headless,
    slowMo: Number.isFinite(slowMo) ? slowMo : 0,
    acceptDownloads: true,
    ...(config.browserProfileDirectory ? { args: [`--profile-directory=${config.browserProfileDirectory}`] } : {}),
    ...(config.browserProxyServer ? { proxy: { server: config.browserProxyServer } } : {})
  });

  const page = await context.newPage();
  const site = new VoiceSiteAdapter(page, config, logger);
  let failed = 0;
  let finished = false;

  try {
    await site.open();
    await site.ensureReady();
    await site.ensureGenerationSupported();

    for (const task of tasks) {
      try {
        const resolved = await copySelector.select(task, { consume: true });
        if (resolved.selectionMode === 'library' || resolved.selectionMode === 'persisted') {
          await logger.info('copy selected', {
            taskId: resolved.id,
            project: resolved.project,
            language: resolved.language,
            material: resolved.material,
            copyId: resolved.copyId,
            title: resolved.copyTitle,
            source: resolved.copySource,
            copyLibraryVersion: resolved.copyLibraryVersion,
            copyTextHash: resolved.copyTextHash || ''
          });
        } else {
          await logger.info('manual copy selected', { taskId: resolved.id, project: resolved.project, language: resolved.language });
        }
        await site.generate(resolved);
      } catch (error) {
        failed += 1;
        await logger.error('task failed', {
          taskId: task.id,
          message: error instanceof Error ? error.message : String(error)
        });
      }
    }
    finished = true;
  } finally {
    await context.close();
    await logger.info('batch finished', { failed });
    if (finished && failed === 0) await logger.cleanupSuccessfulBatchLogs();
  }
}


async function writeBatchHandoff(payload) {
  await batchHandoffFile.write(payload);
  await persistVerifiedCompletionLedger(config.verifiedCompletionLedgerDir, payload);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
