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
import re
import json
import tempfile
import hashlib
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from collections import defaultdict

import db
import rating_engine as eng
import admin_source as adm

# 微信登录真实接入（云托管生产用）。未配置时退化为 mock（code 当 openid）。
WX_APPID = os.environ.get("WX_APPID")
WX_SECRET = os.environ.get("WX_SECRET")

# Web 版管理员口令（可选）。设置后，管理员登录必须输入正确口令；未设置则任意人可用管理员入口（仅联调方便，生产务必设置）。
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# 演示种子文件（仅本地预览用，生产环境删除此路由与常量）
# 演示数据源（默认是本机路径；部署到服务器时用环境变量覆盖即可）
DEMO_ADMIN_XLS = os.environ.get(
    "DEMO_ADMIN_XLS",
    "/Volumes/D/编程/业绩到期查询/项目查询导出.xls")
DEMO_CONTRACT_XLSX = os.environ.get(
    "DEMO_CONTRACT_XLSX",
    "/Volumes/D/编程/业绩到期查询/自定义上传优化版/合同管理.xlsx")

# 通知 / 推送配置（均为可选；未配置时对应渠道静默跳过，不影响其他功能）
WX_TEMPLATE_ID  = os.environ.get("WX_TEMPLATE_ID")    # 微信订阅消息模板ID
WX_TEMPLATE_DATA = os.environ.get("WX_TEMPLATE_DATA")  # JSON：字段映射（占位 {subject}{expiry}{count}）
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
SMTP_TLS  = str(os.environ.get("SMTP_TLS", "1")).lower() not in ("0", "false", "no")

app = Flask(__name__)
CORS(app)  # 开发期允许小程序跨域；生产可收紧

# 默认参数（可用环境变量覆盖）
VALIDITY = int(os.environ.get("VALIDITY", "12"))       # 有效期(月)
REMIND = int(os.environ.get("REMIND", "3"))             # 提醒窗口(月)
OVERDUE_WINDOW = int(os.environ.get("OVERDUE_WINDOW", "12"))  # 过期噪音窗口(月)
FALLBACK_WINDOW = int(os.environ.get("FALLBACK_WINDOW", "24"))  # 兜底窗口(月)

# 预警阈值默认值（提前 N 天提醒）。用户可在设置里改。
DEFAULT_NOTIFY_DAYS = [int(x) for x in
                       os.environ.get("NOTIFY_DAYS", "30,7").split(",") if x.strip()]

db.init_db()


# ---------------------------------------------------------------------------
# 认证辅助
# ---------------------------------------------------------------------------
def current_openid():
    """从 header / query 取 openid（骨架简化）。"""
    oid = request.headers.get("X-Openid") or request.args.get("openid")
    return oid or None


def _openid_from_code(code, role="user"):
    """微信 code -> openid（与 login 共用）。"""
    if code == "admin":
        return "admin"
    if WX_APPID and WX_SECRET:
        url = ("https://api.weixin.qq.com/sns/jscode2session?appid=%s"
               "&secret=%s&js_code=%s&grant_type=authorization_code") % (
            urllib.parse.quote(WX_APPID), urllib.parse.quote(WX_SECRET),
            urllib.parse.quote(code))
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                d = json.loads(resp.read())
        except Exception as e:
            print("[login] code2Session error:", e)
            return None
        return d.get("openid") or None
    # 退化 mock：code -> openid（演示/联调）
    return hashlib.sha1(code.encode("utf-8")).hexdigest()[:20]


def _user_gate(oid):
    """统一审核状态门禁：返回 (oid, None, None) 或 (None, err_json, code)。"""
    if not oid:
        return None, jsonify({"error": "missing X-Openid header"}), 401
    u = db.get_user(oid)
    if not u:
        return None, jsonify({"error": "请先注册账号后再登录",
                              "code": "NOT_REGISTERED"}), 403
    if u["role"] != "admin" and u["status"] != "approved":
        if u["status"] == "pending":
            return None, jsonify({"error": "账号审核中，请等待管理员审核",
                                  "code": "PENDING"}), 403
        if u["status"] == "rejected":
            return None, jsonify({"error": "账号未通过审核：" + (u["reject_reason"] or ""),
                                  "code": "REJECTED"}), 403
        return None, jsonify({"error": "账号状态异常", "code": "BAD_STATUS"}), 403
    return oid, None, None


def require_openid():
    """要求已登录且已审核通过的用户（上传、查询等均走此门禁）。"""
    return _user_gate(current_openid())


@app.route("/api/login", methods=["POST"])
def login():
    """
    微信登录。注册 + 审核通过的账号才能拿到会话；
    未注册 / 待审核 / 已拒绝会被门禁拦截并返回对应 code。
    """
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    role = body.get("role", "user")
    if not code:
        return jsonify({"error": "code required"}), 400

    if role == "admin" or code == "admin":
        # 管理员登录（dev 快捷：code=='admin'；生产建议配 WX + 口令）
        openid = "admin"
        db.upsert_user(openid, role="admin", marketer_name="管理员")
        u = db.get_user(openid)
        return _issue_session(openid, u)

    openid = _openid_from_code(code, role)
    if not openid:
        return jsonify({"error": "code2Session failed"}), 502

    oid, err, code_ = _user_gate(openid)
    if err:
        return err, code_
    u = db.get_user(openid)
    db.log_audit(openid, "login", detail="wx")
    return _issue_session(openid, u)


def _issue_session(openid, u):
    return jsonify({
        "openid": openid,
        "role": u["role"],
        "name": u.get("marketer_name"),
        "status": u["status"],
        "admin_password_required": bool(ADMIN_PASSWORD),
        "token": openid
    })


