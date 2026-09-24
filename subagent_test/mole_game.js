// 打地鼠：30 秒内点击冒头的地鼠得分（+10），结束后点击画面重开。
// 顶部实时显示分数与剩余时间；地鼠出现节奏随游戏进行逐渐加快。

(function () {
  'use strict';

  var W = 720, H = 420;
  var GAME_TIME = 30;            // 游戏总时长（秒）
  var SCORE_PER_HIT = 10;

  // ---- 状态 ----
  var state = 'ready';           // ready | playing | over
  var score = 0;
  var timeLeft = GAME_TIME;
  var lastTs = 0;
  var elapsed = 0;               // 本局已进行的毫秒数

  // 3x3 洞位（等距网格，画布居中偏下留出顶部 HUD）
  var holes = [];
  (function initHoles() {
    var cols = 3, rows = 3;
    var cellW = W / (cols + 1);
    var cellH = 300 / (rows + 1);
    var topY = 130;
    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        holes.push({
          x: cellW * (c + 1),
          y: topY + cellH * (r + 1),
          moleUp: false,     // 是否冒头
          upT: 0,            // 已冒头毫秒
          upDur: 0,          // 本次冒头时长
          hitFx: 0           // 击中特效剩余毫秒
        });
      }
    }
  })();

  // ---- 出怪计时 ----
  var spawnTimer = null;
  var minUp = 700, maxUp = 1100;     // 冒头时长（ms），随进度下限微降
  var minGap = 800, maxGap = 1300;   // 出怪间隔（ms），随进度收紧

  function progress() { return Math.min(1, elapsed / (GAME_TIME * 1000)); }
  function rand(a, b) { return a + Math.random() * (b - a); }

  function clearSpawnTimer() {
    if (spawnTimer !== null) { clearTimeout(spawnTimer); spawnTimer = null; }
  }

  function scheduleSpawn() {
    if (state !== 'playing') return;
    var p = progress();
    var gap = rand(minGap - 350 * p, maxGap - 500 * p);
    spawnTimer = setTimeout(function () {
      spawnTimer = null;
      if (state !== 'playing') return;
      spawnMole();
      scheduleSpawn();
    }, gap);
  }

  function spawnMole() {
    // 优先选择没有地鼠冒头的洞
    var free = holes.filter(function (h) { return !h.moleUp && h.hitFx <= 0; });
    if (!free.length) return;
    var h = free[Math.floor(Math.random() * free.length)];
    var p = progress();
    h.moleUp = true;
    h.upT = 0;
    h.upDur = rand(minUp - 200 * p, maxUp - 150 * p);
  }

  // ---- 特效飘字 ----
  var floaters = []; // {x, y, text, t, life}

  // ---- 流程控制 ----
  function startGame() {
    score = 0;
    timeLeft = GAME_TIME;
    elapsed = 0;
    floaters.length = 0;
    for (var i = 0; i < holes.length; i++) {
      holes[i].moleUp = false;
      holes[i].upT = 0;
      holes[i].upDur = 0;
      holes[i].hitFx = 0;
    }
    clearSpawnTimer();
    state = 'playing';
    scheduleSpawn();
  }

  function endGame() {
    state = 'over';
    clearSpawnTimer(); // 停止出怪，防止定时器泄漏
    for (var i = 0; i < holes.length; i++) holes[i].moleUp = false;
  }

  // ---- 绘制辅助 ----
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawHole(ctx, h) {
    ctx.fillStyle = '#6b4a2f';
    ctx.beginPath();
    ctx.ellipse(h.x, h.y, 46, 18, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#3a2718';
    ctx.beginPath();
    ctx.ellipse(h.x, h.y, 38, 13, 0, 0, Math.PI * 2);
    ctx.fill();
  }

  // 地鼠从洞中升起（upT/upDur -> 0..1 再回落）
  function moleRise(h) {
    var half = h.upDur * 0.25;
    var t = h.upT;
    if (t < half) return t / half;                       // 升起
    if (t > h.upDur - half) return Math.max(0, (h.upDur - t) / half); // 缩回
    return 1;
  }

  function drawMole(ctx, h) {
    var rise = moleRise(h);
    if (rise <= 0) return;
    var bodyH = 70 * rise;
    var bodyW = 52;
    var y = h.y + 8 - bodyH;

    ctx.save();
    // 裁剪到洞口以上，模拟从洞里钻出
    ctx.beginPath();
    ctx.rect(h.x - bodyW, h.y - 130, bodyW * 2, 138);
    ctx.clip();

    // 身体
    ctx.fillStyle = '#a9743f';
    roundRect(ctx, h.x - bodyW / 2, h.y + 8 - bodyH, bodyW, bodyH + 6, 16);
    ctx.fill();
    // 肚皮
    ctx.fillStyle = '#d9b382';
    ctx.beginPath();
    ctx.ellipse(h.x, h.y + 10 - bodyH * 0.35, 14, 16 * rise, 0, 0, Math.PI * 2);
    ctx.fill();
    // 眼睛
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.arc(h.x - 10, h.y + 12 - bodyH, 6, 0, Math.PI * 2);
    ctx.arc(h.x + 10, h.y + 12 - bodyH, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#222';
    ctx.beginPath();
    ctx.arc(h.x - 10, h.y + 12 - bodyH, 3, 0, Math.PI * 2);
    ctx.arc(h.x + 10, h.y + 12 - bodyH, 3, 0, Math.PI * 2);
    ctx.fill();
    // 鼻子
    ctx.fillStyle = '#e06a6a';
    ctx.beginPath();
    ctx.ellipse(h.x, h.y + 20 - bodyH, 6, 4, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  function drawHitFx(ctx, h) {
    if (h.hitFx <= 0) return;
    var t = 1 - h.hitFx / 400; // 0..1
    ctx.strokeStyle = 'rgba(255, 215, 0, ' + (1 - t).toFixed(3) + ')';
    ctx.lineWidth = 4 - 3 * t;
    ctx.beginPath();
    ctx.arc(h.x, h.y - 30, 18 + 40 * t, 0, Math.PI * 2);
    ctx.stroke();
  }

  function drawHud(ctx) {
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 26px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('得分: ' + score, 30, 44);
    ctx.textAlign = 'right';
    ctx.fillText('时间: ' + Math.ceil(timeLeft) + 's', W - 30, 44);
    // 时间条
    ctx.fillStyle = 'rgba(255,255,255,0.25)';
    ctx.fillRect(30, 58, W - 60, 8);
    ctx.fillStyle = timeLeft < 6 ? '#ff5252' : '#8ce99a';
    ctx.fillRect(30, 58, (W - 60) * Math.max(0, timeLeft / GAME_TIME), 8);
  }

  function drawOverlay(ctx) {
    ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#fff';
    ctx.textAlign = 'center';
    if (state === 'ready') {
      ctx.font = 'bold 40px sans-serif';
      ctx.fillText('打地鼠', W / 2, H / 2 - 30);
      ctx.font = '20px sans-serif';
      ctx.fillText('30 秒内点击冒头的地鼠，每只 +10 分', W / 2, H / 2 + 15);
      ctx.fillStyle = '#ffd43b';
      ctx.fillText('点击画面开始', W / 2, H / 2 + 60);
    } else {
      ctx.font = 'bold 44px sans-serif';
      ctx.fillText('时间到！', W / 2, H / 2 - 40);
      ctx.font = 'bold 32px sans-serif';
      ctx.fillText('最终得分: ' + score, W / 2, H / 2 + 10);
      ctx.fillStyle = '#ffd43b';
      ctx.font = '20px sans-serif';
      ctx.fillText('点击画面重新开始', W / 2, H / 2 + 60);
    }
  }

  // ---- 帧循环 ----
  function frame(ts) {
    if (!lastTs) lastTs = ts;
    var dt = Math.min(50, ts - lastTs); // 切后台回来时不突变
    lastTs = ts;

    var ctx = stage.getContext('2d');

    // 背景（每帧重绘避免残影）
    ctx.fillStyle = '#7ec850';
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    for (var g = 0; g < 40; g++) {
      var gx = (g * 137) % W, gy = 90 + ((g * 89) % (H - 100));
      ctx.fillRect(gx, gy, 10, 4);
    }

    if (state === 'playing') {
      elapsed += dt;
      timeLeft = Math.max(0, GAME_TIME - elapsed / 1000);
      if (timeLeft <= 0) endGame();
    }

    for (var i = 0; i < holes.length; i++) {
      var h = holes[i];
      drawHole(ctx, h);
      if (h.moleUp) {
        h.upT += dt;
        if (h.upT >= h.upDur) h.moleUp = false;
      }
      if (h.hitFx > 0) h.hitFx -= dt;
      drawMole(ctx, h);
      drawHitFx(ctx, h);
    }

    // 飘字
    for (var f = floaters.length - 1; f >= 0; f--) {
      var fl = floaters[f];
      fl.t += dt;
      if (fl.t >= fl.life) { floaters.splice(f, 1); continue; }
      var a = 1 - fl.t / fl.life;
      ctx.fillStyle = 'rgba(255, 235, 59, ' + a.toFixed(3) + ')';
      ctx.font = 'bold 24px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(fl.text, fl.x, fl.y - fl.t * 0.05);
    }

    drawHud(ctx);
    if (state !== 'playing') drawOverlay(ctx);

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // ---- 交互 ----
  stage.addEventListener('click', function (e) {
    var x = e.offsetX, y = e.offsetY;
    if (state === 'ready') { startGame(); return; }
    if (state === 'over') { startGame(); return; }

    // 命中判定：与绘制坐标一致（地鼠中心约在 h.x, h.y - 30*rise）
    for (var i = 0; i < holes.length; i++) {
      var h = holes[i];
      if (!h.moleUp) continue;
      var rise = moleRise(h);
      if (rise < 0.4) continue; // 还没怎么冒头，不算
      var cx = h.x, cy = h.y + 11 - 35 * rise; // 与绘制中身体中心一致
      var dx = x - cx, dy = y - cy;
      if (dx * dx + dy * dy <= 40 * 40) {
        h.moleUp = false;
        h.hitFx = 400;
        score += SCORE_PER_HIT;
        floaters.push({ x: h.x, y: h.y - 50, text: '+10', t: 0, life: 700 });
        return;
      }
    }
    // 点空不扣分
  });
})();
