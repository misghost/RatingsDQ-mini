# -*- coding: utf-8 -*-
"""
server.py — 评级到期提醒 后端 API 骨架（Flask）

数据流：
  管理员上传 项目查询导出.xls  → admin_source（评级真相 + 出具时间）
  市场人员上传 合同管理.xlsx    → contract_uploads（合同号 -> openid 主归属）
  市场人员上传 承揽/作业.xlsx   → fallback_uploads（兜底 subject + 基准日）
  触发 compute:
      对每条后台评级记录:
        1) 合同号 join（命中 → 该 openid）            [主，精准]
        2) 否则 客户名 + 时间窗口 兜底                [兜底]
        3) 否则 unassigned（仅管理员可见）
      计算 到期日 / 提醒日 / 三态，按 openid 落 final_ratings
  个人 GET /api/my/ratings        → 只看到自己归属的到期提醒
  管理员 GET /api/admin/overview  → 整体聚合 + 按市场人员下钻

认证（骨架简化，便于联调）：
  - 微信小程序真实接入点见 login() 内注释。
  - 当前：前端每次请求带 header `X-Openid`（= 微信 openid）。
  - /api/login {code} 返回 openid（骨架把 code 当 openid，真实应调 code2Session）。
"""

import os
import io
import json
import tempfile
import hashlib
import urllib.request
import urllib.parse
from datetime import date, datetime

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from collections import defaultdict

import db
import rating_engine as eng
import admin_source as adm

# 微信登录真实接入（云托管生产用）。未配置时退化为 mock（code 当 openid）。
WX_APPID = os.environ.get("WX_APPID")
WX_SECRET = os.environ.get("WX_SECRET")

# 演示种子文件（仅本地预览用，生产环境删除此路由与常量）
DEMO_ADMIN_XLS = "/Volumes/D/编程/业绩到期查询/项目查询导出.xls"
DEMO_CONTRACT_XLSX = "/Volumes/D/编程/业绩到期查询/自定义上传优化版/合同管理.xlsx"

app = Flask(__name__)
CORS(app)  # 开发期允许小程序跨域；生产可收紧

# 默认参数（可用环境变量覆盖）
VALIDITY = int(os.environ.get("VALIDITY", "12"))       # 有效期(月)
REMIND = int(os.environ.get("REMIND", "3"))             # 提醒窗口(月)
OVERDUE_WINDOW = int(os.environ.get("OVERDUE_WINDOW", "12"))  # 过期噪音窗口(月)
FALLBACK_WINDOW = int(os.environ.get("FALLBACK_WINDOW", "24"))  # 兜底窗口(月)

db.init_db()


# ---------------------------------------------------------------------------
# 认证辅助
# ---------------------------------------------------------------------------
def current_openid():
    """从 header / query 取 openid（骨架简化）。"""
    oid = request.headers.get("X-Openid") or request.args.get("openid")
    return oid or None


def require_openid():
    oid = current_openid()
    if not oid:
        return None, jsonify({"error": "missing X-Openid header"}), 401
    u = db.get_user(oid)
    if not u:
        return None, jsonify({"error": "unknown user, login first"}), 403
    return oid, None, None


@app.route("/api/login", methods=["POST"])
def login():
    """
    微信登录。
    真实接入（云托管生产）：配置 WX_APPID / WX_SECRET 后，用小程序 wx.login() 拿到的
    code 调 code2Session 换取 openid。
    未配置时退化为 mock：直接把 code 当作 openid（演示/联调）。
    """
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    role = body.get("role", "user")
    if not code:
        return jsonify({"error": "code required"}), 400

    if code == "admin":
        openid = "admin"
    elif WX_APPID and WX_SECRET:
        # 真实 code2Session
        url = ("https://api.weixin.qq.com/sns/jscode2session?appid=%s"
               "&secret=%s&js_code=%s&grant_type=authorization_code") % (
            urllib.parse.quote(WX_APPID), urllib.parse.quote(WX_SECRET),
            urllib.parse.quote(code))
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                d = json.loads(resp.read())
        except Exception as e:
            return jsonify({"error": f"code2Session error: {e}"}), 502
        if not d.get("openid"):
            return jsonify({"error": "code2Session failed", "detail": d}), 400
        openid = d["openid"]
    else:
        # 退化 mock：code -> openid（演示/联调）
        openid = hashlib.sha1(code.encode("utf-8")).hexdigest()[:20]

    db.upsert_user(openid, role=role, marketer_name=None)
    return jsonify({"openid": openid, "role": db.get_user(openid)["role"],
                    "token": openid})  # 骨架 token == openid


