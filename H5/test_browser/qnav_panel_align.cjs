// 右侧问题导航（map 条）居中回归：轨道与展开列表都在「顶栏下沿 → 输入区上方」垂直居中，
// 且不越过顶栏、不压住输入区；窄屏列表不被居中 transform 顶偏。
// Run: node H5/test_browser/qnav_panel_align.cjs
// 用未登录夹具页（去掉 data-ytools-auth-required）+ 假 API 渲染合成会话，不读真实会话。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const h5 = path.resolve(__dirname, '..');
const { chromium } = require(path.resolve(h5, '../browser/node_modules/playwright'));

function seed(count) {
  // 每问两段回答：足够让展开列表超出可视高度，从而检验高度上限。
  const records = [];
  for (let q = 0; q < count; q++) {
    records.push({ kind: 'user', round: q + 1, content: '第 ' + (q + 1) + ' 个问题：导航居中回归',
      ts: '2026-10-23 14:00:00', source_event_index: q * 3 });
    for (let a = 0; a < 2; a++) {
      records.push({ kind: 'assistant', round: q + 1,
        content: '回答 ' + q + '-' + a + '：' + '用于撑高会话以检验列表高度上限。'.repeat(4),
        ts: '2026-10-23 14:00:00', source_event_index: q * 3 + 1 + a });
    }
  }
  return records;
}

async function openPage(browser, viewport) {
  const page = await browser.newPage({ viewport });
  page.on('pageerror', e => { page.__errors = (page.__errors || []).concat(e.message); });
  await page.route('http://qnav.local/**', async route => {
    const rel = decodeURIComponent(new URL(route.request().url()).pathname).replace(/^\//, '') || 'index.html';
    if (rel === 'index.html') {
      const html = fs.readFileSync(path.join(h5, 'index.html'), 'utf8')
        .replace(' data-ytools-auth-required', '')
        .replace(/src="js\/api\.js[^"]*"/, 'src="fixture-api.js"')
        .replace(/src="js\/app\.js[^"]*"/, 'src="fixture-start.js"');
      return route.fulfill({ body: html, contentType: 'text/html' });
    }
    if (rel === 'fixture-api.js' || rel === 'fixture-start.js') return route.fulfill({ body: '', contentType: 'text/javascript' });
    const file = path.resolve(h5, rel);
    if (!file.startsWith(h5 + path.sep) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: '' });
    const type = rel.endsWith('.js') ? 'text/javascript' : rel.endsWith('.css') ? 'text/css'
      : rel.endsWith('.svg') ? 'image/svg+xml' : 'application/octet-stream';
    return route.fulfill({ body: fs.readFileSync(file), contentType: type });
  });
  await page.addInitScript(url => {
    window.api_url = url;
    window.API = new Proxy({ base: url, BASE: url }, { get: (o, k) => k in o ? o[k] : () => Promise.resolve({}) });
  }, 'http://qnav.local');
  await page.goto('http://qnav.local/', { waitUntil: 'load' });
  await page.waitForFunction(() => window.App && window.App.rebuildQnav, null, { timeout: 15000 });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  return page;
}

function metrics(page) {
  return page.evaluate(() => {
    const rect = sel => { const el = document.querySelector(sel); return el ? el.getBoundingClientRect() : null; };
    const panel = document.querySelector('#qnavPanel');
    const rail = rect('#qnavRail'), panelRect = rect('#qnavPanel');
    return {
      visible: !document.querySelector('#qnav').classList.contains('hidden'),
      panelDisplayed: getComputedStyle(panel).display !== 'none',
      topbarBottom: Math.round(rect('.topbar').bottom),
      railTop: Math.round(rail.top), railBottom: Math.round(rail.bottom),
      panelTop: Math.round(panelRect.top), panelBottom: Math.round(panelRect.bottom),
      composerTop: Math.round(rect('.composer-wrap').top),
      viewportH: innerHeight,
      panelClientH: panel.clientHeight,
      items: panel.querySelectorAll('.qnav-item').length,
    };
  });
}

