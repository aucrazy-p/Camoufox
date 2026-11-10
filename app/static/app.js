/* 指纹环境管理器前端：视图层，不含业务逻辑。
   所有数据来自本地 HTTP API；窗口控制走 pywebview 暴露的纯函数。 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TOKEN = window.__FP_TOKEN__ || '';
const api = async (path, opts = {}) => {
  const r = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', 'X-FP-Token': TOKEN, ...(opts.headers || {}) },
  });
  const txt = await r.text();
  let data; try { data = txt ? JSON.parse(txt) : {}; } catch { data = { detail: txt }; }
  // 页面拿不到令牌时给一句人话，别让用户只看到「未授权」却无从下手
  if (r.status === 401) throw new Error('本地令牌无效（页面未取到令牌），请从托盘完全退出后重新启动应用');
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
};
const post = (p, body = {}) => api(p, { method: 'POST', body: JSON.stringify(body) });
const put  = (p, body = {}) => api(p, { method: 'PUT',  body: JSON.stringify(body) });
const del  = (p) => api(p, { method: 'DELETE' });

let S = { profiles: [], tab: '__all', q: '', asc: true, view: 'profiles', editId: null,
          settings: { theme: 'dark', close_action: 'tray', autostart: false, download_proxy: '' },
          health: null, kernel: null, runtime: null, releases: [], dlTimer: null, rtTimer: null };

function toast(msg, ms = 2600) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(t._timer); t._timer = setTimeout(() => t.hidden = true, ms);
}
function pj() { return typeof window.pywebview !== 'undefined' && window.pywebview.api; }
async function winAction(act) {
  if (!pj()) { toast('窗口控制仅在应用程序内可用'); return; }
  try { await window.pywebview.api.window_action(act); } catch (e) { toast('窗口操作失败：' + e); }
}

/* ---------------- 主题 ---------------- */
function applyTheme(v) {
  const t = v === 'system'
    ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : v;
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('fp-theme', v);
  document.querySelectorAll('#seg-theme button').forEach(b => b.classList.toggle('active', b.dataset.v === v));
}
matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
  if (S.settings.theme === 'system') applyTheme('system');
});

/* ---------------- 视图切换 ---------------- */
function setView(v) {
  S.view = v;
  document.querySelectorAll('.view').forEach(el => el.hidden = true);
  $('view-' + v).hidden = false;
  document.querySelectorAll('.side-btn').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $('titlebar').style.visibility = 'visible';
  if (v === 'kernel') loadKernel();
  if (v === 'automation') loadAutomation();
  if (v === 'logs') loadLogs();
  if (v === 'settings') syncSettings();
}
document.querySelectorAll('.side-btn').forEach(b => b.onclick = () => setView(b.dataset.view));

