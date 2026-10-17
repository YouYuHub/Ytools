"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const parser = require("../js/history_parser.js");
const source = fs.readFileSync(path.join(__dirname, "../js/app/messages.js"), "utf8");
const start = source.indexOf("  async function jumpToHistoryMatch(");
const end = source.indexOf("  function jumpToQuestion(", start);

function node(cls, round, event, text) {
  const classes = new Set([cls]);
  const attributes = { "data-round": String(round), "data-source-event": String(event) };
  return { cls, dataset: {}, textContent: text, classes,
    classList: { add: (c) => classes.add(c) },
    getAttribute: (key) => attributes[key] ?? null,
    matches: (selector) => selector.split(", ").some((c) => classes.has(c.slice(1))),
    querySelectorAll: () => [],
    resultEvent: (event) => { attributes["data-result-event"] = String(event); },
  };
}
function harness(nodes, older) {
  const revealed = [], notices = [];
  const context = { App: { pauseAutoScroll() {} }, state: { sessionId: "s" }, loadOlderState: older || { records: null },
    chatInner: {
      querySelectorAll(selector) {
        const match = selector.match(/^\.([\w-]+)\[data-round="(\d+)"\]$/);
        return match ? nodes.filter((n) => n.cls === match[1] && n.getAttribute("data-round") === match[2]) : [];
      },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    },
    toast: (message) => notices.push(message),
    revealQuoteSource: (target, query) => revealed.push({ target, query }),
    window: { setTimeout },
    runOlderBatch: async () => {},
  };
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  return { context, jump: context.jumpToHistoryMatch, revealed, notices };
}

test("search parser preserves exact event indexes for reasoning and tool/result records", () => {
  const parsed = parser.parseHistory(JSON.stringify({ event: "chat_round", events: [
    { role: "user", content: "Q" },
    { role: "assistant", reasoning_content: "Think", content: "Answer", tool_calls: [{ function: { name: "read_file", arguments: "{}" } }] },
    { role: "tool", tool_name: "read_file", result: "R" },
  ] }));
  for (const kind of ["think", "assistant", "tool"]) assert.equal(parsed.records.find((r) => r.kind === kind).source_event_index, 1);
  assert.equal(parsed.records.find((r) => r.kind === "toolResult").source_event_index, 2);
});
test("identical reasoning text in a round still locates the correct event and unfolds it", async () => {
  const first = node("think-block", 2, 1, "same"), second = node("think-block", 2, 4, "same");
  const h = harness([first, second]);
  assert.equal(await h.jump({ session_id: "s", round_no: 2, event_index: 4, role: "assistant", field: "reasoning_content" }, "same"), true);
  assert.equal(h.revealed[0].target, second); assert.equal(second.classes.has("open"), true);
});
test("tool result uses its own event index even though it shares the call's DOM block", async () => {
  const first = node("tool-block", 1, 1, "result"), second = node("tool-block", 1, 4, "result");
  first.resultEvent(2); second.resultEvent(5);
  const h = harness([first, second]);
  await h.jump({ session_id: "s", round_no: 1, event_index: 5, role: "tool", field: "result" }, "result");
  assert.equal(h.revealed[0].target, second); assert.equal(second.classes.has("open"), true);
});
test("older hits are loaded through the existing batching path before scrolling", async () => {
  const nodes = [], records = [{ round: 1 }, { round: 2 }, { round: 3 }];
  const older = { records, start: 2, loading: false };
  const h = harness(nodes, older); let batches = 0;
  h.context.runOlderBatch = async () => {
    batches += 1; older.start -= 1;
    nodes.push(node("msg-assistant-body", older.start + 1, 1, "answer"));
  };
  await h.jump({ session_id: "s", round_no: 1, event_index: 1, role: "assistant", field: "content" }, "answer");
  assert.equal(batches, 2); assert.equal(h.revealed[0].target.getAttribute("data-round"), "1");
});
test("a newer node in the same round cannot hide an older exact event", async () => {
  const tail = node("msg-assistant-body", 1, 9, "new answer");
  const nodes = [tail], older = { records: [{ round: 1 }, { round: 1 }], start: 1, loading: false };
  const h = harness(nodes, older);
  const early = node("msg-assistant-body", 1, 1, "old answer");
  h.context.runOlderBatch = async () => { older.start = 0; nodes.unshift(early); };
  await h.jump({ session_id: "s", round_no: 1, event_index: 1, role: "assistant", field: "content" }, "old");
  assert.equal(h.revealed[0].target, early);
});
test("jump pauses pending bottom correction and anchors the original matched spelling", async () => {
  const target = node("msg-assistant-body", 1, 1, "Straße");
  const h = harness([target]); let paused = 0;
  h.context.App.pauseAutoScroll = () => paused++;
  await h.jump({ session_id: "s", round_no: 1, event_index: 1, role: "assistant", field: "content",
    snippets: [{ text: "Straße", start: 0, end: 6 }] }, "STRASSE");
  assert.equal(paused, 1); assert.equal(h.revealed[0].query, "Straße");
});
test("switching away while older messages load prevents a stale search jump", async () => {
  const nodes = [], older = { records: [{ round: 1 }, { round: 2 }], start: 1, loading: false };
  const h = harness(nodes, older);
  h.context.runOlderBatch = async () => { older.start = 0; h.context.state.sessionId = "other"; nodes.push(node("msg-user", 1, 0, "Q")); };
  assert.equal(await h.jump({ session_id: "s", round_no: 1, role: "user" }, "Q"), false);
  assert.equal(h.revealed.length, 0);
});
test("subagent identity selects the right parallel task; compaction unfolds normally", async () => {
  const a = node("agent-block", 1, 2, "same"), b = node("agent-block", 1, 3, "same");
  a.dataset.agentId = "a"; b.dataset.agentId = "b";
  const summary = node("compaction-bar", 1, 7, "summary");
  const h = harness([a, b, summary]);
  await h.jump({ session_id: "s", round_no: 1, role: "sub_agent", agent_id: "b" }, "same");
  assert.equal(h.revealed[0].target, b);
  await h.jump({ session_id: "s", round_no: 1, role: "compaction" }, "summary");
  assert.equal(summary.classes.has("open"), true);
});
test("legacy content without a mapped block falls back to the round's question", async () => {
  const question = node("msg-user", 2, 0, "Q"), h = harness([question]);
  assert.equal(await h.jump({ session_id: "s", round_no: 2, role: "tool" }, "old"), true);
  assert.equal(h.revealed[0].target, question);
  assert.equal(await h.jump({ session_id: "s", round_no: 3, role: "tool" }, "deleted"), false);
  assert.equal(h.notices.length, 1);
});

