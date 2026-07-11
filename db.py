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
import hashlib
import secrets
import re
from datetime import datetime, date


# ---------------------------------------------------------------------------
# 密码哈希（标准库 pbkdf2_hmac，无外部依赖；salt 随机、迭代 20 万次）
# 存储格式： pbkdf2$<salt_b64>$<hash_b64>
# ---------------------------------------------------------------------------
def _hash_password(pw):
    if not pw:
        return ""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000)
    return "pbkdf2$" + salt.hex() + "$" + dk.hex()


def verify_password(pw, stored):
    if not pw or not stored or not stored.startswith("pbkdf2$"):
        return False
    try:
        _, salt_hex, hash_hex = stored.split("$", 2)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# 简易强度校验：6-64 位，至少含两类字符
def password_strength_ok(pw):
    if not pw or not (6 <= len(pw) <= 64):
        return False
    kinds = 0
    if re.search(r"[a-z]", pw): kinds += 1
    if re.search(r"[A-Z]", pw): kinds += 1
    if re.search(r"\d", pw): kinds += 1
    if re.search(r"[^\w]", pw): kinds += 1
    return kinds >= 2

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
    _add_col(conn, "users", "password_hash", "TEXT")
    _add_col(conn, "users", "status", "TEXT", "'approved'")
    _add_col(conn, "users", "reviewed_at", "TEXT")
    _add_col(conn, "users", "reviewed_by", "TEXT")
    _add_col(conn, "users", "reject_reason", "TEXT")
    # 软删除标记（NULL/空=未删，有值=删除时间）
    _add_col(conn, "users", "deleted_at", "TEXT")
    # 是否已绑定微信（1=可用微信快捷登录）。web 注册账号默认 0，微信注册账号默认 1
    _add_col(conn, "users", "wx_bound", "INTEGER", "0")
    # 旧数据（status 为 NULL）统一视为已审核通过，避免存量用户被锁死
    conn.execute("UPDATE users SET status='approved' WHERE status IS NULL OR status=''")
    # 存量用户回填：微信注册的账号（openid 非 web_ 前缀、非 admin）视为已绑定微信
    conn.execute(
        "UPDATE users SET wx_bound=1 WHERE openid NOT LIKE 'web_%' AND openid<>'admin' "
        "AND (wx_bound IS NULL OR wx_bound=0)")
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
    # 预警阈值（提前 N 天提醒），JSON 数组
    _add_col(conn, "user_notif", "notify_days", "TEXT", "'[30,7]'")
    # 评级记录：续期/重评闭环标记
    _add_col(conn, "final_ratings", "renewed", "INTEGER", "0")
    _add_col(conn, "final_ratings", "renewed_at", "TEXT")


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

    CREATE TABLE IF NOT EXISTS audit_log(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_openid TEXT,
        actor_name   TEXT,
        action       TEXT,
        target       TEXT,
        detail       TEXT,
        created_at   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS messages(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        openid      TEXT,
        type        TEXT,
        title       TEXT,
        body        TEXT,
        rating_id   INTEGER,
        `read`      INTEGER DEFAULT 0,
        created_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_msg_openid ON messages(openid);

    CREATE TABLE IF NOT EXISTS notifications_sent(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        openid     TEXT,
        rating_id  INTEGER,
        notify_day INTEGER,
        channel    TEXT,
        sent_at    TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_ns_dedupe
        ON notifications_sent(openid, rating_id, notify_day, channel);
    """)
    _migrate_users(conn)
    _migrate_admin_source(conn)
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
def register_user(openid, organization, name, phone, email=None, role="user", password=None):
    """
    提交注册申请：创建待审核(pending)账号。
    若已审核通过(approved)则保持通过、仅更新资料；若待审核/已拒绝则重新置为待审核。
    password：明文，传入时哈希后存入 password_hash（用于 Web 登录/防冒名绑定）。
    """
    pw_hash = _hash_password(password) if password else ""
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()
    wx_bound = 0 if openid.startswith("web_") else 1  # 微信注册即视为已绑定
    if existing:
        c.execute(
            """UPDATE users SET organization=?, marketer_name=?, phone=?, email=?, role=?, wx_bound=?
               WHERE openid=?""",
            (organization, name, phone, email, role, wx_bound, openid))
        if pw_hash and not existing["password_hash"]:
            c.execute("UPDATE users SET password_hash=? WHERE openid=?",
                      (pw_hash, openid))
        if existing["status"] not in ("approved",):
            c.execute("UPDATE users SET status='pending' WHERE openid=?", (openid,))
    else:
        c.execute(
            """INSERT INTO users(openid, role, marketer_name, organization, phone, email, password_hash, wx_bound, status)
               VALUES(?,?,?,?,?,?,?,?,'pending')""",
            (openid, role, name, organization, phone, email, pw_hash, wx_bound))
    conn.commit()
    conn.close()


def set_password(openid, password):
    """设置/重置某用户的密码（哈希存储）。password 为空字符串则清空（禁用密码登录）。"""
    pw_hash = _hash_password(password)
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET password_hash=? WHERE openid=?", (pw_hash, openid))
    conn.commit()
    conn.close()


def user_has_password(openid):
    conn = get_conn()
    row = c = conn.execute("SELECT password_hash FROM users WHERE openid=?",
                           (openid,)).fetchone()
    conn.close()
    return bool(row and row["password_hash"])


def list_users(status=None, role=None, include_deleted=False):
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
    if not include_deleted:
        where.append("(deleted_at IS NULL OR deleted_at='')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (status='pending') DESC, created_at DESC"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_user(openid, **fields):
    """管理员修改用户资料。可改：marketer_name / organization / phone / email / role。
    返回 (ok, error_msg)。手机号变更时校验不与其它账号冲突。
    """
    allowed = {"marketer_name", "organization", "phone", "email", "role"}
    payload = {k: fields[k] for k in allowed if fields.get(k) is not None}
    if not payload:
        return False, "没有可更新的字段"
    conn = get_conn()
    c = conn.cursor()
    u = c.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()
    if not u:
        conn.close()
        return False, "用户不存在"
    # 手机号唯一性校验
    if "phone" in payload:
        new_phone = payload["phone"].strip()
        if new_phone and not re.match(r"^1[3-9]\d{9}$", new_phone):
            conn.close()
            return False, "手机号格式不正确"
        clash = c.execute(
            "SELECT openid FROM users WHERE phone=? AND openid!=?",
            (new_phone, openid)).fetchone()
        if clash:
            conn.close()
            return False, "该手机号已被其它账号使用"
        payload["phone"] = new_phone
    sets = ", ".join(f"{k}=?" for k in payload)
    c.execute(f"UPDATE users SET {sets} WHERE openid=?",
              list(payload.values()) + [openid])
    conn.commit()
    conn.close()
    return True, None


def soft_delete_user(openid, by=None):
    """软删除用户（标记 deleted_at，保留数据便于恢复）。"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET deleted_at=datetime('now'), status='deleted' "
              "WHERE openid=?", (openid,))
    conn.commit()
    conn.close()


def restore_user(openid, by=None):
    """恢复已软删用户（回到 approved 状态）。"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET deleted_at=NULL, status='approved' "
              "WHERE openid=?", (openid,))
    conn.commit()
    conn.close()


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


def bind_wechat_openid(phone, new_openid):
    """将手机号对应的账号绑定/迁移到真实微信 openid（code2Session 返回的 o 开头 openid）。

    兼容历史脏数据：同一手机号可能在库里存在多条记录（mock 时期的微信 openid、
    web_ 手机号 openid、已删除的重复项等）。此函数把所有同手机号记录的数据
    （user_notif / final_ratings）合并到 new_openid，删除重复记录，保留 new_openid
    为唯一账号（wx_bound=1、继承密码哈希与角色），避免数据丢失或迁移错对象。

    返回 (ok, user_dict | None, error_msg)。
    """
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchall()
    if not rows:
        conn.close()
        return False, None, "未找到使用该手机号的注册账号"
    new_exists = c.execute("SELECT * FROM users WHERE openid=?", (new_openid,)).fetchone()
    if new_exists and (new_exists["phone"] or "") != phone:
        conn.close()
        return False, None, "该微信已关联其他账号"
    old_openids = [r["openid"] for r in rows if r["openid"] != new_openid]
    if not old_openids and new_exists:
        c.execute("UPDATE users SET wx_bound=1 WHERE openid=?", (new_openid,))
        conn.commit()
        updated = c.execute("SELECT * FROM users WHERE openid=?", (new_openid,)).fetchone()
        conn.close()
        return True, dict(updated), "已绑定"
    # 主记录：取评级数据最多的那条，用于继承姓名/角色/状态
    def _rc(oid):
        return c.execute("SELECT count(*) FROM final_ratings WHERE openid=?",
                         (oid,)).fetchone()[0]
    primary = max(rows, key=lambda r: _rc(r["openid"]))
    # 状态取最优（已审核优先于待审/已删），避免绑定后账号被误锁
    best_status = "approved"
    for r in rows:
        if r["status"] == "approved":
            best_status = "approved"
            break
        elif r["status"] == "pending" and best_status not in ("approved",):
            best_status = "pending"
    # 继承密码哈希：优先主记录，否则同组任一条非空
    pw = (primary["password_hash"] or "")
    if not pw:
        for r in rows:
            if r["password_hash"]:
                pw = r["password_hash"]
                break
    # 把所有旧 openid 的子表数据迁到 new_openid
    for oid in old_openids:
        c.execute("UPDATE user_notif SET openid=? WHERE openid=?", (new_openid, oid))
        c.execute("UPDATE final_ratings SET openid=? WHERE openid=?", (new_openid, oid))
        c.execute("DELETE FROM users WHERE openid=?", (oid,))
    if new_exists:
        c.execute(
            """UPDATE users SET phone=?, marketer_name=?, role=?, status=?,
                   password_hash=?, wx_bound=1 WHERE openid=?""",
            (primary["phone"], primary["marketer_name"], primary["role"],
             best_status, pw, new_openid))
    else:
        new_row = dict(primary)
        new_row["openid"] = new_openid
        new_row["wx_bound"] = 1
        new_row["password_hash"] = pw
        new_row["status"] = best_status
        cols = ",".join(new_row.keys())
        placeholders = ",".join(["?"] * len(new_row))
        c.execute(f"INSERT INTO users({cols}) VALUES({placeholders})",
                  list(new_row.values()))
    conn.commit()
    updated = c.execute("SELECT * FROM users WHERE openid=?", (new_openid,)).fetchone()
    conn.close()
    return True, dict(updated), f"绑定成功（已合并 {len(old_openids)} 个账号，数据已迁移）"


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
            "rating": r["rating"] or "",
            "outlook": r["outlook"] or "",
            "notes": r["notes"] or "",
            "deleted_at": r["deleted_at"],
        })
    return out


def admin_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM admin_source").fetchone()["n"]
    conn.close()
    return n


def _migrate_admin_source(conn):
    """报告数据表扩展：评级等级 / 展望 / 备注 / 软删除标记。"""
    _add_col(conn, "admin_source", "rating", "TEXT")
    _add_col(conn, "admin_source", "outlook", "TEXT")
    _add_col(conn, "admin_source", "notes", "TEXT")
    _add_col(conn, "admin_source", "deleted_at", "TEXT")
    # 计算结果的评级等级 / 展望，随报告数据带入，便于前端展示
    _add_col(conn, "final_ratings", "rating", "TEXT")
    _add_col(conn, "final_ratings", "outlook", "TEXT")


# ---------------------------------------------------------------------------
# admin_source CRUD（管理员维护报告数据）/ 回收站 / 数据体检
# ---------------------------------------------------------------------------
def _row_to_report(r):
    # 日期统一返回 ISO 字符串（避免 Flask 把 date 序列化为 RFC822 长串，前端 <input type=date> 无法识别）
    li = _parse_iso(r["li_date"])
    iss = _parse_iso(r["issuance"])
    return {
        "id": r["id"],
        "subject": r["subject"] or "",
        "contract_no": r["contract_no"] or "",
        "li_date": li.isoformat() if li else "",
        "issuance": iss.isoformat() if iss else "",
        "issuance_source": r["issuance_source"],
        "project_type": r["project_type"] or "",
        "debt_type": r["debt_type"] or "",
        "rating": r["rating"] or "",
        "outlook": r["outlook"] or "",
        "notes": r["notes"] or "",
        "deleted_at": r["deleted_at"],
    }


def add_admin_rating(rec):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO admin_source
           (subject, contract_no, li_date, issuance, issuance_source,
            project_type, debt_type, rating, outlook, notes)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("subject"), rec.get("contract_no", ""),
         rec.get("li_date"), rec.get("issuance"), rec.get("issuance_source"),
         rec.get("project_type", ""), rec.get("debt_type", ""),
         rec.get("rating", ""), rec.get("outlook", ""), rec.get("notes", "")))
    rid = c.lastrowid
    conn.commit()
    conn.close()
    return rid


def update_admin_rating(rid, fields):
    allowed = {"subject", "contract_no", "li_date", "issuance",
               "issuance_source", "project_type", "debt_type",
               "rating", "outlook", "notes"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return False
    vals.append(rid)
    conn = get_conn()
    conn.execute(
        f"UPDATE admin_source SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return True


def soft_delete_admin_rating(rid):
    conn = get_conn()
    conn.execute(
        "UPDATE admin_source SET deleted_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def restore_admin_rating(rid):
    conn = get_conn()
    conn.execute(
        "UPDATE admin_source SET deleted_at=NULL WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def get_admin_rating(rid, include_deleted=False):
    conn = get_conn()
    if include_deleted:
        r = conn.execute("SELECT * FROM admin_source WHERE id=?", (rid,)).fetchone()
    else:
        r = conn.execute(
            "SELECT * FROM admin_source WHERE id=? AND deleted_at IS NULL",
            (rid,)).fetchone()
    conn.close()
    return _row_to_report(r) if r else None


def list_admin_ratings(q=None, sort=None, order="asc",
                       page=1, page_size=50, include_deleted=False):
    conn = get_conn()
    where, params = [], []
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if q:
        where.append("(subject LIKE ? OR contract_no LIKE ? OR notes LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    sort_whitelist = {"id", "subject", "contract_no", "li_date",
                      "issuance", "project_type", "debt_type", "rating",
                      "outlook", "deleted_at"}
    sort_col = sort if sort in sort_whitelist else "id"
    order_dir = "DESC" if order == "desc" else "ASC"
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM admin_source {sql_where}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM admin_source {sql_where} ORDER BY {sort_col} {order_dir} "
        f"LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()
    conn.close()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_row_to_report(r) for r in rows]}


def list_trashed():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM admin_source WHERE deleted_at IS NOT NULL "
        "ORDER BY deleted_at DESC").fetchall()
    conn.close()
    return [_row_to_report(r) for r in rows]


def batch_soft_delete(ids):
    if not ids:
        return 0
    conn = get_conn()
    qm = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE admin_source SET deleted_at=datetime('now') "
        f"WHERE id IN ({qm})", ids)
    n = conn.total_changes
    conn.commit()
    conn.close()
    return n


def batch_restore(ids):
    if not ids:
        return 0
    conn = get_conn()
    qm = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE admin_source SET deleted_at=NULL WHERE id IN ({qm})", ids)
    conn.commit()
    conn.close()
    return len(ids)


def find_duplicate_report(subject, contract_no, issuance, exclude_id=None):
    """智能去重：相同 (主体, 合同号, 出具日) 的其它有效记录。"""
    conn = get_conn()
    sql = ("SELECT id FROM admin_source WHERE subject=? AND contract_no=? "
           "AND issuance=? AND deleted_at IS NULL")
    params = [subject, contract_no, issuance]
    if exclude_id:
        sql += " AND id<>?"
        params.append(exclude_id)
    r = conn.execute(sql, params).fetchone()
    conn.close()
    return r["id"] if r else None


def admin_source_health():
    """数据体检：健康分 + 问题清单（基于报告数据字段质量）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM admin_source WHERE deleted_at IS NULL").fetchall()
    conn.close()
    issues, seen = [], {}
    today = date.today().isoformat()
    for r in rows:
        rid = r["id"]
        subj = r["subject"] or ""
        if not subj:
            issues.append(_issue("high", "主体", rid, "", "主体(机构名称)缺失"))
        if not r["issuance"]:
            issues.append(_issue("high", "出具日", rid, subj,
                                 "出具时间缺失，无法计算到期"))
        elif r["issuance"] > today:
            issues.append(_issue("mid", "出具日", rid, subj,
                                 "出具时间在未来，疑似录入错误"))
        if not r["contract_no"]:
            issues.append(_issue("low", "合同号", rid, subj,
                                 "合同号缺失（可能影响归属）"))
        key = (subj, r["contract_no"] or "", r["issuance"] or "")
        if key in seen:
            issues.append(_issue("mid", "重复", rid, subj,
                                 f"与记录 #{seen[key]} 主体/合同号/出具日完全相同"))
        else:
            seen[key] = rid
    total = len(rows)
    bad = len(issues)
    score = max(0, 100 - bad * 5) if total else 100
    counts = {"high": 0, "mid": 0, "low": 0}
    for it in issues:
        counts[it["level"]] += 1
    return {"total": total, "issues": issues, "score": score,
            "counts": counts}


def _issue(level, field, rid, subject, msg):
    return {"level": level, "field": field, "id": rid,
            "subject": subject, "msg": msg}



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
                source, extra_json, rating, outlook)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["openid"], r["subject"], r.get("contract_no", ""),
             r["base_date"], r["expiry_date"], r["remind_date"], r["status"],
             r.get("debt_type", ""), r.get("project_type", ""),
             r.get("attribution", ""), r.get("source", ""),
             json.dumps(r.get("extra", {}), ensure_ascii=False),
             r.get("rating"), r.get("outlook")))
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
# 审计日志
# ---------------------------------------------------------------------------
def log_audit(actor_openid, action, target=None, detail=None):
    conn = get_conn()
    c = conn.cursor()
    name = None
    if actor_openid:
        u = c.execute("SELECT marketer_name FROM users WHERE openid=?",
                      (actor_openid,)).fetchone()
        name = u["marketer_name"] if u else None
    c.execute(
        "INSERT INTO audit_log(actor_openid, actor_name, action, target, detail) "
        "VALUES(?,?,?,?,?)",
        (actor_openid, name, action, target, detail))
    conn.commit()
    conn.close()


