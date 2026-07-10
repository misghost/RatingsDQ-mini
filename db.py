# -*- coding: utf-8 -*-
"""
db.py — 评级到期提醒后端 存储层（SQLite，单文件）

表设计：
  users              微信用户（openid + 角色 + 绑定的市场人员名）
  admin_source       管理员后台总体源数据（评级真相 + 出具时间）
  contract_uploads   市场人员上传的合同管理（合同号 -> openid 主归属）
  fallback_uploads   市场人员上传的承揽/作业（兜底，subject+基准日）
  upload_log         上传日志（校验摘要、映射建议）
  final_ratings      归属 + 到期计算后的最终记录（按 openid 隔离）

所有写操作集中在此文件，server.py 只调用这些函数。
"""

import sqlite3
import os
import json
from datetime import datetime

def _db_path():
    """DB 路径动态读取，便于云托管挂载 CFS 卷（如 /data/rating.db）。"""
    return os.environ.get("RATING_DB",
                          os.path.join(os.path.dirname(__file__), "rating.db"))


def _ensure_writable(path):
    """确保数据库父目录存在；若目标路径不可写则回退到容器内可写位置。"""
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        return path
    except OSError:
        # 无法创建父目录（如只读根盘）→ 回退到 /app 下
        fb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rating.db")
        os.makedirs(os.path.dirname(fb), exist_ok=True)
        return fb


