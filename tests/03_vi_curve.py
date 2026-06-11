"""
測試 03 — V-I 曲線量測 (CC 掃描)。

流程：
  1. 連線 → REMOTE
  2. 切到 CC 模式
  3. 從 current_start 到 current_stop 以 current_step 漸增
  4. 每點停留 dwell_seconds 後重複量測 samples_per_point 次平均
  5. 輸出 CSV + V-I 曲線 PNG + 功率曲線 PNG
  6. LOAD OFF → LOCAL → 關連線
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pel5000c import PEL5000C, PELError, PELSafetyError  # type: ignore
from utils import (
    load_config,
    setup_logger,
    CSVLogger,
    plot_vi_curve,
    plot_power_curve,
)
from utils.logger import timestamp_tag
from utils.plotter import plot_combined


def frange(start: float, stop: float, step: float):
    x = start
    # 含端點 (容忍浮點誤差)
    while x <= stop + 1e-9:
        yield round(x, 6)
        x += step


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=cfg["logging"]["log_to_file"],
        log_to_console=cfg["logging"]["log_to_console"],
    )

    inst = cfg["instrument"]
    vi = cfg["vi_curve"]
    dut = cfg["dut"]
    out = cfg["output"]

    tag = timestamp_tag()
    csv_path = Path(out["data_dir"]) / f"vi_curve_{tag}.csv"
    plot_vi_path = Path(out["reports_dir"]) / f"vi_curve_{tag}.{out['plot_format']}"
    plot_p_path = Path(out["reports_dir"]) / f"power_curve_{tag}.{out['plot_format']}"
    plot_all_path = Path(out["reports_dir"]) / f"combined_{tag}.{out['plot_format']}"

    fields = ["timestamp", "set_current_A", "V_avg", "I_avg", "P_avg", "samples"]
    currents, voltages, powers = [], [], []

    try:
        with PEL5000C(**{k: inst[k] for k in ("ip", "port", "timeout", "command_delay", "read_buffer")}) as load, \
             CSVLogger(csv_path, fields, encoding=out["csv_encoding"]) as csvlog:

            log.info("儀器: %s", load.idn())
            log.info("DUT : %s (額定 %.1fV / %.1fA)", dut["name"], dut["rated_voltage"], dut["rated_current"])

            load.set_mode("CC")
            load.write(f"CC:HIGH {vi['current_start']:.4f}")
            load.load_on()
            log.info("LOAD ON — 開始 V-I 掃描")

            for i_set in frange(vi["current_start"], vi["current_stop"], vi["current_step"]):
                load.write(f"CC:HIGH {i_set:.4f}")
                time.sleep(vi["dwell_seconds"])

                vs, is_, ps = [], [], []
                for _ in range(vi["samples_per_point"]):
                    v, i, p = load.measure_vip()
                    vs.append(v); is_.append(i); ps.append(p)
                    time.sleep(vi["sample_interval"])

                v_avg = mean(vs); i_avg = mean(is_); p_avg = mean(ps)
                currents.append(i_avg); voltages.append(v_avg); powers.append(p_avg)

                load.assert_within(v=v_avg, i=i_avg,
                                   v_max=dut["voltage_max"], i_max=dut["current_max"])

                csvlog.write({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "set_current_A": i_set,
                    "V_avg": f"{v_avg:.4f}",
                    "I_avg": f"{i_avg:.4f}",
                    "P_avg": f"{p_avg:.4f}",
                    "samples": vi["samples_per_point"],
                })
                log.info("  I_set=%.2fA  →  V=%.3fV  I=%.3fA  P=%.2fW", i_set, v_avg, i_avg, p_avg)

            load.load_off()
            log.info("LOAD OFF — 掃描完成，繪圖中")

        plot_vi_curve(currents, voltages, plot_vi_path, dpi=out["plot_dpi"])
        plot_power_curve(currents, powers, plot_p_path, dpi=out["plot_dpi"])
        plot_combined(currents, voltages, powers, None, plot_all_path, dpi=out["plot_dpi"])

        log.info("✔ CSV : %s", csv_path)
        log.info("✔ V-I : %s", plot_vi_path)
        log.info("✔ P   : %s", plot_p_path)
        log.info("✔ 綜合: %s", plot_all_path)
        return 0

    except PELSafetyError as e:
        log.error("✘ 安全保護觸發: %s", e)
        return 2
    except PELError as e:
        log.error("✘ 通訊錯誤: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
