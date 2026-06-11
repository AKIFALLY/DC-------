"""
Keyence KV 系列 PLC — 上位鏈路 (Host-Link / 上位リンク) TCP/IP 驅動。

通訊方式: TCP Socket
協定: Keyence 上位鏈路 ASCII 無手順指令
終止符: 送出 '\\r' (CR)，接收 '\\r\\n' (CR+LF)
編碼: ASCII

本驅動實作上位鏈路的基本指令:
    讀字組      RD  <device>.<fmt>
    連續讀      RDS <device>.<fmt> <count>
    寫字組      WR  <device>.<fmt> <value>
    連續寫      WRS <device>.<fmt> <count> <v1> <v2> ...
    繼電器 ON   ST  <device>
    繼電器 OFF  RS  <device>

格式碼 (fmt):
    U  無號 16-bit 十進位 (預設)
    S  有號 16-bit 十進位
    H  16-bit 十六進位
    D  無號 32-bit 十進位 (佔 2 個字組)
    L  有號 32-bit 十進位 (佔 2 個字組)

裝置範例: DM0, DM10100, R000, R2000, MR100, CR1000, T0, C0, W0, EM0 ...

使用範例:
    from keyence import KeyenceKV

    with KeyenceKV("192.168.0.10", port=8500) as plc:
        rpm = plc.read_word("DM10100")        # 讀轉速回授 (位址依現場)
        plc.write_word("DM10000", 1500)       # 寫轉速命令
        plc.set_relay("R2000")                # 啟動繼電器 ON
        plc.reset_relay("R2000")              # OFF
"""
from __future__ import annotations

import socket
import time
import logging
from typing import List, Optional, Sequence

from .exceptions import (
    KVConnectionError,
    KVCommandError,
    KVTimeoutError,
)

log = logging.getLogger(__name__)

# 上位鏈路錯誤回應對照
_KV_ERRORS = {
    "E0": "裝置編號錯誤 (device number error)",
    "E1": "指令錯誤 (command error)",
    "E2": "資料錯誤 (data error)",
    "E4": "寫入禁止 / 程式模式中無法寫入 (write protected)",
    "E5": "未支援的指令 (unsupported)",
    "E6": "其他錯誤",
}


