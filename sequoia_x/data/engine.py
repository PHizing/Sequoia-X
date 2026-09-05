"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：设置 socket 超时，静默 login/logout，批量拉取 baostock 数据。"""
    import contextlib
    import io
    import socket
    socket.setdefaulttimeout(15.0)

    import baostock as bs

    results = []
    # Suppress baostock console stdout (login success / logout failed etc.)
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            lg = bs.login()
            if lg.error_code != "0":
                return results
        except Exception:
            return results

        try:
            for symbol, bs_code, start, end in tasks:
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="1",  # 后复权
                    )
                    if rs.error_code != "0":
                        continue
                    while rs.next():
                        results.append([symbol] + rs.get_row_data())
                except Exception:
                    continue
        finally:
            try:
                bs.logout()
            except Exception:
                pass

    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    @staticmethod
    def _probe_date_has_data(date_str: str) -> bool:
        """快速探测指定日期是否已有日线行情（以基准指数 sh.000001 试探）。"""
        import contextlib
        import io
        import socket
        socket.setdefaulttimeout(10.0)

        import baostock as bs

        has_data = False
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                lg = bs.login()
                if lg.error_code == "0":
                    rs = bs.query_history_k_data_plus(
                        "sh.000001",
                        "date,close",
                        start_date=date_str,
                        end_date=date_str,
                        frequency="d",
                    )
                    if rs.error_code == "0" and rs.next():
                        has_data = True
            except Exception:
                pass
            finally:
                try:
                    bs.logout()
                except Exception:
                    pass

        return has_data

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        today = date.today()
        today_str = today.strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        # 计算本地市场最新收盘日期
        max_dates = [last_date for _, last_date in rows if last_date]
        market_latest_date = max(max_dates) if max_dates else None

        # 如果今天是周末，计算最近的周五
        if today.weekday() == 5:
            last_trading_day = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        elif today.weekday() == 6:
            last_trading_day = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            last_trading_day = today_str

        # 1. 若当前是周末且本地已包含最近周五的数据，则无需重复同步
        if today.weekday() >= 5 and market_latest_date and market_latest_date >= last_trading_day:
            logger.info(f"今日 ({today_str}) 为周末休市日，本地已收录周五最新数据 ({market_latest_date})，无需增量同步")
            return 0

        # 2. 若是工作日且本地已具备上一交易日数据，快速探测今日收盘K线是否已就绪
        prev_workday = (today - timedelta(days=3 if today.weekday() == 0 else 1)).strftime("%Y-%m-%d")
        if market_latest_date and market_latest_date >= prev_workday:
            if not self._probe_date_has_data(today_str):
                logger.info(f"今日 ({today_str}) 行情尚未发布或为休市日，跳过增量更新，直接使用已有最新数据 ({market_latest_date})")
                return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = min(4, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import socket
        socket.setdefaulttimeout(15.0)
        import time
        from datetime import date, timedelta
        from tqdm import tqdm

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次
        report_interval = 100     # 每 100 只打印一次进度日志
        batch_size = 50           # 每攒够 50 只股票批量提交一次 SQLite

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        total = len(symbols)
        logger.info(
            f"开始历史数据回填：目标股票共 {total} 只，起始日期：{self.start_date}，"
            "支持随时中断与断点续传（已启用 SQLite 批量事务加速）..."
        )

        # 1. Open persistent SQLite connection with WAL mode
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        # 2. Pre-fetch existing symbols into memory cache to eliminate repeated DB queries
        rows = conn.execute("SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol").fetchall()
        local_latest_map: dict[str, str] = {r[0]: r[1] for r in rows if r[1]}

        batch_dfs: list[pd.DataFrame] = []

        def _flush_batch() -> None:
            """Flush pending DataFrames in memory to SQLite in a single transaction."""
            nonlocal batch_dfs
            if not batch_dfs:
                return
            try:
                combined = pd.concat(batch_dfs, ignore_index=True)
                combined.to_sql(
                    "stock_daily", conn, if_exists="append",
                    index=False, method="multi", chunksize=1000,
                )
                conn.commit()
            except sqlite3.IntegrityError:
                pass
            except Exception as exc:
                logger.warning(f"批量写入数据库异常: {exc}")
            finally:
                batch_dfs.clear()

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0
        start_time = time.time()

        pbar = tqdm(symbols, desc="回填历史K线", unit="只", dynamic_ncols=True)

        try:
            for i, symbol in enumerate(pbar):
                last_date = local_latest_map.get(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    pbar.set_postfix({"成功": success, "跳过": skipped, "失败": failed})
                    if (i + 1) % report_interval == 0:
                        _flush_batch()
                        elapsed = time.time() - start_time
                        speed = (i + 1) / max(elapsed, 1e-3)
                        eta_min = (total - (i + 1)) / max(speed * 60, 1e-3)
                        logger.info(
                            f"回填进度: {i + 1}/{total} ({(i + 1)/total*100:.1f}%) | "
                            f"成功 {success} 跳过 {skipped} 失败 {failed} | "
                            f"速度 {speed:.1f}只/s | 预估剩余 {eta_min:.1f}分钟"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    _flush_batch()
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    pbar.set_postfix({"成功": success, "跳过": skipped, "失败": failed})
                    continue

                if not rows:
                    skipped += 1
                    pbar.set_postfix({"成功": success, "跳过": skipped, "失败": failed})
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    pbar.set_postfix({"成功": success, "跳过": skipped, "失败": failed})
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                batch_dfs.append(df)
                local_latest_map[symbol] = today_str
                success += 1
                pbar.set_postfix({"成功": success, "跳过": skipped, "失败": failed})

                if len(batch_dfs) >= batch_size:
                    _flush_batch()

                if (i + 1) % report_interval == 0:
                    _flush_batch()
                    elapsed = time.time() - start_time
                    speed = (i + 1) / max(elapsed, 1e-3)
                    eta_min = (total - (i + 1)) / max(speed * 60, 1e-3)
                    logger.info(
                        f"回填进度: {i + 1}/{total} ({(i + 1)/total*100:.1f}%) | "
                        f"成功 {success} 跳过 {skipped} 失败 {failed} | "
                        f"速度 {speed:.1f}只/s | 预估剩余 {eta_min:.1f}分钟"
                    )

        finally:
            _flush_batch()
            conn.close()
            pbar.close()
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """获取全市场 A 股代码列表（baostock 优先并带重试，akshare 兜底）。"""
        import socket
        socket.setdefaulttimeout(15.0)
        import time
        import baostock as bs

        max_retries = 3
        symbols: list[str] = []

        # 1. Attempt to fetch from baostock with retries
        for attempt in range(max_retries):
            lg = bs.login()
            if lg.error_code != "0":
                logger.warning(f"baostock 登录失败 (第{attempt + 1}次): {lg.error_msg}")
                time.sleep(1)
                continue

            try:
                rs = bs.query_stock_basic(code_name="", code="")
                symbols = []
                while rs.next():
                    row = rs.get_row_data()
                    code = row[0]           # e.g. "sh.600000" or "sz.000001"
                    status = row[4]         # "1" = Listed
                    stock_type = row[5]     # "1" = Stock
                    if status == "1" and stock_type == "1":
                        symbols.append(code.split(".")[1])  # Extract numeric code
                if symbols:
                    logger.info(f"获取股票列表完成 (baostock)，共 {len(symbols)} 只")
                    return symbols
            except Exception as e:
                logger.warning(f"baostock 获取股票列表第{attempt + 1}次异常: {e}")
            finally:
                bs.logout()

            time.sleep(2 ** attempt)

        # 2. Fallback to akshare if baostock fails
        logger.warning("baostock 获取股票列表失败或为空，尝试通过 akshare 兜底获取...")
        try:
            import akshare as ak

            stock_info = ak.stock_info_a_code_name()
            if stock_info is not None and not stock_info.empty and "code" in stock_info.columns:
                symbols = [str(c).zfill(6) for c in stock_info["code"].dropna().tolist()]
                logger.info(f"通过 akshare 兜底获取股票列表完成，共 {len(symbols)} 只")
                return symbols
        except Exception as exc:
            logger.error(f"akshare 兜底获取股票列表也失败: {exc}")

        return symbols

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
