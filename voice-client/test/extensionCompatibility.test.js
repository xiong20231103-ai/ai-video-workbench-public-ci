import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const testDir = path.dirname(fileURLToPath(import.meta.url));
const extensionDir = path.resolve(testDir, '..', 'fish-audio-extension');

test('extension manifest declares the Chromium MV3 background worker', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(extensionDir, 'manifest.json'), 'utf8'));

  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.version, '1.3.47');
  assert.ok(manifest.permissions.includes('debugger'));
  assert.deepEqual(manifest.content_scripts[0].js, ['active-voice.js', 'catalog-selectors.js', 'content.js']);
  assert.equal(manifest.background.service_worker, 'background.js');
  assert.equal(manifest.background.scripts, undefined);
});

test('extension scripts select the available WebExtensions API namespace', () => {
  for (const filename of ['background.js', 'content.js']) {
    const source = fs.readFileSync(path.join(extensionDir, filename), 'utf8');
    assert.match(source, /globalThis\.browser \?\? globalThis\.chrome/);
    assert.doesNotMatch(source, /\bchrome\.(?:runtime|downloads)\b/);
  }
});

test('Firefox can start the background listener without Chromium filename events', () => {
  const source = fs.readFileSync(path.join(extensionDir, 'background.js'), 'utf8');
  assert.match(source, /onDeterminingFilename\?\.addListener/);
  assert.match(source, /extensionApi\.runtime\.onMessage\.addListener/);
});

test('voice switching uses only the hovered primary-card switch control', () => {
  const source = fs.readFileSync(path.join(extensionDir, 'content.js'), 'utf8');

  assert.match(source, /revealSavedVoiceOption/);
  assert.match(source, /\['已收藏', 'Saved'\]/);
  assert.match(source, /\['音频1', 'Audio 1'\]/);
  assert.match(source, /findTargetVoiceCard/);
  assert.match(source, /findUseControlInsideVoiceCard/);
  assert.match(source, /targetVoiceCard/);
  assert.match(source, /openVoiceCatalogFromPrimary/);
  assert.match(source, /findPrimaryVoiceCard/);
  assert.match(source, /hoverPrimaryVoiceCard/);
  assert.match(source, /findPrimaryVoiceNameElement/);
  assert.match(source, /findRightPrimaryVoiceNameElement/);
  assert.match(source, /span\.min-w-0\.flex-1\.truncate/);
  assert.match(source, /nameElement\.parentElement/);
  assert.match(source, /hoverPrimaryVoiceName/);
  assert.match(source, /voiceNameElement\.click\(\)/);
  assert.equal((source.match(/voiceNameElement\.click\(\)/g) || []).length, 1);
  assert.match(source, /primaryVoiceActivation/);
  assert.match(source, /nativeHoverPrimaryVoiceCard/);
  assert.match(source, /waitForStablePrimaryVoiceWorkspace/);
  assert.match(source, /isUsablePrimaryVoiceCard/);
  assert.match(source, /FISH_PAGE_NOT_STABLE/);
  assert.match(source, /stableMs < 1_800/);
  assert.match(source, /move-pointer-to-primary-voice-card/);
  assert.match(source, /voiceNameElementDetected/);
  assert.match(source, /voiceNameClicked/);
  assert.match(source, /PRIMARY_VOICE_NAME_NOT_FOUND/);
  assert.match(source, /hover-and-click-primary-voice-name/);
  assert.match(source, /activatePrimaryVoiceName/);
  assert.match(source, /browserPointerActivation/);
  assert.doesNotMatch(source, /add another voice\)\/i\.test\(text\);\s*$/m);
  assert.match(source, /findVoiceSwitchControl/);
  assert.match(source, /VOICE_SWITCH_CONTROL_NOT_FOUND/);
  assert.match(source, /VOICE_CATALOG_NOT_OPENED/);
  assert.match(source, /add another/);
  assert.match(source, /trash\|delete\|remove/);
  assert.match(source, /findVoiceCatalog/);
  assert.match(source, /isFullVoiceCatalog/);
  assert.match(source, /rect\.left >= innerWidth \* 0\.55/);
  assert.match(source, /root\.querySelectorAll/);
  assert.match(source, /voiceNavigationDiagnostics/);
  assert.match(source, /primaryVoiceCard/);
  assert.match(source, /hoverCompleted/);
  assert.match(source, /switchButtonDetected/);
  assert.match(source, /catalogDetected/);
  assert.match(source, /savedTabDetected/);
  assert.match(source, /waitForStableCatalogControl/);
  assert.match(source, /VOICE_SAVED_TAB_TIMEOUT/);
  assert.match(source, /VOICE_COLLECTION_TIMEOUT/);
  assert.match(source, /VOICE_TARGET_CARD_TIMEOUT/);
  assert.match(source, /normalizedControlLabel/);
  assert.match(source, /isSelectedControl/);
  assert.match(source, /catalogSelectors/);
  assert.match(source, /collectionDetected/);
  assert.match(source, /targetVoiceDetected/);
  assert.doesNotMatch(source, /openVoiceDialogFromSettings/);
  assert.match(source, /assertActiveVoice/);
  assert.match(source, /primarySettingVoice/);
  assert.match(source, /headerVoice/);
  assert.match(source, /editorVoice/);
  assert.match(source, /rightCardVoice/);
  assert.doesNotMatch(source, /findRightVoiceSwitchButton/);
  assert.match(source, /interactionPause/);
  assert.match(source, /range\.selectNodeContents\(editor\)/);
  assert.match(source, /allEmpty && \(count === 0 \|\| count == null\)/);
  assert.doesNotMatch(source, /execCommand\('selectAll'/);
  assert.match(source, /await fillEditor\(editor, task\.text\)/);
  assert.match(source, /interactionPause\(25, 65\)/);
  assert.doesNotMatch(source, /findRightCurrentVoice/);
  assert.match(source, /voiceSettingCandidates/);
  assert.match(source, /isVoiceSettingsArea/);
  assert.match(source, /Fish Audio 页面尚未加载完成/);
  assert.match(source, /innerWidth \* 0\.55/);
  assert.match(source, /attempt < 3/);
  assert.match(source, /characterCountTolerance/);
  assert.match(source, /Math\.abs\(actual - expectedLength\) <= tolerance/);
  assert.match(source, /new TextEncoder\(\)\.encode\(task\.text\)\.length/);
  assert.match(source, /BROWSER_SESSION_ID/);
  assert.match(source, /loadBrowserSessionId/);
  assert.match(source, /editorCandidateReason/);
  assert.doesNotMatch(source, /\.filter\(\(element\) => !element\.closest\('\[role="dialog"\]'\)\)/);
  assert.match(source, /maximize-current-fish-window/);
  assert.match(source, /close-current-fish-tab/);
});

