import fs from 'node:fs/promises';
import { spawn } from 'node:child_process';

/**
 * Open Fish Audio in a new Chrome window.  The current home deployment uses
 * the person's normal Chrome profile so its existing Fish login is retained;
 * the extension closes only the Fish tab it opened, never the Chrome process.
 */
export async function launchExtensionBrowser(config, logger, browserSessionId = '') {
  if (!(await exists(config.browserExecutable))) {
    throw new Error(`Google Chrome was not found: ${config.browserExecutable}`);
  }

  if (config.browserUserDataDir) {
    await fs.mkdir(config.browserUserDataDir, { recursive: true });
  }
  const targetUrl = withBrowserSession(config.targetUrl, browserSessionId);
  const args = [
    '--no-first-run',
    '--no-default-browser-check',
    '--start-maximized',
    '--new-window',
    targetUrl
  ];
  if (config.browserUserDataDir) {
    args.unshift(`--user-data-dir=${config.browserUserDataDir}`);
  }
  const child = spawn(config.browserExecutable, args, {
    detached: true,
    stdio: 'ignore',
    windowsHide: false
  });
  child.unref();
  await logger.info('Fish Audio browser started in a new maximized window.', {
    targetUrl: config.targetUrl,
    browserSessionId,
    browser: config.browserExecutable
  });
}

function withBrowserSession(targetUrl, browserSessionId) {
  if (!browserSessionId) return targetUrl;
  const url = new URL(targetUrl);
  // A URL fragment never leaves this machine for Fish Audio.  It identifies
  // only the local bridge session so unrelated Fish tabs cannot claim work.
  url.hash = `fish-batch=${encodeURIComponent(browserSessionId)}`;
  return url.toString();
}

async function exists(value) {
  try {
    await fs.access(value);
    return true;
  } catch {
    return false;
  }
}
