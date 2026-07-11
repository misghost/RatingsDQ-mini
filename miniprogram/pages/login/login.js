const { post } = require('../../utils/request');

Page({
  data: {
    loading: false,
    wxLoading: false,
    errMsg: '',
    phone: '',
    password: '',
    showBind: false,
    bindPhone: '',
    bindPassword: '',
    binding: false
  },

  onLoad() {
    wx.hideTabBar(); // 登录页不显示底部菜单
  },

  onShow() {
    // 每次显示登录页时确保隐藏 tabBar（防止从其他页跳转回来时 tabBar 残留）
    wx.hideTabBar();
  },

  /* ---- 主登录：手机号 + 密码 ---- */
  onPhone(e) {
    this.setData({ phone: e.detail.value });
  },

  onPassword(e) {
    this.setData({ password: e.detail.value });
  },

  doPasswordLogin() {
    if (this.data.loading) return;
    const phone = this.data.phone.replace(/\s/g, '');
    const password = this.data.password || '';
    if (!phone) {
      wx.showToast({ title: '请输入手机号', icon: 'none' });
      return;
    }
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      wx.showToast({ title: '手机号格式不正确', icon: 'none' });
      return;
    }
    if (!password) {
      wx.showToast({ title: '请输入密码', icon: 'none' });
      return;
    }
    this.setData({ loading: true, errMsg: '' });
    post('/api/web/login', { phone, password })
      .then((d) => {
        wx.setStorageSync('openid', d.openid);
        wx.setStorageSync('role', d.role);
        wx.setStorageSync('wxBound', !!d.wx_bound);
        getApp().globalData.openid = d.openid;
        getApp().globalData.role = d.role;
        getApp().globalData.wxBound = !!d.wx_bound;
        wx.showTabBar();
        wx.reLaunch({ url: '/pages/index/index' });
      })
      .catch((err) => {
        const code = (err && err.code) || '';
        const msg = (err && err.error) || '登录失败，请稍后重试';
        if (code === 'NEED_PASSWORD_SET') {
          wx.showModal({
            title: '需先设置密码',
            content: '该账号尚未设置登录密码。请先在网页后台（管理员 → 用户管理 → 重置密码）设置初始密码后，再用手机号 + 密码登录。',
            showCancel: false
          });
        } else if (code === 'NOT_REGISTERED') {
          wx.showModal({
            title: '账号未注册',
            content: '未找到该手机号对应的账号，请先注册并通过审核后再登录。',
            showCancel: false
          });
        } else if (code === 'PENDING') {
          wx.showModal({ title: '审核中', content: msg, showCancel: false });
        } else if (code === 'REJECTED') {
          wx.showModal({ title: '未通过审核', content: msg, showCancel: false });
        } else {
          this.setData({ errMsg: msg });
          wx.showToast({ title: msg, icon: 'none' });
        }
        this.setData({ loading: false });
      });
  },

  /* ---- 微信快捷登录（已关联微信可一键登录） ---- */
  doWechatLogin() {
    if (this.data.wxLoading) return;
    this.setData({ wxLoading: true, errMsg: '' });
    wx.login({
      success: (res) => {
        if (!res.code) {
          wx.showToast({ title: '获取登录凭证失败，请重试', icon: 'none' });
          this.setData({ wxLoading: false });
          return;
        }
        post('/api/login', { code: res.code, role: 'user' })
          .then((d) => {
            wx.setStorageSync('openid', d.openid);
            wx.setStorageSync('role', d.role);
            wx.setStorageSync('wxBound', !!d.wx_bound);
            getApp().globalData.openid = d.openid;
            getApp().globalData.role = d.role;
            getApp().globalData.wxBound = !!d.wx_bound;
            wx.showTabBar();
            wx.reLaunch({ url: '/pages/index/index' });
          })
          .catch((err) => {
            const code = (err && err.code) || '';
            const msg = (err && err.error) || '微信登录失败，请稍后重试';
            if (code === 'NOT_REGISTERED') {
              // 微信未关联账号 → 引导用手机号 + 密码关联
              wx.setStorageSync('_lastCode', res.code);
              this.setData({ _lastCode: res.code, showBind: true, errMsg: '' });
              wx.showToast({ title: '请先用手机号 + 密码关联微信', icon: 'none' });
            } else {
              this.setData({ errMsg: msg });
              wx.showToast({ title: msg, icon: 'none' });
            }
            this.setData({ wxLoading: false });
          });
      },
      fail: () => {
        this.setData({ wxLoading: false });
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

  /* ---- 绑定已有账号（微信未关联时） ---- */
  openBind() {
    this.setData({ showBind: true, errMsg: '', bindPhone: '', bindPassword: '' });
  },

  closeBind() {
    this.setData({ showBind: false });
  },

  onBindPhone(e) {
    this.setData({ bindPhone: e.detail.value });
  },

  onBindPassword(e) {
    this.setData({ bindPassword: e.detail.value });
  },

  doBind() {
    const phone = this.data.bindPhone.replace(/\s/g, '');
    const password = this.data.bindPassword || '';
    if (!phone) {
      wx.showToast({ title: '请输入手机号', icon: 'none' });
      return;
    }
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      wx.showToast({ title: '手机号格式不正确', icon: 'none' });
      return;
    }
    if (!password) {
      wx.showToast({ title: '请输入登录密码', icon: 'none' });
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
    const password = this.data.bindPassword || '';
    this.setData({ binding: true });
    post('/api/bind-account', { phone, code, password })
      .then((d) => {
        if (d.bound) {
          // 绑定成功 → 检查状态
          if (d.status === 'approved') {
            wx.setStorageSync('openid', d.openid);
            wx.setStorageSync('role', d.role || 'user');
            wx.setStorageSync('wxBound', true);
            getApp().globalData.openid = d.openid;
            getApp().globalData.role = d.role || 'user';
            getApp().globalData.wxBound = true;
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
        const code_ = (err && err.code) || '';
        const msg = (err && err.error) || '关联失败，请稍后重试';
        if (code_ === 'NEED_PASSWORD_SET') {
          wx.showModal({
            title: '需先设置密码',
            content: '该账号尚未设置登录密码，无法关联。请先在网页后台（管理员 → 用户管理 → 重置密码）设置初始密码，再回来用「手机号 + 密码」关联微信。',
            showCancel: false
          });
          this.setData({ binding: false });
          return;
        }
        wx.showToast({ title: msg, icon: 'none', duration: 3000 });
        this.setData({ binding: false });
      });
  }
});
