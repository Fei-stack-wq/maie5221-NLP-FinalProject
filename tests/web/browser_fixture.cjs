const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createInterface } = require('node:readline');
const { chromium } = require('playwright');

async function withFixture(moduleName, verify, args = []) {
  const fixture = spawn(process.env.PYTHON || 'python3', ['-Bm', 'tests.web.' + moduleName, ...args],
    { stdio: ['ignore', 'pipe', 'inherit'] });
  let browser;
  try {
    const lines = createInterface({ input: fixture.stdout });
    const [url] = await Promise.race([
      once(lines, 'line'),
      once(fixture, 'exit').then(([code]) => { throw new Error('Fixture stopped ' + code); })
    ]);
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
    const page = await browser.newPage();
    page.setDefaultTimeout(12000);
    await verify(page, url);
  } finally {
    if (browser) await browser.close();
    fixture.kill();
  }
}
module.exports = { withFixture };