/* ---------------- 环境列表 ---------------- */
function groups() {
  const g = new Map();
  S.profiles.forEach(p => { const k = p.group || ''; if (k) g.set(k, (g.get(k) || 0) + 1); });
  return [...g.entries()];
}
function renderTabs() {
  const run = S.profiles.filter(p => p.running).length;
  const items = [['__all', '全部', S.profiles.length], ...groups().map(([k, n]) => [k, k, n]), ['__running', '运行中', run]];
  $('tabs').innerHTML = items.map(([k, label, n]) =>
    `<button class="tab ${S.tab === k ? 'active' : ''}" data-tab="${esc(k)}">${esc(label)} <span class="num">${n}</span></button>`).join('');
  $('tabs').querySelectorAll('.tab').forEach(b => b.onclick = () => { S.tab = b.dataset.tab; render(); });
}
function visible() {
  let list = S.profiles.filter(p => {
    if (S.tab === '__running') { if (!p.running) return false; }
    else if (S.tab !== '__all' && (p.group || '') !== S.tab) return false;
    if (S.q) {
      const hay = [p.name, p.note, p.group, (p.tags || []).join(','), p.proxy.host].join(' ').toLowerCase();
      if (!hay.includes(S.q.toLowerCase())) return false;
    }
    return true;
  });
  list.sort((a, b) => S.asc ? a.name.localeCompare(b.name, 'zh') : b.name.localeCompare(a.name, 'zh'));
  return list;
}
function statusOf(p) {
  if (p.running) return ['ok', '活跃'];
  if (!p.proxy || !p.proxy.host) return ['warn', '待检查'];
  return ['info', '就绪'];
}
function proxyText(p) {
  const x = p.proxy || {};
  return x.host ? `${x.scheme || 'socks5'}://${x.host}:${x.port || ''}` : '—';
}
function netLine(p) {
  const n = p.network || {};
  if (!n.detected_at) return '';
  const ok = n.proxied && n.exit_ip;
  return `<div class="net-line"><span class="dot ${ok ? 'ok' : 'warn'}"></span>`
       + `${esc(n.exit_ip || '—')} · ${esc(n.timezone || '—')}</div>`;
}
function renderNetInfo(p) {
  const n = (p && p.network) || {}, box = $('net-info');
  if (!box) return;
  if (!n.detected_at) {
    box.innerHTML = '尚未启动过。启动环境后会自动记录真实出口 IP 与时区。数据来自内核实际下发的 geoip 配置，不额外联网探测、不往网页注入脚本。';
    return;
  }
  const state = !n.proxied ? '<span class="badge warn">未配置代理 · 本机直连</span>'
    : (n.exit_ip ? '<span class="badge ok">代理已生效</span>' : '<span class="badge warn">已配置代理，但未读到出口 IP</span>');
  const rows = [
    ['状态', state], ['出口 IP', esc(n.exit_ip || '—')], ['时区', esc(n.timezone || '—')],
    ['地区 / 语言', esc([n.region, n.language].filter(Boolean).join(' / ') || '—')],
    ['经纬度', (n.latitude != null && n.longitude != null) ? `${n.latitude}, ${n.longitude}` : '—'],
    ['代理地址', esc(n.proxy_server || '未配置')], ['最近检测', esc(n.detected_at)],
    ['数据来源', esc(n.source || '本次启动实测')],
  ];
  box.innerHTML = rows.map(([k, v]) =>
    `<div style="display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid rgba(128,128,128,.2)"><span>${k}</span><b>${v}</b></div>`).join('');
}
function renderRows() {
  const list = visible();
  const kv = (S.health && S.health.camoufox && S.health.camoufox.kernel) || '—';
  $('grid-body').innerHTML = list.map(p => {
    const [c, txt] = statusOf(p);
    const tags = (p.tags || []).length
      ? p.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')
      : '<span class="cell-dim">—</span>';
    const fp = p.fingerprint && p.fingerprint.screen ? esc(p.fingerprint.screen) : '<span class="cell-dim">未生成</span>';
    const shortKv = kv.length > 24 ? kv.slice(0, 22) + '…' : kv;
    return `<tr data-id="${esc(p.id)}">
      <td><input type="checkbox" class="row-check"></td>
      <td><div class="name-cell">
        <span class="browser-ico">◈</span>
        <button class="play ${p.running ? 'running' : ''}" data-act="${p.running ? 'stop' : 'start'}" title="${p.running ? '停止' : '启动'}">${p.running ? '■' : '▶'}</button>
        <span class="name-txt" title="${esc(p.name)}">${esc(p.name)}</span></div></td>
      <td><span class="status"><span class="dot ${c}"></span>${txt}</span></td>
      <td>${tags}</td>
      <td title="${esc(p.note)}">${p.note ? esc(p.note) : '<span class="cell-dim">—</span>'}</td>
      <td title="${esc(proxyText(p))}">${proxyText(p) === '—' ? '<span class="cell-dim">—</span>' : esc(proxyText(p))}${netLine(p)}</td>
      <td>${fp}</td>
      <td title="${esc(kv)}">${esc(shortKv)}</td>
      <td><button class="info-btn" data-act="edit" title="详情">i</button></td>
    </tr>`;
  }).join('');
  $('row-count').textContent = `${list.length} 行中的 ${S.profiles.length} 行`;
  $('empty').hidden = list.length > 0;
}
function render() {
  renderTabs(); renderRows();
  if (S.editId && !$('drawer').hidden) renderNetInfo(S.profiles.find(x => x.id === S.editId));
}