# ---------------------------------------------------------------------------
# 上传：管理员后台源数据
# ---------------------------------------------------------------------------
@app.route("/api/admin/source", methods=["POST"])
def upload_admin_source():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    path = _save_tmp(f, ".xls")
    try:
        recs = adm.parse_admin_xls(path)
    except Exception as e:
        return jsonify({"error": f"解析失败: {e}"}), 422
    n = db.replace_admin_source(recs)
    db.log_upload(oid, "admin_xls", f.filename, n, 0, {})
    # 自动触发一次计算（管理员源数据变化后）
    stats = run_compute()
    return jsonify({"admin_records": n, "compute_stats": stats})


# ---------------------------------------------------------------------------
# 上传：市场人员合同管理（合同号 join 主归属）
# ---------------------------------------------------------------------------
@app.route("/api/upload/contract", methods=["POST"])
def upload_contract():
    oid, err, code = require_openid()
    if err:
        return err, code
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    market_as = request.form.get("market_as", "").strip()
    path = _save_tmp(f, ".xlsx")
    try:
        cmap_rows = adm.parse_contract_mgmt(path)  # contract_no -> {...}
    except Exception as e:
        return jsonify({"error": f"解析失败: {e}"}), 422

    marketers = sorted({v["marketer"] for v in cmap_rows.values() if v["marketer"]})
    if not marketers:
        return jsonify({"error": "未识别到任何有效合同号或市场人员"}), 422
    if not market_as:
        if len(marketers) == 1:
            market_as = marketers[0]
        else:
            # 多人混合文件：让前端用户选择“我是谁”
            return jsonify({
                "error": "multiple_marketers",
                "marketers": marketers,
                "hint": "请带参数 market_as=<你的姓名> 重新上传，或只上传自己的文件"
            }), 409

    # 绑定 openid -> 市场人员姓名
    db.upsert_user(oid, marketer_name=market_as)
    rows = [{"contract_no": k, "marketer": v["marketer"],
             "entrust": v["entrust"], "bond": v["bond"], "status": v["status"]}
            for k, v in cmap_rows.items()]
    db.replace_contract_uploads(oid, rows)
    db.log_upload(oid, "contract", f.filename, len(rows), 0,
                  {"market_as": market_as, "marketers_found": marketers})
    run_compute()
    return jsonify({
        "openid": oid,
        "bound_marketer": market_as,
        "contract_count": len(rows),
        "contracts_auto_expired_filtered": sum(
            1 for v in cmap_rows.values() if "作废" in v["status"] or "终止" in v["status"]),
    })


# ---------------------------------------------------------------------------
# 上传：兜底（承揽立项 / 项目作业）
# ---------------------------------------------------------------------------
@app.route("/api/upload/fallback", methods=["POST"])
def upload_fallback():
    oid, err, code = require_openid()
    if err:
        return err, code
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    source = request.form.get("source", "").strip()  # chenlan / zuoye
    if source not in ("chenlan", "zuoye"):
        return jsonify({"error": "source must be chenlan or zuoye"}), 400
    data = f.read()
    try:
        res = eng.process_upload(data, source_label=source,
                                 validity=VALIDITY, remind=REMIND)
    except Exception as e:
        return jsonify({"error": f"解析失败: {e}"}), 422

    kept = res["records"]
    rows = [{"subject": r["subject"], "base_date": r["base_date_obj"],
             "bond_type": r["bond_type"], "status_raw": r["status_raw"]}
            for r in kept]
    db.replace_fallback_uploads(oid, source, rows)
    db.log_upload(oid, source, f.filename, len(kept),
                  res["summary"]["dropped_rows"], res["mapping"])
    run_compute()
    return jsonify({
        "openid": oid,
        "source": source,
        "kept": len(kept),
        "dropped": res["summary"]["dropped_rows"],
        "mapping": res["mapping"],
        "status_distribution": res["summary"]["status_distribution"],
    })


