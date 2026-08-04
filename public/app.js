/* ============================================================
 * 儿童储蓄银行 - 前端（原生 JS，PWA）
 * 轻量单页应用：登录/注册 + 家长端 + 儿童端
 * ============================================================ */
const API = '/api';
let token = localStorage.getItem('token') || '';
let user = JSON.parse(localStorage.getItem('user') || 'null');
let tab = 'overview'; // 当前页签
let allowRegister = false; // 是否允许注册（由后端 /api/health 下发）

const $app = document.getElementById('app');
const $modal = document.getElementById('modal-root');

/* ---------- 工具 ---------- */
const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
const money = (n) => '¥' + Number(n || 0).toFixed(2);
const fmtDate = (s) => (s ? String(s).slice(5, 16) : '');

/* ---------- 轻提示（替代原生 alert 弹窗） ---------- */
let __toastTimer = null;
function toast(msg) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(__toastTimer);
  __toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}
/* 覆盖原生 alert：全部改为底部消息条，不再弹窗 */
function alert(msg) { toast(msg); }

const TYPE_LABEL = {
  task_reward: '任务奖励', punish: '惩罚扣款', save: '存钱',
  withdraw: '取钱', consume: '消费', interest: '活期利息',
  term_in: '转存定期', term_out: '定期到期', term_interest: '定期利息',
  parent_deposit: '家长存入',
};
const TYPE_ICON = {
  task_reward: '🎖️', punish: '🚫', save: '💰', withdraw: '🏦',
  consume: '🛒', interest: '✨', term_in: '🏦', term_out: '💵',
  term_interest: '⭐', parent_deposit: '👛',
};
const TASK_STATUS = {
  pending: '待家长审批', active: '待完成', completed: '待家长确认', paid: '已发放', rejected: '已驳回',
};
const TPL_ICONS = ['⭐', '🏆', '💰', '🧹', '🍽️', '📚', '🎨', '🏅', '🍪', '🧸', '🎮', '⚽', '📝', '✏️', '🌱', '🚿', '🦷', '🥗', '🪥', '🚫', '⏰', '😴', '📵', '🤬'];

const PARENT_TABS = [
  { id: 'overview', name: '总览', icon: '🏠' },
  { id: 'children', name: '孩子', icon: '👨‍👧' },
  { id: 'templates', name: '模板', icon: '📋' },
  { id: 'tasks', name: '任务', icon: '🧹' },
  { id: 'reviews', name: '审核', icon: '✅' },
];
const CHILD_TABS = [
  { id: 'overview', name: '总览', icon: '🏠' },
  { id: 'money', name: '存取', icon: '💰' },
  { id: 'goals', name: '目标', icon: '🎯' },
  { id: 'tasks', name: '任务', icon: '🧹' },
  { id: 'records', name: '流水', icon: '📒' },
];

/* ---------- API ---------- */
async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  let res;
  try {
    res = await fetch(API + path, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error('网络异常，请检查连接');
  }
  let data = {};
  try { data = await res.json(); } catch { /* ignore */ }
  if (!res.ok || data.ok === false) {
    const e = new Error(data.msg || '请求失败(' + res.status + ')');
    e.status = res.status;
    throw e;
  }
  return data;
}

function saveAuth() {
  localStorage.setItem('token', token);
  localStorage.setItem('user', JSON.stringify(user));
}
function doLogout() {
  token = ''; user = null; tab = 'overview';
  localStorage.removeItem('token'); localStorage.removeItem('user');
  render();
}
function setTab(id) { tab = id; render(); }

/* ---------- 模态框 ---------- */
function openModal(html) {
  $modal.innerHTML = `<div class="modal-mask" onclick="if(event.target===this)closeModal()"><div class="modal">${html}</div></div>`;
}
function closeModal() { $modal.innerHTML = ''; }

/* ---------- 主渲染 ---------- */
async function render() {
  if (!user || !token) return renderAuth();
  try {
    if (user.role === 'parent') return await renderParent();
    return await renderChild();
  } catch (e) {
    if (e.status === 401) { doLogout(); return; }
    $app.innerHTML = `<div class="card"><h3>出错了</h3><p>${esc(e.message)}</p><button class="btn" onclick="render()">重试</button></div>`;
  }
}

function shell(title, tabs, active, content) {
  return `
  <header class="topbar">
    <div class="topbar-title">🐷 ${title}</div>
    <div class="topbar-actions">
      <button class="chip" onclick="openChangePwd()">🔒 改密码</button>
      <button class="chip" onclick="doLogout()">退出</button>
    </div>
  </header>
  <main class="content">${content}</main>
  <nav class="tabbar">
    ${tabs.map((t) => `
      <button class="tab ${t.id === active ? 'active' : ''}" onclick="setTab('${t.id}')">
        <span class="tab-icon">${t.icon}</span><span class="tab-name">${t.name}</span>
      </button>`).join('')}
  </nav>`;
}

