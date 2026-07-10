// 小程序代码上传脚本（依赖 miniprogram-ci）
// 用法:
//   NODE_PATH=/Users/lion/.workbuddy/binaries/node/workspace/node_modules \
//   PRIVATE_KEY=/path/to/upload.key \
//   node upload_mp.js
// 说明:
//   - PRIVATE_KEY 即微信公众平台「小程序代码上传密钥」(.key 文件)
//   - 默认 appid 取自 project.config.json
const path = require('path');
const ci = require('miniprogram-ci');

const ROOT = path.resolve(__dirname, '..');
const MINI = path.join(ROOT, 'miniprogram');
const privateKey = process.env.PRIVATE_KEY || path.join(__dirname, 'private.key');
const version = process.env.MP_VERSION || '1.0.0';
const desc = process.env.MP_DESC || '评级到期提醒小程序：消息中心+预警阈值+统一视觉，API 指向 df.ratings.ink';

const project = new ci.Project({
  appid: require(path.join(MINI, 'project.config.json')).appid,
  type: 'miniProgram',
  projectPath: MINI,
  privateKeyPath: privateKey,
  ignores: ['node_modules/**/*', '.**/*']
});

console.log('appid        :', project.appid);
console.log('projectPath  :', MINI);
console.log('privateKey   :', privateKey);
console.log('version/desc :', version, '/', desc);

ci.upload({
  project,
  version,
  desc,
  robot: process.env.MP_ROBOT ? Number(process.env.MP_ROBOT) : 1,
  onProgressUpdate: (e) => {
    const p = e && e.status ? `[${e.status}] ${e.message || ''}` : JSON.stringify(e);
    process.stdout.write(p + '\n');
  }
})
  .then((res) => {
    console.log('✅ 上传成功:', JSON.stringify(res));
    process.exit(0);
  })
  .catch((err) => {
    console.error('❌ 上传失败:', err && err.message ? err.message : err);
    process.exit(1);
  });
