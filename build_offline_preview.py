# -*- coding: utf-8 -*-
"""
从已 seed 的 SQLite 导出真实结果，生成一个完全自包含、数据内联的离线预览页
offline_preview.html（纯前端渲染，不依赖后端 / 网络，可在任意预览面板直接点击）。

用法：RATING_DB=rating_preview.db python build_offline_preview.py
"""
import os
import json
import hashlib

import db

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    db.init_db()
    conn = db.get_conn()

    # 所有市场人员用户
    users = conn.execute(
        "SELECT openid, role, marketer_name FROM users").fetchall()
    marketers = [{"openid": r["openid"], "name": r["marketer_name"]}
                 for r in users if r["role"] == "user" and r["marketer_name"]]

    # 每个市场人员的到期提醒
    ratings = {}
    for m in marketers:
        rows = conn.execute(
            "SELECT subject, contract_no, base_date, expiry_date, remind_date, "
            "status, debt_type, project_type, attribution FROM final_ratings "
            "WHERE openid=?", (m["openid"],)).fetchall()
        ratings[m["openid"]] = [dict(r) for r in rows]

    # 管理员总览（原始 by_marketer 只有 openid -> status计数）
    ov = db.get_admin_overview()
    conn.close()

    data = {
        "marketers": marketers,
        "ratings": ratings,
        "overview": ov,
    }

    html = build_html(data)
    out = os.path.join(HERE, "offline_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("written:", out, "| marketers:", [m["name"] for m in marketers],
          "| total final:", ov["total"])


def build_html(data):
    payload = json.dumps(data, ensure_ascii=False)
    return OFFLINE_TEMPLATE.replace("/*__DATA__*/", payload)