$('grid-body').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-act]'); if (!btn) return;
  const id = btn.closest('tr').dataset.id, act = btn.dataset.act;
  if (act === 'edit') return openDrawer(id);
  btn.disabled = true;
  try {
    await post(`/api/profiles/${id}/${act}`);
    toast(act === 'start' ? '环境已启动' : '环境已停止');
    setTimeout(loadProfiles, 1200);
  } catch (err) { toast('操作失败：' + err.message, 5000); }
  finally { btn.disabled = false; }
});
$('search').addEventListener('input', e => { S.q = e.target.value.trim(); renderRows(); });
$('btn-sort').onclick = () => { S.asc = !S.asc; $('btn-sort').lastChild.textContent = S.asc ? ' 名称 ↑' : ' 名称 ↓'; renderRows(); };
$('btn-refresh').onclick = () => { loadProfiles(); loadHealth(); };
$('btn-filter').onclick = () => { S.tab = S.tab === '__running' ? '__all' : '__running'; render(); toast(S.tab === '__running' ? '只看运行中' : '显示全部'); };
$('check-all').onchange = e => document.querySelectorAll('.row-check').forEach(c => c.checked = e.target.checked);

async function loadProfiles() {
  try { S.profiles = await api('/api/profiles'); render(); }
  catch (e) { toast('读取环境失败：' + e.message, 5000); }
}
async function loadHealth() {
  try {
    S.health = await api('/api/health');
    if (!S.health.camoufox.ready && !sessionStorage.getItem('ob-shown')) {
      sessionStorage.setItem('ob-shown', '1'); $('onboarding').hidden = false;
    }
  } catch (e) { /* 服务未就绪，下一轮再试 */ }
}

/* ---------------- 编辑抽屉 ---------------- */
function openDrawer(id) {
  S.editId = id || null;
  const p = id ? S.profiles.find(x => x.id === id) : null;
  $('drawer-title').textContent = p ? '编辑环境' : '新建环境';
  $('btn-del').hidden = !p;
  const x = (p && p.proxy) || {};
  $('f-name').value = p ? p.name : '';
  $('f-group').value = p ? (p.group || '') : '';
  $('f-tags').value = p ? (p.tags || []).join(',') : '';
  $('f-note').value = p ? (p.note || '') : '';
  $('f-scheme').value = x.scheme || 'socks5';
  $('f-host').value = x.host || ''; $('f-port').value = x.port || '';
  $('f-user').value = x.username || ''; $('f-pass').value = x.password || '';
  const curLocale = p ? (p.locale || 'en-US') : 'en-US';
  const lsel = $('f-locale');
  const known = [...lsel.options].some(o => o.value === curLocale);
  lsel.value = known ? curLocale : '__custom__';
  $('f-locale-custom').value = known ? '' : curLocale;
  $('f-locale-custom').hidden = known;
  $('f-sw').value = p ? p.screen_w : 1920; $('f-sh').value = p ? p.screen_h : 1080;
  $('f-tz').value = '';
  $('f-url').value = p ? (p.start_url || 'https://www.google.com') : 'https://www.google.com';
  // 系统选项按内核字库实际能力放行；完整 bundle 下 macOS / Linux 都可选
  const allowed = (S.health && S.health.os_supported) || ['windows', 'macos', 'linux'];
  const sel = $('f-os');
  [...sel.options].forEach(o => { o.disabled = !allowed.includes(o.value); });
  sel.value = p ? (p.os_name || 'windows') : 'windows';
  if (!allowed.includes(sel.value)) sel.value = 'windows';
  const hint = $('os-hint');
  hint.hidden = allowed.length >= 3;
  if (allowed.length < 3) hint.textContent = '当前内核是旧版裁剪版（只剩 Windows 字库）。装一次新内核即可解锁 macOS / Linux 身份。';
  renderNetInfo(p);
  $('drawer').hidden = false;
  setTimeout(() => $('f-name').focus(), 50);
}
function closeDrawer() { $('drawer').hidden = true; S.editId = null; }
$('f-locale').onchange = () => {
  const custom = $('f-locale').value === '__custom__';
  $('f-locale-custom').hidden = !custom;
  if (custom) $('f-locale-custom').focus();
};
function formPayload() {
  const lv = $('f-locale').value;
  const locale = lv === '__custom__' ? ($('f-locale-custom').value.trim() || 'en-US') : lv;
  return {
    name: $('f-name').value.trim() || '未命名',
    group: $('f-group').value.trim(),
    tags: $('f-tags').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    note: $('f-note').value.trim(),
    proxy: { scheme: $('f-scheme').value, host: $('f-host').value.trim(), port: $('f-port').value.trim(),
             username: $('f-user').value.trim(), password: $('f-pass').value },
    os_name: $('f-os').value || 'windows', locale,
    screen_w: +$('f-sw').value || 1920, screen_h: +$('f-sh').value || 1080,
    timezone: '', start_url: $('f-url').value.trim() || 'https://www.google.com',
  };
}
$('btn-new').onclick = () => openDrawer(null);
$('drawer-close').onclick = closeDrawer;
$('btn-cancel').onclick = closeDrawer;
$('btn-save').onclick = async () => {
  const btn = $('btn-save'); btn.disabled = true;
  try {
    if (S.editId) await put(`/api/profiles/${S.editId}`, formPayload());
    else await post('/api/profiles', formPayload());
    closeDrawer(); await loadProfiles(); toast('已保存');
  } catch (e) { toast('保存失败：' + e.message, 5000); }
  finally { btn.disabled = false; }
};
$('btn-del').onclick = async () => {
  if (!S.editId || !confirm('确定删除这个环境？浏览器数据会移到回收目录，可手动找回。')) return;
  try { await del(`/api/profiles/${S.editId}`); closeDrawer(); await loadProfiles(); toast('已删除'); }
  catch (e) { toast('删除失败：' + e.message, 5000); }
};

