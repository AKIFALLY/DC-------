"""
測試 04 — 效率曲線量測。

效率定義：
  η = P_out / P_in
  其中：
    P_out = V_load × I_load   (PEL-5000C 量測到的)
    P_in  = 機械輸入或電池輸入功率，需從外部來源取得

input_power_source 設定方式：
    "manual"   每點測完後由 console 輸入 P_in
    "constant" 全程使用 input_power_constant
    "external_meter" (預留：可改為從另一台儀器讀取)
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
    plot_efficiency_curve,
    plot_power_curve,
)
from utils.logger import timestamp_tag
from utils.plotter import plot_combined


def get_input_power(source: str, constant: float, current_set: float) -> float:
    if source == "constant":
        return float(constant)
    if source == "manual":
        while True:
            try:
                raw = input(f"  → 請輸入 I_set={current_set:.2f}A 點的輸入功率 P_in (W): ").strip()
                return float(raw)
            except ValueError:
                print("    無效輸入，請輸入數字。")
    raise NotImplementedError(f"input_power_source = {source!r} 尚未實作")


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=cfg["logging"]["log_to_file"],
        log_to_console=cfg["logging"]["log_to_console"],
    )

    inst = cfg["instrument"]
    eff = cfg["efficiency_test"]
    dut = cfg["dut"]
    out = cfg["output"]

    tag = timestamp_tag()
    csv_path = Path(out["data_dir"]) / f"efficiency_{tag}.csv"
    plot_eff_path = Path(out["reports_dir"]) / f"efficiency_{tag}.{out['plot_format']}"
    plot_p_path = Path(out["reports_dir"]) / f"power_eff_{tag}.{out['plot_format']}"
    plot_all_path = Path(out["reports_dir"]) / f"combined_eff_{tag}.{out['plot_format']}"

    fields = ["timestamp", "set_current_A", "V_avg", "I_avg", "P_out", "P_in", "efficiency"]
    currents, voltages, powers, effs = [], [], [], []

    try:
        with PEL5000C(**{k: inst[k] for k in ("ip", "port", "timeout", "command_delay", "read_buffer")}) as load, \
             CSVLogger(csv_path, fields, encoding=out["csv_encoding"]) as csvlog:

            log.info("儀器: %s", load.idn())
            log.info("DUT : %s", dut["name"])
            log.info("輸入功率來源: %s", eff["input_power_source"])

            load.set_mode("CC")
            load.write(f"CC:HIGH {eff['current_points'][0]:.4f}")
            load.load_on()
            log.info("LOAD ON — 開始效率掃描")

            for i_set in eff["current_points"]:
                load.write(f"CC:HIGH {float(i_set):.4f}")
                time.sleep(eff["dwell_seconds"])

                vs, is_, ps = [], [], []
                for _ in range(eff["samples_per_point"]):
                    v, i, p = load.measure_vip()
                    vs.append(v); is_.append(i); ps.append(p)
                    time.sleep(0.1)

                v_avg = mean(vs); i_avg = mean(is_); p_out = mean(ps)
                load.assert_within(v=v_avg, i=i_avg,
                                   v_max=dut["voltage_max"], i_max=dut["current_max"])

                p_in = get_input_power(
                    eff["input_power_source"], eff["input_power_constant"], i_set
                )
                eta = (p_out / p_in) if p_in > 0 else 0.0

                currents.append(i_avg); voltages.append(v_avg)
                powers.append(p_out); effs.append(eta)

                csvlog.write({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "set_current_A": i_set,
                    "V_avg": f"{v_avg:.4f}",
                    "I_avg": f"{i_avg:.4f}",
                    "P_out": f"{p_out:.4f}",
                    "P_in": f"{p_in:.4f}",
                    "efficiency": f"{eta:.6f}",
                })
                log.info("  I_set=%.2fA  V=%.3f  I=%.3f  P_out=%.2f  P_in=%.2f  η=%.2f%%",
                         i_set, v_avg, i_avg, p_out, p_in, eta * 100)

            load.load_off()
            log.info("LOAD OFF — 掃描完成")

        plot_efficiency_curve(currents, effs, plot_eff_path, dpi=out["plot_dpi"])
        plot_power_curve(currents, powers, plot_p_path, dpi=out["plot_dpi"])
        plot_combined(currents, voltages, powers, effs, plot_all_path, dpi=out["plot_dpi"])

        log.info("✔ CSV : %s", csv_path)
        log.info("✔ η   : %s", plot_eff_path)
        log.info("✔ P   : %s", plot_p_path)
        log.info("✔ 綜合: %s", plot_all_path)

        if effs:
            best = max(range(len(effs)), key=lambda k: effs[k])
            log.info("最佳效率點: I=%.2fA, η=%.2f%%", currents[best], effs[best] * 100)
        return 0

    except PELSafetyError as e:
        log.error("✘ 安全保護觸發: %s", e)
        return 2
    except PELError as e:
        log.error("✘ 通訊錯誤: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
