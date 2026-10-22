const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const dom = new JSDOM('<body><main><button id="profileCard">设置</button><button id="railAvatar">Y</button><div id="sessionActionsMenu"></div><div class="composer-wrap"></div></main></body>', { runScripts:'outside-only', pretendToBeVisual:true });
  const w=dom.window, calls=[], notices=[], archived=new Set(['old','other']), opened=[];
  w.CSS={escape:String};
  const rows=[{id:'old',title:'已归档 <img src=x>',updated_at:'2026-10-21'}, {id:'other',title:'另一条历史',updated_at:'2026-10-20'}];
  w.App={state:{sessionId:'old',hasConversation:true},el(tag,cls,text){const n=w.document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;},toast:text=>notices.push(text),
    closeMenus(){},closeSessionMenus(){},openThemePicker(){calls.push('theme');},openChatSettings(){calls.push('settings');},
    isSessionArchived:id=>archived.has(id),noteSessionArchive(id,value){if(value)archived.add(id);else archived.delete(id);w.App.archives.syncCurrent();},
    loadSessions:async()=>{},openSession:async id=>opened.push(id),forgetSession(id){archived.delete(id);w.App.archives.noteDeleted(id);},startNewChat(){w.App.state.sessionId=null;},
    historySearch:{open:options=>calls.push({type:'search',options})}};
  w.API={listArchivedSessions:async(q,offset,signal)=>{calls.push({type:'list',q,offset,signal});return {sessions:rows.filter(row=>archived.has(row.id)),total:archived.size,archived_count:archived.size,next_offset:null};},
    setSessionArchived:async(id,value)=>{calls.push({type:'mutate',id,value});return {session_id:id,archived:value};},deleteSession:async id=>calls.push({type:'delete',id})};
  w.eval(fs.readFileSync(path.join(__dirname,'../js/app/session_archives.js'),'utf8'));
  const $=selector=>w.document.querySelector(selector);
  const button=(text,scope=w.document)=>Array.from(scope.querySelectorAll('button')).find(node=>node.textContent===text);
  return {w,$,button,calls,notices,archived,opened,open:async()=>{w.App.archives.open();await settle();},close:()=>w.close()};
}

test('settings popup includes archives/theme, supports keyboard, and stays independent of sidebar expansion',t=>{
  const x=setup();t.after(x.close);const anchor=x.$('#railAvatar');
  x.w.App.sidebarSettings.toggle(anchor);
  assert.equal(x.$('#sidebarSettingsMenu').hidden,false);assert.equal(anchor.getAttribute('aria-expanded'),'true');
  x.button('主题',x.$('#sidebarSettingsMenu')).click();assert.deepEqual(x.calls,['theme']);
  assert.equal(x.$('#sidebarSettingsMenu').hidden,true);
  x.w.App.sidebarSettings.toggle(anchor);x.$('#sidebarSettingsMenu').dispatchEvent(new x.w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert.equal(x.$('#sidebarSettingsMenu').hidden,true);assert.equal(x.w.document.activeElement,anchor);
});

test('archive browsing renders titles as text, opens existing history and restores focus/inert state',async t=>{
  const x=setup();t.after(x.close);x.$('#profileCard').focus();await x.open();
  assert.equal(x.$('#archiveList').children.length,2);assert.equal(x.$('#archiveList img'),null);
  assert.equal(x.$('main').inert,true);assert.match(x.$('#archiveStatus').textContent,/2 个匹配/);
  x.$('.archive-row-title').click();await settle();
  assert.deepEqual(x.opened,['old']);assert.equal(x.$('#sessionArchivesModal').hidden,true);assert.equal(x.$('main').inert,undefined);
  assert.equal(x.archived.has('old'),true);
});

test('current archived session exposes restore; failed mutation preserves status and repeated requests are deduplicated',async t=>{
  const x=setup();t.after(x.close);assert.equal(x.$('#archivedSessionBanner').hidden,false);assert.equal(x.$('#archiveSessionItem').textContent,'取消归档');
  let reject;x.w.API.setSessionArchived=()=>new Promise((_,fail)=>reject=fail);
  const first=x.w.App.archives.setArchived('old',false);const second=await x.w.App.archives.setArchived('old',false);assert.equal(second,false);
  reject(Error('offline'));await first;assert.equal(x.archived.has('old'),true);assert.match(x.notices.at(-1),/offline/);
  x.w.API.setSessionArchived=async()=>({archived:false});await x.w.App.archives.setArchived('old',false);
  assert.equal(x.$('#archivedSessionBanner').hidden,true);assert.equal(x.$('#archiveSessionItem').textContent,'归档会话');
});

test('archive search reuses full-text search with archived scope; deletion requires confirmation and handles failures',async t=>{
  const x=setup();t.after(x.close);await x.open();
  x.button('删除',x.$('.archive-row')).click();assert.equal(x.calls.filter(c=>c.type==='delete').length,0);
  assert.match(x.$('.archive-delete-warning').textContent,/无法恢复/);
  x.w.API.deleteSession=async()=>{throw Error('busy');};x.button('确认删除',x.$('.archive-row')).click();await settle();
  assert.match(x.notices.at(-1),/busy/);assert.equal(x.$('#archiveList').children.length,2);
  x.w.API.deleteSession=async id=>x.calls.push({type:'delete',id});x.button('确认删除',x.$('.archive-row')).click();await settle();
  assert.equal(x.calls.filter(c=>c.type==='delete').length,1);assert.equal(x.w.App.state.sessionId,null);
  x.$('#archiveQuery').value='查内容';x.$('#archiveSearchContent').click();
  const call=x.calls.find(c=>c.type==='search');assert.equal(call.options.archiveScope,'archived');assert.equal(call.options.query,'查内容');
});

test('query change and closing cancel stale lists; late responses cannot overwrite a reopened window',async t=>{
  const x=setup();t.after(x.close);const pending=[];
  x.w.API.listArchivedSessions=(q,offset,signal)=>new Promise(resolve=>pending.push({q,signal,resolve}));
  x.w.App.archives.open();x.$('#archiveQuery').value='new';x.$('#archiveQuery').dispatchEvent(new x.w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  assert.equal(pending[0].signal.aborted,true);
  pending[1].resolve({sessions:[{id:'new',title:'New'}],total:1,archived_count:1,next_offset:null});await settle();
  pending[0].resolve({sessions:[{id:'stale',title:'Stale'}],total:1,archived_count:1,next_offset:null});await settle();assert.equal(x.$('.archive-row').dataset.session,'new');
  x.w.App.archives.close();x.w.App.archives.open();x.w.App.archives.close();assert.equal(pending[2].signal.aborted,true);
  pending[2].resolve({sessions:[{id:'late',title:'Late'}],total:1,archived_count:1,next_offset:null});await settle();assert.equal(x.$('.archive-row').dataset.session,'new');
});
