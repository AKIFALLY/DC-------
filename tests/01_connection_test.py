"""
測試 01 — 基本連線測試。

目的：
  - 驗證 LAN 連線能正常打開
  - 讀取 *IDN? 確認儀器型號
  - 切換到 REMOTE 後再回 LOCAL
  - 不啟動負載 (LOAD OFF)，最安全的第一步

執行：
  python tests/01_connection_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 讓本檔不需安裝即可 import 上層套件
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pel5000c import PEL5000C, PELError
from utils import load_config, setup_logger


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=cfg["logging"]["log_to_file"],
        log_to_console=cfg["logging"]["log_to_console"],
    )

    inst = cfg["instrument"]
    log.info("嘗試連線 %s:%s ...", inst["ip"], inst["port"])

    try:
        with PEL5000C(
            ip=inst["ip"],
            port=inst["port"],
            timeout=inst["timeout"],
            command_delay=inst["command_delay"],
            read_buffer=inst["read_buffer"],
        ) as load:
            log.info("✔ TCP 連線成功")
            idn = load.idn()
            log.info("✔ *IDN? = %s", idn)
            try:
                name = load.name()
                log.info("✔ NAME? = %s", name)
            except PELError as e:
                log.warning("NAME? 不支援或回應異常: %s", e)

            v = load.measure_voltage()
            i = load.measure_current()
            log.info("目前讀值: V=%.4f V, I=%.4f A (LOAD 仍為 OFF)", v, i)

            log.info("✔ 連線測試通過，準備離開遠端模式")
        log.info("✔ 已切回 LOCAL 並關閉連線")
        return 0
    except PELError as e:
        log.error("✘ 測試失敗: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
