const { get } = require('../../utils/request');
const { statusMeta, fmtDate, daysLeft } = require('../../utils/util');

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'overdue', label: '已过期' },
  { key: 'due', label: '即将到期' },
  { key: 'upcoming', label: '有效期内' }
];

// 示例数据——未登录时展示，让审核员看到核心功能
const DEMO_RATINGS = [
  {
    subject: 'XX有限公司2024年度主体信用评级',
    contract_no: 'HT2024-001',
    expiry_date: '2025-03-15',
    remind_date: '2024-12-15',
    status: 'overdue',
    debt_type: '企业债',
    project_type: '信用评级',
    attribution: '市场人员A'
  },
  {
    subject: 'YY集团2025年中期票据评级',
    contract_no: 'HT2025-042',
    expiry_date: '2025-09-20',
    remind_date: '2025-06-20',
    status: 'due',
    debt_type: '中期票据',
    project_type: '跟踪评级',
    attribution: '市场人员B'
  },
  {
    subject: 'ZZ股份公司债券评级',
    contract_no: 'HT2024-088',
    expiry_date: '2026-01-10',
    remind_date: '2025-10-10',
    status: 'upcoming',
    debt_type: '公司债',
    project_type: '初次评级',
    attribution: '市场人员C'
  },
  {
    subject: 'AA实业2025年度跟踪评级',
    contract_no: 'HT2025-103',
    expiry_date: '2025-11-30',
    remind_date: '2025-08-30',
    status: 'upcoming',
    debt_type: '企业债',
    project_type: '跟踪评级',
    attribution: '市场人员A'
  },
  {
    subject: 'BB控股有限公可转债评级',
    contract_no: 'HT2024-156',
    expiry_date: '2025-02-28',
    remind_date: '2024-11-28',
    status: 'overdue',
    debt_type: '可转债',
    project_type: '信用评级',
    attribution: '市场人员B'
  }
];

Page({
  data: {
    loggedIn: false,
    ready: false,
    filters: FILTERS,
    active: 'all',
    summary: { overdue: 0, due: 0, upcoming: 0, total: 0 },
    allRatings: [],
    list: [],
    loading: false
  },

  onShow() {
    const openid = wx.getStorageSync('openid');
    this.setData({ loggedIn: !!openid });
    if (openid) {
      this.load();
    } else {
      // 未登录：展示示例数据 + 引导登录
      this.loadDemo();
    }
  },

  onPullDownRefresh() {
    if (this.data.loggedIn) {
      this.load(() => wx.stopPullDownRefresh());
    } else {
      this.loadDemo();
      wx.stopPullDownRefresh();
    }
  },

  goToLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  switchFilter(e) {
    this.setData({ active: e.currentTarget.dataset.key });
    this.applyFilter();
  },

  applyFilter() {
    const f = this.data.active;
    const all = this.data.allRatings || [];
    this.setData({ list: f === 'all' ? all : all.filter(r => r.status === f) });
  },

  // 已登录：从后端拉真实数据
  load(done) {
    this.setData({ loading: true });
    get('/api/my/ratings')
      .then((d) => {
        const all = (d.ratings || []).map((r) => {
          const meta = statusMeta(r.status);
          return Object.assign({}, r, {
            statusLabel: meta.label,
            statusColor: meta.color,
            statusBg: meta.bg,
            expiry: fmtDate(r.expiry_date),
            remind: fmtDate(r.remind_date),
            left: daysLeft(r.expiry_date)
          });
        });
        const summary = { overdue: 0, due: 0, upcoming: 0, total: all.length };
        all.forEach((r) => { summary[r.status] = (summary[r.status] || 0) + 1; });
        this.setData({ allRatings: all, summary: summary, ready: true, loading: false });
        this.applyFilter();
      })
      .catch((err) => {
        console.error(err);
        if (err && typeof err.error === 'string' && /login first|unknown user/.test(err.error)) {
          wx.removeStorageSync('openid');
          wx.removeStorageSync('role');
          this.setData({ loggedIn: false });
          this.loadDemo();
          return;
        }
        wx.showToast({ title: '加载失败，请检查网络', icon: 'none' });
        this.setData({ loading: false });
      })
      .then(() => { if (done) done(); });
  },

  // 未登录：渲染示例数据
  loadDemo() {
    const all = DEMO_RATINGS.map((r) => {
      const meta = statusMeta(r.status);
      return Object.assign({}, r, {
        statusLabel: meta.label,
        statusColor: meta.color,
        statusBg: meta.bg,
        expiry: fmtDate(r.expiry_date),
        remind: fmtDate(r.remind_date),
        left: daysLeft(r.expiry_date)
      });
    });
    const summary = { overdue: 0, due: 0, upcoming: 0, total: all.length };
    all.forEach((r) => { summary[r.status] = (summary[r.status] || 0) + 1; });
    this.setData({
      allRatings: all, summary: summary, ready: true, loading: false,
      list: all   // 默认显示全部
    });
  }
});