def get_conn():
    path = _ensure_writable(_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：多实例/多连接下读不阻塞写，缓解 SQLite 并发写锁
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _add_col(conn, table, col, coltype, default=None):
    """SQLite 不支持 IF NOT EXISTS 的 ALTER，手动判断列是否存在后再加。"""
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col in cols:
        return
    ddl = f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"
    if default is not None:
        ddl += f" DEFAULT {default}"
    conn.execute(ddl)


def _migrate_users(conn):
    """注册 / 审核能力所需的用户表扩展（向后兼容：旧用户视为已审核通过）。"""
    _add_col(conn, "users", "organization", "TEXT")
    _add_col(conn, "users", "phone", "TEXT")
    _add_col(conn, "users", "email", "TEXT")
    _add_col(conn, "users", "status", "TEXT", "'approved'")
    _add_col(conn, "users", "reviewed_at", "TEXT")
    _add_col(conn, "users", "reviewed_by", "TEXT")
    _add_col(conn, "users", "reject_reason", "TEXT")
    # 旧数据（status 为 NULL）统一视为已审核通过，避免存量用户被锁死
    conn.execute("UPDATE users SET status='approved' WHERE status IS NULL OR status=''")
    # 通知偏好表
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_notif(
        openid          TEXT PRIMARY KEY,
        channels        TEXT,            -- JSON 数组: ["miniprogram","email"]
        email           TEXT,
        wx_subscribed   INTEGER DEFAULT 0,
        wx_subscribe_at TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""")


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        openid          TEXT PRIMARY KEY,
        role            TEXT NOT NULL DEFAULT 'user',  -- user / admin
        marketer_name   TEXT,                          -- 绑定的市场人员姓名
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS admin_source(
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        subject         TEXT,
        contract_no     TEXT,
        li_date         TEXT,     -- 立项日期
        issuance       TEXT,     -- 出具时间(报告落款日 -> 评审 -> 打印)
        issuance_source TEXT,    -- 出具时间来源列名
        project_type    TEXT,
        debt_type       TEXT,
        UNIQUE(subject, contract_no, issuance)
    );
    CREATE INDEX IF NOT EXISTS idx_admin_contract ON admin_source(contract_no);
    CREATE INDEX IF NOT EXISTS idx_admin_subject  ON admin_source(subject);

    CREATE TABLE IF NOT EXISTS contract_uploads(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        openid      TEXT,
        contract_no TEXT,
        marketer    TEXT,
        entrust     TEXT,
        bond        TEXT,
        status      TEXT,
        uploaded_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_ct_openid    ON contract_uploads(openid);
    CREATE INDEX IF NOT EXISTS idx_ct_contract  ON contract_uploads(contract_no);

    CREATE TABLE IF NOT EXISTS fallback_uploads(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        openid       TEXT,
        source_label TEXT,    -- chenlan / zuoye
        subject      TEXT,
        base_date    TEXT,
        bond_type    TEXT,
        status_raw   TEXT,
        uploaded_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_fb_openid   ON fallback_uploads(openid);
    CREATE INDEX IF NOT EXISTS idx_fb_subject  ON fallback_uploads(subject);

    CREATE TABLE IF NOT EXISTS upload_log(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        openid       TEXT,
        file_type    TEXT,    -- admin_xls / contract / chenlan / zuoye
        filename     TEXT,
        rows_kept    INTEGER,
        rows_dropped INTEGER,
        mapping_json TEXT,
        uploaded_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS final_ratings(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        openid       TEXT,
        subject      TEXT,
        contract_no  TEXT,
        base_date    TEXT,    -- 用于算到期的基准（出具时间）
        expiry_date  TEXT,
        remind_date  TEXT,
        status       TEXT,    -- overdue / due / upcoming
        debt_type    TEXT,
        project_type TEXT,
        attribution  TEXT,    -- contract_join / window_match / unassigned
        source       TEXT,    -- 来源标记
        extra_json   TEXT,
        computed_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_final_openid ON final_ratings(openid);
    """)
    _migrate_users(conn)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
def upsert_user(openid, role=None, marketer_name=None, status=None):
    """
    仅供内部/管理员流程调用（登录态已确定）。
    新增用户默认 status='approved'（不自动创建待审核账号——注册须走 register_user）。
    已存在用户：不改动其 status（审核状态由审核流程控制），也不覆盖已审核信息。
    """
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()
    if existing:
        # 仅补充 role（admin 不降级）与 marketer_name（不覆盖已有）
        if role == "admin" and existing["role"] != "admin":
            c.execute("UPDATE users SET role='admin' WHERE openid=?", (openid,))
        if marketer_name and not existing["marketer_name"]:
            c.execute("UPDATE users SET marketer_name=? WHERE openid=?",
                      (marketer_name, openid))
    else:
        c.execute("INSERT INTO users(openid, role, marketer_name, status) VALUES(?,?,?,?)",
                  (openid, role or "user", marketer_name, status or "approved"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 注册 / 审核
# ---------------------------------------------------------------------------
def register_user(openid, organization, name, phone, email=None, role="user"):
    """
    提交注册申请：创建待审核(pending)账号。
    若已审核通过(approved)则保持通过、仅更新资料；若待审核/已拒绝则重新置为待审核。
    """
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()
    if existing:
        c.execute(
            """UPDATE users SET organization=?, marketer_name=?, phone=?, email=?, role=?
               WHERE openid=?""",
            (organization, name, phone, email, role, openid))
        if existing["status"] not in ("approved",):
            c.execute("UPDATE users SET status='pending' WHERE openid=?", (openid,))
    else:
        c.execute(
            """INSERT INTO users(openid, role, marketer_name, organization, phone, email, status)
               VALUES(?,?,?,?,?,?,'pending')""",
            (openid, role, name, organization, phone, email))
    conn.commit()
    conn.close()


def list_users(status=None, role=None):
    conn = get_conn()
    c = conn.cursor()
    sql = "SELECT * FROM users"
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if role:
        where.append("role=?")
        params.append(role)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (status='pending') DESC, created_at DESC"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_user_status(openid, status, reviewed_by=None, reason=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """UPDATE users SET status=?, reviewed_at=datetime('now'),
           reviewed_by=?, reject_reason=? WHERE openid=?""",
        (status, reviewed_by, reason, openid))
    conn.commit()
    conn.close()


def count_pending():
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE status='pending'").fetchone()["n"]
    conn.close()
    return n



def get_user(openid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_user_by_phone_or_name(phone, name):
    """按手机号或姓名解析已注册账号（兼容旧版姓名身份与新版手机号身份）。

    优先手机号，其次姓名（marketer_name）。用于 Web 登录，使历史账号（仅姓名、
    无手机号）与新注册账号（有手机号）都能登录。
    """
    conn = get_conn()
    c = conn.cursor()
    u = None
    if phone:
        u = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if not u and name:
        u = c.execute("SELECT * FROM users WHERE marketer_name=?",
                      (name,)).fetchone()
    conn.close()
    return dict(u) if u else None


def get_admin_openids():
    conn = get_conn()
    rows = conn.execute("SELECT openid FROM users WHERE role='admin'").fetchall()
    conn.close()
    return [r["openid"] for r in rows]


# ---------------------------------------------------------------------------
# admin_source
# ---------------------------------------------------------------------------
def replace_admin_source(recs):
    """全量替换后台源数据。recs: list[dict]。"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM admin_source")
    for r in recs:
        c.execute(
            """INSERT OR IGNORE INTO admin_source
               (subject, contract_no, li_date, issuance, issuance_source,
                project_type, debt_type)
               VALUES(?,?,?,?,?,?,?)""",
            (r["subject"], r.get("contract_no", ""),
             r["li_date"].isoformat() if r.get("li_date") else None,
             r["issuance"].isoformat() if r.get("issuance") else None,
             r.get("issuance_source"), r.get("project_type", ""),
             r.get("debt_type", "")))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM admin_source").fetchone()["n"]
    conn.close()
    return n


def get_all_admin():
    """返回后台评级记录列表（date 对象）。"""
    from datetime import date
    conn = get_conn()
    rows = conn.execute("SELECT * FROM admin_source").fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "subject": r["subject"],
            "contract_no": r["contract_no"] or "",
            "li_date": _parse_iso(r["li_date"]),
            "issuance": _parse_iso(r["issuance"]),
            "issuance_source": r["issuance_source"],
            "project_type": r["project_type"],
            "debt_type": r["debt_type"],
        })
    return out


