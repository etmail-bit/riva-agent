#!/usr/bin/env bash
# 啟動網頁介面，固定綁定 localhost，避免手動下指令時漏打
# --server.address 導致預設監聽 0.0.0.0（外部網路連得到）。
#
# 用法：
#   ./scripts/run_app.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source .venv/bin/activate
exec streamlit run app.py --server.address localhost --server.headless true --browser.gatherUsageStats false
