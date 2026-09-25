/* ============================================================
   前端权限控制（文档 §4「唯一权限定义源」在前端的投影）
   ------------------------------------------------------------
   原则：
     · 权限清单来自 `/api/auth/me`（服务端返回，与后端同源）
     · 本模块只负责**隐藏入口**，改善体验
     · ⚠️ 真正的拦截在服务端 —— 隐藏菜单不等于安全（文档 §2.4）
     · 文档 §6：无权限、会话过期、服务异常要给**不同**提示，
       不得统一表现为「菜单消失」或空白页
   ============================================================ */
(() => {
  'use strict';

  // 按钮 id → 所需权限
  const BUTTON_RULES = {
    // 批次与生成（消耗付费额度）
    'btn-run': 'batch.create',
    'btn-new-batch': 'batch.create',
    'btn-stop': 'batch.create',
    'btn-local-validation-run': 'batch.create',
    'btn-professional-run': 'batch.create',
    'btn-quality-confirm-run': 'batch.create',
    'btn-openai-house-regenerate': 'batch.create',

    // 效果图
    'btn-generate-effects': 'effect.generate',
    'btn-gen-ai-background': 'effect.background.manage',
    'btn-effect-preview': 'effect.params.manage',
    'btn-effect-save': 'effect.params.manage',
    'btn-effect-reset': 'effect.params.manage',
    'effect-background-upload': 'effect.background.manage',

    // 配置
    'btn-save-settings': 'settings.model.manage',
    'btn-deepseek-test': 'settings.model.manage',
    'btn-validate-path': 'settings.path.manage',
    'btn-open-dir': 'settings.path.manage',
    'btn-quality-optimize': 'prompt.template.manage',

    // 导出
    'btn-export-titles': 'batch.export',
    'btn-export-checklist': 'batch.export',
    'btn-export-package': 'batch.export',
  };

  // 需要整块隐藏的区域（父容器 id → 权限）
  const SECTION_RULES = {
    'effect-background-assets': 'effect.read',
  };

  let PERMISSIONS = [];
  let READY = false;

  const has = (code) => PERMISSIONS.includes(code);
  const isAdmin = () => has('system.user.manage');

  /** 记录被隐藏的入口，供「为什么没有这个按钮」提示用 */
  const HIDDEN = [];

  function applyRules() {
    HIDDEN.length = 0;

    Object.entries(BUTTON_RULES).forEach(([id, perm]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (has(perm)) {
        el.hidden = false;
        el.removeAttribute('aria-disabled');
        el.title = el.dataset.origTitle || el.title || '';
      } else {
        el.hidden = true;
        // 保留原因，便于排查「按钮去哪了」
        el.dataset.hiddenBy = perm;
        HIDDEN.push({ id, perm });
      }
    });

    Object.entries(SECTION_RULES).forEach(([id, perm]) => {
      const el = document.getElementById(id);
      if (el && !has(perm)) el.hidden = true;
    });

    // 设置了 hidden 的 label 需要连父级一起处理（上传控件）
    document.querySelectorAll('label.reference-drop').forEach((label) => {
      const input = label.querySelector('input[type=file]');
      if (!input) return;
      const perm = BUTTON_RULES[input.id];
      if (perm && !has(perm)) label.hidden = true;
    });
  }

  /**
   * 统一的 API 调用包装：自动识别 401/403 并给出**区分性**提示（文档 §6）。
   * 业务代码可选使用；不强制。
   */
  async function guardedFetch(path, options = {}) {
    const res = await fetch(path, { credentials: 'same-origin', ...options });
    if (res.status === 401) {
      if (window.__AUTH__ && window.__AUTH__.onSessionExpired) {
        window.__AUTH__.onSessionExpired();
      }
      throw Object.assign(new Error('登录已过期'), { status: 401 });
    }
    if (res.status === 403) {
      let need = '';
      try {
        const j = await res.json();
        need = j.required || '';
      } catch (_) { /* 忽略解析失败 */ }
      if (window.toast) {
        window.toast(need ? `没有权限执行该操作（需要 ${need}）` : '没有权限执行该操作');
      }
      throw Object.assign(new Error('无权限'), { status: 403, required: need });
    }
    return res;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function boot(detail) {
    PERMISSIONS = (detail && detail.permissions) || [];
    READY = true;
    applyRules();
    // 供业务代码查询
    window.__PERMS__ = {
      has,
      isAdmin,
      all: () => PERMISSIONS.slice(),
      hidden: () => HIDDEN.slice(),
      apply: applyRules,
      fetch: guardedFetch,
    };
  }

  // login.js 在确认已登录后派发 auth:ready
  window.addEventListener('auth:ready', (ev) => boot(ev.detail));

  // 若事件已错过（脚本加载顺序问题），主动补一次
  if (window.__AUTH__) {
    window.__AUTH__.refresh().then((st) => {
      if (st && st.authenticated) boot(st);
    }).catch(() => { /* 未登录时忽略 */ });
  }
})();
