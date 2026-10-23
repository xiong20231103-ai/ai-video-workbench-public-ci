import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const testDir = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.resolve(testDir, '..', 'fish-audio-extension', 'active-voice.js'), 'utf8');
const context = { globalThis: {} };
vm.runInNewContext(source, context);
const { evaluate, headerVoiceFromSelector, voiceNameFromCard } = context.globalThis.FishActiveVoice;

test('speaker header evidence rejects a page-sized text container', () => {
  assert.equal(headerVoiceFromSelector('Fish Audio '.repeat(20)), '');
  assert.equal(headerVoiceFromSelector('Ben'), 'Ben');
  assert.equal(headerVoiceFromSelector('Молодой Аналитик'), 'Молодой Аналитик');
});

test('a secondary saved Ben row does not pass while Russian remains primary', () => {
  const result = evaluate({
    expectedVoice: 'Ben',
    headerVoice: 'Молодой Аналитик',
    editorVoice: 'Молодой Аналитик',
    rightCardVoice: 'Молодой Аналитик',
    editorCount: 1,
    availableVoices: ['Молодой Аналитик', 'Ben']
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'voice_evidence_conflict');
  assert.equal(result.actualActiveVoice, 'Молодой Аналитик');
});

test('only a matching primary voice with one editor may generate', () => {
  const result = evaluate({
    expectedVoice: 'Ben',
    headerVoice: 'Ben',
    rightCardVoice: 'Ben',
    editorCount: 1,
    availableVoices: ['Ben']
  });
  assert.equal(result.ok, true);
  assert.equal(result.actualActiveVoice, 'Ben');
});

test('generic page heading is ignored rather than treated as a conflicting speaker', () => {
  const result = evaluate({
    expectedVoice: 'Ben',
    headerVoice: 'Ben',
    editorVoice: '文本转语音',
    rightCardVoice: 'Ben',
    editorCount: 1
  });
  assert.equal(result.ok, true);
  assert.equal(result.evidence.editor, '');
});
test('Chinese voice synthesis heading is ignored as page chrome', () => {
  const result = evaluate({
    expectedVoice: 'Ben',
    headerVoice: 'Ben',
    editorVoice: '语音合成',
    rightCardVoice: 'Ben',
    editorCount: 1
  });
  assert.equal(result.ok, true);
  assert.equal(result.evidence.editor, '');
});

test('conflicting speaker controls and multiple editors are blocked', () => {
  assert.equal(evaluate({
    expectedVoice: 'Ben',
    headerVoice: 'Ben',
    rightCardVoice: 'Молодой Аналитик',
    editorCount: 1,
    availableVoices: ['Ben', 'Молодой Аналитик']
  }).ok, false);
  assert.equal(evaluate({
    expectedVoice: 'Ben',
    headerVoice: 'Ben',
    rightCardVoice: 'Ben',
    editorCount: 2,
    availableVoices: ['Ben']
  }).reason, 'ambiguous_editor_count');
});

test('right-side voice card name ignores gender and metadata', () => {
  assert.equal(
    voiceNameFromCard('Chand · 男性\nA professional and energetic middle-aged man\nEN 男性 中年\n10.42 K 126'),
    'Chand'
  );
  assert.equal(voiceNameFromCard('Молодой Аналитик · 男性'), 'Молодой Аналитик');
});

test('voice consensus is case-insensitive and requires two matching sources', () => {
  assert.equal(evaluate({
    expectedVoice: 'chand',
    headerVoice: 'Chand',
    rightCardVoice: 'CHAND',
    editorCount: 1
  }).ok, true);
  assert.equal(evaluate({
    expectedVoice: 'chand',
    rightCardVoice: 'Chand',
    editorCount: 1
  }).reason, 'insufficient_voice_evidence');
});