@app.route("/api/bind-account", methods=["POST"])
def bind_account():
    """将已有注册账号（通过手机号定位）绑定到当前微信 openid。

    前端在登录遇到 NOT_REGISTERED 时引导用户走此接口：
      1. 用户输入注册时用的手机号
      2. 后端用 wx.login 的 code 派生新 openid
      3. 将该手机号对应账号的 openid 更新为新 openid
      4. 返回登录会话（等效于直接登录成功）
    """
    body = request.get_json(silent=True) or {}
    phone = (body.get("phone") or "").strip()
    code = (body.get("code") or "").strip()
    password = (body.get("password") or "").strip()
    if not phone:
        return jsonify({"error": "请输入注册时使用的手机号"}), 400
    if not code:
        return jsonify({"error": "缺少微信登录凭证"}), 400
    if not re.match(r"^1[3-9]\d{9}$", phone):
        return jsonify({"error": "手机号格式不正确"}), 400

    # 先定位账号并校验密码，防止「知道别人手机号即可冒名绑定」
    target = db.find_user_by_phone_or_name(phone, "")
    if not target:
        return jsonify({"error": "未找到该手机号对应的账号，请先注册"}), 404
    if not target.get("password_hash"):
        return jsonify({"code": "NEED_PASSWORD_SET",
                        "error": "该账号尚未设置密码，请联系管理员设置初始密码后再绑定"}), 403
    if not db.verify_password(password, target["password_hash"]):
        return jsonify({"error": "手机号或密码错误"}), 401

    new_openid = _openid_from_code(code, "user")
    if not new_openid:
        return jsonify({"error": "微信凭证解析失败"}), 502

    ok, u, msg = db.bind_wechat_openid(phone, new_openid)
    if not ok:
        return jsonify({"error": msg}), 404

    # 绑定成功后检查审核状态
    oid2, err, code_ = _user_gate(new_openid)
    if err:
        # 绑定成功了但账号还没通过审核，返回带状态的信息
        return jsonify({
            "openid": new_openid,
            "role": u.get("role", "user"),
            "name": u.get("marketer_name"),
            "status": u.get("status", "pending"),
            "bound": True,
            "message": msg + f"，但账号状态：{u.get('status', 'unknown')}"
        }), 200

    db.log_audit(new_openid, "bind_account",
                 detail=f"phone={phone}, previous_oid={u.get('openid','?')[:12]}")
    resp = _issue_session(new_openid, u)
    resp.get_json(lambda: None)  # noqa — 延迟求值，下面直接改 dict
    d = dict(resp[0].get_data(as_text=True)) if hasattr(resp[0], 'get_data') else {}
    # 简单方式：重新构造
    result = {
        "openid": new_openid,
        "role": u.get("role", "user"),
        "name": u.get("marketer_name"),
        "status": u.get("status", "approved"),
        "bound": True,
        "message": msg,
        "admin_password_required": bool(ADMIN_PASSWORD),
        "token": new_openid
    }
    return jsonify(result)


@app.route("/api/web/login", methods=["POST"])
def web_login():
    """
    Web 版登录（浏览器，无微信）。改为「手机号」登录（与注册一致）。
    为兼容已部署的旧账号，仍支持用姓名登录（sha1(姓名) 身份）。
    注册 + 审核通过后方可登录。
    """
    body = request.get_json(silent=True) or {}
    role = body.get("role", "user")
    if role == "admin":
        name = (body.get("name") or "管理员").strip() or "管理员"
        if ADMIN_PASSWORD and (body.get("password") or "") != ADMIN_PASSWORD:
            return jsonify({"error": "管理员口令错误"}), 401
        openid = "admin"
        db.upsert_user(openid, role="admin", marketer_name=name)
        u = db.get_user(openid)
        # 首次：用环境变量 ADMIN_PASSWORD 播种 password_hash，便于后台自助改密
        if ADMIN_PASSWORD and not u.get("password_hash"):
            db.set_password(openid, ADMIN_PASSWORD)
        db.log_audit(openid, "login", detail="web/admin")
        return _issue_session(openid, u)

    phone = (body.get("phone") or "").strip()
    name = (body.get("name") or "").strip()
    password = (body.get("password") or "").strip()
    if not (phone or name):
        return jsonify({"error": "请输入手机号或姓名"}), 400
    if not password:
        return jsonify({"error": "请输入登录密码"}), 400

    # 按手机号或姓名解析已注册账号（不再自行派生 openid，避免身份错配）
    u = db.find_user_by_phone_or_name(phone, name)
    if not u:
        return jsonify({"code": "NOT_REGISTERED",
                        "error": "未找到账号，请先注册或通过管理员审核"}), 404
    if not u.get("password_hash"):
        return jsonify({"code": "NEED_PASSWORD_SET",
                        "error": "该账号尚未设置密码，请联系管理员设置初始密码"}), 403
    if not db.verify_password(password, u["password_hash"]):
        return jsonify({"error": "手机号/姓名或密码错误"}), 401
    if u["status"] != "approved":
        code = "PENDING" if u["status"] == "pending" else "REJECTED"
        msg = ("账号审核中，请等待管理员审核" if code == "PENDING"
               else f"账号未通过审核{f'：{u.get('reject_reason')}' if u.get('reject_reason') else ''}")
        return jsonify({"code": code, "error": msg}), 403

    db.log_audit(u["openid"], "login", detail="web")
    return _issue_session(u["openid"], u)


