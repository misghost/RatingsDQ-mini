const { post } = require('../../utils/request');

Page({
  data: {
    oldPw: '',
    newPw: '',
    confirmPw: '',
    loading: false,
    errMsg: '',
    okMsg: ''
  },

  onLoad() {
    wx.hideTabBar();
  },

  onInput(e) {
    const f = e.currentTarget.dataset.f;
    this.setData({ [f]: e.detail.value });
  },

  submit() {
    const { oldPw, newPw, confirmPw } = this.data;
    if (!oldPw) {
      this.setData({ errMsg: '请输入原密码', okMsg: '' });
      return;
    }
    if (!newPw || newPw.length < 6) {
      this.setData({ errMsg: '新密码至少 6 位', okMsg: '' });
      return;
    }
    if (!/[a-zA-Z]/.test(newPw) || !/\d/.test(newPw)) {
      this.setData({ errMsg: '新密码至少需包含字母与数字两类', okMsg: '' });
      return;
    }
    if (newPw !== confirmPw) {
      this.setData({ errMsg: '两次输入的新密码不一致', okMsg: '' });
      return;
    }
    this.setData({ loading: true, errMsg: '', okMsg: '' });
    post('/api/change-password', { old_password: oldPw, new_password: newPw })
      .then((d) => {
        this.setData({ loading: false, okMsg: d.message || '密码修改成功', errMsg: '' });
        this.setData({ oldPw: '', newPw: '', confirmPw: '' });
        setTimeout(() => { wx.navigateBack(); }, 1200);
      })
      .catch((err) => {
        this.setData({ loading: false, errMsg: (err && err.error) || '修改失败，请重试', okMsg: '' });
      });
  },

  goBack() {
    wx.navigateBack();
  }
});
