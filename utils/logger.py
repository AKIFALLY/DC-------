"""日誌與 CSV 資料記錄工具。"""
from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Any, Optional

import yaml


def load_config(path: str | Path = "config.yaml") -> dict:
    """讀取 YAML 設定檔。

    開發時相對路徑以「專案根目錄」解析；打包成 exe (PyInstaller) 後則以
    「exe 所在資料夾」解析 — config.yaml 放在 exe 旁邊，現場可直接編輯 (IP/額定等)。
    """
    p = Path(path)
    if not p.is_absolute():
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent.parent
        p = base / p
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logger(
    name: str = "pel5000c",
    level: str = "INFO",
    log_dir: Optional[str | Path] = None,
    log_to_file: bool = True,
    log_to_console: bool = True,
) -> logging.Logger:
    """建立 root logger 並設定輸出。"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_to_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    if log_to_file and log_dir is not None:
        ldir = Path(log_dir)
        ldir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fh = logging.FileHandler(ldir / f"run_{ts}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    # 同時讓 pel5000c.driver 的 log 流入此 logger
    logging.getLogger("pel5000c").setLevel(logger.level)
    for h in logger.handlers:
        logging.getLogger("pel5000c").addHandler(h)

    return logger


class CSVLogger:
    """簡易 CSV 寫入工具，附 BOM (Excel 友善)。"""

    def __init__(
        self,
        path: str | Path,
        fieldnames: Iterable[str],
        encoding: str = "utf-8-sig",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fieldnames = list(fieldnames)
        self._file = open(self.path, "w", encoding=encoding, newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames)
        self._writer.writeheader()
        self._file.flush()

    def write(self, row: Mapping[str, Any]) -> None:
        self._writer.writerow({k: row.get(k, "") for k in self.fieldnames})
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "CSVLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def timestamp_tag() -> str:
    """產生供檔名使用的時間戳。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")
