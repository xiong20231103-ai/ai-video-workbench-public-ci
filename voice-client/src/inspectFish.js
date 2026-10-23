import { chromium } from 'playwright';
import { config } from './config.js';

const context = await chromium.launchPersistentContext(config.browserUserDataDir, {
  executablePath: config.browserExecutable,
  headless: false,
  acceptDownloads: true,
  ...(config.browserProfileDirectory ? { args: [`--profile-directory=${config.browserProfileDirectory}`] } : {}),
  ...(config.browserProxyServer ? { proxy: { server: config.browserProxyServer } } : {})
});

try {
  const pages = context.pages();
  const page = pages[0] ?? await context.newPage();
  await page.goto(config.targetUrl, { waitUntil: 'domcontentloaded', timeout: 120_000 });
  await page.getByRole('heading', { name: '文本转语音' }).waitFor({ state: 'visible', timeout: 120_000 });

  const loginVisible = await page.getByRole('button', { name: '登录 / 注册' }).isVisible().catch(() => false);
  const main = await page.evaluate(() => ({
    url: location.href,
    editables: Array.from(document.querySelectorAll('[contenteditable="true"], textarea, [role="textbox"]')).map((el) => ({
      tag: el.tagName,
      className: String(el.className || '').slice(0, 300),
      role: el.getAttribute('role'),
      ariaLabel: el.getAttribute('aria-label'),
      placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
      contenteditable: el.getAttribute('contenteditable'),
      text: (el.textContent || '').slice(0, 160)
    })),
    buttons: Array.from(document.querySelectorAll('button')).map((el) => ({
      text: (el.innerText || '').trim().slice(0, 100),
      ariaLabel: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      className: String(el.className || '').slice(0, 200),
      disabled: el.disabled
    })).filter((item) => item.text || item.ariaLabel || item.title)
  }));

  console.log(JSON.stringify({ loginVisible, main }, null, 2));

  const historyTab = page.getByRole('tab', { name: '历史' });
  if (await historyTab.isVisible().catch(() => false)) {
    await historyTab.click();
    await page.waitForTimeout(1500);
    const history = await page.evaluate(() => ({
      text: (document.querySelector('[role="tabpanel"]')?.textContent || '').trim().slice(0, 3000),
      controls: Array.from(document.querySelectorAll('[role="tabpanel"] button, [role="tabpanel"] a, [role="tabpanel"] audio')).map((el) => ({
        tag: el.tagName,
        text: (el.innerText || '').trim().slice(0, 120),
        ariaLabel: el.getAttribute('aria-label'),
        title: el.getAttribute('title'),
        href: el.getAttribute('href'),
        download: el.getAttribute('download'),
        src: el.getAttribute('src'),
        className: String(el.className || '').slice(0, 200)
      }))
    }));
    console.log(JSON.stringify({ history }, null, 2));
  }
} finally {
  await context.close();
}
