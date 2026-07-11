const { upload } = require('../../utils/request');

Page({
  data: {
    role: 'user',
    loggedIn: false,
    marketAs: '',
    marketers: [],
    pendingType: '',
    pendingPath: '',
    msg: '',
    resultText: ''
  },

  onShow() {
    const openid = wx.getStorageSync('openid');
    if (!openid) {
      wx.hideTabBar();
      this.setData({ role: 'user', loggedIn: false });
      return;
    }
    wx.showTabBar();
    this.setData({
      role: wx.getStorageSync('role') || 'user',
      loggedIn: true
    });
  },

  goLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  chooseAndUpload(e) {
    if (!this.data.loggedIn) {
      wx.showToast({ title: '请先登录', icon: 'none' });
      return;
    }
    const type = e.currentTarget.dataset.type; // contract | chenlan | zuoye | admin
    wx.chooseMessageFile({
      count: 1,
      type: 'file',
      extension: ['xlsx', 'xls'],
      success: (res) => {
        const file = res.tempFiles[0];
        this.setData({ msg: '上传中：' + file.name, resultText: '' });
        this.doUpload(type, file.path);
      },
      fail: () => {}
    });
  },

  doUpload(type, filePath) {
    let path = '';
    const formData = {};
    if (type === 'contract') {
      path = '/api/upload/contract';
      if (this.data.marketAs) formData.market_as = this.data.marketAs;
    } else if (type === 'chenlan' || type === 'zuoye') {
      path = '/api/upload/fallback';
      formData.source = type;
    } else if (type === 'admin') {
      path = '/api/admin/source';
    }

    upload(path, filePath, formData)
      .then((d) => {
        if (d && d.error === 'multiple_marketers') {
          this.setData({
            marketers: d.marketers,
            pendingType: type,
            pendingPath: filePath,
            msg: '该文件含多位市场人员，请选择“我是谁”后重新上传'
          });
          return;
        }
        let txt = '上传成功';
        if (d.bound_marketer) txt = '已绑定：' + d.bound_marketer + '，合同 ' + d.contract_count + ' 条';
        else if (d.kept !== undefined) txt = '保留 ' + d.kept + ' 条，剔除 ' + d.dropped + ' 条';
        else if (d.admin_records !== undefined) txt = '后台记录 ' + d.admin_records + ' 条，已重算 ' + (d.compute_stats ? d.compute_stats.final_count : '?') + ' 条归属';

        this.setData({ msg: '✅ 上传成功', resultText: txt, pendingType: '', pendingPath: '', marketers: [] });
        wx.showToast({ title: '上传成功', icon: 'success' });
      })
      .catch((err) => {
        console.error(err);
        this.setData({ msg: '❌ 上传失败', resultText: (err && err.error) ? String(err.error) : '网络错误' });
        wx.showToast({ title: '上传失败', icon: 'none' });
      });
  },

  onMarketPick(e) {
    const name = this.data.marketers[e.detail.value];
    this.setData({ marketAs: name, msg: '以「' + name + '」身份重新上传…' });
    this.doUpload(this.data.pendingType, this.data.pendingPath);
  }
});
