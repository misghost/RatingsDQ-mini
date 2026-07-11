const { post } = require('../../utils/request');

Page({
  data: {
    organization: '',
    name: '',
    phone: '',
    email: '',
    loading: false,
    result: null,
    errMsg: ''
  },

  onLoad() {
    wx.hideTabBar();
  },

  onShow() {
    wx.hideTabBar();
  },

  onInput(e) {
    const f = e.currentTarget.dataset.f;
    this.setData({ [f]: e.detail.value });
  },

  submit() {
    const { organization, name, phone, email } = this.data;
    if (!organization || !name || !phone) {
      this.setData({ errMsg: '请填写所属机构、姓名、手机号' });
      return;
    }
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      this.setData({ errMsg: '手机号格式不正确' });
      return;
    }
    this.setData({ loading: true, errMsg: '' });
    wx.login({
      success: (res) => {
        if (!res.code) {
          this.setData({ loading: false, errMsg: '获取登录凭证失败，请重试' });
          return;
        }
        post('/api/register', {
          platform: 'miniprogram',
          code: res.code,
          organization, name, phone, email
        })
          .then((d) => {
            this.setData({ loading: false, result: d });
          })
          .catch((err) => {
            const msg = (err && err.error) || '注册失败，请稍后重试';
            this.setData({ loading: false, errMsg: msg });
          });
      },
      fail: () => {
        this.setData({ loading: false, errMsg: '微信登录失败，请重试' });
      }
    });
  },

  goLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  }
});
