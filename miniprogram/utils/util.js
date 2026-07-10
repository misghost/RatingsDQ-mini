// 状态中文 + 配色（逾期红 / 即将橙 / 有效绿）
const STATUS_META = {
  overdue:  { label: '已过期',   color: '#e54d42', bg: '#fdeceb' },
  due:      { label: '即将到期', color: '#ff976a', bg: '#fff3e9' },
  upcoming: { label: '有效期内', color: '#07c160', bg: '#e8f8ef' }
};

function statusMeta(s) {
  return STATUS_META[s] || { label: s, color: '#9aa0a6', bg: '#f0f1f2' };
}

// '2026-08-01' -> '2026-08-01'；null -> '—'
function fmtDate(s) {
  return s ? String(s).slice(0, 10) : '—';
}

// 距离到期日还有多少天（负数=已超期）
function daysLeft(expiry) {
  if (!expiry) return null;
  const e = new Date(String(expiry).replace(/-/g, '/')).getTime();
  if (isNaN(e)) return null;
  return Math.round((e - Date.now()) / 86400000);
}

module.exports = { STATUS_META, statusMeta, fmtDate, daysLeft };
