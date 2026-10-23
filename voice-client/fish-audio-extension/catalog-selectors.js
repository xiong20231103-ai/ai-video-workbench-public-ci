(() => {
  'use strict';

  function normalize(value) {
    return String(value ?? '')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/[.…]+$/g, '')
      .replace(/[\u3000\s]+/g, ' ')
      .trim()
      .toLocaleLowerCase();
  }

  function isSelectedControl(element) {
    if (!element) return false;
    const attributes = [
      element.getAttribute?.('aria-selected'),
      element.getAttribute?.('aria-current'),
      element.getAttribute?.('data-state'),
      element.getAttribute?.('data-active')
    ].map((value) => String(value || '').toLocaleLowerCase());
    if (attributes.some((value) => ['true', 'page', 'active', 'selected', 'on'].includes(value))) return true;
    return /(?:active|selected|current|checked)/i.test(String(element.className || ''));
  }

  function findExactTextControl(root, labels, helpers = {}) {
    if (!root) return null;
    const visible = helpers.visible || (() => true);
    const inViewport = helpers.inViewport || (() => true);
    const isClickable = helpers.isClickableElement || (() => false);
    const wanted = labels.map(normalize).filter(Boolean);
    const matchesWanted = (value) => wanted.some((label) => value === label || value.includes(label));
    const candidates = [root, ...Array.from(root.querySelectorAll(
      'button, [role="tab"], [role="button"], [tabindex="0"], [aria-label], [title], div, span, p'
    ))]
      .filter((element) => element !== globalThis.document?.body && element.isConnected !== false && visible(element))
      .filter((element) => inViewport(element) || element === root)
      .map((element) => {
        const text = normalize(element.innerText || element.textContent || '');
        const aria = normalize(element.getAttribute?.('aria-label') || '');
        const title = normalize(element.getAttribute?.('title') || '');
        if (!matchesWanted(text) && !matchesWanted(aria) && !matchesWanted(title)) return null;
        const control = element.matches?.('button, [role="tab"], [role="button"], [tabindex="0"]')
          ? element
          : element.closest?.('button, [role="tab"], [role="button"], [tabindex="0"]');
        const usable = control || isClickable(element) ? (control || element) : null;
        if (!usable || !visible(usable) || !inViewport(usable)) return null;
        const role = usable.getAttribute?.('role') || usable.tagName?.toLowerCase() || '';
        const score = (control ? 100 : 40)
          + (role === 'tab' ? 30 : 0)
          + (aria || title ? 10 : 0)
          + (text === normalize(labels[0]) ? 5 : 0)
          + (matchesWanted(text) || matchesWanted(aria) || matchesWanted(title) ? 2 : 0);
        return { element: usable, score };
      })
      .filter(Boolean)
      .sort((a, b) => b.score - a.score);
    return candidates[0]?.element || null;
  }

  globalThis.FishCatalogSelectors = Object.freeze({
    isDirectSavedList(text, targetVerified) {
      const value = normalize(text);
      return targetVerified === true && (
        value.includes('你收藏的声音，方便快速使用') ||
        value.includes('your saved voices')
      );
    },
    normalize,
    isSelectedControl,
    findExactTextControl
  });
})();
