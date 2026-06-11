/* ───────────────────────────────────────────────────────────────────────────
 * simulator.js — 三台儀器的離線模擬模型
 *
 * 取代真實硬體，讓 UI 在不連接任何設備的情況下也有真實操作體驗。
 *   PELSim  : GW Instek PEL-5000C 電子負載 (V/I/P、CC、LOAD ON/OFF)
 *   PLCSim  : Keyence KV PLC (溫度/轉速/伺服/異常 + 繼電器/DM 命令)
 *   GPMSim  : GW Instek GPM-8310 功率計 (功率因數)
 *
 * 物理模型刻意簡化但合理：
 *   發電機開路電壓 ∝ 轉速；上負載後 V = Voc − I·R_internal；P = V·I。
 *   溫度隨功率上升、停機後緩降。轉速朝命令值漸近 (伺服 ON 時)。
 * ─────────────────────────────────────────────────────────────────────────── */

const RPM_FULL_SCALE = 6000;     // 輸出端滿刻度轉速 (config: plc.rpm_full_scale)
const RPM_DIVISIONS = 4000;      // PLC 類比分割數 (解析度 6000/4000 = 1.5 RPM)
const R_INTERNAL = 0.06;         // 發電機等效內阻 (Ω)，決定 V 隨 I 下垂程度
const V_AT_FULL_RPM = 58;        // 滿轉速時的開路電壓 (V)，rated 48V 約落在 5000rpm

// ── PEL-5000C 電子負載 ──────────────────────────────────────────────────────
class PELSim {
  constructor() {
    this.connected = false;
    this.loadOn = false;
    this.mode = "CC";
    this.ccCurrent = 0.5;        // CC 設定電流 (A)
    this._noise = () => (Math.random() - 0.5) * 0.02;
  }
  connect() { this.connected = true; this.loadOn = false; this.mode = "CC"; }
  disconnect() { this.loadOn = false; this.connected = false; }  // 等同送 LOAD OFF + LOCAL
  setModeCC(a) { this.mode = "CC"; this.ccCurrent = a; }
  setCurrent(a) { this.ccCurrent = a; }
  loadOnCmd() { this.loadOn = true; }
  loadOffCmd() { this.loadOn = false; }

  /** 回傳 [V, I, P]，輸入為目前發電機開路電壓 voc (由 PLC 轉速推得)。 */
  measureVip(voc) {
    if (!this.connected) return [0, 0, 0];
    if (!this.loadOn) {
      const v = Math.max(0, voc + this._noise());
      return [v, 0, 0];
    }
    // CC 模式：嘗試抽取設定電流；若發電機帶不動 (V 會降到 0) 則被夾住
    let i = this.ccCurrent;
    let v = voc - i * R_INTERNAL;
    if (v < 0) { v = 0; i = voc / R_INTERNAL; }   // 發電機極限：拉不到設定電流
    v = Math.max(0, v + this._noise());
    i = Math.max(0, i + this._noise());
    return [v, i, v * i];
  }
}

// ── Keyence KV PLC ─────────────────────────────────────────────────────────
class PLCSim {
  constructor() {
    this.connected = false;
    // 命令 (UI 寫入的繼電器 / DM)
    this.servoCmd = false;        // MR100
    this.manualEnableCmd = false; // MR102
    this.rpmCommand = 0;          // DM102 (RPM)
    // 內部狀態 (回授到 DM 監控區)
    this.rpmActual = 0;           // DM7006
    this.temperature = 25;        // DM7000
    this.ambient = 25;
    this.speedAuto = true;        // DM7002 (模擬器可切；預設自動，方便體驗自動測試)
    this.alarmCode = 0;           // DM7004 (逐 bit)
    this._lastPower = 0;          // 供溫度模型
  }
  connect() { this.connected = true; }
  disconnect() { this.connected = false; }

  setRelay(name) { if (name === "servo") this.servoCmd = true; if (name === "manual_enable") this.manualEnableCmd = true; }
  resetRelay(name) { if (name === "servo") this.servoCmd = false; if (name === "manual_enable") this.manualEnableCmd = false; }
  toggleManualEnable() { this.manualEnableCmd = !this.manualEnableCmd; }
  writeRpmCommand(rpm) { this.rpmCommand = Math.max(0, Math.min(RPM_FULL_SCALE, Math.round(rpm))); }
  alarmReset() { this.alarmCode = 0; }
  injectAlarm(bit) { this.alarmCode |= (1 << bit); }

  /** 每個模擬 tick 推進物理狀態。dt 秒。power 為負載目前消耗功率 (W)。 */
  tick(dt, power) {
    // 轉速：伺服 ON → 朝命令漸近；OFF → 緩停
    const target = this.servoCmd ? this.rpmCommand : 0;
    const tau = 0.8;  // 轉速時間常數 (秒)
    this.rpmActual += (target - this.rpmActual) * Math.min(1, dt / tau);
    if (Math.abs(this.rpmActual - target) < 1) this.rpmActual = target;
    // 溫度：朝 (環境 + 0.012·功率) 漸近，升溫慢、降溫更慢
    this._lastPower = power;
    const tTarget = this.ambient + 0.012 * power;
    const tTau = tTarget > this.temperature ? 25 : 60;
    this.temperature += (tTarget - this.temperature) * Math.min(1, dt / tTau);
  }

  /** 回傳 UI 輪詢用的監控區數值 (對應 config.plc.signals)。 */
  readMonitor() {
    return {
      temperature: Math.round(this.temperature),       // DM7000 (整數)
      rpm_percent: Math.round(this.rpmActual / RPM_FULL_SCALE * 100),  // DM7001
      mode: this.speedAuto ? 1 : 0,                     // DM7002
      servo: this.servoCmd ? 1 : 0,                     // DM7003
      alarm: this.alarmCode !== 0 ? 1 : 0,              // DM7004 (非0=異常)
      alarm_code: this.alarmCode & 0xffff,              // DM7004 逐 bit
      manual_enable: this.manualEnableCmd ? 1 : 0,      // DM7005
      rpm: Math.round(this.rpmActual),                  // DM7006
    };
  }
  voc() { return this.rpmActual / RPM_FULL_SCALE * V_AT_FULL_RPM; }  // 開路電壓
}

// ── GPM-8310 功率計 ─────────────────────────────────────────────────────────
class GPMSim {
  constructor() { this.connected = false; }
  connect() { this.connected = true; }
  disconnect() { this.connected = false; }
  /** DC 系統功率因數 ≈ 1，帶極小雜訊。 */
  readPF() { return 0.999 + (Math.random() - 0.5) * 0.002; }
}

const ALARM_BITS = { 0: "緊急停止", 1: "馬達驅動器異常" };
