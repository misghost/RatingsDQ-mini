# 服务器版（Web 网页访问）部署说明

本目录同时包含 **微信小程序后端** 与 **网页版前端**。本文档只讲「把程序部署到一台服务器、用浏览器访问」的方案。

网页版与小程序版**共用同一套后端 `server.py`**，区别仅在于登录方式：

| | 小程序版 | 网页版 |
|---|---|---|
| 账号 | 先「注册」（机构/姓名/手机）→ 管理员审核 → `wx.login()` 登录 | 先「注册」（机构/姓名/手机）→ 管理员审核 → **手机号**登录 |
| 登录接口 | `/api/login`（需配 `WX_APPID/WX_SECRET`） | `/api/web/login`（**无需微信**） |
| 前端 | `miniprogram/` | 单文件 `webapp.html`（已由 `/` 自动托管） |
| 适用 | 手机 | 电脑/手机浏览器 |

> **登录即注册改为「先注册、后审核」**：现在不再「输入姓名直接匹配数据」，而是用户先在登录页点「注册」提交资料，由管理员在「用户审核」中通过/拒绝，审核通过后该手机号（网页）或微信（小程序）才能登录。未注册 / 待审核 / 已拒绝的登录会被后端拦截并提示对应状态。
>
> 网页版身份 = `web_` + `sha1(手机号)[:16]`；小程序身份 = 微信 openid。**不要**给网页版配 `WX_APPID/WX_SECRET`（配了也不影响网页登录，但没必要）。小程序版才需要。

---

## 〇、为什么打开网页报「网络错误，请检查后端地址」

`webapp.html` **不是普通静态页面**，它必须运行在 `server.py` 提供的同地址下（即浏览器地址栏就是后端地址，`API` 留空=同源）。
**不能**把它当孤立 HTML 双击打开、或丢进纯静态托管/预览面板单独打开——那样没有后端在同一地址，`fetch` 必然失败。

现在程序已做容错：连不上后端时，页面顶部会出现**「无法连接后端」横幅**，可直接填入后端地址（如 `http://服务器IP:5001`）并保存重试；
也可访问 `http://前端地址/?api=http://后端地址` 一次性指定。

> 正确做法：部署后直接用浏览器访问 **server.py 提供的地址**（如 `http://你的服务器:5001/`），后端同源、无需任何配置，横幅不会出现。

---

## 一、三种运行方式（任选）

### 方式 A：Python 直跑（最快，适合先验证）

```bash
cd rating-engine
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 关键环境变量（详见第三节）
export RATING_DB=/var/data/rating.db     # 数据持久化路径，务必落在持久盘
export ADMIN_PASSWORD=你的管理员口令      # 强烈建议设置
# ENABLE_DEMO=1                          # 可选：开启演示数据一键灌入

python server.py
# 默认监听 0.0.0.0:5001（改端口用 PORT=8080 python server.py）
```

浏览器打开 `http://<服务器IP>:5001` 即可。

### 方式 B：Gunicorn（生产推荐）

```bash
pip install gunicorn
export RATING_DB=/var/data/rating.db ADMIN_PASSWORD=xxxx
gunicorn -w 1 -k gthread --threads 8 -b 0.0.0.0:5001 --timeout 120 server:app
```

`-w 1` 单进程 + 多线程是配合 SQLite WAL 的稳妥配置，避免多进程写锁竞争。并发更高时建议换 MySQL/Postgres 并提升 workers。

### 方式 C：Docker（含反向代理/多实例最省心）

```bash
docker build -t rating-engine .
docker run -d --name rating \
  -p 5001:80 \
  -e RATING_DB=/data/rating.db \
  -e ADMIN_PASSWORD=xxxx \
  -v /var/data:/data \
  rating-engine
```

镜像内已用 `gunicorn` 启动并读 `PORT`（默认 80）。`-v` 挂载把数据库落到宿主机持久盘。

---

## 二、用 Nginx 反代 + HTTPS（对外正式发布）

```nginx
server {
    listen 443 ssl;
    server_name rating.your-domain.com;
    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 50m;        # 上传 Excel 需要
    }
}
```

浏览器访问 `https://rating.your-domain.com`，后端地址填写同理。

---

