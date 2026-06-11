"""
測試 05 — 多模式自動掃描測試。

依 config.yaml 中 load_sweep.modes 列表，依序執行 CC / CR / CV / CP 掃描。
適合用來快速驗證儀器在四種模式下對 DUT 的反應。

每模式輸出獨立 CSV，總結報告寫入 reports/load_sweep_<時間>.csv
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pel5000c import PEL5000C, PELError, PELSafetyError  # type: ignore
from utils import load_config, setup_logger, CSVLogger
from utils.logger import timestamp_tag


def frange(start: float, stop: float, step: float):
    x = start
    while x <= stop + 1e-9:
        yield round(x, 6)
        x += step


def sweep_one_mode(load, mode: str, points, dwell: float, dut, log) -> list[dict]:
    rows = []
    log.info("─── %s 模式掃描開始 ───", mode)
    load.set_mode(mode)
    cmd_prefix = {"CC": "CC:HIGH", "CR": "CR:HIGH", "CV": "CV:HIGH", "CP": "CP:HIGH"}[mode]
    load.write(f"{cmd_prefix} {points[0]:.4f}")
    load.load_on()
    try:
        for setpoint in points:
            load.write(f"{cmd_prefix} {setpoint:.4f}")
            time.sleep(dwell)
            v, i, p = load.measure_vip()
            load.assert_within(v=v, i=i,
                               v_max=dut["voltage_max"], i_max=dut["current_max"])
            rows.append({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "setpoint": setpoint,
                "V": f"{v:.4f}",
                "I": f"{i:.4f}",
                "P": f"{p:.4f}",
            })
            log.info("  %s set=%.3f  V=%.3f  I=%.3f  P=%.2f",
                     mode, setpoint, v, i, p)
    finally:
        load.load_off()
        log.info("─── %s 模式 LOAD OFF ───", mode)
    return rows


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=cfg["logging"]["log_to_file"],
        log_to_console=cfg["logging"]["log_to_console"],
    )

    inst = cfg["instrument"]
    sw = cfg["load_sweep"]
    dut = cfg["dut"]
    out = cfg["output"]

    tag = timestamp_tag()
    summary_path = Path(out["reports_dir"]) / f"load_sweep_{tag}.csv"
    fields = ["timestamp", "mode", "setpoint", "V", "I", "P"]

    range_map = {
        "CC": sw["cc_range"],
        "CR": sw["cr_range"],
        "CV": sw["cv_range"],
        "CP": sw["cp_range"],
    }

    try:
        with PEL5000C(**{k: inst[k] for k in ("ip", "port", "timeout", "command_delay", "read_buffer")}) as load, \
             CSVLogger(summary_path, fields, encoding=out["csv_encoding"]) as summary:

            log.info("儀器: %s", load.idn())
            for mode in sw["modes"]:
                start, stop, step = range_map[mode]
                points = list(frange(start, stop, step))
                rows = sweep_one_mode(load, mode, points, sw["dwell_seconds"], dut, log)
                for r in rows:
                    summary.write(r)
                # 模式之間放 LOAD 一段時間冷卻
                time.sleep(1.0)

        log.info("✔ 全模式掃描完成: %s", summary_path)
        return 0

    except PELSafetyError as e:
        log.error("✘ 安全保護觸發: %s", e)
        return 2
    except PELError as e:
        log.error("✘ 通訊錯誤: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