/* ---------------- 浏览器运行时包 ---------------- */
async function loadRuntime() {
  try {
    const s = await api('/api/runtime/status');
    S.runtime = s;
    $('rt-badge').textContent = s.installed ? '已安装' : '未安装';
    $('rt-badge').className = 'badge ' + (s.installed ? 'ok' : 'warn');
    const yes = (b) => `<span class="badge ${b ? 'ok' : 'warn'}">${b ? '就位' : '缺失'}</span>`;
    const m = s.modules || {};
    $('rt-kv').innerHTML = [
      ['状态', s.installed ? '<span class="badge ok">已安装，可启动环境</span>' : '<span class="badge warn">未安装，环境无法启动</span>'],
      ['版本标记', esc(s.tag || '—')],
      ['安装路径', esc(s.path || '—')],
      ['占用体积', s.size_mb ? s.size_mb + ' MB' : '—'],
      ['Playwright', yes(m['playwright/driver/node.exe'])],
      ['Camoufox', yes(m['camoufox/utils.py'])],
      ['指纹模型', yes(m['fpgen/__init__.py'])],
    ].map(([k, v]) => `<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
  } catch (e) { toast('读取运行时状态失败：' + e.message, 5000); }
}
function startRtInstall(localZip) {
  const tag = (S.runtime && S.runtime.tag) || 'runtime-v1';
  if (!localZip && !confirm(
    `即将下载并安装浏览器运行时 ${tag}。\n\n` +
    '· 下载约 70 MB，装到 CamoufoxData\\runtime-extra\\\n' +
    '· 只装一次，之后换 exe 不用重装\n' +
    '· 来源固定为 GitHub Releases 的 runtime-v1，失败不会自动尝试第三方镜像\n' +
    '· 下载代理复用下方「下载代理」设置\n\n确定开始？')) return;
  if (localZip && !confirm(`从本地文件安装浏览器运行时？\n${localZip}\n\n会先校验内容与版本，再替换现有目录。`)) return;
  post('/api/runtime/install', { local_zip: localZip, proxy: $('kernel-proxy').value.trim() })
    .then(() => { $('rt-box').hidden = false; pollRtProgress(); })
    .catch(e => toast('启动安装失败：' + e.message, 6000));
}
function pollRtProgress() {
  clearInterval(S.rtTimer);
  S.rtTimer = setInterval(async () => {
    try {
      const p = await api('/api/runtime/progress');
      $('rt-msg').textContent = p.message || p.phase;
      $('rt-pct').textContent = (p.percent || 0) + '%';
      $('rt-bar').style.width = (p.percent || 0) + '%';
      if (p.phase === 'done' || p.phase === 'error' || (p.phase === 'idle' && p.message)) {
        clearInterval(S.rtTimer); S.rtTimer = null;
        if (p.phase === 'done') { toast('浏览器运行时安装完成'); loadRuntime(); loadKernel(); loadHealth(); }
        else if (p.phase === 'error') toast('安装失败：' + p.error, 8000);
      }
    } catch (e) { clearInterval(S.rtTimer); S.rtTimer = null; }
  }, 700);
}
$('btn-rt-install').onclick = () => startRtInstall('');
$('btn-rt-local').onclick = async () => {
  if (!pj()) return toast('本地文件选择仅在应用程序内可用');
  try {
    const p = await window.pywebview.api.choose_zip();
    if (p) startRtInstall(p);
  } catch (e) { toast('选择文件失败：' + e); }
};
$('btn-rt-cancel').onclick = async () => { await post('/api/runtime/cancel'); toast('已请求取消'); };

/* ---------------- 内核管理 ---------------- */
async function loadKernel() {
  loadRuntime();
  try {
    const s = await api('/api/kernel/status');
    S.kernel = s;
    const yes = (b, t = '就位', f = '缺失') => `<span class="badge ${b ? 'ok' : 'warn'}">${b ? t : f}</span>`;
    const f = s.fonts || { label: '—', files: 0, size_mb: 0 };
    $('kernel-kv').innerHTML = [
      ['当前版本', s.installed ? esc(s.version) : '<span class="badge warn">未安装</span>'],
      ['安装 / 升级时间', esc(s.installed_at || '—')],
      ['距上次升级', esc(s.age_text || '—') + (s.age_days > 0 ? `（${s.age_days} 天）` : '')],
      ['字库', `${esc(f.label)} · ${f.files} 个 / ${f.size_mb} MB`],
      ['升级策略', s.auto_install ? '<span class="badge warn">自动安装</span>' : '<span class="badge ok">只提示，不自动装</span>'],
      ['安装路径', esc(s.path || '—')],
      ['占用体积', s.size_mb ? s.size_mb + ' MB' : '—'],
      ['指纹模型', yes(s.fpgen) + (s.fpgen ? '' : ' <span class="muted">首次启动环境时自动补齐（约 1.5MB）</span>')],
      ['GeoIP 数据库', yes(s.geoip) + (s.geoip ? '' : ' <span class="muted">装内核时顺手补齐（约 50MB）</span>')],
      ['可用磁盘', (s.free_mb / 1024).toFixed(1) + ' GB'],
    ].map(([k, v]) => `<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
  } catch (e) { toast('读取内核状态失败：' + e.message, 5000); }
  checkUpgrade(true);   // 只读查询版本，绝不触发下载
}
function renderRelList(items) {
  $('rel-list').innerHTML = items.length ? items.slice(0, 12).map(x =>
    `<div class="rel-item"><span class="tagname">${esc(x.tag)}</span>
     <span class="meta">${(x.size / 1024 / 1024).toFixed(0)} MB · ${esc((x.published || '').slice(0, 10))}${x.sha256 ? ' · 已签名' : ''}</span>
     <span class="grow"></span><button class="btn small" data-ver="${esc(x.tag)}">安装此版本</button></div>`).join('')
    : '没有找到 Windows x64 内核';
  $('rel-list').querySelectorAll('[data-ver]').forEach(btn => btn.onclick = () => startInstall(btn.dataset.ver, ''));
}
async function checkUpgrade(auto) {
  const badge = $('upgrade-badge'), msg = $('upgrade-msg');
  const btn = $('btn-check-rel');
  badge.textContent = '查询中…';
  if (!auto) { btn.disabled = true; btn.textContent = '查询中…'; $('rel-list').innerHTML = '正在连接官方 Releases…'; }
  try {
    // 只发一次只读 GET，拿版本列表；不会下载任何文件、不会调用 install()
    const r = await api('/api/kernel/releases?proxy=' + encodeURIComponent($('kernel-proxy').value.trim()));
    S.releases = r.items;
    renderRelList(r.items);
    const cur = (S.kernel && S.kernel.version) || '';
    const latest = r.items[0];
    if (!latest) { badge.textContent = '查询完成'; msg.textContent = '官方 Releases 里没有找到 Windows x64 内核。'; }
    else if (!cur) {
      badge.textContent = '需要安装';
      msg.innerHTML = `尚未安装内核。最新版 <b>${esc(latest.tag)}</b>（${(latest.size / 1024 / 1024).toFixed(0)} MB，${esc((latest.published || '').slice(0, 10))}）。到下方列表选择后手动安装。`;
    } else if (latest.name.includes(cur) || latest.tag === cur) {
      badge.textContent = '已是最新';
      msg.textContent = `当前 ${cur} 已是官方最新版本，${S.kernel.age_text || ''}。保持现状即可，不必追 beta 小版本。`;
    } else {
      badge.textContent = '有新版本';
      msg.innerHTML = `官方最新 <b>${esc(latest.tag)}</b>（${esc((latest.published || '').slice(0, 10))}），当前 <b>${esc(cur)}</b>，${esc(S.kernel.age_text || '')}。<br>`
        + `这里<b>只提示、不会自动下载</b>。需要升级请在下方列表点「安装此版本」并二次确认；不升也不影响使用。`;
    }
  } catch (e) {
    badge.textContent = '查询失败';
    msg.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span><br>仅影响版本提示，不影响已安装内核。可重试、填写代理，或用「指定本地 zip」离线安装。`;
  } finally {
    if (!auto) { btn.disabled = false; btn.textContent = '检查新版本'; }
  }
}
$('btn-check-rel').onclick = () => checkUpgrade(false);
$('btn-local-zip').onclick = async () => {
  if (!pj()) return toast('本地文件选择仅在应用程序内可用');
  try {
    const p = await window.pywebview.api.choose_zip();
    if (p) startInstall('', p);
  } catch (e) { toast('选择文件失败：' + e); }
};
async function startInstall(version, localZip) {
  if (!localZip && !confirm(
    `即将下载并安装内核 ${version || '（最新版）'}。\n\n` +
    '· 下载约 1,259 MB，峰值需要约 3.8GB 可用磁盘\n' +
    '· 保留完整三系统字库（不裁剪），最终占用约 2.5GB\n' +
    '· 只从官方 Releases 拉取，失败不会自动尝试第三方镜像\n' +
    '· 更换内核会改变 UA 版本等运行时读数，已登录账号可能触发验证\n' +
    '· 旧版保留备份，可在「回滚旧版」一键退回\n\n确定开始？')) return;
  if (localZip && !confirm(`从本地文件安装内核？\n${localZip}\n\n会先校验内容再替换现有内核（旧版保留备份）。`)) return;
  try {
    await post('/api/kernel/install', { version, local_zip: localZip, proxy: $('kernel-proxy').value.trim() });
    $('dl-box').hidden = false; pollProgress();
  } catch (e) { toast('启动安装失败：' + e.message, 6000); }
}
function pollProgress() {
  clearInterval(S.dlTimer);
  S.dlTimer = setInterval(async () => {
    try {
      const p = await api('/api/kernel/progress');
      $('dl-msg').textContent = p.message || p.phase;
      $('dl-pct').textContent = (p.percent || 0) + '%';
      $('dl-bar').style.width = (p.percent || 0) + '%';
      if (p.phase === 'done' || p.phase === 'error' || (p.phase === 'idle' && p.message)) {
        clearInterval(S.dlTimer); S.dlTimer = null;
        if (p.phase === 'done') { toast('内核安装完成'); loadKernel(); loadHealth(); }
        else if (p.phase === 'error') toast('安装失败：' + p.error, 8000);
      }
    } catch (e) { clearInterval(S.dlTimer); S.dlTimer = null; }
  }, 700);
}
$('btn-cancel-dl').onclick = async () => { await post('/api/kernel/cancel'); toast('已请求取消'); };
$('btn-rollback').onclick = async () => {
  if (!confirm('回滚到上一个内核版本？当前版本会被移到回收目录。')) return;
  try { const r = await post('/api/kernel/rollback'); toast('已回滚到 ' + r.version); loadKernel(); }
  catch (e) { toast('回滚失败：' + e.message, 5000); }
};

/* ---------------- 自动化 / 日志 / 设置 ---------------- */
async function copyText(text) {
  if (!text) { toast('没有可复制的内容'); return; }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast('已复制');
      return;
    }
  } catch (_) { /* WebView2 某些环境不给剪贴板权限，走下面的兜底 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    toast(ok ? '已复制' : '复制失败，请手动选中文本');
  } catch (e) { toast('复制失败，请手动选中文本'); }
}

async function loadAutomation() {
  try {
    const a = await api('/api/automation');
    const rows = [
      ['服务地址', a.base_url], ['端口', a.port], ['令牌', a.token],
      ['发现文件', a.runtime_file], ['curl 示例', a.curl], ['CLI 示例', a.cli],
    ];
    $('auto-card').innerHTML = `<h3>连接信息</h3>` + rows.map(([k, v]) =>
      `<div class="auto-row"><span class="k">${esc(k)}</span><code>${esc(v)}</code>
       <button class="btn small" data-copy="${esc(v)}">复制</button></div>`).join('') +
      `<h3 style="margin-top:20px">可用接口</h3>` + a.endpoints.map(x => `<div class="auto-row"><code>${esc(x)}</code></div>`).join('');
    $('auto-card').querySelectorAll('[data-copy]').forEach(b => b.onclick = () => copyText(b.dataset.copy));
  } catch (e) { toast('读取失败：' + e.message); }
}
let LOG_PATH = '';
async function loadLogs() {
  try {
    const r = await api('/api/logs');
    $('log-text').textContent = r.log || '（暂无日志）';
    LOG_PATH = r.path || '';
  } catch (e) { $('log-text').textContent = '读取失败：' + e.message; }
}
$('btn-log-refresh').onclick = loadLogs;
$('btn-log-copy').onclick = () => copyText($('log-text').textContent);
$('btn-log-path').onclick = () => copyText(LOG_PATH);

function syncSettings() {
  applyTheme(S.settings.theme || 'dark');
  document.querySelectorAll('#seg-close button').forEach(b => b.classList.toggle('active', b.dataset.v === (S.settings.close_action || 'tray')));
  $('set-autostart').checked = !!S.settings.autostart;
  $('set-proxy').value = S.settings.download_proxy || '';
  $('kernel-proxy').value = S.settings.download_proxy || '';
}
$('seg-theme').onclick = e => { const b = e.target.closest('[data-v]'); if (b) { S.settings.theme = b.dataset.v; applyTheme(b.dataset.v); } };
$('seg-close').onclick = e => { const b = e.target.closest('[data-v]'); if (b) { S.settings.close_action = b.dataset.v; syncSettings(); } };
$('btn-save-settings').onclick = async () => {
  const patch = { theme: S.settings.theme, close_action: S.settings.close_action,
                  autostart: $('set-autostart').checked, download_proxy: $('set-proxy').value.trim() };
  try {
    S.settings = await put('/api/settings', patch);
    $('kernel-proxy').value = patch.download_proxy;
    $('settings-tip').textContent = '已保存';
    if (pj()) { try { await window.pywebview.api.apply_autostart(patch.autostart); } catch (_) {} }
    setTimeout(() => $('settings-tip').textContent = '', 2500);
  } catch (e) { toast('保存失败：' + e.message); }
};

/* ---------------- 窗口按钮 / 引导 ---------------- */
$('win-min').onclick = () => winAction('minimize');
$('win-max').onclick = () => winAction('maximize');
$('win-close').onclick = () => winAction('close');
$('btn-help').onclick = () => toast('环境独立 Profile + 锁定指纹 + 独立代理；关闭窗口默认缩到系统托盘');
$('ob-later').onclick = () => $('onboarding').hidden = true;
$('ob-go').onclick = () => { $('onboarding').hidden = true; setView('kernel'); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeDrawer(); $('onboarding').hidden = true; } });

/* ---------------- 启动 ---------------- */
(async function init() {
  try { S.settings = await api('/api/settings'); } catch (_) {}
  syncSettings();
  await Promise.all([loadProfiles(), loadHealth()]);
  setView('profiles');
  // 环境启停状态定期刷新（关闭浏览器后能自动变回「就绪」）
  setInterval(loadProfiles, 6000);
})();
