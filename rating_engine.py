# -*- coding: utf-8 -*-
"""
rating_engine.py — 评级到期提醒 规则引擎（零依赖，纯标准库）

设计目标（对应 RatingsDQ 自定义上传优化版）：
- 不再依赖后台固定源文件，每个使用者上传自己的 Excel（xlsx）。
- 自适应识别列结构（承揽立项 / 项目作业 两种格式 + 通用模糊兜底）。
- 支持单文件或双文件合并（按 受评主体 + 债券类型 去重，取最新基准日）。
- 计算到期日 / 提醒日 / 三态状态（overdue / due / upcoming）。

可直接被后端接收上传 bytes 后调用 process_upload()。
"""

import zipfile
import re
import json
import calendar
from datetime import datetime, timedelta, date
from collections import Counter, defaultdict

# ----------------------------------------------------------------------------
# 1. 零依赖 xlsx 读取（zipfile + XML，仅第一个工作表，沿用原 rating_data.py 思路）
# ----------------------------------------------------------------------------
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col_letters(ref):
    return "".join(c for c in ref if c.isalpha())


def _col_idx(letters):
    n = 0
    for c in letters:
        n = n * 26 + (ord(c.upper()) - 64)
    return n


def read_xlsx(path_or_bytes):
    """返回 (headers: list[(col, name)], rows: list[dict])。headers 按列序。"""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        z = zipfile.ZipFile(__import__("io").BytesIO(path_or_bytes))
    else:
        z = zipfile.ZipFile(path_or_bytes)

    wb = _et(z.read("xl/workbook.xml"))
    rels = _et(z.read("xl/_rels/workbook.xml.rels"))
    relmap = {r.attrib["Id"]: r.attrib["Target"] for r in rels}

    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        sx = _et(z.read("xl/sharedStrings.xml"))
        for it in sx:
            shared.append("".join(t.text or "" for t in it.iter(NS + "t")))

    sheets = wb.find("a:sheets".replace("a:", NS) if False else "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets")
    first = list(sheets)[0]
    tgt = relmap[first.attrib[REL]].lstrip("/")
    if not tgt.startswith("xl/"):
        tgt = "xl/" + tgt
    sx = _et(z.read(tgt))
    sd = sx.find(NS + "sheetData")

    rows = []
    for row in sd.findall(NS + "row"):
        d = {}
        for c in row.findall(NS + "c"):
            ref = c.attrib["r"]
            col = _col_letters(ref)
            t = c.attrib.get("t")
            if t == "inlineStr":
                v = "".join(n.text or "" for n in c.iter(NS + "t")).strip()
            else:
                vn = c.find(NS + "v")
                v = vn.text.strip() if (vn is not None and vn.text) else ""
                if t == "s" and v:
                    v = shared[int(v)]
            d[col] = v
        rows.append(d)

    if not rows:
        return [], []
    hdr = rows[0]
    cols = sorted(hdr.keys(), key=_col_idx)
    headers = [(c, (hdr.get(c) or "").strip()) for c in cols]
    return headers, rows[1:]


def _et(data):
    return __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(data)


# ----------------------------------------------------------------------------
# 2. 列识别（自适应 + 模糊兜底）
# ----------------------------------------------------------------------------
def _norm(s):
    """归一化表头：去空格、转小写、去常见标点，仅保留数字字母汉字。"""
    s = (s or "").lower()
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)
    return s


# 每个标准字段 → 候选表头（按优先级排序，靠前的更准）
SUBJECT_CANDIDATES = [
    "受评人", "受评主体", "被评级方", "评级对象", "评级主体",
    "客户名称", "主体名称", "企业名称",
]
SUBJECT_FALLBACK = ["委托方", "委托人", "客户"]
BASE_DATE_CANDIDATES = [
    # 优先级高 → 低：评审/报告日最准，其次派单/申请日
    "评审日期", "评级日期", "报告日期", "出报告日期",
    "派单日期", "申请时间", "立项时间", "派单时间",
]
# 年月兜底（仅当无任何日期列时）
YEAR_COL = "年份"
MONTH_COL = "月份"
STATUS_CANDIDATES = ["审核状态", "作业进度", "审批状态", "状态"]
BOND_TYPE_CANDIDATES = [
    "债券类型", "子项目债券类型", "业务类型", "项目类型", "评级类型", "债项类型",
]
MARKETER_CANDIDATES = [
    "申请人", "营销人员", "市场人员", "业务员", "负责人", "客户经理",
]


