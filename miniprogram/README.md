# 评级到期提醒 · 微信小程序前端

原生微信小程序工程，对接同仓库的 Flask 后端（`../server.py`）。
功能：微信登录 → 查看「我的到期提醒」→ 上传自己的 Excel（合同/承揽/作业）→ 管理员总览。

## 目录结构

```
miniprogram/
├── app.js / app.json / app.wxss      全局配置
├── project.config.json               AppID 已填 wxf0377d6984b59454
├── sitemap.json
├── utils/
│   ├── config.js                     ⚠️ 改这里：填后端 API 基地址
│   ├── request.js                    wx.request / wx.uploadFile 封装（带 X-Openid）
│   └── util.js                       状态配色、日期、剩余天数
└── pages/
    ├── login/      登录（微信授权，可选管理员）
    ├── index/      我的到期提醒（三态筛选、下拉刷新）
    ├── upload/     上传数据（合同/承揽/作业/后台源）
    └── admin/      管理员总览（按人下钻）
```

## 第一步：填后端地址（必做）

打开 `utils/config.js`，把 `API_BASE` 改成你的后端域名：

- **本地调试**：后端 `python server.py` 跑在本机 `:5001`，
  填你电脑局域网 IP，如 `'http://192.168.1.10:5001'`。
  开发者工具里勾选「不校验合法域名」即可（project.config.json 已默认 `urlCheck:false`）。
- **生产发布**：填「微信云托管」给你的服务域名
  （形如 `https://xxxx.ap-shanghai.app.tcloudbase.com`），
  并到 **mp.weixin.qq.com → 开发管理 → 开发设置 → 服务器域名**，
  把该 https 域名加入 `request 合法域名` 和 `uploadFile 合法域名`。

> ⚠️ 后端必须配 `WX_APPID` / `WX_SECRET` 环境变量（云托管控制台）：
> 否则 `wx.login` 的 code 换不到稳定 openid，每次登录身份都变、数据不连续。

## 第二步：用微信开发者工具导入并预览

1. 下载安装「微信开发者工具」。
2. 打开 → 导入项目 → 目录选本 `miniprogram/` 文件夹。
3. AppID 选「测试号」或你自己的 `wxf0377d6984b59454`。
4. 点「编译」，模拟器里即可看到登录页 → 点「微信登录」。

## 第三步：上传到版本管理（让版本管理不再为空）

1. 开发者工具左上角点 **「上传」**（不是「预览」）。
2. 填版本号（如 `1.0.0`）和项目备注 → 确定。
3. 打开 **mp.weixin.qq.com → 管理 → 版本管理**，
   就能看到刚上传的「开发版本」了 ✅。
4. 测试无误后 → 点「提交审核」→ 审核通过 →「发布」上线。

## 说明 / 已知限制

- 管理员登录：登录页打开「以管理员身份登录」开关即可（骨架阶段角色由前端声明，
  生产建议加管理员口令或 `ADMIN_OPENIDS` 白名单，见 server.py 注释）。
- 上传 Excel 走 `wx.chooseMessageFile`：先把文件发到「文件传输助手」再在此选择。
- 合同文件含多位市场人员时，后端返回 `multiple_marketers`，前端会让你选「我是谁」再传。
- tabBar 第三格「管理总览」对非管理员显示「无权限」。
