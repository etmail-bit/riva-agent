#!/usr/bin/env python3
"""印出 config/staffing_rules.json 的雲端安全子集（排除 wages／employee_roles 這兩個
含真實薪資／員工代碼對照的段落），供使用者自己複製貼上到 Streamlit Cloud 的 Secrets
（STAFFING_RULES_JSON 這把 key），比照 COST_RATES_JSON 的既有建立方式。

這支腳本只印到 stdout，不會自動上傳到任何地方——真實薪資數字完全不會被這支腳本以外
的任何流程碰到，貼上雲端後台一律由使用者自己在自己的終端機操作。

**stdout 只會是純 JSON**，可以直接 `python3 -m scripts.print_safe_staffing_config | pbcopy`
拿去貼 Streamlit Secrets 的 STAFFING_RULES_JSON，不會混進其他文字（2026-07-14 修正：
「已排除機敏欄位」這行說明改印到 stderr，之前混在 stdout 裡的話，直接複製終端機輸出
貼進 Secrets 會讓那把 JSON 解析失敗，雲端會靜默 fallback 讀本機檔案、報出看起來不相干
的 FileNotFoundError，很難聯想到是這一行註解搞的鬼）。

用法：
    source .venv/bin/activate
    python3 -m scripts.print_safe_staffing_config
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "staffing_rules.json"

SAFE_KEYS = ["capacity", "delivery", "tea_brewing", "shifts", "part_time", "scenario"]


def build_safe_config():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {k: config[k] for k in SAFE_KEYS if k in config}


def main():
    safe_config = build_safe_config()
    excluded = [k for k in ("wages", "employee_roles") if k in json.loads(CONFIG_PATH.read_text(encoding="utf-8"))]
    if excluded:
        print(f"# 已排除機敏欄位：{', '.join(excluded)}（不會出現在下面的內容裡）", file=sys.stderr)
    print(json.dumps(safe_config, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
