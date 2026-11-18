// Optional browser regression. Set PYTHON to the project interpreter and install Playwright Chromium.
const assert = require('node:assert/strict');
const { withFixture } = require('./browser_fixture.cjs');

withFixture('serve_preview_fixture', async (page, url) => {
  const response = await page.goto(url);
  assert.equal(response.status(), 200);
  assert.match(response.headers()['content-security-policy'], /connect-src 'none'/);
  assert.equal(await page.locator('iframe').getAttribute('sandbox'), 'allow-scripts');
  const deck = page.frameLocator('iframe');
  assert.equal(await deck.locator('section:visible').textContent(), '研究范围');
  await deck.getByText('下一页').click();
  assert.equal(await deck.locator('section:visible').textContent(), '投资风险');
  assert.equal(await deck.locator('section:visible').evaluate(node => getComputedStyle(node).color), 'rgb(10, 20, 30)');
  await deck.getByText('上一页').click();
  assert.equal(await deck.locator('section:visible').textContent(), '研究范围');
  assert.equal(await deck.locator('body').evaluate(() => window.origin), 'null');
  const blocked = await deck.locator('body').evaluate((_, assetURL) => new Promise(resolve => {
    document.addEventListener('securitypolicyviolation', e => resolve(e.effectiveDirective), { once: true });
    const image = new Image();
    image.src = assetURL;
  }), new URL('/fixture-chart.png', url).href);
  assert.equal(blocked, 'img-src');
  await deck.getByText('打开链接').click();
  assert.equal(page.url(), url);
  assert.equal(await page.locator('section').count(), 0);
  console.log('Slide rendering, two-way navigation, local CSS/JS, opaque origin, blocked remote image and top navigation passed.');
}).catch(e => { console.error(e); process.exitCode = 1; });
