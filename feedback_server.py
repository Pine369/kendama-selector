"""
反馈接收端

飞书卡片按钮点击后会跳转到这里,把决策(买入/放弃 + 原因)写进 SQLite。
本服务只监听本机 5001 端口,不直接对公网暴露;对外的 HTTPS 入口由 Caddy
反向代理提供(公网地址例如 https://feedback.pine369.com/feedback,反代到
127.0.0.1:5001),把这个 HTTPS 地址写进主程序的 .env 里的 FEEDBACK_URL。
5001 端口本身是否暴露到公网,取决于服务器防火墙/安全组配置,应确保只有
Caddy 能访问它,不应该被外部直接连接到。

请求必须带有效的 HMAC-SHA256 签名(见 main.py 的 sign_feedback_params),
签名密钥来自环境变量 FEEDBACK_SIGNING_SECRET,须与主程序一致。
缺签名/签名错误一律拒绝写入,返回 403。

启动:
    python feedback_server.py

查看记录:
    sqlite3 feedback.db "SELECT * FROM feedback ORDER BY ts DESC LIMIT 20;"
"""
import os
import hmac
import hashlib
import sqlite3
import logging
from html import escape
from datetime import datetime

from flask import Flask, request
from dotenv import load_dotenv

import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

DB_FILE = "feedback.db"
PORT = int(os.getenv("FEEDBACK_PORT", "5001"))
SIGNING_SECRET = os.getenv("FEEDBACK_SIGNING_SECRET")

app = Flask(__name__)


def expected_signature(item_id, action, reason):
    """与 main.py 的 sign_feedback_params 保持一致的签名算法。"""
    payload = "\x1f".join([item_id, action, reason])
    return hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signature_valid(item_id, action, reason, sig):
    """密钥未配置、签名缺失或不匹配都视为无效,一律拒绝(fail closed)。"""
    if not SIGNING_SECRET or not sig:
        return False
    return hmac.compare_digest(expected_signature(item_id, action, reason), sig)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            url TEXT,
            action TEXT,
            reason TEXT,
            ts TEXT
        )
    """)
    conn.commit()
    conn.close()


@app.route("/feedback")
def feedback():
    item_id = request.args.get("id", "")
    action = request.args.get("action", "")
    reason = request.args.get("reason", "")
    url = request.args.get("url", "")
    sig = request.args.get("sig", "")

    if not item_id or not action:
        return "missing params", 400

    if not signature_valid(item_id, action, reason, sig):
        logger.warning(f"反馈签名校验失败,拒绝写入: id={item_id} action={action}")
        return "invalid signature", 403

    ts = datetime.now().isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(DB_FILE)
        # 同一 item_id 重复点击以最后一次为准
        conn.execute(
            "INSERT OR REPLACE INTO feedback (id, url, action, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (item_id, url, action, reason, ts),
        )
        conn.commit()
        conn.close()
        logger.info(f"记录反馈: {item_id} {action} ({reason})")
    except Exception as e:
        logger.error(f"写入失败: {e}")
        return "db error", 500

    # 旧库写入成功后,追加写入 kendama.db.feedback_events 留存完整历史(不覆盖、可查多条)。
    # 这里额外包一层 try/except:虽然 db.py 的公开函数自己已经吞异常,但这是 HTTP 请求处理
    # 路径,任何未预期的异常都不能让本已成功的合法反馈请求返回非 200。
    try:
        listing_id = db.get_listing_id_by_url(url) if url else None
        dedupe_key = f"live:{url}:{action}:{reason}:{datetime.now().isoformat()}"
        db.record_feedback_event(
            listing_id=listing_id,
            legacy_item_id=item_id,
            url=url,
            action=action,
            reason=reason,
            source="live",
            dedupe_key=dedupe_key,
            created_at=ts,
        )
    except Exception as e:
        logger.error(f"写入 feedback_events 失败(不影响反馈接口): {e}")

    # 极简页面,浏览器跳过来看到这个就行
    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>已记录</title>
    <style>
      body {{ font-family: -apple-system, sans-serif; padding: 40px;
              text-align: center; color: #333; }}
      h2 {{ color: #2c7be5; }}
      .meta {{ color: #888; margin-top: 12px; font-size: 14px; }}
    </style></head>
    <body>
      <h2>✓ 已记录</h2>
      <p>{escape(action)} · {escape(reason)}</p>
      <p class="meta">{ts}</p>
      <p class="meta">关闭此页面即可</p>
    </body></html>
    """


@app.route("/health")
def health():
    return {"ok": True, "ts": datetime.now().isoformat(timespec="seconds")}


if __name__ == "__main__":
    init_db()
    if not db.init_db():
        logger.warning("kendama.db 初始化失败,反馈事件追加写入将被跳过")
    logger.info(f"反馈接收端启动,端口 {PORT}")
    app.run(host="0.0.0.0", port=PORT)