def _find_header(headers, candidate, norm_headers=None):
    if norm_headers is None:
        norm_headers = {c: _norm(n) for c, n in headers}
    nc = _norm(candidate)
    if not nc:
        return None
    # 精确（归一化相等）优先
    for c, n in headers:
        if norm_headers[c] == nc:
            return c
    # 包含匹配
    for c, n in headers:
        hn = norm_headers[c]
        if nc and (nc in hn or hn in nc):
            return c
    return None


def map_columns(headers):
    """返回 {标准字段: (列字母, 表头名, 置信度)}，未匹配为 None。"""
    norm_headers = {c: _norm(n) for c, n in headers}
    mapping = {}

    def best(candidates):
        for cand in candidates:
            col = _find_header(headers, cand, norm_headers)
            if col:
                conf = "high" if norm_headers[col] == _norm(cand) else "medium"
                return col, dict(headers)[col], conf
        return None

    subj = best(SUBJECT_CANDIDATES)
    mapping["subject"] = subj
    mapping["subject_fallback"] = best(SUBJECT_FALLBACK)
    mapping["base_date"] = best(BASE_DATE_CANDIDATES)
    mapping["year"] = best([YEAR_COL]) if mapping["base_date"] is None else None
    mapping["month"] = best([MONTH_COL]) if mapping["base_date"] is None else None
    mapping["status"] = best(STATUS_CANDIDATES)
    mapping["bond_type"] = best(BOND_TYPE_CANDIDATES)
    mapping["marketer"] = best(MARKETER_CANDIDATES)
    return mapping


def detect_format(headers):
    names = {_norm(n) for _, n in headers}
    if any("受评人" in n for n in names):
        return "chenlan"   # 承揽立项
    if any("受评主体" in n for n in names):
        return "zuoye"     # 项目作业
    return "generic"


