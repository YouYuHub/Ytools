const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');

// ```chart 数据图表：渲染器单测 + markdown 栅栏接入。
function loadChartApi() {
  const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/chart.ts'), 'utf8'));
  const sandbox = { module: { exports: {} } };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox.module.exports;
}

function loadMarkdownApi(chartApi) {
  const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/markdown.ts'), 'utf8'));
  const sandbox = { module: { exports: {} }, ChartRender: chartApi };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  // markdown IIFE 取 globalThis 作为 global：把 ChartRender 挂到上下文全局
  sandbox.ChartRender = chartApi;
  return sandbox.module.exports;
}

const BAR_JSON = JSON.stringify({
  type: 'bar',
  title: '单次请求费用（美元）',
  xAxis: ['Claude Haiku 5.5', 'GPT-6 Luna'],
  series: [{ name: '费用', data: [0.085, 0.022] }],
});

test('bar：双类目出双柱 + 标题 + 数值', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(BAR_JSON);
  assert.ok(out.svg, '应渲染出 svg');
  assert.match(out.svg, /^<svg[^>]*viewBox="0 0 640 400"/);
  assert.ok(out.svg.includes('单次请求费用（美元）'));
  // 2 根柱（rx=2 的 rect；单系列无图例）
  assert.equal((out.svg.match(/<rect /g) || []).length, 2);
  assert.ok(out.svg.includes('0.085') && out.svg.includes('0.022'));
});

test('line：折线 + 数据点 + 悬浮标题', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(JSON.stringify({
    type: 'line', title: '趋势',
    xAxis: ['周一', '周二', '周三'],
    series: [{ name: '访问', data: [10, 20, 15] }],
  }));
  assert.ok(out.svg.includes('<polyline '));
  assert.equal((out.svg.match(/<circle /g) || []).length, 3);
  assert.ok(out.svg.includes('<title>访问 · 周二：20</title>'));
});

test('pie：name/value 数据出扇区 + 百分比 + 图例', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(JSON.stringify({
    type: 'pie', title: '占比',
    data: [{ name: '甲', value: 70 }, { name: '乙', value: 30 }],
  }));
  assert.equal((out.svg.match(/<path /g) || []).length, 2);
  assert.ok(out.svg.includes('70%') && out.svg.includes('30%'));
  assert.ok(out.svg.includes('甲 70（70.0%）'));
});

test('pie：兼容 series[0].data + xAxis 写法', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(JSON.stringify({
    type: 'pie', xAxis: ['甲', '乙'], series: [{ data: [1, 3] }],
  }));
  assert.equal((out.svg.match(/<path /g) || []).length, 2);
  assert.ok(out.svg.includes('25.0%') && out.svg.includes('75.0%'));
});

test('hbar：横条 + 动态高度', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(JSON.stringify({
    type: 'hbar', xAxis: ['很长很长的类目名称甲', '乙'],
    series: [{ data: [5, 10] }],
  }));
  assert.equal((out.svg.match(/<rect /g) || []).length, 2);
  assert.ok(out.svg.includes('很长很长的类目名称甲'));
});

test('histogram 别名按 bar 渲染', () => {
  const Chart = loadChartApi();
  const parsed = Chart.normalizeSpec(JSON.stringify({
    type: 'histogram', xAxis: ['0-10', '10-20'], series: [{ data: [3, 5] }],
  }));
  assert.equal(parsed.ok, true);
  assert.equal(parsed.spec.type, 'bar');
});

test('非法输入进错误态（不抛异常）', () => {
  const Chart = loadChartApi();
  assert.match(Chart.renderChart('{oops').error, /JSON 解析失败/);
  assert.match(Chart.renderChart('[]').error, /必须是 JSON 对象/);
  assert.match(Chart.renderChart('{"type":"radar"}').error, /未知图表类型/);
  assert.match(Chart.renderChart('{"type":"bar"}').error, /缺少 series/);
  assert.match(Chart.renderChart('{"type":"pie","data":[]}').error, /缺少数据/);
  assert.match(
    Chart.renderChart('{"type":"pie","data":[{"name":"a","value":0}]}').error, /全为 0/
  );
});

