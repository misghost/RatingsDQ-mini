# 微信云托管部署指南（Flask 版）

本目录即为云托管服务的**代码根目录**，含 `Dockerfile`，可直接「代码部署」自动构建。

---

## 一、部署架构

```
微信小程序 (wx.request)
        │  HTTPS  (云托管默认域名 / 私有链路)
        ▼
微信云托管 (容器: Flask + gunicorn, 监听 $PORT)
        │
        ├── 内存/请求中：X-Openid 标识用户
        └── 持久化：/data/rating.db  (SQLite, 挂在 CFS 文件存储卷上)
```

**为什么用挂载卷**：云托管容器是无状态、可随时重建的。SQLite 是单文件数据库，
必须放在「文件存储(CFS)挂载卷」上才能持久化、且多实例共享同一份数据。
没挂载则每次部署/重启数据全丢。

---

## 二、控制台配置步骤

在微信云托管控制台，进入你的服务后：

### 1. 端口
- 容器监听端口 = `$PORT`（代码里用 `os.environ.get("PORT", "80")`）
- gunicorn 已绑定 `${PORT:-80}`，云托管会自动注入 `PORT`，**无需手动设**。

### 2. 环境变量（服务设置 → 环境变量）
| 变量 | 值 | 说明 |
|---|---|---|
| `RATING_DB` | `/data/rating.db` | SQLite 文件路径，**必须指向挂载卷** |
| `VALIDITY` | `12` | 评级有效期（月） |
| `REMIND` | `3` | 提前提醒窗口（月） |
| `OVERDUE_WINDOW` | `12` | 过期噪音窗口（月，过滤陈年过期） |
| `WX_APPID` | `wx....` | 小程序 AppID（真实微信登录用，可选） |
| `WX_SECRET` | `....` | 小程序 Secret（可选） |

> 不设 `WX_APPID/WX_SECRET` 时，`/api/login` 退化为 mock（把 code 当 openid），
> 便于先用小程序联调；正式上线务必配上，走真实 `code2Session`。

### 3. 存储挂载（关键！否则数据丢失）
- 服务设置 → 存储 → 新建/选择「文件存储」
- 挂载路径填 `/data`（与 `RATING_DB=/data/rating.db` 对应）
- 容器内的 `/data` 即为持久化的网络文件系统

### 4. 实例与扩缩
- **先用单实例**（实例数=1）。SQLite 写并发有限，单 worker（`-w 1`）最稳。
- 需要高可用/更高并发时，**强烈建议把 SQLite 换成云数据库(MySQL/Postgres)**，
  见末尾「生产进阶」。

---

## 三、上传代码（任选其一）

### 方式 A：微信开发者工具（最省事）
1. 打开项目 → 顶部「云开发」→ 云托管
2. 选择服务 → 「上传并部署：直接部署」（选本目录 `rating-engine`）
3. 等待镜像构建完成

### 方式 B：连接 Git 仓库（推荐，可持续集成）
1. 把本目录推到 GitHub/GitLab/工蜂
2. 云托管服务 → 设置 → 代码源 → 关联仓库与分支
3. 之后每次 push 自动构建部署

### 方式 C：命令行（CloudBase CLI）
```bash
npm i -g @cloudbase/cli
tcb login
cd rating-engine
tcb framework deploy   # 或按云托管文档用 container 部署
```

---

## 四、验证部署

部署完成后，用服务的「访问路径/默认域名」测试：

```bash
# 健康检查
curl https://<你的服务域名>/api/health
# -> {"ok": true, "admin_records": 0}

# 管理员登录（mock 模式）
curl -X POST https://<域名>/api/login -H "Content-Type: application/json" \
  -d '{"code":"admin","role":"admin"}'
```

返回 `{"openid":"admin",...}` 即成功。随后即可在管理端上传后台 xls、在微信小程序上传合同。

---

## 五、小程序前端接入要点

