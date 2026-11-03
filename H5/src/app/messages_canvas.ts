/**
 * Canvas 沙箱运行时与 Mermaid 异步渲染（原 messages.js 3583-4216 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App: any) {
  "use strict";
  const {
    el, chatInner
  } = App;

  // ---------- Canvas 沙箱运行时（```canvas 程序块） ----------
  // 每块独立 <iframe sandbox="allow-scripts">（不给 allow-same-origin →
  // opaque origin：父页读不到它的 contentDocument，它也访问不到页面
  // DOM/cookie/存储/同源接口）。通信全走 postMessage：
  //   1) iframe BOOT 完成 → 回发 canvas-ready；
  //   2) 父页按 ev.source 匹配 frame，下发 canvas-run{id,code}；
  //   3) iframe 内 new Function('stage','console',code) 执行，console 逐行
  //      实时回传（canvas-log{id,line}）；
  //   4) 同步体结束后"结算探测"：包装 rAF/定时器统计待执行回调数——归零则
  //      脚本是一次性绘制，回传 canvas-done{logs,shot}（shot 为沙箱内
  //      stage.toDataURL 自报快照——跨源读不到画布像素）；仍有任务在跑
  //      （动画/游戏循环）则回发 canvas-live，父页保持运行态：用户可与
  //      画布交互（点击/键盘），父页可随时下发 canvas-shot 索要当前帧；
  //      脚本自身收尾（live 归零）时自动补发 canvas-done 携带最终快照；
  //      异步回调抛错经 __onasync 上报 canvas-error，父页复位状态。
  // 用户代码不经 srcdoc 注入，避免 HTML 解析转义风险；流式重建的旧
  // iframe 随节点一起被丢弃，无需显式回收。

  const CANVAS_RUNTIME_CSS =
    // height:100%：body 高度必须显式铺满 iframe，#stage 的百分比高度才有
    // 确定的解析基准（否则随内容收缩，画布位图与点击坐标全部失真）；
    // html 锁定不滚动、body 承担滚动——显示缩放（stage 尺寸乘倍率）放大后
    // 舞台超出 iframe 视口时由 body 滚动查看
    "html,body{margin:0;padding:0;width:100%;height:100%;background:transparent;}" +
    "html{overflow:hidden;}" +
    "body{overflow:auto;}" +
    // object-fit:contain：画布位图等比缩放居中，常规/全屏下都不拉伸变形；
    // margin:0 auto 让未溢出时水平居中，放大溢出时 auto 归零（从左起边可滚）
    "#stage{display:block;width:100%;height:100%;object-fit:contain;margin:0 auto;}";

  // 沙箱内宿主脚本：监听父页指令，执行用户脚本并回传日志/异常/快照
  const CANVAS_BOOT_JS = [
    "var stage=document.getElementById('stage');",
    // 常见 Canvas 全局别名。作为沙箱 window 全局暴露而不是 new Function 参数，
    // 这样既能直接使用 ctx，也兼容脚本内部 `const ctx = ...` 的声明写法。
    "var canvas=stage;",
    "var ctx=stage.getContext('2d'),context=ctx;",
    "var width=stage.width,height=stage.height,W=width,H=height,WIDTH=width,HEIGHT=height;",
    // 保留原生定时器引用（包装前的），探测逻辑自身不走被包装的全局
    "var __raf=window.requestAnimationFrame.bind(window),",
    "  __craf=window.cancelAnimationFrame.bind(window),",
    "  __st=window.setTimeout.bind(window),",
    "  __si=window.setInterval.bind(window),",
    "  __cst=window.clearTimeout.bind(window),",
    "  __csi=window.clearInterval.bind(window);",
    // 活跃度追踪：__live = 待执行的 rAF/timeout/interval 回调数
    "var __live=0,__pend={r:{},t:{},i:{}},__sentLive=false,__runId=null,logs=[];",
    "function shot(){",
    "  try{return stage.toDataURL('image/png');}catch(e){",
    "    return 'ERR:'+String(e&&e.message||e);",
    "  }",
    "}",
    "function fmt(v: any){",
    "  try{return typeof v==='string'?v:JSON.stringify(v);}catch(_){return String(v);}",
    "}",
    "function join(args: any){return Array.prototype.map.call(args,fmt).join(' ');}",
    "function push(line: any){",
    "  logs.push(line);if(logs.length>400)logs.splice(0,logs.length-400);",
    "  parent.postMessage({type:'canvas-log',id:__runId,line:line},'*');",
    "}",
    // 独活中 live 归零 → 脚本自然收尾：补发 done 与最终快照（宏任务里复查，
    // 给 rAF 回调"先减计数再调度下一帧"的过程留出重新累加的机会）
    "function __checkDone(){",
    "  if(!__sentLive||__live>0)return;",
    "  __sentLive=false;",
    "  parent.postMessage({type:'canvas-done',id:__runId,logs:logs.slice(-200),shot:shot()},'*');",
    "}",
    "function __rel(kind: any, h: any){",
    "  if(__pend[kind][h]){delete __pend[kind][h];__live--;}",
    "  if(__sentLive)__st(__checkDone,0);",
    "}",
    // 异步回调抛错不静默：上报 canvas-error，父页复位状态（日志行由父页
    // 在 canvas-error 分支统一追加，这里不再重复 push）
    "function __onasync(e: any){",
    "  parent.postMessage({type:'canvas-error',id:__runId,logs:logs.slice(-200),shot:shot(),error:String(e&&e.message||e)},'*');",
    "}",
    "window.requestAnimationFrame=function(cb: any){",
    "  var h;h=__raf(function(t: any){__rel('r',h);try{cb(t);}catch(e){__onasync(e);}});",
    "  __pend.r[h]=1;__live++;return h;",
    "};",
    "window.cancelAnimationFrame=function(h: any){__rel('r',h);return __craf(h);};",
    "window.setTimeout=function(f: any, ms: any){",
    "  var a=Array.prototype.slice.call(arguments,2),h;",
    "  h=__st(function(){__rel('t',h);try{f.apply(null,a);}catch(e){__onasync(e);}},ms);",
    "  __pend.t[h]=1;__live++;return h;",
    "};",
    "window.clearTimeout=function(h: any){__rel('t',h);return __cst(h);};",
    "window.setInterval=function(f: any, ms: any){",
    "  var a=Array.prototype.slice.call(arguments,2),h;",
    "  h=__si(function(){try{f.apply(null,a);}catch(e){__onasync(e);}},ms);",
    "  __pend.i[h]=1;__live++;return h;",
    "};",
    "window.clearInterval=function(h: any){__rel('i',h);return __csi(h);};",
    "window.addEventListener('message',function(ev: any){",
    "  var d=ev.data||{};",
    "  if(d.type==='canvas-shot'){",
    "    parent.postMessage({type:'canvas-shot-data',id:d.id,shot:shot()},'*');",
    "    return;",
    "  }",
    // 显示缩放：父页把倍率下发为 stage 元素尺寸百分比（object-fit:contain
    // 等比放大位图），body 滚动查看；toDataURL 快照不受影响（位图原始尺寸）
    "  if(d.type==='canvas-zoom'){",
    "    try{stage.style.width=(d.zoom*100)+'%';stage.style.height=(d.zoom*100)+'%';}catch(_){}",
    "    return;",
    "  }",
    "  if(d.type!=='canvas-run')return;",
    "  __runId=d.id;",
    "  if(d.zoom&&d.zoom!==1){try{stage.style.width=(d.zoom*100)+'%';stage.style.height=(d.zoom*100)+'%';}catch(_){}}",
    "  var sc={log:function(){push(join(arguments));},",
    "    info:function(){push(join(arguments));},",
    "    warn:function(){push('[WARNING] '+join(arguments));},",
    "    error:function(){push('[error] '+join(arguments));}};",
    "  try{",
    "    new Function('stage','console',d.code)(stage,sc);",
    "  }catch(err){",
    "    push('[异常] '+String(err&&err.message||err));",
    "    parent.postMessage({type:'canvas-error',id:d.id,logs:logs.slice(-200),shot:shot(),error:String(err&&err.message||err)},'*');",
    "    return;",
    "  }",
    // 结算探测：同步体结束后等一帧再判定（期间脚本已把自己的首轮 rAF/
    // 定时任务挂上）；仍有任务 → 独活模式，父页保持运行态供交互
    "  __st(function(){__raf(function(){",
    "    if(__live>0){",
    "      __sentLive=true;",
    "      parent.postMessage({type:'canvas-live',id:d.id},'*');",
    "    }else{",
    "      parent.postMessage({type:'canvas-done',id:d.id,logs:logs.slice(-200),shot:shot()},'*');",
    "    }",
    "  });});",
    "});",
    // 未捕获异常兜底（用户直接挂在 stage/window 上的监听器抛错不走包装）
    "window.onerror=function(msg: any, src: any, line: any){",
    "  __onasync(new Error(String(msg)+(line?('（第'+line+'行）'):'')));",
    "  return true;",
    "};",
    // 事件监听支持：
    // 1) stage 指针事件的 offsetX/offsetY 换算为画布位图坐标——沙箱画布
    //    以 object-fit:contain 等比缩放居中，CSS 坐标 ≠ 位图坐标，不换算
    //    会导致点击命中有系统性偏移；facade 继承原事件，preventDefault 等
    //    方法显式转发；
    // 2) 监听器计入活跃度：纯点击交互的小游戏没有帧循环，也要保持运行态；
    //    removeEventListener 对应回落（按原监听器函数映射到包装函数）。
    "var __liveListeners=new WeakMap();",
    "function __wrapEvent(e: any){",
    "  if(!e||typeof e.clientX!=='number')return e;",
    "  try{",
    "    var r=stage.getBoundingClientRect();",
    "    var s=Math.min(r.width/stage.width,r.height/stage.height)||1;",
    "    var ox=(r.width-stage.width*s)/2,oy=(r.height-stage.height*s)/2;",
    // Proxy 而非 Object.create 继承：原生事件访问器（clientX/target 等）
    // 依赖内部槽位，原型链继承后 this 指向 facade 会抛 Illegal invocation；
    // Proxy 的 get 先拦截 offset 坐标，其余属性回退原事件（函数绑定 this）
    "    return new Proxy(e,{",
    "      get:function(t: any, p: any){",
    "        if(p==='offsetX')return (t.clientX-r.left-ox)/s;",
    "        if(p==='offsetY')return (t.clientY-r.top-oy)/s;",
    "        var v=t[p];",
    "        return typeof v==='function'?v.bind(t):v;",
    "      }",
    "    });",
    "  }catch(_){return e;}",
    "}",
    "var __stageAen=stage.addEventListener.bind(stage),__stageRen=stage.removeEventListener.bind(stage);",
    "stage.addEventListener=function(t: any, fn: any, opt: any){",
    "  if(typeof fn!=='function')return __stageAen(t,fn,opt);",
    "  __live++;",
    "  var wrapped=function(e: any){return fn.call(this,__wrapEvent(e));};",
    "  __liveListeners.set(fn,wrapped);",
    "  return __stageAen(t,wrapped,opt);",
    "};",
    "stage.removeEventListener=function(t: any, fn: any, opt: any){",
    "  var wrapped=__liveListeners.get(fn);",
    "  if(wrapped){__liveListeners.delete(fn);__live--;return __stageRen(t,wrapped,opt);}",
    "  return __stageRen(t,fn,opt);",
    "};",
    "var __winAen=window.addEventListener.bind(window),__winRen=window.removeEventListener.bind(window);",
    "window.addEventListener=function(t: any, fn: any, opt: any){",
    "  if(typeof fn!=='function')return __winAen(t,fn,opt);",
    "  __live++;",
    "  __liveListeners.set(fn,fn);",
    "  return __winAen(t,fn,opt);",
    "};",
    "window.removeEventListener=function(t: any, fn: any, opt: any){",
    "  if(__liveListeners.get(fn)===fn){__liveListeners.delete(fn);__live--;}",
    "  return __winRen(t,fn,opt);",
    "};",
    "parent.postMessage({type:'canvas-ready'},'*');",
  ].join("\n");

  // 生成沙箱 iframe：srcdoc 为受控运行时模板（仅画布 + BOOT，代码走消息）
  function buildCanvasFrame() {
    const frame = document.createElement("iframe");
    frame.className = "md-canvas-frame";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("title", "Canvas 沙箱");
    frame.srcdoc =
      "<!doctype html><html><head><meta charset=\"utf-8\"><style>" +
      CANVAS_RUNTIME_CSS + "</style></head><body>" +
      "<canvas id=\"stage\" width=\"720\" height=\"420\"></canvas>" +
      "<script>" + CANVAS_BOOT_JS + "<\/script></body></html>";
    return frame;
  }

  // 画布快照缓存：canvasId -> {dataUrl, error}（canvas-done/error 时写入，
  // 独活模式下由 canvas-shot-data 实时更新）
  const canvasShots = new Map();
  // 独活模式"截图"请求中标记：canvasId -> true（响应到达后触发导出）
  const pendingCanvasExport = new Map();

  function findCanvasBlock(canvasId: any) {
    return chatInner.querySelector(
      '.md-canvas-block[data-canvas-id="' + canvasId + '"]');
  }

  // 沙箱消息总线：ready → 下发该块源码；log → 日志实时追加；
  // live → 保持运行态（画布可交互）；done/error → 状态复位 + 快照缓存
  function onCanvasMessage(ev: any) {
    const data = ev.data || {};
    if (data.type === "canvas-ready") {
      // 按 source 匹配发起方 iframe，取其所在块的源码下发
      const frames = chatInner.querySelectorAll(".md-canvas-frame");
      for (let i = 0; i < frames.length; i++) {
        if (frames[i].contentWindow === ev.source) {
          const block = frames[i].closest(".md-canvas-block");
          if (block && frames[i].contentWindow) {
            frames[i].contentWindow.postMessage({
              type: "canvas-run",
              id: block.dataset.canvasId || "",
              code: Markdown.unescapeHtml(block.dataset.svgCode || ""),
              zoom: App.zoomScaleOf(block),
            }, "*");
          }
          return;
        }
      }
      return;
    }
    if (typeof data.type !== "string" || !data.type.startsWith("canvas-")) return;
    const sourceBlock: any = Array.from(chatInner.querySelectorAll(".md-canvas-frame"))
      .find((frame: any) => frame.contentWindow === ev.source);
    if (!sourceBlock || sourceBlock.closest(".md-canvas-block").dataset.canvasId !== data.id) return;
    if (data.type === "canvas-log") {
      const block = findCanvasBlock(data.id || "");
      if (block) appendCanvasLog(block, [String(data.line || "")]);
      return;
    }
    if (data.type === "canvas-live") {
      // 动画/游戏脚本仍在运行：保持运行态（■ 停止），画布可交互
      const block = findCanvasBlock(data.id || "");
      if (block) setCanvasState(block, "running");
      return;
    }
    if (data.type === "canvas-shot-data") {
      // 独活模式实时快照：缓存后若有挂起的导出请求则继续导出
      // （导出的是缓存里的 dataUrl 字符串——此前误传 block DOM 元素，
      // fetch("[object HTMLDivElement]") 直接 "Failed to fetch"）
      const id = data.id || "";
      storeCanvasShot(id, data.shot);
      if (pendingCanvasExport.get(id)) {
        pendingCanvasExport.delete(id);
        const rec = canvasShots.get(id);
        if (rec && rec.dataUrl) {
          exportCanvasShot(rec.dataUrl);
        } else if (rec && rec.error) {
          App.toast(rec.error);
        } else {
          App.toast("快照未就绪，请重试截图");
        }
      }
      return;
    }
    if (data.type === "canvas-done" || data.type === "canvas-error") {
      const block = findCanvasBlock(data.id || "");
      if (!block) return;
      storeCanvasShot(data.id || "", data.shot);
      // 日志已由 canvas-log 逐行实时到达（postMessage 同通道 FIFO 有序），
      // done/error 不再重复追加
      if (data.type === "canvas-error" && data.error) {
        appendCanvasLog(block, ["[异常] " + String(data.error)]);
      }
      // 同步脚本瞬间完成（或独活脚本自然收尾）：状态复位，按钮回到「▶ 运行」
      setCanvasState(block, "idle");
    }
  }
  window.addEventListener("message", onCanvasMessage);

  // 快照缓存写入：只接受 PNG dataURL；污染失败转为可读错误说明
  function storeCanvasShot(id: any, shotData: any) {
    const shot = String(shotData || "");
    canvasShots.set(id || "", {
      dataUrl: shot.indexOf("data:image/png") === 0 ? shot : "",
      error: shot.indexOf("data:image/png") === 0 ? "" :
        (shot ? shot.replace(/^ERR:/, "画布被浏览器标记污染，无法导出：") : ""),
    });
  }

  // 日志区：画布下方等宽文本框（textContent 注入，防日志内容注入 HTML）。
  // 行缓冲挂在块元素属性上（流式逐行追加，上限 300 行，超出丢最旧）
  function appendCanvasLog(block: any, lines: any) {
    const view = block.querySelector(".md-canvas-view");
    if (!view) return;
    let logBox = view.querySelector(".md-canvas-log");
    if (!logBox) {
      logBox = document.createElement("div");
      logBox.className = "md-canvas-log";
      view.appendChild(logBox);
    }
    const buffer = block.__canvasLogLines || (block.__canvasLogLines = []);
    for (let i = 0; i < lines.length; i++) buffer.push(lines[i]);
    while (buffer.length > 300) buffer.shift();
    logBox.textContent = buffer.length ? buffer.join("\n") : "(无输出)";
    logBox.scrollTop = logBox.scrollHeight;
  }

  function resetCanvasLog(block: any) {
    block.__canvasLogLines = [];
    const view = block.querySelector(".md-canvas-view");
    const logBox = view && view.querySelector(".md-canvas-log");
    if (logBox) logBox.remove();
  }

  // 运行状态辅助：更新 data-canvas-state 并同步「运行/停止」按钮文案
  function setCanvasState(block: any, stateName: any) {
    block.dataset.canvasState = stateName;
    const runBtn = block.querySelector("[data-canvas-action='run']");
    if (!runBtn) return;
    if (stateName === "running") {
      runBtn.textContent = "■ 停止";
      runBtn.classList.add("is-running");
    } else {
      runBtn.textContent = "▶ 运行";
      runBtn.classList.remove("is-running");
    }
  }

  // Canvas 控件交互（事件委托，与 SVG 控件同容器挂载）
  chatInner.addEventListener("click", async function(e: any) {
    const btn = e.target.closest("[data-canvas-action]");
    if (!btn) return;
    const block = btn.closest(".md-canvas-block");
    if (!block) return;
    const action = btn.dataset.canvasAction;

    // 「运行/停止」同一颗按钮：未运行 → 确认后在沙箱执行；运行中 → 停止。
    // 实际执行由沙箱 ready 握手触发（onCanvasMessage 收到 canvas-ready 后
    // 下发源码），这里只负责确认 + 建 iframe + 置运行态。
    if (action === "run") {
      if (block.dataset.canvasState === "running") {
        setCanvasState(block, "idle");
        const frame = block.querySelector(".md-canvas-frame");
        if (frame) frame.remove();
        const view = block.querySelector(".md-canvas-view");
        if (view) {
          view.innerHTML = '<div class="md-canvas-placeholder">已停止 · 点击「▶ 运行」重新执行</div>';
        }
        return;
      }
      // 运行确认：沙箱只禁 DOM 访问，脚本体本身无法预检
      if (!window.confirm("在该沙箱中运行此 Canvas 脚本？\n（脚本在隔离 iframe 中执行，无法访问本页数据，但请注意其绘制/计算内容）")) {
        return;
      }
      const view = block.querySelector(".md-canvas-view");
      if (!view) return;
      resetCanvasLog(block);
      view.innerHTML = "";
      view.appendChild(buildCanvasFrame());
      setCanvasState(block, "running");
      return;
    }

    // 「重置」：任意状态下清空画布回到未运行占位（区别于「停止」：不要求
    // 处于运行态），同时清掉上次运行的截图缓存，防止重置后截到旧图
    if (action === "reset") {
      const frame = block.querySelector(".md-canvas-frame");
      if (frame) frame.remove();
      canvasShots.delete(block.dataset.canvasId || "");
      resetCanvasLog(block);
      setCanvasState(block, "idle");
      const view = block.querySelector(".md-canvas-view");
      if (view) {
        view.innerHTML = '<div class="md-canvas-placeholder">未运行 · 点击「▶ 运行」在沙箱中执行脚本</div>';
      }
      return;
    }

    if (action === "full") {
      // 全屏：作用于整个控件块（含操作按钮，全屏内仍可停止/截图/退出），
      // 沙箱 iframe 随容器铺满，沙箱内 #stage 等比缩放。全屏的是父页元素
      // 而非 iframe 自身，无需给沙箱加 allow-fullscreen
      if (document.fullscreenElement === block) {
        document.exitFullscreen().then(function () {
          syncCanvasFullscreenButtons();
        }, function () { /* 失败保持原按钮 */ });
        return;
      }
      if (!document.fullscreenEnabled) {
        App.toast("当前环境不支持全屏显示");
        return;
      }
      block.requestFullscreen().then(function () {
        // promise 落定时全屏已生效，直接同步按钮（Esc 退出仍靠事件）。
        syncCanvasFullscreenButtons();
      }, function(err: any) {
        App.toast("进入全屏失败：" + ((err as any) && (err as any).message || err));
      });
      return;
    }

    if (action === "shot") {
      // 截图导出：同步绘制用 done 时自报的快照（canvasShots 缓存）；
      // 独活模式（动画/游戏）向沙箱实时索要当前帧（跨源读不到 iframe
      // 像素，截图只能由沙箱内 stage.toDataURL 自报）
      const id = block.dataset.canvasId || "";
      if (block.dataset.canvasState === "running") {
        const frame = block.querySelector(".md-canvas-frame");
        if (!frame || !frame.contentWindow) {
          App.toast("沙箱未在运行，先点「▶ 运行」");
          return;
        }
        pendingCanvasExport.set(id, true);
        frame.contentWindow.postMessage({ type: "canvas-shot", id: id }, "*");
        return;
      }
      const rec = canvasShots.get(id);
      if (!rec || (!rec.dataUrl && !rec.error)) {
        App.toast("尚未运行，先点「▶ 运行」");
        return;
      }
      if (rec.error) { App.toast(rec.error); return; }
      exportCanvasShot(rec.dataUrl);
      return;
    }
  });

  // data URL → Blob：不走 fetch（Chromium 对 URL 有 2MB 上限，大画布的
  // PNG data URL 超限后 fetch 直接抛 "Failed to fetch"；file:// 源下同样
  // 受限），用 atob 手动解码，无长度限制
  function dataUrlToBlob(dataUrl: any) {
    const comma = dataUrl.indexOf(",");
    const mime = ((/^data:([^;,]*)/.exec(dataUrl.slice(0, comma)) || [])[1]) || "image/png";
    const bin = atob(dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  // 快照导出：优先复制到剪贴板，剪贴板不可用/写入失败降级为下载 PNG
  async function exportCanvasShot(dataUrl: any) {
    let blob;
    try {
      blob = /^data:/i.test(dataUrl) ? dataUrlToBlob(dataUrl) : await (await fetch(dataUrl)).blob();
    } catch (err) {
      App.toast("截图失败：" + ((err as any) && (err as any).message || err));
      return;
    }
    if (navigator.clipboard && (window as any).ClipboardItem) {
      try {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        App.toast("已复制画布截图");
        return;
      } catch (err) {
        // 剪贴板被策略/权限拒绝：走下载兜底，不再直接报错
      }
    }
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "canvas-shot.png";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    App.toast("已下载画布截图 PNG");
  }

  // SVG/Mermaid 全屏尺寸同步：图片视图的 svg 带流式渲染注入的内联样式
  // （width:min(100%,170vh*宽高比)，内联优先级高于 CSS 且每图比例不同），
  // 全屏"整图限高完整可见"的宽度必须按各图比例在 JS 里算：
  // 进入全屏 → 改写内联 width = min(块宽, (视口高-96px)*比例)；退出 → 还原。
  // 显示缩放（--md-zoom）生效时基准宽/高上限同乘倍率（放大后容器滚动查看）。
  function syncSvgFullscreenLayout() {
    const fsEl = document.fullscreenElement;
    chatInner.querySelectorAll(".md-svg-block").forEach(function(block: any) {
      const svg = block.querySelector(".md-svg-view svg, .md-svg-view img.md-animated-svg");
      if (!svg) return;
      if (fsEl === block) {
        if (block.__svgStyleBackup == null) {
          block.__svgStyleBackup = svg.getAttribute("style") || "";
        }
        const m = /aspect-ratio:\s*([\d.]+)/.exec(svg.getAttribute("style") || "");
        const ar = m ? parseFloat(m[1]) : 0;
        const maxH = Math.max(240, window.innerHeight - 96);
        const zoom = App.zoomScaleOf(block);
        const w = (ar > 0 ? Math.min(block.clientWidth, maxH * ar) : block.clientWidth) * zoom;
        svg.style.width = Math.floor(w) + "px";
        svg.style.height = "auto";
        svg.style.maxHeight = Math.floor(maxH * zoom) + "px";
      } else if (block.__svgStyleBackup != null) {
        svg.setAttribute("style", block.__svgStyleBackup);
        block.__svgStyleBackup = null;
      }
    });
  }
  // 全屏状态同步按钮文案（Esc 退出 / 多块同时存在时统一刷新）：
  // canvas 与 SVG/Mermaid 控件各有一套 full 按钮，两套委托都在此统一刷新；
  // 图标与媒体预览弹窗共用 Markdown.fullscreenIcons，取不到时回退字符。
  function canvasFullscreenLabel(fullscreen: boolean, text: string): string {
    const md: any = (window as unknown as { Markdown: any }).Markdown;
    const icons = md && md.fullscreenIcons;
    const icon = icons && icons.expand && icons.compress
      ? (fullscreen ? icons.compress : icons.expand)
      : (fullscreen ? "⤡ " : "⛶ ");
    return icon + (icon.charAt(0) === "<" ? " " + text : text);
  }
  function syncCanvasFullscreenButtons(): void {
    const fullscreen = Boolean(document.fullscreenElement);
    chatInner.querySelectorAll('[data-canvas-action="full"]').forEach(function(btn: any) {
      btn.innerHTML = canvasFullscreenLabel(fullscreen, fullscreen ? "退出全屏" : "全屏");
      btn.title = fullscreen ? "退出全屏（Esc）" : "全屏显示画布（Esc 退出）";
    });
    chatInner.querySelectorAll('.md-svg-block [data-svg-action="full"]').forEach(function(btn: any) {
      btn.innerHTML = canvasFullscreenLabel(fullscreen, fullscreen ? "退出全屏" : "全屏");
      btn.title = fullscreen ? "退出全屏（Esc）" : "全屏显示（Esc 退出）";
    });
  }
  document.addEventListener("fullscreenchange", function () {
    syncSvgFullscreenLayout();
    syncCanvasFullscreenButtons();
  });
  // 全屏期间窗口尺寸变化：重算当前全屏块的图片尺寸
  window.addEventListener("resize", function () {
    const fsEl = document.fullscreenElement;
    if (fsEl && fsEl.classList && fsEl.classList.contains("md-svg-block")) {
      syncSvgFullscreenLayout();
    }
  });

  // 媒体元素加载失败（404/路径失效等）：capture 捕获 media error 事件
  // （error 不冒泡），控件整体标记为不可用并显示占位说明
  document.addEventListener("error", function(e: any) {
    const target = e.target;
    if (!target || !target.classList || !target.classList.contains("md-media-el")) return;
    const widget = target.closest(".md-media");
    if (widget) widget.classList.add("is-broken");
  }, true);

  // ---------- Mermaid 异步渲染接线 ----------
  // ```mermaid 控件在 Markdown.render()（同步纯函数）里只产出「渲染中」占位，
  // mermaid.render 是异步 API 且库 5.5MB 需懒加载——这里在节点挂载后异步回填：
  // MutationObserver 捕获流式整块重建后的新控件，按 data-mermaid-id 去重渲染。
  // pending 集合做并发合并：同一控件流式期间重复渲染只保留最后一次。
  const mermaidPending = new Map(); // id → Promise

  function scheduleMermaidRender(widget: any) {
    const id = widget.dataset.mermaidId;
    const state = widget.dataset.mermaidState;
    if (!id || (state !== "pending" && state !== "rerender") || mermaidPending.has(id)) return;
    const holder = widget.querySelector('[data-mermaid-holder="' + id + '"]');
    if (!holder) return;
    const code = Markdown.unescapeHtml(widget.dataset.svgCode || "");
    if (!code.trim()) return;
    widget.dataset.mermaidState = "rendering";
    holder.classList.remove("md-mermaid-error");
    // 渲染参数：换用当前控件 id 调 mermaid.render（流式重建后同图已换新 id，
    // 沿用旧临时节点 id 可能撞上上次的孤儿节点导致渲染不出）；源码原文做
    // 缓存 key——同 code 的重建命中缓存直接复用上次 SVG，不再重复 render。
    const opts = { renderId: id, cachedCode: code };
    const attempt = function(isRetry: any) {
      const p = Markdown.renderMermaidInto(id, code, holder, opts)
        .then(function () {
          widget.dataset.mermaidState = "done";
          mermaidPending.delete(id);
        })
        .catch(function () {
          mermaidPending.delete(id);
          // 失败自动重试一次（mermaid.render 偶发瞬时失败，重试常能成功）；
          // 重试仍失败才落 failed 态，交给「↻ 重渲染」按钮手动恢复
          if (!isRetry && widget.isConnected &&
            widget.dataset.mermaidState === "rendering") {
            return attempt(true);
          }
          widget.dataset.mermaidState = "failed";
        });
      mermaidPending.set(id, p);
      return p;
    };
    attempt(false);
  }

  function mountMermaidBlocks(root: any) {
    if (!root) return;
    if (root.nodeType === 1 && root.matches && root.matches(".md-mermaid-block")) {
      scheduleMermaidRender(root);
      return;
    }
    const nodes = root.querySelectorAll ? root.querySelectorAll(".md-mermaid-block") : [];
    for (let i = 0; i < nodes.length; i++) scheduleMermaidRender(nodes[i]);
  }

  // 挂载触发：markdown 重渲染是整体替换 innerHTML/appendChild 混用，
  // MutationObserver 统一捕获（配置 childList+subtree 足够，无需 attributes）
  if (typeof MutationObserver !== "undefined") {
    new MutationObserver(function(mutations: any) {
      for (let i = 0; i < mutations.length; i++) {
        const added = mutations[i].addedNodes;
        for (let k = 0; k < added.length; k++) {
          if (added[k].nodeType === 1) mountMermaidBlocks(added[k]);
        }
      }
    }).observe(chatInner, { childList: true, subtree: true });
  }

  // 流结束兜底：finish() 里清掉仍处于渲染中的控件状态残留。
  // 竞态修复：只处理 promise 不在飞且无 svg 的孤儿控件——流结束的瞬间
  // 渲染 promise 往往仍在异步执行（懒加载/解析耗时），直接强置 failed
  // 会误杀几秒后正常完成的渲染（表现：提示失败但刷新后正常）。
  function finalizeMermaidBlocks() {
    chatInner.querySelectorAll('.md-mermaid-block[data-mermaid-state="rendering"]').forEach(function(widget: any) {
      if (mermaidPending.has(widget.dataset.mermaidId)) return; // 在飞：等回填/重试收尾
      const holder = widget.querySelector("[data-mermaid-holder]");
      if (holder && holder.querySelector("svg")) {
        widget.dataset.mermaidState = "done"; // 已有结果：仅纠正状态标记
        return;
      }
      widget.dataset.mermaidState = "failed";
      if (holder && !holder.classList.contains("md-mermaid-error")) {
        holder.classList.add("md-mermaid-error");
        holder.textContent = "Mermaid 渲染未完成（流式被中断），点「↻ 重渲染」恢复";
      }
    });
    // pending（未开始渲染）的控件：正常情况下流结束后 MutationObserver 会补
    // 渲染，但若最终 DOM 重建早于观察回调则可能永远停留——这里补一枪。
    chatInner.querySelectorAll('.md-mermaid-block[data-mermaid-state="pending"]').forEach(function(widget: any) {
      scheduleMermaidRender(widget);
    });
  }

  // 手动重渲染（↻ 重渲染按钮）：失败态或显示异常时重建渲染
  function rerenderMermaid(block: any) {
    if (!block) return;
    const id = block.dataset.mermaidId;
    if (!id) return;
    const holder = block.querySelector('[data-mermaid-holder="' + id + '"]');
    if (!holder) return;
    // 去掉上次失败文案，回占位态后按 pending 重新调度（若 promise 在飞则跳过）
    holder.classList.remove("md-mermaid-error");
    if (!holder.querySelector("svg")) {
      holder.innerHTML = "Mermaid 渲染中…";
    }
    block.dataset.mermaidState = "pending";
    scheduleMermaidRender(block);
  }

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.finalizeMermaidBlocks = finalizeMermaidBlocks;
  // syncSvgFullscreenLayout 为 messages.js 拆分新增导出（原模块内直接调用，现跨文件经 App.* 调用）
  App.syncSvgFullscreenLayout = syncSvgFullscreenLayout;
  // rerenderMermaid 同上（SVG/图片视图的 ↻ 重渲染按钮经 widgets 文件调用）
  App.rerenderMermaid = rerenderMermaid;

})((window as unknown as { App: any }).App);