test("subagent aggregation preserves physical event positions for repeated tool results", () => {
  const parsed = parser.parseHistory(JSON.stringify({event:'chat_round',events:[
    {role:'user',content:'Q'},
    {event:'sub_agent',phase:'start',agent_id:'a',task:'task'},
    {event:'sub_agent',phase:'model_call',agent_id:'a',seq:1,content:'text'},
    {event:'sub_agent',phase:'tool_result',agent_id:'a',seq:1,tool_call_id:'x',result:'same'},
    {event:'sub_agent',phase:'tool_result',agent_id:'a',seq:1,tool_call_id:'y',result:'same'},
    {event:'sub_agent',phase:'done',agent_id:'a',final_reply:'done'},
  ]}));
  const agent = parsed.records.find(r=>r.kind==='agentBlock');
  assert.equal(agent.source_event_index,1);
  assert.deepEqual(agent.entries.map(e=>e.source_event_index),[2,3,4]);
  assert.equal(agent.done_event_index,5);
});

test("subagent jump selects the exact nested result and opens its parent containers", async () => {
  const agent=node('agent-block',1,1,'same'); agent.dataset.agentId='a';
  const first=node('tool-block',1,2,'same'), second=node('tool-block',1,3,'same');
  first.parentElement=agent;second.parentElement=agent;
  const get = second.getAttribute;
  second.getAttribute=k=>k==='data-agent-result-event'?'8':get(k);
  const output=node('tool-io',1,8,'same');
  second.querySelectorAll=()=>[node('tool-io',1,7,'{}'),output];
  agent.querySelectorAll=()=>[first,second];
  const h=harness([agent]);
  await h.jump({session_id:'s',round_no:1,event_index:8,role:'sub_agent',field:'result',agent_id:'a'},'same');
  assert.equal(h.revealed[0].target,output);
  assert.equal(second.classes.has('open'),true);assert.equal(agent.classes.has('open'),true);
  assert.equal(first.classes.has('open'),false);
});

function textAnchor(parts, snippet, needle) {
  const nodes=parts.map(nodeValue=>({nodeValue,parentElement:{}}));
  let cursor=0;
  const context={NodeFilter:{SHOW_TEXT:4},quoteMatchText:s=>String(s||'').replace(/\s+/g,' ').trim(),
    document:{createTreeWalker:()=>({nextNode:()=>nodes[cursor++]||null}),
      createRange:()=>({setStart(node,offset){this.start=[nodes.indexOf(node),offset];},setEnd(node,offset){this.end=[nodes.indexOf(node),offset];}})}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function normalizedTextOffsets('),source.indexOf('  function revealQuoteSource(')),context);
  return context.searchTextAnchor({},needle,snippet).range;
}

test("selected excerpt resolves the later occurrence across inline Markdown text nodes", () => {
  const range=textAnchor(['前面也有心跳。','指定的心','跳出现在这里'],{text:'指定的心跳出现在这里',start:3,end:5},'心跳');
  assert.deepEqual(range.start,[1,3]);assert.deepEqual(range.end,[2,1]);
});

test("snippet character offsets disambiguate repeated terms and Unicode emoji", () => {
  const range=textAnchor(['🙂心跳，与后面的心跳不同'],{text:'🙂心跳，与后面的心跳不同',start:8,end:10},'心跳');
  assert.deepEqual(range.start,[0,9]);assert.deepEqual(range.end,[0,11]);
});
