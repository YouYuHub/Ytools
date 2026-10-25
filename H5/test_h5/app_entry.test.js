const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm');
const fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../js/app.js'),'utf8');
test('application startup uses only an explicit session parameter',()=>{
  for(const [search,target] of [['',null],['?session=alpha','alpha'],['?session=default','default'],['?other=1',null]]){
    const opened=[];const noop=()=>{};
    const App={refreshThemeUI:noop,updateExportButton:noop,autosize:noop,isMobile:()=>false,
      loadTools:noop,loadWorkDir:noop,loadSessions:noop,openInitialSession:id=>opened.push(id)};
    vm.runInNewContext(source,{window:{App},location:{search},URLSearchParams,
      localStorage:{getItem:()=>null,setItem:noop,removeItem:noop}});
    assert.deepEqual(opened,[target]);
  }
});