test('background limits window changes to the sender Fish tab', () => {
  const source = fs.readFileSync(path.join(extensionDir, 'background.js'), 'utf8');
  assert.match(source, /isFishSender\(sender\)/);
  assert.match(source, /extensionApi\.windows\.update\(sender\.tab\.windowId/);
  assert.match(source, /extensionApi\.tabs\.remove\(sender\.tab\.id\)/);
  assert.match(source, /hover-and-click-primary-voice-name/);
  assert.match(source, /extensionApi\.debugger\.attach/);
  assert.match(source, /Input\.dispatchMouseEvent/);
  assert.match(source, /extensionApi\.debugger\.detach/);
});

test('generate click is confirmed and falls back before failing fast', () => {
  const source = fs.readFileSync(path.join(extensionDir, 'content.js'), 'utf8');
  assert.match(source, /await startGeneration\(editor, generateButton, beforeControls, beforePlayerSignature\)/);
  assert.doesNotMatch(source, /^\s*generateButton\.click\(\);\s*\n\s*setPhase\('generating'\)/m);
  assert.match(source, /dispatchPointerClick\(retryButton\)/);
  assert.match(source, /dispatchCtrlEnter\(editor\)/);
  assert.match(source, /FISH_GENERATE_NOT_STARTED/);
  assert.match(source, /document\.visibilityState/);
  assert.match(source, /errorCode === 'FISH_PAGE_NOT_STABLE'[\s\S]{0,200}location\.reload\(\)/);
});
