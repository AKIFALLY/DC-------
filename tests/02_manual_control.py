"""
測試 02 — 互動式手動控制。

目的：在 console 上以指令操作 PEL-5000C，方便除錯與手感測試。

支援指令：
    cc <A>      切到 CC 模式並設定電流 (A)
    cr <Ω>      切到 CR 模式並設定電阻 (Ω)
    cv <V>      切到 CV 模式並設定電壓 (V)
    cp <W>      切到 CP 模式並設定功率 (W)
    on          LOAD ON
    off         LOAD OFF
    m / meas    讀取 V, I, P
    idn         讀取 *IDN?
    raw <SCPI>  直接送出原始 SCPI 指令
    q / quit    結束 (會自動 LOAD OFF + LOCAL)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pel5000c import PEL5000C, PELError
from utils import load_config, setup_logger


HELP = __doc__


def main() -> int:
    cfg = load_config()
    log = setup_logger(
        level=cfg["logging"]["level"],
        log_dir=cfg["logging"]["log_dir"],
        log_to_file=False,
        log_to_console=True,
    )
    inst = cfg["instrument"]
    print(HELP)

    try:
        with PEL5000C(**{k: inst[k] for k in ("ip", "port", "timeout", "command_delay", "read_buffer")}) as load:
            print(f"已連線: {load.idn()}\n")
            while True:
                try:
                    line = input(">>> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""
                try:
                    if cmd in ("q", "quit", "exit"):
                        break
                    elif cmd in ("h", "help", "?"):
                        print(HELP)
                    elif cmd == "cc":
                        load.set_mode_cc(float(arg))
                    elif cmd == "cr":
                        load.set_mode_cr(float(arg))
                    elif cmd == "cv":
                        load.set_mode_cv(float(arg))
                    elif cmd == "cp":
                        load.set_mode_cp(float(arg))
                    elif cmd == "on":
                        load.load_on()
                    elif cmd == "off":
                        load.load_off()
                    elif cmd in ("m", "meas"):
                        v, i, p = load.measure_vip()
                        print(f"  V = {v:.4f} V    I = {i:.4f} A    P = {p:.4f} W")
                    elif cmd == "idn":
                        print(" ", load.idn())
                    elif cmd == "raw":
                        if "?" in arg:
                            print(" <-", load.query(arg))
                        else:
                            load.write(arg)
                    else:
                        print(f"未知指令: {cmd}  (h 顯示說明)")
                except PELError as e:
                    print(f"  [錯誤] {e}")
                except ValueError:
                    print("  [錯誤] 參數需為數字")
    except PELError as e:
        log.error("✘ 連線失敗: %s", e)
        return 1
    print("已關閉連線。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
