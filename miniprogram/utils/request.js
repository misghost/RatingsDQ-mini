const { API_BASE } = require('./config');
const OPENID_KEY = 'openid';

// 取出本地存储的 openid
function getOpenid() {
  return wx.getStorageSync(OPENID_KEY) || '';
}

// 把 openid 附加到 URL 查询参数。
// 注意：生产 nginx 会剥离自定义头 X-Openid（proxy_set_header X-Openid ""），
// 因此统一改用查询参数 ?openid= 传递身份，与网页端保持一致。
function withOpenid(path) {
  const openid = getOpenid();
  if (!openid) return path;
  if (path.indexOf('openid=') >= 0) return path; // 已带 openid 则不重复追加
  const sep = path.indexOf('?') >= 0 ? '&' : '?';
  return path + sep + 'openid=' + encodeURIComponent(openid);
}

// 普通 JSON 请求
function request(path, method, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: API_BASE + withOpenid(path),
      method: method || 'GET',
      data: data,
      header: { 'Content-Type': 'application/json' },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
        else reject(res.data || { error: 'HTTP ' + res.statusCode });
      },
      fail: (err) => reject(err)
    });
  });
}

// 文件上传（Excel），用于各 upload 接口
function upload(path, filePath, formData) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: API_BASE + withOpenid(path),
      filePath: filePath,
      name: 'file',
      formData: formData || {},
      success: (res) => {
        try { resolve(JSON.parse(res.data)); }
        catch (e) { resolve(res.data); }
      },
      fail: (err) => reject(err)
    });
  });
}

module.exports = {
  get: (p, d) => request(p, 'GET', d),
  post: (p, d) => request(p, 'POST', d),
  upload: upload
};