def get_audit_log(page=1, page_size=50):
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size)).fetchall()
    conn.close()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 站内消息中心
# ---------------------------------------------------------------------------
def add_message(openid, mtype, title, body, rating_id=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages(openid, type, title, body, rating_id) "
        "VALUES(?,?,?,?,?)",
        (openid, mtype, title, body, rating_id))
    conn.commit()
    conn.close()


def get_messages(openid, unread_only=False, page=1, page_size=20):
    conn = get_conn()
    where = "WHERE openid=?" + (" AND `read`=0" if unread_only else "")
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM messages {where}", (openid,)).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (openid, page_size, (page - 1) * page_size)).fetchall()
    unread = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE openid=? AND `read`=0",
        (openid,)).fetchone()["n"]
    conn.close()
    return {"total": total, "unread": unread, "page": page,
            "page_size": page_size, "items": [dict(r) for r in rows]}


def mark_message_read(openid, msg_id=None):
    conn = get_conn()
    c = conn.cursor()
    if msg_id:
        c.execute("UPDATE messages SET `read`=1 WHERE openid=? AND id=?",
                  (openid, msg_id))
    else:
        c.execute("UPDATE messages SET `read`=1 WHERE openid=?", (openid,))
    conn.commit()
    conn.close()


def unread_count(openid):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE openid=? AND `read`=0",
        (openid,)).fetchone()["n"]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# 推送去重（避免定时任务每日重复发送）
