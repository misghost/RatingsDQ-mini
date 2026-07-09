# -*- coding: utf-8 -*-
"""
admin_source.py — 管理员后台总体源数据(.xls) 解析 + 归属算法原型

职责：
  1. 解析项目查询导出.xls（全量评级台账，含 主体名称 + 报告落款日/评审日期）
  2. 以「报告落款日」为出具时间(缺失回退 评审日期→打印报告日期)，算到期
  3. 把每条评级记录「归属」给正确的市场人员(openid)：
     【核心原则】不按客户名归属，而按 (客户 + 时间窗口) 归属 ——
     匹配后台 立项日期 与 前端用户上传的 申请/派单基准日，取落在
     该评级周期、且时间最接近的市场的人员。这样天然实现
     “一年内一般一个市场人员”，并正确拆分“23年我 / 25年别人”的跨年易主。

注：本模块是后端场景，使用 xlrd 解析 .xls（前端引擎 rating_engine.py 仍零依赖）。
"""

import xlrd
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter


# ---- 列定位 ----
def _cidx(hdr, name):
    for i, h in enumerate(hdr):
        if name in h:
            return i
    return None


def _parse_date(v):
    v = (v or "").strip()
    if v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
        except Exception:
            return None
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(v, f).date()
        except ValueError:
            pass
    return None


def parse_admin_xls(path):
    """返回后台评级记录列表。"""
    wb = xlrd.open_workbook(path, on_demand=True)
    sh = wb.sheet_by_index(0)
    hdr = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    c_subj = _cidx(hdr, "主体名称")
    c_li = _cidx(hdr, "立项日期")
    c_rev = _cidx(hdr, "评审日期")
    c_sign = _cidx(hdr, "报告落款日")
    c_print = _cidx(hdr, "打印报告日期")
    c_type = _cidx(hdr, "项目类型")
    c_debt = _cidx(hdr, "债项类型")
    c_contract = _cidx(hdr, "合同编号")

    recs = []
    for r in range(1, sh.nrows):
        subj = str(sh.cell_value(r, c_subj)).strip()
        if not subj:
            continue
        li = _parse_date(str(sh.cell_value(r, c_li)).strip())
        rev = _parse_date(str(sh.cell_value(r, c_rev)).strip())
        sign = _parse_date(str(sh.cell_value(r, c_sign)).strip())
        prt = _parse_date(str(sh.cell_value(r, c_print)).strip())
        # 出具时间优先级：报告落款日 > 评审日期 > 打印报告日期
        issuance = sign or rev or prt
        recs.append({
            "subject": subj,
            "contract_no": str(sh.cell_value(r, c_contract)).strip()
                            if c_contract is not None else "",
            "li_date": li,                 # 立项日期（用于时间窗口匹配）
            "issuance": issuance,         # 出具时间
            "issuance_source": ("报告落款日" if sign else
                                "评审日期" if rev else
                                "打印报告日期" if prt else "无"),
            "project_type": str(sh.cell_value(r, c_type)).strip(),
            "debt_type": str(sh.cell_value(r, c_debt)).strip(),
        })
    return recs


def compute_expiry(rec, validity_months=12):
    """给定出具时间算到期日 + 三态。validity 可按 project_type 调整。"""
    if not rec.get("issuance"):
        return None
    exp = _add_months(rec["issuance"], validity_months)
    return exp


def _add_months(d, n):
    import calendar
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


# ---------------------------------------------------------------------------
# 归属算法（核心）
# ---------------------------------------------------------------------------
def build_user_index(user_records):
    """
    user_records: 来自各前端用户上传的清洗记录，每项含
      {openid/marketer, subject(受评主体), base_date(申请/派单日)}
    返回 index: subject -> [(openid, base_date), ...]
    """
    idx = defaultdict(list)
    for u in user_records:
        idx[u["subject"]].append((u["openid"], u["base_date"]))
    return idx


