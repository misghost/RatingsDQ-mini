const { post } = require('../../utils/request');

Page({
  data: {
    loading: false,
    isAdmin: false,
    errMsg: '',
    showBind: false,
    showBindHint: false,
    bindPhone: '',
    binding: false
  },

  onLoad() {
    wx.hideTabBar(); // 登录页不显示底部菜单
  },

  onShow() {
    // 每次显示登录页时确保隐藏 tabBar（防止从其他页跳转回来时 tabBar 残留）
    wx.hideTabBar();
  },

  toggleRole(e) {
    this.setData({ isAdmin: e.detail.value });
  },

  doLogin() {
    if (this.data.loading) return;
    this.setData({ loading: true, errMsg: '', showBindHint: false });
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
            wx.showTabBar(); // 确保 tabBar 显示
            wx.reLaunch({ url: '/pages/index/index' });
          })
          .catch((err) => {
            console.error(err);
            const msg = (err && err.errMsg && /request:fail/.test(err.errMsg))
              ? '网络连接失败，请检查网络后重试'
              : ((err && err.error) || '登录服务暂时不可用，请稍后重试');
            const code = (err && err.code) || '';
            // 关键：NOT_REGISTERED 时展示绑定提示
            const showHint = code === 'NOT_REGISTERED';
            this.setData({
              errMsg: msg,
              loading: false,
              showBindHint: showHint,
              _lastCode: res.code // 缓存 code 用于后续绑定
            });
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
    wx.navigateBack({ fail: () => { wx.switchTab({ url: '/pages/index/index' }); } });
  },

  goRegister() {
    wx.navigateTo({ url: '/pages/register/register' });
  },

  /* ---- 绑定已有账号 ---- */
  openBind() {
    this.setData({ showBind: true, errMsg: '', bindPhone: '' });
  },

  closeBind() {
    this.setData({ showBind: false });
  },

  onBindPhone(e) {
    this.setData({ bindPhone: e.detail.value });
  },

  doBind() {
    const phone = this.data.bindPhone.replace(/\s/g, '');
    if (!phone) {
      wx.showToast({ title: '请输入手机号', icon: 'none' });
      return;
    }
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      wx.showToast({ title: '手机号格式不正确', icon: 'none' });
      return;
    }
    const lastCode = this.data._lastCode;
    if (!lastCode) {
      // 没有缓存的 code，重新获取
      this.setData({ binding: true });
      wx.login({
        success: (res) => {
          if (!res.code) {
            this.setData({ binding: false });
            wx.showToast({ title: '获取凭证失败', icon: 'none' });
            return;
          }
          this.setData({ _lastCode: res.code });
          this._callBind(res.code, phone);
        },
        fail: () => {
          this.setData({ binding: false });
          wx.showToast({ title: '微信接口调用失败', icon: 'none' });
        }
      });
      return;
    }
    this._callBind(lastCode, phone);
  },

  _callBind(code, phone) {
    this.setData({ binding: true });
    post('/api/bind-account', { phone, code })
      .then((d) => {
        if (d.bound) {
          // 绑定成功 → 检查状态
          if (d.status === 'approved') {
            wx.setStorageSync('openid', d.openid);
            wx.setStorageSync('role', d.role || 'user');
            getApp().globalData.openid = d.openid;
            getApp().globalData.role = d.role || 'user';
            wx.showToast({ title: '关联成功', icon: 'success' });
            setTimeout(() => {
              wx.showTabBar();
              wx.reLaunch({ url: '/pages/index/index' });
            }, 1200);
          } else {
            wx.showModal({
              title: '关联成功',
              content: `账号已关联微信，但状态为「${d.status === 'pending' ? '待审核' : d.status}」，请联系管理员审核。`,
              showCancel: false
            });
          }
        } else {
          wx.showToast({ title: d.message || '关联失败', icon: 'none' });
        }
        this.setData({ binding: false });
      })
      .catch((err) => {
        const msg = (err && err.error) || '关联失败，请稍后重试';
        wx.showToast({ title: msg, icon: 'none', duration: 3000 });
        this.setData({ binding: false });
      });
  }
});
