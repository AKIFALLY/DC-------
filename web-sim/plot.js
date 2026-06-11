/* ───────────────────────────────────────────────────────────────────────────
 * plot.js — 自動測試 / 手動紀錄曲線繪製 (Canvas)
 *
 * 對應 utils/plotter.py 的 _build_auto_test_fig：3 列 × 2 欄，共享 x 軸 (時間)。
 *   左欄：電壓 V / 功率 P / 轉速 RPM
 *   右欄：電流 I / 溫度 / 功率因數 PF
 * 各階段分界以灰色虛線標出，最上一列標階段名稱。整段無資料的格畫「無資料」。
 * ─────────────────────────────────────────────────────────────────────────── */

const PANELS = [
  { label: "電壓 V (V)", key: "V", color: "#1f77b4" },
  { label: "電流 I (A)", key: "I", color: "#ff7f0e" },
  { label: "功率 P (W)", key: "P", color: "#2ca02c" },
  { label: "溫度 (°C)", key: "temperature", color: "#d62728" },
  { label: "轉速 RPM", key: "rpm", color: "#9467bd" },
  { label: "功率因數 PF", key: "power_factor", color: "#8c564b" },
];

function stageBounds(rows) {
  const b = []; let last = null;
  for (const r of rows) { if (r.stage !== last) { b.push([r.stage, r.elapsed_s]); last = r.stage; } }
  return b;
}

function renderPlot(canvas, rows, title) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 880;
  const cssH = Math.max(360, Math.round(cssW * 0.62));
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  canvas.style.height = cssH + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, cssW, cssH);

  ctx.fillStyle = "#222"; ctx.font = "bold 14px 'Microsoft JhengHei',sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(title || "自動測試 — 階段負載時間曲線", cssW / 2, 18);

  if (!rows || rows.length === 0) return;

  const ncols = 2, nrows = 3;
  const padTop = 30, padBottom = 8, padL = 12, padR = 12, gapX = 44, gapY = 30;
  const cellW = (cssW - padL - padR - gapX) / ncols;
  const cellH = (cssH - padTop - padBottom - gapY * (nrows - 1)) / nrows;

  const t = rows.map(r => r.elapsed_s);
  const tMin = Math.min(...t), tMax = Math.max(...t, tMin + 1);
  const bounds = stageBounds(rows);

  PANELS.forEach((p, idx) => {
    const col = idx % ncols, row = Math.floor(idx / ncols);
    const x0 = padL + col * (cellW + gapX);
    const y0 = padTop + row * (cellH + gapY);
    drawPanel(ctx, p, rows, t, tMin, tMax, x0, y0, cellW, cellH, bounds, row === 0);
  });
}

function drawPanel(ctx, panel, rows, t, tMin, tMax, x0, y0, w, h, bounds, topRow) {
  // 軸框
  ctx.strokeStyle = "#ccc"; ctx.lineWidth = 1;
  ctx.strokeRect(x0, y0, w, h);

  const series = rows.map(r => r[panel.key]);
  const has = series.some(v => v !== null && v !== undefined && !Number.isNaN(v));

  // y 軸標籤 (左側、彩色)
  ctx.save();
  ctx.translate(x0 - 6, y0 + h / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillStyle = panel.color; ctx.font = "11px 'Microsoft JhengHei',sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "bottom";
  ctx.fillText(panel.label, 0, 0);
  ctx.restore();

  // 階段分界虛線
  ctx.setLineDash([4, 3]); ctx.strokeStyle = "rgba(120,120,120,0.6)";
  for (const [label, t0] of bounds) {
    const x = x0 + (t0 - tMin) / (tMax - tMin) * w;
    ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y0 + h); ctx.stroke();
    if (topRow) {
      ctx.setLineDash([]);
      ctx.fillStyle = "dimgray"; ctx.font = "9px 'Microsoft JhengHei',sans-serif";
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(label, x + 2, y0 + 2);
      ctx.setLineDash([4, 3]);
    }
  }
  ctx.setLineDash([]);

  if (!has) {
    ctx.fillStyle = "#bbb"; ctx.font = "12px 'Microsoft JhengHei',sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("無資料", x0 + w / 2, y0 + h / 2);
    return;
  }

  const vals = series.filter(v => v !== null && v !== undefined && !Number.isNaN(v));
  let yMin = Math.min(...vals), yMax = Math.max(...vals);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const pad = (yMax - yMin) * 0.08; yMin -= pad; yMax += pad;

  // y 刻度數值 (上下界)
  ctx.fillStyle = "#999"; ctx.font = "9px 'Consolas',monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "top";
  ctx.fillText(yMax.toFixed(1), x0 - 2, y0);
  ctx.textBaseline = "bottom";
  ctx.fillText(yMin.toFixed(1), x0 - 2, y0 + h);

  // 曲線
  ctx.strokeStyle = panel.color; ctx.lineWidth = 1.4; ctx.beginPath();
  let started = false;
  for (let k = 0; k < rows.length; k++) {
    const v = series[k];
    if (v === null || v === undefined || Number.isNaN(v)) { started = false; continue; }
    const x = x0 + (t[k] - tMin) / (tMax - tMin) * w;
    const y = y0 + h - (v - yMin) / (yMax - yMin) * h;
    if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
  }
  ctx.stroke();
}
