# -*- coding: utf-8 -*-
"""
validate_upload.py — 上传校验 + 规则演示

对一次/多次上传运行 rating_engine，输出：
  1) 每个文件的「列识别映射建议」
  2) 校验问题清单（error/warning）
  3) 校验报告（行数、丢弃、状态分布）
  4) 合并去重后的到期提醒结果（overdue/due/upcoming 计数 + 预览）

用法:
  python validate_upload.py 承揽立项.xlsx 项目作业.xlsx
可选环境变量:
  REF_DATE=2026-07-09 VALIDITY=12 REMIND=3 python validate_upload.py ...
"""

import os
import sys
import json
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rating_engine as eng


def _fmt_name(fmt):
    return {"chenlan": "承揽立项", "zuoye": "项目作业", "generic": "通用/未知"}.get(fmt, fmt)


def print_report(results, merged, ref_date, validity, remind, overdue_window=None):
    print("=" * 70)
    print(f"评级到期提醒 · 上传校验报告")
    print(f"参考日期={ref_date}  有效期={validity}月  提醒窗口={remind}月"
          f"  过期提醒窗口={overdue_window or '全部'}月")
    print("=" * 70)

    for i, res in enumerate(results, 1):
        print(f"\n### 文件 {i}：识别为【{_fmt_name(res['format'])}】###")
        print("列识别映射建议：")
        for k, v in res["mapping"].items():
            if v is None:
                print(f"  - {k}: 未识别")
            else:
                print(f"  - {v['field']:12s}← [{v['col']}] {v['header']!r}  "
                      f"(置信度:{v['confidence']})")
        if res["validation"]:
            print("校验问题：")
            for v in res["validation"]:
                print(f"  ! [{v['level']}] {v['msg']}")
        else:
            print("校验问题：无")
        s = res["summary"]
        print(f"行数：原始 {s['raw_rows']} / 保留 {s['kept_rows']} / 丢弃 {s['dropped_rows']}")
        print("状态分布：", s["status_distribution"])

    print("\n" + "-" * 70)
    print("合并 + 去重 + 到期计算（最终交付）")
    print("-" * 70)
    print(f"去重丢弃行数：{merged['dropped_dups']}")
    if "dropped_overdue" in merged:
        print(f"过期窗口过滤丢弃：{merged['dropped_overdue']}")
    print(f"最终记录数：{merged['total']}")
    print("状态计数：", merged["by_status"])
    print("\n提醒预览（前 15 条，overdue/due 优先）：")
    print(f"  {'受评主体':<28s} {'类型':<14s} {'基准日':<12s} {'到期日':<12s} {'状态'}")
    for r in merged["records"][:15]:
        print(f"  {r['subject'][:26]:<28s} {(r['bond_type'] or '-')[:12]:<14s} "
              f"{r['base_date']:<12s} {r['expiry_date']:<12s} {r['status']}")


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_upload.py file1.xlsx [file2.xlsx ...]")
        sys.exit(1)

    ref_date = date.today()
    if os.environ.get("REF_DATE"):
        ref_date = date.fromisoformat(os.environ["REF_DATE"])
    validity = int(os.environ.get("VALIDITY", "12"))
    remind = int(os.environ.get("REMIND", "3"))
    overdue_window = os.environ.get("OVERDUE_WINDOW")
    overdue_window = int(overdue_window) if overdue_window else None

    # 依据文件名推测来源标签
    labels = []
    for p in sys.argv[1:]:
        name = p.lower()
        if "承揽" in name or "立项" in name:
            labels.append("chenlan")
        elif "作业" in name:
            labels.append("zuoye")
        else:
            labels.append(None)

    results, merged = eng.run(sys.argv[1:], ref_date=ref_date,
                              validity=validity, remind=remind,
                              source_labels=labels,
                              overdue_window=overdue_window)

    print_report(results, merged, ref_date, validity, remind,
                 overdue_window=overdue_window)

    # 同时导出 JSON，方便接后端
    out = {
        "ref_date": ref_date.isoformat(),
        "validity_months": validity,
        "remind_months": remind,
        "per_file": [r["summary"] for r in results],
        "merged": merged,
    }
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "validation_report.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n[已导出 validation_report.json]")


if __name__ == "__main__":
    main()
