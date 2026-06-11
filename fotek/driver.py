"""
FOTEK NT 系列溫度控制器 — RS-485 / Modbus RTU 驅動。

只實作「監看溫度 (PV)」功能 — 不支援寫入、自整定、警報設定等其餘功能。

通訊規格 (依 TC-NT-RS 手冊):
  - 介面: RS-485
  - 協定: Modbus RTU (RS=0) 或 ASCII (RS=1)；本驅動只實作 RTU
  - 功能碼: 0x03 (Read holding register)
  - PV 暫存器位址: 0x0041 (40066)
  - 小數點設定位址: 0x0019 (40026)  0=無小數、1=一位小數
  - 預設值需依 dP 設定除以 10 後才是實際溫度

使用範例:
    from fotek import FotekNT22

    with FotekNT22("COM3", baudrate=9600, slave_id=1) as tc:
        t = tc.read_temperature()
        print(f"目前溫度 = {t:.1f} °C")
"""
from __future__ import annotations

import logging
from typing import Optional

import serial  # pyserial

from .exceptions import (
    FotekConnectionError,
    FotekTimeoutError,
    FotekCommandError,
)

log = logging.getLogger(__name__)


# 暫存器位址 (Protocol base / Base 0)
REG_DECIMAL_POINT = 0x0019  # dP: 0=無小數、1=一位小數
REG_UNIT = 0x0018           # Unt: 0=℃, 1=℉
REG_PV = 0x0041             # PV: Process Value (現在溫度，-999~9999)


def _crc16(data: bytes) -> bytes:
    """Modbus RTU CRC-16 (polynomial 0xA001, little-endian output)。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, "little")


class FotekNT22:
    """FOTEK NT-22-RS (及同系列) 溫度控制器 — 只讀 PV。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        slave_id: int = 1,
        timeout: float = 1.0,
    ) -> None:
        if not (1 <= slave_id <= 0xFF):
            raise ValueError("slave_id 必須在 1..255")
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._scale: float = 1.0  # PV / _scale = 實際溫度；由 dP 暫存器決定

    # ----- 連線管理 -----
    def connect(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
        except serial.SerialException as e:
            raise FotekConnectionError(
                f"無法開啟 {self.port} @ {self.baudrate}bps — {e}"
            ) from e
        log.info("Connected to FOTEK NT @ %s, %dbps, id=%d",
                 self.port, self.baudrate, self.slave_id)

        # 讀一次 dP 設定，決定後續 PV 的縮放
        try:
            dp = self._read_holding_register(REG_DECIMAL_POINT)
            self._scale = 10.0 if dp == 1 else 1.0
            log.info("FOTEK dP=%d → scale=%.0f", dp, self._scale)
        except (FotekTimeoutError, FotekCommandError) as e:
            log.warning("讀取 dP 失敗，預設 scale=1.0: %s", e)
            self._scale = 1.0

    def disconnect(self) -> None:
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception:
                pass
            self._serial = None
            log.info("Disconnected from FOTEK NT")

    def __enter__(self) -> "FotekNT22":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ----- 對外 API -----
    def read_temperature(self) -> float:
        """讀取現在溫度 (PV)，依儀表 dP 設定自動縮放。

        Returns:
            float — 溫度值，單位由儀表 `Unt` 暫存器決定 (預設 ℃)。
        """
        raw = self._read_holding_register(REG_PV)
        # PV 是 signed 16-bit
        if raw >= 0x8000:
            raw -= 0x10000
        return raw / self._scale

    # ----- 低階 Modbus -----
    def _read_holding_register(self, address: int) -> int:
        if self._serial is None or not self._serial.is_open:
            raise FotekConnectionError("尚未連線，請先呼叫 connect()")

        req = bytes([
            self.slave_id,
            0x03,
            (address >> 8) & 0xFF,
            address & 0xFF,
            0x00, 0x01,        # 讀 1 個 register
        ])
        req += _crc16(req)

        try:
            self._serial.reset_input_buffer()
            self._serial.write(req)
        except serial.SerialException as e:
            raise FotekConnectionError(f"寫入失敗: {e}") from e

        log.debug(">>> %s", req.hex(" "))

        # 正常回應: [id][03][bytecount=2][hi][lo][crc_lo][crc_hi] = 7 bytes
        # 錯誤回應: [id][83][error][crc_lo][crc_hi] = 5 bytes
        # 先讀 3 bytes 判斷頭，再讀剩下的
        try:
            head = self._serial.read(3)
        except serial.SerialException as e:
            raise FotekConnectionError(f"讀取失敗: {e}") from e

        if len(head) < 3:
            raise FotekTimeoutError(
                f"讀取逾時 — 只收到 {len(head)} bytes: {head.hex(' ')}"
            )

        slave_resp, func, third = head[0], head[1], head[2]

        if slave_resp != self.slave_id:
            raise FotekCommandError(
                f"站號不符: 期待 {self.slave_id:02X}, 收到 {slave_resp:02X}"
            )

        if func == 0x83:
            # 錯誤回應: 還要再讀 2 bytes CRC
            tail = self._serial.read(2)
            full = head + tail
            log.debug("<<< %s", full.hex(" "))
            error_code = third
            err_msgs = {
                0x01: "指令錯誤",
                0x02: "位址錯誤",
                0x03: "資料長度錯誤",
                0x04: "資料值錯誤",
                0x05: "CRC 錯誤",
                0x06: "Parity 錯誤",
            }
            raise FotekCommandError(
                f"儀表回應錯誤 (code {error_code:02X}: {err_msgs.get(error_code, '未知')})"
            )

        if func != 0x03:
            raise FotekCommandError(f"未預期的功能碼: {func:02X}")

        byte_count = third
        if byte_count != 2:
            raise FotekCommandError(f"byte_count 異常: {byte_count} (期待 2)")

        # 再讀 data (2 bytes) + CRC (2 bytes)
        rest = self._serial.read(byte_count + 2)
        if len(rest) < byte_count + 2:
            raise FotekTimeoutError(
                f"讀取資料/CRC 逾時 — 只收到 {len(rest)}/{byte_count + 2} bytes"
            )
        full = head + rest
        log.debug("<<< %s", full.hex(" "))

        # 驗 CRC
        if _crc16(full[:-2]) != full[-2:]:
            raise FotekCommandError(f"CRC 錯誤: {full.hex(' ')}")

        return (rest[0] << 8) | rest[1]
