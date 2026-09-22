/* ============================================================================
   设置弹窗交互统一（2026-09-20）
   ----------------------------------------------------------------------------
   与 settings-unify.css 配套，处理 CSS 管不了的几件事：

     1. 切换面板时**重置内容区滚动位置**
        （从"模型服务"滚到底部后切到"生成参数"，仍停在底部 → 看起来像空白）

     2. 键盘可达性：左栏导航支持上下键切换

     3. 面板切换后**聚焦第一个可输入元素**，方便键盘用户

     4. 在底部提示当前面板名，避免"我改的是哪一页"的困惑
   ============================================================================ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  function contentEl() {
    return document.querySelector('#settings-modal .settings-content');
  }

  function currentPane() {
    const items = [...document.querySelectorAll('.settings-nav-item')];
    const active = items.find((el) => el.classList.contains('active'));
    return active ? active.dataset.pane : '';
  }

  function paneTitle(pane) {
    const sec = document.querySelector(`.settings-pane[data-pane="${pane}"]:not([hidden])`);
    const h = sec && sec.querySelector('h3');
    return h ? h.textContent.replace(/\s+/g, ' ').trim() : pane;
  }

  /** 更新底部栏的面板提示 */
  function syncFoot() {
    const el = $('settings-foot-pane');
    if (!el) return;
    const pane = currentPane();
    el.textContent = pane ? `当前：${paneTitle(pane)}` : '';
  }

  /** 切换后：滚回顶部 + 聚焦首个输入 */
  function afterPaneSwitch() {
    const box = contentEl();
    if (box) box.scrollTop = 0;

    syncFoot();

    // 聚焦面板里第一个可见的输入控件（跳过只读与禁用）
    const pane = currentPane();
    const sec = [...document.querySelectorAll(`.settings-pane[data-pane="${pane}"]`)]
      .find((s) => !s.hidden);
    if (!sec) return;
    const target = sec.querySelector(
      'input:not([type=hidden]):not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled])'
    );
    if (target && typeof target.focus === 'function') {
      // 用 preventScroll 避免把刚重置的滚动位置又带走
      try { target.focus({ preventScroll: true }); } catch (_) { /* 老浏览器忽略 */ }
    }
  }

  // ---------------------------------------------------------------- 监听切换
  function init() {
    const nav = $('settings-nav');
    const box = contentEl();
    if (!nav || !box) return;

    // 点击导航 → 等 app.js 处理完再调整
    nav.addEventListener('click', () => setTimeout(afterPaneSwitch, 0));

    // 键盘上下键切换
    nav.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const items = [...nav.querySelectorAll('.settings-nav-item')];
      const idx = items.findIndex((el) => el === document.activeElement);
      if (idx < 0) return;
      e.preventDefault();
      const next = e.key === 'ArrowDown'
        ? items[Math.min(items.length - 1, idx + 1)]
        : items[Math.max(0, idx - 1)];
      next.focus();
      next.click();
    });

    // 打开弹窗时也同步一次
    const modal = $('settings-modal');
    if (modal) {
      new MutationObserver(() => {
        if (modal.classList.contains('show')) setTimeout(afterPaneSwitch, 60);
      }).observe(modal, { attributes: true, attributeFilter: ['class'] });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