/* ---------- 修改密码 ---------- */
function openChangePwd() {
  openModal(`
    <h3>🔒 修改密码</h3>
    <div class="field"><input id="cp-old" type="password" placeholder="当前密码" autocomplete="current-password"></div>
    <div class="field"><input id="cp-new" type="password" placeholder="新密码（至少 6 位）" autocomplete="new-password"></div>
    <div class="field"><input id="cp-new2" type="password" placeholder="确认新密码" autocomplete="new-password"></div>
    <div class="btn-row">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn ok" onclick="submitChangePwd()">确认修改</button>
    </div>`);
}
async function submitChangePwd() {
  const old_pwd = document.getElementById('cp-old').value;
  const new_pwd = document.getElementById('cp-new').value;
  const new2 = document.getElementById('cp-new2').value;
  if (new_pwd !== new2) { alert('两次输入的新密码不一致'); return; }
  try {
    const r = await api('POST', '/auth/change-password', { old_password: old_pwd, new_password: new_pwd });
    closeModal();
    alert(r.msg + '，请使用新密码重新登录');
    doLogout();
  } catch (err) { alert(err.message); }
}

/* ================= 登录 / 注册 ================= */
let authMode = 'login';

function renderAuth() {
  $app.innerHTML = `
  <div class="auth-wrap">
    <div class="auth-card">
      <div class="auth-logo">🐷</div>
      <h1>小猪银行</h1>
      <p class="auth-sub">亲子存钱 · 快乐成长</p>
      <form onsubmit="authSubmit(event)" class="vform">
        <div class="field"><input id="f-username" placeholder="用户名" autocomplete="username" required></div>
        <div class="field"><input id="f-password" type="password" placeholder="密码（至少 6 位）" autocomplete="current-password" required></div>
        <div id="f-reg" hidden>
          <div class="field"><input id="f-name" placeholder="昵称" autocomplete="nickname"></div>
          <div class="field"><select id="f-role"><option value="parent">我是家长</option><option value="child">我是孩子</option></select></div>
        </div>
        <button class="btn primary btn-block" type="submit" id="f-submit">登 录</button>
      </form>
      ${allowRegister ? '<button class="link" onclick="toggleAuthMode()" id="f-toggle">没有账号？立即注册</button>' : ''}
    </div>
  </div>`;
}

function toggleAuthMode() {
  authMode = authMode === 'login' ? 'register' : 'login';
  document.getElementById('f-reg').hidden = authMode !== 'register';
  document.getElementById('f-submit').textContent = authMode === 'login' ? '登 录' : '注 册';
  document.getElementById('f-toggle').textContent = authMode === 'login' ? '没有账号？立即注册' : '已有账号？去登录';
}

async function authSubmit(e) {
  e.preventDefault();
  const username = document.getElementById('f-username').value.trim();
  const password = document.getElementById('f-password').value;
  try {
    if (authMode === 'register') {
      const name = document.getElementById('f-name').value.trim();
      const role = document.getElementById('f-role').value;
      if (!name) throw new Error('请填写昵称');
      await api('POST', '/auth/register', { username, password, name, role });
      alert('注册成功，已自动登录');
    }
    const data = await api('POST', '/auth/login', { username, password });
    token = data.token; user = data.user; saveAuth();
    render();
  } catch (err) { alert(err.message); }
}

/* ================= 家长端 ================= */
async function renderParent() {
  let content = '';
  if (tab === 'overview') content = await parentOverview();
  else if (tab === 'children') content = await parentChildren();
  else if (tab === 'templates') content = await parentTemplates();
  else if (tab === 'tasks') content = await parentTasks();
  else if (tab === 'reviews') content = await parentReviews();
  $app.innerHTML = shell(`家长 · ${esc(user.name)}`, PARENT_TABS, tab, content);
}

async function parentOverview() {
  const [c, r] = await Promise.all([api('GET', '/children'), api('GET', '/reviews')]);
  const total = c.children.reduce((s, ch) => s + Number(ch.balance || 0), 0);
  return `
    <div class="greet">👋 你好，${esc(user.name)}</div>
    <div class="stat-row">
      <div class="stat-card"><div class="stat-num">${c.children.length}</div><div class="stat-label">孩子</div></div>
      <div class="stat-card"><div class="stat-num">${money(total)}</div><div class="stat-label">总余额</div></div>
      <div class="stat-card"><div class="stat-num">${r.reviews.length}</div><div class="stat-label">待审核</div></div>
    </div>
    ${c.children.length === 0
      ? '<div class="empty">还没有绑定孩子，请到「孩子」页绑定</div>'
      : `<h3 class="sec-title">孩子账户</h3>` + c.children.map((ch) => `
        <div class="card row-card">
          <div class="avatar">${esc(ch.name[0])}</div>
          <div class="row-main">
            <div class="row-title">${esc(ch.name)} <span class="tag">${esc(ch.username)}</span></div>
            <div class="row-sub">年利率 ${(Number(ch.interest_rate) * 100).toFixed(1)}%</div>
          </div>
          <div class="row-amount">${money(ch.balance)}</div>
        </div>`).join('')}
    <div class="quick-grid">
      <button class="quick" onclick="setTab('children')">👨‍👧 管理孩子</button>
      <button class="quick" onclick="setTab('templates')">📋 奖惩模板</button>
      <button class="quick" onclick="setTab('tasks')">🧹 发布任务</button>
      <button class="quick" onclick="setTab('reviews')">✅ 去审核${r.reviews.length ? `(${r.reviews.length})` : ''}</button>
    </div>`;
}