def attribute(rec, user_index, window_months=24):
    """
    把一条后台评级记录归属给市场人员。
    规则：
      - 在 user_index 中找 subject 相同的候选用户
      - 取 base_date 落在 [出具时间 - window, 出具时间] 内、且最接近出具时间的用户
        （即“发起该评级项目的市场人员”）
      - 无人命中 → 返回 None（unassigned，仅管理员可见）
    返回 (openid_or_None, reason)
    """
    subj = rec["subject"]
    issuance = rec.get("issuance")
    candidates = user_index.get(subj, [])
    if not candidates:
        return None, "no_candidate"
    if issuance is None:
        # 无出具时间无法做窗口，退化为最近一次 base_date 的用户
        best = max(candidates, key=lambda x: x[1])
        return best[0], "no_issuance_fallback"

    cutoff = _add_months(issuance, -window_months)
    in_window = [(oid, bd) for oid, bd in candidates
                 if cutoff <= bd <= issuance]
    pool = in_window if in_window else candidates
    # 最接近出具时间（且不晚于出具时间优先）
    best = min(pool, key=lambda x: abs((x[1] - issuance).days))
    return best[0], "window_match" if in_window else "closest_fallback"


# ---------------------------------------------------------------------------
# 合同号 join 归属（主机制，比时间窗口更精准）
# ---------------------------------------------------------------------------
import re as _re
_CONTRACT_PAT = _re.compile(r"^GC-")


def parse_contract_mgmt(path_xlsx):
    """
    解析前端市场人员上传的 合同管理.xlsx。
    返回 cmap: 合同编号 -> {申请人(marketer), 委托方, 债券类型, 合同状态}
    仅收录有效合同号(^GC-)，过滤占位/空；标记 已作废。
    """
    import rating_engine as _re_eng
    hdr, rows = _re_eng.read_xlsx(path_xlsx)
    name_of = {c: n for c, n in hdr}
    c_no = next((c for c, n in hdr if n == "合同编号"), None)
    c_app = next((c for c, n in hdr if n == "申请人"), None)
    c_ent = next((c for c, n in hdr if n == "委托方"), None)
    c_bond = next((c for c, n in hdr if n == "债券类型"), None)
    c_st = next((c for c, n in hdr if n == "合同状态"), None)
    cmap = {}
    for r in rows:
        no = (r.get(c_no, "") or "").strip()
        if not no or not _CONTRACT_PAT.match(no):
            continue
        cmap[no] = {
            "marketer": (r.get(c_app, "") or "").strip(),
            "entrust": (r.get(c_ent, "") or "").strip(),
            "bond": (r.get(c_bond, "") or "").strip(),
            "status": (r.get(c_st, "") or "").strip(),
        }
    return cmap


def attribute_by_contract(rec, cmap):
    """
    主归属：用合同号精确 join。
    返回 (marketer_or_None, reason)。无合同号或不在 cmap → (None, 'no_contract')。
    """
    no = (rec.get("contract_no") or "").strip()
    if not no or not _CONTRACT_PAT.match(no):
        return None, "no_contract"
    if no in cmap:
        return cmap[no]["marketer"], "contract_join"
    return None, "contract_not_in_upload"


