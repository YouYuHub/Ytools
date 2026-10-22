// node H5/test_browser/ask_answer_stream.cjs
// Real ask modal, API SSE reader, chat pipeline and virtual history in Chromium.
// All API responses and history are synthetic; no model or real session is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('../../browser/node_modules/playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1300,height:900}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      if (new URL(route.request().url()).origin !== 'http://ask.local') return route.abort();
      const relative = decodeURIComponent(new URL(route.request().url()).pathname).replace(/^\//, '') || 'index.html';
      if (relative === 'js/app.js') return route.fulfill({body:'',contentType:'text/javascript'});
      const file = path.resolve(root, relative);
      if (!file.startsWith(root+path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile())
        return route.fulfill({body:'{}',contentType:'application/json'});
      return route.fulfill({body:fs.readFileSync(file),contentType:relative.endsWith('.js')?'text/javascript':relative.endsWith('.css')?'text/css':'text/html'});
    });
    await page.addInitScript(() => localStorage.setItem('ytools-api-base','http://ask.local'));
    await page.goto('http://ask.local/', {waitUntil:'load'});
    await page.evaluate(() => {
      App.state.sessionId = 'ask-fixture'; App.state.hasConversation = true; App.setEmpty(false);
      App.refreshContextTokenStats = () => {}; App.refreshSessionUsage = async () => true;
      App.scheduleContextTokenStatsRefresh = () => {}; App.flushPendingMessages = () => {};
      const records = [{kind:'user',content:'old task',round:1}];
      for (let i=0; i<180; i++) records.push({kind:'assistant',content:'old step '+i+' '+('long text '.repeat(15)),round:1});
      App.renderRecords(records); App.rebuildQnav();
      window.calls = [];
      const originalFetch = window.fetch.bind(window);
      window.fetch = async (url, options) => {
        if (!String(url).endsWith('/chat_with_tool')) return originalFetch(url, options);
        calls.push(JSON.parse(options.body));
        return new Response(new ReadableStream({start(controller) {
          window.feed = data => controller.enqueue(new TextEncoder().encode('data: '+JSON.stringify(data)+'\n\n'));
          window.complete = () => {controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n')); controller.close();};
        }}), {headers:{'Content-Type':'text/event-stream'}});
      };
      // Following can synchronously recycle the current history window. It
      // must already see this task's nodes, never the previous stream.
      const showTail = App.showHistoryTail;
      App.showHistoryTail = () => {
        if (App.state.streaming) {
          const active = App.state.activeStream;
          if (!active || active.completed) throw new Error('new stream was registered after following');
        }
        showTail();
      };
    });
    async function answerModal() {
      for (const question of await page.locator('.ask-question').all()) await question.locator('.ask-option').first().click();
      await page.locator('#askSubmit').click();
    }
    async function ask(round) {
      await page.evaluate(round => {
        feed({round_started:{round}});
        feed({round_model:{name:'fixture model'}});
        feed({content:'task question '+round});
        feed({event:'ask_user',questions:[{question:'Continue task '+round+'?',options:['Yes','No']}]});
        complete();
      }, round);
      await page.waitForFunction(() => !App.state.streaming);
      assert.equal(await page.locator('#askModal').getAttribute('aria-hidden'), 'false');
    }
    await page.evaluate(() => { App.send({text:'start task'}); });
    await page.waitForFunction(() => calls.length === 1);
    await ask(2);
    await answerModal();
    await page.waitForFunction(() => calls.length === 2);
    assert.equal(await page.evaluate(() => 'insert_round' in calls[1]), false, 'latest answer is an ordinary continuation');
    await page.evaluate(() => {
      feed({round_started:{round:3}});
      feed({round_model:{name:'fixture model'}});
      feed({reasoning_content:'continuing reasoning'});
      feed({content:'live answer after question'});
    });
    await page.waitForTimeout(100);
    async function checkLive() {
      assert.deepEqual(await page.evaluate(() => {
        const active = App.state.activeStream;
        return {round:active.round,attached:active.messageNode.parentNode === App.chatInner,
          visible:getComputedStyle(active.messageNode).display !== 'none',
          question:active.userNode.textContent.includes('【回答模型提问】')};
      }), {round:3,attached:true,visible:true,question:true});
    }
    await checkLive();
    // Rebuilding the visible tail must not leave the pipeline writing into a
    // detached DOM node. The next event restores the same container.
    await page.evaluate(() => {
      window.firstLiveNode = App.state.activeStream.messageNode;
      firstLiveNode.remove();
      feed({content:' output after tail remount'});
    });
    await page.waitForTimeout(100);
    await checkLive();
    assert.equal(await page.evaluate(() => App.state.activeStream.messageNode === firstLiveNode),true);
    for (let i=0; i<25; i++) {
      await page.evaluate(i => {
        feed({tool_calls:[{index:0,id:'tool-'+i,type:'function',function:{name:'read_file',arguments:'{"path":"file-'+i+'"}'}}]});
        feed({tool_return:{tool_call_id:'tool-'+i,function_name:'read_file',result:'tool output '+i}});
        feed({content:'continued step '+i});
      }, i);
      await page.waitForTimeout(20);
      await checkLive();
    }
    await page.evaluate(() => {
      window.liveNode = App.state.activeStream.messageNode;
      App.pauseAutoScroll(); App.renderHistoryWindowAt(0); App.chatScroll.scrollTop = 0;
    });
    await page.waitForTimeout(100);
    await page.evaluate(() => { feed({content:'new output while reading history'}); });
    await page.evaluate(() => App.scrollToBottom(true));
    await page.waitForTimeout(100);
    await checkLive();
    assert.equal(await page.evaluate(() => App.state.activeStream.messageNode === liveNode), true);
    assert.ok(await page.locator('#chatInner').innerText().then(text => text.includes('new output while reading history')));
    assert.equal(await page.locator('#chatInner .tool-block').count(), 25);
    // A second automatically opened question must continue immediately too.
    await ask(3); await answerModal();
    await page.waitForFunction(() => calls.length === 3);
    await page.evaluate(() => { feed({round_started:{round:4}}); feed({content:'second continuation'}); });
    await page.waitForTimeout(100);
    assert.ok(await page.locator('#chatInner').innerText().then(text => text.includes('second continuation')));
    await page.evaluate(() => complete());
    await page.waitForFunction(() => !App.state.streaming);
    // Explicit insertion has an identity even before the confirmation frame.
    await page.evaluate(() => { App.send({text:'explicit insert',insertAfterRound:4}); });
    await page.waitForFunction(() => calls.length === 4);
    assert.equal(await page.evaluate(() => App.state.activeStream.round), 5);
    await page.evaluate(() => { feed({round_started:{round:5}}); feed({content:'inserted output'}); complete(); });
    await page.waitForFunction(() => !App.state.streaming);
    // Completed live turns are not yet in the initial history snapshot. A
    // window recycle must restore both the turns and their navigation entries.
    assert.equal(await page.locator('.qnav-item').count(),5);
    await page.evaluate(() => { App.pauseAutoScroll(); App.renderHistoryWindowAt(0); App.historyScroll.refresh(); });
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.qnav-item').count(),5,'navigation retains all completed live questions in an older window');
    assert.ok((await page.locator('.qnav-item').last().textContent()).includes('explicit insert'));
    await page.evaluate(() => App.scrollToBottom(true));
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.qnav-item').count(),5);
    assert.deepEqual(errors, []);
    console.log('PASS: automatic answers, 25 tool calls, live/completed question navigation through history recycling, repeat answer and explicit insert; zero browser errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