@app.route("/api/change-password", methods=["POST"])
def change_password():
    """自助修改密码（任何已登录用户，含管理员）。

    普通用户：需提供正确的旧密码。
    管理员：旧密码可为 ADMIN_PASSWORD（环境变量）或已设置的 password_hash。
    """
    oid, err, code_ = _user_gate(current_openid())
    if err:
        return err, code_
    u = db.get_user(oid)
    body = request.get_json(silent=True) or {}
    old_pw = (body.get("old_password") or "").strip()
    new_pw = (body.get("new_password") or "").strip()
    if not new_pw:
        return jsonify({"error": "请输入新密码"}), 400
    if not db.password_strength_ok(new_pw):
        return jsonify({"error": "新密码需 6-64 位，至少含字母与数字两类"}), 400

    is_admin = u["role"] == "admin"
    # 校验旧密码
    if u.get("password_hash"):
        if not db.verify_password(old_pw, u["password_hash"]):
            return jsonify({"error": "原密码错误"}), 401
    elif is_admin and ADMIN_PASSWORD:
        # 管理员尚未设置 password_hash：允许用环境变量口令作为原密码
        if old_pw != ADMIN_PASSWORD:
            return jsonify({"error": "原密码错误"}), 401
    else:
        return jsonify({"error": "该账号尚未设置密码，请联系管理员"}), 400

    db.set_password(oid, new_pw)
    db.log_audit(oid, "change_password")
    return jsonify({"ok": True, "message": "密码修改成功"})


@app.route("/api/admin/reset-password", methods=["POST"])
def admin_reset_password():
    """管理员重置指定用户的密码（用于给存量无密码账号初始化）。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    body = request.get_json(silent=True) or {}
    target = (body.get("openid") or "").strip()
    new_pw = (body.get("new_password") or "").strip()
    if not target:
        return jsonify({"error": "缺少目标 openid"}), 400
    if not db.password_strength_ok(new_pw):
        return jsonify({"error": "新密码需 6-64 位，至少含字母与数字两类"}), 400
    tu = db.get_user(target)
    if not tu:
        return jsonify({"error": "目标账号不存在"}), 404
    db.set_password(target, new_pw)
    db.log_audit(oid, "reset_password", target=target)
    return jsonify({"ok": True, "message": f"已重置 {tu.get('marketer_name') or target} 的密码"})


# ---------------------------------------------------------------------------
# 注册 / 审核
# ---------------------------------------------------------------------------
@app.route("/api/register", methods=["POST"])
def register():
    """
    提交注册申请（不再「登录即匹配数据」）。
    小程序：带 wx.login 的 code；Web：带 phone（平台自行派生 openid）。
    必填：所属机构 organization、姓名 name、手机号 phone；邮箱 email 选填。
    """
    body = request.get_json(silent=True) or {}
    organization = (body.get("organization") or "").strip()
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    email = (body.get("email") or "").strip() or None
    code = (body.get("code") or "").strip()
    platform = body.get("platform", "miniprogram")
    password = body.get("password") or ""

    if not (organization and name and phone):
        return jsonify({"error": "请填写所属机构、姓名、手机号"}), 400
    if not re.match(r"^1[3-9]\d{9}$", phone):
        return jsonify({"error": "手机号格式不正确"}), 400
    if not db.password_strength_ok(password):
        return jsonify({"error": "请设置登录密码（6-64位，至少含字母与数字两类）"}), 400

    # 派生稳定 openid
    if platform == "web":
        openid = "web_" + hashlib.sha1(phone.encode("utf-8")).hexdigest()[:16]
    elif code:
        openid = _openid_from_code(code, "user")
        if not openid:
            return jsonify({"error": "微信凭证解析失败"}), 502
    else:
        return jsonify({"error": "缺少登录凭证(code)"}), 400

    u = db.get_user(openid)
    if u and u["status"] == "approved":
        return jsonify({"status": "approved", "message": "该账号已审核通过，可直接登录"})
    db.register_user(openid, organization, name, phone, email, role="user",
                     password=password)
    db.log_audit(None, "register", target=phone,
                 detail=f"{name} / {organization}")
    return jsonify({"status": "pending", "message": "注册成功，请等待管理员审核"})


@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    scope = request.args.get("scope")  # all | deleted
    if scope == "deleted":
        rows = [u for u in db.list_users(include_deleted=True)
                if u.get("deleted_at")]
    else:
        rows = db.list_users()
    users = [{
        "openid": u["openid"], "role": u["role"],
        "name": u.get("marketer_name"), "organization": u.get("organization"),
        "phone": u.get("phone"), "email": u.get("email"),
        "status": u["status"], "created_at": u.get("created_at"),
        "deleted_at": u.get("deleted_at"),
        "reject_reason": u.get("reject_reason")
    } for u in rows]
    return jsonify({"pending_count": db.count_pending(), "users": users})


@app.route("/api/admin/users/review", methods=["POST"])
def admin_review_user():
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    body = request.get_json(silent=True) or {}
    target = (body.get("openid") or "").strip()
    action = (body.get("action") or "").strip()
    reason = (body.get("reason") or "").strip() or None
    if not target or action not in ("approve", "reject"):
        return jsonify({"error": "参数错误"}), 400
    u = db.get_user(target)
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    new_status = "approved" if action == "approve" else "rejected"
    db.set_user_status(target, new_status, reviewed_by=oid, reason=reason)
    db.log_audit(oid, "review:" + action, target=target, detail=reason)
    return jsonify({"ok": True, "openid": target, "status": new_status})


# ---------------------------------------------------------------------------
# 用户管理：修改 / 停用 / 启用 / 删除(软) / 恢复
# ---------------------------------------------------------------------------
def _guard_target(oid, target, allow_self=False, allow_admin=False):
    """统一校验：返回 (target_user, error_json, code)。

    - 不可操作自己（除非 allow_self）
    - 管理员账号不可被删除/停用（除非 allow_admin）
    """
    if not target:
        return None, jsonify({"error": "缺少目标 openid"}), 400
    if not allow_self and target == oid:
        return None, jsonify({"error": "不能对自己执行此操作"}), 400
    u = db.get_user(target)
    if not u:
        return None, jsonify({"error": "用户不存在"}), 404
    if not allow_admin and u["role"] == "admin":
        return None, jsonify({"error": "管理员账号不可被停用或删除"}), 400
    return u, None, None


@app.route("/api/admin/users/<openid>", methods=["PUT"])
def admin_update_user(openid):
    """修改用户资料（姓名/机构/手机/邮箱/角色）。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    if not openid:
        return jsonify({"error": "缺少目标 openid"}), 400
    body = request.get_json(silent=True) or {}
    u = db.get_user(openid)
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    # 自保护：不能取消自己的管理员权限
    new_role = (body.get("role") or "").strip()
    if new_role and new_role != "admin" and u["role"] == "admin" and openid == oid:
        return jsonify({"error": "不能取消自己的管理员权限"}), 400
    ok, emsg = db.update_user(
        openid,
        marketer_name=(body.get("name") or "").strip() or None,
        organization=(body.get("organization") or "").strip() or None,
        phone=(body.get("phone") or "").strip(),
        email=(body.get("email") or "").strip() or None,
        role=new_role or None)
    if not ok:
        return jsonify({"error": emsg}), 400
    db.log_audit(oid, "update_user", target=openid,
                 detail=f"name={body.get('name')};org={body.get('organization')};"
                        f"phone={body.get('phone')};email={body.get('email')};role={new_role}")
    return jsonify({"ok": True, "message": "已更新用户资料"})


