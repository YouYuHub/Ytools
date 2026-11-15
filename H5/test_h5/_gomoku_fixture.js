// 五子棋：15×15 棋盘，黑白轮流落子，先连成五子者胜；点击底部按钮可重新开始
stage.width = 640;
stage.height = 720;
const W = stage.width, H = stage.height;

const SIZE = 15;
const CELL = 38;
const MARGIN = (W - (SIZE - 1) * CELL) / 2;
const TOP = 70;

let board, current, gameOver, winner, lastMove;

function init() {
  board = Array.from({ length: SIZE }, () => Array(SIZE).fill(0));
  current = 1; // 1=黑, 2=白
  gameOver = false;
  winner = 0;
  lastMove = null;
  draw();
}

function toPixel(r, c) {
  return { x: MARGIN + c * CELL, y: TOP + r * CELL };
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function drawStone(r, c, color) {
  const p = toPixel(r, c);
  const radius = CELL / 2 - 2;
  const grad = ctx.createRadialGradient(p.x - 5, p.y - 5, 2, p.x, p.y, radius);
  if (color === 1) {
    grad.addColorStop(0, '#666');
    grad.addColorStop(1, '#111');
  } else {
    grad.addColorStop(0, '#fff');
    grad.addColorStop(1, '#d9d9d9');
  }
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = color === 1 ? '#000' : '#aaa';
  ctx.lineWidth = 1;
  ctx.stroke();
}

function draw() {
  // 背景
  ctx.fillStyle = '#e7cfa3';
  ctx.fillRect(0, 0, W, H);

  // 顶部状态栏
  ctx.fillStyle = '#5d4037';
  ctx.fillRect(0, 0, W, 50);
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 22px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  let status;
  if (gameOver) {
    status = winner ? (winner === 1 ? '黑棋获胜！' : '白棋获胜！') : '平局！';
  } else {
    status = current === 1 ? '轮到：黑棋 ●' : '轮到：白棋 ○';
  }
  ctx.fillText('五子棋  —  ' + status, W / 2, 25);

  // 棋盘网格
  ctx.strokeStyle = '#4a3b2a';
  ctx.lineWidth = 1;
  for (let i = 0; i < SIZE; i++) {
    const a = toPixel(i, 0), b = toPixel(i, SIZE - 1);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    const c = toPixel(0, i), d = toPixel(SIZE - 1, i);
    ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(d.x, d.y); ctx.stroke();
  }

  // 星位
  const stars = [[3,3],[3,7],[3,11],[7,3],[7,7],[7,11],[11,3],[11,7],[11,11]];
  ctx.fillStyle = '#333';
  for (const [r, c] of stars) {
    const p = toPixel(r, c);
    ctx.beginPath();
    ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
    ctx.fill();
  }

  // 棋子
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      if (board[r][c]) drawStone(r, c, board[r][c]);
    }
  }

  // 最后一手标记
  if (lastMove) {
    const p = toPixel(lastMove.r, lastMove.c);
    ctx.strokeStyle = board[lastMove.r][lastMove.c] === 1 ? '#fff' : '#c00';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
    ctx.stroke();
  }

  // 重新开始按钮
  const bx = W / 2 - 70, by = H - 55, bw = 140, bh = 38;
  ctx.fillStyle = '#5d4037';
  roundRect(bx, by, bw, bh, 8);
  ctx.fill();
  ctx.fillStyle = '#fff';
  ctx.font = '18px sans-serif';
  ctx.fillText('重新开始', W / 2, by + bh / 2);

  ctx.textBaseline = 'alphabetic';
}

function checkWin(r, c, color) {
  const dirs = [[1, 0], [0, 1], [1, 1], [1, -1]];
  for (const [dr, dc] of dirs) {
    let count = 1;
    for (let d = 1; d < 5; d++) {
      const nr = r + dr * d, nc = c + dc * d;
      if (nr < 0 || nr >= SIZE || nc < 0 || nc >= SIZE || board[nr][nc] !== color) break;
      count++;
    }
    for (let d = 1; d < 5; d++) {
      const nr = r - dr * d, nc = c - dc * d;
      if (nr < 0 || nr >= SIZE || nc < 0 || nc >= SIZE || board[nr][nc] !== color) break;
      count++;
    }
    if (count >= 5) return true;
  }
  return false;
}

function isFull() {
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      if (board[r][c] === 0) return false;
    }
  }
  return true;
}

stage.addEventListener('click', (e) => {
  const mx = e.offsetX, my = e.offsetY;

  // 重新开始按钮
  const bx = W / 2 - 70, by = H - 55, bw = 140, bh = 38;
  if (mx >= bx && mx <= bx + bw && my >= by && my <= by + bh) {
    init();
    console.log('已重新开始，黑棋先行。');
    return;
  }

  if (gameOver) return;

  // 计算最近交叉点
  const c = Math.round((mx - MARGIN) / CELL);
  const r = Math.round((my - TOP) / CELL);
  if (r < 0 || r >= SIZE || c < 0 || c >= SIZE) return;

  const p = toPixel(r, c);
  if (Math.hypot(mx - p.x, my - p.y) > CELL * 0.45) return;
  if (board[r][c] !== 0) return;

  board[r][c] = current;
  lastMove = { r, c };

  if (checkWin(r, c, current)) {
    gameOver = true;
    winner = current;
    console.log((current === 1 ? '黑棋' : '白棋') + '获胜！');
  } else if (isFull()) {
    gameOver = true;
    winner = 0;
    console.log('平局！');
  } else {
    current = 3 - current;
  }

  draw();
});

init();
console.log('游戏开始：黑棋先行，点击棋盘交叉点落子。');