test('超限截断 + 非数字置零 + 警告注记', () => {
  const Chart = loadChartApi();
  const xAxis = Array.from({ length: 70 }, (_, i) => 'c' + i);
  const parsed = Chart.normalizeSpec(JSON.stringify({
    type: 'bar', xAxis, series: [{ data: xAxis.map(() => 1) }],
  }));
  assert.equal(parsed.spec.xAxis.length, Chart.MAX_CATEGORIES);
  assert.ok(parsed.warnings.join('').includes('截断'));
  const bad = Chart.normalizeSpec(JSON.stringify({
    type: 'bar', xAxis: ['a', 'b'], series: [{ data: ['x', 2] }],
  }));
  assert.equal(JSON.stringify(bad.spec.series[0].data), '[0,2]');
  assert.ok(bad.warnings.join('').includes('置 0'));
  const out = Chart.renderChart(JSON.stringify({
    type: 'bar', xAxis, series: [{ data: xAxis.map(() => 1) }],
  }));
  assert.ok(out.svg.includes('截断'));
});

test('多系列出分组 + 底部图例', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(JSON.stringify({
    type: 'bar', xAxis: ['a', 'b'],
    series: [{ name: '甲', data: [1, 2] }, { name: '乙', data: [3, 4] }],
  }));
  // 4 根柱 + 2 个图例色块
  assert.equal((out.svg.match(/<rect /g) || []).length, 6);
});

test('文本转义：标签注入被中和', () => {
  const Chart = loadChartApi();
  const out = Chart.renderChart(JSON.stringify({
    type: 'bar', title: '<script>alert(1)</script>',
    xAxis: ['<img src=x>'], series: [{ data: [1] }],
  }));
  assert.ok(!out.svg.includes('<script>') && !out.svg.includes('<img src'));
  assert.ok(out.svg.includes('&lt;script&gt;'));
});

test('主题与导出兼容：只用 currentColor，不用 var(--*)', () => {
  const Chart = loadChartApi();
  for (const code of [
    BAR_JSON,
    '{"type":"line","xAxis":["a"],"series":[{"data":[1]}]}',
    '{"type":"pie","data":[{"name":"a","value":1}]}',
    '{"type":"hbar","xAxis":["a"],"series":[{"data":[1]}]}',
  ]) {
    const out = Chart.renderChart(code);
    assert.ok(!out.svg.includes('var(--'), code);
    assert.ok(out.svg.includes('currentColor'), code);
  }
});

test('markdown 接入：闭合 chart 栅栏成双视图控件', () => {
  const Chart = loadChartApi();
  const Markdown = loadMarkdownApi(Chart);
  const html = Markdown.render('前言\n\n```chart\n' + BAR_JSON + '\n```\n\n后记');
  assert.ok(html.includes('md-chart-block'));
  assert.ok(html.includes('data-svg-kind="chart"'));
  assert.ok(html.includes('数据图表'));
  assert.ok(html.includes('language-json'));
  assert.ok(html.includes('<svg '));
  assert.ok(html.includes('前言') && html.includes('后记'));
});

test('markdown 接入：非法 chart 显示错误占位 + 保留代码视图', () => {
  const Chart = loadChartApi();
  const Markdown = loadMarkdownApi(Chart);
  const html = Markdown.render('```chart\n{oops}\n```');
  assert.ok(html.includes('md-chart-block'));
  assert.ok(html.includes('md-chart-error'));
  assert.ok(html.includes('JSON 解析失败'));
});

test('markdown 接入：未闭合 chart 栅栏保持普通代码块', () => {
  const Chart = loadChartApi();
  const Markdown = loadMarkdownApi(Chart);
  const html = Markdown.render('```chart\n' + BAR_JSON + '\n');
  assert.ok(!html.includes('md-chart-block'));
  assert.ok(html.includes('codeblock'));
});