# ---------------------------------------------------------------------------
# 计算：合同号 join 为主 + 时间窗口兜底
# ---------------------------------------------------------------------------
def attribute(rec, cmap, fb_index, window=FALLBACK_WINDOW):
    """返回 (openid_or_None, reason)。"""
    no = (rec.get("contract_no") or "").strip()
    if no and no in cmap:
        return cmap[no], "contract_join"
    subj = rec["subject"]
    cands = fb_index.get(subj, [])
    if not cands:
        return None, "no_candidate"
    issuance = rec.get("issuance")
    if issuance is None:
        best = max(cands, key=lambda x: x[1])
        return best[0], "no_issuance_fallback"
    cutoff = eng.add_months(issuance, -window)
    in_window = [(o, b) for o, b in cands if cutoff <= b <= issuance]
    pool = in_window if in_window else cands
    best = min(pool, key=lambda x: abs((x[1] - issuance).days))
    return best[0], ("window_match" if in_window else "closest_fallback")


def run_compute(ref_date=None):
    if ref_date is None:
        ref_date = date.today()
    admin = db.get_all_admin()
    cmap = db.get_contract_openid_map()      # contract_no -> openid
    fb = db.get_fallback_index()             # subject -> [(openid, base_date)]

    finals = []
    skipped_no_issuance = 0
    reason_counter = {}
    for rec in admin:
        openid, reason = attribute(rec, cmap, fb)
        reason_counter[reason] = reason_counter.get(reason, 0) + 1
        if openid is None:
            continue
        if not rec["issuance"]:
            skipped_no_issuance += 1
            continue
        expiry = eng.add_months(rec["issuance"], VALIDITY)
        remind_date = eng.add_months(expiry, -REMIND)
        st = eng.classify(rec["issuance"], ref_date, VALIDITY, REMIND)
        finals.append({
            "openid": openid,
            "subject": rec["subject"],
            "contract_no": rec["contract_no"],
            "base_date": rec["issuance"].isoformat(),
            "expiry_date": expiry.isoformat(),
            "remind_date": remind_date.isoformat(),
            "status": st,
            "debt_type": rec["debt_type"],
            "project_type": rec["project_type"],
            "attribution": reason,
            "source": "admin_source",
            "extra": {"issuance_source": rec["issuance_source"]},
        })

    # 按 (openid, subject, debt_type) 去重，取最新出具时间
    best = {}
    for r in finals:
        k = (r["openid"], r["subject"], r["debt_type"])
        if k not in best or r["base_date"] > best[k]["base_date"]:
            best[k] = r
    finals = list(best.values())

    # 过期窗口过滤陈年噪音
    dropped_overdue = 0
    if OVERDUE_WINDOW is not None:
        cutoff = eng.add_months(ref_date, -OVERDUE_WINDOW)
        kept = []
        for r in finals:
            if r["status"] == "overdue" and r["expiry_date"] < cutoff.isoformat():
                dropped_overdue += 1
            else:
                kept.append(r)
        finals = kept

    n = db.replace_final_ratings(finals)
    return {
        "final_count": n,
        "admin_total": len(admin),
        "skipped_no_issuance": skipped_no_issuance,
        "attribution_breakdown": reason_counter,
        "dropped_overdue_noise": dropped_overdue,
        "ref_date": ref_date.isoformat(),
    }


@app.route("/api/compute", methods=["POST"])
def compute():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    ref = request.get_json(silent=True) or {}
    rd = None
    if ref.get("ref_date"):
        from datetime import date as _d
        rd = _d.fromisoformat(ref["ref_date"])
    stats = run_compute(rd)
    return jsonify(stats)


# ---------------------------------------------------------------------------
# 查询：个人到期提醒
# ---------------------------------------------------------------------------
@app.route("/api/my/ratings", methods=["GET"])
def my_ratings():
    oid, err, code = require_openid()
    if err:
        return err, code
    status = request.args.get("status")
    if status not in (None, "overdue", "due", "upcoming"):
        return jsonify({"error": "bad status"}), 400
    rows = db.get_my_ratings(oid, status)
    return jsonify({
        "openid": oid,
        "count": len(rows),
        "ratings": [_public(r) for r in rows],
    })


