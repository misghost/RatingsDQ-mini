# 后端服务骨架 · 评级到期提醒（自定义上传优化版）

把已验证的规则引擎（`rating_engine.py` + `admin_source.py`）封装成**可独立部署的 HTTP 服务**，
支撑「微信小程序 + 市场人员自传数据 + 管理员总览」的产品形态。

---

## 1. 目录结构

```
rating-engine/
├── rating_engine.py   # 零依赖规则引擎（读 xlsx / 列识别 / 校验 / 去重 / 到期计算）
├── admin_source.py    # 后台 .xls 解析 + 合同号 join 归属（原型/被 server 复用）
├── db.py              # SQLite 存储层（users / 源数据 / 上传 / 最终记录）
├── server.py          # Flask API 骨架（本文件主角）
├── test_smoke.py      # 端到端冒烟测试（真实数据）
├── requirements.txt   # Flask / flask-cors / xlrd
└── README_server.md   # 本文件
```

---

## 2. 快速启动

依赖环境已用隔离 venv 装好（`/Users/lion/.workbuddy/binaries/python/envs/default`）。
如需重建：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

启动服务（默认端口 5001）：

```bash
cd rating-engine
RATING_DB=rating.db VALIDITY=12 REMIND=3 OVERDUE_WINDOW=12 \
  python server.py
# 或指定端口： PORT=8000 python server.py
```

健康检查：

```bash
curl http://localhost:5001/api/health
```

---

## 3. 数据流与归属机制

```
管理员上传 项目查询导出.xls  ──► admin_source 表（评级真相 + 出具时间）
        │
市场人员上传 合同管理.xlsx    ──► contract_uploads 表（合同号 → openid 主归属）
市场人员上传 承揽/作业.xlsx   ──► fallback_uploads 表（客户名 + 基准日，兜底）
        │
        ▼  POST /api/compute  （或每次上传后自动触发）
   对每条后台评级记录：
     ① 合同号 join 命中  → 该 openid           【主机制，精准】
     ② 否则 客户名+时间窗口 匹配 → 该 openid     【兜底】
     ③ 否则 unassigned（仅管理员可见）
   计算 到期日 / 提醒日 / 三态 → final_ratings（按 openid 隔离）
        │
        ▼
   GET /api/my/ratings        → 个人只看到自己归属的到期提醒
   GET /api/admin/overview     → 管理员看整体聚合 + 按市场人员下钻
```

**关键结论（实测）**：合同号 join 比客户名匹配精准得多。
同一客户跨年易主用合同号天然隔离——不同年份是不同合同号，各归各上传者，
不存在「客户名维度跨年错配」。这也是为什么优先推「市场人员上传合同管理.xlsx」。

---

## 4. 微信登录（接入点）

骨架阶段用 `X-Openid` Header 简化联调；真实接入在 `server.login()`：

```python
# 真实应替换为：
import requests
r = requests.get("https://api.weixin.qq.com/sns/jscode2session",
    params={"appid": APPID, "secret": SECRET,
            "js_code": code, "grant_type": "authorization_code"})
openid = r.json()["openid"]
```

小程序端调用流程：

```js
wx.login({ success: res => {
  wx.request({ url: '/api/login', method:'POST',
    data: { code: res.code },
    success: r => {
      wx.setStorageSync('openid', r.data.openid)  // 存本地
    }})
}})
// 之后每次请求带： header: { 'X-Openid': wx.getStorageSync('openid') }
```

---

## 5. API 清单

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/login` | 公开 | `{code}` → `{openid, token}`（真实接 code2Session） |
| GET | `/api/health` | 公开 | `{ok, admin_records}` |
| POST | `/api/admin/source` | admin | form-data `file`(.xls) 上传后台源数据，自动触发 compute |
| POST | `/api/upload/contract` | 用户 | form-data `file`(.xlsx) + 可选 `market_as`；合同号 join 归属 |
| POST | `/api/upload/fallback` | 用户 | form-data `file`(.xlsx) + `source=chenlan\|zuoye`；兜底上传 |
| POST | `/api/compute` | admin | 手动触发重算（可带 `ref_date`） |
| GET | `/api/my/ratings?status=` | 用户 | 个人到期提醒（`status=overdue\|due\|upcoming`） |
| GET | `/api/admin/overview` | admin | 整体聚合 + `by_marketer` |
| GET | `/api/admin/marketer?openid=` | admin | 某市场人员明细 |

所有受保护接口需带 `X-Openid` Header（或 `?openid=`）。

---

## 6. 覆盖率与现实说明（重要）

实测：后台全量台账 39603 条，合同管理有效合同号 132 个，
经合同号 join 命中 **134 条**（一个合同含多个子项目），其余 39469 条 `no_candidate`。

含义：**合同号 join 只覆盖「前端上传的合同」与「后台有该合同号」的交集**。
后台是多年全量历史台账，合同管理是某市场人员近期有效合同子集，交集很小是正常的。

要扩大覆盖、让更多历史评级也归属到正确市场人员，需要：
1. **更多市场人员上传自己的合同管理**（每人只传自己的，隔离且覆盖自己的客户）；
2. 或上传**承揽立项 / 项目作业**作为兜底（客户名 + 时间窗口补全无合同号的早期评级）。

> ⚠️ 兜底（客户名匹配）是近似机制，存在同名/近似名误匹配风险，
> 仅当无合同号时启用；必要时需用户在前端确认归属。

---

## 7. 生产部署提示

- **HTTPS**：小程序要求请求走 HTTPS，反向代理（nginx）终止 TLS。
- **会话**：`token == openid` 仅为骨架；生产应下发随机 session token（HttpOnly + SameSite），
  服务端用 Redis/DB 存 token→openid 映射。
- **DB 备份**：`final_ratings` / `contract_uploads` 为用户数据，定期备份 `rating.db`。
- **权限**：`admin` 角色通过 `users.role` 控制；换管理员在 DB 里置位即可。
- **大文件**：后台 .xls 46k 行解析约数十秒，建议异步任务（Celery/线程）避免阻塞请求。
- **有效期**：`VALIDITY` 可按项目类型（初评/更新/跟踪）细化，当前统一 12 月。