def admin_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM admin_source").fetchone()["n"]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# contract_uploads
# ---------------------------------------------------------------------------
def replace_contract_uploads(openid, rows):
    """全量替换某 openid 的合同上传（一份文件 = 一次上传）。"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM contract_uploads WHERE openid=?", (openid,))
    for r in rows:
        c.execute(
            """INSERT INTO contract_uploads
               (openid, contract_no, marketer, entrust, bond, status)
               VALUES(?,?,?,?,?,?)""",
            (openid, r["contract_no"], r["marketer"], r["entrust"],
             r["bond"], r["status"]))
    conn.commit()
    conn.close()


def get_contract_openid_map():
    """返回 contract_no -> openid（主归属表，last-write-wins）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT contract_no, openid FROM contract_uploads").fetchall()
    conn.close()
    return {r["contract_no"]: r["openid"] for r in rows}


def get_my_contract_count(openid):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM contract_uploads WHERE openid=?",
        (openid,)).fetchone()["n"]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# fallback_uploads
# ---------------------------------------------------------------------------
def replace_fallback_uploads(openid, source_label, rows):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM fallback_uploads WHERE openid=? AND source_label=?",
              (openid, source_label))
    for r in rows:
        c.execute(
            """INSERT INTO fallback_uploads
               (openid, source_label, subject, base_date, bond_type, status_raw)
               VALUES(?,?,?,?,?,?)""",
            (openid, source_label, r["subject"],
             r["base_date"].isoformat() if r.get("base_date") else None,
             r.get("bond_type", ""), r.get("status_raw", "")))
    conn.commit()
    conn.close()


def get_fallback_index():
    """返回 subject -> [(openid, base_date), ...]（兜底用）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT subject, openid, base_date FROM fallback_uploads").fetchall()
    conn.close()
    from collections import defaultdict
    idx = defaultdict(list)
    for r in rows:
        if r["base_date"]:
            from datetime import date
            idx[r["subject"]].append((r["openid"], _parse_iso(r["base_date"])))
    return idx


# ---------------------------------------------------------------------------
# upload_log
# ---------------------------------------------------------------------------
def log_upload(openid, file_type, filename, rows_kept, rows_dropped, mapping):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO upload_log
           (openid, file_type, filename, rows_kept, rows_dropped, mapping_json)
           VALUES(?,?,?,?,?,?)""",
        (openid, file_type, filename, rows_kept, rows_dropped,
         json.dumps(mapping, ensure_ascii=False)))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# final_ratings
