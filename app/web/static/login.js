/* ============================================================
   登录 / 首次初始化 界面
   ------------------------------------------------------------
   设计依据（平台安全开发提示词）：
     §6  无权限、登录过期、服务异常应显示**不同**提示，
         不得统一表现为菜单消失或空白页
     §3.8 登录失败提示不区分「账号不存在」与「密码错误」
     §3.9 管理员需支持动态验证码（MFA）
     §3.11 使用安全 Cookie（由后端设置，前端不接触 token）
   ============================================================ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  let AUTH_STATE = { needsSetup: false, authenticated: false, user: null, permissions: [] };
  let MFA_REQUIRED = false;

  // ---------------------------------------------------------------- 请求
  async function api(path, options = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || `请求失败（HTTP ${res.status}）`;
      const err = new Error(msg);
      err.status = res.status;
      err.code = data && data.code;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- 状态
  async function refreshStatus() {
    try {
      AUTH_STATE = await api('/api/auth/status');
      return AUTH_STATE;
    } catch (e) {
      // §6：服务异常要与「未登录」区分开
      showFatal('无法连接服务', e.message);
      return null;
    }
  }

  // ---------------------------------------------------------------- 界面
  function panel(html) {
    $('login-body').innerHTML = html;
  }

  function showFatal(title, detail) {
    $('login-overlay').hidden = false;
    $('login-title').textContent = title;
    panel(`<p class="login-error">${escapeHtml(detail)}</p>
      <button class="btn primary" id="btn-retry">重试</button>`);
    const b = $('btn-retry');
    if (b) b.addEventListener('click', () => location.reload());
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function showSetup() {
    $('login-overlay').hidden = false;
    $('login-title').textContent = '首次使用 · 创建管理员';
    panel(`
      <p class="login-hint">
        这是第一次启动。请创建一个管理员账号 —— 之后所有操作都需要登录。
        创建后，现有的历史批次会自动归到该账号名下。
      </p>
      <label class="login-field">
        <span>登录名</span>
        <input id="setup-login" type="text" autocomplete="username"
               placeholder="小写字母、数字、点、下划线，3~32 位">
      </label>
      <label class="login-field">
        <span>显示姓名</span>
        <input id="setup-name" type="text" placeholder="例如：张经理">
      </label>
      <label class="login-field">
        <span>密码</span>
        <input id="setup-pw" type="password" autocomplete="new-password"
               placeholder="至少 10 位，含大小写/数字/符号中的三类">
      </label>
      <label class="login-field">
        <span>确认密码</span>
        <input id="setup-pw2" type="password" autocomplete="new-password">
      </label>
      <button class="btn primary login-submit" id="btn-setup">创建管理员并进入</button>
      <p class="login-error" id="setup-error" hidden></p>
    `);

    const submit = async () => {
      const login = $('setup-login').value.trim();
      const name = $('setup-name').value.trim();
      const pw = $('setup-pw').value;
      const pw2 = $('setup-pw2').value;
      const err = $('setup-error');
      err.hidden = true;

      if (pw !== pw2) {
        err.textContent = '两次输入的密码不一致';
        err.hidden = false;
        return;
      }
      $('btn-setup').disabled = true;
      try {
        await api('/api/auth/setup', {
          method: 'POST',
          body: JSON.stringify({ login_name: login, display_name: name || login, password: pw }),
        });
        location.reload();
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
        $('btn-setup').disabled = false;
      }
    };

    $('btn-setup').addEventListener('click', submit);
    ['setup-login', 'setup-name', 'setup-pw', 'setup-pw2'].forEach((id) => {
      $(id).addEventListener('keydown', (ev) => { if (ev.key === 'Enter') submit(); });
    });
    $('setup-login').focus();
  }

  function showLogin(prefill = '') {
    $('login-overlay').hidden = false;
    $('login-title').textContent = '登录';
    panel(`
      <p class="login-hint">请使用管理员分配的账号登录。</p>
      <label class="login-field">
        <span>登录名</span>
        <input id="login-name" type="text" autocomplete="username" value="${escapeHtml(prefill)}">
      </label>
      <label class="login-field">
        <span>密码</span>
        <input id="login-pw" type="password" autocomplete="current-password">
      </label>
      <label class="login-field" id="mfa-field" hidden>
        <span>动态验证码</span>
        <input id="login-mfa" type="text" inputmode="numeric" maxlength="6"
               placeholder="认证器 App 上的 6 位数字">
      </label>
      <button class="btn primary login-submit" id="btn-login">登录</button>
      <p class="login-error" id="login-error" hidden></p>
    `);

    const submit = async () => {
      const err = $('login-error');
      err.hidden = true;
      const body = {
        login_name: $('login-name').value.trim(),
        password: $('login-pw').value,
        mfa_code: MFA_REQUIRED ? $('login-mfa').value.trim() : '',
      };
      $('btn-login').disabled = true;
      try {
        const res = await api('/api/auth/login', { method: 'POST', body: JSON.stringify(body) });
        if (res && res.needs_mfa) {
          MFA_REQUIRED = true;
          $('mfa-field').hidden = false;
          $('btn-login').disabled = false;
          $('login-mfa').focus();
          return;
        }
        location.reload();
      } catch (e) {
        // §3.8：后端已做防枚举，这里原样展示即可
        err.textContent = e.message;
        err.hidden = false;
        $('btn-login').disabled = false;
      }
    };

    $('btn-login').addEventListener('click', submit);
    ['login-name', 'login-pw', 'login-mfa'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') submit(); });
    });
    const focusEl = prefill ? $('login-pw') : $('login-name');
    if (focusEl) focusEl.focus();
  }

  // ---------------------------------------------------------------- 启动
  window.__AUTH__ = {
    state: () => AUTH_STATE,
    /** 业务前端用这个判断「有没有权限」，据此渲染导航（§4 唯一来源） */
    can: (code) => AUTH_STATE.permissions.includes(code),
    /** 会话过期时由业务前端调用（§6 区分提示） */
    onSessionExpired: () => {
      showLogin(AUTH_STATE.user ? AUTH_STATE.user.login_name : '');
      const err = $('login-error');
      if (err) {
        err.textContent = '登录已过期，请重新登录';
        err.hidden = false;
      }
    },
    refresh: refreshStatus,
  };

  async function boot() {
    const st = await refreshStatus();
    if (!st) return;

    if (st.needs_setup) { showSetup(); return; }
    if (!st.authenticated) { showLogin(); return; }

    // 已登录：隐藏遮罩，放行业务界面
    $('login-overlay').hidden = true;
    document.body.classList.add('authenticated');
    window.dispatchEvent(new CustomEvent('auth:ready', { detail: st }));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
