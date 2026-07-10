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
  }
});