function hoverRail(page) {
  return page.evaluate(() => {
    const r = document.querySelector('#qnavRail').getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + Math.min(30, r.height / 2) };
  }).then(async box => {
    await page.mouse.move(box.x, box.y);
    await page.waitForTimeout(300);
  });
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  try {
    // ---------- 桌面：居中 + 高度上限 + hover 展开/收起 ----------
    const page = await openPage(browser, { width: 1440, height: 900 });
    await page.evaluate(records => {
      App.resetHistoryView(); App.state.activeStream = null; App.chatInner.replaceChildren();
      App.state.sessionId = 'qnav-panel-align'; App.state.hasConversation = true; App.setEmpty(false);
      App.renderRecords(records, { noScroll: true });
      App.rebuildQnav(); App.pauseAutoScroll();
      App.chatScroll.style.scrollBehavior = 'auto'; App.chatScroll.scrollTop = 0;
    }, seed(30));
    await page.waitForTimeout(300);

    const idle = await metrics(page);
    assert.ok(idle.visible && idle.items >= 30, '导航应显示全部问题：' + JSON.stringify(idle));
    assert.equal(idle.panelDisplayed, false, '未 hover 时列表应收起');

    await hoverRail(page);
    const normal = await metrics(page);
    const availMid = (normal.topbarBottom + normal.composerTop) / 2;
    const railMid = (normal.railTop + normal.railBottom) / 2;
    const panelMid = (normal.panelTop + normal.panelBottom) / 2;
    assert.equal(normal.panelDisplayed, true, 'hover 轨道应展开列表');
    assert.ok(Math.abs(railMid - availMid) <= 8,
      'map 条（轨道）应在可用高度内垂直居中：中心 ' + railMid + ' vs 可用区中心 ' + availMid);
    assert.ok(Math.abs(panelMid - availMid) <= 8,
      '展开列表应与轨道一同居中：中心 ' + panelMid + ' vs 可用区中心 ' + availMid);
    assert.ok(normal.railTop > normal.topbarBottom + 2, '居中后轨道顶部应低于顶栏：' + normal.railTop);
    assert.ok(normal.panelTop >= normal.topbarBottom - 2, '列表不应越过顶栏下沿：' + normal.panelTop);
    assert.ok(normal.panelBottom <= normal.composerTop, '列表不应压住输入区：' + normal.panelBottom + ' > ' + normal.composerTop);
    assert.ok(normal.railBottom <= normal.composerTop, '轨道不应压住输入区：' + normal.railBottom + ' > ' + normal.composerTop);
    assert.ok(normal.panelBottom - normal.panelTop <= 480, '列表高度仍受 480px 上限约束');

    // 鼠标移入列表后保持展开（可点击跳转）
    const panelBox = await page.evaluate(() => {
      const r = document.querySelector('#qnavPanel').getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + 20 };
    });
    await page.mouse.move(panelBox.x, panelBox.y);
    await page.waitForTimeout(250);
    assert.equal((await metrics(page)).panelDisplayed, true, '鼠标进入列表后应保持展开');
    await page.mouse.move(200, 300);
    await page.waitForTimeout(300);
    assert.equal((await metrics(page)).panelDisplayed, false, '移开导航区应收起列表');

    // 底部区变厚（多行输入 + todo/状态栏）时，靠 --bottom-stack-height 一起收缩
    await page.evaluate(() => { document.querySelector('.main').style.setProperty('--bottom-stack-height', '320px'); });
    await hoverRail(page);
    const thick = await metrics(page);
    assert.ok(thick.panelBottom <= thick.viewportH - 320 - 8, '厚底部时列表底边应在输入区上方：' + thick.panelBottom);
    assert.ok(thick.railBottom <= thick.viewportH - 320 - 8, '厚底部时轨道底边应在输入区上方：' + thick.railBottom);
    const thickMid = (thick.topbarBottom + (thick.viewportH - 320)) / 2;
    assert.ok(Math.abs((thick.panelTop + thick.panelBottom) / 2 - thickMid) <= 10, '厚底部时列表仍居中');
    assert.deepEqual(page.__errors || [], [], '桌面页不应有脚本错误');

    // ---------- 窄屏：顶栏入口列表是静态定位，不能被居中 transform 顶偏 ----------
    const mobile = await openPage(browser, { width: 420, height: 860 });
    await mobile.evaluate(records => {
      App.resetHistoryView(); App.state.activeStream = null; App.chatInner.replaceChildren();
      App.state.sessionId = 'qnav-panel-align-mobile'; App.state.hasConversation = true; App.setEmpty(false);
      App.renderRecords(records, { noScroll: true });
      App.rebuildQnav(); App.pauseAutoScroll();
    }, seed(20));
    await mobile.waitForTimeout(300);
    // 窄屏下侧栏可能遮挡顶栏按钮，直接派发 click 更稳定
    await mobile.evaluate(() => document.querySelector('#qnavMobileBtn').click());
    await mobile.waitForTimeout(300);
    const mob = await metrics(mobile);
    assert.ok(mob.items >= 20, '窄屏列表应列出全部问题');
    assert.ok(mob.panelTop >= mob.topbarBottom - 4, '窄屏列表不应被顶到顶栏之上：' + mob.panelTop + ' vs ' + mob.topbarBottom);
    assert.ok(mob.panelBottom <= mob.viewportH + 1, '窄屏列表不应超出视口：' + mob.panelBottom);
    assert.deepEqual(mobile.__errors || [], [], '窄屏页不应有脚本错误');

    console.log('qnav panel align: OK', JSON.stringify({ normal, thick, mob }));
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error('qnav panel align: FAIL', error.message); process.exit(1); });
