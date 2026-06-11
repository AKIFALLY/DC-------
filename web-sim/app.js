/* ───────────────────────────────────────────────────────────────────────────
 * app.js — UI 邏輯，1:1 對應 ui/main_window.py
 *
 * 串接模擬器與畫面：連線狀態機、三條輪詢、手動控制、按鈕啟用條件、
 * 兩種自動測試模式、手動紀錄、即時繪圖、CSV 下載。
 * ─────────────────────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);

// ── 模擬器與全域狀態 ────────────────────────────────────────────────────────
const sim = { pel: new PELSim(), plc: new PLCSim(), gpm: new GPMSim() };
const GPM_ENABLED = false;        // 對應 config.power_meter.enabled (false → 功率計屬選用)
const STAGE_PERCENTS = [5, 50, 100];

const S = {
  pelReady: false, plcReady: false, gpmReady: false,
  servoOn: false, alarmActive: false, speedAuto: false, manualEnableOn: false, gentestOn: null,
  testRunning: false, manualRecording: false,
  lastV: null, lastI: null, lastP: null, lastTemp: null, lastRpm: null, lastPf: null,
};

let pelTimer = null, plcTimer = null, gpmTimer = null, physTimer = null;
let liveRows = [], livePlotTimer = null, livePlotted = 0;
let autoRunner = null, testTimerInt = null, testT0 = 0, testTotal = 0;
let manualRows = [], manualTimer = null, manualT0 = 0;

// ── 工具 ───────────────────────────────────────────────────────────────────
function status(msg) { $("statusbar").textContent = msg; }
function tag() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}
function led(el, text, cls) { el.textContent = text; el.className = "st-led " + cls; }

// ═══ 物理 tick (100ms)：推進轉速 / 溫度 ═════════════════════════════════════
physTimer = setInterval(() => {
  const voc = sim.plc.voc();
  const [, , p] = sim.pel.measureVip(voc);
  sim.plc.tick(0.1, sim.pel.loadOn ? p : 0);
}, 100);

// ═══ 負載機 (PEL-5000C) ═════════════════════════════════════════════════════
function onPelConnect() {
  sim.pel.connect();
  sim.pel.setModeCC(parseFloat($("set-current").value) || 0);
  S.pelReady = true;
  $("pel-status").className = "status-led on"; $("pel-status").textContent = "● 已連線";
  $("pel-connect").disabled = true; $("pel-disconnect").disabled = false;
  $("pel-ip").disabled = true; $("pel-port").disabled = true;
  refreshManual();
  status(`已連線: GW Instek,PEL-5000C (模擬)`);
  pelTimer = setInterval(pelPoll, 200);
}
function onPelDisconnect() {
  if (S.manualRecording) stopManualRecord();
  clearInterval(pelTimer); pelTimer = null;
  sim.pel.disconnect();  // LOAD OFF + LOCAL
  S.pelReady = false;
  $("pel-status").className = "status-led off"; $("pel-status").textContent = "● 未連線";
  $("pel-connect").disabled = false; $("pel-disconnect").disabled = true;
  $("pel-ip").disabled = false; $("pel-port").disabled = false;
  $("val-v").textContent = "---"; $("val-i").textContent = "---"; $("val-p").textContent = "---";
  led($("load-state"), "● ---", "gray"); $("pel-mode").textContent = "---"; $("pel-mode").className = "st-led gray";
  refreshManual(); status("負載機已中斷連線");
}
function pelPoll() {
  if (!S.pelReady) return;
  const [v, i, p] = sim.pel.measureVip(sim.plc.voc());
  $("val-v").textContent = v.toFixed(3); $("val-i").textContent = i.toFixed(3); $("val-p").textContent = p.toFixed(2);
  S.lastV = v; S.lastI = i; S.lastP = p;
  if (sim.pel.loadOn) led($("load-state"), "● LOAD ON", "green");
  else led($("load-state"), "● LOAD OFF", "idle");
  $("pel-mode").textContent = sim.pel.mode; $("pel-mode").className = "st-led blue";
}

// ═══ PLC (Keyence KV) ═══════════════════════════════════════════════════════
function onPlcConnect() {
  sim.plc.connect(); S.plcReady = true;
  $("plc-status").className = "status-led on"; $("plc-status").textContent = "● 已連線";
  $("plc-connect").disabled = true; $("plc-disconnect").disabled = false;
  $("plc-ip").disabled = true; $("plc-port").disabled = true;
  refreshManual(); status(`PLC 已連線 (模擬)`);
  plcTimer = setInterval(plcPoll, 200);
}
function onPlcDisconnect() {
  clearInterval(plcTimer); plcTimer = null;
  sim.plc.disconnect();
  S.plcReady = false; S.speedAuto = false; S.servoOn = false; S.alarmActive = false;
  updateSpeedRow(); setManualEnable(false);
  S.lastTemp = null; S.lastRpm = null;
  $("plc-status").className = "status-led off"; $("plc-status").textContent = "● 未連線";
  $("plc-connect").disabled = false; $("plc-disconnect").disabled = true;
  $("plc-ip").disabled = false; $("plc-port").disabled = false;
  $("val-t").textContent = "---"; $("val-rpm").textContent = "---"; $("val-rpm-pct").textContent = "(--%)";
  led($("servo-state"), "● ---", "gray"); setGentest(null);
  led($("alarm-state"), "● ---", "gray"); led($("alarm-msg"), "---", "gray");
  refreshManual(); status("PLC 已中斷連線");
}
function plcPoll() {
  if (!S.plcReady) return;
  const m = sim.plc.readMonitor();
  $("val-t").textContent = Math.round(m.temperature);
  $("val-rpm").textContent = Math.round(m.rpm); $("val-rpm-pct").textContent = `(${m.rpm_percent}%)`;
  S.lastTemp = m.temperature; S.lastRpm = m.rpm;
  // 伺服
  const servo = m.servo !== 0;
  if (servo !== S.servoOn) { S.servoOn = servo; refreshManual(); }
  led($("servo-state"), servo ? "● ON" : "● OFF", servo ? "green" : "red");
  // 手動啟用 (DM7005)
  setManualEnable(m.manual_enable !== 0);
  // 異常
  S.alarmActive = m.alarm !== 0;
  led($("alarm-state"), S.alarmActive ? "● 異常" : "● 正常", S.alarmActive ? "red" : "green");
  updateAlarmMsg(m.alarm_code);
  // 速度控制來源 (DM7002)
  const sa = m.mode !== 0;
  if (sa !== S.speedAuto) {
    S.speedAuto = sa; updateSpeedRow(); refreshManual();
    status(sa ? "速度：自動控制 (可由速度設定送出 RPM)" : "速度：手動控制");
  }
  updatePrereqs();
}
function updateAlarmMsg(code) {
  if (code === 0) { led($("alarm-msg"), "無異常", "green"); return; }
  const msgs = [];
  for (let b = 0; b < 16; b++) if (code & (1 << b)) msgs.push(ALARM_BITS[b] || `bit${b}`);
  led($("alarm-msg"), "⚠ " + msgs.join("、"), "red");
}

// ═══ 功率計 (GPM-8310) ══════════════════════════════════════════════════════
function onGpmConnect() {
  sim.gpm.connect(); S.gpmReady = true;
  $("gpm-status").className = "status-led on"; $("gpm-status").textContent = "● 已連線";
  $("gpm-connect").disabled = true; $("gpm-disconnect").disabled = false;
  $("gpm-ip").disabled = true; $("gpm-port").disabled = true;
  updatePrereqs(); status("功率計已連線 (模擬)");
  gpmTimer = setInterval(() => { if (S.gpmReady) { const pf = sim.gpm.readPF(); $("val-pf").textContent = pf.toFixed(4); S.lastPf = pf; } }, 1000);
}
function onGpmDisconnect() {
  clearInterval(gpmTimer); gpmTimer = null;
  sim.gpm.disconnect(); S.gpmReady = false; S.lastPf = null;
  $("gpm-status").className = "status-led off"; $("gpm-status").textContent = "● 未連線";
  $("gpm-connect").disabled = false; $("gpm-disconnect").disabled = true;
  $("gpm-ip").disabled = false; $("gpm-port").disabled = false;
  $("val-pf").textContent = "---"; updatePrereqs(); status("功率計已中斷連線");
}

// ═══ 手動控制 ═══════════════════════════════════════════════════════════════
function setManualEnable(on) {
  if (on === S.manualEnableOn) return;
  S.manualEnableOn = on;
  $("control-box").classList.toggle("enabled-bg", on);
  const me = $("manual-enable");
  me.className = on ? "btn-green" : "btn-blue";
  refreshManual();
}
function setGentest(on) {
  S.gentestOn = on;
  if (on === null) led($("gentest-state"), "● ---", "gray");
  else led($("gentest-state"), on ? "● ON" : "● OFF", on ? "green" : "red");
}
function updateSpeedRow() { $("speed-row").style.display = S.speedAuto ? "flex" : "none"; }

function refreshManual() {
  $("settings-box").disabled = S.testRunning || S.manualRecording;
  $("control-box").disabled = S.testRunning;
  const manual = !S.testRunning && S.manualEnableOn;
  const pel = S.pelReady && manual, plc = S.plcReady && manual;
  $("set-current").disabled = !pel; $("apply-current").disabled = !pel;
  $("load-on").disabled = !pel; $("load-off").disabled = !pel;
  $("servo-on").disabled = !plc; $("servo-off").disabled = !plc;
  $("gentest-on").disabled = !plc; $("gentest-off").disabled = !plc;
  $("alarm-reset").disabled = !S.plcReady;
  $("manual-enable").disabled = !S.plcReady;
  const speedOk = S.plcReady && S.servoOn;
  $("manual-rpm").disabled = !speedOk; $("apply-rpm").disabled = !speedOk; $("stop-rpm").disabled = !speedOk;
  $("start-test").disabled = !(prereqsOk() && !S.testRunning && !S.manualRecording);
  $("stop-test").disabled = !S.testRunning;
  $("manual-record").disabled = !(S.pelReady && !S.testRunning);
  updatePrereqs();
}
function prereqsOk() {
  return S.pelReady && S.plcReady && S.servoOn && S.speedAuto && !S.alarmActive && (!GPM_ENABLED || S.gpmReady);
}
function updatePrereqs() {
  const el = $("prereq");
  if (S.testRunning) { el.style.display = "none"; return; }
  const issues = [];
  if (!S.pelReady) issues.push("負載機未連線");
  if (!S.plcReady) issues.push("PLC 未連線");
  else {
    if (!S.servoOn) issues.push("伺服未 ON");
    if (!S.speedAuto) issues.push("速度未切至自動");
    if (S.alarmActive) issues.push("異常發生中 (請先 RESET)");
  }
  if (GPM_ENABLED && !S.gpmReady) issues.push("功率計未連線");
  el.style.display = "block";
  if (issues.length) { el.className = "prereq warn"; el.textContent = "⚠ 開始測試前請確認： " + issues.join("、"); }
  else { el.className = "prereq ok"; el.textContent = "✓ 條件齊全，可開始測試"; }
}

// ═══ 自動測試 ═══════════════════════════════════════════════════════════════
function readStages() {
  return STAGE_PERCENTS.map((_, i) => ({
    time: parseInt($(`stage-time-${i}`).value) || 0,
    curr: parseFloat($(`stage-curr-${i}`).value) || 0,
    rpm: parseInt($(`stage-rpm-${i}`).value) || 0,
  }));
}
function onStartTest() {
  if (!S.pelReady || S.testRunning) return;
  const isRpm = $("mode-combo").value === "1";
  const st = readStages();
  const iMax = 60, vMax = 60;   // config dut.current_max / voltage_max
  let stages, mode, fixedCurrent = null, initialRpm;

  if (isRpm) {
    fixedCurrent = parseFloat($("fixed-current").value) || 0;
    if (fixedCurrent <= 0) return status("固定負載電流需 > 0");
    if (fixedCurrent > iMax) return status(`固定負載電流 ${fixedCurrent}A 超過 DUT 上限 ${iMax}A`);
    const rpms = st.map(s => s.rpm);
    if (Math.max(...rpms) <= 0) return status("各階段轉速設定至少要有一階 > 0");
    stages = ["速度一", "速度二", "速度三"].map((l, i) => [l, rpms[i], st[i].time]);
    mode = "rpm"; initialRpm = rpms[0];
  } else {
    const currents = st.map(s => s.curr);
    if (Math.max(...currents) <= 0) return status("各階段負載電流設定至少要有一階 > 0");
    const over = currents.filter(c => c > iMax);
    if (over.length) return status(`階段電流 ${Math.max(...over)}A 超過 DUT 上限 ${iMax}A`);
    stages = STAGE_PERCENTS.map((p, i) => [`${p}%`, currents[i], st[i].time]);
    mode = "current"; initialRpm = parseInt($("fixed-rpm").value) || 0;
  }

  S.testRunning = true; refreshManual();
  $("pel-disconnect").disabled = true;
  liveRows = []; livePlotted = 0; $("plot-placeholder").style.display = "none";
  $("test-status").textContent = "● 測試進行中"; $("test-status").className = "test-status running";
  status("自動測試開始");
  testTotal = stages.reduce((a, s) => a + s[2], 0); testT0 = performance.now() / 1000;
  updateTestTimer();

  // 連動 PLC：先開發電機測試 → 寫起始轉速 → 設 CC+LOAD ON → 測試繼電器 ON
  setGentest(true);
  sim.plc.writeRpmCommand(initialRpm);
  sim.pel.setModeCC(mode === "rpm" ? fixedCurrent : stages[0][1]);
  sim.pel.loadOnCmd();

  autoRunner = new AutoRunner(stages, mode, fixedCurrent, vMax, iMax);
  autoRunner.start();

  livePlotTimer = setInterval(redrawLive, 1000);
  testTimerInt = setInterval(updateTestTimer, 1000);
}
function updateTestTimer() {
  if (!S.testRunning) return;
  const el = Math.floor(performance.now() / 1000 - testT0);
  $("test-timer").textContent = `${el} / ${testTotal} 秒　剩 ${Math.max(0, testTotal - el)}s`;
}
function onStopTest() { if (autoRunner) { $("test-status").textContent = "停止中…"; autoRunner.stop(); } }

class AutoRunner {
  constructor(stages, mode, fixedCurrent, vMax, iMax) {
    this.stages = stages; this.mode = mode; this.fixedCurrent = fixedCurrent;
    this.vMax = vMax; this.iMax = iMax;
    this.rows = []; this.running = true; this.stageIdx = -1; this.t0 = 0; this.sampleInt = 500;
  }
  start() {
    this.t0 = performance.now();
    this.enterStage(0);
    this.int = setInterval(() => this.tick(), this.sampleInt);
  }
  enterStage(i) {
    this.stageIdx = i;
    const [label, setpoint] = this.stages[i];
    if (this.mode === "rpm") { sim.plc.writeRpmCommand(setpoint); $("test-status").textContent = `${label}：${setpoint} RPM`; }
    else { sim.pel.setCurrent(setpoint); $("test-status").textContent = `${label}：${setpoint.toFixed(2)} A`; }
    this.stageEnd = this.stages.slice(0, i + 1).reduce((a, s) => a + s[2], 0);
  }
  tick() {
    if (!this.running) return;
    const elapsed = (performance.now() - this.t0) / 1000;
    // 進入下一階段？
    while (this.stageIdx < this.stages.length - 1 && elapsed >= this.stageEnd) this.enterStage(this.stageIdx + 1);
    // 取樣
    const [v, i, p] = sim.pel.measureVip(sim.plc.voc());
    if (v > this.vMax || i > this.iMax) { sim.pel.loadOffCmd(); return this.fail(`超出安全上限 V=${v.toFixed(1)} I=${i.toFixed(1)}`); }
    const row = {
      timestamp: new Date().toLocaleString("sv"), elapsed_s: elapsed, stage: this.stages[this.stageIdx][0],
      set_current_A: this.mode === "rpm" ? this.fixedCurrent : this.stages[this.stageIdx][1],
      V: v, I: i, P: p, temperature: S.lastTemp, rpm: S.lastRpm, power_factor: S.lastPf,
    };
    this.rows.push(row); liveRows.push(row);
    status(`取樣中… ${this.rows.length} 筆`);
    // 全部階段跑完
    if (elapsed >= this.stageEnd && this.stageIdx >= this.stages.length - 1) this.finish();
  }
  finish() { this.running = false; clearInterval(this.int); sim.pel.loadOffCmd(); finalizeTest(); if (this.rows.length) saveRecord(this.rows, "自動測試 — 階段負載時間曲線"); else $("test-status").textContent = "測試結束（無資料）"; }
  fail(msg) { this.running = false; clearInterval(this.int); finalizeTest(); $("test-status").textContent = `測試失敗：${msg}`; status(`[自動測試] ${msg}`); }
  stop() { if (!this.running) return; this.running = false; clearInterval(this.int); sim.pel.loadOffCmd(); finalizeTest(); if (this.rows.length) saveRecord(this.rows, "自動測試 — 階段負載時間曲線"); }
}
function finalizeTest() {
  S.testRunning = false; autoRunner = null;
  clearInterval(livePlotTimer); livePlotTimer = null;
  clearInterval(testTimerInt); testTimerInt = null;
  $("test-status").classList.remove("running");
  sim.plc.writeRpmCommand(0); setGentest(false);
  refreshManual(); $("pel-disconnect").disabled = !S.pelReady;
}

// ═══ 手動紀錄 ═══════════════════════════════════════════════════════════════
function onToggleManualRecord() { S.manualRecording ? stopManualRecord() : startManualRecord(); }
function startManualRecord() {
  if (!S.pelReady) return status("請先連線負載機再開始手動紀錄");
  if (S.testRunning) return status("自動測試進行中，無法同時手動紀錄");
  S.manualRecording = true; manualRows = []; manualT0 = performance.now() / 1000;
  liveRows = []; livePlotted = 0; $("plot-placeholder").style.display = "none";
  $("manual-record").textContent = "停止紀錄"; $("pel-disconnect").disabled = true;
  refreshManual(); status("手動紀錄開始");
  manualTimer = setInterval(manualSample, 1000);
  livePlotTimer = setInterval(redrawLive, 1000);
}
function manualSample() {
  if (S.lastV === null) return;
  const row = {
    timestamp: new Date().toLocaleString("sv"), elapsed_s: performance.now() / 1000 - manualT0, stage: "",
    set_current_A: parseFloat($("set-current").value) || 0,
    V: S.lastV, I: S.lastI, P: S.lastP, temperature: S.lastTemp, rpm: S.lastRpm, power_factor: S.lastPf,
  };
  manualRows.push(row); liveRows.push(row);
  $("manual-rec-status").textContent = `紀錄中… ${manualRows.length} 筆`;
}
function stopManualRecord() {
  clearInterval(manualTimer); manualTimer = null;
  clearInterval(livePlotTimer); livePlotTimer = null;
  S.manualRecording = false; $("manual-record").textContent = "手動紀錄";
  $("pel-disconnect").disabled = !S.pelReady; refreshManual();
  if (!manualRows.length) { $("manual-rec-status").textContent = "已停止 (無資料)"; return status("手動紀錄停止 — 無資料"); }
  $("manual-rec-status").textContent = `已停止 — 共 ${manualRows.length} 筆`;
  saveRecord(manualRows, "手動紀錄 — 時間曲線");
}

// ═══ 即時繪圖 / 存檔 ════════════════════════════════════════════════════════
function redrawLive() {
  if (!(S.testRunning || S.manualRecording) || !liveRows.length) return;
  if (liveRows.length <= livePlotted) return;
  renderPlot($("plot"), liveRows.slice(), "自動測試 — 階段負載時間曲線 (進行中)");
  livePlotted = liveRows.length;
}
function saveRecord(rows, title) {
  renderPlot($("plot"), rows, title);
  const fields = ["timestamp", "elapsed_s", "stage", "set_current_A", "V", "I", "P", "temperature", "rpm", "power_factor"];
  const fmt = (r) => [
    r.timestamp, r.elapsed_s.toFixed(2), r.stage, r.set_current_A.toFixed(3),
    r.V.toFixed(4), r.I.toFixed(4), r.P.toFixed(4),
    r.temperature == null ? "" : r.temperature.toFixed(1),
    r.rpm == null ? "" : r.rpm.toFixed(0),
    r.power_factor == null ? "" : r.power_factor.toFixed(4),
  ].join(",");
  const csv = "﻿" + fields.join(",") + "\n" + rows.map(fmt).join("\n");
  const name = `${tag()}.csv`;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click();
  URL.revokeObjectURL(a.href);
  $("test-status").textContent = `完成：${rows.length} 筆 → ${name}`;
  status(`✔ 已下載 ${name}（曲線圖顯示於右側；瀏覽器版輸出 CSV，PNG 請用截圖）`);
}

// ═══ 設定區：動態建立三階段 + 模式切換 + 計算器 + 持久化 ═════════════════════
function buildStages() {
  const wrap = $("stage-groups");
  const defT = [30, 60, 120];
  STAGE_PERCENTS.forEach((pct, i) => {
    const g = document.createElement("div"); g.className = "stage-group";
    g.innerHTML = `
      <div class="gtitle" id="stage-title-${i}">${pct}%</div>
      <div class="frow"><label>測試時間 (秒):</label><input id="stage-time-${i}" type="number" min="1" value="${defT[i]}"></div>
      <div class="frow curr-row"><label>負載電流設定 (A):</label><input id="stage-curr-${i}" type="number" min="0" step="0.5" value="${(60 * pct / 100).toFixed(1)}"></div>
      <div class="frow rpm-row" style="display:none"><label>轉速設定 (RPM):</label><input id="stage-rpm-${i}" type="number" min="0" value="0"></div>`;
    wrap.appendChild(g);
  });
}
function onModeChanged() {
  const isRpm = $("mode-combo").value === "1";
  document.querySelectorAll(".mode1-field").forEach(e => e.style.display = isRpm ? "none" : "");
  document.querySelectorAll(".mode2-field").forEach(e => e.style.display = isRpm ? "" : "none");
  document.querySelectorAll(".curr-row").forEach(e => e.style.display = isRpm ? "none" : "flex");
  document.querySelectorAll(".rpm-row").forEach(e => e.style.display = isRpm ? "flex" : "none");
  const titles = isRpm ? ["速度一", "速度二", "速度三"] : STAGE_PERCENTS.map(p => `${p}%`);
  STAGE_PERCENTS.forEach((_, i) => $(`stage-title-${i}`).textContent = titles[i]);
  saveSettings();
}
function onMaxCurrentChanged() {
  const v = parseFloat($("max-current").value) || 0;
  STAGE_PERCENTS.forEach((p, i) => { $(`stage-curr-${i}`).value = (v * p / 100).toFixed(1); });
  saveSettings();
}
function saveSettings() {
  const o = { mode: $("mode-combo").value, maxCurrent: $("max-current").value, fixedRpm: $("fixed-rpm").value, fixedCurrent: $("fixed-current").value, maxRpmCalc: $("max-rpm-calc").value };
  STAGE_PERCENTS.forEach((_, i) => { o[`t${i}`] = $(`stage-time-${i}`).value; o[`c${i}`] = $(`stage-curr-${i}`).value; o[`r${i}`] = $(`stage-rpm-${i}`).value; });
  try { localStorage.setItem("dcsim", JSON.stringify(o)); } catch (e) {}
}
function loadSettings() {
  let o; try { o = JSON.parse(localStorage.getItem("dcsim") || "{}"); } catch (e) { o = {}; }
  if (o.maxCurrent) $("max-current").value = o.maxCurrent;
  if (o.fixedRpm) $("fixed-rpm").value = o.fixedRpm;
  if (o.fixedCurrent) $("fixed-current").value = o.fixedCurrent;
  if (o.maxRpmCalc) $("max-rpm-calc").value = o.maxRpmCalc;
  STAGE_PERCENTS.forEach((_, i) => {
    if (o[`t${i}`]) $(`stage-time-${i}`).value = o[`t${i}`];
    if (o[`c${i}`]) $(`stage-curr-${i}`).value = o[`c${i}`];
    if (o[`r${i}`]) $(`stage-rpm-${i}`).value = o[`r${i}`];
  });
  if (o.mode) $("mode-combo").value = o.mode;
}

// ═══ 事件接線 ═══════════════════════════════════════════════════════════════
function wire() {
  $("pel-connect").onclick = onPelConnect; $("pel-disconnect").onclick = onPelDisconnect;
  $("plc-connect").onclick = onPlcConnect; $("plc-disconnect").onclick = onPlcDisconnect;
  $("gpm-connect").onclick = onGpmConnect; $("gpm-disconnect").onclick = onGpmDisconnect;

  $("apply-current").onclick = () => { if (!S.pelReady) return; const a = parseFloat($("set-current").value) || 0; sim.pel.setCurrent(a); status(`已套用 CC 電流 = ${a.toFixed(1)} A`); };
  $("load-on").onclick = () => { sim.pel.loadOnCmd(); status("LOAD ON"); };
  $("load-off").onclick = () => { sim.pel.loadOffCmd(); status("LOAD OFF"); };
  $("servo-on").onclick = () => { sim.plc.setRelay("servo"); status("伺服 ON"); };
  $("servo-off").onclick = () => { sim.plc.resetRelay("servo"); status("伺服 OFF"); };
  $("gentest-on").onclick = () => { setGentest(true); status("發電機測試 ON"); };
  $("gentest-off").onclick = () => { setGentest(false); status("發電機測試 OFF"); };
  $("alarm-reset").onclick = () => { sim.plc.alarmReset(); status("異常 RESET 已送出"); };
  $("manual-enable").onclick = () => { if (!S.plcReady) return; sim.plc.toggleManualEnable(); status(`手動啟用 ${sim.plc.manualEnableCmd ? "ON" : "OFF"}`); };
  $("apply-rpm").onclick = () => { if (!S.servoOn) return status("伺服未 ON，無法設定速度"); const r = parseInt($("manual-rpm").value) || 0; sim.plc.writeRpmCommand(r); status(`速度設定 ${r} RPM 已送出`); };
  $("stop-rpm").onclick = () => { if (!S.servoOn) return; sim.plc.writeRpmCommand(0); status("速度已停止"); };

  $("mode-combo").onchange = onModeChanged;
  $("max-current").oninput = onMaxCurrentChanged;
  ["fixed-rpm", "fixed-current", "max-rpm-calc"].forEach(id => $(id).oninput = saveSettings);
  STAGE_PERCENTS.forEach((_, i) => ["stage-time-", "stage-curr-", "stage-rpm-"].forEach(p => $(p + i).oninput = saveSettings));

  $("start-test").onclick = onStartTest; $("stop-test").onclick = onStopTest;
  $("manual-record").onclick = onToggleManualRecord;

  // 模擬器控制台
  $("sim-speed-auto").onclick = () => { sim.plc.speedAuto = true; $("sim-speed-auto").classList.add("active"); $("sim-speed-manual").classList.remove("active"); };
  $("sim-speed-manual").onclick = () => { sim.plc.speedAuto = false; $("sim-speed-manual").classList.add("active"); $("sim-speed-auto").classList.remove("active"); };
  $("sim-alarm-estop").onclick = () => { sim.plc.injectAlarm(0); status("[模擬] 注入緊急停止"); };
  $("sim-alarm-drv").onclick = () => { sim.plc.injectAlarm(1); status("[模擬] 注入馬達驅動器異常"); };
  $("sim-ambient").oninput = () => { sim.plc.ambient = parseFloat($("sim-ambient").value) || 25; };

  window.addEventListener("resize", () => { if (liveRows.length) renderPlot($("plot"), liveRows.slice(), "自動測試 — 階段負載時間曲線"); });
}

// ═══ 啟動 ═══════════════════════════════════════════════════════════════════
buildStages();
loadSettings();
wire();
onModeChanged();
updateSpeedRow();
refreshManual();
