"""
GW Instek GPM-8310 Power Meter — Python LAN/TCP driver.

通訊方式: TCP Socket (出廠預設 Socket Port 23)
協定: SCPI (Standard Commands for Programmable Instruments)
終止符: 送出 '\\n' (LF)，接收 '\\r\\n' (CRLF) — LAN 介面 EOL 固定 CR+LF

本驅動聚焦在「讀取功率因數 (Power Factor)」這一項客戶端規格需求。
功率因數在 GPM-8310 的功能代碼是 LAMBda (面板顯示 λ，指示燈 [PF])，
透過 :NUMeric 數值輸出介面查詢。

⚠ 觀念提醒: 純 DC 系統的功率因數恆為 1 (沒有相位差、沒有諧波)，PF 只有在
   交流側量測才有物理意義。本驅動不臆測接線位置，僅忠實把儀器回傳的 λ 讀回。
   量測側 (AC / DC / ACDC) 由呼叫端視現場接線以 set_input_mode() 決定，
   未指定時不更動儀器既有設定。

使用範例:
    from gpm8310 import GPM8310

    with GPM8310("192.168.0.100", port=23) as meter:
        meter.configure_power_factor()   # 設定 ITEM1 = 功率因數
        pf = meter.read_power_factor()
        print(f"功率因數 = {pf:.4f}")
"""
from __future__ import annotations

import socket
import time
import logging
import math
from typing import Optional

from .exceptions import (
    GPMConnectionError,
    GPMCommandError,
    GPMTimeoutError,
)

log = logging.getLogger(__name__)


class GPM8310:
    """GW Instek GPM-8310 功率計 LAN 驅動類別。"""

    VALID_INPUT_MODES = ("AC", "DC", "ACDC")
    TERM_TX = "\n"
    TERM_RX = "\r\n"

    def __init__(
        self,
        ip: str,
        port: int = 23,
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
            raise GPMConnectionError(
                f"無法連線到 {self.ip}:{self.port} — {e}"
            ) from e
        log.info("Connected to GPM-8310 @ %s:%d", self.ip, self.port)
        # 進入遠端模式；關閉回應標頭，讓查詢只回純數值好解析
        self.write(":COMMunicate:REMote ON")
        self.write(":COMMunicate:HEADer OFF")
        self.write(":NUMeric:FORMat ASCii")

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            # 切回本機 (不動 LOCKout，面板 Local 鍵仍可用)
            self.write(":COMMunicate:REMote OFF")
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None
            self._connected = False
            log.info("Disconnected from GPM-8310")

    def __enter__(self) -> "GPM8310":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ----- 低階通訊 -----
    def _ensure(self) -> socket.socket:
        if not self._connected or self._sock is None:
            raise GPMConnectionError("尚未連線，請先呼叫 connect() 或使用 with 區塊")
        return self._sock

    def write(self, cmd: str) -> None:
        """送出 SCPI 指令 (不等回應)。"""
        s = self._ensure()
        payload = (cmd.strip() + self.TERM_TX).encode("ascii")
        try:
            s.sendall(payload)
        except OSError as e:
            raise GPMConnectionError(f"送出指令失敗: {e}") from e
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
            raise GPMTimeoutError(f"查詢逾時: {cmd}") from e
        except OSError as e:
            raise GPMConnectionError(f"讀取回應失敗: {e}") from e
        if not data:
            raise GPMConnectionError("連線已被儀器關閉 (空回應)")
        text = data.decode("ascii", errors="replace").strip("\r\n \t")
        log.debug("<<< %s", text)
        return text

    # ----- 系統 / 識別 -----
    def idn(self) -> str:
        return self.query("*IDN?")

    def reset(self) -> None:
        self.write("*RST")

    def clear_status(self) -> None:
        self.write("*CLS")

    # ----- 輸入模式 -----
    def set_input_mode(self, mode: str) -> None:
        """設定量測輸入模式 (AC / DC / ACDC)，依現場接線決定。"""
        m = mode.upper()
        if m not in self.VALID_INPUT_MODES:
            raise GPMCommandError(
                f"未知輸入模式 {mode!r}，可用: {self.VALID_INPUT_MODES}"
            )
        self.write(f":INPut:MODE {m}")

    # ----- 功率因數 -----
    def configure_power_factor(self) -> None:
        """設定數值輸出第 1 項為功率因數 (LAMBda)，後續用 read_power_factor() 讀回。

        GPM-8310 沒有 MEAS:PF? 之類的捷徑，功率因數須透過 :NUMeric 介面取得。
        這裡把 ITEM1 指定成 LAMBda、輸出項目數設為 1，讓查詢最單純。
        """
        self.write(":NUMeric:NORMal:ITEM1 LAMBda")
        self.write(":NUMeric:NORMal:NUMber 1")

    def read_power_factor(self) -> float:
        """讀取功率因數 (λ / PF)。

        需先呼叫過 configure_power_factor() (ITEM1 設為 LAMBda)。
        回傳浮點數；DC 系統下通常 ≈ 1.0。

        儀器於無資料時回 NAN、超量程時回 INF — 兩者皆轉成例外，
        讓呼叫端能與「實際數值」明確區分。
        """
        raw = self.query(":NUMeric:NORMal:VALue? 1")
        token = raw.split(",")[0].strip()
        if not token:
            raise GPMCommandError("VALue? 回應為空")
        up = token.upper()
        if "NAN" in up:
            raise GPMCommandError("功率因數無資料 (NAN) — 請確認接線與量測模式")
        if "INF" in up:
            raise GPMCommandError("功率因數超量程 (INF) — 請確認電壓/電流量程")
        try:
            value = float(token)
        except ValueError as e:
            raise GPMCommandError(f"功率因數回應無法解析: {token!r}") from e
        if math.isnan(value) or math.isinf(value):
            raise GPMCommandError(f"功率因數回應異常: {token!r}")
        return value