def demo_contract(admin_path, mgmt_path, validity=12, ref_date=None):
    """演示：合同号 join 把后台评级精确归属到各市场人员。"""
    from datetime import date
    if ref_date is None:
        ref_date = date.today()
    recs = parse_admin_xls(admin_path)
    cmap = parse_contract_mgmt(mgmt_path)
    print(f"后台评级记录: {len(recs)}  合同管理有效合同号: {len(cmap)}")

    # 归因 + 到期
    by_mkt = defaultdict(list)
    no_contract = 0
    for r in recs:
        m, why = attribute_by_contract(r, cmap)
        if m is None:
            no_contract += 1
            continue
        exp = compute_expiry(r, validity)
        by_mkt[m].append((r["subject"], r["issuance"], exp, why, r["debt_type"]))

    print(f"经合同号成功归属: {sum(len(v) for v in by_mkt.values())}  "
          f"未带合同号/未匹配: {no_contract}")
    print("\n=== 各市场人员 归属到的评级数 ===")
    for m, lst in sorted(by_mkt.items(), key=lambda x: -len(x[1])):
        print(f"  {m}: {len(lst)} 条")

    # 展示 刘鹏 的到期预览（前12）
    liu = by_mkt.get("刘鹏", [])
    liu_valid = [x for x in liu if x[2]]
    liu_valid.sort(key=lambda x: (x[2] < ref_date, x[2]))
    print(f"\n=== 刘鹏 到期预览(前12, 参考日{ref_date}) ===")
    print(f"  {'主体':<26s} {'出具':<12s} {'到期':<12s} {'债项'}")
    for subj, iss, exp, why, debt in liu_valid[:12]:
        tag = "已过期" if exp < ref_date else ("3月内" if _add_months(exp, -3) <= ref_date else "有效")
        print(f"  {subj[:24]:<26s} {str(iss):<12s} {str(exp):<12s} {(debt or '-')[:10]} [{tag}]")


# ---------------------------------------------------------------------------
# 演示：用真实后台数据 + 模拟两个市场人员，验证跨年易主归属
# ---------------------------------------------------------------------------
def demo(path, subject_filter=None):
    recs = parse_admin_xls(path)
    print(f"后台评级记录总数(去空主体): {len(recs)}")
    has_iss = [r for r in recs if r["issuance"]]
    print(f"有出具时间的记录: {len(has_iss)}")
    src = Counter(r["issuance_source"] for r in recs)
    print("出具时间来源分布:", dict(src))

    # —— 模拟两个前端市场人员 ——
    # 用后台「立项日期」近似前端上传的「申请/派单基准日」：
    #   市场人员 A(刘鹏) 拥有 立项年份<=2023 的记录；
    #   市场人员 B(他人) 拥有 立项年份>=2024 的记录。
    # 这复现用户场景：“23年我做的，25年别人做的”。
    user_records = []
    for r in recs:
        if not r["li_date"]:
            continue
        oid = "A_刘鹏" if r["li_date"].year <= 2023 else "B_他人"
        user_records.append({"openid": oid, "subject": r["subject"],
                             "base_date": r["li_date"]})
    user_index = build_user_index(user_records)
    print(f"\n模拟市场人员覆盖的独特客户数: {len(user_index)}")

    # 选一个跨年易主的真实客户做归属演示
    if not subject_filter:
        # 自动挑「同客户跨年且 A/B 都有」的客户
        by_subj = defaultdict(list)
        for u in user_records:
            by_subj[u["subject"]].append(u["openid"])
        for s, oids in by_subj.items():
            if "A_刘鹏" in oids and "B_他人" in oids:
                subject_filter = s
                break
    print(f"\n===== 归属演示客户: {subject_filter} =====")
    target = [r for r in recs if r["subject"] == subject_filter]
    target.sort(key=lambda r: r["li_date"] or date(1900, 1, 1))
    for r in target:
        oid, reason = attribute(r, user_index)
        exp = compute_expiry(r)
        print(f"  立项={r['li_date']} 出具={r['issuance']}({r['issuance_source']}) "
              f"类型={r['project_type']}/{r['debt_type'] or '-'} "
              f"→ 归属={oid} [{reason}] 到期={exp}")


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "/Volumes/D/编程/业绩到期查询/项目查询导出.xls"
    sf = sys.argv[2] if len(sys.argv) > 2 else None
    mode = sys.argv[3] if len(sys.argv) > 3 else "timewindow"
    if mode == "contract":
        mgmt = sys.argv[4] if len(sys.argv) > 4 else \
            "/Volumes/D/编程/业绩到期查询/自定义上传优化版/合同管理.xlsx"
        demo_contract(p, mgmt)
    else:
        demo(p, sf)
