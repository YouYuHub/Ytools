const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
test('sidebar defaults to active rows, single-session lookup includes archives, search always sends its scope',async()=>{
  const urls=[],window={},storage={getItem:()=>null,setItem(){},removeItem(){}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../js/api.js'),'utf8'),{
    window,localStorage:storage,sessionStorage:storage,URLSearchParams,
    location:{origin:'http://test.local',protocol:'http:',pathname:'/ui/',search:'',hash:''},
    fetch:async url=>{urls.push(new URL(url));return {ok:true,json:async()=>({})};},
  });
  await window.API.listSessionSummaries(null,{withArchiveState:true});
  assert.equal(urls[0].searchParams.get('archive_scope'),'active');assert.equal(urls[0].searchParams.get('with_archive_state'),'true');
  await window.API.listSessionSummaries('归档会话');
  assert.equal(urls[1].searchParams.get('archive_scope'),'all');assert.equal(urls[1].searchParams.get('session_id'),'归档会话');
  await window.API.searchHistory('query');assert.equal(urls[2].searchParams.get('archive_scope'),'active');
  await window.API.searchHistory('query',{archiveScope:'archived'});assert.equal(urls[3].searchParams.get('archive_scope'),'archived');
  await window.API.searchHistory('query',{archiveScope:'all'});assert.equal(urls[4].searchParams.get('archive_scope'),'all');
});