async function parentChildren() {
  const c = await api('GET', '/children');
  window.__childrenData = c.children; // 供阶梯利率弹窗读取
  return `
    <div class="card">
      <h3>👨‍👧 绑定孩子</h3>
      <form onsubmit="bindChild(event)" class="inline-form">
        <input id="bind-username" placeholder="输入孩子的用户名" required>
        <button class="btn primary">绑定</button>
      </form>
      <div class="btn-row" style="margin-top:8px">
        <button class="btn" onclick="createChildModal()">➕ 创建账号</button>
      </div>
    </div>
    ${c.children.map((ch) => `
      <div class="card">
        <div class="row-card">
          <div class="avatar">${esc(ch.name[0])}</div>
          <div class="row-main">
            <div class="row-title">${esc(ch.name)} <span class="tag">${esc(ch.username)}</span></div>
            <div class="row-sub">活期 ${money(ch.balance)}${ch.term_balance > 0 ? ` · 定期 ${money(ch.term_balance)}` : ''} · ${rateLabel(ch)}</div>
          </div>
        </div>
        <div class="btn-row">
          <button class="btn" onclick="tierModal(${ch.id})">📈 活期利率</button>
          <button class="btn" onclick="termTierModal(${ch.id})">⏱ 定期利率</button>
          <button class="btn" onclick="settleInterest(${ch.id})">结息</button>
          <button class="btn warn" onclick="punishModal(${ch.id},'${esc(ch.name)}')">惩罚</button>
          <button class="btn ghost" onclick="unbindChild(${ch.id})">解绑</button>
        </div>
      </div>`).join('')}
    <p class="hint">可用「创建账号」为孩子建号并自动绑定，或创建新的家长账号；若孩子已自行注册，也可输入其用户名绑定。</p>`;
}

function rateLabel(ch) {
  const eff = ch.effective_rate != null
    ? (Number(ch.effective_rate) * 100).toFixed(1)
    : (Number(ch.interest_rate) * 100).toFixed(1);
  if (ch.tiers && ch.tiers.length) return `阶梯 ${ch.tiers.length} 档 · 当前综合 ${eff}%`;
  return `年利率 ${eff}%`;
}

/* ---------- 阶梯利率配置 ---------- */
function tierModal(childId) {
  const ch = (window.__childrenData || []).find((x) => x.id === childId) || {};
  const tiers = (ch.tiers && ch.tiers.length)
    ? ch.tiers.map((t) => ({ min_amount: Number(t.min_amount), rate: Number(t.rate) }))
    : [{ min_amount: 0, rate: Number(ch.interest_rate) || 0.02 }];
  window.__tierRows = tiers;
  openModal(`
    <h3>📈 阶梯利率 · ${esc(ch.name || '')}</h3>
    <p class="hint">按余额分段计息：处于某档区间内的那部分钱，按该档年利率计息（日利息 = 年利率 ÷ 365）。</p>
    <div id="tier-rows"></div>
    <div class="btn-row">
      <button class="btn" onclick="addTierRow()">＋ 添加一档</button>
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn ok" onclick="saveTiers(${childId})">保存</button>
    </div>`);
  renderTierRows();
}
function renderTierRows() {
  const el = document.getElementById('tier-rows');
  if (!el) return;
  el.innerHTML = window.__tierRows.map((r, i) => `
    <div class="field-row tier-row">
      <span class="tier-symbol">≥</span>
      <input class="tier-min" type="number" step="1" min="0" value="${r.min_amount}" placeholder="金额"
        onchange="window.__tierRows[${i}].min_amount = parseFloat(this.value) || 0">
      <span class="tier-symbol">元</span>
      <span class="tier-arrow">→</span>
      <input class="tier-rate" type="number" step="0.1" min="0" max="100" value="${(r.rate * 100).toFixed(1)}" placeholder="%"
        onchange="window.__tierRows[${i}].rate = (parseFloat(this.value) || 0) / 100">
      <span class="tier-symbol">%</span>
      <button class="btn ghost" onclick="removeTierRow(${i})">✕</button>
    </div>`).join('');
}
function addTierRow() { window.__tierRows.push({ min_amount: 0, rate: 0.02 }); renderTierRows(); }
function removeTierRow(i) { window.__tierRows.splice(i, 1); renderTierRows(); }
async function saveTiers(childId) {
  try {
    const tiers = window.__tierRows.map((r) => ({ min_amount: r.min_amount, rate: r.rate }));
    const r = await api('PUT', '/children/' + childId + '/tiers', { tiers });
    closeModal(); alert(r.msg); render();
  } catch (err) { alert(err.message); }
}

