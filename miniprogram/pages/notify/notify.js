const { get, post } = require('../../utils/request');

Page({
  data: {
    loggedIn: false,
    channels: { miniprogram: false, email: false },
    email: '',
    wx_subscribed: 0,
    loading: false,
    msg: '',
    canMiniprogram: true
  },

  onShow() {
    const openid = wx.getStorageSync('openid');
    this.setData({ loggedIn: !!openid });
    if (openid) this.load();
  },

  load() {
    get('/api/my/notification')
      .then((d) => {
        const ch = d.channels || [];
        this.setData({
          channels: {
            miniprogram: ch.indexOf('miniprogram') >= 0,
            email: ch.indexOf('email') >= 0
          },
          email: d.email || '',
          wx_subscribed: d.wx_subscribed || 0
        });
      })
      .catch(() => {});
    get('/api/config')
      .then((cfg) => {
        this.setData({ canMiniprogram: !!(cfg.channels && cfg.channels.miniprogram) });
      })
      .catch(() => {});
  },

  goLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  toggle(e) {
    const f = e.currentTarget.dataset.f;
    this.setData({ ['channels.' + f]: !this.data.channels[f] });
  },

  onEmail(e) {
    this.setData({ email: e.detail.value });
  },

  save() {
    const ch = [];
    if (this.data.channels.miniprogram) ch.push('miniprogram');
    if (this.data.channels.email) ch.push('email');
    if (ch.indexOf('email') >= 0 && !this.data.email) {
      this.setData({ msg: '启用邮件提醒需填写邮箱地址' });
      return;
    }
    this.setData({ loading: true, msg: '' });
    post('/api/my/notification', { channels: ch, email: this.data.email })
      .then(() => { this.setData({ loading: false, msg: '设置已保存' }); })
      .catch((err) => {
        this.setData({ loading: false, msg: (err && err.error) || '保存失败' });
      });
  },

  subscribe() {
    get('/api/config')
      .then((cfg) => {
        const tmpl = (cfg.wx_template_id || '').trim();
        if (!tmpl) {
          wx.showToast({ title: '未配置订阅模板', icon: 'none' });
          return;
        }
        wx.requestSubscribeMessage({
          tmplIds: [tmpl],
          success: (res) => {
            const ok = res[tmpl] === 'accept';
            post('/api/my/notification/subscribe', { subscribed: ok })
              .then(() => {
                this.setData({ wx_subscribed: ok ? 1 : 0 });
                wx.showToast({ title: ok ? '已订阅服务提醒' : '未授权订阅', icon: 'none' });
              });
          },
          fail: () => { wx.showToast({ title: '订阅已取消', icon: 'none' }); }
        });
      });
  },

  test() {
    post('/api/my/notification/test', {})
      .then((d) => {
        console.log('notify test', d);
        const fails = (d.results || []).filter((r) => !r.ok);
        this.setData({ msg: fails.length ? '部分渠道未发送成功，请检查配置' : '已触发测试提醒' });
      })
      .catch((err) => {
        this.setData({ msg: (err && err.error) || '测试失败' });
      });
  }
});
