# -*- coding: utf-8 -*-
"""
scheduler.py — 评级到期提醒 定时推送守护

设计：
  - 作为独立进程运行（避免与 gunicorn 多 worker 耦合），由 systemd timer 每日调用：
        python scheduler.py once        # 立即跑一次扫描+推送
  - 也可以前台常驻模式运行（每天 09:00 自动跑）：
        python scheduler.py             # 循环模式

依赖 server.dispatch_notifications()（站内消息 + 邮件 + 微信订阅，配置驱动）。
"""
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import server  # 复用 dispatch_notifications / 常量；导入即执行 db.init_db()


def run_once(ref_date=None):
    if ref_date is None:
        ref_date = date.today()
    print(f"[scheduler] run at {datetime.now().isoformat()} ref={ref_date}")
    try:
        res = server.dispatch_notifications(ref_date=ref_date, dry=False)
        print(f"[scheduler] messages={len(res['messages'])} "
              f"channel_sent={len(res['sent'])} skipped={len(res['skipped'])}")
        return res
    except Exception as e:
        print("[scheduler] ERROR:", e)
        return {"error": str(e)}


def _sleep_until_next_run(hour=9):
    now = datetime.now()
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    secs = (nxt - now).total_seconds()
    print(f"[scheduler] next run scheduled at {nxt.isoformat()} "
          f"(in {int(secs)}s)")
    time.sleep(secs)


def loop():
    # 启动后先跑一次，之后每天固定时刻跑
    run_once()
    while True:
        _sleep_until_next_run(9)
        run_once()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        loop()
