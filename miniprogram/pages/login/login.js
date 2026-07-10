const { post } = require('../../utils/request');

Page({
  data: {
    loading: false,
    isAdmin: false,
    errMsg: ''
  },

  toggleRole(e) {
    this.setData({ isAdmin: e.detail.value });
  },

  doLogin() {
    if (this.data.loading) return;
    this.setData({ loading: true, errMsg: '' });
    wx.login({
      success: (res) => {
        if (!res.code) {
          wx.showToast({ title: '获取登录凭证失败，请重试', icon: 'none' });
          this.setData({ loading: false });
          return;
        }
        post('/api/login', { code: res.code, role: this.data.isAdmin ? 'admin' : 'user' })
          .then((d) => {
            wx.setStorageSync('openid', d.openid);
            wx.setStorageSync('role', d.role);
            getApp().globalData.openid = d.openid;
            getApp().globalData.role = d.role;
            wx.reLaunch({ url: '/pages/index/index' });
          })
          .catch((err) => {
            console.error(err);
            // 区分网络错误 vs 后端业务错误，给友好提示
            const msg = (err && err.errMsg && /request:fail/.test(err.errMsg))
              ? '网络连接失败，请检查网络后重试'
              : ((err && err.error) || '登录服务暂时不可用，请稍后重试');
            this.setData({ errMsg: msg, loading: false });
            wx.showToast({ title: msg, icon: 'none', duration: 3000 });
          });
      },
      fail: () => {
        this.setData({
          errMsg: '微信登录接口调用失败',
          loading: false
        });
        wx.showToast({ title: '微信登录失败，请重试', icon: 'none' });
      }
    });
  },

  skipLogin() {
    // 暂不登录，返回首页浏览示例
    wx.navigateBack({ fail: () => { wx.switchTab({ url: '/pages/index/index' }); } });
  },

  goRegister() {
    wx.navigateTo({ url: '/pages/register/register' });
  }
});
