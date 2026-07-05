"""
kendama.db - SQLite 影子写入层 (Phase 1)

只负责表结构、连接管理、增删改查,不做任何业务判断(品牌筛选/LLM/利润/标签),
那些逻辑留在 main.py / ai_filter.py。main.py 负责调用这里的函数并根据返回值
决定要不要跳过某一步影子写入;所有公开函数自己吞掉异常、记录日志、返回
None/False,绝不向调用方抛出,保证数据库出问题不会影响抓取/LLM/推送主流程。

商品身份用 url 本身(NOT NULL UNIQUE),不是 md5 短哈希;legacy_url_hash
只是为了以后兼容 feedback.db 现有的 id 算法而保留的冗余字段,Phase 1 不依赖它。
"""
import sqlite3
import logging

logger = logging.getLogger(__name__)

DB_FILE = "kendama.db"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        raw_item_count INTEGER,
        brand_matched_count INTEGER,
        llm_input_count INTEGER,
        evaluated_count INTEGER,
        candidate_count INTEGER,
        status TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        legacy_url_hash TEXT NOT NULL,
        title TEXT NOT NULL,
        img_url TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
        price_jpy INTEGER NOT NULL,
        raw_price_text TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        UNIQUE(listing_id, scan_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
        price_jpy INTEGER NOT NULL,
        domestic_ref_price REAL NOT NULL,
        is_gold_mine INTEGER NOT NULL,
        reason TEXT,
        estimated_profit REAL NOT NULL,
        total_cost REAL NOT NULL,
        taxed INTEGER NOT NULL,
        tag TEXT NOT NULL,
        selected_for_push INTEGER NOT NULL,
        evaluated_at TEXT NOT NULL,
        brand TEXT,
        source_category TEXT,
        previous_price_jpy INTEGER,
        price_drop_jpy INTEGER,
        price_drop_pct REAL,
        is_price_drop INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NULL REFERENCES listings(id),
        legacy_item_id TEXT NULL,
        url TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NULL,
        source TEXT NOT NULL,
        dedupe_key TEXT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
)


def _connect_db(db_file=None):
    """每个新连接都统一设置 foreign_keys / busy_timeout,不依赖调用方记得设置。"""
    conn = sqlite3.connect(db_file or DB_FILE)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_columns(conn, table, column_defs):
    """安全、幂等地给已存在的表补齐缺失列。只用 ALTER TABLE ADD COLUMN,
    不重建、不覆盖、不删除已有数据;已存在的列直接跳过。
    column_defs: [(列名, 列类型定义字符串), ...]"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, column_type in column_defs:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


def init_db(db_file=None):
    """建表 + 开启 WAL(数据库级持久设置,这里设一次即可,不需要每次连接重复设) +
    给已存在的旧表补齐新增列(幂等,不影响已有历史行)。失败只记日志,返回 False。"""
    try:
        conn = _connect_db(db_file)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            # 对已存在的旧 evaluations 表(建表早于这几个字段被引入)补列;
            # 新建的表在上面 CREATE TABLE 时已经包含这些列,这里是无操作的幂等空跑。
            _ensure_columns(conn, "evaluations", [
                ("previous_price_jpy", "INTEGER"),
                ("price_drop_jpy", "INTEGER"),
                ("price_drop_pct", "REAL"),
                ("is_price_drop", "INTEGER NOT NULL DEFAULT 0"),
            ])
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        logger.error(f"kendama.db 初始化失败: {e}")
        return False


def get_or_create_listing(platform, url, legacy_url_hash, title, img_url, observed_at, db_file=None):
    """按 url 做 upsert(已存在则更新 last_seen_at/title/img_url),返回 listing_id;失败返回 None。"""
    try:
        conn = _connect_db(db_file)
        try:
            conn.execute(
                """
                INSERT INTO listings (platform, url, legacy_url_hash, title, img_url, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    title = excluded.title,
                    img_url = excluded.img_url
                """,
                (platform, url, legacy_url_hash, title, img_url or "", observed_at, observed_at),
            )
            conn.commit()
            row = conn.execute("SELECT id FROM listings WHERE url = ?", (url,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"写入 listings 失败 (url={url}): {e}")
        return None


def record_price(listing_id, scan_run_id, price_jpy, raw_price_text, observed_at, db_file=None):
    """写一条价格快照。同一 (listing_id, scan_run_id) 已存在则忽略(INSERT OR IGNORE),不报错。"""
    if listing_id is None or scan_run_id is None:
        return False
    try:
        conn = _connect_db(db_file)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO price_history
                    (listing_id, scan_run_id, price_jpy, raw_price_text, observed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (listing_id, scan_run_id, price_jpy, raw_price_text, observed_at),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"写入 price_history 失败 (listing_id={listing_id}, scan_run_id={scan_run_id}): {e}")
        return False


def record_evaluation(listing_id, scan_run_id, price_jpy, domestic_ref_price, is_gold_mine,
                       reason, estimated_profit, total_cost, taxed, tag, selected_for_push,
                       evaluated_at, brand=None, source_category=None,
                       previous_price_jpy=None, price_drop_jpy=None, price_drop_pct=None,
                       is_price_drop=False, db_file=None):
    """写一条评估结果快照,普通 INSERT,不做 upsert,同一商品的历史全部保留。
    brand/source_category/降价相关字段允许为空,缺失不影响这条记录本身的写入。"""
    if listing_id is None or scan_run_id is None:
        return False
    try:
        conn = _connect_db(db_file)
        try:
            conn.execute(
                """
                INSERT INTO evaluations
                    (listing_id, scan_run_id, price_jpy, domestic_ref_price, is_gold_mine,
                     reason, estimated_profit, total_cost, taxed, tag, selected_for_push,
                     evaluated_at, brand, source_category,
                     previous_price_jpy, price_drop_jpy, price_drop_pct, is_price_drop)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (listing_id, scan_run_id, price_jpy, domestic_ref_price, int(bool(is_gold_mine)),
                 reason, estimated_profit, total_cost, int(bool(taxed)), tag,
                 int(bool(selected_for_push)), evaluated_at, brand, source_category,
                 previous_price_jpy, price_drop_jpy, price_drop_pct, int(bool(is_price_drop))),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"写入 evaluations 失败 (listing_id={listing_id}, scan_run_id={scan_run_id}): {e}")
        return False


