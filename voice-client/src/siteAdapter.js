/**
 * This adapter is the only file that should know the target website's UI.
 * Fish Audio login is supported. Generation stays locked until the
 * speaker/text/download controls have been mapped against a logged-in page.
 */
export class VoiceSiteAdapter {
  constructor(page, config, logger) {
    this.page = page;
    this.config = config;
    this.logger = logger;
  }

  async open() {
    await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' });
  }

  async ensureReady() {
    await this.page.waitForLoadState('domcontentloaded');
    const pageHeading = this.page.getByRole('heading', { name: '文本转语音' });
    await pageHeading.waitFor({ state: 'visible', timeout: 2 * 60 * 1000 });

    const loginButton = this.page.getByRole('button', { name: '登录 / 注册' });

    if (await loginButton.isVisible().catch(() => false)) {
      await this.logger.info('等待 Fish Audio 登录，请在打开的 Google Chrome 中完成登录（最长等待 10 分钟）');
      await loginButton.waitFor({ state: 'hidden', timeout: 10 * 60 * 1000 });
      await this.logger.info('Fish Audio 登录成功，登录状态已保存');
      await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' });
      await pageHeading.waitFor({ state: 'visible', timeout: 2 * 60 * 1000 });
    } else {
      await this.logger.info('Fish Audio 已登录');
    }

    const skipButton = this.page.getByRole('button', { name: '跳过' });
    if (await skipButton.isVisible().catch(() => false)) {
      await skipButton.click();
    }
  }

  async ensureGenerationSupported() {
    throw new Error('Fish Audio 登录已就绪，但音色选择与下载步骤尚待适配；本次没有消耗文案次数。');
  }

  async generate(task) {
    await this.logger.info('task started', { taskId: task.id, project: task.project, language: task.language });

    await this.selectProject(task.project);
    await this.selectLanguage(task.language);
    await this.selectVoice(task.voice);
    await this.fillText(task.text);

    const download = await this.clickGenerateAndWaitForDownload(task);

    await this.logger.info('task finished', { taskId: task.id, download });
    return download;
  }

  async selectProject(project) {
    await this.logger.info('project placeholder', { project });
  }

  async selectLanguage(language) {
    await this.logger.info('language placeholder', { language });
  }

  async selectVoice(voice) {
    await this.logger.info('voice placeholder', { voice });
  }

  async fillText(text) {
    await this.logger.info('text placeholder', { length: text.length });
  }

  async clickGenerateAndWaitForDownload(task) {
    await this.logger.info('generate/download placeholder', { outputName: task.outputName });
    return null;
  }
}