## 三、环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `RATING_DB` | 建议 | SQLite 路径。**务必指向持久盘**，否则容器/进程重启数据清空。默认 `./rating.db` |
| `ADMIN_PASSWORD` | **强烈建议** | 网页版管理员口令。不设则任何人都能以管理员身份进入 |
| `PORT` | 否 | 监听端口，默认 `5001` |
| `VALIDITY` | 否 | 评级有效期（月），默认 `12` |
| `REMIND` | 否 | 提醒提前窗口（月），默认 `3` |
| `OVERDUE_WINDOW` | 否 | 过期噪音过滤窗口（月），默认 `12` |
| `FALLBACK_WINDOW` | 否 | 兜底归属时间窗口（月），默认 `24` |
| `ENABLE_DEMO` | 否 | 设为 `1` 开启「演示数据一键灌入」按钮（依赖本地真实 xls，仅本地联调用） |
| `WX_APPID` / `WX_SECRET` | 否 | **网页版不用**。仅小程序真实登录 + 微信订阅消息发送需要 |
| `WX_TEMPLATE_ID` | 否 | 微信订阅消息模板 ID。配了 + `WX_APPID/WX_SECRET` 后，「小程序服务提醒」渠道才能真实下发 |
| `WX_TEMPLATE_DATA` | 否 | JSON，订阅消息字段映射，占位符 `{subject}{expiry}{count}`，如 `{"thing1":"{subject}","time2":"{expiry}","thing3":"{count}条评级待关注"}` |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | 否 | 邮件提醒 SMTP 配置。三者齐备后「邮件提醒」渠道才能真实发送 |
| `SMTP_PORT` | 否 | SMTP 端口，默认 `465`（SSL） |
| `SMTP_FROM` | 否 | 发件人，默认同 `SMTP_USER` |
| `SMTP_TLS` | 否 | 是否 SSL（`1` 用 SMTP_SSL，`0` 用 STARTTLS），默认 `1` |

---

## 四、使用流程

### 管理员（首次搭建）
1. 浏览器打开站点 → 登录页选「管理员」→ 输入姓名 + 口令
2. 「后台源数据」上传 `项目查询导出.xls`（全量评级真相）
3. 点「重新计算」→ 总览出现按市场人员的归属统计
4. （可选）点「演示数据」快速灌入内置样例

### 市场人员
1. 登录页点「注册」→ 填写所属机构、姓名、手机号（邮箱选填）→ 提交，等待管理员审核
2. 审核通过后，用**注册手机号**登录
3. 上传自己的 `合同管理.xlsx`（唯一合同号精准绑定归属）
   - 若文件含多名同事 → 系统提示「请选择你是谁」→ 填自己的姓名重传
4. 「我的提醒」看到自己归属的评级：红=已过期、橙=即将到期、绿=有效期内，含剩余天数
5. 兜底：也可上传「承揽立项 / 项目作业」xlsx，按客户名+时间窗口补充归属
6. 「提醒设置」勾选提醒方式（小程序服务提醒 / 邮件提醒），保存后由管理员触发推送

---

## 四-二、注册与审核

- 用户注册：`POST /api/register`（小程序带 `code`、网页带 `phone`），仅创建 `pending` 账号，**不会自动登录**。
- 管理员审核：管理后台「用户审核」卡片（或小程序管理页）→ 通过 / 拒绝（可填原因）。
- 审核状态在 `users.status`（`pending` / `approved` / `rejected`）。**所有非管理员登录都受此状态门禁**：未注册→`NOT_REGISTERED`、待审核→`PENDING`、已拒绝→`REJECTED`。
- 已部署的旧账号（如演示数据）迁移时统一置为 `approved`，不受影响。

## 四-三、提醒与推送

用户在「提醒设置」中可多选渠道：**小程序服务提醒（微信订阅消息）**、**邮件提醒**。

- **小程序服务提醒**：需在小程序内点击「授权小程序服务提醒」完成 `wx.requestSubscribeMessage` 授权（微信为一次性订阅），并在服务端配 `WX_TEMPLATE_ID` + `WX_APPID/WX_SECRET`。
- **邮件提醒**：需配 `SMTP_*` 环境变量，并在提醒设置里填写邮箱。
- **触发**：管理员在管理后台点「发送提醒」（或调用 `POST /api/admin/notify/send`，可 `dry_run` 预演）。系统会向每个已开启渠道、且当前有「即将到期/已过期」评级的用户推送。各渠道未配置时**优雅跳过**，不影响其他功能。
- **测试**：用户可在提醒设置里点「发送测试提醒」验证自身渠道。