def get_listing_id_by_url(url, db_file=None):
    """按 url 精确查找 listing_id;查不到、url 为空或出错都返回 None。"""
    if not url:
        return None
    try:
        conn = _connect_db(db_file)
        try:
            row = conn.execute("SELECT id FROM listings WHERE url = ?", (url,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"查询 listings 失败 (url={url}): {e}")
        return None


def get_previous_price(listing_id, scan_run_id, db_file=None):
    """查询该 listing 在更早 scan_run(排除当前 scan_run_id)中最近写入的一条价格。
    查不到(比如首次发现)或出错都返回 None。"""
    if listing_id is None:
        return None
    try:
        conn = _connect_db(db_file)
        try:
            row = conn.execute(
                """
                SELECT price_jpy FROM price_history
                WHERE listing_id = ? AND scan_run_id != ?
                ORDER BY id DESC LIMIT 1
                """,
                (listing_id, scan_run_id),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"查询历史价格失败 (listing_id={listing_id}): {e}")
        return None


def record_feedback_event(listing_id, legacy_item_id, url, action, reason, source,
                           dedupe_key, created_at, db_file=None):
    """追加一条反馈历史事件,不做 upsert,同一商品的多次反馈都保留。
    用 dedupe_key 的 UNIQUE 约束(INSERT OR IGNORE)保证幂等——同一个 dedupe_key
    重复调用不会产生重复行,这是 --migrate-feedback 可重复执行的关键。
    返回是否真正插入了新行(True)还是被幂等忽略/出错(False),不向调用方抛出。"""
    try:
        conn = _connect_db(db_file)
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO feedback_events
                    (listing_id, legacy_item_id, url, action, reason, source, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (listing_id, legacy_item_id, url, action, reason, source, dedupe_key, created_at),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"写入 feedback_events 失败 (url={url}): {e}")
        return False


def create_scan_run(started_at, db_file=None):
    """创建一条扫描运行记录,返回 scan_run_id;失败返回 None。"""
    try:
        conn = _connect_db(db_file)
        try:
            cur = conn.execute(
                "INSERT INTO scan_runs (started_at) VALUES (?)",
                (started_at,),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"创建 scan_run 失败: {e}")
        return None


def finish_scan_run(scan_run_id, finished_at, status, raw_item_count=None,
                     brand_matched_count=None, llm_input_count=None,
                     evaluated_count=None, candidate_count=None, db_file=None):
    """收尾一条扫描运行记录。scan_run_id 为 None(创建阶段就已失败)时直接跳过。

    status 只反映"这轮扫描是否正常跑完",不代表候选数量:
    - 正常跑完(即使候选为 0)写 'ok';
    - 已知且可恢复的部分流程异常写 'partial_failure'(Phase 1 暂无检测这种情况的信号源,
      预留给以后使用,当前代码路径不会产生这个值);
    - 捕获到未处理异常写 'error'。
    """
    if scan_run_id is None:
        return False
    try:
        conn = _connect_db(db_file)
        try:
            conn.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?, status = ?, raw_item_count = ?,
                    brand_matched_count = ?, llm_input_count = ?,
                    evaluated_count = ?, candidate_count = ?
                WHERE id = ?
                """,
                (finished_at, status, raw_item_count, brand_matched_count,
                 llm_input_count, evaluated_count, candidate_count, scan_run_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"收尾 scan_run 失败 (scan_run_id={scan_run_id}): {e}")
        return False
