const { get, post } = require('../../utils/request');
const { statusMeta, fmtDate, daysLeft } = require('../../utils/util');

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'overdue', label: '已过期' },
  { key: 'due', label: '即将到期' },
  { key: 'upcoming', label: '有效期内' }
];
const SORTS = [
  { key: 'status', label: '按状态' },
  { key: 'expiry_asc', label: '最早到期' },
  { key: 'expiry_desc', label: '最晚到期' },
  { key: 'subject', label: '按客户' }
];

const DEMO_RATINGS = [
  { subject: 'XX有限公司2024年度主体信用评级', contract_no: 'HT2024-001', expiry_date: '2025-03-15', remind_date: '2024-12-15', status: 'overdue', debt_type: '企业债', project_type: '信用评级', attribution: '市场人员A' },
  { subject: 'YY集团2025年中期票据评级', contract_no: 'HT2025-042', expiry_date: '2025-09-20', remind_date: '2025-06-20', status: 'due', debt_type: '中期票据', project_type: '跟踪评级', attribution: '市场人员B' },
  { subject: 'ZZ股份公司债券评级', contract_no: 'HT2024-088', expiry_date: '2026-01-10', remind_date: '2025-10-10', status: 'upcoming', debt_type: '公司债', project_type: '初次评级', attribution: '市场人员C' },
  { subject: 'AA实业2025年度跟踪评级', contract_no: 'HT2025-103', expiry_date: '2025-11-30', remind_date: '2025-08-30', status: 'upcoming', debt_type: '企业债', project_type: '跟踪评级', attribution: '市场人员A' },
  { subject: 'BB控股可转债评级', contract_no: 'HT2024-156', expiry_date: '2025-02-28', remind_date: '2024-11-28', status: 'overdue', debt_type: '可转债', project_type: '信用评级', attribution: '市场人员B' }
];

Page({
  data: {
    loggedIn: false,
    ready: false,
    filters: FILTERS,
    sorts: SORTS,
    active: 'all',
    sort: 'status',
    search: '',
    summary: { overdue: 0, due: 0, upcoming: 0, total: 0 },
    allRatings: [],
    list: [],
    loading: false
  },

  onShow() {
    const openid = wx.getStorageSync('openid');
    if (!openid) {
      wx.hideTabBar();
      this.setData({ loggedIn: false });
      this.loadDemo();
      return;
    }
    wx.showTabBar();
    this.setData({ loggedIn: true });
    if (openid) {
      getApp().refreshUnread();
      this.load();
    } else {
      this.loadDemo();
    }
  },

  onPullDownRefresh() {
    if (this.data.loggedIn) { this.load(() => wx.stopPullDownRefresh()); }
    else { this.loadDemo(); wx.stopPullDownRefresh(); }
  },

  goToLogin() { wx.navigateTo({ url: '/pages/login/login' }); },
  goMessages() { wx.switchTab({ url: '/pages/messages/messages' }); },

  switchFilter(e) { this.setData({ active: e.currentTarget.dataset.key }); this.applyFilter(); },
  setSort(e) { this.setData({ sort: e.currentTarget.dataset.key }); if (this.data.loggedIn) this.load(); else this.applyFilter(); },
  onSearch(e) { this.setData({ search: e.detail.value }); if (this.data.loggedIn) this.load(); else this.applyFilter(); },

  applyFilter() {
    const f = this.active = this.data.active;
    const s = this.data.sort;
    let all = this.data.allRatings || [];
    if (this.data.search) {
      const q = this.data.search.toLowerCase();
      all = all.filter(r => (r.subject || '').toLowerCase().includes(q) || (r.contract_no || '').toLowerCase().includes(q));
    }
    if (f !== 'all') all = all.filter(r => r.status === f);
    if (s === 'expiry_asc') all = all.slice().sort((a, b) => (a.expiry_date > b.expiry_date ? 1 : -1));
    else if (s === 'expiry_desc') all = all.slice().sort((a, b) => (a.expiry_date < b.expiry_date ? 1 : -1));
    else if (s === 'subject') all = all.slice().sort((a, b) => (a.subject > b.subject ? 1 : -1));
    this.setData({ list: all });
  },

  load(done) {
    this.setData({ loading: true });
    const q = [];
    q.push('sort=' + encodeURIComponent(this.data.sort));
    q.push('page=1'); q.push('page_size=200');
    if (this.data.active !== 'all') q.push('status=' + this.data.active);
    if (this.data.search) q.push('q=' + encodeURIComponent(this.data.search));
    get('/api/my/ratings?' + q.join('&'))
      .then((d) => {
        const all = (d.ratings || []).map((r) => {
          const meta = statusMeta(r.status);
          return Object.assign({}, r, {
            statusLabel: meta.label,
            statusColor: meta.color,
            statusBg: meta.bg,
            expiry: fmtDate(r.expiry_date),
            remind: fmtDate(r.remind_date),
            left: daysLeft(r.expiry_date),
            renewed: r.renewed && r.renewed !== 0 && r.renewed !== '0'
          });
        });
        const summary = { overdue: 0, due: 0, upcoming: 0, total: all.length };
        all.forEach((r) => { summary[r.status] = (summary[r.status] || 0) + 1; });
        this.setData({ allRatings: all, summary: summary, ready: true, loading: false });
        this.applyFilter();
      })
      .catch((err) => {
        if (err && typeof err.error === 'string' && /login first|unknown user/.test(err.error)) {
          wx.removeStorageSync('openid'); wx.removeStorageSync('role');
          this.setData({ loggedIn: false }); this.loadDemo(); return;
        }
        wx.showToast({ title: '加载失败，请检查网络', icon: 'none' });
        this.setData({ loading: false });
      })
      .then(() => { if (done) done(); });
  },

  loadDemo() {
    const all = DEMO_RATINGS.map((r) => {
      const meta = statusMeta(r.status);
      return Object.assign({}, r, {
        statusLabel: meta.label, statusColor: meta.color, statusBg: meta.bg,
        expiry: fmtDate(r.expiry_date), remind: fmtDate(r.remind_date),
        left: daysLeft(r.expiry_date), renewed: false
      });
    });
    const summary = { overdue: 0, due: 0, upcoming: 0, total: all.length };
    all.forEach((r) => { summary[r.status] = (summary[r.status] || 0) + 1; });
    this.setData({ allRatings: all, summary: summary, ready: true, loading: false, list: all });
  },

  renew(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: '标记已续期 / 已重评',
      content: '确认后该条将从到期存量中移出。',
      success: (res) => {
        if (!res.confirm) return;
        post('/api/my/ratings/' + id + '/renew', {})
          .then(() => { wx.showToast({ title: '已标记', icon: 'success' }); this.load(); })
          .catch((err) => wx.showToast({ title: (err && err.error) || '操作失败', icon: 'none' }));
      }
    });
  }
});
