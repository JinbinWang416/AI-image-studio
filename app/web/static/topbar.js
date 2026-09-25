/* ============================================================
   顶栏交互：系统管理菜单 + 用户菜单 + 设置齿轮
   ------------------------------------------------------------
   设计依据（平台安全开发提示词）：
     §4   菜单项按权限动态生成，权限清单来自 /api/auth/me（与后端同源）
     §6   无权限、会话过期、服务异常给出**不同**提示
     §3.9 MFA 启用/关闭
     §3.12 用户可查看并撤销自己的登录设备
     §3.13 改密码后其它会话失效
   ============================================================ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  let ME = { user: null, permissions: [] };
  let busy = false;

  async function api(path, options = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    if (res.status === 401) {
      if (window.__AUTH__ && window.__AUTH__.onSessionExpired) {
        window.__AUTH__.onSessionExpired();
      }
      throw Object.assign(new Error('登录已过期，请重新登录'), { status: 401 });
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* 可能无 body */ }
    if (!res.ok) {
      throw Object.assign(new Error((data && data.detail) || `HTTP ${res.status}`), {
        status: res.status,
      });
    }
    return data;
  }

  const can = (code) => (ME.permissions || []).includes(code);
  const canAny = (codes) => codes.some(can);

  // ---------------------------------------------------------------- 菜单开合
  function closeAll(except) {
    document.querySelectorAll('.dropdown .menu').forEach((m) => {
      if (m !== except) { m.hidden = true; }
    });
    document.querySelectorAll('.account-btn[aria-expanded]').forEach((b) => {
      const menu = b.parentElement.querySelector('.menu');
      b.setAttribute('aria-expanded', String(Boolean(menu && !menu.hidden)));
    });
    const anyOpen = [...document.querySelectorAll('.dropdown .menu')].some((m) => !m.hidden);
    const bd = $('menu-backdrop');
    if (bd) bd.hidden = !anyOpen;
  }

  function toggle(menuId, btnId) {
    const menu = $(menuId);
    if (!menu) return;
    const willOpen = menu.hidden;
    closeAll(menu);
    menu.hidden = !willOpen;
    const btn = $(btnId);
    if (btn) btn.setAttribute('aria-expanded', String(willOpen));
    const bd = $('menu-backdrop');
    if (bd) bd.hidden = !willOpen;
  }

  // ---------------------------------------------------------------- 系统管理
  const SYSTEM_ITEMS = [
    { perm: 'system.user.manage',   href: '/admin#users',    ico: '👥', label: '用户管理' },
    { perm: 'system.role.manage',   href: '/admin#roles',    ico: '🔑', label: '角色与权限' },
    { perm: 'system.session.manage', href: '/admin#sessions', ico: '🖥', label: '会话监控' },
    { perm: 'system.audit.read',    href: '/admin#audit',    ico: '📋', label: '审计中心' },
    { perm: 'system.health.read',   href: '/admin#health',   ico: '💚', label: '系统健康' },
  ];

  function renderSystemMenu() {
    const dd = $('dd-system');
    const menu = $('menu-system');
    const divider = $('acct-divider');
    if (!dd || !menu) return;

    const visible = SYSTEM_ITEMS.filter((it) => can(it.perm));
    if (!visible.length) {
      // 没有任何管理权限 → 整个入口不显示（§6：不显示无权限入口）
      dd.hidden = true;
      if (divider) divider.hidden = true;
      return;
    }
    dd.hidden = false;
    if (divider) divider.hidden = false;

    menu.innerHTML =
      '<div class="mnote">后台管理入口（仅显示你有权限的部分）</div>'
      + visible.map((it) => `
        <a class="mi" href="${esc(it.href)}" role="menuitem">
          <span class="ico">${it.ico}</span>${esc(it.label)}
        </a>`).join('')
      + '<div class="sep"></div>'
      + `<a class="mi" href="/admin" role="menuitem"><span class="ico">⚙️</span>管理后台首页</a>`;
  }

  // ---------------------------------------------------------------- 用户菜单
  function roleTags() {
    const roles = (ME.user && ME.user.roles) || [];
    return roles.map((r) => `<span class="tag role">${esc(r)}</span>`).join('');
  }

  function userMenuHome() {
    const menu = $('menu-user');
    if (!menu || !ME.user) return;
    menu.innerHTML = `
      <div class="mh">
        <div class="nm">${esc(ME.user.display_name)}</div>
        <div class="mnote" style="padding:2px 0 0">${esc(ME.user.login_name)}</div>
        <div class="rl">${roleTags()}</div>
      </div>
      <button class="mi" data-act="sessions" role="menuitem">
        <span class="ico">🖥</span>我的登录设备</button>
      <button class="mi" data-act="password" role="menuitem">
        <span class="ico">🔒</span>修改密码</button>
      <button class="mi" data-act="mfa" role="menuitem">
        <span class="ico">🛡</span>动态验证码
        <span class="hint">${ME.user.mfa_enabled ? '已启用' : '未启用'}</span></button>
      <div class="sep"></div>
      <button class="mi danger" data-act="logout" role="menuitem">
        <span class="ico">🚪</span>退出登录</button>`;

    menu.querySelectorAll('[data-act]').forEach((b) => {
      b.addEventListener('click', () => onUserAction(b.dataset.act));
    });
  }

  function menuMsg(text, ok = true) {
    const menu = $('menu-user');
    if (!menu) return;
    let el = menu.querySelector('.mmsg');
    if (!el) {
      el = document.createElement('div');
      el.className = 'mmsg';
      menu.appendChild(el);
    }
    el.className = 'mmsg ' + (ok ? 'ok' : 'err');
    el.textContent = text;
  }

  async function onUserAction(act) {
    if (busy) return;
    try {
      if (act === 'logout') {
        await api('/api/auth/logout', { method: 'POST' });
        location.reload();
      } else if (act === 'sessions') {
        await showSessions();
      } else if (act === 'password') {
        showPasswordForm();
      } else if (act === 'mfa') {
        await showMfa();
      }
    } catch (e) {
      if (e.status !== 401) menuMsg(e.message, false);
    }
  }

  async function showSessions() {
    const menu = $('menu-user');
    menu.innerHTML = '<div class="mnote">正在读取…</div>';
    const d = await api('/api/auth/sessions');
    menu.innerHTML = `
      <div class="mh"><div class="nm">我的登录设备</div>
        <div class="mnote" style="padding:2px 0 0">撤销后该设备需重新登录</div></div>
      <div class="msess">${(d.sessions || []).map((s) => `
        <div class="it">
          <span class="u">
            <b>${esc(s.ip || '未知来源')}</b>
            ${s.current ? '<span class="now">当前</span>' : ''}
            <div class="a">${esc((s.last_seen_at || '').replace('T', ' ').slice(0, 16))}</div>
            <div class="a">${esc((s.user_agent || '').slice(0, 34))}</div>
          </span>
          ${s.current ? '' : `<button class="btn sm danger" data-sid="${esc(s.id)}">撤销</button>`}
        </div>`).join('') || '<div class="mnote">没有其它设备</div>'}</div>
      <div class="sep"></div>
      <button class="mi" data-back="1"><span class="ico">←</span>返回</button>`;

    menu.querySelectorAll('[data-sid]').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          await api(`/api/auth/sessions/${b.dataset.sid}`, { method: 'DELETE' });
          await showSessions();
        } catch (e) { menuMsg(e.message, false); }
      });
    });
    bindBack(menu);
  }

  function showPasswordForm() {
    const menu = $('menu-user');
    menu.innerHTML = `
      <div class="mh"><div class="nm">修改密码</div>
        <div class="mnote" style="padding:2px 0 0">修改后其它设备上的登录会失效</div></div>
      <div class="mform">
        <label>原密码</label>
        <input type="password" id="pw-old" autocomplete="current-password">
        <label>新密码（≥10 位，含大小写/数字/符号三类）</label>
        <input type="password" id="pw-new" autocomplete="new-password">
        <label>确认新密码</label>
        <input type="password" id="pw-new2" autocomplete="new-password">
        <div class="row2">
          <button class="btn" id="pw-go" style="flex:1">保存</button>
          <button class="btn ghost" data-back="1">取消</button>
        </div>
      </div>`;

    $('pw-go').addEventListener('click', async () => {
      const oldPw = $('pw-old').value;
      const newPw = $('pw-new').value;
      if (newPw !== $('pw-new2').value) { menuMsg('两次输入的新密码不一致', false); return; }
      busy = true;
      try {
        await api('/api/auth/password', {
          method: 'POST',
          body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
        });
        menuMsg('密码已修改，其它设备已退出登录');
      } catch (e) {
        menuMsg(e.message, false);
      } finally {
        busy = false;
      }
    });
    bindBack(menu);
  }

  async function showMfa() {
    const menu = $('menu-user');
    if (ME.user.mfa_enabled) {
      menu.innerHTML = `
        <div class="mh"><div class="nm">动态验证码</div>
          <div class="mnote" style="padding:2px 0 0">当前已启用</div></div>
        <div class="mform">
          <label>输入一次验证码以关闭</label>
          <input id="mfa-code" inputmode="numeric" maxlength="6" placeholder="6 位数字">
          <div class="row2">
            <button class="btn danger" id="mfa-off" style="flex:1">关闭</button>
            <button class="btn ghost" data-back="1">返回</button>
          </div>
        </div>`;
      $('mfa-off').addEventListener('click', async () => {
        try {
          await api('/api/auth/mfa/disable', {
            method: 'POST', body: JSON.stringify({ code: $('mfa-code').value.trim() }),
          });
          ME.user.mfa_enabled = false;
          userMenuHome();
          menuMsg('已关闭动态验证码');
        } catch (e) { menuMsg(e.message, false); }
      });
    } else {
      const d = await api('/api/auth/mfa/setup', { method: 'POST' });
      menu.innerHTML = `
        <div class="mh"><div class="nm">启用动态验证码</div>
          <div class="mnote" style="padding:2px 0 0">用认证器 App（Google Authenticator / 微软验证器）扫码或手动录入</div></div>
        <div class="mnote">密钥：</div>
        <div class="msecret">${esc(d.secret)}</div>
        <div class="mform">
          <label>录入后输入一次 6 位验证码以确认</label>
          <input id="mfa-code" inputmode="numeric" maxlength="6" placeholder="6 位数字">
          <div class="row2">
            <button class="btn" id="mfa-on" style="flex:1">启用</button>
            <button class="btn ghost" data-back="1">取消</button>
          </div>
        </div>`;
      $('mfa-on').addEventListener('click', async () => {
        try {
          await api('/api/auth/mfa/enable', {
            method: 'POST', body: JSON.stringify({ code: $('mfa-code').value.trim() }),
          });
          ME.user.mfa_enabled = true;
          userMenuHome();
          menuMsg('已启用动态验证码');
        } catch (e) { menuMsg(e.message, false); }
      });
    }
    bindBack(menu);
  }

  function bindBack(menu) {
    menu.querySelectorAll('[data-back]').forEach((b) => {
      b.addEventListener('click', () => userMenuHome());
    });
  }

  // ---------------------------------------------------------------- 设置
  function hookSettings() {
    const btn = $('btn-settings');
    if (!btn) return;
    // 原生 app.js 会绑定自己的处理；这里只补充无障碍属性
    btn.setAttribute('aria-label', '设置');
  }

  // ---------------------------------------------------------------- 启动
  //
  // ⚠️ init 必须**只执行一次**。
  //    下面有两个触发点（login.js 的 auth:ready 事件 + 主动 refresh），
  //    早期没有去重，导致 init 跑两遍 → 按钮上绑了两个 click 监听 →
  //    点一下执行两次 toggle（打开又立刻关闭），表现为「点菜单没反应」。
  let INITED = false;

  async function init() {
    if (INITED) return;
    INITED = true;

    try {
      ME = await api('/api/auth/me');
    } catch (_) {
      INITED = false;   // 未登录时允许后续重试
      return;
    }

    const nameEl = $('user-name');
    if (nameEl) nameEl.textContent = ME.user.display_name;

    renderSystemMenu();
    userMenuHome();
    hookSettings();

    const sysBtn = $('btn-system');
    if (sysBtn) sysBtn.addEventListener('click', (e) => {
      e.stopPropagation(); toggle('menu-system', 'btn-system');
    });
    const userBtn = $('btn-user');
    if (userBtn) userBtn.addEventListener('click', (e) => {
      e.stopPropagation(); toggle('menu-user', 'btn-user');
    });

    const bd = $('menu-backdrop');
    if (bd) bd.addEventListener('click', () => closeAll());

    // ---------------------------------------------------------------- 菜单导航
    //
    // ⚠️ 实测踩到的坑：菜单项是 `<a href="/admin#users">`，但点击**不跳转**。
    //    排查过程：
    //      · hit-test 确认菜单项在最上层（层叠上下文问题已单独修复）
    //      · 无 preventDefault / 无全局 click 拦截
    //      · 但 `.click()` 与合成 MouseEvent 都不触发导航
    //      · 直接 `location.href = '/admin#audit'` 却完全正常
    //
    //    与其继续追浏览器默认行为的差异，这里改为**显式导航** ——
    //    不管默认行为为何失效，显式赋值 location 一定生效，且行为可预测。
    document.addEventListener('click', (e) => {
      const link = e.target && e.target.closest
        ? e.target.closest('#menu-system a.mi, #menu-user a.mi')
        : null;
      if (!link) return;
      const href = link.getAttribute('href');
      if (!href) return;
      e.preventDefault();
      closeAll();
      location.href = href;
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeAll();
    });

    // 供 permissions-ui.js 复用同一份权限数据
    window.__ME__ = ME;
  }

  window.addEventListener('auth:ready', init);
  if (window.__AUTH__) {
    window.__AUTH__.refresh().then((st) => { if (st && st.authenticated) init(); }).catch(() => {});
  }
})();