@app.route("/api/admin/users/<openid>/disable", methods=["POST"])
def admin_disable_user(openid):
    """停用用户（status -> disabled）。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    u, e, c = _guard_target(oid, openid, allow_self=False, allow_admin=False)
    if e:
        return e, c
    db.set_user_status(openid, "disabled", reviewed_by=oid, reason="管理员停用")
    db.log_audit(oid, "disable_user", target=openid, detail=u.get("marketer_name"))
    return jsonify({"ok": True, "openid": openid, "status": "disabled"})


@app.route("/api/admin/users/<openid>/enable", methods=["POST"])
def admin_enable_user(openid):
    """启用用户（status -> approved）。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    u = db.get_user(openid)
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    db.set_user_status(openid, "approved", reviewed_by=oid)
    db.log_audit(oid, "enable_user", target=openid, detail=u.get("marketer_name"))
    return jsonify({"ok": True, "openid": openid, "status": "approved"})


@app.route("/api/admin/users/<openid>", methods=["DELETE"])
def admin_delete_user(openid):
    """软删除用户（标记 deleted_at，保留数据与关联记录便于恢复）。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    u, e, c = _guard_target(oid, openid, allow_self=False, allow_admin=False)
    if e:
        return e, c
    db.soft_delete_user(openid, by=oid)
    db.log_audit(oid, "delete_user", target=openid, detail=u.get("marketer_name"))
    return jsonify({"ok": True, "message": f"已删除用户 {u.get('marketer_name') or openid}"})


@app.route("/api/admin/users/<openid>/restore", methods=["POST"])
def admin_restore_user(openid):
    """恢复已软删用户。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    u = db.get_user(openid)
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    db.restore_user(openid, by=oid)
    db.log_audit(oid, "restore_user", target=openid, detail=u.get("marketer_name"))
    return jsonify({"ok": True, "message": "已恢复用户", "status": "approved"})


# ---------------------------------------------------------------------------
# 通知 / 推送
# ---------------------------------------------------------------------------
@app.route("/api/my/notification", methods=["GET"])
def get_notification():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    n = db.get_notif(oid)
    if not n:
        return jsonify({"channels": [], "email": None, "wx_subscribed": 0})
    return jsonify({
        "channels": json.loads(n["channels"] or "[]"),
        "email": n["email"],
        "wx_subscribed": n["wx_subscribed"],
        "notify_days": db.get_notify_days(oid)
    })


@app.route("/api/my/notification", methods=["POST"])
def set_notification():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    body = request.get_json(silent=True) or {}
    channels = body.get("channels") or []
    email = (body.get("email") or "").strip() or None
    allowed = {"miniprogram", "email"}
    channels = [c for c in channels if c in allowed]
    if "email" in channels and not email:
        return jsonify({"error": "启用邮件提醒需填写邮箱地址"}), 400
    db.set_notif(oid, json.dumps(channels, ensure_ascii=False), email)
    if "notify_days" in body and isinstance(body["notify_days"], list):
        days = [int(x) for x in body["notify_days"] if str(x).isdigit()]
        if days:
            db.set_notify_days(oid, days)
    return jsonify({"ok": True, "channels": channels, "email": email,
                    "notify_days": db.get_notify_days(oid)})


@app.route("/api/my/notification/subscribe", methods=["POST"])
def notif_subscribe():
    """记录小程序订阅消息授权结果（客户端 wx.requestSubscribeMessage 成功后回调）。"""
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    body = request.get_json(silent=True) or {}
    sub = body.get("subscribed", True)
    if isinstance(sub, str):
        sub = sub.lower() not in ("false", "0", "no")
    db.set_notif_subscribed(oid, bool(sub))
    return jsonify({"ok": True, "wx_subscribed": 1 if sub else 0})


