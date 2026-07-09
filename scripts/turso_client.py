"""Turso 雲端資料庫的輕量 HTTP 連線層。

只做 calculate_pnl.py／app_pnl.py 需要的最小介面（execute().fetchall()/.fetchone()、
commit()、dict 相容的 row），刻意模仿 sqlite3 的用法，讓既有的 calculate_pnl.py
（get_periods/get_revenue_breakdown/get_cost_actuals/save_pnl_result）可以直接吃這個
連線物件、不用改一行邏輯。

背景：Turso 官方的 libsql Python 套件在 Python 3.14 + Intel Mac 上編譯不起來（缺
cmake／完整 Rust 工具鏈），改用 Turso 的「SQL over HTTP」介面繞過去，見 PROGRESS.md
「月盈虧上雲部署」一節。
"""
import os

import requests


def _to_arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": str(value)}
    return {"type": "text", "value": str(value)}


def _from_cell(cell):
    cell_type = cell["type"]
    if cell_type == "null":
        return None
    if cell_type == "integer":
        return int(cell["value"])
    if cell_type == "float":
        return float(cell["value"])
    return cell["value"]  # text / blob


class TursoCursor:
    def __init__(self, cols, rows):
        self._rows = [dict(zip(cols, (_from_cell(cell) for cell in row))) for row in rows]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class TursoConnection:
    """跟 sqlite3.Connection 一樣：conn.execute(sql, params).fetchall()/.fetchone()。
    每個 execute() 各自即時送出、即時生效，commit()/close() 只是為了介面相容而存在。"""

    def __init__(self, url=None, token=None):
        base_url = url or os.environ["TURSO_DATABASE_URL"]
        self._pipeline_url = base_url.replace("libsql://", "https://") + "/v2/pipeline"
        self._token = token or os.environ["TURSO_AUTH_TOKEN"]
        # 用同一個 Session 重複使用 HTTPS 連線（keep-alive），避免每次查詢都重新握手，
        # 一頁若有 9~10 次查詢，這樣做可以省下大部分的網路往返時間。
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        )

    def execute(self, sql, params=()):
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [_to_arg(p) for p in params]
        resp = self._session.post(
            self._pipeline_url,
            json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
            timeout=15,
        )
        resp.raise_for_status()
        result_entry = resp.json()["results"][0]
        if result_entry["type"] == "error":
            raise RuntimeError(f"Turso 查詢失敗: {result_entry['error']['message']}\nSQL: {sql}")
        result = result_entry["response"]["result"]
        cols = [c["name"] for c in result["cols"]]
        return TursoCursor(cols, result["rows"])

    def executescript(self, sql_script):
        """依序執行多個以分號分隔的 SQL 陳述式，用於一次性建立 schema。"""
        statements = [s.strip() for s in sql_script.split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)

    def commit(self):
        pass

    def close(self):
        pass
