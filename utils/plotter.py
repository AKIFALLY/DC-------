"""matplotlib 繪圖工具：V-I、功率、效率曲線。"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence, Optional

import matplotlib

matplotlib.use("Agg")  # 非互動式環境也能用
import matplotlib.pyplot as plt

# 中文字型 (Windows 內建微軟正黑體)
plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _save(fig, path: str | Path, dpi: int = 120) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return p


def plot_vi_curve(
    currents: Sequence[float],
    voltages: Sequence[float],
    out_path: str | Path,
    title: str = "DC 發電機 V-I 曲線",
    dpi: int = 120,
) -> Path:
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(currents, voltages, marker="o", linewidth=1.5)
    ax.set_xlabel("負載電流 I (A)")
    ax.set_ylabel("發電機端電壓 V (V)")
    ax.set_title(title)
    ax.grid(True, alpha=0.4)
    return _save(fig, out_path, dpi)


def plot_power_curve(
    currents: Sequence[float],
    powers: Sequence[float],
    out_path: str | Path,
    title: str = "DC 發電機輸出功率曲線",
    dpi: int = 120,
) -> Path:
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(currents, powers, marker="s", color="tab:orange", linewidth=1.5)
    ax.set_xlabel("負載電流 I (A)")
    ax.set_ylabel("輸出功率 P (W)")
    ax.set_title(title)
    ax.grid(True, alpha=0.4)
    # 標出最大功率點 (MPP)
    if powers:
        i_max = max(range(len(powers)), key=lambda k: powers[k])
        ax.annotate(
            f"MPP\nI={currents[i_max]:.2f}A\nP={powers[i_max]:.2f}W",
            xy=(currents[i_max], powers[i_max]),
            xytext=(10, 10),
            textcoords="offset points",
            fontsize=9,
            arrowprops=dict(arrowstyle="->", color="red"),
        )
    return _save(fig, out_path, dpi)


def plot_efficiency_curve(
    currents: Sequence[float],
    efficiencies: Sequence[float],
    out_path: str | Path,
    title: str = "DC 發電機效率曲線",
    dpi: int = 120,
) -> Path:
    fig, ax = plt.subplots(figsize=(8, 5))
    eff_pct = [e * 100.0 for e in efficiencies]
    ax.plot(currents, eff_pct, marker="^", color="tab:green", linewidth=1.5)
    ax.set_xlabel("負載電流 I (A)")
    ax.set_ylabel("效率 η (%)")
    ax.set_title(title)
    ax.set_ylim(0, 105)
    ax.grid(True, alpha=0.4)
    if eff_pct:
        i_max = max(range(len(eff_pct)), key=lambda k: eff_pct[k])
        ax.annotate(
            f"峰值\nI={currents[i_max]:.2f}A\nη={eff_pct[i_max]:.2f}%",
            xy=(currents[i_max], eff_pct[i_max]),
            xytext=(10, -30),
            textcoords="offset points",
            fontsize=9,
            arrowprops=dict(arrowstyle="->", color="red"),
        )
    return _save(fig, out_path, dpi)


def plot_combined(
    currents: Sequence[float],
    voltages: Sequence[float],
    powers: Sequence[float],
    efficiencies: Optional[Sequence[float]],
    out_path: str | Path,
    title: str = "DC 發電機綜合特性",
    dpi: int = 120,
) -> Path:
    fig, ax1 = plt.subplots(figsize=(9, 5.5))
    ax1.plot(currents, voltages, marker="o", color="tab:blue", label="V (V)")
    ax1.set_xlabel("負載電流 I (A)")
    ax1.set_ylabel("V (V)", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")
    ax1.grid(True, alpha=0.4)

    ax2 = ax1.twinx()
    ax2.plot(currents, powers, marker="s", color="tab:orange", label="P (W)")
    ax2.set_ylabel("P (W)", color="tab:orange")
    ax2.tick_params(axis="y", labelcolor="tab:orange")

    if efficiencies is not None:
        ax3 = ax1.twinx()
        ax3.spines["right"].set_position(("outward", 50))
        eff_pct = [e * 100.0 for e in efficiencies]
        ax3.plot(currents, eff_pct, marker="^", color="tab:green", label="η (%)")
        ax3.set_ylabel("η (%)", color="tab:green")
        ax3.tick_params(axis="y", labelcolor="tab:green")
        ax3.set_ylim(0, 105)

    ax1.set_title(title)
    fig.tight_layout()
    return _save(fig, out_path, dpi)


def _auto_test_stage_bounds(rows):
    """回傳 [(stage_label, elapsed_start), ...]，標記每階段第一次出現的時間。"""
    bounds = []
    last = None
    for r in rows:
        s = r.get("stage")
        if s != last:
            bounds.append((s, r["elapsed_s"]))
            last = s
    return bounds


def _build_auto_test_fig(
    rows,
    title: str = "自動測試 — 階段負載時間曲線",
):
    """建構自動測試時間序列圖並回傳 matplotlib figure。

    六宮格以 3 列 × 2 欄 排列 (單欄太窄)，共享 x 軸 (時間)：
        左欄：電壓 V / 功率 P / 轉速 RPM
        右欄：電流 I / 溫度 / 功率因數 PF
    各階段分界以虛線標出 (六格皆畫，標籤只標最上一列)。
    rows: list[dict]，每筆含 elapsed_s / V / I / P / stage，溫度 (temperature)、
    轉速 (rpm)、功率因數 (power_factor) 為選用欄位 — 整段無資料的格畫成「無資料」。
    """
    if not rows:
        raise ValueError("沒有資料可繪圖")

    t = [r["elapsed_s"] for r in rows]

    # (標籤, 取值函式, 顏色) — 依序填入 3 列 × 2 欄 (列優先：左→右、上→下)
    panels = [
        ("電壓 V (V)", lambda r: r.get("V"), "tab:blue"),
        ("電流 I (A)", lambda r: r.get("I"), "tab:orange"),
        ("功率 P (W)", lambda r: r.get("P"), "tab:green"),
        ("溫度 (°C)", lambda r: r.get("temperature"), "tab:red"),
        ("轉速 RPM", lambda r: r.get("rpm"), "tab:purple"),
        ("功率因數 PF", lambda r: r.get("power_factor"), "#8c564b"),
    ]

    ncols = 2
    nrows = (len(panels) + ncols - 1) // ncols   # 6 → 3 列
    fig, axes2d = plt.subplots(
        nrows, ncols, figsize=(13, 3.0 * nrows), sharex=True, squeeze=False
    )
    axes = [axes2d[r][c] for r in range(nrows) for c in range(ncols)]   # 列優先攤平

    for ax, (label, getter, color) in zip(axes, panels):
        series = [getter(r) for r in rows]
        ax.set_ylabel(label, color=color)
        ax.tick_params(axis="y", labelcolor=color)
        ax.grid(True, alpha=0.4)
        if any(x is not None for x in series):
            yy = [x if x is not None else float("nan") for x in series]
            ax.plot(t, yy, color=color, linewidth=1.3)
        else:
            # 整段無資料 (例：未連 PLC 無溫度/轉速、PF 未啟用) — 仍保留該格
            ax.annotate(
                "無資料", xy=(0.5, 0.5), xycoords="axes fraction",
                ha="center", va="center", fontsize=11, color="#bbbbbb",
            )

    # 多出來的空格 (panels 不足以填滿格狀) 隱藏
    for ax in axes[len(panels):]:
        ax.set_visible(False)

    # 標題置於最上一列正中；最下一列每欄都標 x 軸
    fig.suptitle(title, fontsize=13)
    for c in range(ncols):
        axes2d[nrows - 1][c].set_xlabel("時間 (s)")

    # 階段分界線 + 標籤 (畫在所有有效子圖；標籤只標在最上一列)
    used_axes = axes[:len(panels)]
    top_row_axes = [axes2d[0][c] for c in range(ncols)]
    for (label, t0) in _auto_test_stage_bounds(rows):
        for ax in used_axes:
            ax.axvline(t0, color="gray", linestyle="--", alpha=0.6)
        for ax in top_row_axes:
            ax.annotate(
                label, xy=(t0, 1.0), xycoords=("data", "axes fraction"),
                xytext=(3, -14), textcoords="offset points",
                fontsize=9, color="dimgray",
            )

    fig.tight_layout()
    return fig


def plot_auto_test(
    rows,
    out_path: str | Path,
    title: str = "自動測試 — 階段負載時間曲線",
    dpi: int = 120,
) -> Path:
    """自動測試時間序列圖：上 V/I、中 P(+溫度)、下 功率因數，並標出各階段分界。"""
    fig = _build_auto_test_fig(rows, title)
    return _save(fig, out_path, dpi)


def render_auto_test_png(
    rows,
    title: str = "自動測試 — 階段負載時間曲線 (進行中)",
    dpi: int = 110,
) -> bytes:
    """把自動測試曲線渲染成 PNG bytes (供 UI 即時預覽，不落地成固定檔名)。"""
    import io

    fig = _build_auto_test_fig(rows, title)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