@app.route("/api/my/notification/test", methods=["POST"])
def notif_test():
    """给当前用户发一条测试提醒（按已开启渠道）。"""
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    n = db.get_notif(oid) or {"channels": "[]", "email": None, "wx_subscribed": 0}
    channels = json.loads(n["channels"] or "[]")
    u = db.get_user(oid)
    ratings = db.get_my_ratings(oid)
    results = []
    for ch in channels:
        if ch == "email":
            to = n["email"] or u.get("email")
            if to:
                ok = _send_email(to, "【测试】评级到期提醒", "这是一封测试提醒邮件，您的提醒渠道配置正常。")
                results.append({"channel": "email", "ok": ok})
            else:
                results.append({"channel": "email", "ok": False, "note": "未填写邮箱"})
        elif ch == "miniprogram":
            if n["wx_subscribed"] and WX_TEMPLATE_ID and WX_APPID and WX_SECRET:
                ok = _send_subscribe(oid, ratings[:1])
                results.append({"channel": "miniprogram", "ok": ok})
            else:
                results.append({"channel": "miniprogram", "ok": False,
                                "note": "未订阅或未配置模板/微信凭证"})
    db.add_message(oid, "system", "【测试】站内消息",
                   "这是一条测试站内消息，消息中心工作正常。")
    return jsonify({"ok": True, "results": results})


def dispatch_notifications(ref_date=None, dry=False, manual=False):
    """扫描所有已审核市场人员的到期/临期评级，按各自预警阈值与订阅渠道推送。
    站内消息中心（始终创建）+ 邮件（SMTP 已配）+ 微信订阅（已订阅且已配）。
    返回 {messages, sent, skipped}。供手动触发与定时任务共用。"""
    if ref_date is None:
        ref_date = date.today()
    ref_iso = ref_date.isoformat()
    # 1) 站内消息：所有 approved 市场人员（系统自带通道，不依赖渠道开关）
    all_users = db.list_users(status="approved", role="user")
    messages, sent, skipped = [], [], []
    for u in all_users:
        oid = u["openid"]
        ratings = db.get_my_ratings(oid)
        for r in ratings:
            if r.get("renewed") in (1, "1"):
                continue
            try:
                exp = date.fromisoformat(r["expiry_date"])
            except Exception:
                continue
            days = (exp - ref_date).days
            if r["status"] != "overdue" and not (r["remind_date"]
                                                  and r["remind_date"] <= ref_iso):
                continue
            ndays = db.get_notify_days(oid)
            triggered = [d for d in ndays if days <= d]
            if not triggered and r["status"] != "overdue":
                continue
            key = min(triggered) if triggered else -1
            if db.already_sent(oid, r["id"], key, "inapp"):
                continue
            if not dry:
                tag = "已过期" if r["status"] == "overdue" else "即将到期"
                db.add_message(oid, "expire_warn",
                               f"{tag}：{r['subject']}",
                               f"评级将于 {r['expiry_date']} 到期（剩余 {days} 天），请及时处理。",
                               rating_id=r["id"])
                db.mark_sent(oid, r["id"], key, "inapp")
            messages.append({"openid": oid, "subject": r["subject"], "days": days})
    # 2) 渠道推送（邮件/微信）：仅对开启对应渠道的用户
    users = db.get_approved_users_with_notif()
    for u in users:
        ratings = db.get_my_ratings(u["openid"])
        active = [r for r in ratings
                  if r.get("renewed") in (0, None) and r["status"] in ("due", "overdue")]
        if not active:
            continue
        for ch in u["channels"]:
            if ch == "email" and u.get("email"):
                if not dry:
                    _send_email(u["email"], _notif_subject(active), _notif_body(u, active))
                sent.append({"openid": u["openid"], "name": u["marketer_name"],
                             "channel": "email"})
            elif ch == "miniprogram":
                if u["wx_subscribed"] and WX_TEMPLATE_ID and WX_APPID and WX_SECRET:
                    if not dry:
                        _send_subscribe(u["openid"], active)
                    sent.append({"openid": u["openid"], "name": u["marketer_name"],
                                 "channel": "miniprogram"})
                else:
                    skipped.append({"openid": u["openid"], "name": u["marketer_name"],
                                    "channel": "miniprogram", "reason": "未订阅或未配置"})
    return {"messages": messages, "sent": sent, "skipped": skipped,
            "ref_date": ref_iso}


@app.route("/api/admin/notify/send", methods=["POST"])
def admin_notify_send():
    """管理员手动触发：向所有开启渠道的用户推送当前到期/即将到期提醒。"""
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    body = request.get_json(silent=True) or {}
    dry = bool(body.get("dry_run", False))
    res = dispatch_notifications(ref_date=date.today(), dry=dry, manual=True)
    db.log_audit(oid, "notify:send",
                 detail=f"消息 {len(res['messages'])} / 渠道 {len(res['sent'])}")
    return jsonify(res)


# ---- 发送助手（配置驱动，未配置则优雅跳过）----
def _notif_subject(due):
    return f"【评级到期提醒】您有 {len(due)} 条评级即将到期/已过期"


def _notif_body(u, due):
    lines = [f"{u.get('marketer_name') or ''} 您好，以下评级项目需要关注："]
    for r in due[:20]:
        tag = "已过期" if r["status"] == "overdue" else "即将到期"
        lines.append(f"- {r['subject']}（到期 {r['expiry_date']} / {tag}）")
    lines.append("")
    lines.append("请登录系统查看详情并及时跟进。")
    return "\n".join(lines)


def _send_email(to, subject, body_text):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        print("[notify] SMTP 未配置，跳过邮件发送 ->", to)
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body_text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to
        if SMTP_TLS:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        return True
    except Exception as e:
        print("[notify] 邮件发送失败:", e)
        return False


_wx_token_cache = {"token": None, "exp": 0}


def _wx_access_token():
    if _wx_token_cache["token"] and _wx_token_cache["exp"] > datetime.now().timestamp() + 60:
        return _wx_token_cache["token"]
    if not (WX_APPID and WX_SECRET):
        return None
    url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
           "&appid=%s&secret=%s") % (urllib.parse.quote(WX_APPID),
                                     urllib.parse.quote(WX_SECRET))
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            d = json.loads(r.read())
    except Exception as e:
        print("[notify] 获取 access_token 失败:", e)
        return None
    if d.get("access_token"):
        _wx_token_cache["token"] = d["access_token"]
        _wx_token_cache["exp"] = datetime.now().timestamp() + d.get("expires_in", 7200)
        return d["access_token"]
    return None


