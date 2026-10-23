import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

test('直接收藏列表必须同时具备专属说明和精确目标音色卡', () => {
  const selectors = loadSelectors();
  assert.equal(selectors.isDirectSavedList('已收藏 你收藏的声音，方便快速使用。 Farid Dieck', true), true);
  assert.equal(selectors.isDirectSavedList('已收藏 你收藏的声音，方便快速使用。', false), false);
  assert.equal(selectors.isDirectSavedList('探索 Farid Dieck', true), false);
  assert.equal(selectors.isDirectSavedList('已收藏 音频1', false), false);
});

const testDir = path.dirname(fileURLToPath(import.meta.url));
const selectorSource = fs.readFileSync(
  path.resolve(testDir, '..', 'fish-audio-extension', 'catalog-selectors.js'),
  'utf8'
);

class FakeElement {
  constructor({ tag = 'div', text = '', ariaLabel = '', title = '', role = '', selected = '', visible = true, inView = true, cursor = 'default' } = {}) {
    this.tagName = tag.toUpperCase();
    this.innerText = text;
    this.textContent = text;
    this.className = selected === 'class' ? 'active selected' : '';
    this.isConnected = true;
    this.visible = visible;
    this.inView = inView;
    this.cursor = cursor;
    this.parentElement = null;
    this.children = [];
    this.attributes = new Map([
      ['aria-label', ariaLabel], ['title', title], ['role', role], ['aria-selected', selected === 'aria' ? 'true' : '']
    ]);
  }

  append(child) {
    child.parentElement = this;
    this.children.push(child);
    this.textContent = `${this.textContent} ${child.textContent}`.trim();
    this.innerText = this.textContent;
    return this;
  }

  getAttribute(name) { return this.attributes.get(name) || ''; }

  matches(selector) {
    return selector.split(',').some((part) => {
      const value = part.trim();
      return (value === 'button' && this.tagName === 'BUTTON')
        || (value === '[role="tab"]' && this.getAttribute('role') === 'tab')
        || (value === '[role="button"]' && this.getAttribute('role') === 'button')
        || (value === '[tabindex="0"]' && this.getAttribute('tabindex') === '0');
    });
  }

  closest(selector) {
    for (let node = this; node; node = node.parentElement) {
      if (node.matches(selector)) return node;
    }
    return null;
  }

  querySelectorAll() {
    const result = [];
    const visit = (node) => {
      for (const child of node.children) {
        result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }
}

function loadSelectors() {
  const context = { document: { body: null } };
  vm.runInNewContext(selectorSource, context);
  return context.FishCatalogSelectors;
}

test('语义选择器能从嵌套span找到已收藏tab并保留选中状态', () => {
  const selectors = loadSelectors();
  const root = new FakeElement({ tag: 'div' });
  const tab = new FakeElement({ tag: 'button', role: 'tab', selected: 'aria' });
  tab.append(new FakeElement({ tag: 'span', text: ' 已收藏… ' }));
  root.append(tab);
  root.append(new FakeElement({ tag: 'span', text: '已收藏', visible: false }));
  const selected = selectors.findExactTextControl(root, ['已收藏', 'Saved'], {
    visible: (element) => element.visible,
    inViewport: (element) => element.inView,
    isClickableElement: () => false
  });
  assert.equal(selected, tab);
  assert.equal(selectors.isSelectedControl(selected), true);
});

test('语义选择器支持Saved的aria-label并排除不可见重复控件', () => {
  const selectors = loadSelectors();
  const root = new FakeElement({ tag: 'div' });
  const hidden = new FakeElement({ tag: 'button', role: 'tab', ariaLabel: 'Saved', visible: false });
  const visible = new FakeElement({ tag: 'div', ariaLabel: 'Saved', cursor: 'pointer' });
  root.append(hidden).append(visible);
  const found = selectors.findExactTextControl(root, ['已收藏', 'Saved'], {
    visible: (element) => element.visible,
    inViewport: (element) => element.inView,
    isClickableElement: (element) => element.cursor === 'pointer'
  });
  assert.equal(found, visible);
});

test('语义选择器允许收藏标签附带数量或动态状态文字', () => {
  const selectors = loadSelectors();
  const root = new FakeElement({ tag: 'div' });
  const tab = new FakeElement({ tag: 'button', role: 'tab' });
  tab.append(new FakeElement({ tag: 'span', text: '已收藏 (12)' }));
  root.append(tab);
  const found = selectors.findExactTextControl(root, ['已收藏', 'Saved'], {
    visible: (element) => element.visible,
    inViewport: (element) => element.inView,
    isClickableElement: () => false
  });
  assert.equal(found, tab);
});
