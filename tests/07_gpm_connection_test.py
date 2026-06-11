"""
測試 07 — GPM-8310 功率計連線 + 讀功率因數測試。

目的：
  - 驗證 GPM-8310 LAN 連線能正常打開
  - 讀取 *IDN? 確認儀器型號
  - (選用) 依 config 的 power_meter.input_mode 設定量測模式
  - 設定數值輸出項目為功率因數 (LAMBda)，連續讀幾次 PF
  - 此測試不啟動任何負載，純讀值，安全

前置：
  config.yaml 的 power_meter.ip / port 需設成現場 GPM-8310 實際位址。
  (出廠預設 IP 192.168.0.100、Socket Port 23)

執行：
  python tests/07_gpm_connection_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 讓本檔不需安裝即可 import 上層套件
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpm8310 import GPM8310, GPMError
from utils import load_config, setup_logger


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=cfg["logging"]["log_to_file"],
        log_to_console=cfg["logging"]["log_to_console"],
    )

    pm = cfg.get("power_meter") or {}
    if not pm:
        log.error("✘ config.yaml 找不到 power_meter 區段")
        return 1

    log.info("嘗試連線 GPM-8310 %s:%s ...", pm.get("ip"), pm.get("port", 23))

    try:
        with GPM8310(
            ip=pm["ip"],
            port=int(pm.get("port", 23)),
            timeout=float(pm.get("timeout", 5.0)),
            command_delay=float(pm.get("command_delay", 0.05)),
            read_buffer=int(pm.get("read_buffer", 4096)),
        ) as meter:
            log.info("✔ TCP 連線成功")
            idn = meter.idn()
            log.info("✔ *IDN? = %s", idn)

            mode = str(pm.get("input_mode", "") or "").strip()
            if mode:
                meter.set_input_mode(mode)
                log.info("✔ 已設定輸入模式 = %s", mode.upper())
            else:
                log.info("（未指定 input_mode，沿用儀器既有量測模式）")

            meter.configure_power_factor()
            log.info("✔ 已將數值輸出第 1 項設為功率因數 (LAMBda)")

            for n in range(5):
                pf = meter.read_power_factor()
                log.info("讀值 %d/5: 功率因數 PF = %.4f", n + 1, pf)
                time.sleep(0.5)

            log.info("✔ 連線測試通過，準備離開遠端模式")
        log.info("✔ 已切回 LOCAL 並關閉連線")
        return 0
    except GPMError as e:
        log.error("✘ 測試失敗: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