# ---------------------------------------------------------------------------
# 查询：管理员总览
# ---------------------------------------------------------------------------
@app.route("/api/admin/overview", methods=["GET"])
def admin_overview():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    ov = db.get_admin_overview()
    # 给每个市场人员补名字
    conn = db.get_conn()
    names = {r["openid"]: r["marketer_name"]
             for r in conn.execute("SELECT openid, marketer_name FROM users")}
    conn.close()
    by_marketer = {o: {"name": names.get(o), "by_status": m,
                        "total": sum(m.values())}
                   for o, m in ov["by_marketer"].items()}
    return jsonify({**ov, "by_marketer": by_marketer})


@app.route("/api/admin/marketer", methods=["GET"])
def admin_marketer():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    target = request.args.get("openid")
    if not target:
        return jsonify({"error": "openid required"}), 400
    rows = db.get_marketer_ratings(target)
    return jsonify({"openid": target, "count": len(rows),
                    "ratings": [_public(r) for r in rows]})


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _auth_admin():
    oid = current_openid()
    if not oid:
        return None, jsonify({"error": "missing X-Openid"}), 401
    u = db.get_user(oid)
    if not u:
        return None, jsonify({"error": "unknown user"}), 403
    if u["role"] != "admin":
        return None, jsonify({"error": "admin only"}), 403
    return oid, None, None


def _public(r):
    return {
        "subject": r["subject"],
        "contract_no": r["contract_no"],
        "expiry_date": r["expiry_date"],
        "remind_date": r["remind_date"],
        "status": r["status"],
        "debt_type": r["debt_type"],
        "project_type": r["project_type"],
        "attribution": r["attribution"],
    }


def _save_tmp(f, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fp:
        fp.write(f.read())
    return path


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "admin_records": db.admin_count()})


# ---------------------------------------------------------------------------
# 演示辅助（仅本地预览；生产删除）
# ---------------------------------------------------------------------------
def _mk_oid(name):
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:20]


@app.route("/api/me/name", methods=["POST"])
def me_name():
    """登录后关联市场人员姓名（预览用）。"""
    oid, err, code = require_openid()
    if err:
        return err, code
    b = request.get_json(silent=True) or {}
    db.upsert_user(oid, marketer_name=b.get("name"))
    return jsonify({"ok": True})


@app.route("/api/demo/seed", methods=["POST"])
def demo_seed():
    """自动读取本地真实文件灌入：后台全量 + 按市场人员拆分合同归属。
    仅本地预览用；生产环境默认禁用（依赖本地文件路径）。设置 ENABLE_DEMO=1 开启。"""
    if not os.environ.get("ENABLE_DEMO"):
        return jsonify({"error": "demo disabled"}), 404
    if not os.path.exists(DEMO_ADMIN_XLS) or not os.path.exists(DEMO_CONTRACT_XLSX):
        return jsonify({"error": "演示文件不存在"}), 404
    recs = adm.parse_admin_xls(DEMO_ADMIN_XLS)
    db.replace_admin_source(recs)
    db.upsert_user("admin", role="admin", marketer_name="管理员")
    cmap = adm.parse_contract_mgmt(DEMO_CONTRACT_XLSX)
    by_m = defaultdict(dict)
    for k, v in cmap.items():
        by_m[v["marketer"]][k] = v
    for m, rows in by_m.items():
        oid = _mk_oid(m)
        db.upsert_user(oid, role="user", marketer_name=m)
        db.replace_contract_uploads(oid, [
            {"contract_no": k, "marketer": v["marketer"], "entrust": v["entrust"],
             "bond": v["bond"], "status": v["status"]} for k, v in rows.items()])
    stats = run_compute()
    return jsonify({"seed": "ok", "marketers": list(by_m.keys()), "stats": stats})


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "preview.html"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    # 生产：关闭 debug / reloader（云托管用 gunicorn 托管，见 Dockerfile）
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
