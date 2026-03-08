const historyPoints = [];
let revealKeys = false;

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, credentials: 'include', ...options });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.detail || data.error?.message || data.message || '请求失败');
  return data;
}

function showLoggedIn(loggedIn) {
  document.getElementById('login-card').classList.toggle('hidden', loggedIn);
  document.getElementById('admin-app').classList.toggle('hidden', !loggedIn);
}

function card(label, value, extra = '') {
  return `<div class="card"><div class="label">${label}</div><div class="stat-num">${value}</div><div class="hint">${extra}</div></div>`;
}

function shortKey(v) {
  return v.length > 20 ? `${v.slice(0, 10)}...${v.slice(-6)}` : v;
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.opacity = '0';
  input.style.pointerEvents = 'none';
  document.body.appendChild(input);
  input.focus();
  input.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(input);
  if (!ok) {
    throw new Error('copy_failed');
  }
}

function healthText(rate) {
  if (rate >= 95) return '状态超稳';
  if (rate >= 80) return '状态良好';
  if (rate >= 60) return '有点波动';
  return '需要关注';
}

function drawTrend() {
  const canvas = document.getElementById('trend-canvas');
  if (!canvas) return;
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
  try {
    const data = await api('/admin/api/dashboard');
    showLoggedIn(true);
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
    document.getElementById('health-meta').textContent = `当前 ${data.active_requests} 个活跃请求，${data.valid_accounts}/${data.current_pool_size} 个账号可用。失败重建冷却 ${data.rebuild_cooldown}s，5 分钟内最多 ${data.rebuild_max_retries} 次。`;

    const rebuildForm = document.getElementById('rebuild-form');
    if (rebuildForm) {
      rebuildForm.elements.rebuild_cooldown.value = data.rebuild_cooldown;
      rebuildForm.elements.rebuild_max_retries.value = data.rebuild_max_retries;
    }

    document.getElementById('accounts').innerHTML = data.accounts.map(acc => `
      <tr>
        <td><div><strong>${acc.username || 'Guest'}</strong></div><div class="hint">${acc.user_id}</div></td>
        <td><span class="pill ${acc.valid ? 'ok' : 'bad'}">${acc.valid ? '正常' : '失效'}</span></td>
        <td>${acc.request_count}</td>
        <td>${acc.success_count}</td>
        <td>${acc.failure_count}</td>
        <td><span class="pill ${acc.success_rate >= 80 ? 'ok' : acc.success_rate >= 50 ? 'warn' : 'bad'}">${acc.success_rate}%</span></td>
        <td><div>${acc.last_success_at || '暂无'}</div><div class="hint">${acc.last_error || '无错误'}</div></td>
        <td><button class="ghost remove-account-btn" data-user-id="${acc.user_id}" ${acc.active > 0 ? 'disabled' : ''}>移除</button></td>
      </tr>`).join('');

    const keys = await api('/admin/api/keys');
    document.getElementById('keys').innerHTML = keys.data.map(item => `
      <tr>
        <td>${item.name}</td>
        <td>
          <div class="key-cell">
            <span title="${item.key}">${revealKeys ? item.key : shortKey(item.key)}</span>
            <button class="ghost icon-btn copy-key-btn" data-key-id="${item.id}">复制</button>
          </div>
        </td>
        <td>${item.total_requests}</td>
        <td>${item.last_used_at || '暂无'}</td>
        <td><button class="ghost delete-key-btn" data-key-id="${item.id}">删除</button></td>
      </tr>`).join('');

    window.__keyMap = Object.fromEntries(keys.data.map(item => [item.id, item.key]));

    pushHistory(data);
  } catch (err) {
    if (String(err.message).includes('Admin auth required')) {
      showLoggedIn(false);
      return;
    }
    throw err;
  }
}

async function addAccount() { await api('/admin/api/accounts', { method: 'POST', body: '{}' }); loadDashboard(); }
async function removeAccount(userId) { await api(`/admin/api/accounts/${userId}`, { method: 'DELETE' }); loadDashboard(); }
async function deleteKey(id) { await api(`/admin/api/keys/${id}`, { method: 'DELETE' }); loadDashboard(); }
async function logout() { await api('/admin/api/logout', { method: 'POST', body: '{}' }); showLoggedIn(false); }
async function copyKey(id) {
  const value = window.__keyMap?.[id];
  if (!value) return;
  try {
    await copyText(value);
    document.getElementById('key-msg').textContent = '已复制 Key';
  } catch (err) {
    document.getElementById('key-msg').textContent = '复制失败，请尝试使用 HTTPS、localhost，或手动长按复制';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('refresh-btn').addEventListener('click', loadDashboard);
  document.getElementById('logout-btn').addEventListener('click', logout);
  document.getElementById('add-account-btn').addEventListener('click', addAccount);

  document.getElementById('accounts').addEventListener('click', async (e) => {
    const btn = e.target.closest('.remove-account-btn');
    if (!btn || btn.disabled) return;
    await removeAccount(btn.dataset.userId);
  });

  document.getElementById('keys').addEventListener('click', async (e) => {
    const copyBtn = e.target.closest('.copy-key-btn');
    if (copyBtn) {
      await copyKey(copyBtn.dataset.keyId);
      return;
    }
    const deleteBtn = e.target.closest('.delete-key-btn');
    if (deleteBtn) {
      await deleteKey(deleteBtn.dataset.keyId);
    }
  });

  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api('/admin/api/login', { method: 'POST', body: JSON.stringify({ password: fd.get('password') || '' }) });
      document.getElementById('login-msg').textContent = '';
      e.target.reset();
      loadDashboard();
    } catch (err) {
      document.getElementById('login-msg').textContent = err.message;
    }
  });

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

  document.getElementById('toggle-keys-btn').addEventListener('click', () => {
    revealKeys = !revealKeys;
    document.getElementById('toggle-keys-btn').textContent = revealKeys ? '隐藏完整 Key' : '显示完整 Key';
    loadDashboard();
  });

  document.getElementById('password-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      current_password: fd.get('current_password') || '',
      new_password: fd.get('new_password') || '',
    };
    try {
      const res = await api('/admin/api/change-password', { method: 'POST', body: JSON.stringify(payload) });
      document.getElementById('password-msg').textContent = res.message || '密码已更新';
      e.target.reset();
      showLoggedIn(false);
    } catch (err) {
      document.getElementById('password-msg').textContent = err.message;
    }
  });

  document.getElementById('rebuild-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      rebuild_cooldown: Number(fd.get('rebuild_cooldown') || 0),
      rebuild_max_retries: Number(fd.get('rebuild_max_retries') || 1),
    };
    try {
      const res = await api('/admin/api/rebuild-settings', { method: 'POST', body: JSON.stringify(payload) });
      document.getElementById('rebuild-msg').textContent = `已保存：冷却 ${res.rebuild_cooldown}s，重试上限 ${res.rebuild_max_retries}`;
      loadDashboard();
    } catch (err) {
      document.getElementById('rebuild-msg').textContent = err.message;
    }
  });

  loadDashboard();
  setInterval(loadDashboard, 8000);
});