> 定期推送可用系统 `cron` / systemd timer 周期调用 `POST /api/admin/notify/send` 实现「每日提醒」。

---

## 五、数据归属逻辑（回顾）

1. **合同号 join（主，精准）**：市场人员上传的合同号命中后台记录 → 100% 归该人
2. **客户名 + 时间窗口（兜底）**：无合同号时，按客户名 + 出具时间就近匹配
3. **unassigned**：两者都不中 → 仅管理员可见，便于提醒补传

---

## 六、与小程序的差异提醒

- 网页版**不依赖微信**，部署在任意服务器即可，不受小程序审核/类目限制。
- 同一后端、同一数据库：若你既想要小程序又想要网页，**建议两者用不同的 `RATING_DB` 或不同的服务器**，避免身份体系混用（小程序 openid 来自微信、网页 openid 来自姓名，两套标识不互通）。
- 小程序需要的 `WX_APPID/WX_SECRET`、`服务器域名白名单` 等，在网页版里**不需要**。

---

## 七、本轮新增能力（P0–P3 优化）

### P0 · 安全与可靠
- **强管理员口令**：`ADMIN_PASSWORD` 环境变量（生产已设为 24 位字母数字强口令）。登录管理后台必须校验，错误返回 `401`。
- **HTTPS**：nginx 监听 443（自签名证书 `/etc/ssl/rating/`），80 强制 301 跳转 443。
- **每日自动备份**：`rating-backup.timer` 每日 03:00 执行 `backup_db.sh`，副本存于 `/var/backups/rating/rating.db.YYYY-MM-DD_HHMMSS`，保留最近 30 份，写入前做完整性校验。
- **操作审计**：新增 `audit_log` 表，记录登录 / 注册 / 审核 / 上传 / 提醒发送等关键动作，管理员可在「审计日志」卡片查看（`/api/admin/audit`）。

### P1 · 自动提醒
- **定时推送**：`rating-scheduler.timer` 每日 09:00 调用 `scheduler.py once` → `dispatch_notifications()`，自动扫描所有已审核市场人员的临期/过期评级并推送。
- **站内消息中心**：无论是否配置邮件/微信，系统始终为每个命中用户创建站内消息（`messages` 表），前端右上角铃铛显示未读角标。
- **可配置预警阈值**：用户自行设置「提前 N 天」提醒（`notify_days`，默认 `[30,7]`）。

### P2 · 体验与数据
- **搜索 / 排序 / 分页**：我的提醒支持按客户名 / 合同号搜索，按状态、到期日、客户名排序，服务端分页。
- **日历热力**：未来 6 个月到期分布热力图（`/api/my/calendar`）。
- **CSV 导出**：我的清单 `/api/export/my`、全员清单 `/api/export/admin`（UTF-8-SIG，Excel 直接打开）。
- **续期闭环**：对已续期/已重评的评级点「标记已续期」，自动从到期存量中移出（`final_ratings.renewed`）。

### P3 · 小程序
- 与网页端统一视觉设计（沉稳蓝主色、白卡、状态色、健康条）。
- 新增**消息中心**页（tabBar 第 5 项，未读红点角标，支持单条/全部已读）。
- 提醒设置页新增**预警阈值** day-chip 选择，与网页端一致。

---

## 八、运维常用命令

```bash
# 数据库备份（手动）
/opt/rating-engine/backup_db.sh
ls -lt /var/backups/rating/ | head

# 手动触发一次提醒扫描
cd /opt/rating-engine && set -a && . ./envfile && set +a && /opt/rating-engine/venv/bin/python scheduler.py once

# 查看服务日志
journalctl -u rating.service -n 100 --no-pager

# 重启 Web 服务
systemctl restart rating.service

# 定时器状态
systemctl list-timers 'rating-*'
```

### 环境变量（envfile）
| 变量 | 说明 |
| --- | --- |
| `RATING_DB` | SQLite 文件路径（默认 `/var/lib/rating/rating.db`） |
| `ADMIN_PASSWORD` | 管理后台口令（**生产务必强口令**） |
| `NOTIFY_DAYS` | 默认预警阈值，逗号分隔，如 `30,7` |
| `ENABLE_DEMO` | 是否允许「灌入演示数据」（**生产建议设为 `0`**） |
| `SMTP_*` / `WX_*` | 邮件 / 微信订阅消息集成（留空则对应渠道禁用） |

