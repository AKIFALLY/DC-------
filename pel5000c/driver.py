"""
PEL-5000C Series Electronic Load — Python LAN/TCP driver.

通訊方式: TCP Socket (預設 Port 4001)
協定: SCPI (Standard Commands for Programmable Instruments)
終止符: 送出 '\\n' (LF), 接收 '\\r\\n' (CRLF)

使用範例:
    from pel5000c import PEL5000C

    with PEL5000C("192.168.0.100") as load:
        load.set_mode_cc(5.0)
        load.load_on()
        v, i, p = load.measure_vip()
        load.load_off()
"""
from __future__ import annotations

import socket
import time
import logging
from typing import Optional, Tuple

from .exceptions import (
    PELConnectionError,
    PELCommandError,
    PELTimeoutError,
)

log = logging.getLogger(__name__)


class PEL5000C:
    """GW Instek PEL-5000C 電子負載 LAN 驅動類別。"""

    VALID_MODES = ("CC", "CR", "CV", "CP")
    TERM_TX = "\n"
    TERM_RX = "\r\n"

    def __init__(
        self,
        ip: str,
        port: int = 4001,
        timeout: float = 5.0,
        command_delay: float = 0.05,
        read_buffer: int = 4096,
    ) -> None:
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.command_delay = command_delay
        self.read_buffer = read_buffer
        self._sock: Optional[socket.socket] = None
        self._connected = False

    # ----- 連線管理 -----
    def connect(self) -> None:
        if self._connected:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.ip, self.port))
            self._sock = s
            self._connected = True
        except OSError as e:
            raise PELConnectionError(
                f"無法連線到 {self.ip}:{self.port} — {e}"
            ) from e
        log.info("Connected to PEL-5000C @ %s:%d", self.ip, self.port)
        # 進入遠端模式才能下指令
        self.write("REMOTE")

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self.write("LOAD OFF")
            self.write("LOCAL")
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None
            self._connected = False
            log.info("Disconnected from PEL-5000C")

    def __enter__(self) -> "PEL5000C":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ----- 低階通訊 -----
    def _ensure(self) -> socket.socket:
        if not self._connected or self._sock is None:
            raise PELConnectionError("尚未連線，請先呼叫 connect() 或使用 with 區塊")
        return self._sock

    def write(self, cmd: str) -> None:
        """送出 SCPI 指令 (不等回應)。"""
        s = self._ensure()
        payload = (cmd.strip() + self.TERM_TX).encode("ascii")
        try:
            s.sendall(payload)
        except OSError as e:
            raise PELConnectionError(f"送出指令失敗: {e}") from e
        log.debug(">>> %s", cmd)
        if self.command_delay > 0:
            time.sleep(self.command_delay)

    def query(self, cmd: str) -> str:
        """送出查詢指令並讀取一行回應。"""
        s = self._ensure()
        self.write(cmd)
        try:
            data = s.recv(self.read_buffer)
        except socket.timeout as e:
            raise PELTimeoutError(f"查詢逾時: {cmd}") from e
        except OSError as e:
            raise PELConnectionError(f"讀取回應失敗: {e}") from e
        if not data:
            raise PELConnectionError("連線已被儀器關閉 (空回應)")
        text = data.decode("ascii", errors="replace").strip("\r\n \t")
        log.debug("<<< %s", text)
        return text

    # ----- 系統 / 識別 -----
    def idn(self) -> str:
        return self.query("*IDN?")

    def name(self) -> str:
        return self.query("NAME?")

    def reset(self) -> None:
        self.write("*RST")

    def clear_status(self) -> None:
        self.write("*CLS")

    def beep(self) -> None:
        self.write("SYST:BEEP")

    # ----- 模式切換 -----
    def set_mode(self, mode: str) -> None:
        m = mode.upper()
        if m not in self.VALID_MODES:
            raise PELCommandError(f"未知模式 {mode}，可用: {self.VALID_MODES}")
        self.write(f"MODE {m}")

    def set_mode_cc(self, current_a: float) -> None:
        """定電流模式 (Constant Current)。"""
        self.set_mode("CC")
        self.write(f"CC:HIGH {float(current_a):.4f}")

    def set_mode_cr(self, resistance_ohm: float) -> None:
        """定電阻模式 (Constant Resistance)。"""
        self.set_mode("CR")
        self.write(f"CR:HIGH {float(resistance_ohm):.4f}")

    def set_mode_cv(self, voltage_v: float) -> None:
        """定電壓模式 (Constant Voltage)。"""
        self.set_mode("CV")
        self.write(f"CV:HIGH {float(voltage_v):.4f}")

    def set_mode_cp(self, power_w: float) -> None:
        """定功率模式 (Constant Power)。"""
        self.set_mode("CP")
        self.write(f"CP:HIGH {float(power_w):.4f}")

    # ----- 負載 ON / OFF -----
    def load_on(self) -> None:
        self.write("LOAD ON")

    def load_off(self) -> None:
        self.write("LOAD OFF")

    def short(self, on: bool) -> None:
        self.write("SHOR ON" if on else "SHOR OFF")

    def dynamic(self, on: bool) -> None:
        self.write("DYN ON" if on else "DYN OFF")

    def is_load_on(self) -> bool:
        """查詢負載狀態。手冊規定 0=ON, 1=OFF (反直覺，已在此包成布林)。"""
        raw = self.query("LOAD?").strip().upper()
        if raw in ("0", "ON"):
            return True
        if raw in ("1", "OFF"):
            return False
        raise PELCommandError(f"LOAD? 回應無法解析: {raw!r}")

    def get_mode(self) -> str:
        """查詢目前模式，回傳 'CC' / 'CR' / 'CV' / 'CP'。"""
        raw = self.query("MODE?").strip().upper()
        code_map = {"0": "CC", "1": "CR", "2": "CV", "3": "CP"}
        if raw in code_map:
            return code_map[raw]
        if raw in self.VALID_MODES:
            return raw
        raise PELCommandError(f"MODE? 回應無法解析: {raw!r}")

    # ----- 量測 -----
    def measure_voltage(self) -> float:
        return float(self.query("MEAS:VOLT?"))

    def measure_current(self) -> float:
        return float(self.query("MEAS:CURR?"))

    def measure_power(self) -> float:
        return float(self.query("MEAS:POW?"))

    def measure_vi(self) -> Tuple[float, float]:
        """一次取回 V, I (儀器原生支援，比兩次查詢同步性更佳)。"""
        raw = self.query("MEAS:VC?")
        # 回傳格式: "<V>,<I>" 或空白分隔，做寬容解析
        sep = "," if "," in raw else None
        parts = [p for p in raw.replace(",", " ").split() if p]
        if len(parts) < 2:
            raise PELCommandError(f"MEAS:VC? 回應格式錯誤: {raw!r}")
        return float(parts[0]), float(parts[1])

    def measure_vip(self) -> Tuple[float, float, float]:
        """回傳 (V, I, P)，P 由 V*I 計算 (避免三次查詢的時間差)。"""
        v, i = self.measure_vi()
        return v, i, v * i

    # ----- 安全檢查輔助 -----
    def assert_within(
        self,
        v: Optional[float] = None,
        i: Optional[float] = None,
        v_max: Optional[float] = None,
        i_max: Optional[float] = None,
    ) -> None:
        from .exceptions import PELSafetyError
        if v is not None and v_max is not None and v > v_max:
            self.load_off()
            raise PELSafetyError(f"電壓 {v:.3f}V 超過上限 {v_max:.3f}V")
        if i is not None and i_max is not None and i > i_max:
            self.load_off()
            raise PELSafetyError(f"電流 {i:.3f}A 超過上限 {i_max:.3f}A")
