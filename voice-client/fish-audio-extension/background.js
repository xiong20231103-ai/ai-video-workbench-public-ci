const extensionApi = globalThis.browser ?? globalThis.chrome;

let pendingBaseName = '';
let lastDownloadId = null;
let lastDownloadAt = 0;

extensionApi.downloads.onCreated.addListener((item) => {
  lastDownloadId = item.id;
  lastDownloadAt = Date.now();
});

if (extensionApi.downloads.onDeterminingFilename?.addListener) {
  extensionApi.downloads.onDeterminingFilename.addListener((item, suggest) => {
    if (!pendingBaseName) return;
    const extension = extensionFromName(item.filename) || '.mp3';
    const filename = `BatchVoiceClientTemp/${sanitizeBaseName(pendingBaseName)}${extension}`;
    pendingBaseName = '';
    lastDownloadId = item.id;
    lastDownloadAt = Date.now();
    suggest({ filename, conflictAction: 'uniquify' });
  });
}

extensionApi.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'maximize-current-fish-window') {
    return updateCurrentFishWindow(sender, { state: 'maximized' }, sendResponse);
  }

  if (message?.type === 'current-fish-window-state') {
    return readCurrentFishWindow(sender, sendResponse);
  }

  // Fish only reveals the crossed-arrows control while its primary voice name
  // is really hovered.  Dispatching MouseEvent from a content script does not
  // update CSS :hover, so use Chromium's tab-scoped input channel instead.
  // This is deliberately limited to the currently sending Fish tab and is
  // detached immediately after one move/click sequence.
  if (message?.type === 'hover-and-click-primary-voice-name') {
    return activatePrimaryVoiceName(sender, message, sendResponse);
  }

  if (message?.type === 'move-pointer-to-primary-voice-card') {
    return movePointer(sender, message, sendResponse, 'primary voice card');
  }

  if (message?.type === 'close-current-fish-tab') {
    if (!isFishSender(sender)) {
      sendResponse({ ok: false, error: 'Only a Fish Audio tab can close itself' });
      return;
    }
    extensionApi.tabs.remove(sender.tab.id)
      .then(() => sendResponse({ ok: true, tabId: sender.tab.id }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === 'prepare-download') {
    pendingBaseName = sanitizeBaseName(message.baseName || 'fish-audio');
    sendResponse({ ok: true, since: Date.now() });
    return;
  }

  if (message?.type === 'download-url') {
    const baseName = sanitizeBaseName(message.baseName || 'fish-audio');
    const extension = extensionFromUrl(message.url) || '.mp3';
    extensionApi.downloads.download({
      url: message.url,
      filename: `BatchVoiceClientTemp/${baseName}${extension}`,
      conflictAction: 'uniquify',
      saveAs: false
    }).then((id) => {
      lastDownloadId = id;
      lastDownloadAt = Date.now();
      sendResponse({ ok: true, id });
    }).catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === 'latest-download') {
    const since = Number(message.since || 0);
    if (!lastDownloadId || lastDownloadAt < since) {
      sendResponse({ ok: true, id: null });
      return;
    }
    extensionApi.downloads.search({ id: lastDownloadId }).then((items) => {
      const item = items[0];
      sendResponse({
        ok: true,
        id: item?.id ?? null,
        state: item?.state ?? null,
        filename: item?.filename ?? '',
        error: item?.error ?? ''
      });
    }).catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
});

function isFishSender(sender) {
  return Boolean(sender?.tab?.id && sender?.tab?.windowId != null && /^https:\/\/(?:[^/]+\.)?fish\.audio\//i.test(sender.tab.url || ''));
}

function updateCurrentFishWindow(sender, update, sendResponse) {
  if (!isFishSender(sender)) {
    sendResponse({ ok: false, error: 'Only a Fish Audio tab can control its window' });
    return;
  }
  extensionApi.windows.update(sender.tab.windowId, update)
    .then(() => extensionApi.windows.get(sender.tab.windowId))
    .then((windowInfo) => sendResponse({ ok: windowInfo?.state === 'maximized', state: windowInfo?.state || '', windowId: sender.tab.windowId, tabId: sender.tab.id }))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
}

function readCurrentFishWindow(sender, sendResponse) {
  if (!isFishSender(sender)) {
    sendResponse({ ok: false, error: 'Only a Fish Audio tab can read its window' });
    return;
  }
  extensionApi.windows.get(sender.tab.windowId)
    .then((windowInfo) => sendResponse({ ok: true, state: windowInfo?.state || '', windowId: sender.tab.windowId, tabId: sender.tab.id }))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
}

function activatePrimaryVoiceName(sender, message, sendResponse) {
  if (!isFishSender(sender)) {
    sendResponse({ ok: false, error: 'Only a Fish Audio tab can activate its primary voice name' });
    return;
  }
  if (!extensionApi.debugger?.attach || !extensionApi.debugger?.sendCommand) {
    sendResponse({ ok: false, error: 'Browser-level pointer activation is unavailable in this browser' });
    return;
  }

  const x = Number(message?.x);
  const y = Number(message?.y);
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x > 20_000 || y > 20_000) {
    sendResponse({ ok: false, error: 'Invalid primary voice name coordinates' });
    return;
  }

  return dispatchPointer(sender, x, y, true, sendResponse, 'primary voice name');
}

function movePointer(sender, message, sendResponse, label) {
  if (!isFishSender(sender)) {
    sendResponse({ ok: false, error: `Only a Fish Audio tab can move to its ${label}` });
    return;
  }
  if (!extensionApi.debugger?.attach || !extensionApi.debugger?.sendCommand) {
    sendResponse({ ok: false, error: 'Browser-level pointer activation is unavailable in this browser' });
    return;
  }
  const x = Number(message?.x);
  const y = Number(message?.y);
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x > 20_000 || y > 20_000) {
    sendResponse({ ok: false, error: `Invalid ${label} coordinates` });
    return;
  }
  return dispatchPointer(sender, x, y, false, sendResponse, label);
}

function dispatchPointer(sender, x, y, click, sendResponse, label) {
  const debuggee = { tabId: sender.tab.id };
  (async () => {
    let attached = false;
    try {
      await extensionApi.debugger.attach(debuggee, '1.3');
      attached = true;
      await extensionApi.debugger.sendCommand(debuggee, 'Input.dispatchMouseEvent', {
        type: 'mouseMoved', x, y, button: 'none', buttons: 0
      });
      await delay(120);
      if (click) {
        await extensionApi.debugger.sendCommand(debuggee, 'Input.dispatchMouseEvent', {
          type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1
        });
        await extensionApi.debugger.sendCommand(debuggee, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1
        });
      }
      sendResponse({ ok: true, method: 'chromium-debugger-pointer', action: click ? 'move-and-click' : 'move', label, x, y, tabId: sender.tab.id });
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    } finally {
      if (attached) {
        try {
          await extensionApi.debugger.detach(debuggee);
        } catch {
          // The tab can navigate while Fish changes its controls.  There is no
          // remaining debugger attachment in that case.
        }
      }
    }
  })();
  return true;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function sanitizeBaseName(value) {
  return String(value).replace(/[<>:"/\\|?*\x00-\x1F]/g, '_').replace(/\s+/g, '_').slice(0, 180);
}

function extensionFromName(value) {
  const match = String(value || '').match(/\.(mp3|wav|m4a|ogg|flac|aac)$/i);
  return match ? `.${match[1].toLowerCase()}` : '';
}

function extensionFromUrl(value) {
  try {
    return extensionFromName(new URL(value).pathname);
  } catch {
    return '';
  }
}