```js
// app.js 登录
wx.login({
  success: (res) => {
    wx.request({
      url: 'https://<服务域名>/api/login',
      method: 'POST',
      data: { code: res.code, role: 'user' },
      success: (r) => {
        wx.setStorageSync('openid', r.data.openid) // 保存，后续请求带在 header
      }
    })
  }
})

// 查我的到期提醒
wx.request({
  url: 'https://<服务域名>/api/my/ratings',
  header: { 'X-Openid': wx.getStorageSync('openid') },
  success: (r) => { /* r.data.ratings */ }
})

// 上传我的合同管理.xlsx
wx.chooseMessageFile({ count: 1, success: (sel) => {
  const fd = new FormData()
  fd.append('file', sel.tempFiles[0])
  wx.request({ url: 'https://<域名>/api/upload/contract',
    method: 'POST', header: { 'X-Openid': openid }, data: fd })
}})
```

> 微信云托管支持「微信私有链路」调用，小程序内无需 HTTPS 备案域名也可访问，
> 具体以云托管控制台「调用信息」为准。

---

## 六、API 清单（后端已就绪）

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/api/login` | 微信登录（真实/ mock） | 公开 |
| POST | `/api/admin/source` | 管理员上传后台全量 xls | admin |
| POST | `/api/upload/contract` | 市场人员上传合同管理（合同号 join） | 登录 |
| POST | `/api/upload/fallback` | 兜底上传（承揽/作业） | 登录 |
| POST | `/api/compute` | 触发归属+到期计算 | admin |
| GET | `/api/my/ratings` | 我的到期提醒 | 登录 |
| GET | `/api/admin/overview` | 管理员总览 | admin |
| GET | `/api/admin/marketer` | 某市场人员下钻 | admin |
| GET | `/api/health` | 健康检查 | 公开 |

---

## 七、生产进阶（重要）

### SQLite → 云数据库
当前为最小改动方案（CFS 挂载 + 单实例 + WAL）。若需要：
- 多实例同时写
- 大并发
- 后台 4.6 万行台账频繁重算

请把 `db.py` 的 SQLite 替换为云托管 MySQL / Postgres（云托管可一键开通数据库，
与容器同 VPC 内网互通）。`db.py` 已把所有 SQL 收敛在一处，替换成本低。

### 安全
- 当前 token == openid（mock），生产应签发随机 session token 或微信 `session_key` 派生。
- CORS 当前宽松（`CORS(app)`），生产可收紧到小程序域名。
- 管理员接口仅按 `role=admin` 判定，正式环境建议加独立管理员密钥。

### 已知数据现象
- 合同号 join 只覆盖「前端上传合同 ∩ 后台该合同号」的交集；要让更多历史评级归属到位，
  需更多市场人员上传自己的合同管理，或传承揽/作业做兜底。
- 黄天祺/邓少平样本合同少且出具时间早，会被过期噪音窗口过滤成 0 条（真实稀疏，非 bug）。

### 常见故障：容器启动即崩溃 `sqlite3.OperationalError: unable to open database file`
- **原因**：`RATING_DB=/data/rating.db` 但 `/data` 目录不存在（CFS 未挂载或挂载路径不符）。
  SQLite 不会自动创建父目录，导致连接失败、应用启动崩溃、容器无限重启。
- **修复**：`db.py` 的 `get_conn()` 已改为连接前自动 `makedirs` 父目录，并在 `/data` 不可写时
  回退到 `/app/rating.db`。重新打包部署即可。
- **但注意**：未挂载 CFS 时，数据库落在容器临时盘，**每次重新部署/重启数据会清空**，
  管理员需重新上传后台 xls。务必在控制台挂载文件存储实例到 `/data` 以持久化。
- 若仍报 `initenv.sh exited with 137`，通常是上述崩溃引发的 CrashLoopBackOff 连锁反应；
  应用能正常启动后该钩子会自动恢复正常。

---

## 八、本目录文件
- `server.py` — Flask 服务（生产形态：debug off、真实微信登录、demo 默认关）
- `db.py` — SQLite 存储层（动态路径 + WAL）
- `rating_engine.py` — 零依赖规则引擎（读 xlsx/识别/校验/去重/到期）
- `admin_source.py` — 后台 xls 解析 + 合同号归属算法
- `preview.html` / `offline_preview.html` — 预览页
- `Dockerfile` / `requirements.txt` / `.dockerignore` — 容器化部署
- `RULES.md` — 完整规则设计文档