def _send_subscribe(openid, ratings):
    """微信订阅消息发送（一次性订阅，需用户此前已授权）。"""
    token = _wx_access_token()
    if not token or not WX_TEMPLATE_ID:
        return False
    default_map = {"thing1": "{subject}", "time2": "{expiry}", "thing3": "{count}条评级待关注"}
    fmap = default_map
    if WX_TEMPLATE_DATA:
        try:
            fmap = json.loads(WX_TEMPLATE_DATA)
        except Exception:
            fmap = default_map
    top = ratings[0] if ratings else {}
    subj = (top.get("subject") or "")[:20]
    expiry = top.get("expiry_date") or ""
    count = len(ratings)
    data = {k: {"value": str(v).format(subject=subj, expiry=expiry, count=count)}
            for k, v in fmap.items()}
    payload = {"touser": openid, "template_id": WX_TEMPLATE_ID, "data": data}
    url = ("https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token=%s"
           % urllib.parse.quote(token))
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read())
        if d.get("errcode") == 0:
            return True
        print("[notify] subscribe send err:", d)
        return False
    except Exception as e:
        print("[notify] subscribe send fail:", e)
        return False


# ---------------------------------------------------------------------------
# 站内消息中心
# ---------------------------------------------------------------------------
@app.route("/api/my/messages", methods=["GET"])
def my_messages():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    unread = request.args.get("unread") == "1"
    page = int(request.args.get("page", "1"))
    return jsonify(db.get_messages(oid, unread_only=unread, page=page, page_size=20))


@app.route("/api/my/messages/unread", methods=["GET"])
def my_unread():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    return jsonify({"unread": db.unread_count(oid)})


@app.route("/api/my/messages/read", methods=["POST"])
def read_messages():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    b = request.get_json(silent=True) or {}
    db.mark_message_read(oid, b.get("id"))
    return jsonify({"ok": True, "unread": db.unread_count(oid)})


# ---------------------------------------------------------------------------
# 我的提醒：搜索 / 排序 / 分页 / 续期
# ---------------------------------------------------------------------------
@app.route("/api/my/ratings", methods=["GET"])
def my_ratings():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    status = request.args.get("status")
    if status not in (None, "overdue", "due", "upcoming"):
        return jsonify({"error": "bad status"}), 400
    q = request.args.get("q") or None
    sort = request.args.get("sort", "status")
    page = int(request.args.get("page", "1"))
    page_size = min(int(request.args.get("page_size", "50")), 200)
    inc = request.args.get("include_renewed") == "1"
    res = db.query_my_ratings(oid, status, q, sort, page, page_size,
                              include_renewed=inc)
    return jsonify({
        "openid": oid,
        "count": len(res["items"]),
        "total": res["total"], "page": res["page"], "page_size": res["page_size"],
        "ratings": [_public(r) for r in res["items"]],
    })


@app.route("/api/my/ratings/<int:rid>/renew", methods=["POST"])
def renew_rating(rid):
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    b = request.get_json(silent=True) or {}
    new_expiry = (b.get("new_expiry") or "").strip() or None
    db.mark_renewed(rid, openid=oid, new_expiry=new_expiry)
    db.log_audit(oid, "renew", target=str(rid), detail=new_expiry)
    return jsonify({"ok": True, "id": rid})


@app.route("/api/my/calendar", methods=["GET"])
def my_calendar():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    months = int(request.args.get("months", "6"))
    return jsonify(db.calendar_expiry(openid=oid, months=months))


# ---------------------------------------------------------------------------
# 导出（CSV，Excel 可直接打开）
# ---------------------------------------------------------------------------
def _ratings_to_csv(rows):
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["客户名称", "合同号", "评级类型", "项目类型",
                "出具时间", "到期日", "状态", "归属"])
    st = {"overdue": "已过期", "due": "即将到期", "upcoming": "有效期内"}
    for r in rows:
        w.writerow([r["subject"], r.get("contract_no", ""),
                    r.get("debt_type", ""), r.get("project_type", ""),
                    r.get("base_date", ""), r["expiry_date"],
                    st.get(r["status"], r["status"]), r.get("attribution", "")])
    return buf.getvalue()


def _csv_response(text, filename):
    from flask import Response
    from urllib.parse import quote
    resp = Response(text, mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename*=UTF-8''{quote(filename)}"
    return resp


@app.route("/api/export/my", methods=["GET"])
def export_my():
    oid, err, code_ = require_openid()
    if err:
        return err, code_
    rows = db.get_my_ratings(oid)
    csv_text = _ratings_to_csv([r for r in rows
                                if r.get("renewed") in (0, None)])
    return _csv_response(csv_text, f"我的评级到期_{date.today().isoformat()}.csv")


@app.route("/api/export/admin", methods=["GET"])
def export_admin():
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    ov = db.overview_excluding_renewed()
    rows = []
    for o in ov["by_marketer"]:
        u = db.get_user(o)
        name = u["marketer_name"] if u else o
        for r in db.get_marketer_ratings(o):
            if r.get("renewed") in (1, "1"):
                continue
            r2 = dict(r)
            r2["_mkt"] = name
            rows.append(r2)
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["市场人员", "客户名称", "合同号", "评级类型", "项目类型",
                "出具时间", "到期日", "状态", "归属"])
    st = {"overdue": "已过期", "due": "即将到期", "upcoming": "有效期内"}
    for r in rows:
        w.writerow([r.get("_mkt", ""), r["subject"], r.get("contract_no", ""),
                    r.get("debt_type", ""), r.get("project_type", ""),
                    r.get("base_date", ""), r["expiry_date"],
                    st.get(r["status"], r["status"]), r.get("attribution", "")])
    return _csv_response(buf.getvalue(), f"全员评级到期_{date.today().isoformat()}.csv")


