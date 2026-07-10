const { get } = require('./utils/request');

App({
  globalData: {
    openid: '',
    role: ''
  },
  onLaunch() {
    const openid = wx.getStorageSync('openid');
    const role = wx.getStorageSync('role');
    if (openid) {
      this.globalData.openid = openid;
      this.globalData.role = role;
    }
  },
  // 拉取未读消息数并刷新 tabBar 红点（消息中心为第 5 个 tab，index=4）
  refreshUnread() {
    const openid = wx.getStorageSync('openid');
    if (!openid) { try { wx.removeTabBarBadge({ index: 4 }); } catch (e) {} return; }
    get('/api/my/messages/unread')
      .then((d) => {
        const n = d.unread || 0;
        if (n > 0) wx.setTabBarBadge({ index: 4, text: n > 99 ? '99+' : String(n) });
        else wx.removeTabBarBadge({ index: 4 });
      })
      .catch(() => {});
  }
});
