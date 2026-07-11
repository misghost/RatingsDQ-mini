const { get, post } = require('../../utils/request');

Page({
  data: {
    role: 'user',
    loggedIn: false,
    overview: null,
    marketers: [],
    loading: false,
    marketer: null,
    marketerRatings: [],
    pending: [],
    pendingCount: 0
  },

  onShow() {
    const openid = wx.getStorageSync('openid');
    const role = wx.getStorageSync('role') || 'user';
    if (!openid) {
      wx.hideTabBar();
      this.setData({ role, loggedIn: false });
      return;
    }
    wx.showTabBar();
    this.setData({ role, loggedIn: true });
    if (role !== 'admin') return;   // 非管理员：显示无权限
    this.loadOverview();
    this.loadReview();
  },

  goLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  loadReview() {
    get('/api/admin/users?status=pending')
      .then((d) => {
        this.setData({ pending: d.users || [], pendingCount: d.pending_count || 0 });
      })
      .catch(() => {});
  },

  approve(e) {
    this.review(e.currentTarget.dataset.oid, 'approve');
  },

  reject(e) {
    const oid = e.currentTarget.dataset.oid;
    wx.showModal({
      title: '拒绝该注册',
      editable: true,
      placeholderText: '可填写拒绝原因（选填）',
      success: (res) => {
        if (res.confirm) this.review(oid, 'reject', res.content || '');
      }
    });
  },

  review(oid, action, reason) {
    post('/api/admin/users/review', { openid: oid, action: action, reason: reason })
      .then(() => {
        wx.showToast({ title: action === 'approve' ? '已通过' : '已拒绝', icon: 'success' });
        this.loadReview();
        this.loadOverview();
      })
      .catch((err) => {
        wx.showToast({ title: (err && err.error) || '操作失败', icon: 'none' });
      });
  },

  loadOverview() {
    this.setData({ loading: true });
    get('/api/admin/overview')
      .then((d) => {
        const marketers = Object.keys(d.by_marketer || {}).map((oid) => {
          const m = d.by_marketer[oid];
          return {
            openid: oid,
            name: m.name || '(未命名)',
            total: m.total,
            by_status: m.by_status || {}
          };
        });
        this.setData({
          overview: d,
          marketers: marketers,
          loading: false,
          marketer: null,
          marketerRatings: []
        });
      })
      .catch((err) => {
        console.error(err);
        if (err && typeof err.error === 'string' && /login first|unknown user/.test(err.error)) {
          wx.removeStorageSync('openid');
          wx.removeStorageSync('role');
          wx.reLaunch({ url: '/pages/login/login' });
          return;
        }
        wx.showToast({ title: '加载失败', icon: 'none' });
        this.setData({ loading: false });
      });
  },

  openMarketer(e) {
    const oid = e.currentTarget.dataset.oid;
    get('/api/admin/marketer?openid=' + encodeURIComponent(oid))
      .then((d) => {
        const name = (this.data.marketers.find((m) => m.openid === oid) || {}).name || '';
        this.setData({ marketer: name, marketerRatings: d.ratings || [] });
      });
  },

  back() {
    this.setData({ marketer: null, marketerRatings: [] });
  }
});