# ---------------------------------------------------------------------------
# 管理员：审计日志 / 市场人员下钻（搜索）
# ---------------------------------------------------------------------------
@app.route("/api/admin/audit", methods=["GET"])
def admin_audit():
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    page = int(request.args.get("page", "1"))
    return jsonify(db.get_audit_log(page=page, page_size=50))


@app.route("/api/admin/marketer", methods=["GET"])
def admin_marketer():
    oid, err, code_ = _auth_admin()
    if err:
        return err, code_
    target = request.args.get("openid")
    if not target:
        return jsonify({"error": "openid required"}), 400
    q = request.args.get("q") or None
    sort = request.args.get("sort", "status")
    page = int(request.args.get("page", "1"))
    res = db.query_my_ratings(target, None, q, sort, page, 200,
                              include_renewed=True)
    return jsonify({"openid": target, "count": len(res["items"]),
                    "total": res["total"], "page": res["page"],
                    "ratings": [_public(r) for r in res["items"]]})


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
    db.log_audit(oid, "upload:admin_source", detail=f"{n} 条评级记录")
    # 自动触发一次计算（管理员源数据变化后）
    stats = run_compute()
    return jsonify({"admin_records": n, "compute_stats": stats})


# ---------------------------------------------------------------------------
# 管理员：报告数据 CRUD（admin_source：评级真相，可单条增/改/删/恢复）
# ---------------------------------------------------------------------------
_RATING_FIELDS = ["subject", "contract_no", "li_date", "issuance",
                  "issuance_source", "project_type", "debt_type",
                  "rating", "outlook", "notes"]


@app.route("/api/admin/report", methods=["GET"])
def admin_report_list():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "id")
    order = request.args.get("order", "asc")
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page, page_size = 1, 50
    include_del = request.args.get("include_deleted") == "1"
    data = db.list_admin_ratings(q=q, sort=sort, order=order,
                                 page=page, page_size=page_size,
                                 include_deleted=include_del)
    return jsonify(data)


@app.route("/api/admin/report/<int:rid>", methods=["GET"])
def admin_report_get(rid):
    oid, err, code = _auth_admin()
    if err:
        return err, code
    rec = db.get_admin_rating(rid, include_deleted=True)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(rec)


@app.route("/api/admin/report", methods=["POST"])
def admin_report_add():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    b = request.get_json(silent=True) or {}
    subject = (b.get("subject") or "").strip()
    if not subject:
        return jsonify({"error": "主体(机构名称)为必填项"}), 400
    li_date = _norm_date(b.get("li_date"))
    issuance = _norm_date(b.get("issuance"))
    if (b.get("li_date") and li_date is None) or (b.get("issuance") and issuance is None):
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 422
    # 智能去重：相同 主体/合同号/出具日 视为重复
    dup = db.find_duplicate_report(subject, b.get("contract_no", ""), issuance)
    if dup:
        return jsonify({"error": f"已存在相同 主体/合同号/出具日 的记录（#{dup}），请勿重复录入",
                        "duplicate_id": dup}), 409
    rec = {
        "subject": subject,
        "contract_no": (b.get("contract_no") or "").strip(),
        "li_date": li_date,
        "issuance": issuance,
        "issuance_source": b.get("issuance_source") or None,
        "project_type": (b.get("project_type") or "").strip(),
        "debt_type": (b.get("debt_type") or "").strip(),
        "rating": (b.get("rating") or "").strip(),
        "outlook": (b.get("outlook") or "").strip(),
        "notes": (b.get("notes") or "").strip(),
    }
    rid = db.add_admin_rating(rec)
    db.log_audit(oid, "report:add", target=str(rid),
                 detail=f"{subject} / {rec['contract_no']} / {rec['issuance']}")
    run_compute()
    return jsonify({"id": rid, "ok": True}), 201


@app.route("/api/admin/report/<int:rid>", methods=["PUT"])
def admin_report_update(rid):
    oid, err, code = _auth_admin()
    if err:
        return err, code
    cur = db.get_admin_rating(rid, include_deleted=True)
    if not cur:
        return jsonify({"error": "not found"}), 404
    b = request.get_json(silent=True) or {}
    fields = {}
    for k in _RATING_FIELDS:
        if k in b:
            v = b[k]
            if k in ("li_date", "issuance"):
                v = _norm_date(v)
                if v is None and b[k] not in (None, ""):
                    return jsonify({"error": f"{k} 日期格式应为 YYYY-MM-DD"}), 422
            elif k == "issuance_source":
                v = v or None
            else:
                v = (v or "").strip()
            fields[k] = v
    # 去重检查（排除自身）
    if ("subject" in fields or "contract_no" in fields or "issuance" in fields):
        subj = fields.get("subject", cur["subject"])
        cno = fields.get("contract_no", cur["contract_no"])
        iss = fields.get("issuance", cur["issuance"])
        dup = db.find_duplicate_report(subj, cno, iss, exclude_id=rid)
        if dup:
            return jsonify({"error": f"会与记录 #{dup} 重复（相同 主体/合同号/出具日）",
                            "duplicate_id": dup}), 409
    db.update_admin_rating(rid, fields)
    changed = ", ".join(f"{k}={fields[k]}" for k in fields)
    db.log_audit(oid, "report:update", target=str(rid),
                 detail=f"{cur['subject']} → {changed}")
    run_compute()
    return jsonify({"ok": True})