OFFLINE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>评级到期提醒 · 离线预览</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--line:#e6e8eb;--txt:#1f2329;--sub:#8a9099;
        --blue:#2b6cff;--red:#e74c3c;--orange:#f39c12;--green:#27ae60;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--txt);padding:18px;max-width:920px;margin:0 auto}
  h1{font-size:20px;margin-bottom:2px}
  .sub{color:var(--sub);font-size:13px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
  h2{font-size:15px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
  .badge{font-size:11px;padding:2px 8px;border-radius:20px;background:#eef2ff;color:var(--blue)}
  button{background:var(--blue);color:#fff;border:none;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;transition:.15s}
  button:hover{opacity:.9}
  button.grey{background:#eef0f2;color:var(--txt)}
  button.active{outline:2px solid var(--blue);outline-offset:1px}
  .ident{font-size:13px}
  .pill{padding:4px 10px;border-radius:20px;background:#eef2ff;color:var(--blue);font-size:12px}
  .pill.admin{background:#fdecec;color:var(--red)}
  .stat{display:flex;gap:14px;flex-wrap:wrap;margin-top:6px}
  .stat .b{background:#fafbfc;border:1px solid var(--line);border-radius:10px;padding:10px 14px;text-align:center}
  .stat .n{font-size:22px;font-weight:700}
  .stat .l{font-size:12px;color:var(--sub)}
  .ratings{display:grid;gap:10px}
  .r{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fff;position:relative}
  .r .name{font-weight:600;font-size:14px}
  .r .meta{color:var(--sub);font-size:12px;margin-top:4px;line-height:1.6}
  .r .st{position:absolute;top:12px;right:14px;font-size:12px;padding:3px 10px;border-radius:20px;color:#fff}
  .st.overdue{background:var(--red)}.st.due{background:var(--orange)}.st.upcoming{background:var(--green)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
  th{color:var(--sub);font-weight:500;font-size:12px}
  .sec-title{font-size:12px;color:var(--sub);margin:14px 0 6px;font-weight:600}
  .legend span{display:inline-block;margin-right:12px;font-size:12px;color:var(--sub)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
  .empty{color:var(--sub);font-size:13px;padding:8px 0}
  .note{font-size:12px;color:var(--sub);margin-top:8px}
</style>
</head>
<body>
  <h1>📅 评级到期提醒 · 小程序（离线预览）</h1>
  <div class="sub">本页为自包含演示：数据已内联，无需后端。参考日 2026-07-09｜归属：合同号 join（主）+ 时间窗口（兜底）</div>

  <div class="card">
    <h2>身份切换</h2>
    <div class="row" style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="grey" id="b-admin" onclick="switchTo('admin')">管理员</button>
      <span id="mkButtons"></span>
    </div>
    <div class="ident" style="margin-top:10px"><span class="pill" id="rolePill">未选择</span> <span id="whoText"></span></div>
  </div>

  <div class="card hide" id="adminPanel">
    <h2>管理员总览 <span class="badge">admin</span></h2>
    <div class="stat" id="ovStat"></div>
    <div class="sec-title">按市场人员下钻</div>
    <table><thead><tr><th>市场人员</th><th>即将到期</th><th>已过期</th><th>有效期内</th><th>合计</th></tr></thead><tbody id="ovBody"></tbody></table>
    <div class="note">说明：当前演示数据里仅「刘鹏」名下有命中合同号的评级；黄天祺/邓少平样本合同较少且出具时间较早，被过期噪音窗口过滤后为 0 条（真实数据稀疏，非异常）。</div>
  </div>

  <div class="card hide" id="userPanel">
    <h2>我的到期提醒 <span class="badge" id="mkName"></span></h2>
    <div class="legend" style="margin-bottom:6px">
      <span><i class="dot" style="background:var(--red)"></i>已过期(可重新营销)</span>
      <span><i class="dot" style="background:var(--orange)"></i>即将到期(3月内)</span>
      <span><i class="dot" style="background:var(--green)"></i>有效期内</span>
    </div>
    <div class="ratings" id="myRatings"></div>
  </div>

<script>
const DATA = /*__DATA__*/;
const NAME_BY_OID = {};
DATA.marketers.forEach(m => NAME_BY_OID[m.openid] = m.name);

function switchTo(role, openid){
  document.getElementById("b-admin").classList.toggle("active", role==="admin");
  (DATA.marketers||[]).forEach(m=>{
    const el=document.getElementById("mk-"+m.openid);
    if(el) el.classList.toggle("active", role!=="admin" && openid===m.openid);
  });
  const isAdmin = role==="admin";
  document.getElementById("adminPanel").classList.toggle("hide", !isAdmin);
  document.getElementById("userPanel").classList.toggle("hide", isAdmin);
  const pill=document.getElementById("rolePill");
  if(isAdmin){ pill.textContent="管理员"; pill.className="pill admin"; document.getElementById("whoText").textContent="全局视角"; renderOverview(); }
  else {
    const m=DATA.marketers.find(x=>x.openid===openid);
    pill.textContent="市场人员"; pill.className="pill";
    document.getElementById("whoText").textContent=m?m.name:"";
    document.getElementById("mkName").textContent=m?m.name:"";
    renderMy(openid);
  }
}
function renderMy(openid){
  const rs=(DATA.ratings[openid]||[]);
  const el=document.getElementById("myRatings");
  if(!rs.length){ el.innerHTML='<div class="empty">该市场人员名下暂无命中合同号的到期提醒（演示数据稀疏）。</div>'; return; }
  const order={overdue:0,due:1,upcoming:2};
  rs.sort((a,b)=>order[a.status]-order[b.status]);
  el.innerHTML=rs.map(r=>{
    const st={overdue:"已过期",due:"即将到期",upcoming:"有效期内"}[r.status];
    return `<div class="r"><span class="st ${r.status}">${st}</span>
      <div class="name">${r.subject}</div>
      <div class="meta">合同号：${r.contract_no||"—"}<br>
        出具日：${r.base_date} ｜ 到期日：<b>${r.expiry_date}</b><br>
        类型：${r.debt_type||"—"} ｜ 归属：${r.attribution==="contract_join"?"合同号匹配":"其他"}</div></div>`;
  }).join("");
}
function renderOverview(){
  const o=DATA.overview; const bs=o.by_status||{};
  document.getElementById("ovStat").innerHTML=
    `<div class="b"><div class="n">${o.total||0}</div><div class="l">总归属</div></div>`+
    `<div class="b"><div class="n" style="color:var(--red)">${bs.overdue||0}</div><div class="l">已过期</div></div>`+
    `<div class="b"><div class="n" style="color:var(--orange)">${bs.due||0}</div><div class="l">即将到期</div></div>`+
    `<div class="b"><div class="n" style="color:var(--green)">${bs.upcoming||0}</div><div class="l">有效期内</div></div>`;
  const tb=document.getElementById("ovBody"); tb.innerHTML="";
  const bm=o.by_marketer||{};
  const ids=Object.keys(bm);
  if(!ids.length){ tb.innerHTML='<tr><td colspan="5" class="empty">暂无归属数据</td></tr>'; return; }
  ids.forEach(oid=>{
    const m=bm[oid]; const name=NAME_BY_OID[oid]||oid;
    tb.innerHTML+=`<tr><td>${name}</td><td style="color:var(--orange)">${m.overdue||0}</td><td style="color:var(--red)">${m.due||0}</td><td style="color:var(--green)">${m.upcoming||0}</td><td><b>${m.overdue+m.due+m.upcoming}</b></td></tr>`;
  });
}
// 渲染市场人员按钮
(function(){
  const box=document.getElementById("mkButtons");
  (DATA.marketers||[]).forEach(m=>{
    const b=document.createElement("button");
    b.className="grey"; b.id="mk-"+m.openid; b.textContent=m.name;
    b.onclick=()=>switchTo("user", m.openid);
    box.appendChild(b);
  });
  // 默认进入刘鹏视角
  if(DATA.marketers && DATA.marketers.length) switchTo("user", DATA.marketers[0].openid);
  else switchTo("admin");
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