# ---------------------------------------------------------------------------
def already_sent(openid, rating_id, notify_day, channel):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications_sent "
        "WHERE openid=? AND rating_id=? AND notify_day=? AND channel=?",
        (openid, rating_id, notify_day, channel)).fetchone()["n"]
    conn.close()
    return n > 0


def mark_sent(openid, rating_id, notify_day, channel):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO notifications_sent(openid, rating_id, notify_day, channel) "
        "VALUES(?,?,?,?)", (openid, rating_id, notify_day, channel))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 预警阈值（提前 N 天提醒）
# ---------------------------------------------------------------------------
def get_notify_days(openid):
    n = get_notif(openid)
    if n and n.get("notify_days"):
        try:
            return json.loads(n["notify_days"])
        except Exception:
            pass
    return [30, 7]


def set_notify_days(openid, days):
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute(
        "SELECT * FROM user_notif WHERE openid=?", (openid,)).fetchone()
    js = json.dumps(days, ensure_ascii=False)
    if existing:
        c.execute(
            "UPDATE user_notif SET notify_days=?, updated_at=datetime('now') "
            "WHERE openid=?", (js, openid))
    else:
        c.execute(
            "INSERT INTO user_notif(openid, notify_days) VALUES(?,?)",
            (openid, js))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 续期 / 重评闭环
