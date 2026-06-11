"""
PEL-5000C 手動控制 UI (PyQt6) — 最小版本

功能:
  - 連線 / 中斷
  - 即時監視 V / I / P (背景 5 Hz 輪詢)
  - 設定 CC 電流 + 變更按鈕
  - LOAD ON / LOAD OFF

執行:
  python ui/main_window.py
"""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QSettings
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QDoubleSpinBox, QSpinBox, QStatusBar, QFormLayout, QSizePolicy,
    QAbstractSpinBox, QFileDialog, QComboBox, QMessageBox,
)

from pel5000c import PEL5000C, PELError
from keyence import KeyenceKV, KVError
from gpm8310 import GPM8310, GPMError, GPMCommandError
from utils import load_config, CSVLogger, plot_auto_test, render_auto_test_png
from utils.logger import timestamp_tag


def _dm_number(device: str) -> int:
    """取出裝置位址的數字部分 (例: 'DM7000' -> 7000)。"""
    digits = "".join(ch for ch in device if ch.isdigit())
    if not digits:
        raise ValueError(f"無法解析裝置位址: {device!r}")
    return int(digits)


class PollingThread(QThread):
    """背景輪詢 V/I/P (每 cycle) 與 LOAD/MODE (每 N cycles)。"""

    measured = pyqtSignal(float, float, float)
    state_updated = pyqtSignal(bool, str)  # load_on, mode
    failed = pyqtSignal(str)

    def __init__(
        self,
        load: PEL5000C,
        lock: threading.Lock,
        hz: float = 5.0,
        state_every: int = 5,
    ) -> None:
        super().__init__()
        self.load = load
        self.lock = lock
        self.interval_ms = int(1000.0 / hz)
        self.state_every = state_every
        self._running = False
        self._force_state = True  # 一上線就強制查一次

    def request_state_refresh(self) -> None:
        """讓下一輪 poll 立即同時查 LOAD/MODE (用於使用者剛按完按鈕)。"""
        self._force_state = True

    def run(self) -> None:
        self._running = True
        counter = 0
        while self._running:
            do_state = self._force_state or (counter % self.state_every == 0)
            try:
                with self.lock:
                    v, i, p = self.load.measure_vip()
                    if do_state:
                        load_on = self.load.is_load_on()
                        mode = self.load.get_mode()
                        self._force_state = False
                self.measured.emit(v, i, p)
                if do_state:
                    self.state_updated.emit(load_on, mode)
            except PELError as e:
                self.failed.emit(str(e))
                break
            except Exception as e:  # 防止意外例外讓 thread 靜默死亡
                self.failed.emit(f"未預期例外: {e}")
                break
            counter += 1
            self.msleep(self.interval_ms)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)


class PLCPollingThread(QThread):
    """獨立的 Keyence KV PLC 上位鏈路輪詢執行緒。

    一次讀回集中監控區 (DM7000 起 N 個 word)，依 signals 對應表轉成數值。
    溫度、轉速等都由 PLC 整合到此區。與 PEL-5000C 各自獨立 (不共用 PEL 的
    socket)，但與「伺服 ON/OFF」按鈕共用同一條 PLC socket，故用 lock 序列化。
    """

    measured = pyqtSignal(object)  # dict{name: float}
    failed = pyqtSignal(str)

    def __init__(
        self,
        driver: KeyenceKV,
        lock: threading.Lock,
        start: str,
        count: int,
        signals: dict,
        interval_sec: float = 0.2,
        heartbeat_device: str | None = None,
        heartbeat_interval_sec: float = 1.0,
    ) -> None:
        super().__init__()
        self.driver = driver
        self.lock = lock
        self.start_device = start   # 不可叫 self.start — 會蓋掉 QThread.start() 方法
        self.count = count
        self.signals = signals  # {name: {"offset", "scale", "signed"}}
        self.interval_ms = int(interval_sec * 1000)
        self.heartbeat_device = heartbeat_device   # None=不送心跳；否則定期寫遞增計數
        self._hb_interval_ms = max(self.interval_ms, int(heartbeat_interval_sec * 1000))
        self._hb_accum_ms = self._hb_interval_ms   # 初值=門檻 → 第一輪即送第一次心跳
        self._hb = 0
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                with self.lock:
                    words = self.driver.read_words(self.start_device, self.count)
                    # PC→PLC 心跳：每滿 heartbeat 間隔 (預設 1s) 寫一次遞增計數 (0~32767 循環)，
                    # 與輪詢頻率 (200ms) 解耦。PLC 監看其變化判斷上位機存活；輪詢一停心跳即停。
                    if self.heartbeat_device:
                        self._hb_accum_ms += self.interval_ms
                        if self._hb_accum_ms >= self._hb_interval_ms:
                            self._hb_accum_ms = 0
                            self._hb = (self._hb + 1) % 32768
                            self.driver.write_word(self.heartbeat_device, self._hb)
                out = {}
                for name, spec in self.signals.items():
                    raw = words[spec["offset"]]
                    if spec.get("signed") and raw >= 0x8000:
                        raw -= 0x10000
                    out[name] = raw / spec.get("scale", 1.0)
                self.measured.emit(out)
            except KVError as e:
                self.failed.emit(str(e))
                break
            except Exception as e:
                self.failed.emit(f"PLC 輪詢未預期例外: {e}")
                break
            self.msleep(self.interval_ms)

    def stop(self) -> None:
        self._running = False
        self.wait(3000)


class GPMPollingThread(QThread):
    """獨立的 GPM-8310 功率計輪詢執行緒，週期讀回功率因數 (PF)。

    與 PEL-5000C / PLC 各自獨立 (不共用 socket)。只有此執行緒會碰 GPM 的
    socket，但仍透過 lock 序列化，保留未來加入手動指令的彈性。
    """

    measured = pyqtSignal(float)   # power factor
    unavailable = pyqtSignal(str)  # 資料層級 (超量程/無資料)：只顯示，不斷線
    failed = pyqtSignal(str)       # 連線層級：致命，需斷線

    def __init__(
        self,
        driver: GPM8310,
        lock: threading.Lock,
        interval_sec: float = 1.0,
    ) -> None:
        super().__init__()
        self.driver = driver
        self.lock = lock
        self.interval_ms = int(interval_sec * 1000)
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                with self.lock:
                    pf = self.driver.read_power_factor()
                self.measured.emit(pf)
            except GPMCommandError as e:
                # 超量程 (INF) / 無資料 (NAN) 等資料層級問題 — 連線仍正常，
                # 只更新顯示後繼續輪詢，不要因為一筆讀值就斷線。
                self.unavailable.emit(str(e))
            except GPMError as e:
                # 連線/逾時等致命錯誤 — 中止輪詢並斷線
                self.failed.emit(str(e))
                break
            except Exception as e:
                self.failed.emit(f"功率計輪詢未預期例外: {e}")
                break
            self.msleep(self.interval_ms)

    def stop(self) -> None:
        self._running = False
        self.wait(3000)


