"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const SlashCommandUtils = require("../js/slash_command_utils.js");

test("只在输入框开头且光标位于命令前缀时激活", function () {
  assert.deepEqual(SlashCommandUtils.readTrigger("/comp", 5, 5), { query: "comp", end: 5 });
  assert.deepEqual(SlashCommandUtils.readTrigger("/后面已有文本", 1, 1), { query: "", end: 1 });
  assert.equal(SlashCommandUtils.readTrigger("请用 /compact", 11, 11), null);
  assert.equal(SlashCommandUtils.readTrigger("/compact 内容", 11, 11), null);
  assert.equal(SlashCommandUtils.readTrigger("/compact", 3, 5), null);
});

test("执行命令只移除斜杠前缀，保留后续草稿", function () {
  const value = "/file 请分析附件";
  const trigger = SlashCommandUtils.readTrigger(value, 5, 5);
  assert.equal(SlashCommandUtils.removeTrigger(value, trigger), "请分析附件");
  assert.equal(SlashCommandUtils.removeTrigger("/已有正文", SlashCommandUtils.readTrigger("/已有正文", 1, 1)), "已有正文");
});

test("按命令、中文名称和别名筛选", function () {
  const commands = [
    { id: "compact", title: "压缩对话", description: "压缩上下文", aliases: ["总结"] },
    { id: "file", title: "上传文件", description: "添加附件", aliases: ["attach"] },
  ];
  assert.deepEqual(SlashCommandUtils.filterCommands(commands, "COMP").map(c => c.id), ["compact"]);
  assert.deepEqual(SlashCommandUtils.filterCommands(commands, "附件").map(c => c.id), ["file"]);
  assert.deepEqual(SlashCommandUtils.filterCommands(commands, "总结").map(c => c.id), ["compact"]);
  assert.deepEqual(SlashCommandUtils.filterCommands(commands, "").map(c => c.id), ["compact", "file"]);
});