# ---------------------------------------------------------------------------
def mark_renewed(rating_id, openid=None, new_expiry=None):
    conn = get_conn()
    c = conn.cursor()
    if openid:
        c.execute(
            "UPDATE final_ratings SET renewed=1, renewed_at=datetime('now') "
            "WHERE id=? AND openid=?", (rating_id, openid))
    else:
        c.execute(
            "UPDATE final_ratings SET renewed=1, renewed_at=datetime('now') "
            "WHERE id=?", (rating_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 搜索 / 排序 / 分页查询
# ---------------------------------------------------------------------------
def query_my_ratings(openid, status_filter=None, q=None, sort="status",
                     page=1, page_size=50, include_renewed=False):
    conn = get_conn()
    base = "FROM final_ratings WHERE openid=?"
    params = [openid]
    clause = ""
    if not include_renewed:
        clause += " AND (renewed IS NULL OR renewed=0)"
    if status_filter:
        clause += " AND status=?"
        params.append(status_filter)
    if q:
        clause += " AND (subject LIKE ? OR contract_no LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    order = {
        "status": "ORDER BY (status!='overdue'), expiry_date",
        "expiry_asc": "ORDER BY expiry_date ASC",
        "expiry_desc": "ORDER BY expiry_date DESC",
        "subject": "ORDER BY subject ASC",
    }.get(sort, "ORDER BY (status!='overdue'), expiry_date")
    total = conn.execute(
        f"SELECT COUNT(*) AS n {base} {clause}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * {base} {clause} {order} LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()
    conn.close()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [dict(r) for r in rows]}


def overview_excluding_renewed():
    """管理员总览：已续期/已重评的评级不计入未结存量。"""
    conn = get_conn()
    by_status = conn.execute(
        "SELECT status, COUNT(*) AS n FROM final_ratings "
        "WHERE renewed=0 OR renewed IS NULL GROUP BY status").fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM final_ratings "
        "WHERE renewed=0 OR renewed IS NULL").fetchone()["n"]
    by_marketer = conn.execute(
        "SELECT openid, status, COUNT(*) AS n FROM final_ratings "
        "WHERE renewed=0 OR renewed IS NULL GROUP BY openid, status").fetchall()
    unassigned = conn.execute(
        "SELECT COUNT(*) AS n FROM final_ratings "
        "WHERE (renewed=0 OR renewed IS NULL) AND attribution='unassigned'"
        ).fetchone()["n"]
    conn.close()
    bs = {r["status"]: r["n"] for r in by_status}
    mkt = {}
    for r in by_marketer:
        mkt.setdefault(r["openid"], {})[r["status"]] = r["n"]
    return {"total": total, "by_status": bs, "unassigned": unassigned,
            "by_marketer": mkt}


def calendar_expiry(openid=None, months=6):
    """按到期日聚合，用于日历热力图。返回 {start,end,days:{date:{status:count}}}。"""
    from datetime import date, timedelta
    import calendar as _cal
    conn = get_conn()
    today = date.today()
    start = today.replace(day=1)
    y, m = start.year, start.month
    for _ in range(months):
        m += 1
        if m > 12:
            y += 1
            m = 1
    end = date(y, m, 1)
    sql = ("SELECT expiry_date, status, COUNT(*) AS n FROM final_ratings "
           "WHERE renewed=0 OR renewed IS NULL")
    params = []
    if openid:
        sql += " AND openid=?"
        params.append(openid)
    sql += " GROUP BY expiry_date, status"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    days = {}
    for r in rows:
        if not r["expiry_date"]:
            continue
        d = days.setdefault(r["expiry_date"], {"overdue": 0, "due": 0,
                                               "upcoming": 0})
        if r["status"] in d:
            d[r["status"]] = r["n"]
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days}


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