# ----------------------------------------------------------------------------
# 3. 日期解析 / 月份运算 / 状态分类
# ----------------------------------------------------------------------------
def parse_date(value):
    t = (value or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    # Excel 序列号
    try:
        s = float(t)
        return (datetime(1899, 12, 30) + timedelta(days=s)).date()
    except ValueError:
        return None


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def classify(base_date, ref_date, validity=12, remind=3):
    exp = add_months(base_date, validity)
    rem = add_months(exp, -remind)
    if ref_date > exp:
        return "overdue"
    if ref_date >= rem:
        return "due"
    return "upcoming"


# 状态过滤：各格式认为"有效"的取值（子串匹配）
STATUS_OK = {
    "chenlan": {"已通过"},
    "zuoye": {"结项"},          # 含 "结项,结项"
    "generic": None,            # 未知格式：不过滤（有 subject+date 即纳入）
}


def _status_ok(status_raw, fmt):
    ok_set = STATUS_OK.get(fmt)
    if ok_set is None:
        return True
    s = (status_raw or "").strip()
    if not s:
        return False
    return any(k in s for k in ok_set)


# ----------------------------------------------------------------------------
# 4. 处理一条上传（统一规范化 + 校验 + 计算）
# ----------------------------------------------------------------------------
# 合并时数据源优先级（数值越大越优先用于"最新基准日"判定）
SOURCE_PRECEDENCE = {"zuoye": 2, "chenlan": 1, "generic": 1}


def process_upload(path_or_bytes, ref_date=None, validity=12, remind=3,
                   source_label=None):
    """
    处理一次上传，返回 dict：
      format, mapping, validation(问题列表), records(清洗后记录),
      raw_count, kept_count, dropped_count, summary(状态统计)
    records 元素: {subject, base_date(str), bond_type, status_raw,
                   status_ok, marketer, source, extra}
    """
    if ref_date is None:
        ref_date = date.today()

    headers, rows = read_xlsx(path_or_bytes)
    fmt = detect_format(headers)
    if source_label is None:
        source_label = fmt
    mapping = map_columns(headers)
    hdr_names = dict(headers)

    validation = []

    # 必填列检查
    if mapping["subject"] is None and mapping["subject_fallback"] is None:
        validation.append({"level": "error",
                           "msg": "未识别到『受评方』列（受评人/受评主体/委托方），无法继续"})
    if mapping["base_date"] is None and not (mapping["year"] and mapping["month"]):
        validation.append({"level": "error",
                           "msg": "未识别到任何日期列（申请时间/派单日期/评审日期/年份+月份）"})

    col_subj = mapping["subject"][0] if mapping["subject"] else None
    col_fb = mapping["subject_fallback"][0] if mapping["subject_fallback"] else None
    col_date = mapping["base_date"][0] if mapping["base_date"] else None
    col_year = mapping["year"][0] if mapping["year"] else None
    col_month = mapping["month"][0] if mapping["month"] else None
    col_status = mapping["status"][0] if mapping["status"] else None
    col_bond = mapping["bond_type"][0] if mapping["bond_type"] else None
    col_mkt = mapping["marketer"][0] if mapping["marketer"] else None

    records = []
    dropped = 0
    date_unparsed = 0

    for r in rows:
        subj = (r.get(col_subj, "") if col_subj else "").strip()
        if not subj and col_fb:
            subj = (r.get(col_fb, "") or "").strip()
        if not subj:
            dropped += 1
            continue

        # 基准日
        base = None
        if col_date:
            base = parse_date(r.get(col_date, ""))
        if base is None and col_year and col_month:
            y = (r.get(col_year, "") or "").strip()
            m = (r.get(col_month, "") or "").strip()
            try:
                base = date(int(float(y)), int(float(m)), 1)
            except (ValueError, TypeError):
                base = None
        if base is None:
            date_unparsed += 1
            dropped += 1
            continue

        status_raw = (r.get(col_status, "") if col_status else "").strip()
        bond = (r.get(col_bond, "") if col_bond else "").strip()
        mkt = (r.get(col_mkt, "") if col_mkt else "").strip()

        status_ok = _status_ok(status_raw, fmt)
        if not status_ok:
            # 状态不过滤行，但标记，便于统计与可选排除
            pass

        extra = {hdr_names.get(c, c): (r.get(c, "") or "").strip()
                 for c in r.keys()
                 if c not in (col_subj, col_fb, col_date, col_year, col_month,
                              col_status, col_bond, col_mkt)}

        records.append({
            "subject": subj,
            "base_date": base.isoformat(),
            "base_date_obj": base,
            "bond_type": bond,
            "status_raw": status_raw,
            "status_ok": status_ok,
            "marketer": mkt,
            "source": source_label,
            "extra": extra,
        })

    if date_unparsed:
        validation.append({"level": "warning",
                           "msg": f"{date_unparsed} 行因日期无法解析被丢弃"})

    # 状态统计
    status_dist = Counter((rec["status_raw"] or "(空)") for rec in records)
    kept_count = len(records)

    summary = {
        "format": fmt,
        "raw_rows": len(rows),
        "kept_rows": kept_count,
        "dropped_rows": dropped,
        "status_distribution": dict(status_dist),
    }

    return {
        "format": fmt,
        "mapping": _mapping_public(mapping, hdr_names),
        "validation": validation,
        "records": records,
        "summary": summary,
    }


def _mapping_public(mapping, hdr_names):
    out = {}
    label_map = {
        "subject": "受评方", "subject_fallback": "受评方兜底(委托方)",
        "base_date": "基准日", "year": "年份", "month": "月份",
        "status": "状态", "bond_type": "评级类型", "marketer": "业务人员",
    }
    for k, v in mapping.items():
        if v is None:
            out[k] = None
        else:
            col, name, conf = v
            out[k] = {"col": col, "header": name, "confidence": conf,
                      "field": label_map.get(k, k)}
    return out


# ----------------------------------------------------------------------------
# 5. 多文件合并 + 去重 + 到期计算（核心交付）
# ----------------------------------------------------------------------------
def merge_and_compute(results, ref_date=None, validity=12, remind=3,
                      include_nonok=False, overdue_window=None):
    """
    results: process_upload 返回的列表（可多个上传）
    overdue_window: 可选，int 月。若设置，则丢弃 expired 且
                    expiry < ref_date - overdue_window 的陈年过期记录（噪音过滤）。
    返回 {
      records: 去重后的最终记录（含 expiry/remind/status）,
      by_status: 状态计数,
      total: 最终记录数,
      dropped_dups: 被去重丢弃的行数,
      dropped_overdue: 被过期窗口过滤的行数,
    }
    """
    if ref_date is None:
        ref_date = date.today()

    groups = defaultdict(list)
    for res in results:
        for rec in res["records"]:
            if not include_nonok and not rec["status_ok"]:
                continue
            key = (rec["subject"], rec["bond_type"] or "")
            groups[key].append(rec)

    final = []
    dropped_dups = 0
    for key, recs in groups.items():
        # 取基准日最新；并列时优先 source 优先级高者
        best = None
        for rec in recs:
            if best is None:
                best = rec
                continue
            bd = rec["base_date_obj"]
            bd_best = best["base_date_obj"]
            if bd > bd_best:
                best = rec
            elif bd == bd_best:
                sp = SOURCE_PRECEDENCE.get(rec["source"], 1)
                spb = SOURCE_PRECEDENCE.get(best["source"], 1)
                if sp > spb:
                    best = rec
        dropped_dups += len(recs) - 1

        base = best["base_date_obj"]
        expiry = add_months(base, validity)
        remind_date = add_months(expiry, -remind)
        st = classify(base, ref_date, validity, remind)
        final.append({
            "subject": best["subject"],
            "bond_type": best["bond_type"],
            "base_date": base.isoformat(),
            "expiry_date": expiry.isoformat(),
            "expiry_date_obj": expiry,
            "remind_date": remind_date.isoformat(),
            "status": st,
            "status_raw": best["status_raw"],
            "marketer": best["marketer"],
            "source": best["source"],
            "extra": best["extra"],
        })

    # 过期窗口过滤：丢弃过期的陈年记录（仅保留近 overdue_window 月内的）
    dropped_overdue = 0
    if overdue_window is not None:
        cutoff = add_months(ref_date, -overdue_window)
        kept = []
        for r in final:
            if r["status"] == "overdue" and r["expiry_date_obj"] < cutoff:
                dropped_overdue += 1
            else:
                kept.append(r)
        final = kept

    by_status = Counter(r["status"] for r in final)
    final.sort(key=lambda r: (r["status"] != "overdue", r["expiry_date"]))
    return {
        "records": final,
        "by_status": dict(by_status),
        "total": len(final),
        "dropped_dups": dropped_dups,
        "dropped_overdue": dropped_overdue,
    }


# ----------------------------------------------------------------------------
# 6. 便捷入口：一次处理（可多文件 bytes/路径）
# ----------------------------------------------------------------------------
def run(paths_or_bytes_list, ref_date=None, validity=12, remind=3,
        include_nonok=False, source_labels=None, overdue_window=None):
    results = []
    for i, p in enumerate(paths_or_bytes_list):
        lbl = None
        if source_labels and i < len(source_labels):
            lbl = source_labels[i]
        results.append(process_upload(p, ref_date=ref_date,
                                      validity=validity, remind=remind,
                                      source_label=lbl))
    merged = merge_and_compute(results, ref_date=ref_date,
                               validity=validity, remind=remind,
                               include_nonok=include_nonok,
                               overdue_window=overdue_window)
    return results, merged


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python rating_engine.py file1.xlsx [file2.xlsx ...]")
        sys.exit(1)
    res, merged = run(sys.argv[1:])
    print(json.dumps({"per_file": [r["summary"] for r in res],
                      "merged": merged}, ensure_ascii=False, indent=2,
                      default=str))