/* ---------- 定期利率（时间阶梯）配置 ---------- */
function termTierModal(childId) {
  const ch = (window.__childrenData || []).find((x) => x.id === childId) || {};
  const tiers = (ch.term_tiers && ch.term_tiers.length)
    ? ch.term_tiers.map((t) => ({ min_days: Number(t.min_days), rate: Number(t.rate) }))
    : [{ min_days: 0, rate: Number(ch.interest_rate) || 0.02 }];
  window.__termRows = tiers;
  openModal(`
    <h3>⏱ 定期利率 · ${esc(ch.name || '')}</h3>
    <p class="hint">存期达到对应天数时使用该档年利率，存期越长利率越高。</p>
    <div id="term-rows"></div>
    <div class="btn-row">
      <button class="btn" onclick="addTermRow()">＋ 添加一档</button>
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn ok" onclick="saveTermTiers(${childId})">保存</button>
    </div>`);
  renderTermRows();
}
function renderTermRows() {
  const el = document.getElementById('term-rows');
  if (!el) return;
  el.innerHTML = window.__termRows.map((r, i) => `
    <div class="field-row tier-row">
      <span class="tier-symbol">≥</span>
      <input class="tier-min" type="number" step="1" min="0" value="${r.min_days}" placeholder="天数"
        onchange="window.__termRows[${i}].min_days = parseInt(this.value) || 0">
      <span class="tier-symbol">天</span>
      <span class="tier-arrow">→</span>
      <input class="tier-rate" type="number" step="0.1" min="0" max="100" value="${(r.rate * 100).toFixed(1)}" placeholder="%"
        onchange="window.__termRows[${i}].rate = (parseFloat(this.value) || 0) / 100">
      <span class="tier-symbol">%</span>
      <button class="btn ghost" onclick="removeTermRow(${i})">✕</button>
    </div>`).join('');
}
function addTermRow() { window.__termRows.push({ min_days: 0, rate: 0.02 }); renderTermRows(); }
function removeTermRow(i) { window.__termRows.splice(i, 1); renderTermRows(); }
async function saveTermTiers(childId) {
  try {
    const tiers = window.__termRows.map((r) => ({ min_days: r.min_days, rate: r.rate }));
    const r = await api('PUT', '/children/' + childId + '/term-tiers', { tiers });
    closeModal(); alert(r.msg); render();
  } catch (err) { alert(err.message); }
}