class KeyenceKV:
    """Keyence KV 系列 PLC 上位鏈路 (TCP) 驅動類別。"""

    VALID_FORMATS = ("U", "S", "H", "D", "L")
    TERM_TX = "\r"
    TERM_RX = "\n"  # 回應以 CR+LF 結尾，讀到 LF 即收完一行

    def __init__(
        self,
        ip: str,
        port: int = 8500,
        timeout: float = 2.0,
        command_delay: float = 0.0,
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
            raise KVConnectionError(
                f"無法連線到 {self.ip}:{self.port} — {e}"
            ) from e
        log.info("Connected to Keyence KV @ %s:%d", self.ip, self.port)

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None
            self._connected = False
            log.info("Disconnected from Keyence KV")

    def __enter__(self) -> "KeyenceKV":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ----- 低階通訊 -----
    def _ensure(self) -> socket.socket:
        if not self._connected or self._sock is None:
            raise KVConnectionError("尚未連線，請先呼叫 connect() 或使用 with 區塊")
        return self._sock

    def _recv_line(self, s: socket.socket) -> str:
        """讀取一行回應 (累積到收到 LF 為止)。"""
        buf = bytearray()
        while True:
            try:
                chunk = s.recv(self.read_buffer)
            except socket.timeout as e:
                raise KVTimeoutError("回應逾時") from e
            except OSError as e:
                raise KVConnectionError(f"讀取回應失敗: {e}") from e
            if not chunk:
                raise KVConnectionError("連線已被 PLC 關閉 (空回應)")
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        return buf.decode("ascii", errors="replace").strip("\r\n \t")

    def _command(self, cmd: str) -> str:
        """送出一筆上位鏈路指令並回傳回應字串；遇 E? 錯誤碼則丟例外。"""
        s = self._ensure()
        payload = (cmd.strip() + self.TERM_TX).encode("ascii")
        try:
            s.sendall(payload)
        except OSError as e:
            raise KVConnectionError(f"送出指令失敗: {e}") from e
        log.debug(">>> %s", cmd)

        resp = self._recv_line(s)
        log.debug("<<< %s", resp)

        if self.command_delay > 0:
            time.sleep(self.command_delay)

        # 錯誤回應: E0 / E1 / E4 ...
        if len(resp) == 2 and resp[0] == "E" and resp[1].isdigit():
            msg = _KV_ERRORS.get(resp, "未知錯誤")
            raise KVCommandError(f"PLC 回應錯誤 {resp}: {msg} (指令: {cmd})")
        return resp

    # ----- 格式輔助 -----
    @staticmethod
    def _suffix(fmt: str) -> str:
        f = fmt.upper()
        if f not in KeyenceKV.VALID_FORMATS:
            raise KVCommandError(f"未知格式碼 {fmt!r}，可用: {KeyenceKV.VALID_FORMATS}")
        return f

    @staticmethod
    def _parse_value(token: str, fmt: str) -> int:
        return int(token, 16) if fmt.upper() == "H" else int(token)

    # ----- 讀取 -----
    def read_word(self, device: str, fmt: str = "U") -> int:
        """讀取單一裝置 (RD)。fmt='D'/'L' 時讀 32-bit (佔 2 個字組)。"""
        f = self._suffix(fmt)
        resp = self._command(f"RD {device}.{f}")
        token = resp.split()[0] if resp else ""
        if not token:
            raise KVCommandError(f"RD {device} 回應為空")
        return self._parse_value(token, f)

    def read_words(self, device: str, count: int, fmt: str = "U") -> List[int]:
        """連續讀取 count 個裝置 (RDS)。"""
        if count < 1:
            raise KVCommandError("count 必須 >= 1")
        f = self._suffix(fmt)
        resp = self._command(f"RDS {device}.{f} {count}")
        tokens = resp.split()
        if len(tokens) < count:
            raise KVCommandError(
                f"RDS {device} 回應數量不足: 期待 {count}, 收到 {len(tokens)} — {resp!r}"
            )
        return [self._parse_value(t, f) for t in tokens[:count]]

    def read_double(self, device: str, signed: bool = False) -> int:
        """讀取 32-bit 雙字組 (D=無號, L=有號)。"""
        return self.read_word(device, "L" if signed else "D")

    # ----- 寫入 -----
    def write_word(self, device: str, value: int, fmt: str = "U") -> None:
        """寫入單一裝置 (WR)，成功回 OK。"""
        f = self._suffix(fmt)
        val = format(int(value), "X") if f == "H" else str(int(value))
        resp = self._command(f"WR {device}.{f} {val}")
        if resp.upper() != "OK":
            raise KVCommandError(f"WR {device} 未回 OK: {resp!r}")

    def write_words(self, device: str, values: Sequence[int], fmt: str = "U") -> None:
        """連續寫入多個裝置 (WRS)，成功回 OK。"""
        if not values:
            raise KVCommandError("values 不可為空")
        f = self._suffix(fmt)
        if f == "H":
            vals = " ".join(format(int(v), "X") for v in values)
        else:
            vals = " ".join(str(int(v)) for v in values)
        resp = self._command(f"WRS {device}.{f} {len(values)} {vals}")
        if resp.upper() != "OK":
            raise KVCommandError(f"WRS {device} 未回 OK: {resp!r}")

    # ----- 繼電器 (bit) -----
    def set_relay(self, device: str) -> None:
        """繼電器 ON (ST)，成功回 OK。"""
        resp = self._command(f"ST {device}")
        if resp.upper() != "OK":
            raise KVCommandError(f"ST {device} 未回 OK: {resp!r}")

    def reset_relay(self, device: str) -> None:
        """繼電器 OFF (RS)，成功回 OK。"""
        resp = self._command(f"RS {device}")
        if resp.upper() != "OK":
            raise KVCommandError(f"RS {device} 未回 OK: {resp!r}")

    def read_relay(self, device: str) -> bool:
        """讀取繼電器狀態，回傳布林。"""
        return self.read_word(device, "U") != 0

    # ----- 識別 -----
    def cpu_model_code(self) -> str:
        """查詢連線 CPU 機種代碼 (?K)。回傳原始代碼字串 (不臆測對應機種)。"""
        return self._command("?K")
