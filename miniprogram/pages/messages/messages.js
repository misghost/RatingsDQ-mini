const { get, post } = require('../../utils/request');

Page({
  data: {
    loggedIn: false,
    items: [],
    total: 0,
    unread: 0,
    loading: false
  },

  onShow() {
    const openid = wx.getStorageSync('openid');
    this.setData({ loggedIn: !!openid });
    if (openid) this.load();
    else wx.hideTabBarRedDot({ index: 4 });
  },

  load() {
    this.setData({ loading: true });
    get('/api/my/messages?page=1')
      .then((d) => {
        const items = (d.items || []).map((m) => Object.assign({}, m, {
          created_at_fmt: (m.created_at || '').replace('T', ' ').slice(0, 19)
        }));
        this.setData({
          items: items,
          total: d.total || 0,
          unread: d.unread || 0,
          loading: false
        });
        this.refreshBadge(d.unread || 0);
      })
      .catch(() => { this.setData({ loading: false }); });
  },

  refreshBadge(unread) {
    if (unread > 0) {
      wx.setTabBarBadge({ index: 4, text: unread > 99 ? '99+' : String(unread) });
    } else {
      wx.removeTabBarBadge({ index: 4 });
    }
  },

  readOne(e) {
    const id = e.currentTarget.dataset.id;
    post('/api/my/messages/read', { id })
      .then(() => this.load())
      .catch(() => {});
  },

  markAll() {
    post('/api/my/messages/read', {})
      .then(() => {
        wx.showToast({ title: '已全部标记已读', icon: 'success' });
        this.load();
      })
      .catch(() => {});
  },

  goLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  onPullDownRefresh() {
    if (this.data.loggedIn) this.load();
    wx.stopPullDownRefresh();
  }
});
