import 'dotenv/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, '..');

function boolFromEnv(value, fallback) {
  if (value == null || value === '') return fallback;
  return ['1', 'true', 'yes', 'y'].includes(String(value).toLowerCase());
}

function intFromEnv(value, fallback) {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  projectRoot,
  targetUrl: process.env.TARGET_URL ?? 'https://example.com/voice',
  browserExecutable: path.resolve(
    projectRoot,
    process.env.BROWSER_EXECUTABLE ?? '.browsers/chrome-win64/chrome.exe'
  ),
  // Leave empty to use the person's normal Chrome profile and its existing
  // login cookies.  A dedicated profile is useful for isolation but can cause
  // anti-bot challenges on Fish Audio.
  browserUserDataDir: process.env.BROWSER_USER_DATA_DIR?.trim()
    ? path.resolve(projectRoot, process.env.BROWSER_USER_DATA_DIR.trim())
    : '',
  browserProfileDirectory: process.env.BROWSER_PROFILE_DIRECTORY?.trim() || '',
  browserProxyServer: process.env.BROWSER_PROXY_SERVER?.trim() || '',
  browserMode: process.env.BROWSER_MODE?.trim() || 'extension',
  extensionDirectory: path.resolve(projectRoot, process.env.EXTENSION_DIRECTORY ?? 'fish-audio-extension'),
  bridgePort: intFromEnv(process.env.BRIDGE_PORT, 5178),
  headless: boolFromEnv(process.env.HEADLESS, false),
  slowMoMs: intFromEnv(process.env.SLOW_MO_MS, 0),
  taskFile: path.resolve(projectRoot, process.env.TASK_FILE ?? 'data/tasks.csv'),
  copyLibraryFile: path.resolve(projectRoot, process.env.COPY_LIBRARY_FILE ?? 'data/copy_library.csv'),
  copyRulesFile: path.resolve(projectRoot, process.env.COPY_RULES_FILE ?? 'data/project_copy_rules.csv'),
  copyUsageFile: path.resolve(projectRoot, process.env.COPY_USAGE_FILE ?? 'data/copy_usage.json'),
  projectOutputDirsFile: path.resolve(projectRoot, process.env.PROJECT_OUTPUT_DIRS_FILE ?? 'data/project_output_dirs.csv'),
  batchHandoffFile: path.resolve(projectRoot, process.env.BATCH_HANDOFF_FILE ?? 'data/last_batch_handoff.json'),
  verifiedCompletionLedgerDir: path.resolve(
    projectRoot,
    process.env.VERIFIED_COMPLETION_LEDGER_DIR ?? 'data/verified_voice_completions'
  ),
  languageVoiceRulesFile: path.resolve(projectRoot, process.env.LANGUAGE_VOICE_RULES_FILE ?? 'data/language_voice_rules.csv'),
  // Remote workbench runs provide the exact dated handoff folder.  Local
  // interactive batches leave this empty and keep their existing mappings.
  audioOutputDir: process.env.AUDIO_OUTPUT_DIR?.trim() || '',
  downloadDir: path.resolve(projectRoot, process.env.DOWNLOAD_DIR ?? 'downloads'),
  logDir: path.resolve(projectRoot, process.env.LOG_DIR ?? 'logs')
};