class AutoTestThread(QThread):
    """自動測試序列：依各階段套用設定 (負載電流 或 轉速) 並持續取樣。

    支援兩種模式 (mode)：
      "current" 定轉速、變負載 — 逐階段改寫 PEL 的 CC 電流 (RPM 由主執行緒寫一次)。
      "rpm"     定負載、變轉速 — CC 電流設一次固定，逐階段改寫 PLC 轉速 (DM102)。

    PEL 永遠走 io_lock 序列化；轉速寫入透過 apply_stage_value callback (內部走 plc_lock，
    無 GUI 呼叫，回傳錯誤字串)。溫度/轉速/PF 取自 PLC/GPM 快取 (get_aux)，不另搶 socket。
    """

    progress = pyqtSignal(str)
    sampled = pyqtSignal(int)            # 已取樣筆數
    sample_row = pyqtSignal(object)      # 最新一筆 row (供即時繪圖)
    finished_ok = pyqtSignal(object)     # rows: list[dict]
    failed = pyqtSignal(str)

    def __init__(
        self,
        load: PEL5000C,
        io_lock: threading.Lock,
        stages,                          # [(label, setpoint, duration_s), ...]
        sample_interval: float,
        v_max: float,
        i_max: float,
        get_aux,                         # callable -> (temperature, rpm, power_factor)
        mode: str = "current",           # "current" (變負載) / "rpm" (變轉速)
        fixed_current: float | None = None,  # mode=="rpm" 時的固定 CC 電流
        apply_stage_value=None,          # mode=="rpm" 時逐階段寫轉速: callable(rpm)->Optional[str]
    ) -> None:
        super().__init__()
        self.load = load
        self.io_lock = io_lock
        self.stages = stages
        self.sample_interval = sample_interval
        self.v_max = v_max
        self.i_max = i_max
        self.get_aux = get_aux
        self.mode = mode
        self.fixed_current = fixed_current
        self.apply_stage_value = apply_stage_value
        self._running = True

    def run(self) -> None:
        rows: list = []
        try:
            # 初始：設 CC 模式並把固定電流先設好 (rpm 模式用 fixed_current，
            # current 模式沿用第一階段電流)，再 LOAD ON。
            initial_current = (
                self.fixed_current if self.mode == "rpm"
                else self.stages[0][1]
            )
            with self.io_lock:
                self.load.set_mode("CC")
                self.load.write(f"CC:HIGH {float(initial_current):.4f}")
                self.load.load_on()
            t_start = time.monotonic()

            for (label, setpoint, duration) in self.stages:
                if not self._running:
                    break
                if self.mode == "rpm":
                    # 逐階段改寫轉速 (走 plc_lock；失敗非致命，僅回報後續跑)
                    err = None
                    if self.apply_stage_value is not None:
                        err = self.apply_stage_value(setpoint)
                    if err:
                        self.progress.emit(f"[轉速命令] {err}")
                    self.progress.emit(
                        f"{label}：{setpoint:.0f} RPM × {int(duration)} s"
                    )
                else:
                    # 逐階段改寫 CC 電流
                    with self.io_lock:
                        self.load.write(f"CC:HIGH {float(setpoint):.4f}")
                    self.progress.emit(
                        f"{label}：{setpoint:.2f} A × {int(duration)} s"
                    )
                stage_t0 = time.monotonic()
                while self._running and (time.monotonic() - stage_t0) < duration:
                    with self.io_lock:
                        v, i, p = self.load.measure_vip()
                        # 超限會先 LOAD OFF 再丟 PELSafetyError (兩模式皆保留)
                        self.load.assert_within(
                            v=v, i=i, v_max=self.v_max, i_max=self.i_max
                        )
                    temp, rpm, pf = self.get_aux()
                    rows.append({
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "elapsed_s": time.monotonic() - t_start,
                        "stage": label,
                        # 固定/變動的設定值都記負載電流 (rpm 模式為 fixed_current)
                        "set_current_A": float(
                            self.fixed_current if self.mode == "rpm" else setpoint
                        ),
                        "V": v, "I": i, "P": p,
                        "temperature": temp,
                        "rpm": rpm,
                        "power_factor": pf,
                    })
                    self.sampled.emit(len(rows))
                    self.sample_row.emit(rows[-1])
                    time.sleep(self.sample_interval)

            with self.io_lock:
                self.load.load_off()
        except PELError as e:
            self._safe_load_off()
            self.failed.emit(str(e))
            return
        except Exception as e:
            self._safe_load_off()
            self.failed.emit(f"未預期例外: {e}")
            return
        self.finished_ok.emit(rows)

    def _safe_load_off(self) -> None:
        try:
            with self.io_lock:
                self.load.load_off()
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DC Generator Testing System")
        self.resize(1300, 800)

        self.cfg = load_config()
        self.load: PEL5000C | None = None
        self.poller: PollingThread | None = None
        self.io_lock = threading.Lock()  # 序列化 socket 存取 (poller vs 使用者按鈕)

        # PLC 集中監控 (Keyence KV 上位鏈路) — 與 PEL-5000C 各自獨立
        # 溫度、轉速等由 PLC 整合到 DM 監控區，UI 只讀 PLC
        self.plc_driver: KeyenceKV | None = None
        self.plc_poller: PLCPollingThread | None = None
        self.plc_lock = threading.Lock()  # 序列化 PLC socket (poller vs 伺服按鈕)
        self.servo_relay = "MR100"        # 伺服 ON/OFF 繼電器 (於 on_plc_connect 由 config 覆寫)
        self.test_relay = "MR101"         # 自動測試進行中繼電器 (開始 ON / 結束 OFF，config 覆寫)
        self.manual_enable_relay = "MR102"  # 手動啟用繼電器 (ST/RS；狀態反映到 DM7005，config 覆寫)
        self.generator_test_relay = "MR103" # 發電機測試 ON/OFF 繼電器 (ST/RS，config 覆寫)
        self._manual_enable_on = False    # 手動啟用 (MR102/DM7005) 目前狀態，控制手動控制框背景色
        self.rpm_command_device = "DM102" # 轉速命令寫入點 (直接存 RPM 整數，config 覆寫)
        # DM102 直接寫 RPM (輸出制)；rpm_full_scale = 輸出端滿刻度 (轉速表讀值，6000)，
        # 非馬達轉速 (2000)。PLC 端把 RPM 換算成 rpm_divisions(4000) 分割類比。詳見 config.yaml。
        _plc_cfg = self.cfg.get("plc") or {}
        self.rpm_full_scale = float(_plc_cfg.get("rpm_full_scale", 6000.0))
        self.rpm_divisions = float(_plc_cfg.get("rpm_divisions", 4000.0))   # 類比分割數 (決定解析度)
        self.alarm_reset_relay = "MR502"  # 異常 RESET 繼電器 (按下直接 ST=ON，config 覆寫)
        # 異常碼 (DM7010) 各 bit → 訊息對照 (config 覆寫)
        self.alarm_bit_labels = {0: "緊急停止", 1: "馬達驅動器異常"}

        # 功率計 GPM-8310 (LAN) — 與 PEL-5000C / PLC 各自獨立，只讀功率因數 (PF)
        self.gpm_driver: GPM8310 | None = None
        self.gpm_poller: GPMPollingThread | None = None
        self.gpm_lock = threading.Lock()
        self._gpm_ready = False
        self._last_pf = None              # GPM 最新功率因數 (供自動測試取樣快取)

        # DM7002 = 速度控制來源：True=自動控制速度 (由 UI 速度設定送 DM102)，False=手動控制速度。
        # 已與手動按鈕鎖定脫鉤；只用來決定手動控制區「速度設定」列是否顯示。
        self._speed_auto = False          # True=自動控制速度 (DM7002=1)
        self._pel_ready = False           # 負載機已連線
        self._plc_ready = False           # PLC 已連線
        self._servo_on = False            # 伺服馬達 ON (DM7003=1)，供自動測試前置條件提醒
        self._alarm_active = False        # 異常發生中 (DM7004≠0)，供前置條件提醒

        # 自動測試
        self._test_running = False
        # 紀錄 (CSV + 曲線圖) 存檔路徑，預設取自 config.output.save_dir (UI 可改)
        _out = self.cfg.get("output") or {}
        self.save_dir = Path(_out.get("save_dir") or _out.get("data_dir") or "data")
        self.auto_test_thread: AutoTestThread | None = None
        self._last_temp = None            # PLC 最新溫度 (供自動測試取樣快取)
        self._last_rpm = None             # PLC 最新轉速
        self._last_v = None               # PEL 最新 V/I/P (供手動紀錄取樣快取)
        self._last_i = None
        self._last_p = None
        self._test_pixmap: QPixmap | None = None
        # 自動測試 / 手動紀錄 即時繪圖 (兩者互斥，共用同一組累積/重繪)
        self._live_rows: list = []        # 累積的取樣 (供即時重繪)
        self._live_plotted_count = 0      # 上次重繪時的筆數 (節流用)
        self._live_plot_timer: QTimer | None = None
        # 自動測試計時 (秒數顯示)
        self._test_elapsed_timer: QTimer | None = None
        self._test_t0 = 0.0               # 測試起始 time.monotonic()
        self._test_total_s = 0            # 預估總秒數 (各階段時間總和)
        # 手動紀錄 (與自動流程無關的被動記錄器)
        self._manual_recording = False
        self._manual_rows: list = []
        self._manual_t0 = None
        self._manual_timer: QTimer | None = None

        # 使用者設定持久化 (存 Windows 登錄，不動 config.yaml)：最大電流 / RPM / 存檔路徑
        self.settings = QSettings("ChingTech", "DCGeneratorTester")

        self._build_ui()
        self._load_saved_settings()   # 套用上次關閉前的設定 (在 UI 建好後)
        # 若 config 設了 plc.enabled，啟動時自動連 PLC（之後仍可用按鈕手動連/斷）
        if (self.cfg.get("plc") or {}).get("enabled", False):
            self.on_plc_connect()
        # 同樣地，power_meter.enabled 時自動連功率計
        if (self.cfg.get("power_meter") or {}).get("enabled", False):
            self.on_gpm_connect()

    # ---------- UI 建立 ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ─ 標題列：最左公司 LOGO，後接標題文字 ─
        header = QHBoxLayout()
        logo = QLabel()
        # 打包成 exe 後，assets 被收進 bundle (sys._MEIPASS)；開發時則在原始碼旁。
        if getattr(sys, "frozen", False):
            asset_base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        else:
            asset_base = Path(__file__).resolve().parent
        logo_path = asset_base / "assets" / "logo.png"
        pix = QPixmap(str(logo_path))
        if not pix.isNull():
            # 高度固定 48px、等比縮放
            logo.setPixmap(pix.scaledToHeight(48, Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(logo)
        title = QLabel("DC 300A直流充電馬達測試器")
        title_font = QFont(); title_font.setPointSize(18); title_font.setBold(True)
        title.setFont(title_font)
        header.addSpacing(12)
        header.addWidget(title)
        header.addStretch()
        root.addLayout(header)

        # 主體左右分欄
        body = QHBoxLayout()
        body.setSpacing(10)

        # ─ 左欄：PLC連線 + 即時監控 + 手動控制 + 設定 ─
        # 寬度依內容收窄，分界線落在「負載機連線」起始 (左緣)
        left = QVBoxLayout()
        left.setSpacing(10)

        # PLC 連線框直接加入左欄 → 撐滿欄寬，與下方即時監控框同寬
        left.addWidget(self._build_plc_connection_box())

        left.addWidget(self._build_measurement_box(), 2)
        left.addLayout(self._build_manual_enable_row())   # 手動啟用閘門 (手動控制框外，上方)
        left.addWidget(self._build_control_box(), 2)
        left.addWidget(self._build_settings_box(), 2)

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        # Maximum + stretch 0：左欄不搶橫向空間，依內容收到最窄
        left_wrap.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        body.addWidget(left_wrap, 0)

        # ─ 右欄：負載機連線 + 功率計連線 + 自動測試 (吃掉剩餘寬度) ─
        right = QVBoxLayout()
        right.setSpacing(10)

        conn_row = QHBoxLayout()
        conn_row.setContentsMargins(0, 0, 0, 0)
        conn_row.addWidget(self._build_pel_connection_box())
        conn_row.addWidget(self._build_gpm_connection_box())
        conn_row.addStretch()
        right.addLayout(conn_row)

        right.addWidget(self._build_auto_test_box(), 1)

        right_wrap = QWidget()
        right_wrap.setLayout(right)
        body.addWidget(right_wrap, 1)   # 右欄佔滿剩餘空間

        root.addLayout(body, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("尚未連線")

        self._refresh_manual_enabled()

    def _build_plc_connection_box(self) -> QGroupBox:
        box = QGroupBox("PLC 連線")
        h = QHBoxLayout(box)
        pcfg = self.cfg.get("plc") or {}

        h.addWidget(QLabel("IP:"))
        self.plc_ip_edit = QLineEdit(str(pcfg.get("ip", "192.168.0.10")))
        self.plc_ip_edit.setFixedWidth(140)
        h.addWidget(self.plc_ip_edit)

        h.addWidget(QLabel("Port:"))
        self.plc_port_edit = QLineEdit(str(pcfg.get("port", 8500)))
        self.plc_port_edit.setFixedWidth(60)
        h.addWidget(self.plc_port_edit)

        self.btn_plc_connect = QPushButton("連線")
        self.btn_plc_connect.clicked.connect(self.on_plc_connect)
        h.addWidget(self.btn_plc_connect)

        self.btn_plc_disconnect = QPushButton("中斷")
        self.btn_plc_disconnect.clicked.connect(self.on_plc_disconnect)
        self.btn_plc_disconnect.setEnabled(False)
        h.addWidget(self.btn_plc_disconnect)

        self.lbl_plc_conn_status = QLabel("● 未連線")
        self.lbl_plc_conn_status.setStyleSheet("color: #d62728; font-weight: bold;")
        h.addWidget(self.lbl_plc_conn_status)
        return box

    def _build_pel_connection_box(self) -> QGroupBox:
        box = QGroupBox("負載機連線")
        h = QHBoxLayout(box)

        h.addWidget(QLabel("IP:"))
        self.ip_edit = QLineEdit(self.cfg["instrument"]["ip"])
        self.ip_edit.setFixedWidth(140)
        h.addWidget(self.ip_edit)

        h.addWidget(QLabel("Port:"))
        self.port_edit = QLineEdit(str(self.cfg["instrument"]["port"]))
        self.port_edit.setFixedWidth(60)
        h.addWidget(self.port_edit)

        self.btn_connect = QPushButton("連線")
        self.btn_connect.clicked.connect(self.on_connect)
        h.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("中斷")
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_disconnect.setEnabled(False)
        h.addWidget(self.btn_disconnect)

        self.lbl_conn_status = QLabel("● 未連線")
        self.lbl_conn_status.setStyleSheet("color: #d62728; font-weight: bold;")
        h.addWidget(self.lbl_conn_status)
        return box

    def _build_gpm_connection_box(self) -> QGroupBox:
        box = QGroupBox("功率計連線")
        h = QHBoxLayout(box)
        gcfg = self.cfg.get("power_meter") or {}

        h.addWidget(QLabel("IP:"))
        self.gpm_ip_edit = QLineEdit(str(gcfg.get("ip", "192.168.0.100")))
        self.gpm_ip_edit.setFixedWidth(140)
        h.addWidget(self.gpm_ip_edit)

        h.addWidget(QLabel("Port:"))
        self.gpm_port_edit = QLineEdit(str(gcfg.get("port", 23)))
        self.gpm_port_edit.setFixedWidth(60)
        h.addWidget(self.gpm_port_edit)

        self.btn_gpm_connect = QPushButton("連線")
        self.btn_gpm_connect.clicked.connect(self.on_gpm_connect)
        h.addWidget(self.btn_gpm_connect)

        self.btn_gpm_disconnect = QPushButton("中斷")
        self.btn_gpm_disconnect.clicked.connect(self.on_gpm_disconnect)
        self.btn_gpm_disconnect.setEnabled(False)
        h.addWidget(self.btn_gpm_disconnect)

        self.lbl_gpm_conn_status = QLabel("● 未連線")
        self.lbl_gpm_conn_status.setStyleSheet("color: #d62728; font-weight: bold;")
        h.addWidget(self.lbl_gpm_conn_status)
        return box

    def _build_measurement_box(self) -> QGroupBox:
        box = QGroupBox("即時監視 (5 Hz)")
        outer = QVBoxLayout(box)

        # ─ 狀態列：燈號與文字放大一倍 (原 8 → 16)；分兩行 ─
        status_font = QFont()
        status_font.setPointSize(16)   # 燈號/文字大一倍 (原 8)
        status_font.setBold(True)

        # 第 1 行：負載狀態 / 模式
        status_row1 = QHBoxLayout()
        lbl_load_title = QLabel("負載狀態:")
        lbl_load_title.setFont(status_font)
        status_row1.addWidget(lbl_load_title)

        self.lbl_load_state = QLabel("● ---")
        self.lbl_load_state.setFont(status_font)
        self.lbl_load_state.setStyleSheet("color: gray;")
        self.lbl_load_state.setFixedWidth(220)
        status_row1.addWidget(self.lbl_load_state)

        status_row1.addSpacing(30)

        lbl_mode_title = QLabel("模式:")
        lbl_mode_title.setFont(status_font)
        status_row1.addWidget(lbl_mode_title)

        self.lbl_mode = QLabel("---")
        self.lbl_mode.setFont(status_font)
        self.lbl_mode.setStyleSheet("color: gray;")
        status_row1.addWidget(self.lbl_mode)

        status_row1.addSpacing(30)

        lbl_gentest_title = QLabel("發電機測試開關:")
        lbl_gentest_title.setFont(status_font)
        status_row1.addWidget(lbl_gentest_title)

        self.lbl_gentest_state = QLabel("● ---")
        self.lbl_gentest_state.setFont(status_font)
        self.lbl_gentest_state.setStyleSheet("color: gray;")
        self.lbl_gentest_state.setFixedWidth(120)
        status_row1.addWidget(self.lbl_gentest_state)

        status_row1.addStretch()
        outer.addLayout(status_row1)

        # 第 2 行：伺服 / 異常 (含異常訊息)
        status_row2 = QHBoxLayout()
        lbl_servo_title = QLabel("伺服:")
        lbl_servo_title.setFont(status_font)
        status_row2.addWidget(lbl_servo_title)

        self.lbl_servo_state = QLabel("● ---")
        self.lbl_servo_state.setFont(status_font)
        self.lbl_servo_state.setStyleSheet("color: gray;")
        self.lbl_servo_state.setFixedWidth(180)
        status_row2.addWidget(self.lbl_servo_state)

        status_row2.addSpacing(30)

        lbl_alarm_title = QLabel("異常:")
        lbl_alarm_title.setFont(status_font)
        status_row2.addWidget(lbl_alarm_title)

        self.lbl_alarm_state = QLabel("● ---")
        self.lbl_alarm_state.setFont(status_font)
        self.lbl_alarm_state.setStyleSheet("color: gray;")
        self.lbl_alarm_state.setFixedWidth(180)
        status_row2.addWidget(self.lbl_alarm_state)

        # 異常訊息 (DM7010 逐 bit 解碼) — 緊接在「異常」狀態右側
        self.lbl_alarm_msg = QLabel("---")
        self.lbl_alarm_msg.setFont(status_font)
        self.lbl_alarm_msg.setStyleSheet("color: gray;")
        status_row2.addWidget(self.lbl_alarm_msg)

        status_row2.addStretch()
        outer.addLayout(status_row2)

        # ─ 異常 RESET (PLC 繼電器 MR502)：放在「異常」狀態下一行 ─
        alarm_btn_row = QHBoxLayout()
        self.btn_alarm_reset = QPushButton("異常 RESET")
        self.btn_alarm_reset.setFixedSize(140, 36)
        self.btn_alarm_reset.setStyleSheet(
            "QPushButton { background:#ff7f0e; color:white; font-size:14px; "
            "font-weight:bold; border-radius:6px; }"
            "QPushButton:disabled { background:#aaa; color:#eee; }"
            "QPushButton:hover:!disabled { background:#ff9326; }"
        )
        self.btn_alarm_reset.clicked.connect(self.on_alarm_reset)
        self.btn_alarm_reset.setEnabled(False)
        alarm_btn_row.addWidget(self.btn_alarm_reset)
        alarm_btn_row.addStretch()
        outer.addLayout(alarm_btn_row)

        # ─ 量測數值區：六項分兩欄 (每欄三項直排)、字體放大讓使用者看得更清楚 ─
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)

        big_font = QFont()
        big_font.setPointSize(28)   # 放大數值字體 (原 20)
        big_font.setBold(True)
        big_font.setFamily("Consolas")
        unit_font = QFont()
        unit_font.setPointSize(15)  # 原 11
        label_font = QFont()
        label_font.setPointSize(20) # 放大項目名稱 (原 16)
        label_font.setBold(True)

        def add_item(row: int, col: int, name: str, color: str, unit: str) -> QLabel:
            """在 (row, col) 起放一組「名稱 + 數值 + 單位」(佔 col、col+1、col+2 三欄)。"""
            lbl_name = QLabel(name)
            lbl_name.setFont(label_font)
            grid.addWidget(lbl_name, row, col + 0)

            lbl_value = QLabel("---")
            lbl_value.setFont(big_font)
            lbl_value.setStyleSheet(f"color: {color};")
            # 固定寬度 + 右對齊：數字靠標題且各列個位數對齊，單位緊跟其後
            lbl_value.setFixedWidth(140)
            lbl_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lbl_value, row, col + 1)

            lbl_unit = QLabel(unit)
            lbl_unit.setFont(unit_font)
            grid.addWidget(lbl_unit, row, col + 2)
            return lbl_value

        # 左欄 (col 0~2)：電壓 / 電流 / 功率
        self.lbl_v = add_item(0, 0, "電壓(DC)", "#1f77b4", "V")
        self.lbl_i = add_item(1, 0, "電流(DC)", "#d62728", "A")
        self.lbl_p = add_item(2, 0, "功率", "#2ca02c", "W")
        # 右欄 (col 4~6)：溫度 / 轉速 / 功率因數
        self.lbl_t = add_item(0, 4, "溫度", "#ff7f0e", "°C")
        # 馬達轉速 — 主值 RPM (DM7006)，旁邊括弧顯示 % (DM7001)
        self.lbl_rpm = add_item(1, 4, "轉速", "#9467bd", "RPM")
        self.lbl_rpm_pct = QLabel("(--%)")
        self.lbl_rpm_pct.setFont(unit_font)
        grid.addWidget(self.lbl_rpm_pct, 1, 7)
        # 功率因數 — GPM-8310 功率計 (LAN)，DC 系統下通常 ≈ 1
        self.lbl_pf = add_item(2, 4, "功率因數", "#8c564b", "PF")

        grid.setColumnMinimumWidth(3, 28)   # 兩欄之間留間距
        grid.setColumnStretch(8, 1)         # 拉伸尾欄 → 兩組整體靠左
        outer.addLayout(grid)
        return box

    def update_temperature(self, temp_c) -> None:
        """供 TempPollingThread / 外部呼叫；None 表示讀失敗或尚未連線。"""
        if temp_c is None:
            self.lbl_t.setText("---")
        else:
            self.lbl_t.setText(f"{float(temp_c):8.0f}")

    # ---------- PLC 連線 / 中斷 + 集中監控 (Keyence KV 上位鏈路) ----------
    def on_plc_connect(self) -> None:
        """連線 PLC 並開始輪詢 DM 監控區；失敗只在狀態列提示，不影響負載機。"""
        if self.plc_driver is not None:
            return
        pcfg = self.cfg.get("plc") or {}
        try:
            port = int(self.plc_port_edit.text().strip())
        except ValueError:
            self.status.showMessage("PLC Port 必須是整數", 4000)
            return

        try:
            self.plc_driver = KeyenceKV(
                ip=self.plc_ip_edit.text().strip(),
                port=port,
                timeout=float(pcfg.get("timeout", 2.0)),
                command_delay=float(pcfg.get("command_delay", 0.0)),
                read_buffer=int(pcfg.get("read_buffer", 4096)),
            )
            self.plc_driver.connect()
        except KVError as e:
            self.status.showMessage(f"PLC 連線失敗: {e}", 6000)
            self.plc_driver = None
            return

        # 伺服 ON/OFF 用的繼電器 (可由 config 覆寫，預設 MR100)
        self.servo_relay = str((pcfg.get("control") or {}).get("servo", "MR100"))
        # 自動測試進行中繼電器 (開始 ON / 結束 OFF，可由 config 覆寫，預設 MR101)
        self.test_relay = str((pcfg.get("control") or {}).get("test", "MR101"))
        # 手動啟用繼電器 (狀態反映到 DM7005，可由 config 覆寫，預設 MR102)
        self.manual_enable_relay = str((pcfg.get("control") or {}).get("manual_enable", "MR102"))
        # 發電機測試 ON/OFF 繼電器 (可由 config 覆寫，預設 MR103)
        self.generator_test_relay = str((pcfg.get("control") or {}).get("generator_test", "MR103"))
        # 轉速命令寫入點 (直接存 RPM 整數，可由 config 覆寫，預設 DM102)
        self.rpm_command_device = str((pcfg.get("control") or {}).get("rpm_command", "DM102"))
        self.rpm_full_scale = float(pcfg.get("rpm_full_scale", self.rpm_full_scale))
        self.rpm_divisions = float(pcfg.get("rpm_divisions", self.rpm_divisions))
        # 異常 RESET 繼電器 (可由 config 覆寫，預設 MR502)
        self.alarm_reset_relay = str((pcfg.get("control") or {}).get("alarm_reset", "MR502"))
        # 異常碼 (DM7010) bit → 訊息對照 (可由 config 覆寫；key 容許字串或整數)
        raw_bits = pcfg.get("alarm_bits")
        if raw_bits:
            self.alarm_bit_labels = {int(k): str(v) for k, v in raw_bits.items()}

        mon = pcfg.get("monitor") or {}
        start = mon.get("start", "DM7000")
        count = int(mon.get("count", 20))
        try:
            start_num = _dm_number(start)
        except ValueError as e:
            self.status.showMessage(f"PLC 監控區設定錯誤: {e}", 8000)
            self._stop_plc_monitor()
            return

        # 解析 signals → 計算相對監控區起始的 offset
        signals: dict = {}
        for name, spec in (pcfg.get("signals") or {}).items():
            dev = (spec or {}).get("device")
            if not dev:
                continue
            try:
                offset = _dm_number(dev) - start_num
            except ValueError:
                continue
            if not (0 <= offset < count):
                self.status.showMessage(
                    f"PLC 訊號 {name} ({dev}) 超出監控區 {start}+{count}", 6000
                )
                continue
            signals[name] = {
                "offset": offset,
                "scale": float(spec.get("scale", 1.0)),
                "signed": bool(spec.get("signed", False)),
            }

        # 連線成功 — 更新狀態 UI
        self.lbl_plc_conn_status.setText("● 已連線")
        self.lbl_plc_conn_status.setStyleSheet("color: green; font-weight: bold;")
        self.btn_plc_connect.setEnabled(False)
        self.btn_plc_disconnect.setEnabled(True)
        self.plc_ip_edit.setEnabled(False)
        self.plc_port_edit.setEnabled(False)
        self._plc_ready = True
        self._refresh_manual_enabled()

        # PC→PLC 心跳設定 (enabled 時每輪把遞增計數寫入 device，預設 DM130)
        hb_cfg = pcfg.get("heartbeat") or {}
        hb_device = (
            str(hb_cfg.get("device", "DM130"))
            if hb_cfg.get("enabled", False) else None
        )
        self.plc_poller = PLCPollingThread(
            self.plc_driver, self.plc_lock, start, count, signals,
            interval_sec=float(pcfg.get("poll_interval_sec", 0.2)),
            heartbeat_device=hb_device,
            heartbeat_interval_sec=float(hb_cfg.get("interval_sec", 1.0)),
        )
        self.plc_poller.measured.connect(self.on_plc_measured)
        self.plc_poller.failed.connect(self.on_plc_poller_failed)
        self.plc_poller.start()
        self.status.showMessage(
            f"PLC 已連線: {self.plc_ip_edit.text().strip()}:{port}", 4000
        )

    def on_plc_disconnect(self) -> None:
        self._stop_plc_monitor()
        self.status.showMessage("PLC 已中斷連線", 3000)

    def _stop_plc_monitor(self) -> None:
        if self.plc_poller is not None:
            self.plc_poller.stop()
            self.plc_poller = None
        if self.plc_driver is not None:
            try:
                self.plc_driver.disconnect()
            except Exception:
                pass
            self.plc_driver = None
        # PLC 斷線 → 速度控制狀態歸手動 (DM7002 已無法讀取)，清掉溫度/轉速快取
        self._plc_ready = False
        self._speed_auto = False
        self._servo_on = False
        self._alarm_active = False
        self._update_speed_control_visibility()   # 斷線 → 隱藏速度設定列
        self._last_temp = None
        self._last_rpm = None
        self._set_manual_enable(False)   # 斷線 → 手動控制框背景回暗灰
        self._refresh_manual_enabled()
        # 重置 PLC 連線狀態 UI
        if hasattr(self, "lbl_plc_conn_status"):
            self.lbl_plc_conn_status.setText("● 未連線")
            self.lbl_plc_conn_status.setStyleSheet("color: #d62728; font-weight: bold;")
            self.btn_plc_connect.setEnabled(True)
            self.btn_plc_disconnect.setEnabled(False)
            self.plc_ip_edit.setEnabled(True)
            self.plc_port_edit.setEnabled(True)
            self.update_temperature(None)
            self.lbl_rpm.setText("---")
            self.lbl_rpm_pct.setText("(--%)")
            self.lbl_servo_state.setText("● ---")
            self.lbl_servo_state.setStyleSheet("color: gray;")
            self._set_gentest_state(None)
            self.lbl_alarm_state.setText("● ---")
            self.lbl_alarm_state.setStyleSheet("color: gray;")
            self.lbl_alarm_msg.setText("---")
            self.lbl_alarm_msg.setStyleSheet("color: gray;")

    def on_plc_measured(self, data) -> None:
        """data 為 dict{name: float} 或 None (讀失敗)。"""
        if data is None:
            self.update_temperature(None)
            return
        if "temperature" in data:
            self.update_temperature(data["temperature"])
            self._last_temp = data["temperature"]
        if "rpm" in data:               # DM7006: 轉速 (RPM)
            self.lbl_rpm.setText(f"{data['rpm']:8.0f}")
            self._last_rpm = data["rpm"]
        if "rpm_percent" in data:       # DM7001: 轉速 (%)，顯示於 RPM 旁括弧
            self.lbl_rpm_pct.setText(f"({data['rpm_percent']:.0f}%)")
        if "servo" in data:
            new_servo = round(data["servo"]) != 0   # DM7003: 1=ON, 0=OFF
            if new_servo != self._servo_on:
                self._servo_on = new_servo
                self._refresh_manual_enabled()   # 伺服狀態變 → 更新速度設定可用性
            if self._servo_on:
                self.lbl_servo_state.setText("● ON")
                self.lbl_servo_state.setStyleSheet("color: #2ca02c; font-weight: bold;")
            else:
                self.lbl_servo_state.setText("● OFF")
                self.lbl_servo_state.setStyleSheet("color: #d62728; font-weight: bold;")
        if "manual_enable" in data:         # DM7005: 非0=手動啟用 (MR102 ON)
            self._set_manual_enable(round(data["manual_enable"]) != 0)
        if "alarm" in data:
            self._alarm_active = round(data["alarm"]) != 0   # DM7004: 非0=異常
            if self._alarm_active:
                self.lbl_alarm_state.setText("● 異常")
                self.lbl_alarm_state.setStyleSheet("color: #d62728; font-weight: bold;")
            else:
                self.lbl_alarm_state.setText("● 正常")
                self.lbl_alarm_state.setStyleSheet("color: #2ca02c; font-weight: bold;")
        if "alarm_code" in data:        # DM7004: 逐 bit 解碼成訊息 (bit0=急停, bit1=驅動器異常)
            self._update_alarm_messages(int(round(data["alarm_code"])) & 0xFFFF)
        if "mode" in data:
            speed_auto = round(data["mode"]) != 0   # DM7002: 1=自動控制速度, 0=手動控制速度
            if speed_auto != self._speed_auto:
                self._speed_auto = speed_auto
                self._update_speed_control_visibility()
                self._refresh_manual_enabled()
                self.status.showMessage(
                    "速度：自動控制 (可由速度設定送出 RPM)" if speed_auto
                    else "速度：手動控制", 3000
                )
        # 伺服/異常狀態每輪都可能變 → 更新自動測試前置條件提醒
        self._update_test_prereqs()

    def _update_alarm_messages(self, code: int) -> None:
        """把 DM7004 異常碼逐 bit 解碼成訊息顯示；0 表示無異常。"""
        if code == 0:
            self.lbl_alarm_msg.setText("無異常")
            self.lbl_alarm_msg.setStyleSheet("color: #2ca02c; font-weight: bold;")
            return
        msgs = []
        for bit in range(16):
            if code & (1 << bit):
                msgs.append(self.alarm_bit_labels.get(bit, f"bit{bit}"))
        self.lbl_alarm_msg.setText("⚠ " + "、".join(msgs))
        self.lbl_alarm_msg.setStyleSheet("color: #d62728; font-weight: bold;")

    def on_plc_poller_failed(self, msg: str) -> None:
        self.status.showMessage(f"[PLC] {msg}", 8000)
        self._stop_plc_monitor()

    # ---------- 伺服 ON / OFF (PLC 繼電器) ----------
    def on_servo_on(self) -> None:
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法操作伺服", 4000)
            return
        try:
            with self.plc_lock:
                self.plc_driver.set_relay(self.servo_relay)
        except KVError as e:
            self.status.showMessage(f"[伺服 ON 失敗] {e}", 5000)
            return
        self.status.showMessage("伺服 ON", 3000)

    def on_servo_off(self) -> None:
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法操作伺服", 4000)
            return
        try:
            with self.plc_lock:
                self.plc_driver.reset_relay(self.servo_relay)
        except KVError as e:
            self.status.showMessage(f"[伺服 OFF 失敗] {e}", 5000)
            return
        self.status.showMessage("伺服 OFF", 3000)

    # ---------- 手動啟用 (PLC 繼電器 MR102，toggle ST/RS) ----------
    def on_manual_enable_toggle(self) -> None:
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法切換手動啟用", 4000)
            return
        turn_on = not self._manual_enable_on   # 依目前狀態決定 ST(ON) 或 RS(OFF)
        try:
            with self.plc_lock:
                if turn_on:
                    self.plc_driver.set_relay(self.manual_enable_relay)
                else:
                    self.plc_driver.reset_relay(self.manual_enable_relay)
        except KVError as e:
            self.status.showMessage(f"[手動啟用切換失敗] {e}", 5000)
            return
        # 不在此處改 _manual_enable_on / 背景色 — 等下一輪 DM7005 回授為準
        self.status.showMessage(
            f"手動啟用 {'ON' if turn_on else 'OFF'}", 3000
        )

    def _set_manual_enable(self, on: bool) -> None:
        """依 DM7005 回授更新手動啟用狀態：變色 + 連動框內元件可用性。"""
        if on == self._manual_enable_on:
            return
        self._manual_enable_on = on
        self._apply_control_box_style()
        self._apply_manual_enable_btn_style()   # 按鈕 ON→綠 / OFF→藍
        self._refresh_manual_enabled()   # 啟用切換 → 框內元件可用性跟著變

    def _apply_control_box_style(self) -> None:
        """手動啟用(MR102/DM7005) ON → 淺綠背景；否則回原底色 (框內元件改以 disable 表示不可用)。"""
        if not hasattr(self, "control_box"):
            return
        if self._manual_enable_on:
            self.control_box.setStyleSheet(
                "QGroupBox#controlBox { background-color:#cdf0cd; }"
            )
        else:
            self.control_box.setStyleSheet("")   # 清空 → 回原本底色

    def _apply_manual_enable_btn_style(self) -> None:
        """手動啟用按鈕：ON→綠 (作用中)、OFF→藍。"""
        if not hasattr(self, "btn_manual_enable"):
            return
        bg, hover = ("#2ca02c", "#34b237") if self._manual_enable_on else ("#1f77b4", "#2a8fd0")
        self.btn_manual_enable.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:white; font-size:14px; "
            f"font-weight:bold; border-radius:6px; }}"
            f"QPushButton:disabled {{ background:#aaa; color:#eee; }}"
            f"QPushButton:hover:!disabled {{ background:{hover}; }}"
        )

    # ---------- 發電機測試 ON / OFF (PLC 繼電器 MR103) ----------
    def _set_gentest_state(self, on) -> None:
        """更新即時監視的「發電機測試開關」狀態燈。on=True/False/None(未知)。"""
        if not hasattr(self, "lbl_gentest_state"):
            return
        if on is None:
            self.lbl_gentest_state.setText("● ---")
            self.lbl_gentest_state.setStyleSheet("color: gray;")
        elif on:
            self.lbl_gentest_state.setText("● ON")
            self.lbl_gentest_state.setStyleSheet("color: #2ca02c; font-weight: bold;")
        else:
            self.lbl_gentest_state.setText("● OFF")
            self.lbl_gentest_state.setStyleSheet("color: #d62728; font-weight: bold;")

    def on_gentest_on(self) -> None:
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法操作發電機測試", 4000)
            return
        try:
            with self.plc_lock:
                self.plc_driver.set_relay(self.generator_test_relay)
        except KVError as e:
            self.status.showMessage(f"[發電機測試 ON 失敗] {e}", 5000)
            return
        self._set_gentest_state(True)
        self.status.showMessage("發電機測試 ON", 3000)

    def on_gentest_off(self) -> None:
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法操作發電機測試", 4000)
            return
        try:
            with self.plc_lock:
                self.plc_driver.reset_relay(self.generator_test_relay)
        except KVError as e:
            self.status.showMessage(f"[發電機測試 OFF 失敗] {e}", 5000)
            return
        self._set_gentest_state(False)
        self.status.showMessage("發電機測試 OFF", 3000)

    # ---------- 異常 RESET (PLC 繼電器，直接 ON) ----------
    def on_alarm_reset(self) -> None:
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法 RESET 異常", 4000)
            return
        try:
            with self.plc_lock:
                self.plc_driver.set_relay(self.alarm_reset_relay)
        except KVError as e:
            self.status.showMessage(f"[異常 RESET 失敗] {e}", 5000)
            return
        self.status.showMessage("異常 RESET 已送出", 3000)

    # ---------- 功率計連線 / 中斷 + 功率因數輪詢 (GPM-8310) ----------
    def on_gpm_connect(self) -> None:
        """連線 GPM-8310 並開始輪詢功率因數；失敗只在狀態列提示，不影響其他儀器。"""
        if self.gpm_driver is not None:
            return
        gcfg = self.cfg.get("power_meter") or {}
        try:
            port = int(self.gpm_port_edit.text().strip())
        except ValueError:
            self.status.showMessage("功率計 Port 必須是整數", 4000)
            return

        try:
            self.gpm_driver = GPM8310(
                ip=self.gpm_ip_edit.text().strip(),
                port=port,
                timeout=float(gcfg.get("timeout", 5.0)),
                command_delay=float(gcfg.get("command_delay", 0.05)),
                read_buffer=int(gcfg.get("read_buffer", 4096)),
            )
            self.gpm_driver.connect()
            mode = str(gcfg.get("input_mode", "") or "").strip()
            if mode:
                self.gpm_driver.set_input_mode(mode)
            self.gpm_driver.configure_power_factor()
        except GPMError as e:
            self.status.showMessage(f"功率計連線失敗: {e}", 6000)
            self.gpm_driver = None
            return

        self.lbl_gpm_conn_status.setText("● 已連線")
        self.lbl_gpm_conn_status.setStyleSheet("color: green; font-weight: bold;")
        self.btn_gpm_connect.setEnabled(False)
        self.btn_gpm_disconnect.setEnabled(True)
        self.gpm_ip_edit.setEnabled(False)
        self.gpm_port_edit.setEnabled(False)
        self._gpm_ready = True
        self._update_test_prereqs()   # 功率計連線 → 更新前置條件提醒

        self.gpm_poller = GPMPollingThread(
            self.gpm_driver, self.gpm_lock,
            interval_sec=float(gcfg.get("poll_interval_sec", 1.0)),
        )
        self.gpm_poller.measured.connect(self.on_gpm_measured)
        self.gpm_poller.unavailable.connect(self.on_gpm_unavailable)
        self.gpm_poller.failed.connect(self.on_gpm_poller_failed)
        self.gpm_poller.start()
        self.status.showMessage(
            f"功率計已連線: {self.gpm_ip_edit.text().strip()}:{port}", 4000
        )

    def on_gpm_disconnect(self) -> None:
        self._stop_gpm_monitor()
        self.status.showMessage("功率計已中斷連線", 3000)

    def _stop_gpm_monitor(self) -> None:
        if self.gpm_poller is not None:
            self.gpm_poller.stop()
            self.gpm_poller = None
        if self.gpm_driver is not None:
            try:
                self.gpm_driver.disconnect()
            except Exception:
                pass
            self.gpm_driver = None
        self._gpm_ready = False
        self._last_pf = None
        self._update_test_prereqs()   # 功率計斷線 → 更新前置條件提醒
        if hasattr(self, "lbl_gpm_conn_status"):
            self.lbl_gpm_conn_status.setText("● 未連線")
            self.lbl_gpm_conn_status.setStyleSheet("color: #d62728; font-weight: bold;")
            self.btn_gpm_connect.setEnabled(True)
            self.btn_gpm_disconnect.setEnabled(False)
            self.gpm_ip_edit.setEnabled(True)
            self.gpm_port_edit.setEnabled(True)
            self.lbl_pf.setText("---")

    def on_gpm_measured(self, pf: float) -> None:
        self.lbl_pf.setText(f"{pf:8.4f}")
        self._last_pf = pf

    def on_gpm_unavailable(self, msg: str) -> None:
        """資料層級 (超量程/無資料)：顯示 0 但維持連線、繼續輪詢。"""
        self.lbl_pf.setText(f"{0.0:8.4f}")
        self._last_pf = 0.0         # 取樣快取也記 0，與顯示一致
        self.status.showMessage(f"[功率計] {msg}", 4000)

    def on_gpm_poller_failed(self, msg: str) -> None:
        self.status.showMessage(f"[功率計] {msg}", 8000)
        self._stop_gpm_monitor()

    def _build_control_box(self) -> QGroupBox:
        box = QGroupBox("手動控制")
        box.setObjectName("controlBox")   # 供背景色 ID 選擇器使用 (依手動啟用狀態變色)
        self.control_box = box
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)   # 縮小手動控制各欄位垂直間距 (原 12)
        grid.setContentsMargins(8, 6, 8, 6)

        lbl_font = QFont(); lbl_font.setPointSize(11)
        lbl_set = QLabel("設定電流 (A):")
        lbl_set.setFont(lbl_font)
        grid.addWidget(lbl_set, 0, 0)

        self.spin_current = QDoubleSpinBox()
        i_max = float(self.cfg["dut"]["current_max"])
        self.spin_current.setRange(0.0, i_max)
        self.spin_current.setDecimals(1)
        self.spin_current.setSingleStep(0.1)
        self.spin_current.setValue(0.5)
        self.spin_current.setFixedWidth(120)
        spin_font = QFont(); spin_font.setPointSize(11)
        self.spin_current.setFont(spin_font)
        grid.addWidget(self.spin_current, 0, 1)

        self.btn_apply = QPushButton("變更 (套用)")
        self.btn_apply.setStyleSheet(
            "QPushButton { padding:2px 12px; font-size:11px; }"
        )
        self.btn_apply.clicked.connect(self.on_apply_current)
        grid.addWidget(self.btn_apply, 0, 2)
        grid.setColumnStretch(3, 1)

        # ── 動作按鈕：統一固定大小、靠左排列 (不再撐滿格子) ──
        BTN_W, BTN_H = 124, 26   # 與「發電機測試」同寬，整齊一致
        BTN_GAP = 6              # ON / OFF 之間留小間距，不黏在一起

        def _action_btn(text, bg, hover, slot):
            b = QPushButton(text)
            b.setFixedSize(BTN_W, BTN_H)
            b.setStyleSheet(
                f"QPushButton {{ background:{bg}; color:white; font-size:12px; "
                f"font-weight:bold; border-radius:6px; }}"
                f"QPushButton:disabled {{ background:#aaa; color:#eee; }}"
                f"QPushButton:hover:!disabled {{ background:{hover}; }}"
            )
            b.clicked.connect(slot)
            return b

        # PEL 負載 ON / OFF
        self.btn_load_on = _action_btn("LOAD ON", "#2ca02c", "#34b237", self.on_load_on)
        self.btn_load_off = _action_btn("LOAD OFF", "#d62728", "#e34a4b", self.on_load_off)
        pel_row = QHBoxLayout()
        pel_row.addWidget(self.btn_load_on)
        pel_row.addSpacing(BTN_GAP)
        pel_row.addWidget(self.btn_load_off)
        pel_row.addStretch()
        grid.addLayout(pel_row, 1, 0, 1, 4)

        # PLC 伺服 ON / OFF (繼電器 MR100)。異常 RESET 已移至即時監視「異常」下一行
        self.btn_servo_on = _action_btn("伺服 ON", "#2ca02c", "#34b237", self.on_servo_on)
        self.btn_servo_off = _action_btn("伺服 OFF", "#d62728", "#e34a4b", self.on_servo_off)
        plc_row = QHBoxLayout()
        plc_row.addWidget(self.btn_servo_on)
        plc_row.addSpacing(BTN_GAP)
        plc_row.addWidget(self.btn_servo_off)
        plc_row.addStretch()
        grid.addLayout(plc_row, 2, 0, 1, 4)

        # PLC 發電機測試 ON / OFF (繼電器 MR103)
        self.btn_gentest_on = _action_btn("發電機測試 ON", "#2ca02c", "#34b237", self.on_gentest_on)
        self.btn_gentest_off = _action_btn("發電機測試 OFF", "#d62728", "#e34a4b", self.on_gentest_off)
        gen_row = QHBoxLayout()
        gen_row.addWidget(self.btn_gentest_on)
        gen_row.addSpacing(BTN_GAP)
        gen_row.addWidget(self.btn_gentest_off)
        gen_row.addStretch()
        grid.addLayout(gen_row, 3, 0, 1, 4)

        # ── 速度設定 (RPM)：僅當 DM7002=自動控制速度 時顯示。
        # 啟動鈕把 RPM 換算成 % 寫入 PLC DM102 (沿用 _write_rpm_setpoint)。──
        full_rpm = int(self.rpm_full_scale) if self.rpm_full_scale > 0 else 6000
        # DM102 直接寫 RPM；解析度受 PLC 類比分割限制 = 滿刻度 / 分割數 (例 6000/4000=1.5 RPM)
        divisions = self.rpm_divisions if self.rpm_divisions > 0 else 4000.0
        min_rpm = (self.rpm_full_scale if self.rpm_full_scale > 0 else 6000.0) / divisions
        min_rpm_str = f"{min_rpm:g}"
        lbl_speed = QLabel("速度設定 (RPM):"); lbl_speed.setFont(lbl_font)
        self.spin_manual_rpm = QSpinBox()
        self.spin_manual_rpm.setRange(0, full_rpm)
        self.spin_manual_rpm.setSingleStep(max(1, int(round(min_rpm))))
        self.spin_manual_rpm.setValue(0)
        self.spin_manual_rpm.setFixedWidth(100)
        self.spin_manual_rpm.setFont(spin_font)
        self.spin_manual_rpm.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.btn_apply_rpm = _action_btn("速度設定", "#1f77b4", "#2a8fd0", self.on_apply_manual_rpm)
        self.btn_stop_rpm = _action_btn("停止", "#d62728", "#e34a4b", self.on_stop_manual_rpm)
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 0, 0, 0)
        ctrl_row.addWidget(lbl_speed)
        ctrl_row.addWidget(self.spin_manual_rpm)
        ctrl_row.addSpacing(BTN_GAP)
        ctrl_row.addWidget(self.btn_apply_rpm)
        ctrl_row.addSpacing(BTN_GAP)
        ctrl_row.addWidget(self.btn_stop_rpm)
        ctrl_row.addStretch()
        # 提醒：DM102 直接寫 RPM，解析度受類比分割限制
        hint = QLabel(f"※ 最小解析度約 {min_rpm_str} RPM")
        hint_font = QFont(); hint_font.setPointSize(9)
        hint.setFont(hint_font); hint.setStyleSheet("color:#b35900;"); hint.setWordWrap(True)
        speed_box = QVBoxLayout()
        speed_box.setContentsMargins(0, 0, 0, 0); speed_box.setSpacing(2)
        speed_box.addLayout(ctrl_row); speed_box.addWidget(hint)
        self._row_manual_rpm = QWidget(); self._row_manual_rpm.setLayout(speed_box)
        self._row_manual_rpm.setVisible(False)   # 預設隱藏，等 DM7002=自動才顯示
        grid.addWidget(self._row_manual_rpm, 4, 0, 1, 4)

        # 伺服 / 發電機測試 按鈕的啟用狀態跟著 PLC 連線 (非 PEL)；預設停用
        self.btn_servo_on.setEnabled(False)
        self.btn_servo_off.setEnabled(False)
        self.btn_gentest_on.setEnabled(False)
        self.btn_gentest_off.setEnabled(False)
        self._apply_control_box_style()   # 初始套用暗灰背景
        return box

    def _build_manual_enable_row(self) -> QHBoxLayout:
        """手動啟用 (MR102) 列 — 置於手動控制框「上方」，當作開啟下方手動操作的閘門。"""
        # toggle：依目前狀態 ST/RS；狀態以 DM7005 回授為準
        self.btn_manual_enable = QPushButton("手動啟用")
        self.btn_manual_enable.setFixedSize(110, 26)
        self.btn_manual_enable.clicked.connect(self.on_manual_enable_toggle)
        self.btn_manual_enable.setEnabled(False)   # 跟著 PLC 連線；預設停用
        self._apply_manual_enable_btn_style()      # 依目前狀態套用藍/綠
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.btn_manual_enable)
        row.addStretch()
        return row

    def _build_settings_box(self) -> QGroupBox:
        self.settings_box = box = QGroupBox("設定")
        outer = QVBoxLayout(box)
        outer.setSpacing(10)

        lbl_font = QFont(); lbl_font.setPointSize(10)
        spin_font = QFont(); spin_font.setPointSize(10)

        atcfg = self.cfg.get("auto_test") or {}
        i_cap = float(self.cfg["dut"]["current_max"])
        fixed_i_default = float(atcfg.get("default_fixed_current", 5.0))
        percents = atcfg.get("load_percents", [5, 50, 100])
        self._stage_percents = list(percents[:3])   # 供「最大電流/最大RPM 變更→按比例填回」用
        full_rpm = int(self.rpm_full_scale) if self.rpm_full_scale > 0 else 6000

        title_font = QFont(); title_font.setPointSize(11); title_font.setBold(True)

        # ── 測試模式選擇 (下拉) ──
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("模式一：定轉速、變負載")   # index 0 → "current"
        self.mode_combo.addItem("模式二：定負載、變轉速")   # index 1 → "rpm"
        self.mode_combo.setFont(spin_font)
        lbl_mode = QLabel("測試模式:"); lbl_mode.setFont(title_font)
        # 測試模式下拉 + 該模式的設定欄位放「同一排」省高度 (欄位於下方建立後再加入)
        mode_row = QHBoxLayout()
        mode_row.addWidget(lbl_mode)
        mode_row.addWidget(self.mode_combo)
        mode_row.addSpacing(16)

        def _make_row(label_text, color, spin) -> QWidget:
            lbl = QLabel(label_text); lbl.setFont(title_font)
            lbl.setStyleSheet(f"color:{color};")
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(lbl); row.addWidget(spin); row.addStretch()
            w = QWidget(); w.setLayout(row)
            return w

        # ── 模式一專屬：最大電流設定 (計算器) + RPM 固定設定值 ──
        self.spin_max_current = QDoubleSpinBox()
        self.spin_max_current.setRange(0.0, 300.0)
        self.spin_max_current.setDecimals(1); self.spin_max_current.setSingleStep(0.5)
        self.spin_max_current.setValue(i_cap)
        self.spin_max_current.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_max_current.setFixedWidth(100)
        self.spin_max_current.setFont(spin_font)
        self.spin_max_current.setStyleSheet(
            "QDoubleSpinBox { color:#d62728; font-weight:bold; "
            "border:2px solid #d62728; border-radius:4px; padding:2px; "
            "background:#fff5f5; }"
        )
        self._row_max_current = _make_row("最大電流設定 (A):", "#d62728", self.spin_max_current)
        mode_row.addWidget(self._row_max_current)

        self.spin_max_rpm = QSpinBox()
        self.spin_max_rpm.setRange(0, full_rpm)   # 上限 = DM102 100% 對應轉速
        self.spin_max_rpm.setValue(full_rpm)
        self.spin_max_rpm.setFixedWidth(100)
        self.spin_max_rpm.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_max_rpm.setFont(spin_font)
        self.spin_max_rpm.setStyleSheet(
            "QSpinBox { color:#1f77b4; font-weight:bold; "
            "border:2px solid #1f77b4; border-radius:4px; padding:2px; "
            "background:#f5f9ff; }"
        )
        self._row_fixed_rpm = _make_row("RPM設定值:", "#1f77b4", self.spin_max_rpm)
        mode_row.addWidget(self._row_fixed_rpm)

        # ── 模式二專屬：固定負載電流 + 最大RPM設定 (計算器) ──
        self.spin_fixed_current = QDoubleSpinBox()
        self.spin_fixed_current.setRange(0.0, 300.0)
        self.spin_fixed_current.setDecimals(1); self.spin_fixed_current.setSingleStep(0.5)
        self.spin_fixed_current.setValue(fixed_i_default)
        self.spin_fixed_current.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_fixed_current.setFixedWidth(100)
        self.spin_fixed_current.setFont(spin_font)
        self.spin_fixed_current.setStyleSheet(
            "QDoubleSpinBox { color:#d62728; font-weight:bold; "
            "border:2px solid #d62728; border-radius:4px; padding:2px; "
            "background:#fff5f5; }"
        )
        self._row_fixed_current = _make_row("固定負載電流 (A):", "#d62728", self.spin_fixed_current)
        mode_row.addWidget(self._row_fixed_current)

        self.spin_max_rpm_calc = QSpinBox()
        self.spin_max_rpm_calc.setRange(0, full_rpm)
        self.spin_max_rpm_calc.setValue(full_rpm)
        self.spin_max_rpm_calc.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_max_rpm_calc.setFixedWidth(100)
        self.spin_max_rpm_calc.setFont(spin_font)
        self.spin_max_rpm_calc.setStyleSheet(
            "QSpinBox { color:#1f77b4; font-weight:bold; "
            "border:2px solid #1f77b4; border-radius:4px; padding:2px; "
            "background:#f5f9ff; }"
        )
        self._row_max_rpm_calc = _make_row("最大RPM設定:", "#1f77b4", self.spin_max_rpm_calc)
        mode_row.addWidget(self._row_max_rpm_calc)
        mode_row.addStretch()
        outer.addLayout(mode_row)   # 模式下拉 + 設定欄位 一次加入同一排

        # ── 三階段 group：每個 group 內含「測試時間」+「負載電流」+「轉速」(後兩者依模式切換顯示) ──
        default_times = [30, 60, 120]
        self.stage_time_spins = []     # 各階段測試時間 (秒)
        self.stage_curr_spins = []     # 各階段負載電流設定 (A) — 模式一用
        self.stage_rpm_spins = []      # 各階段轉速設定 (RPM) — 模式二用
        self._stage_curr_rows = []     # (label, spin) 供切模式顯示/隱藏
        self._stage_rpm_rows = []
        groups_row = QHBoxLayout()
        groups_row.setSpacing(10)
        gb_font = QFont(); gb_font.setPointSize(11); gb_font.setBold(True)
        self._stage_groups = []
        for idx, pct in enumerate(self._stage_percents):
            gb = QGroupBox(f"{pct}%")
            gb.setFont(gb_font)   # group 標題字體與其他文字一致 (原為較小的預設)
            form = QFormLayout(gb)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

            t = QSpinBox()
            t.setRange(1, 86400)
            t.setValue(default_times[idx] if idx < len(default_times) else 60)
            t.setFixedWidth(90); t.setFont(spin_font)
            t.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

            c = QDoubleSpinBox()
            c.setRange(0.0, 300.0); c.setDecimals(1); c.setSingleStep(0.5)
            c.setValue(round(i_cap * pct / 100.0, 1))   # 預設 = 最大電流×百分比
            c.setFixedWidth(90); c.setFont(spin_font)
            c.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

            r = QSpinBox()
            r.setRange(0, full_rpm)
            r.setValue(0)   # 模式二由使用者自行輸入，不預設百分比
            r.setFixedWidth(90); r.setFont(spin_font)
            r.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

            lt = QLabel("測試時間 (秒):"); lt.setFont(lbl_font)
            lc = QLabel("負載電流設定 (A):"); lc.setFont(lbl_font)
            lr = QLabel("轉速設定 (RPM):"); lr.setFont(lbl_font)
            form.addRow(lt, t)
            form.addRow(lc, c)
            form.addRow(lr, r)

            self.stage_time_spins.append(t)
            self.stage_curr_spins.append(c)
            self.stage_rpm_spins.append(r)
            self._stage_curr_rows.append((lc, c))
            self._stage_rpm_rows.append((lr, r))
            # 變更即存檔 → 下次開機沿用各階段最後輸入值 (含手動覆寫的)
            t.valueChanged.connect(
                lambda val, k=f"stage_time_{idx}": self.settings.setValue(k, int(val))
            )
            c.valueChanged.connect(
                lambda val, k=f"stage_curr_{idx}": self.settings.setValue(k, float(val))
            )
            r.valueChanged.connect(
                lambda val, k=f"stage_rpm_{idx}": self.settings.setValue(k, int(val))
            )
            self._stage_groups.append(gb)
            groups_row.addWidget(gb)
        groups_row.addStretch()
        outer.addLayout(groups_row)

        # 計算器接線：最大電流/最大RPM 變更 → 依百分比填回各階段 (USER 仍可手動覆寫)
        self.spin_max_current.valueChanged.connect(self._on_max_current_changed)
        self.spin_max_rpm_calc.valueChanged.connect(self._on_max_rpm_changed)
        # 變更即存檔 → 下次開啟沿用
        self.spin_max_current.valueChanged.connect(
            lambda v: self.settings.setValue("max_current", float(v))
        )
        self.spin_max_rpm.valueChanged.connect(
            lambda v: self.settings.setValue("rpm", int(v))
        )
        self.spin_fixed_current.valueChanged.connect(
            lambda v: self.settings.setValue("fixed_current", float(v))
        )
        self.spin_max_rpm_calc.valueChanged.connect(
            lambda v: self.settings.setValue("max_rpm_calc", int(v))
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._on_mode_changed(0)   # 預設模式一 (稍後 _load_saved_settings 會覆寫)
        return box

    def _on_max_current_changed(self, value: float) -> None:
        """最大電流變更時，按各階段百分比自動更新負載電流設定 (帶入起始值，可手動覆寫)。"""
        for spin, pct in zip(self.stage_curr_spins, self._stage_percents):
            spin.blockSignals(True)
            spin.setValue(round(float(value) * pct / 100.0, 1))
            spin.blockSignals(False)

    def _on_max_rpm_changed(self, value: int) -> None:
        """最大RPM變更時，按各階段百分比自動更新轉速設定（僅模式一）。
        模式二各階段轉速由使用者自行輸入，不自動帶入。"""
        if self.mode_combo.currentIndex() == 1:
            return
        for spin, pct in zip(self.stage_rpm_spins, self._stage_percents):
            spin.blockSignals(True)
            spin.setValue(int(round(float(value) * pct / 100.0)))
            spin.blockSignals(False)

    def _on_mode_changed(self, idx: int) -> None:
        """切換測試模式：顯示對應的固定/計算器欄與各階段值列 (電流 ↔ 轉速)。"""
        if self._test_running:
            # 測試中不可切換；還原下拉選擇
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(0 if self.mode_combo.currentIndex() == 1 else 1)
            self.mode_combo.blockSignals(False)
            return
        is_rpm = (idx == 1)   # 模式二 = 定負載、變轉速
        # 頂部固定/計算器欄
        self._row_max_current.setVisible(not is_rpm)
        self._row_fixed_rpm.setVisible(not is_rpm)
        self._row_fixed_current.setVisible(is_rpm)
        self._row_max_rpm_calc.setVisible(is_rpm)
        # 各階段值列
        for (lc, c) in self._stage_curr_rows:
            lc.setVisible(not is_rpm); c.setVisible(not is_rpm)
        for (lr, r) in self._stage_rpm_rows:
            lr.setVisible(is_rpm); r.setVisible(is_rpm)
        # 模式二：group 標題改「速度一/二/三」；模式一：還原百分比
        if hasattr(self, "_stage_groups"):
            rpm_labels = ["速度一", "速度二", "速度三"]
            curr_labels = [f"{pct}%" for pct in self._stage_percents]
            for gb, label in zip(self._stage_groups, rpm_labels if is_rpm else curr_labels):
                gb.setTitle(label)
        self.settings.setValue("test_mode", int(idx))

    def _load_saved_settings(self) -> None:
        """套用上次關閉前儲存的設定 (最大電流 / RPM / 存檔路徑)；無則維持預設。"""
        s = self.settings
        mc = s.value("max_current")
        if mc is not None:
            try:
                self.spin_max_current.setValue(float(mc))   # 連動更新各階段電流
            except (TypeError, ValueError):
                pass
        rpm = s.value("rpm")
        if rpm is not None:
            try:
                self.spin_max_rpm.setValue(int(float(rpm)))
            except (TypeError, ValueError):
                pass
        fc = s.value("fixed_current")
        if fc is not None:
            try:
                self.spin_fixed_current.setValue(float(fc))
            except (TypeError, ValueError):
                pass
        mrc = s.value("max_rpm_calc")
        if mrc is not None:
            try:
                self.spin_max_rpm_calc.setValue(int(float(mrc)))   # 連動更新各階段轉速
            except (TypeError, ValueError):
                pass
        # 各階段「測試時間 / 負載電流 / 轉速」— 須在 max_current/max_rpm 自動帶入「之後」載入，
        # 才能用上次手動輸入值覆寫掉百分比帶入的預設。
        def _restore_stage(spins, key_prefix, conv):
            for i, spin in enumerate(spins):
                val = s.value(f"{key_prefix}_{i}")
                if val is None:
                    continue
                try:
                    spin.setValue(conv(val))
                except (TypeError, ValueError):
                    pass
        _restore_stage(self.stage_time_spins, "stage_time", lambda v: int(float(v)))
        _restore_stage(self.stage_curr_spins, "stage_curr", lambda v: float(v))
        _restore_stage(self.stage_rpm_spins, "stage_rpm", lambda v: int(float(v)))

        sd = s.value("save_dir")
        if sd:
            self.save_dir = Path(str(sd))
            self.lbl_save_dir.setText(str(self.save_dir))

        # 三組連線的 IP / Port：載入上次值，並在編輯完成時即存
        ip_fields = [
            ("pel_ip", self.ip_edit), ("pel_port", self.port_edit),
            ("plc_ip", self.plc_ip_edit), ("plc_port", self.plc_port_edit),
            ("gpm_ip", self.gpm_ip_edit), ("gpm_port", self.gpm_port_edit),
        ]
        for key, widget in ip_fields:
            val = s.value(key)
            if val:
                widget.setText(str(val))
            widget.editingFinished.connect(
                lambda w=widget, k=key: self.settings.setValue(k, w.text().strip())
            )

        # 測試模式 (最後套用，確保正確的欄位組顯示)
        tm = s.value("test_mode")
        if tm is not None:
            try:
                idx = int(float(tm))
            except (TypeError, ValueError):
                idx = 0
            if idx in (0, 1):
                self.mode_combo.setCurrentIndex(idx)
                self._on_mode_changed(idx)   # currentIndex 若未變則不會自動觸發

    def _build_auto_test_box(self) -> QGroupBox:
        box = QGroupBox("自動測試 (5% / 50% / 100% 階段負載)")
        v = QVBoxLayout(box)

        # ─ 控制列 ─
        ctrl = QHBoxLayout()
        self.btn_start_test = QPushButton("開始測試")
        self.btn_start_test.setStyleSheet(
            "QPushButton { background:#2ca02c; color:white; font-size:16px; "
            "padding:10px 24px; font-weight:bold; border-radius:6px; }"
            "QPushButton:disabled { background:#aaa; color:#eee; }"
            "QPushButton:hover:!disabled { background:#34b237; }"
        )
        self.btn_start_test.clicked.connect(self.on_start_test)
        self.btn_start_test.setEnabled(False)
        ctrl.addWidget(self.btn_start_test)

        self.btn_stop_test = QPushButton("停止")
        self.btn_stop_test.setStyleSheet(
            "QPushButton { background:#d62728; color:white; font-size:16px; "
            "padding:10px 24px; font-weight:bold; border-radius:6px; }"
            "QPushButton:disabled { background:#aaa; color:#eee; }"
            "QPushButton:hover:!disabled { background:#e34a4b; }"
        )
        self.btn_stop_test.clicked.connect(self.on_stop_test)
        self.btn_stop_test.setEnabled(False)
        ctrl.addWidget(self.btn_stop_test)
        ctrl.addStretch()
        v.addLayout(ctrl)

        # ─ 前置條件提醒 (負載機/PLC/伺服/功率計/異常等未就緒時顯示) ─
        self.lbl_test_prereq = QLabel("")
        pf_warn = QFont(); pf_warn.setPointSize(11); pf_warn.setBold(True)
        self.lbl_test_prereq.setFont(pf_warn)
        self.lbl_test_prereq.setWordWrap(True)
        self.lbl_test_prereq.setVisible(False)
        v.addWidget(self.lbl_test_prereq)

        # ─ 存檔路徑 (CSV + 曲線圖 存同一資料夾，檔名為日期+時間) ─
        path_row = QHBoxLayout()
        lbl_path_title = QLabel("存檔路徑:")
        pf2 = QFont(); pf2.setPointSize(11)
        lbl_path_title.setFont(pf2)
        path_row.addWidget(lbl_path_title)
        self.lbl_save_dir = QLabel(str(self.save_dir))
        self.lbl_save_dir.setFont(pf2)
        self.lbl_save_dir.setStyleSheet("color:#555;")
        self.lbl_save_dir.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_row.addWidget(self.lbl_save_dir, 1)
        self.btn_browse_dir = QPushButton("瀏覽…")
        self.btn_browse_dir.clicked.connect(self.on_browse_save_dir)
        path_row.addWidget(self.btn_browse_dir)
        v.addLayout(path_row)

        # ─ 進度 + 秒數計時 (放大、醒目) ─
        prog_row = QHBoxLayout()
        self.lbl_test_status = QLabel("待命")
        sf = QFont(); sf.setPointSize(15); sf.setBold(True)
        self.lbl_test_status.setFont(sf)
        self.lbl_test_status.setStyleSheet("color:#555;")
        prog_row.addWidget(self.lbl_test_status)
        prog_row.addStretch()
        self.lbl_test_timer = QLabel("")
        tmf = QFont(); tmf.setPointSize(20); tmf.setBold(True); tmf.setFamily("Consolas")
        self.lbl_test_timer.setFont(tmf)
        self.lbl_test_timer.setStyleSheet("color:#1f77b4;")
        prog_row.addWidget(self.lbl_test_timer)
        v.addLayout(prog_row)

        # ─ 手動紀錄 (與自動流程無關的被動記錄器) ─
        manual_row = QHBoxLayout()
        self.btn_manual_record = QPushButton("手動紀錄")
        self.btn_manual_record.setStyleSheet(
            "QPushButton { background:#1f77b4; color:white; font-size:14px; "
            "padding:6px 18px; font-weight:bold; border-radius:6px; }"
            "QPushButton:disabled { background:#aaa; color:#eee; }"
            "QPushButton:hover:!disabled { background:#2a8fd0; }"
        )
        self.btn_manual_record.clicked.connect(self.on_toggle_manual_record)
        self.btn_manual_record.setEnabled(False)
        manual_row.addWidget(self.btn_manual_record)
        self.lbl_manual_status = QLabel("")
        mf = QFont(); mf.setPointSize(11)
        self.lbl_manual_status.setFont(mf)
        manual_row.addWidget(self.lbl_manual_status)
        manual_row.addStretch()
        v.addLayout(manual_row)

        # ─ 曲線圖 ─
        self.lbl_plot = QLabel("（測試完成後在此顯示曲線圖）")
        self.lbl_plot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_plot.setStyleSheet("color:#aaa; border:1px solid #ddd;")
        self.lbl_plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.lbl_plot.setMinimumHeight(320)
        v.addWidget(self.lbl_plot, 1)
        return box

    def on_browse_save_dir(self) -> None:
        """選擇紀錄存檔路徑 (CSV + 曲線圖)。預設值來自 config，UI 設定僅作用於本次執行。"""
        d = QFileDialog.getExistingDirectory(self, "選擇存檔路徑", str(self.save_dir))
        if d:
            self.save_dir = Path(d)
            self.lbl_save_dir.setText(str(self.save_dir))
            self.settings.setValue("save_dir", str(self.save_dir))   # 存檔 → 下次沿用
            self.status.showMessage(f"存檔路徑設為：{d}", 4000)

    # ---------- 手動紀錄 (被動記錄器，與自動流程無關) ----------
    def on_toggle_manual_record(self) -> None:
        if self._manual_recording:
            self._stop_manual_record()
        else:
            self._start_manual_record()

    def _start_manual_record(self) -> None:
        if self.load is None:
            self.status.showMessage("請先連線負載機再開始手動紀錄", 4000)
            return
        if self._test_running:
            self.status.showMessage("自動測試進行中，無法同時手動紀錄", 4000)
            return
        self._manual_recording = True
        self._manual_rows = []
        self._manual_t0 = time.monotonic()
        self._live_rows = []
        self._live_plotted_count = 0
        self._test_pixmap = None
        self.lbl_plot.setText("（手動紀錄中…曲線將即時更新）")
        self.btn_manual_record.setText("停止紀錄")
        self.btn_disconnect.setEnabled(False)   # 紀錄中不可中斷負載機
        self._refresh_manual_enabled()
        self.status.showMessage("手動紀錄開始", 4000)
        # 取樣計時器 (每秒記一筆當下快取值)
        self._manual_timer = QTimer(self)
        self._manual_timer.setInterval(1000)
        self._manual_timer.timeout.connect(self._manual_sample)
        self._manual_timer.start()
        # 即時重繪計時器 (與自動測試共用同一個重繪函式)
        self._live_plot_timer = QTimer(self)
        self._live_plot_timer.setInterval(1000)
        self._live_plot_timer.timeout.connect(self._redraw_live_plot)
        self._live_plot_timer.start()

    def _manual_sample(self) -> None:
        """每秒把目前快取的量測值記一筆 (不主動讀 socket，避免與輪詢競爭)。"""
        if self._last_v is None:
            return  # 尚未有量測值
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": time.monotonic() - self._manual_t0,
            "stage": "",                       # 手動紀錄無階段
            "set_current_A": float(self.spin_current.value()),
            "V": self._last_v, "I": self._last_i, "P": self._last_p,
            "temperature": self._last_temp,
            "rpm": self._last_rpm,
            "power_factor": self._last_pf,
        }
        self._manual_rows.append(row)
        self._live_rows.append(row)
        self.lbl_manual_status.setText(f"紀錄中… {len(self._manual_rows)} 筆")

    def _stop_manual_record(self) -> None:
        if self._manual_timer is not None:
            self._manual_timer.stop()
            self._manual_timer = None
        self._stop_live_plot_timer()
        self._manual_recording = False
        self.btn_manual_record.setText("手動紀錄")
        self.btn_disconnect.setEnabled(self.load is not None)
        self._refresh_manual_enabled()
        rows = self._manual_rows
        if not rows:
            self.lbl_manual_status.setText("已停止 (無資料)")
            self.status.showMessage("手動紀錄停止 — 無資料", 4000)
            return
        self.lbl_manual_status.setText(f"已停止 — 共 {len(rows)} 筆")
        self._save_record(rows, "手動紀錄 — 時間曲線")

    # ---------- 自動測試 ----------
    def on_start_test(self) -> None:
        if self.load is None:
            self.status.showMessage("請先連線負載機再開始測試", 4000)
            return
        if self._test_running:
            return

        dut = self.cfg["dut"]
        atcfg = self.cfg.get("auto_test") or {}
        percents = atcfg.get("load_percents", [5, 50, 100])
        sample_interval = float(atcfg.get("sample_interval", 0.5))
        # 各階段時間直接取自三個 group (不再由最大電流×百分比換算)
        durations = [s.value() for s in self.stage_time_spins]
        i_max_limit = float(dut["current_max"])
        v_max_limit = float(dut["voltage_max"])

        # 依測試模式組裝階段資料 (mode_combo: 0=定轉速變負載, 1=定負載變轉速)
        is_rpm = (self.mode_combo.currentIndex() == 1)
        if is_rpm:
            # 模式二：固定 CC 電流，逐階段改寫轉速
            fixed_current = float(self.spin_fixed_current.value())
            if fixed_current <= 0:
                self.status.showMessage("固定負載電流需 > 0", 4000)
                return
            if fixed_current > i_max_limit:
                self.status.showMessage(
                    f"固定負載電流 {fixed_current:.1f}A 超過 DUT 上限 {i_max_limit:.1f}A",
                    6000,
                )
                return
            rpms = [r.value() for r in self.stage_rpm_spins]
            if max(rpms, default=0) <= 0:
                self.status.showMessage("各階段轉速設定至少要有一階 > 0", 4000)
                return
            # (label, setpoint_RPM, duration_s) — 階段值為轉速
            stages = [
                (label, float(rpm), dur)
                for label, rpm, dur in zip(["速度一", "速度二", "速度三"], rpms, durations)
            ]
            mode = "rpm"
            initial_rpm = rpms[0]            # 先把第一階段轉速寫入，馬達先轉再上負載
        else:
            # 模式一：固定轉速，逐階段改寫 CC 電流
            fixed_current = None
            currents = [c.value() for c in self.stage_curr_spins]
            if max(currents, default=0.0) <= 0:
                self.status.showMessage("各階段負載電流設定至少要有一階 > 0", 4000)
                return
            over = [c for c in currents if c > i_max_limit]
            if over:
                self.status.showMessage(
                    f"階段電流 {max(over):.1f}A 超過 DUT 上限 {i_max_limit:.1f}A，請降低該階段電流設定",
                    6000,
                )
                return
            # (label, setpoint_A, duration_s) — group 名稱(百分比)與各自電流/時間一一對應
            stages = [
                (f"{pct}%", curr, dur)
                for pct, curr, dur in zip(percents, currents, durations)
            ]
            mode = "current"
            initial_rpm = self.spin_max_rpm.value()   # 固定轉速

        self._test_running = True
        self._refresh_manual_enabled()
        self.btn_disconnect.setEnabled(False)   # 測試中不可中斷負載機
        self._test_pixmap = None
        self._live_rows = []
        self._live_plotted_count = 0
        self.lbl_plot.setText("（測試進行中…曲線將即時更新）")
        self.lbl_test_status.setText("● 測試進行中")
        self.lbl_test_status.setStyleSheet("color:#2ca02c;")   # 進行中 → 綠色醒目
        self.status.showMessage("自動測試開始", 4000)
        # 秒數計時：總時間 = 各階段時間總和；起算並立即顯示一次
        self._test_total_s = int(sum(durations))
        self._test_t0 = time.monotonic()
        self._update_test_timer()

        self.auto_test_thread = AutoTestThread(
            self.load, self.io_lock, stages, sample_interval,
            v_max_limit, i_max_limit,
            get_aux=lambda: (self._last_temp, self._last_rpm, self._last_pf),
            mode=mode,
            fixed_current=fixed_current,
            apply_stage_value=self._apply_stage_rpm if is_rpm else None,
        )
        self.auto_test_thread.progress.connect(self.on_test_progress)
        self.auto_test_thread.sampled.connect(self.on_test_sampled)
        self.auto_test_thread.sample_row.connect(self.on_test_row)
        self.auto_test_thread.finished_ok.connect(self.on_test_finished)
        self.auto_test_thread.failed.connect(self.on_test_failed)
        # 連動 PLC：最先開啟發電機測試 (MR103)，讓測試電路在馬達轉動前就緒。
        self._set_gentest_relay(True)
        # 再把起始轉速寫入 PLC DM102 (馬達先轉)，接著啟動負載序列。
        # 模式一為固定轉速；模式二為第一階段轉速 (執行緒會於各階段再逐次改寫)。
        self._write_rpm_setpoint(initial_rpm)
        self.auto_test_thread.start()

        # 連動 PLC：測試開始 → MR101 ON (PLC 未連線則略過)
        self._set_test_relay(True)

        # 啟動即時重繪計時器 (每秒重繪一次，避免每筆取樣都重畫拖慢)
        self._live_plot_timer = QTimer(self)
        self._live_plot_timer.setInterval(1000)
        self._live_plot_timer.timeout.connect(self._redraw_live_plot)
        self._live_plot_timer.start()

        # 啟動秒數計時器 (每秒更新「已經過 / 總時間 / 剩餘」)
        self._test_elapsed_timer = QTimer(self)
        self._test_elapsed_timer.setInterval(1000)
        self._test_elapsed_timer.timeout.connect(self._update_test_timer)
        self._test_elapsed_timer.start()

    def _update_test_timer(self) -> None:
        """更新自動測試秒數顯示：已經過 / 總時間（剩餘）。"""
        if not self._test_running:
            return
        elapsed = int(time.monotonic() - self._test_t0)
        total = self._test_total_s
        remain = max(0, total - elapsed)
        self.lbl_test_timer.setText(f"{elapsed} / {total} 秒　剩 {remain}s")

    def _stop_test_timer(self) -> None:
        if self._test_elapsed_timer is not None:
            self._test_elapsed_timer.stop()
            self._test_elapsed_timer = None

    def on_stop_test(self) -> None:
        if self.auto_test_thread is not None:
            self.lbl_test_status.setText("停止中…")
            self.auto_test_thread.stop()

    def on_test_progress(self, msg: str) -> None:
        self.lbl_test_status.setText(msg)

    def on_test_sampled(self, n: int) -> None:
        self.status.showMessage(f"取樣中… {n} 筆", 2000)

    def on_test_row(self, row) -> None:
        """收到一筆取樣 → 累積供即時重繪 (實際重繪由計時器節流)。"""
        self._live_rows.append(row)

    def _redraw_live_plot(self) -> None:
        """測試 / 手動紀錄進行中，每秒把目前累積的取樣重繪成曲線 (含 PF) 顯示。"""
        if not (self._test_running or self._manual_recording) or not self._live_rows:
            return
        # 沒有新資料就不重畫
        if len(self._live_rows) <= self._live_plotted_count:
            return
        try:
            png = render_auto_test_png(list(self._live_rows))
        except Exception:
            return  # 繪圖失敗不可影響測試本身
        pix = QPixmap()
        if not pix.loadFromData(png):
            return
        self._test_pixmap = pix   # 讓 resizeEvent 也能依新尺寸重新縮放
        self.lbl_plot.setPixmap(pix.scaled(
            self.lbl_plot.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self._live_plotted_count = len(self._live_rows)

    def _stop_live_plot_timer(self) -> None:
        if self._live_plot_timer is not None:
            self._live_plot_timer.stop()
            self._live_plot_timer = None

    def _set_test_relay(self, on: bool) -> None:
        """測試開始/結束時連動 PLC 繼電器 (預設 MR101)。

        PLC 未連線則略過 (測試只驅動 PEL，PLC 連動屬選用)；失敗只在狀態列提示，
        不中斷測試流程。與伺服按鈕共用 plc_lock 序列化 PLC socket。
        """
        if self.plc_driver is None:
            return
        try:
            with self.plc_lock:
                if on:
                    self.plc_driver.set_relay(self.test_relay)
                else:
                    self.plc_driver.reset_relay(self.test_relay)
        except KVError as e:
            self.status.showMessage(
                f"[測試繼電器 {'ON' if on else 'OFF'} 失敗] {e}", 5000
            )

    def _set_gentest_relay(self, on: bool) -> None:
        """自動測試開始/結束時連動發電機測試繼電器 (預設 MR103)。

        PLC 未連線則略過 (連動屬選用)；失敗只在狀態列提示，不中斷測試流程。
        與手動發電機測試按鈕共用 plc_lock，並同步更新狀態燈。
        """
        if self.plc_driver is None:
            return
        try:
            with self.plc_lock:
                if on:
                    self.plc_driver.set_relay(self.generator_test_relay)
                else:
                    self.plc_driver.reset_relay(self.generator_test_relay)
            self._set_gentest_state(on)
        except KVError as e:
            self.status.showMessage(
                f"[發電機測試 {'ON' if on else 'OFF'} 失敗] {e}", 5000
            )

    def _update_speed_control_visibility(self) -> None:
        """依 DM7002 (速度控制來源) 顯示/隱藏手動控制區的速度設定列。

        僅「自動控制速度」(DM7002=1) 時顯示 — 此時速度由 UI 命令 (DM102) 控制；
        「手動控制速度」時速度由實體手動端控制，UI 設定無意義故隱藏。
        """
        if hasattr(self, "_row_manual_rpm"):
            self._row_manual_rpm.setVisible(self._speed_auto)

    def on_apply_manual_rpm(self) -> None:
        """手動控制區「速度設定」鈕：把 RPM 直接寫入 PLC DM102 (需伺服 ON)。"""
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法設定速度", 4000)
            return
        if not self._servo_on:
            self.status.showMessage("伺服未 ON，無法設定速度", 4000)
            return
        rpm = self.spin_manual_rpm.value()
        self._write_rpm_setpoint(rpm)   # 直接寫 RPM 並走 plc_lock (失敗會自報)
        self.status.showMessage(f"速度設定 {rpm} RPM 已送出", 3000)

    def on_stop_manual_rpm(self) -> None:
        """手動控制區「停止」鈕：只把速度命令歸 0 (DM102=0)，保留輸入框設定值 (需伺服 ON)。"""
        if self.plc_driver is None:
            self.status.showMessage("PLC 未連線，無法設定速度", 4000)
            return
        if not self._servo_on:
            self.status.showMessage("伺服未 ON，無法設定速度", 4000)
            return
        self._write_rpm_setpoint(0)     # 直接寫 0 RPM (輸入框不動)
        self.status.showMessage("速度已停止", 3000)

    def _rpm_to_dm102(self, rpm) -> int:
        """把設定轉速(RPM)轉成 DM102 要寫的整數值。

        DM102 直接存 RPM (輸出制，轉速表同單位)，故不再換算百分比 —
        只夾在 0..rpm_full_scale 並取整數。PLC 端把此 RPM 換算成類比 (4000 分割)。
        """
        full = self.rpm_full_scale if self.rpm_full_scale > 0 else 6000.0
        return max(0, min(int(round(full)), int(round(float(rpm)))))

    def _write_rpm_setpoint(self, rpm) -> None:
        """把設定轉速(RPM)直接寫入 PLC DM102 (不換算百分比)。

        PLC 未連線則略過 (轉速連動屬選用)；失敗只在狀態列提示，不中斷測試。
        與伺服/測試繼電器共用 plc_lock 序列化 PLC socket。
        """
        if self.plc_driver is None:
            return
        try:
            with self.plc_lock:
                self.plc_driver.write_word(self.rpm_command_device, self._rpm_to_dm102(rpm))
        except (KVError, ValueError) as e:
            self.status.showMessage(f"[轉速命令寫入失敗] {e}", 5000)

    def _apply_stage_rpm(self, rpm):
        """供 AutoTestThread 在工作緒中逐階段寫轉速 (模式二)，回傳錯誤字串或 None。

        與 _write_rpm_setpoint 同樣直接寫 RPM 並走 plc_lock，但**不觸碰任何
        GUI 元件** — 錯誤以字串回傳，由執行緒透過 progress 信號送回主緒顯示
        (跨執行緒碰 Qt 元件不安全)。PLC 未連線視為無連動，回傳 None。
        """
        if self.plc_driver is None:
            return None
        try:
            with self.plc_lock:
                self.plc_driver.write_word(self.rpm_command_device, self._rpm_to_dm102(rpm))
            return None
        except (KVError, ValueError) as e:
            return str(e)

    def _finalize_test(self) -> None:
        self._test_running = False
        self.auto_test_thread = None
        self._stop_live_plot_timer()
        self._stop_test_timer()
        self.lbl_test_status.setStyleSheet("color:#555;")   # 結束 → 文字回中性色
        # 連動 PLC：流程結束 → 轉速命令 DM102 歸 0、MR101 OFF、發電機測試 MR103 OFF
        self._write_rpm_setpoint(0)
        self._set_test_relay(False)
        self._set_gentest_relay(False)
        self._refresh_manual_enabled()
        self.btn_disconnect.setEnabled(self.load is not None)

    def on_test_failed(self, msg: str) -> None:
        self._finalize_test()
        self.lbl_test_status.setText(f"測試失敗：{msg}")
        self.status.showMessage(f"[自動測試] {msg}", 8000)

    def on_test_finished(self, rows) -> None:
        self._finalize_test()
        if not rows:
            self.lbl_test_status.setText("測試結束（無資料）")
            return
        self._save_record(rows, "自動測試 — 階段負載時間曲線")

    def _save_record(self, rows, plot_title: str) -> None:
        """把一批取樣 rows 存成 CSV + 曲線圖 (同一資料夾、檔名日期+時間)。

        自動測試與手動紀錄共用；rows 欄位需含
        timestamp/elapsed_s/stage/set_current_A/V/I/P/temperature/rpm/power_factor。
        """
        out = self.cfg["output"]
        tag = timestamp_tag()   # 檔名 = 日期+時間 (例 20260609_153012)
        save_dir = Path(self.save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        csv_path = save_dir / f"{tag}.csv"
        plot_path = save_dir / f"{tag}.{out['plot_format']}"
        fields = ["timestamp", "elapsed_s", "stage", "set_current_A",
                  "V", "I", "P", "temperature", "rpm", "power_factor"]
        try:
            with CSVLogger(csv_path, fields, encoding=out["csv_encoding"]) as csvlog:
                for r in rows:
                    csvlog.write({
                        "timestamp": r["timestamp"],
                        "elapsed_s": f'{r["elapsed_s"]:.2f}',
                        "stage": r["stage"],
                        "set_current_A": f'{r["set_current_A"]:.3f}',
                        "V": f'{r["V"]:.4f}',
                        "I": f'{r["I"]:.4f}',
                        "P": f'{r["P"]:.4f}',
                        "temperature": "" if r["temperature"] is None else f'{r["temperature"]:.1f}',
                        "rpm": "" if r["rpm"] is None else f'{r["rpm"]:.0f}',
                        "power_factor": "" if r["power_factor"] is None else f'{r["power_factor"]:.4f}',
                    })
            plot_auto_test(rows, plot_path, title=plot_title, dpi=out["plot_dpi"])
            self._show_test_plot(plot_path)
            self.lbl_test_status.setText(f"完成：{len(rows)} 筆 → {csv_path.name}")
            self.status.showMessage(f"✔ 已存 {csv_path.name} 與 {plot_path.name}", 8000)
        except Exception as e:
            self.lbl_test_status.setText(f"輸出失敗：{e}")
            self.status.showMessage(f"輸出失敗: {e}", 8000)

    def _show_test_plot(self, path) -> None:
        pix = QPixmap(str(path))
        if pix.isNull():
            return
        self._test_pixmap = pix
        self.lbl_plot.setPixmap(pix.scaled(
            self.lbl_plot.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _refresh_manual_enabled(self) -> None:
        """依連線狀態 / 是否測試中，更新手動與測試按鈕。

        自動測試或手動紀錄進行中鎖定設定/手動控制；DM7002 (速度手動/自動) 不參與按鈕鎖定。
        """
        if not hasattr(self, "btn_servo_on"):
            return
        # 設定區：自動測試或手動紀錄進行中皆鎖定 (模式/電流/轉速/各階段值不可改)，
        # 避免進行途中改設定造成序列與紀錄不一致。
        if hasattr(self, "settings_box"):
            self.settings_box.setEnabled(
                not self._test_running and not self._manual_recording
            )
        # 手動控制區：只在自動測試進行中鎖定 (自動序列獨占負載機)；
        # 手動紀錄為被動記錄器，需保留手動控制讓操作員邊操作邊記錄。
        if hasattr(self, "control_box"):
            self.control_box.setEnabled(not self._test_running)
        # 手動控制框內元件還需「手動啟用」(MR102/DM7005) 為 ON 才可用。
        # 註：DM7002 (速度手動/自動控制) 已與按鈕鎖定脫鉤，不再參與此判斷。
        manual = not self._test_running and self._manual_enable_on
        pel = self._pel_ready and manual       # 負載機手動 (電流/LOAD)
        plc = self._plc_ready and manual       # PLC 手動 (伺服)
        self.spin_current.setEnabled(pel)
        self.btn_apply.setEnabled(pel)
        self.btn_load_on.setEnabled(pel)
        self.btn_load_off.setEnabled(pel)
        self.btn_servo_on.setEnabled(plc)
        self.btn_servo_off.setEnabled(plc)
        self.btn_gentest_on.setEnabled(plc)
        self.btn_gentest_off.setEnabled(plc)
        # 異常 RESET 屬恢復動作：只要 PLC 連線就可按 (不受測試中鎖定)
        self.btn_alarm_reset.setEnabled(self._plc_ready)
        # 手動啟用 (MR102)：模式切換動作，只要 PLC 連線就可切 (不受測試中鎖定)
        if hasattr(self, "btn_manual_enable"):
            self.btn_manual_enable.setEnabled(self._plc_ready)
        # 速度設定 (DM102)：需 PLC 連線且「伺服 ON」才可用，否則停用、不可寫 DM102
        if hasattr(self, "btn_apply_rpm"):
            speed_ok = self._plc_ready and self._servo_on
            self.spin_manual_rpm.setEnabled(speed_ok)
            self.btn_apply_rpm.setEnabled(speed_ok)
            self.btn_stop_rpm.setEnabled(speed_ok)
        if hasattr(self, "btn_start_test"):
            self.btn_start_test.setEnabled(
                self._prereqs_ok()
                and not self._test_running and not self._manual_recording
            )
            self.btn_stop_test.setEnabled(self._test_running)
        if hasattr(self, "btn_manual_record"):
            # 手動紀錄：負載機已連線且未在自動測試中即可 (紀錄中保持可按以便停止)
            self.btn_manual_record.setEnabled(
                self._pel_ready and not self._test_running
            )
        self._update_test_prereqs()

    def _prereqs_ok(self) -> bool:
        """自動測試所有前置條件都就緒時回傳 True（與 _update_test_prereqs 判斷一致）。"""
        gpm_enabled = bool((self.cfg.get("power_meter") or {}).get("enabled", False))
        return (
            self._pel_ready
            and self._plc_ready
            and self._servo_on
            and self._speed_auto
            and not self._alarm_active
            and (not gpm_enabled or self._gpm_ready)
        )

    def _update_test_prereqs(self) -> None:
        """更新自動測試前置條件提醒：逐項檢查未就緒的條件並列在提醒列。

        測試進行中不顯示提醒 (此時設定/控制皆已鎖定)；全部就緒則顯示綠色「條件齊全」。
        功率計屬選用 (config 未啟用則不檢查；PF 缺漏圖表會標「無資料」)。
        """
        if not hasattr(self, "lbl_test_prereq"):
            return
        if self._test_running:
            self.lbl_test_prereq.setVisible(False)
            return

        gpm_enabled = bool((self.cfg.get("power_meter") or {}).get("enabled", False))
        issues = []
        if not self._pel_ready:
            issues.append("負載機未連線")
        if not self._plc_ready:
            issues.append("PLC 未連線")
        else:
            # PLC 已連線才有意義判斷以下細項
            if not self._servo_on:
                issues.append("伺服未 ON")
            if not self._speed_auto:
                issues.append("速度未切至自動")
            if self._alarm_active:
                issues.append("異常發生中 (請先 RESET)")
        if gpm_enabled and not self._gpm_ready:
            issues.append("功率計未連線")

        if issues:
            self.lbl_test_prereq.setText("⚠ 開始測試前請確認： " + "、".join(issues))
            self.lbl_test_prereq.setStyleSheet(
                "color:#b35900; background:#fff3e0; "
                "border:1px solid #ffb74d; border-radius:4px; padding:6px;"
            )
        else:
            self.lbl_test_prereq.setText("✓ 條件齊全，可開始測試")
            self.lbl_test_prereq.setStyleSheet(
                "color:#2ca02c; background:#f1faf1; "
                "border:1px solid #a5d6a7; border-radius:4px; padding:6px;"
            )
        self.lbl_test_prereq.setVisible(True)

    # ---------- 連線 / 中斷 ----------
    def on_connect(self) -> None:
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            self.status.showMessage("Port 必須是整數", 4000)
            return

        inst = self.cfg["instrument"]
        try:
            self.load = PEL5000C(
                ip=self.ip_edit.text().strip(),
                port=port,
                timeout=inst["timeout"],
                command_delay=inst["command_delay"],
                read_buffer=inst["read_buffer"],
            )
            self.load.connect()
            idn = self.load.idn()
            # 預設切到 CC 並下達目前 spin 的設定值
            self.load.set_mode_cc(self.spin_current.value())
        except PELError as e:
            self.status.showMessage(f"連線失敗: {e}", 6000)
            self.load = None
            return

        self.lbl_conn_status.setText("● 已連線")
        self.lbl_conn_status.setStyleSheet("color: green; font-weight: bold;")
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(True)
        self.ip_edit.setEnabled(False)
        self.port_edit.setEnabled(False)
        self._pel_ready = True
        self._refresh_manual_enabled()
        self.status.showMessage(f"已連線: {idn}")

        self.poller = PollingThread(self.load, self.io_lock, hz=5.0)
        self.poller.measured.connect(self.on_measured)
        self.poller.state_updated.connect(self.on_state_updated)
        self.poller.failed.connect(self.on_poller_failed)
        self.poller.start()

    def on_disconnect(self) -> None:
        # 若正在手動紀錄，先停止並存檔 (負載機要斷線了，數值來源沒了)
        if self._manual_recording:
            self._stop_manual_record()
        if self.poller is not None:
            self.poller.stop()
            self.poller = None
        if self.load is not None:
            try:
                # disconnect() 會送 LOAD OFF + LOCAL — 硬體安全網
                self.load.disconnect()
            except Exception:
                pass
            self.load = None

        self.lbl_conn_status.setText("● 未連線")
        self.lbl_conn_status.setStyleSheet("color: #d62728; font-weight: bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.ip_edit.setEnabled(True)
        self.port_edit.setEnabled(True)
        self._pel_ready = False
        self._refresh_manual_enabled()
        self.lbl_v.setText("---")
        self.lbl_i.setText("---")
        self.lbl_p.setText("---")
        # 溫度獨立通道 (RS485)，不跟 PEL-5000C 連線連動；保留現值
        self.lbl_load_state.setText("● ---")
        self.lbl_load_state.setStyleSheet("color: gray;")
        self.lbl_mode.setText("---")
        self.lbl_mode.setStyleSheet("color: gray;")
        self.status.showMessage("已中斷連線")

    # ---------- Slots ----------
    def on_measured(self, v: float, i: float, p: float) -> None:
        self.lbl_v.setText(f"{v:8.3f}")
        self.lbl_i.setText(f"{i:8.3f}")
        self.lbl_p.setText(f"{p:8.2f}")
        self._last_v, self._last_i, self._last_p = v, i, p   # 供手動紀錄取樣

    def on_state_updated(self, load_on: bool, mode: str) -> None:
        if load_on:
            self.lbl_load_state.setText("● LOAD ON")
            self.lbl_load_state.setStyleSheet("color: #2ca02c; font-weight: bold;")
        else:
            self.lbl_load_state.setText("● LOAD OFF")
            self.lbl_load_state.setStyleSheet("color: #888;")
        self.lbl_mode.setText(mode)
        self.lbl_mode.setStyleSheet("color: #1f77b4; font-weight: bold;")

    def on_poller_failed(self, msg: str) -> None:
        self.status.showMessage(f"[輪詢中止] {msg}", 8000)
        self.on_disconnect()

    def on_apply_current(self) -> None:
        if self.load is None:
            return
        value = self.spin_current.value()
        try:
            with self.io_lock:
                # 直接重新下 CC:HIGH，不重切模式
                self.load.write(f"CC:HIGH {value:.4f}")
        except PELError as e:
            self.status.showMessage(f"[錯誤] {e}", 5000)
            return
        self.status.showMessage(f"已套用 CC 電流 = {value:.1f} A", 3000)

    def on_load_on(self) -> None:
        if self.load is None:
            return
        try:
            with self.io_lock:
                self.load.load_on()
        except PELError as e:
            self.status.showMessage(f"[錯誤] {e}", 5000)
            return
        if self.poller is not None:
            self.poller.request_state_refresh()
        self.status.showMessage("LOAD ON", 3000)

    def on_load_off(self) -> None:
        if self.load is None:
            return
        try:
            with self.io_lock:
                self.load.load_off()
        except PELError as e:
            self.status.showMessage(f"[錯誤] {e}", 5000)
            return
        if self.poller is not None:
            self.poller.request_state_refresh()
        self.status.showMessage("LOAD OFF", 3000)

    def resizeEvent(self, ev) -> None:
        # 視窗縮放時，重新依新尺寸縮放已顯示的測試曲線圖
        super().resizeEvent(ev)
        if self._test_pixmap is not None and not self._test_pixmap.isNull():
            self.lbl_plot.setPixmap(self._test_pixmap.scaled(
                self.lbl_plot.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    # ---------- 收尾 ----------
    def closeEvent(self, ev) -> None:
        self._stop_live_plot_timer()
        self._stop_test_timer()
        if self.auto_test_thread is not None:
            self.auto_test_thread.stop()
            self.auto_test_thread.wait(3000)
            # 關閉時測試仍在跑 → 趁 PLC 尚未斷線把轉速 DM102 歸 0、MR101 OFF、發電機測試 OFF
            self._write_rpm_setpoint(0)
            self._set_test_relay(False)
            self._set_gentest_relay(False)
        self.on_disconnect()
        self._stop_plc_monitor()
        self._stop_gpm_monitor()
        super().closeEvent(ev)


def main() -> int:
    app = QApplication(sys.argv)
    try:
        win = MainWindow()
    except FileNotFoundError as e:
        # 多半是找不到 config.yaml (打包後 config 須放在 exe 旁邊)
        QMessageBox.critical(
            None, "啟動失敗 — 找不到設定檔",
            "找不到設定檔 config.yaml。\n\n"
            "請確認 config.yaml 與本程式 (exe) 放在「同一個資料夾」。\n"
            f"\n詳細：{e}",
        )
        return 1
    except Exception as e:
        # 其餘啟動錯誤也跳明確訊息，避免在新電腦上無聲秒退難以排查
        import traceback
        QMessageBox.critical(
            None, "啟動失敗",
            f"程式啟動時發生錯誤：\n\n{e}\n\n{traceback.format_exc()}",
        )
        return 1
    win.showMaximized()   # 視窗開到滿版，空間較充足
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
