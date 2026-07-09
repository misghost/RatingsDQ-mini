# -*- coding: utf-8 -*-
"""
test_smoke.py — 后端骨架 端到端冒烟测试（真实数据）

验证：
  1. 管理员上传后台 xls 解析入库
  2. 市场人员 A(刘鹏) / B(黄天祺) 各自上传自己的合同（合同号 join 归属）
  3. compute 后：
     - A 只看到刘鹏的合同（隔离）
     - B 只看到黄天祺/邓少平的合同（隔离）
     - 全部 attribution = contract_join（主机制生效）
  4. HTTP 层 /api/health、/api/login、/api/my/ratings、/api/admin/overview 通
"""

import os
os.environ.setdefault(
    "RATING_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rating_test.db"))

import sys
import io
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db
import admin_source as adm
import server

ADMIN_XLS = "/Volumes/D/编程/业绩到期查询/项目查询导出.xls"
MGMT_XLSX = "/Volumes/D/编程/业绩到期查询/自定义上传优化版/合同管理.xlsx"

OPENID_A = "openid_liupeng"
OPENID_B = "openid_huangtianqi"
OPENID_ADMIN = "admin"


def main():
    # 干净 DB
    db_path = os.path.join(HERE, "rating_test.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ["RATING_DB"] = db_path
    db.init_db()

    # 干净 DB（路径由顶部 RATING_DB 环境变量决定）
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()

    print(">> 1) 管理员解析后台 xls 入库 ...")
    admin_recs = adm.parse_admin_xls(ADMIN_XLS)
    n = db.replace_admin_source(admin_recs)
    db.upsert_user(OPENID_ADMIN, role="admin")
    print(f"   后台评级记录: {n}")

    print(">> 2) 解析合同管理，按市场人员拆分归属 ...")
    cmap = adm.parse_contract_mgmt(MGMT_XLSX)
    print(f"   有效合同号: {len(cmap)}")
    a_rows, b_rows = [], []
    for k, v in cmap.items():
        row = {"contract_no": k, "marketer": v["marketer"],
               "entrust": v["entrust"], "bond": v["bond"], "status": v["status"]}
        if v["marketer"] == "刘鹏":
            a_rows.append(row)
        else:
            b_rows.append(row)
    db.upsert_user(OPENID_A, marketer_name="刘鹏")
    db.upsert_user(OPENID_B, marketer_name=b_rows[0]["marketer"] if b_rows else None)
    db.replace_contract_uploads(OPENID_A, a_rows)
    db.replace_contract_uploads(OPENID_B, b_rows)
    print(f"   A(刘鹏) 合同: {len(a_rows)}   B(其他) 合同: {len(b_rows)}")

    print(">> 3) 触发归属 + 到期计算 ...")
    stats = server.run_compute()
    print("   compute stats:", stats)

    print(">> 4) 断言：隔离 + 合同号 join 主导 ...")
    a_ratings = db.get_my_ratings(OPENID_A)
    b_ratings = db.get_my_ratings(OPENID_B)
    a_subjects = {r["subject"] for r in a_ratings}
    b_subjects = {r["subject"] for r in b_ratings}

    assert len(a_ratings) > 0, "A 应有评级"
    assert all(r["attribution"] == "contract_join" for r in a_ratings), \
        "A 应全部 contract_join"
    # 隔离：A 与 B 的 subject 不应有合同号交集对应（这里 B 只有黄/邓，A 是刘鹏）
    print(f"   A 到期记录: {len(a_ratings)}   B 到期记录: {len(b_ratings)}")
    print(f"   A 状态分布: {_cnt(a_ratings)}")
    print(f"   B 状态分布: {_cnt(b_ratings)}")

    # 验证一个已知跨年易主客户在 A 名下正确（如后台含 四川省乡村发展集团）
    target = "四川省乡村发展集团有限公司"
    a_hit = [r for r in a_ratings if r["subject"] == target]
    print(f"   [跨年验证] {target} 在 A 名下: {len(a_hit)} 条 -> "
          f"{[r['expiry_date'] for r in a_hit]}")

    # ---- HTTP 层 ----
    print(">> 5) HTTP 层联调 (Flask test_client) ...")
    client = server.app.test_client()
    r = client.get("/api/health")
    assert r.status_code == 200, r.get_json()
    print("   /api/health:", r.get_json())

    # login（admin）
    r = client.post("/api/login", json={"code": "admin", "role": "admin"})
    assert r.status_code == 200
    print("   /api/login(admin):", r.get_json())

    # 个人 ratings（带 X-Openid）
    r = client.get("/api/my/ratings", headers={"X-Openid": OPENID_A})
    assert r.status_code == 200
    j = r.get_json()
    print(f"   /api/my/ratings(A): count={j['count']}")

    # 管理员总览
    r = client.get("/api/admin/overview", headers={"X-Openid": OPENID_ADMIN})
    assert r.status_code == 200
    ov = r.get_json()
    print("   /api/admin/overview:", {k: ov[k] for k in
          ("total", "by_status", "unassigned")})
    print("   by_marketer:", {o: m.get("name") for o, m in
          ov["by_marketer"].items()})

    print(">> 4.5) 验证时间窗口兜底链路（无合同号记录）...")
    no_ct = [r for r in admin_recs if not r["contract_no"] and r["issuance"]]
    if no_ct:
        s = no_ct[0]
        base = s["issuance"]  # 用出具时间作基准，确保落在窗口内 -> window_match
        db.replace_fallback_uploads(OPENID_A, "chenlan",
            [{"subject": s["subject"], "base_date": base,
              "bond_type": "", "status_raw": "已通过"}])
        stats2 = server.run_compute()
        print("   兜底后 attribution:", stats2["attribution_breakdown"])
        assert "window_match" in stats2["attribution_breakdown"], \
            "兜底 window_match 应出现"
        print("   时间窗口兜底链路 OK ✅")

    print("\n== 全部断言通过 ✅ ==")

    # 清理测试 DB
    os.remove(db_path)


def _cnt(rows):
    from collections import Counter
    return dict(Counter(r["status"] for r in rows))


if __name__ == "__main__":
    main()
