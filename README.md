# DC 發電機量測系統 (PEL-5000C + PLC + 功率計 / Python LAN 框架)

以 GW Instek PEL-5000C 高功率電子負載為核心,整合 Keyence PLC、GPM-8310 功率計,
做 DC 發電機效能量測的 Python 自動化框架 + PyQt6 操作介面。所有網路儀器皆走
自寫 socket 驅動 (無 VISA 相依)。

## 整合儀器一覽

| 驅動套件 | 儀器 | 通訊 | 任務 |
|---|---|---|---|
| `pel5000c/` | GW Instek PEL-5000C 電子負載 | LAN/TCP **:4001** SCPI | 負載槽 (CC/CR/CV/CP)、讀 V/I/P |
| `keyence/` | Keyence KV-N40 PLC (上位鏈路) | LAN/TCP **:8500** ASCII | 伺服 ON/OFF + 集中監控 (溫度/轉速/模式/異常) |
| `gpm8310/` | GW Instek GPM-8310 功率計 | LAN/TCP **:23** SCPI | 讀 **功率因數 (PF / λ)** — 客戶端規格需求 |
| `fotek/` | FOTEK NT-22-RS 溫控器 | RS-485 Modbus RTU | ⚠ 已停用 (溫度改走 PLC),保留備用 |

操作主程式為 `ui/main_window.py` (PyQt6),把上述儀器整合在同一畫面。

---

## 專案結構

```
DC發電機量測系統/
├── README.md                       # 本檔
├── requirements.txt                # Python 套件需求
├── config.yaml                     # 各儀器連線/測試/DUT 設定
├── 儀器評估報告_PEL5000C_GPM8310.md  # 前期儀器選型評估
├── 速度控制設定說明.md              # 士林伺服驅動器速度模式參數
│
├── pel5000c/                       # PEL-5000C 電子負載驅動
├── keyence/                        # Keyence KV PLC 上位鏈路驅動
├── gpm8310/                        # GPM-8310 功率計驅動 (讀功率因數)
├── fotek/                          # FOTEK 溫控驅動 (已停用，保留)
│   (各套件均含 driver.py / exceptions.py / __init__.py)
│
├── ui/
│   └── main_window.py              # PyQt6 操作主程式
│
├── utils/
│   ├── logger.py                   # 日誌 + CSV + YAML 載入
│   └── plotter.py                  # V-I / 功率 / 效率 / 自動測試繪圖
│
├── tests/                          # 編號越小越基礎，建議照順序跑
│   ├── 01_connection_test.py       # PEL 連線驗證 (LOAD OFF，最安全)
│   ├── 02_manual_control.py        # PEL 互動式 console
│   ├── 03_vi_curve.py              # PEL V-I 曲線 + 功率曲線
│   ├── 04_efficiency_test.py       # PEL 效率曲線
│   ├── 05_load_sweep.py            # PEL CC/CR/CV/CP 四模式自動掃描
│   ├── 06_plc_connection_test.py   # PLC 連線 + 讀監控區
│   └── 07_gpm_connection_test.py   # 功率計連線 + 讀功率因數
│
├── data/                           # CSV 原始資料 + log 檔
├── reports/                        # 自動產生的圖表 (PNG)
└── docs/
    └── 使用手冊.md
```

---

## 快速開始

### 1. 安裝 Python 套件
```powershell
cd "C:\Users\USER\Documents\捷耀\DC發電機量測系統"
python -m pip install -r requirements.txt
```
（本機已安裝 Python 3.13；若 `python` 指令叫出 Microsoft Store，請到
「設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名」關閉 python.exe 別名。）

### 2. 設定儀器 IP
編輯 `config.yaml`，各儀器有獨立區段；非 PEL 的儀器另有 `enabled:` 旗標，
設 `true` 時 UI 啟動會自動連線：
```yaml
instrument:        # PEL-5000C 電子負載
  ip: "192.168.0.100"   # 出廠預設；Port 4001 固定
power_meter:       # GPM-8310 功率計 (讀功率因數)
  enabled: false
  ip: "192.168.0.100"   # 出廠預設；Socket Port 出廠預設 23
plc:               # Keyence KV PLC
  enabled: false
  ip: "192.168.0.10"    # Port 8500
```
PEL-5000C / GPM-8310 出廠預設 IP 皆為 `192.168.0.100`，由前面板 LAN 設定查看或修改。

### 3. 啟動操作介面 (建議)
```powershell
python ui\main_window.py
```
畫面提供 PLC / 負載機 / 功率計 三個連線框、即時監視 (V/I/P/溫度/轉速/功率因數)、
手動控制、以及 5%/50%/100% 三階段自動測試 (結果自動存 CSV + 曲線圖)。

### 4. 第一次連線 (CLI)
```powershell
python tests\01_connection_test.py     # PEL-5000C
python tests\07_gpm_connection_test.py # GPM-8310 功率計 (讀功率因數)
```
看到 `*IDN?` 正確回應即代表連線成功。**此測試不會啟動負載**，最安全。

### 5. 互動測試
```powershell
python tests\02_manual_control.py
```
進入 console 後試試：
```
>>> idn
>>> cc 1.0      # 切到 CC 模式 1A
>>> on          # LOAD ON
>>> m           # 讀 V/I/P
>>> off
>>> q
```

### 6. 跑完整 V-I 曲線
```powershell
python tests\03_vi_curve.py
```
完成後 `data/vi_curve_*.csv` 與 `reports/vi_curve_*.png` 會自動產生。

---

## 安全注意事項

1. **接線前先確認 DUT 極性與 PEL-5000C 端子方向**（端子上有 + / - 標記）。
2. 第一次跑 `03_vi_curve.py` 前，**務必把 `config.yaml` 的 `current_stop` 設小**（例如 5A），確認 DUT 反應正常後再加大。
3. 框架內建 `voltage_max` / `current_max` 保護，超過會自動 `LOAD OFF`，但仍建議 DUT 端裝保險絲。
4. 程式異常結束時，`__exit__` 會嘗試送 `LOAD OFF` + `LOCAL`，但若是斷電/網路斷線無法保證。**不要把人放在會被高壓打到的位置**。

---

## 常見指令對照（給除錯用）

| 動作 | SCPI 指令 |
|---|---|
| 識別 | `*IDN?` |
| 進入遠端 | `REMOTE` |
| 退出遠端 | `LOCAL` |
| 切 CC 模式 | `MODE CC` |
| 設 CC 電流 (HIGH) | `CC:HIGH 5.0` |
| 啟動負載 | `LOAD ON` |
| 關閉負載 | `LOAD OFF` |
| 讀電壓 | `MEAS:VOLT?` |
| 讀電流 | `MEAS:CURR?` |
| 一次讀 V,I | `MEAS:VC?` |

---

## 後續可擴充項目

- 串接外部功率計或扭力計測 P_in（目前 `04_efficiency_test.py` 用人工輸入 / 固定值）
- 加入溫度感測（NTC / Thermocouple）做溫升曲線
- 自動產生 PDF 測試報告
- 多機並聯（PEL-5000C 主從架構，最多 5 台）

詳細使用步驟請見 `docs/使用手冊.md`。
