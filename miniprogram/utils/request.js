const { API_BASE } = require('./config');
const OPENID_KEY = 'openid';

// 把本地存储的 openid 放进 X-Openid 头（后端据此识别用户）
function authHeader(extra) {
  const openid = wx.getStorageSync(OPENID_KEY) || '';
  return Object.assign({ 'X-Openid': openid }, extra || {});
}

// 普通 JSON 请求
function request(path, method, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: API_BASE + path,
      method: method || 'GET',
      data: data,
      header: Object.assign({ 'Content-Type': 'application/json' }, authHeader()),
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
      url: API_BASE + path,
      filePath: filePath,
      name: 'file',
      header: authHeader(),
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