/* ---------- 定期存款 ---------- */
async function createTermDeposit(e) {
  e.preventDefault();
  try {
    const r = await api('POST', '/term-deposits', {
      amount: parseFloat(document.getElementById('td-amount').value),
      term_days: parseInt(document.getElementById('td-days').value, 10),
    });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function settleTerm() {
  try { const r = await api('POST', '/term-deposits/settle'); alert(r.msg); render(); }
  catch (err) { alert(err.message); }
}

async function parentTemplates() {
  const t = await api('GET', '/templates');
  const card = (x) => `
    <div class="card row-card">
      <div class="avatar">${esc(x.icon || (x.type === 'reward' ? '⭐' : '🚫'))}</div>
      <div class="row-main">
        <div class="row-title">${esc(x.name)} <span class="tag ${x.type}">${x.type === 'reward' ? '奖励' : '惩罚'}</span></div>
        <div class="row-sub">${esc(x.description || '')}</div>
      </div>
      <div class="row-amount">${money(x.amount)}<span class="per">/次</span></div>
      <button class="btn ghost" onclick="deleteTemplate(${x.id})">删</button>
    </div>`;
  const rewards = t.templates.filter((x) => x.type === 'reward').map(card).join('');
  const punishes = t.templates.filter((x) => x.type === 'punish').map(card).join('');
  return `
    <div class="card">
      <h3>📋 新增奖惩项目</h3>
      <form onsubmit="addTemplate(event)" class="vform">
        <div class="field"><input id="t-name" placeholder="项目名称，如：扫地" required></div>
        <div class="field"><select id="t-type"><option value="reward">奖励</option><option value="punish">惩罚</option></select></div>
        <div class="field">
          <label class="tpl-icon-label">图标（点击选择）：</label>
          <div class="emoji-picker" id="t-icon-picker">
            ${TPL_ICONS.map((e) => `<button type="button" class="emoji-opt" data-e="${e}" onclick="pickTplIcon('${e}')">${e}</button>`).join('')}
          </div>
          <input id="t-icon" placeholder="或输入自定义 emoji" maxlength="4" autocomplete="off" oninput="pickTplIconCustom(this.value)">
        </div>
        <div class="field"><div class="field-row">
          <input id="t-amount" type="number" step="0.5" min="0.5" placeholder="每次价格(元)" required>
          <button class="btn primary">保存</button>
        </div></div>
        <div class="field"><input id="t-desc" placeholder="说明(可选)"></div>
      </form>
    </div>
    <h3 class="sec-title">奖励项目</h3>${rewards || '<div class="empty">暂无奖励项目</div>'}
    <h3 class="sec-title">惩罚项目</h3>${punishes || '<div class="empty">暂无惩罚项目</div>'}`;
}

async function parentTasks() {
  const [children, tasks] = await Promise.all([api('GET', '/children'), api('GET', '/tasks')]);
  const childOpts = children.children.map((ch) => `<option value="${ch.id}">${esc(ch.name)}</option>`).join('');
  const tpls = (await api('GET', '/templates')).templates.filter((x) => x.type === 'reward');
  const tplOpts = '<option value="">不使用模板</option>' +
    tpls.map((t) => `<option value="${t.id}">${esc(t.name)}(${money(t.amount)})</option>`).join('');
  const list = tasks.tasks.map((t) => {
    let action = '';
    if (t.status === 'pending' && t.initiator === 'child') {
      action = `<div class="btn-row">
        <button class="btn ok" onclick="reviewTask(${t.id},'approve')">通过</button>
        <button class="btn" onclick="reviewTask(${t.id},'reject')">驳回</button></div>`;
    } else if (t.status === 'completed') {
      action = `<div class="btn-row">
        <button class="btn ok" onclick="reviewTask(${t.id},'approve')">发放${money(t.reward_amount)}</button>
        <button class="btn" onclick="reviewTask(${t.id},'reject')">驳回</button></div>`;
    }
    return `<div class="card task-card">
      <div class="row-card">
        <div class="row-main">
          <div class="row-title">${esc(t.title)} <span class="tag">${t.initiator === 'parent' ? '家长布置' : '孩子申请'}</span></div>
          <div class="row-sub">${esc(t.child_name || '')} · ${TASK_STATUS[t.status] || t.status}${t.description ? '<br>' + esc(t.description) : ''}</div>
        </div>
        <div class="row-amount">${money(t.reward_amount)}</div>
      </div>${action}
    </div>`;
  }).join('');
  return `
    <div class="card">
      <h3>🧹 发布家务任务</h3>
      <form onsubmit="createTask(event)" class="vform">
        <div class="field"><select id="tk-child" required><option value="">选择孩子</option>${childOpts}</select></div>
        <div class="field"><select id="tk-tpl">${tplOpts}</select></div>
        <div class="field"><input id="tk-title" placeholder="任务名称（选了模板可留空，用模板名）"></div>
        <div class="field"><input id="tk-reward" type="number" step="0.5" min="0" placeholder="奖励金额(元，留空用模板价)"></div>
        <div class="field"><input id="tk-desc" placeholder="说明(可选)"></div>
        <div class="field"><label><input id="tk-completed" type="checkbox"> 已完成（孩子已经做完，直接登记）</label></div>
        <button class="btn primary btn-block">发布</button>
      </form>
    </div>
    <h3 class="sec-title">任务列表</h3>${list || '<div class="empty">暂无任务</div>'}`;
}

async function parentReviews() {
  const r = await api('GET', '/reviews');
  const list = r.reviews.map((t) => `
    <div class="card">
      <div class="row-card">
        <div class="avatar">${TYPE_ICON[t.type] || '💸'}</div>
        <div class="row-main">
          <div class="row-title">${esc(t.child_name || '')} · ${TYPE_LABEL[t.type] || t.type} <span class="tag pending">待审核</span></div>
          <div class="row-sub">${esc(t.description || '')} · ${fmtDate(t.created_at)}</div>
        </div>
        <div class="row-amount ${Number(t.amount) >= 0 ? 'plus' : 'minus'}">${Number(t.amount) >= 0 ? '+' : '-'}${money(Math.abs(t.amount))}</div>
      </div>
      <div class="btn-row">
        <button class="btn ok" onclick="reviewTx(${t.id},'approve')">${Number(t.amount) >= 0 ? '确认入账' : '通过'}</button>
        <button class="btn" onclick="reviewTx(${t.id},'reject')">驳回</button>
      </div>
    </div>`).join('');
  return `<h3 class="sec-title">待审核项目（存钱 / 取钱 / 消费）</h3>${list || '<div class="empty">暂无待审核项目 🎉</div>'}`;
}

/* ================= 儿童端 ================= */
async function renderChild() {
  let content = '';
  if (tab === 'overview') content = await childOverview();
  else if (tab === 'money') content = await childMoney();
  else if (tab === 'goals') content = await childGoals();
  else if (tab === 'tasks') content = await childTasks();
  else if (tab === 'records') content = await childRecords();
  $app.innerHTML = shell(`${esc(user.name)} 的储蓄罐`, CHILD_TABS, tab, content);
}

const txItem = (t) => `
  <div class="card row-card">
    <div class="avatar">${TYPE_ICON[t.type] || '💸'}</div>
    <div class="row-main">
      <div class="row-title">${TYPE_LABEL[t.type] || t.type}
        ${t.status === 'pending' ? '<span class="tag pending">待审核</span>' : ''}
        ${t.status === 'rejected' ? '<span class="tag">已驳回</span>' : ''}</div>
      <div class="row-sub">${esc(t.description || '')} · ${fmtDate(t.created_at)}</div>
    </div>
    <div class="row-amount ${Number(t.amount) >= 0 ? 'plus' : 'minus'}">
      ${Number(t.amount) >= 0 ? '+' : ''}${money(t.amount)}</div>
  </div>`;

const goalPct = (g) => (g.target_amount > 0 ? Math.min(100, Math.round((g.saved_amount / g.target_amount) * 100)) : 0);

async function childOverview() {
  const [accData, goalsData] = await Promise.all([api('GET', '/me/account'), api('GET', '/goals')]);
  const acc = accData.account;
  const active = goalsData.goals.filter((g) => g.status === 'active');
  const rateText = (acc.tiers && acc.tiers.length)
    ? `阶梯利率 · 当前综合 ${(Number(acc.effective_rate) * 100).toFixed(1)}%`
    : `年利率 ${(Number(acc.interest_rate) * 100).toFixed(1)}%`;
  const termText = acc.term_balance > 0 ? ` · 定期 ${money(acc.term_balance)}` : '';
  return `
    <div class="hero">
      <div class="hero-emoji">🐷</div>
      <div class="hero-label">我的储蓄罐</div>
      <div class="hero-balance">${money(acc.balance)}</div>
      <div class="hero-sub">活期余额${termText} · ${rateText}</div>
    </div>
    <div class="quick-grid">
      <button class="quick" onclick="setTab('money')">💰 存取钱</button>
      <button class="quick" onclick="setTab('goals')">🎯 储蓄目标</button>
      <button class="quick" onclick="setTab('tasks')">🧹 领任务</button>
      <button class="quick" onclick="setTab('records')">📒 看流水</button>
    </div>
    <h3 class="sec-title">进行中的目标</h3>
    ${active.slice(0, 3).map((g) => `
      <div class="card">
        <div class="row-main">
          <div class="row-title">${esc(g.name)}</div>
          <div class="row-sub">${money(g.saved_amount)} / ${money(g.target_amount)}</div>
        </div>
        <div class="progress"><div class="progress-bar" style="width:${goalPct(g)}%"></div></div>
      </div>`).join('') || '<div class="empty">还没有储蓄目标，去「目标」页创建一个吧 🎯</div>'}`;
}

const isMatured = (d) => new Date(String(d.mature_at).replace(' ', 'T')) <= new Date();
function termDaysOptions(termTiers) {
  let days = termTiers && termTiers.length ? termTiers.map((t) => Number(t.min_days)) : [7, 30, 90, 365];
  days = [...new Set(days)].filter((d) => d > 0).sort((a, b) => a - b);
  if (!days.length) days = [7, 30, 90, 365];
  return days.map((d) => `<option value="${d}">${d} 天</option>`).join('');
}

async function childMoney() {
  const accData = await api('GET', '/me/account');
  const acc = accData.account;
  const goals = (await api('GET', '/goals')).goals.filter((g) => g.status === 'active');
  const goalOpts = '<option value="">不关联目标</option>' +
    goals.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join('');
  const deposits = (acc.term_deposits || []).filter((d) => d.status === 'active');
  const recent = (await api('GET', '/transactions')).transactions.slice(0, 5);
  const dayOptions = termDaysOptions(acc.term_tiers);
  return `
    <div class="card balance-card"><span>活期余额</span><b>${money(acc.balance)}</b></div>
    ${acc.term_balance > 0 ? `<div class="card balance-card"><span>定期合计</span><b>${money(acc.term_balance)}</b></div>` : ''}
    <div class="card">
      <h3>💰 存钱</h3>
      <form onsubmit="saveMoney(event)" class="vform">
        <div class="field"><input id="s-amount" type="number" step="0.5" min="0.5" placeholder="存入金额(元)" required></div>
        <div class="field"><select id="s-goal">${goalOpts}</select></div>
        <div class="field"><input id="s-desc" placeholder="备注(可选)"></div>
        <button class="btn ok btn-block">存入储蓄罐</button>
      </form>
    </div>
    <div class="card">
      <h3>🏦 定期存款（锁定期内不能取，到期自动还本付息）</h3>
      <form onsubmit="createTermDeposit(event)" class="vform">
        <div class="field"><div class="field-row">
          <input id="td-amount" type="number" step="0.5" min="0.5" placeholder="转存金额(元)" required>
          <select id="td-days">${dayOptions}</select>
        </div></div>
        <button class="btn primary btn-block">转存定期</button>
      </form>
    </div>
    <h3 class="sec-title">我的定期</h3>
    ${deposits.length ? deposits.map((d) => `
      <div class="card row-card">
        <div class="avatar">🏦</div>
        <div class="row-main">
          <div class="row-title">${money(d.amount)} <span class="tag">${d.term_days} 天 · 年利率 ${(Number(d.rate) * 100).toFixed(1)}%</span></div>
          <div class="row-sub">${fmtDate(d.mature_at)} 到期${isMatured(d) ? ' · 已到期可结算' : ''}</div>
        </div>
      </div>`).join('')
      : '<div class="empty">还没有定期存款，把暂时用不到的钱转成定期吧</div>'}
    <div class="btn-row"><button class="btn ok" onclick="settleTerm()">结算到期定期</button></div>
    <div class="card">
      <h3>🏦 取钱（需家长审核）</h3>
      <form onsubmit="takeMoney(event,'withdraw')" class="vform">
        <div class="field"><input id="w-amount" type="number" step="0.5" min="0.5" placeholder="取款金额(元)" required></div>
        <div class="field"><input id="w-desc" placeholder="用途(可选)"></div>
        <button class="btn primary btn-block">申请取款</button>
      </form>
    </div>
    <div class="card">
      <h3>🛒 消费（需家长审核）</h3>
      <form onsubmit="takeMoney(event,'consume')" class="vform">
        <div class="field"><input id="c-amount" type="number" step="0.5" min="0.5" placeholder="消费金额(元)" required></div>
        <div class="field"><input id="c-desc" placeholder="买了什么(可选)"></div>
        <button class="btn warn btn-block">提交消费</button>
      </form>
    </div>
    ${recent.length ? `<h3 class="sec-title">最近流水</h3>` + recent.map(txItem).join('') : ''}`;
}

async function childGoals() {
  const goals = (await api('GET', '/goals')).goals;
  return `
    <div class="card">
      <h3>🎯 新建储蓄目标</h3>
      <form onsubmit="addGoal(event)" class="vform">
        <div class="field"><input id="g-name" placeholder="目标名称，如：买乐高" required></div>
        <div class="field"><div class="field-row">
          <input id="g-amount" type="number" step="1" min="1" placeholder="目标金额(元)" required>
          <input id="g-deadline" type="date">
        </div></div>
        <button class="btn primary btn-block">创建目标</button>
      </form>
    </div>
    ${goals.map((g) => `
      <div class="card ${g.status === 'achieved' ? 'achieved' : ''}">
        <div class="row-main">
          <div class="row-title">${esc(g.name)}
            <span class="tag">${g.status === 'achieved' ? '已达成 🎉' : g.status === 'cancelled' ? '已取消' : '进行中'}</span></div>
          <div class="row-sub">${money(g.saved_amount)} / ${money(g.target_amount)}${g.deadline ? ' · 截止 ' + g.deadline : ''}</div>
        </div>
        <div class="progress"><div class="progress-bar" style="width:${goalPct(g)}%"></div></div>
        ${g.status === 'active' ? `<div class="btn-row"><button class="btn ghost" onclick="cancelGoal(${g.id})">取消目标</button></div>` : ''}
      </div>`).join('') || '<div class="empty">还没有目标</div>'}`;
}

async function childTasks() {
  const tasks = (await api('GET', '/tasks')).tasks;
  const list = tasks.map((t) => {
    let action = '';
    if (t.status === 'active') {
      action = `<div class="btn-row"><button class="btn ok" onclick="completeTask(${t.id})">✓ 我完成了</button></div>`;
    }
    return `<div class="card">
      <div class="row-card">
        <div class="row-main">
          <div class="row-title">${esc(t.title)} <span class="tag">${t.initiator === 'parent' ? '家长布置' : '我申请的'}</span></div>
          <div class="row-sub">${TASK_STATUS[t.status] || t.status}${t.description ? '<br>' + esc(t.description) : ''}</div>
        </div>
        <div class="row-amount plus">+${money(t.reward_amount)}</div>
      </div>${action}
    </div>`;
  }).join('');
  return `
    <div class="card">
      <h3>🧹 申请任务（默认已完成，家长审批后直接发奖励）</h3>
      <form onsubmit="applyTask(event)" class="vform">
        <div class="field"><input id="a-title" placeholder="我想做什么，如：帮爸爸擦车" required></div>
        <div class="field"><input id="a-reward" type="number" step="0.5" min="0.5" placeholder="期望奖励(元)" required></div>
        <div class="field"><input id="a-desc" placeholder="说明(可选)"></div>
        <div class="field"><label><input id="a-completed" type="checkbox" checked> 已完成（家长审批后直接发放奖励）</label></div>
        <button class="btn primary btn-block">申请任务</button>
      </form>
    </div>
    <h3 class="sec-title">我的任务</h3>${list || '<div class="empty">暂无任务'}`;
}

async function childRecords() {
  const data = await api('GET', '/transactions');
  const list = data.transactions.map(txItem).join('');
  return `<h3 class="sec-title">全部流水</h3>${list || '<div class="empty">暂无记录'}`;
}

/* ================= 操作处理 ================= */
async function bindChild(e) {
  e.preventDefault();
  try {
    const r = await api('POST', '/children/bind', { username: document.getElementById('bind-username').value.trim() });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
function createChildModal() {
  openModal(`
    <h3>➕ 创建账号</h3>
    <p class="hint">家长可为孩子创建账号（自动绑定），或为家人创建家长账号。</p>
    <form onsubmit="createChild(event)" class="vform">
      <div class="field"><select id="cc-role"><option value="child">孩子（自动绑定）</option><option value="parent">家长</option></select></div>
      <div class="field"><input id="cc-username" placeholder="用户名（至少 3 个字符）" autocomplete="off" required></div>
      <div class="field"><input id="cc-password" type="password" placeholder="密码（至少 6 位）" autocomplete="new-password" required></div>
      <div class="field"><input id="cc-name" placeholder="昵称" autocomplete="off" required></div>
      <div class="btn-row">
        <button class="btn ghost" type="button" onclick="closeModal()">取消</button>
        <button class="btn primary" type="submit">创建</button>
      </div>
    </form>`);
}
async function createChild(e) {
  e.preventDefault();
  try {
    const r = await api('POST', '/children', {
      role: document.getElementById('cc-role').value,
      username: document.getElementById('cc-username').value.trim(),
      password: document.getElementById('cc-password').value,
      name: document.getElementById('cc-name').value.trim(),
    });
    closeModal(); alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function unbindChild(id) {
  if (!confirm('确定解除与该孩子的绑定吗？')) return;
  try { await api('DELETE', '/children/' + id); render(); } catch (err) { alert(err.message); }
}
async function settleInterest(childId) {
  try {
    const r = await api('POST', '/children/' + childId + '/interest');
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function punishModal(childId, name) {
  const t = await api('GET', '/templates');
  const puns = t.templates.filter((x) => x.type === 'punish');
  const opts = puns.map((p) => `<option value="${p.id}">${esc(p.icon || '')} ${esc(p.name)}（${money(p.amount)}）</option>`).join('');
  openModal(`
    <h3>惩罚扣款 · ${esc(name)}</h3>
    <div class="field"><select id="pm-tpl"><option value="">自定义金额</option>${opts}</select></div>
    <div class="field"><input id="pm-amount" type="number" step="0.5" min="0.5" placeholder="扣款金额(元)"></div>
    <div class="field"><input id="pm-desc" placeholder="原因(可选)"></div>
    <div class="btn-row">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn warn" onclick="submitPunish(${childId})">确认扣款</button>
    </div>`);
}
async function submitPunish(childId) {
  try {
    const body = {
      template_id: document.getElementById('pm-tpl').value || null,
      amount: document.getElementById('pm-amount').value || undefined,
      description: document.getElementById('pm-desc').value,
    };
    const r = await api('POST', '/children/' + childId + '/punish', body);
    closeModal(); alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
function pickTplIcon(e) {
  const ip = document.getElementById('t-icon');
  if (ip) ip.value = e;
  const el = document.getElementById('t-icon-picker');
  if (el) el.querySelectorAll('.emoji-opt').forEach((b) => b.classList.toggle('on', b.dataset.e === e));
}
function pickTplIconCustom(v) {
  const val = (v || '').trim();
  const el = document.getElementById('t-icon-picker');
  if (el) el.querySelectorAll('.emoji-opt').forEach((b) => b.classList.toggle('on', b.dataset.e === val));
}
async function addTemplate(e) {
  e.preventDefault();
  try {
    const body = {
      name: document.getElementById('t-name').value.trim(),
      type: document.getElementById('t-type').value,
      icon: document.getElementById('t-icon').value.trim(),
      amount: parseFloat(document.getElementById('t-amount').value),
      description: document.getElementById('t-desc').value.trim(),
    };
    const r = await api('POST', '/templates', body);
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function deleteTemplate(id) {
  if (!confirm('确定删除该模板吗？')) return;
  try { await api('DELETE', '/templates/' + id); render(); } catch (err) { alert(err.message); }
}
async function createTask(e) {
  e.preventDefault();
  try {
    const rewardVal = document.getElementById('tk-reward').value;
    const body = {
      child_id: parseInt(document.getElementById('tk-child').value, 10),
      template_id: document.getElementById('tk-tpl').value || null,
      title: document.getElementById('tk-title').value.trim(),
      reward_amount: rewardVal ? parseFloat(rewardVal) : 0,
      description: document.getElementById('tk-desc').value.trim(),
      completed: document.getElementById('tk-completed').checked,
    };
    const r = await api('POST', '/tasks', body);
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function reviewTask(id, action) {
  try {
    const r = await api('PATCH', '/tasks/' + id + '/review', { action });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function reviewTx(id, action) {
  try {
    const r = await api('PATCH', '/transactions/' + id + '/review', { action });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function saveMoney(e) {
  e.preventDefault();
  try {
    const r = await api('POST', '/transactions', {
      type: 'save',
      amount: parseFloat(document.getElementById('s-amount').value),
      goal_id: document.getElementById('s-goal').value || null,
      description: document.getElementById('s-desc').value.trim(),
    });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function takeMoney(e, type) {
  e.preventDefault();
  try {
    const isW = type === 'withdraw';
    const body = {
      type,
      amount: parseFloat(document.getElementById(isW ? 'w-amount' : 'c-amount').value),
      description: document.getElementById(isW ? 'w-desc' : 'c-desc').value.trim(),
    };
    const r = await api('POST', '/transactions', body);
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function addGoal(e) {
  e.preventDefault();
  try {
    const r = await api('POST', '/goals', {
      name: document.getElementById('g-name').value.trim(),
      target_amount: parseFloat(document.getElementById('g-amount').value),
      deadline: document.getElementById('g-deadline').value || null,
    });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function cancelGoal(id) {
  if (!confirm('确定取消该目标吗？')) return;
  try { await api('PATCH', '/goals/' + id + '/cancel'); render(); } catch (err) { alert(err.message); }
}
async function applyTask(e) {
  e.preventDefault();
  try {
    const r = await api('POST', '/tasks', {
      title: document.getElementById('a-title').value.trim(),
      reward_amount: parseFloat(document.getElementById('a-reward').value),
      description: document.getElementById('a-desc').value.trim(),
      completed: document.getElementById('a-completed').checked,
    });
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}
async function completeTask(id) {
  try {
    const r = await api('PATCH', '/tasks/' + id + '/complete');
    alert(r.msg); render();
  } catch (err) { alert(err.message); }
}

/* ---------- PWA 注册 + 安装按钮 ---------- */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* ignore */ });
  });
}

(function setupInstallButton() {
  // 已以 App 方式运行则不再显示安装按钮
  if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return;

  const btn = document.createElement('button');
  btn.id = 'pwa-install';
  btn.style.display = 'none';
  document.body.appendChild(btn);

  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  let deferredPrompt = null;
  let shown = false;

  const showHint = () => {
    if (shown) return;
    shown = true;
    btn.style.display = 'block';
    btn.textContent = isIOS ? '📲 添加到主屏幕' : '📲 安装到桌面';
    btn.onclick = () => {
      if (isIOS) {
        alert('iPhone/iPad 添加方式：点底部「分享」按钮 → 「添加到主屏幕」，即可像 App 一样用。');
      } else {
        alert('未检测到安装入口。请用 https 或 localhost 访问本站，再点浏览器菜单「安装应用 / 添加到主屏幕」。');
      }
    };
  };

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    shown = true;
    btn.style.display = 'block';
    btn.textContent = '📲 安装到桌面';
    btn.onclick = async () => {
      btn.style.display = 'none';
      try {
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
      } catch (err) { /* 用户取消或已安装 */ }
      deferredPrompt = null;
      btn.style.display = 'none';
    };
  });

  window.addEventListener('appinstalled', () => {
    btn.style.display = 'none';
    deferredPrompt = null;
  });

  if (isIOS) {
    showHint();
  } else {
    // 一段时间后仍无可安装入口（如 http 局域网访问），显示操作指引
    setTimeout(showHint, 6000);
  }
})();

/* 启动：先拉取后端配置（注册开关等），再渲染页面 */
(async function boot() {
  try {
    const h = await api('GET', '/health');
    allowRegister = !!h.allow_register;
  } catch (e) { /* 拉取失败保持默认：不允许注册 */ }
  render();
})();
