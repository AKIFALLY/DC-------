"""
測試 06 — Keyence KV PLC 上位鏈路連線測試。

目的：
  - 驗證 TCP 上位鏈路連線 (預設 port 8500)
  - 嘗試查 CPU 機種代碼 (?K)
  - 一次讀回集中監控區 (config plc.monitor: DM7000 起 N 個 word)
  - 依 plc.signals 對應表印出已命名的訊號 (如溫度)
  - 異常碼 (DM7010) 逐 bit 依 plc.alarm_bits 解碼成訊息
  - 全程唯讀 — 不寫入、不動繼電器、不碰馬達，最安全的第一步

執行：
  python tests\06_plc_connection_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keyence import KeyenceKV, KVError
from utils import load_config, setup_logger


def _dm_number(device: str) -> int:
    digits = "".join(ch for ch in device if ch.isdigit())
    if not digits:
        raise ValueError(f"無法解析裝置位址: {device!r}")
    return int(digits)


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=cfg["logging"]["log_to_file"],
        log_to_console=cfg["logging"]["log_to_console"],
    )

    pcfg = cfg.get("plc") or {}
    if not pcfg:
        log.error("✘ config.yaml 找不到 plc 區段")
        return 1

    log.info("嘗試連線 PLC %s:%s ...", pcfg["ip"], pcfg["port"])
    try:
        with KeyenceKV(
            ip=pcfg["ip"],
            port=int(pcfg.get("port", 8500)),
            timeout=float(pcfg.get("timeout", 2.0)),
            command_delay=float(pcfg.get("command_delay", 0.0)),
            read_buffer=int(pcfg.get("read_buffer", 4096)),
        ) as plc:
            log.info("✔ TCP 連線成功")

            # CPU 機種代碼 (部分機種/設定可能不支援 ?K，失敗不算致命)
            try:
                log.info("✔ ?K (CPU 機種代碼) = %s", plc.cpu_model_code())
            except KVError as e:
                log.warning("?K 不支援或回應異常: %s", e)

            # 一次讀回整個集中監控區
            mon = pcfg.get("monitor") or {}
            start = mon.get("start", "DM7000")
            count = int(mon.get("count", 20))
            words = plc.read_words(start, count)
            start_num = _dm_number(start)
            log.info("✔ 監控區 %s ×%d:", start, count)
            for k in range(0, count, 5):
                row = "  ".join(
                    f"{start_num + k + j}:{words[k + j]:>6}"
                    for j in range(min(5, count - k))
                )
                log.info("    %s", row)

            # 依 signals 對應表印出已命名訊號
            for name, spec in (pcfg.get("signals") or {}).items():
                dev = (spec or {}).get("device")
                if not dev:
                    continue
                offset = _dm_number(dev) - start_num
                if not (0 <= offset < count):
                    log.warning("  訊號 %s (%s) 超出監控區", name, dev)
                    continue
                raw = words[offset]
                if spec.get("signed") and raw >= 0x8000:
                    raw -= 0x10000
                val = raw / float(spec.get("scale", 1.0))
                log.info("  %s (%s) = %s", name, dev, val)

            # 異常碼 (DM7010) 逐 bit 解碼成訊息
            ac_spec = (pcfg.get("signals") or {}).get("alarm_code") or {}
            ac_dev = ac_spec.get("device", "DM7010")
            ac_off = _dm_number(ac_dev) - start_num
            if 0 <= ac_off < count:
                code = words[ac_off] & 0xFFFF
                bit_labels = {
                    int(k): str(v) for k, v in (pcfg.get("alarm_bits") or {}).items()
                }
                if code == 0:
                    log.info("✔ 異常碼 %s = 0x%04X → 無異常", ac_dev, code)
                else:
                    msgs = [
                        bit_labels.get(b, f"bit{b}")
                        for b in range(16) if code & (1 << b)
                    ]
                    log.warning(
                        "⚠ 異常碼 %s = 0x%04X → %s", ac_dev, code, "、".join(msgs)
                    )
            else:
                log.warning("  異常碼 %s 超出監控區，未解碼", ac_dev)

            log.info("✔ PLC 連線測試通過 (全程唯讀)")
        log.info("✔ 已關閉連線")
        return 0
    except KVError as e:
        log.error("✘ 測試失敗: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
