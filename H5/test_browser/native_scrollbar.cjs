// Run from the project root: node H5/test_browser/native_scrollbar.cjs
// Uses the pinned browser/ Playwright runtime and synthetic history; no backend.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const runtime = path.resolve(__dirname, '../../browser/node_modules');
const { chromium } = require(path.join(runtime, 'playwright'));
const { PNG } = require(path.join(runtime, 'playwright-core/lib/utilsBundle.js'));
const h5 = path.resolve(__dirname, '..');

(async () => {
  // Playwright hides scrollbars by default. Keep the real OS scrollbar and use
  // mouse input; synthetic scroll events cannot reproduce a native thumb drag.
  const browser = await chromium.launch({ headless: true, ignoreDefaultArgs: ['--hide-scrollbars'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1300, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://scroll.local/**', async route => {
      const relative = decodeURIComponent(new URL(route.request().url()).pathname).replace(/^\//, '') || 'index.html';
      if (['js/app.js', 'js/api.js'].includes(relative)) return route.fulfill({body:'', contentType:'text/javascript'});
      const file = path.resolve(h5, relative);
      if (!file.startsWith(h5 + path.sep) || !fs.existsSync(file)) return route.fulfill({status:404, body:''});
      let body = fs.readFileSync(file);
      return route.fulfill({body, contentType:relative.endsWith('.js') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.addInitScript(() => {
      window.api_url = 'http://scroll.local';
      window.API = new Proxy({base:api_url, BASE:api_url}, {get:(object,key) => key in object ? object[key] : () => Promise.resolve({})});
    });
    await page.goto('http://scroll.local/', {waitUntil:'load'});
    const metrics = () => page.evaluate(() => {
      const scroll = App.chatScroll, rect = scroll.getBoundingClientRect(), testPaging = App.getHistoryWindow();
      return {left:rect.left, right:rect.right, top:rect.top, height:scroll.clientHeight,
        gutter:scroll.offsetWidth-scroll.clientWidth, max:scroll.scrollHeight-scroll.clientHeight,
        position:scroll.scrollTop, start:testPaging.start, end:testPaging.end,
        dragging:App.isChatScrollbarDragging()};
    });
    async function thumb() {
      const m = await metrics();
      assert.ok(m.gutter > 0, 'test requires a visible native scrollbar');
      const clip = {x:m.right-m.gutter, y:m.top, width:m.gutter, height:m.height};
      const png = PNG.sync.read(await page.screenshot({clip}));
      // This fixture uses the light theme. Ignore the arrow buttons at either
      // end and locate the actual painted thumb, including its native minimum.
      const rows = [];
      for (let y = m.gutter; y < png.height - m.gutter; y++) {
        const i = (y * png.width + Math.floor(png.width/2)) * 4;
        if (png.data[i] < 220 && png.data[i+1] < 220 && png.data[i+2] < 220) rows.push(y);
      }
      assert.ok(rows.length > 2, 'native thumb must be painted');
      return {x:m.right-m.gutter/2, y:m.top+(rows[0]+rows.at(-1))/2, ...m};
    }
    async function reset(search) {
      await page.evaluate(search => {
        document.documentElement.dataset.theme = 'light';
        const records = Array.from({length:600}, (_,i) => ({
          kind:i%5 === 0 ? 'user' : i%5 === 1 ? 'think' : 'assistant',
          content:i%5 === 0 ? 'question '+i : 'paragraph '+i+' '+('text 内容 '.repeat(i%3 ? 80 : 1000)),
          round:Math.floor(i/5)+1, ts:'2026-10-21 12:00:00', source_event_index:i%5,
        }));
        App.resetHistoryView(); App.state.activeStream = null;
        App.chatInner.replaceChildren();
        App.state.sessionId = 'fixture'; App.state.hasConversation = true; App.setEmpty(false);
        App.renderRecords(records, search ? {historyIndex:250,noScroll:true} : {noScroll:true});
        App.rebuildQnav(); App.pauseAutoScroll(); App.chatScroll.style.scrollBehavior = 'auto';
        App.chatScroll.scrollTop = (App.chatScroll.scrollHeight-App.chatScroll.clientHeight)*.5;
      }, search);
      await page.waitForTimeout(200);
    }
    async function holdAndSample() {
      return page.evaluate(async () => {
        const samples = [];
        for (let i=0; i<18; i++) {
          await new Promise(resolve => requestAnimationFrame(resolve));
          const view = App.getHistoryWindow();
          samples.push([App.chatScroll.scrollTop, App.chatScroll.scrollHeight, view.start, view.end]);
        }
        return samples;
      });
    }
    async function visibleContent() {
      return page.evaluate(() => {
        const box = App.chatScroll.getBoundingClientRect(), counts = new Map();
        const nodes = Array.from(App.chatInner.children).filter(node => node.hasAttribute('data-history-index'));
        const visible = nodes.map(node => {
          const index = Number(node.dataset.historyIndex), part = counts.get(index)||0;
          counts.set(index,part+1);
          const r = node.getBoundingClientRect();
          return {key:index+':'+part,index,top:r.top-box.top,bottom:r.bottom-box.top};
        }).filter(node => node.bottom>0 && node.top<App.chatScroll.clientHeight);
        return {visible, count:App.chatInner.children.length, first:visible[0]||null, last:visible.at(-1)||null};
      });
    }
    async function painted() {
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    }
    let lastPoint;
    async function dragTo(x,y) {
      const start = lastPoint || {x,y};
      // Native scrollbar input is compositor driven. Pace physical moves with
      // painted frames; CDP's unpaced steps can overwrite pending native input.
      for (let j=1;j<=8;j++) {
        await page.mouse.move(x,start.y+(y-start.y)*j/8); await painted();
      }
      lastPoint={x,y};
    }
    async function grab() {
      await painted();
      let point = await thumb();
      await page.mouse.move(point.x,point.y); point = await thumb();
      await page.mouse.move(point.x,point.y); await painted(); await page.mouse.down();
      lastPoint=point; return point;
    }
    function sameView(before,after) {
      assert.ok(before.first && after.first,'viewport must contain real messages');
      assert.equal(after.first.key,before.first.key,'mouseup must keep the visible record');
      assert.ok(Math.abs(after.first.top-before.first.top)<=1,'mouseup must not move the visible content: '+JSON.stringify({before:before.first,after:after.first}));
    }
    await reset(true);
    const point = await grab(), locked = await metrics();
    const offsets = [], trajectory = [];
    for (const ratio of [.1,.25,.5,.75,.9,0,1,0]) {
      const y = point.top+1+(point.height-2)*ratio;
      await dragTo(point.x,y);
      const actual = await thumb(), content = await visibleContent();
      assert.equal(actual.dragging,true);
      assert.equal(actual.max,locked.max,'virtual window changes must not change the held thumb range');
      assert.ok(content.count<260,'drag must only render a bounded window');
      assert.ok(content.visible.length,'drag must never show an empty history spacer');
      if (ratio>0 && ratio<1) offsets.push(actual.y-y);
      else {
        assert.equal(actual.position,ratio ? actual.max : 0);
        assert.equal(ratio ? actual.end : actual.start,ratio ? 600 : 0,'true edge must already be mounted while the mouse is held');
        assert.equal(ratio ? content.last.index : content.first.index,ratio ? 599 : 0,'true first/last record must be visible before mouseup');
        const samples = await holdAndSample();
        assert.ok(samples.every(sample => sample.every((value,index) => value === samples[0][index])),'stationary edge must not jitter');
      }
      trajectory.push({ratio,first:content.first.index,last:content.last.index,position:actual.position,max:actual.max});
    }
    assert.ok(Math.max(...offsets.map(Math.abs))<=4,'native thumb must stay under the pointer');
    assert.ok(trajectory[0].first<trajectory[1].first && trajectory[1].first<trajectory[2].first && trajectory[2].first<trajectory[3].first && trajectory[3].first<trajectory[4].first,'content must progress through the conversation with the thumb');
    const heldHead = await visibleContent();
    await page.mouse.up(); await page.waitForTimeout(180);
    sameView(heldHead,await visibleContent());

    const releases = [];
    for (const search of [false,true]) for (const ratio of [0,.4,1]) {
      await reset(search); const p = await grab();
      await dragTo(p.x,p.top+1+(p.height-2)*ratio);
      const held = await visibleContent(), position = await metrics();
      if (ratio===0 || ratio===1) assert.equal(ratio ? held.last.index : held.first.index,ratio ? 599 : 0);
      await page.mouse.up(); await page.waitForTimeout(200);
      const after = await metrics(); sameView(held,await visibleContent());
      if (ratio===0 || ratio===1) assert.equal(after.position,ratio ? after.max : 0);
      releases.push({search,ratio,record:held.first.index,heldPosition:position.position,releasedPosition:after.position});
    }

    const wheelEdges = [];
    for (const edge of [0,1]) {
      await reset(true); await page.mouse.move(900,350);
      let prior = (await visibleContent()).first.index;
      for (let i=0;i<180;i++) {
        await page.mouse.wheel(0,edge ? 4000 : -4000); await painted();
        const current = await metrics(), content = await visibleContent();
        assert.ok(content.first,'wheel must not expose an empty placeholder');
        assert.ok(Math.abs(content.first.index-prior)<80,'wheel must not skip a history page');
        assert.ok(edge ? content.first.index>=prior-1 : content.first.index<=prior+1,'wheel must keep moving in the requested direction');
        prior = content.first.index;
        if (edge ? current.position===current.max : current.position===0) break;
      }
      await page.waitForTimeout(200); const after = await metrics(), content = await visibleContent();
      assert.equal(after.position,edge ? after.max : 0);
      assert.equal(edge ? content.last.index : content.first.index,edge ? 599 : 0);
      wheelEdges.push({edge,record:content.first.index,position:after.position,max:after.max});
    }

    // Search and the full question list still resolve into the same global
    // coordinate system, including after a distant thumb jump.
    const searchResult = await page.evaluate(() => App.jumpToHistoryMatch({session_id:'fixture',round_no:51,event_index:3,role:'assistant'},'paragraph 253'));
    assert.equal(searchResult,true); await page.waitForTimeout(180);
    const searchContent = await visibleContent();
    assert.ok(searchContent.visible.some(node => node.index===253));
    await page.evaluate(() => App.qnavPanel.querySelectorAll('.qnav-item')[80].click());
    await page.waitForTimeout(750);
    assert.ok((await visibleContent()).visible.some(node => node.index===400),'question navigation must remain exact');

    // Recycle distant windows without recreating the edit draft, its local
    // attachment nodes or a running stream's mutable message container.
    await reset(true);
    await page.evaluate(() => {
      const user=App.chatInner.querySelector('.msg-user[data-history-index="250"]');
      user.classList.add('is-editing');
      const textarea=document.createElement('textarea'); textarea.value='unsent draft';
      user.appendChild(textarea); window.savedDraft=user;
      const message=document.createElement('div'); message.id='synthetic-retained-live';
      message.textContent='running reply'; App.chatInner.appendChild(message);
      App.state.activeStream={sessionId:'fixture',messageNode:message,userNode:null};
      App.historyScroll.refresh();
      App.renderHistoryWindowAt(0);
    });
    await painted();
    assert.equal(await page.evaluate(() => savedDraft.isConnected && savedDraft.hidden===false && !App.chatInner.contains(savedDraft)),true);
    assert.equal(await page.evaluate(() => {
      const node=document.querySelector('#synthetic-retained-live');
      return node.parentNode===App.chatInner && node.classList.contains('history-live-hidden');
    }),true,'offscreen live nodes stay attached for stream completion');
    await page.evaluate(() => {
      document.querySelector('#synthetic-retained-live').textContent='finished while reading older history';
      App.state.activeStream=null;
      App.renderHistoryWindowAt(250);
    });
    await painted();
    assert.equal(await page.evaluate(() => App.chatInner.querySelector('.msg-user[data-history-index="250"]')===savedDraft && savedDraft.querySelector('textarea').value==='unsent draft'),true);
    await page.evaluate(() => App.scrollToBottom(true)); await painted();
    assert.equal(await page.evaluate(() => {
      const node=document.querySelector('#synthetic-retained-live');
      return node.parentNode===App.chatInner && !node.classList.contains('history-live-hidden') && node.textContent==='finished while reading older history';
    }),true,'completed output survives returning to the tail');

    // Incoming output must be visible at the end while the pointer is still
    // held; restoring a growing tail must not wait for mouseup either.
    await reset(false); const p = await grab();
    await dragTo(p.x,p.top+p.height-1);
    const bottom = await metrics();
    await page.evaluate(() => {
      const node = document.createElement('div'); node.id='synthetic-live-output';
      node.style.height='1600px'; node.textContent='synthetic streamed output';
      App.chatInner.appendChild(node); App.stickToBottom();
    });
    await page.waitForTimeout(150);
    const streamed = await page.evaluate(() => {
      const r=document.querySelector('#synthetic-live-output').getBoundingClientRect(),s=App.chatScroll.getBoundingClientRect();
      return {bottom:r.bottom-s.top,height:App.chatScroll.clientHeight,max:App.chatScroll.scrollHeight-App.chatScroll.clientHeight};
    });
    assert.equal(streamed.max,bottom.max);
    assert.ok(streamed.bottom>0 && streamed.bottom<=streamed.height,'new tail output must be visible before release');
    await page.mouse.up(); await page.waitForTimeout(120);
    const following = await metrics(); assert.equal(following.position,following.max);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({maxThumbOffset:Math.max(...offsets.map(Math.abs)),trajectory,releases,wheelEdges,streamed,errors},null,2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

