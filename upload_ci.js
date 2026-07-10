// 用官方 miniprogram-ci 把 miniprogram/ 工程上传到「版本管理 → 开发版本」
// 用法: node upload_ci.js <key文件绝对路径> [版本号] [描述]
const ci = require('/Users/lion/.workbuddy/binaries/node/workspace/node_modules/miniprogram-ci');
const fs = require('fs');

const KEY = process.argv[2];
const VERSION = process.argv[3] || '1.0.0';
const DESC = process.argv[4] || '评级到期提醒小程序 初版上传';

if (!KEY || !fs.existsSync(KEY)) {
  console.error('缺少密钥文件: 用法 node upload_ci.js <key路径>');
  process.exit(2);
}

const PROJECT_PATH = '/Users/lion/WorkBuddy/2026-07-09-23-16-22/rating-engine/miniprogram';
const APPID = 'wxf0377d6984b59454';

const project = new ci.Project({
  appid: APPID,
  type: 'miniProgram',
  projectPath: PROJECT_PATH,
  privateKeyPath: KEY,
  ignores: ['node_modules/**', 'upload_ci.js'],
});

console.log(`准备上传: appid=${APPID} version=${VERSION} -> ${PROJECT_PATH}`);

ci.upload({
  project,
  version: VERSION,
  desc: DESC,
  setting: {
    urlCheck: false,         // 关闭域名校验，方便真机调试阶段直接连后端
    es6: true,
    minified: true,
    bigPackageSizeSupport: true,
  },
  onProgressUpdate: (info) => console.log('[progress]', info),
})
  .then((res) => {
    console.log('UPLOAD_OK', JSON.stringify(res));
    process.exit(0);
  })
  .catch((err) => {
    console.error('UPLOAD_FAIL', err && err.message ? err.message : err);
    process.exit(1);
  });
