import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { applyLanguageVoiceRules, loadLanguageVoiceRules } from '../src/languageVoiceRules.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('十种语言音色映射完整', async () => {
  const rules = await loadLanguageVoiceRules(path.join(root, 'data', 'language_voice_rules.csv'));
  assert.equal(rules.size, 10);
  assert.equal(rules.get('英语'), 'chand');
  assert.equal(rules.get('俄语'), 'Молодой Аналитик');
  assert.equal(rules.get('韩语'), 'JWY0521');
});

test('default 使用语言音色且人工指定音色优先', () => {
  const rules = new Map([['德语', 'Ben']]);
  const tasks = applyLanguageVoiceRules([
    { language: '德语', voice: 'default' },
    { language: '德语', voice: 'My Voice' }
  ], rules);
  assert.equal(tasks[0].voice, 'Ben');
  assert.equal(tasks[1].voice, 'My Voice');
});