# ---------------------------------------------------------------------------
def replace_final_ratings(rows):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM final_ratings")
    for r in rows:
        c.execute(
            """INSERT INTO final_ratings
               (openid, subject, contract_no, base_date, expiry_date,
                remind_date, status, debt_type, project_type, attribution,
                source, extra_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["openid"], r["subject"], r.get("contract_no", ""),
             r["base_date"], r["expiry_date"], r["remind_date"], r["status"],
             r.get("debt_type", ""), r.get("project_type", ""),
             r.get("attribution", ""), r.get("source", ""),
             json.dumps(r.get("extra", {}), ensure_ascii=False)))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM final_ratings").fetchone()["n"]
    conn.close()
    return n


def get_my_ratings(openid, status_filter=None):
    conn = get_conn()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM final_ratings WHERE openid=? AND status=? "
            "ORDER BY (status!='overdue'), expiry_date",
            (openid, status_filter)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM final_ratings WHERE openid=? "
            "ORDER BY (status!='overdue'), expiry_date",
            (openid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_admin_overview():
    """管理员总览：按状态聚合 + 按市场人员(openid)下钻计数。"""
    conn = get_conn()
    by_status = conn.execute(
        "SELECT status, COUNT(*) AS n FROM final_ratings GROUP BY status").fetchall()
    by_marketer = conn.execute(
        """SELECT openid, status, COUNT(*) AS n
           FROM final_ratings GROUP BY openid, status""").fetchall()
    total = conn.execute("SELECT COUNT(*) AS n FROM final_ratings").fetchone()["n"]
    unassigned = conn.execute(
        "SELECT COUNT(*) AS n FROM final_ratings WHERE attribution='unassigned'"
        ).fetchone()["n"]
    conn.close()
    bs = {r["status"]: r["n"] for r in by_status}
    mkt = {}
    for r in by_marketer:
        mkt.setdefault(r["openid"], {})[r["status"]] = r["n"]
    return {"total": total, "by_status": bs, "unassigned": unassigned,
            "by_marketer": mkt}


def get_marketer_ratings(openid):
    return get_my_ratings(openid)


# ---------------------------------------------------------------------------
# 通知偏好（user_notif）
# ---------------------------------------------------------------------------
def get_notif(openid):
    conn = get_conn()
    r = conn.execute("SELECT * FROM user_notif WHERE openid=?", (openid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def set_notif(openid, channels_json, email):
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT * FROM user_notif WHERE openid=?", (openid,)).fetchone()
    if existing:
        c.execute(
            "UPDATE user_notif SET channels=?, email=?, updated_at=datetime('now') "
            "WHERE openid=?", (channels_json, email, openid))
    else:
        c.execute(
            "INSERT INTO user_notif(openid, channels, email) VALUES(?,?,?)",
            (openid, channels_json, email))
    conn.commit()
    conn.close()


def set_notif_subscribed(openid, subscribed):
    conn = get_conn()
    c = conn.cursor()
    flag = 1 if subscribed else 0
    existing = c.execute(
        "SELECT * FROM user_notif WHERE openid=?", (openid,)).fetchone()
    if existing:
        c.execute(
            "UPDATE user_notif SET wx_subscribed=?, wx_subscribe_at=datetime('now'), "
            "updated_at=datetime('now') WHERE openid=?", (flag, openid))
    else:
        c.execute(
            "INSERT INTO user_notif(openid, wx_subscribed, wx_subscribe_at) "
            "VALUES(?,?,datetime('now'))", (openid, flag))
    conn.commit()
    conn.close()


def get_approved_users_with_notif():
    """已审核通过、且至少开启一个通知渠道的市场人员（不含 admin）。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT u.openid, u.marketer_name, u.organization, u.phone, u.email AS u_email,
                  n.channels, n.email AS n_email, n.wx_subscribed
           FROM users u
           LEFT JOIN user_notif n ON u.openid = n.openid
           WHERE u.status='approved' AND u.role!='admin'
             AND n.channels IS NOT NULL AND n.channels != '[]'""").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["channels"] = json.loads(d["channels"] or "[]")
        d["email"] = d["n_email"] or d["u_email"]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
def _parse_iso(s):
    if not s:
        return None
    from datetime import date
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    init_db()
    print("DB initialized at", _db_path())
