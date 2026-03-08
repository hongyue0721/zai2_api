const historyPoints = [];

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.error?.message || data.message || '请求失败');
  return data;
}

function card(label, value, extra = '') {
  return `<div class="card"><div class="label">${label}</div><div class="stat-num">${value}</div><div class="hint">${extra}</div></div>`;
}

function shortKey(v) {
  return v.length > 20 ? `${v.slice(0, 10)}...${v.slice(-6)}` : v;
}

function healthText(rate) {
  if (rate >= 95) return '状态超稳';
  if (rate >= 80) return '状态良好';
  if (rate >= 60) return '有点波动';
  return '需要关注';
}

function drawTrend() {
  const canvas = document.getElementById('trend-canvas');
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = 'rgba(111,124,151,.18)';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    const y = (height / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  if (historyPoints.length < 2) return;
  const maxVal = Math.max(...historyPoints.map(v => Math.max(v.requests, v.success)), 1);
  const drawLine = (color, selector) => {
    ctx.beginPath();
    ctx.lineWidth = 3;
    ctx.strokeStyle = color;
    historyPoints.forEach((point, index) => {
      const x = (width / Math.max(historyPoints.length - 1, 1)) * index;
      const y = height - (selector(point) / maxVal) * (height - 18) - 9;
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  drawLine('#73b7ff', p => p.requests);
  drawLine('#ff7aa2', p => p.success);
}

function pushHistory(data) {
  historyPoints.push({ time: Date.now(), requests: data.total_requests, success: data.total_success });
  if (historyPoints.length > 20) historyPoints.shift();
  drawTrend();
}

async function loadDashboard() {
  const data = await api('/admin/api/dashboard');
  document.getElementById('stats').innerHTML = [
    card('有效账号', data.valid_accounts, `目标 ${data.target_pool_size}`),
    card('活跃请求', data.active_requests, `当前池 ${data.current_pool_size}`),
    card('总调用', data.total_requests, '所有 free 账号累计'),
    card('总成功', data.total_success, `成功率 ${data.success_rate}%`),
    card('总失败', data.total_failures, `Key ${data.api_key_count} 个`),
  ].join('');

  const health = Math.max(0, Math.min(100, data.success_rate));
  document.getElementById('health-fill').style.width = `${health}%`;
  document.getElementById('health-text').textContent = `${healthText(health)} · ${health}%`;
  document.getElementById('health-meta').textContent = `当前 ${data.active_requests} 个活跃请求，${data.valid_accounts}/${data.current_pool_size} 个账号可用。`;

  document.getElementById('accounts').innerHTML = data.accounts.map(acc => `
    <tr>
      <td><div><strong>${acc.username || 'Guest'}</strong></div><div class="hint">${acc.user_id}</div></td>
      <td><span class="pill ${acc.valid ? 'ok' : 'bad'}">${acc.valid ? '正常' : '失效'}</span> <span class="pill busy">并发 ${acc.active}</span></td>
      <td>${acc.request_count}</td>
      <td>${acc.success_count}</td>
      <td>${acc.failure_count}</td>
      <td><span class="pill ${acc.success_rate >= 80 ? 'ok' : acc.success_rate >= 50 ? 'warn' : 'bad'}">${acc.success_rate}%</span></td>
      <td><div>${acc.last_success_at || '暂无'}</div><div class="hint">${acc.last_error || '无错误'}</div></td>
      <td><button class="ghost" ${acc.active > 0 ? 'disabled' : ''} onclick="removeAccount('${acc.user_id}')">移除</button></td>
    </tr>`).join('');

  const keys = await api('/admin/api/keys');
  document.getElementById('keys').innerHTML = keys.data.map(item => `
    <tr>
      <td>${item.name}</td>
      <td title="${item.key}">${shortKey(item.key)}</td>
      <td>${item.total_requests}</td>
      <td>${item.last_used_at || '暂无'}</td>
      <td><button class="ghost" onclick="deleteKey('${item.id}')">删除</button></td>
    </tr>`).join('');

  pushHistory(data);
}

async function addAccount() { await api('/admin/api/accounts', { method: 'POST', body: '{}' }); loadDashboard(); }
async function removeAccount(userId) { await api(`/admin/api/accounts/${userId}`, { method: 'DELETE' }); loadDashboard(); }
async function deleteKey(id) { await api(`/admin/api/keys/${id}`, { method: 'DELETE' }); loadDashboard(); }

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('key-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = { name: fd.get('name') || '', key: fd.get('key') || '' };
    try {
      const res = await api('/admin/api/keys', { method: 'POST', body: JSON.stringify(payload) });
      document.getElementById('key-msg').textContent = `已创建: ${res.key}`;
      e.target.reset();
      loadDashboard();
    } catch (err) {
      document.getElementById('key-msg').textContent = err.message;
    }
  });
  loadDashboard();
  setInterval(loadDashboard, 8000);
});
