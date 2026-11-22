// Browser acceptance driver for the disposable synthetic stack only.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const port = Number(process.argv[2]);
const output = process.argv[3];
(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.DSC_CHROME_EXECUTABLE ? {executablePath: process.env.DSC_CHROME_EXECUTABLE} : {}),
    args: ['--autoplay-policy=no-user-gesture-required']});
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  const page = await context.newPage();
  const checks = [];
  let starts = 0;
  page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/live/cameras/camera-1/sessions")) starts += 1; });
  try {
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.getByRole('button', {name: '회사 계정 로그인'}).click();
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    checks.push('PKCE login and permitted camera list');
    await page.evaluate(() => { start(); start(); });
    await page.waitForFunction(() => {
      const v = document.getElementById('video');
      return v.videoWidth > 0 && v.getVideoPlaybackQuality().totalVideoFrames > 5;
    }, {timeout: 30000});
    checks.push('RTSP H264 to WebRTC decoded browser frames');
    if (starts !== 1) throw new Error('concurrent starts created duplicate viewing sessions');
    checks.push('Concurrent viewing starts create one session');
    await page.screenshot({path: path.join(output, 'live-desktop.png')});
    const denied = await context.request.post(`http://127.0.0.1:${port}/media/camera-1/whep`, {
      headers: {'Authorization': 'Bearer invalid', 'Content-Type': 'application/sdp'},
      data: await page.evaluate(() => peer.localDescription.sdp)});
    if (denied.status() < 400) throw new Error('invalid token was accepted');
    checks.push('Invalid viewing capability rejected');
    const publish = await context.request.post(`http://127.0.0.1:${port}/media/camera-1/whip`, {data: ''});
    if (publish.status() !== 404) throw new Error('publishing route exposed');
    checks.push('Publishing and other media routes closed');
    await page.getByRole('button', {name: '시청 종료', exact: true}).click();
    await page.waitForFunction(() => !peer && !live);
    checks.push('User stop closes the peer and revokes the backend session');
    await page.getByRole('button', {name: '실시간 보기', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('video').videoWidth > 0 && peer?.connectionState === 'connected');
    // Suppress the UI timer so expiration must be enforced by the gateway, not the page.
    await page.evaluate(() => clearTimeout(expiryTimer));
    await context.request.post('http://127.0.0.1:18080/test/expire');
    await page.waitForFunction(() => peer?.connectionState !== 'connected', {timeout: 20000});
    checks.push('Server expiration disconnects an established peer');
    await context.request.post('http://127.0.0.1:18080/test/reset');
    await page.getByRole('button', {name: '실시간 보기', exact: true}).click();
    await page.waitForFunction(() => peer?.connectionState === 'connected');
    await context.request.post('http://127.0.0.1:18080/test/revoke');
    await page.waitForFunction(() => peer?.connectionState !== 'connected', {timeout: 20000});
    checks.push('Permission withdrawal disconnects an established peer');
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(output, 'live-mobile.png')});
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('mobile overflow');
    checks.push('Mobile layout has no horizontal overflow');
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({status: 'PASS', checks,
      boundary: 'synthetic OAuth and RTSP on local Docker; actual browser, MediaMTX and product authorization',
      physicalAcceptance: 'NOT_RUN'}, null, 2));
    console.log(JSON.stringify({status: 'PASS', checks}, null, 2));
  } catch (error) {
    console.error(error.message);
    console.error(await page.locator('#status').textContent().catch(() => 'page not loaded'));
    await page.screenshot({path: path.join(output, 'failure.png')}).catch(() => {});
    process.exitCode = 1;
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