@app.route("/api/admin/report/<int:rid>", methods=["DELETE"])
def admin_report_delete(rid):
    oid, err, code = _auth_admin()
    if err:
        return err, code
    cur = db.get_admin_rating(rid, include_deleted=True)
    if not cur:
        return jsonify({"error": "not found"}), 404
    db.soft_delete_admin_rating(rid)
    db.log_audit(oid, "report:delete", target=str(rid),
                 detail=f"{cur['subject']}（移入回收站）")
    run_compute()
    return jsonify({"ok": True, "trashed": True})


@app.route("/api/admin/report/<int:rid>/restore", methods=["POST"])
def admin_report_restore(rid):
    oid, err, code = _auth_admin()
    if err:
        return err, code
    cur = db.get_admin_rating(rid, include_deleted=True)
    if not cur:
        return jsonify({"error": "not found"}), 404
    db.restore_admin_rating(rid)
    db.log_audit(oid, "report:restore", target=str(rid), detail=cur["subject"])
    run_compute()
    return jsonify({"ok": True})


@app.route("/api/admin/report/trash", methods=["GET"])
def admin_report_trash():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    return jsonify({"items": db.list_trashed()})


@app.route("/api/admin/report/health", methods=["GET"])
def admin_report_health():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    return jsonify(db.admin_source_health())


@app.route("/api/admin/report/batch-delete", methods=["POST"])
def admin_report_batch_delete():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    b = request.get_json(silent=True) or {}
    ids = [int(x) for x in (b.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return jsonify({"error": "ids required"}), 400
    n = db.batch_soft_delete(ids)
    db.log_audit(oid, "report:batch-delete", detail=f"{n} 条 → 回收站")
    run_compute()
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/admin/report/batch-restore", methods=["POST"])
def admin_report_batch_restore():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    b = request.get_json(silent=True) or {}
    ids = [int(x) for x in (b.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return jsonify({"error": "ids required"}), 400
    n = db.batch_restore(ids)
    db.log_audit(oid, "report:batch-restore", detail=f"{n} 条恢复")
    run_compute()
    return jsonify({"ok": True, "restored": n})


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
    db.log_audit(oid, "upload:contract", detail=f"{market_as} / {len(rows)} 份合同")
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
    db.log_audit(oid, "upload:" + source, detail=f"{len(kept)} 条 / 丢弃 {res['summary']['dropped_rows']}")
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
            "rating": rec.get("rating"),
            "outlook": rec.get("outlook"),
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
# 查询：管理员总览
# ---------------------------------------------------------------------------
@app.route("/api/admin/overview", methods=["GET"])
def admin_overview():
    oid, err, code = _auth_admin()
    if err:
        return err, code
    ov = db.overview_excluding_renewed()
    # 给每个市场人员补名字
    conn = db.get_conn()
    names = {r["openid"]: r["marketer_name"]
             for r in conn.execute("SELECT openid, marketer_name FROM users")}
    conn.close()
    by_marketer = {o: {"name": names.get(o), "by_status": m,
                        "total": sum(m.values())}
                   for o, m in ov["by_marketer"].items()}
    return jsonify({**ov, "by_marketer": by_marketer})


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
        "id": r["id"],
        "subject": r["subject"],
        "contract_no": r["contract_no"],
        "expiry_date": r["expiry_date"],
        "remind_date": r["remind_date"],
        "status": r["status"],
        "debt_type": r["debt_type"],
        "project_type": r["project_type"],
        "attribution": r["attribution"],
        "rating": r.get("rating") or "",
        "outlook": r.get("outlook") or "",
        "renewed": r.get("renewed", 0),
        "renewed_at": r.get("renewed_at"),
    }


def _save_tmp(f, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fp:
        fp.write(f.read())
    return path


def _norm_date(v):
    """接受 YYYY-MM-DD（兼容 / 分隔），返回 ISO 字符串；空/非法返回 None。"""
    if v in (None, ""):
        return None
    if isinstance(v, str):
        v = v.strip().replace("/", "-")
        if not v:
            return None
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            return None
    return None


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


@app.route("/api/config", methods=["GET"])
def web_config():
    """前端据此决定是否显示管理员口令框 / demo 按钮 / 注册入口，以及渠道可用性。"""
    return jsonify({
        "admin_password_required": bool(ADMIN_PASSWORD),
        "demo_enabled": bool(os.environ.get("ENABLE_DEMO")),
        "registration_enabled": True,
        "channels": {
            "miniprogram": bool(WX_APPID and WX_SECRET and WX_TEMPLATE_ID),
            "email": bool(SMTP_HOST and SMTP_USER and SMTP_PASS)
        },
        "wx_template_id": WX_TEMPLATE_ID or "",
        "notify_days_default": DEFAULT_NOTIFY_DAYS,
        "message_center": True
    })


def _static(name):
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))


@app.route("/")
def index():
    """正式 Web 前端（面向浏览器用户）。"""
    return _static("webapp.html")


# 微信公众平台 / 小程序「业务域名 / 服务器域名」校验文件托管
# 将微信下发的校验文件（如 MP_verify_xxxx.txt）放到 deploy 目录下的 verify/ 子目录即可被根路径访问。
VERIFY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify")
@app.route("/MP_verify_<token>.txt")
@app.route("/<token>.txt")
def wx_verify(token):
    for cand in (f"MP_verify_{token}.txt", f"{token}.txt"):
        p = os.path.join(VERIFY_DIR, cand)
        if os.path.exists(p):
            return send_file(p, mimetype="text/plain; charset=utf-8")
    return "", 404


@app.route("/preview")
def preview():
    """旧的演示预览页（保留，硬编码身份切换，仅联调用）。"""
    return _static("preview.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    # 生产：关闭 debug / reloader（云托管用 gunicorn 托管，见 Dockerfile）
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
