import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const extension = new URL('../fish-audio-extension/', import.meta.url);
const source = fs.readFileSync(new URL('content.js', extension), 'utf8');
// Execute the production selector functions, without starting the extension,
// polling a bridge or touching a real Fish tab.
const selectors = source.slice(source.indexOf('  function findVoiceOption('), source.indexOf('  function voiceOptionScore('));

class Element {
  constructor(tag, text = '', width = 200, height = 20) {
    this.tagName = tag;
    this.innerText = text;
    this.children = [];
    this.parentElement = null;
    this.width = width;
    this.height = height;
    this.visible = true;
  }
  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      this.children.push(child);
    }
    return this;
  }
  contains(element) {
    return element === this || this.children.some((child) => child.contains(element));
  }
  getBoundingClientRect() { return { width: this.width, height: this.height }; }
  querySelectorAll(query) {
    const tags = query.split(',').map((part) => part.trim());
    return this.children.flatMap((child) => [
      ...(tags.includes(child.tagName) ? [child] : []),
      ...child.querySelectorAll(query)
    ]);
  }
}

function card(name, { button = true, visible = true } = {}) {
  const result = new Element('section', `${name} · author description`, 440, 130);
  const label = new Element('span', name);
  const use = new Element('button', '使用', 64, 32);
  result.append(label);
  if (button) result.append(use);
  result.visible = visible;
  label.visible = visible;
  use.visible = visible;
  return { card: result, label, use };
}

function environment(...cards) {
  const root = new Element('section', '', 1500, 700).append(...cards.map((entry) => entry.card));
  const context = {
    document: { body: new Element('body') }, innerWidth: 2000,
    visible: (element) => element.visible,
    normalizedText: (element) => String(element?.innerText || '').replace(/\s+/g, ' ').trim()
  };
  vm.runInNewContext(fs.readFileSync(new URL('active-voice.js', extension), 'utf8'), context);
  context.activeVoiceApi = context.FishActiveVoice;
  vm.runInNewContext(selectors, context);
  return { context, find: (name, excluded) => context.findVoiceOption(root, name, excluded) };
}

test('Farid Dieck matches the screenshot name Farid dieck and its own Use button', () => {
  const other = card('chand');
  const target = card('Farid dieck');
  const result = environment(other, target).find('Farid Dieck');
  assert.equal(result.card, target.card);
  assert.equal(result.useControl, target.use);
});

test('whitespace and Cyrillic case follow the existing active-voice comparison', () => {
  const target = card('Молодой Аналитик');
  assert.equal(environment(target).find('  МОЛОДОЙ   аналитик ').useControl, target.use);
});

test('no fuzzy, prefix or creator-name matching', () => {
  const target = card('Farid dieck');
  const { find } = environment(target);
  for (const name of ['Farid', 'Farid dieck 2', 'author', '', ' ']) assert.equal(find(name), null);
});

test('a missing target button cannot borrow an adjacent card button', () => {
  assert.equal(environment(card('Farid dieck', { button: false }), card('chand')).find('Farid Dieck'), null);
});

test('multiple names differing only in case are ambiguous and must not click', () => {
  assert.equal(environment(card('Farid dieck'), card('Farid Dieck')).find('Farid Dieck'), null);
});

test('multiple Use buttons in a card are rejected', () => {
  const target = card('Farid dieck');
  target.card.append(new Element('button', '使用', 64, 32));
  assert.equal(environment(target).find('Farid Dieck'), null);
});

test('hidden and excluded current-selector names cannot become target options', () => {
  const hidden = card('Farid dieck', { visible: false });
  const target = card('Farid dieck');
  assert.equal(environment(hidden).find('Farid Dieck'), null);
  assert.equal(environment(target).find('Farid Dieck', target.card), null);
});

test('missing verification module fails closed', () => {
  const env = environment(card('Farid dieck'));
  env.context.activeVoiceApi = null;
  assert.equal(env.find('Farid Dieck'), null);
});

test('post-click verification still rejects a different primary voice', () => {
  const { context } = environment();
  assert.equal(context.activeVoiceApi.evaluate({ expectedVoice: 'Farid Dieck', headerVoice: 'Farid dieck', rightCardVoice: 'Farid dieck', editorCount: 1 }).ok, true);
  assert.equal(context.activeVoiceApi.evaluate({ expectedVoice: 'Farid Dieck', headerVoice: 'chand', rightCardVoice: 'Farid dieck', editorCount: 1 }).ok, false);
});
