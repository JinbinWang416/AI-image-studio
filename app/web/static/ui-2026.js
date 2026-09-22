/* ============================================================================
   UI 优化增强层（2026-09-20）
   ----------------------------------------------------------------------------
   设计取舍：**不修改 app.js**，改用 MutationObserver 在渲染后「增强」DOM。

   理由：app.js 有 1900+ 行且承载业务逻辑，直接改它风险高、回归面大。
   本层只做「呈现增强」——角标、徽章、折叠、去重——不改任何数据流。

   实现的能力（对应规划编号）：
     P1  运行状态 chip            ← #run-status
     P2  模拟背景徽章             ← #badge-bg-mode（第一重）
     P3  批次号 + ⧉ 复制、移除长路径 ← #batch-chip / #meta
     P4  主数值放大               ← #counts（CSS 负责）
     P5  视图标签带数量、去重      ← #cnt-generated / #cnt-effect
     P9  日志区默认收起 + 报错自动展开 ← #logpanel / #log
     P11 图片序号 + 状态角标       ← .thumb .tn / .ts
     P14 模拟背景角标（第二、三重）  ← .thumb .tm / .store-card .bg-tag
   ============================================================================ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s);

  const LOG_EXPANDED_KEY = 'dsh.logExpanded';

  let lastOutputRoot = '';     // 完整输出路径（供复制）
  let simBackground = false;   // 当前是否使用模拟门店背景

  // ================================================================ 工具
  function parseState() {
    // 从 DOM 文案推断运行状态（不额外发请求，避免与 app.js 打架）
    const stop = $('btn-stop');
    const run = $('btn-run');
    const bar = $('bar');
    const pct = $('pct');

    const running = Boolean(stop && !stop.disabled);
    const pctText = pct ? pct.textContent.trim() : '0%';
    const pctNum = parseFloat(pctText) || 0;

    if (running) return { kind: 'running', text: `进行中 ${pctText}` };
    if (pctNum >= 100) return { kind: 'done', text: '已完成' };
    if (pctNum > 0) return { kind: 'idle', text: `已完成 ${pctText}` };
    return { kind: 'idle', text: '空闲' };
  }

  function renderRunStatus() {
    const chip = $('run-status');
    const text = $('run-status-text');
    const track = $('progress-track');
    if (!chip || !text) return;

    const st = parseState();
    chip.className = 'status-chip ' + st.kind;
    text.textContent = st.text;
    if (track) track.classList.toggle('running', st.kind === 'running');

    // P1：「✓ 当前批次已完成」其实是 btn-run 的文案（app.js:294），
    //     却渲染成蓝色主按钮 —— 状态被做成了按钮，与「重新生成」抢注意力。
    //     批次完成时把它降级为状态型次要按钮。
    const runBtn = $('btn-run');
    if (runBtn) {
      const txt = (runBtn.textContent || '').trim();
      const isDoneLabel = /当前批次已完成|全部完成/.test(txt);
      runBtn.classList.toggle('done-state', isDoneLabel);
      if (isDoneLabel) {
        runBtn.classList.remove('primary');
        runBtn.title = '当前批次已全部完成，如需再生成请点「重新生成」';
      } else if (!runBtn.classList.contains('primary')) {
        runBtn.classList.add('primary');
        runBtn.title = '';
      }
    }
  }

  // ================================================================ 状态栏
  /**
   * 增强 #meta：
   *   · 移除冗长的「输出 E:\...」（P3：路径不占位）
   *   · 抽出批次号显示为 chip，并支持一键复制
   *   · 生成「模拟门店背景」「真实感 V3」徽章（P2/P14）
   */
  function enhanceMeta() {
    const meta = $('meta');
    if (!meta) return;

    const html = meta.innerHTML || '';

    // ① 抓完整输出路径（供复制），然后从显示中删掉
    const outMatch = html.match(/输出\s*<b>([^<]+)<\/b>/);
    if (outMatch) {
      lastOutputRoot = outMatch[1].trim();
      meta.innerHTML = html.replace(/\s*·?\s*输出\s*<b>[^<]+<\/b>/, '');
    }

    // ② 抓批次号
    const batchMatch = (meta.innerHTML || '').match(/批次\s*<b>([^<]+)<\/b>/);
    const chip = $('batch-chip');
    const label = $('batch-label');
    if (batchMatch && chip && label) {
      const raw = batchMatch[1].trim();
      // 缩短：batch_20260920_144539_qwen_qwen-image-3.0 → 20260920_144539
      const m = raw.match(/(\d{8}_\d{6})/);
      label.textContent = m ? m[1] : raw.replace(/^batch_/, '').slice(0, 22);
      chip.hidden = false;
      chip.title = lastOutputRoot ? `完整路径：${lastOutputRoot}` : raw;
      // 从 meta 里移掉批次，避免重复
      meta.innerHTML = meta.innerHTML.replace(/\s*·?\s*批次\s*<b>[^<]+<\/b>/, '');
    }
  }

  /** 从视图栏文案里提取「模拟门店背景 / 真实感 Vn」→ 徽章（P2/P14） */
  function enhanceBadges() {
    const vc = $('image-view-count');
    const text = vc ? vc.textContent || '' : '';

    simBackground = /模拟门店背景/.test(text);

    const bgBadge = $('badge-bg-mode');
    if (bgBadge) {
      bgBadge.hidden = !simBackground;
      bgBadge.textContent = '⚠️ 模拟门店背景';
      bgBadge.title = '本批次效果图使用模拟门店背景，不是真实门店实拍照片';
    }

    const rm = text.match(/真实感\s*(V\d)/i);
    const rBadge = $('badge-realism');
    if (rBadge) {
      rBadge.hidden = !rm;
      if (rm) {
        rBadge.textContent = `真实感 ${rm[1].toUpperCase()}`;
        rBadge.title = '提示词的真实感等级（V1~V3）';
      }
    }
  }

  /** 视图标签带数量（P5：把数量前置到标签上） */
  function enhanceTabCounts() {
    const vc = $('image-view-count');
    if (!vc) return;
    const text = vc.textContent || '';

    // 形如「已完成 6 / 6 张效果图」或「共 6 张生成图」
    const m = text.match(/(\d+)\s*\/\s*(\d+)/);
    const effectView = $('image-view-effect');
    const isEffect = effectView && effectView.classList.contains('active');

    const g = $('cnt-generated');
    const e = $('cnt-effect');
    if (m) {
      if (isEffect && e) e.textContent = m[1];
      else if (!isEffect && g) g.textContent = m[1];
    } else {
      const single = text.match(/(\d+)\s*张/);
      if (single) {
        if (isEffect && e) e.textContent = single[1];
        else if (!isEffect && g) g.textContent = single[1];
      }
    }
  }

  // ================================================================ 图片卡片
  /**
   * 给缩略图加角标（P11 / P14）：
   *   · 左上：序号（1~6）
   *   · 右上：状态（✓ 已生成 / ⏳ 生成中 / ✗ 失败）
   *   · 左下：模拟背景角标（仅模拟时显示）
   */
  function enhanceThumbs() {
    const stores = $('stores');
    if (!stores) return;

    const effectView = $('image-view-effect');
    const isEffect = effectView && effectView.classList.contains('active');

    // ⚠️ app.js 生成的卡片类名是 `.card`（不是 `.store-card`）。
    //    这里两种都匹配，避免因类名变化而静默失效。
    stores.querySelectorAll('.store-card, .card').forEach((card) => {
      // 卡片头：加「模拟背景」标签（第二重提示）
      const head = card.querySelector('.card-head, .store-head, header, .card-title');
      if (head && simBackground && !head.querySelector('.bg-tag')) {
        const tag = document.createElement('span');
        tag.className = 'bg-tag';
        tag.textContent = '模拟背景';
        tag.title = '该门店效果图使用模拟门店背景，不是实拍照片';
        head.appendChild(tag);
      }

      // 缩略图角标
      const imgs = card.querySelectorAll('img');
      imgs.forEach((img, idx) => {
        const parent = img.parentElement;
        if (!parent) return;
        // ⚠️ `.thumb` 是 app.js 已有的类名，不能用它判断是否已增强；
        //    改用自定义 data 属性做幂等标记。
        if (parent.dataset.uiEnhanced === '1') return;
        parent.dataset.uiEnhanced = '1';
        parent.classList.add('thumb');

        // ① 序号（左上）
        const tn = document.createElement('span');
        tn.className = 'tn';
        tn.textContent = String(idx + 1);
        tn.setAttribute('aria-label', `第 ${idx + 1} 张`);
        parent.appendChild(tn);

        // ② 状态（右上）：app.js 已用 `.dot success` 表达状态
        const dot = parent.querySelector('.dot');
        const failed = Boolean(parent.querySelector('.dot.failed, .failed'))
          || (dot && dot.classList.contains('failed'));
        const pending = !img.getAttribute('src');
        const ts = document.createElement('span');
        ts.className = 'ts' + (failed ? ' failed' : pending ? ' pending' : '');
        ts.textContent = failed ? '✗' : pending ? '⏳' : '✓';
        ts.title = failed ? '生成失败' : pending ? '等待生成' : '已生成';
        parent.appendChild(ts);

        // ③ 模拟背景角标（左下，第三重提示）
        if (simBackground && isEffect) {
          const tm = document.createElement('span');
          tm.className = 'tm';
          tm.textContent = '模拟';
          tm.title = '模拟门店背景（非实拍）';
          parent.appendChild(tm);
        }
      });
    });
  }

  // ================================================================ 日志区
  function logLineClass(line) {
    if (/错误|失败|error|exception|traceback|❌/i.test(line)) return 'lg-error';
    if (/警告|warn|⚠/i.test(line)) return 'lg-warn';
    if (/完成|成功|✅/i.test(line)) return 'lg-ok';
    return '';
  }

  function enhanceLog() {
    const pre = $('log');
    const panel = $('logpanel');
    const count = $('log-count');
    if (!pre || !panel) return;

    const raw = pre.textContent || '';
    const lines = raw.split('\n').filter((l) => l.trim());
    const errCount = lines.filter((l) => logLineClass(l) === 'lg-error').length;

    if (count) {
      count.textContent = String(lines.length);
      count.classList.toggle('has-error', errCount > 0);
    }

    // 有报错 → 自动展开（Q4 决策）
    if (errCount > 0) setLogExpanded(true);
  }

  function setLogExpanded(open, persist = true) {
    const panel = $('logpanel');
    const head = $('loghead');
    if (!panel) return;
    panel.classList.toggle('collapsed', !open);
    if (head) head.setAttribute('aria-expanded', String(open));
    if (persist) {
      try { localStorage.setItem(LOG_EXPANDED_KEY, open ? '1' : '0'); } catch (_) {}
    }
  }

  function initLogToggle() {
    const head = $('loghead');
    if (!head) return;

    // 恢复上次选择（Q4：首次访问且无日志 → 收起）
    let expanded = false;
    try { expanded = localStorage.getItem(LOG_EXPANDED_KEY) === '1'; } catch (_) {}
    setLogExpanded(expanded, false);

    const toggle = () => {
      const panel = $('logpanel');
      setLogExpanded(panel ? panel.classList.contains('collapsed') : true);
    };
    head.addEventListener('click', (e) => {
      // 点「清空」按钮不触发折叠
      if (e.target && e.target.id === 'btn-clear-log') return;
      toggle();
    });
    head.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  }

  // ================================================================ 批次复制
  /**
   * 带超时的剪贴板写入。
   *
   * ⚠️ 踩过的坑：在自动化环境（或用户拒绝剪贴板权限）下，
   *    `navigator.clipboard.writeText()` 会**一直 pending** ——
   *    既不 resolve 也不 reject，把后面的 UI 反馈代码**永久卡住**，
   *    表现为「点了复制没任何反应」。
   *    因此必须加超时兜底，且 **UI 反馈要在等待之前就给出**。
   */
  function copyWithTimeout(text, ms = 1200) {
    return new Promise((resolve) => {
      let settled = false;
      const done = (ok) => { if (!settled) { settled = true; resolve(ok); } };

      const timer = setTimeout(() => done(false), ms);

      const fallback = () => {
        // 退化方案：临时 textarea + execCommand（老 API，但兼容性好）
        try {
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          const ok = document.execCommand('copy');
          ta.remove();
          return ok;
        } catch (_) {
          return false;
        }
      };

      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        clearTimeout(timer);
        return done(fallback());
      }

      navigator.clipboard.writeText(text).then(
        () => { clearTimeout(timer); done(true); },
        () => { clearTimeout(timer); done(fallback()); },
      );
    });
  }

  function initCopyBatch() {
    const btn = $('btn-copy-batch');
    if (!btn) return;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const text = lastOutputRoot
        || ($('batch-chip') ? $('batch-chip').title.replace(/^完整路径：/, '') : '')
        || ($('batch-label') ? $('batch-label').textContent : '');
      if (!text) return;

      const old = btn.textContent;
      // ① 先给反馈 —— 不等剪贴板结果，避免「点了没反应」
      btn.classList.add('copied');
      btn.textContent = '✓';
      btn.title = '已复制：' + text;
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = old;
        btn.title = '复制完整输出路径';
      }, 1400);

      // ② 后台真正执行复制（带超时，失败也不影响上面的反馈）
      copyWithTimeout(text).then((ok) => {
        if (!ok) {
          btn.textContent = '!';
          btn.title = '复制失败，请手动选择：' + text;
          setTimeout(() => { btn.textContent = old; }, 1400);
        }
      });
    });
  }

  // ================================================================ 印刷导出状态
  /**
   * 在状态栏显示印刷 TIF 的导出状态。
   *
   * 三种状态：
   *   ⏳ 导出中 / ✅ 印刷文件已生成 / ⚠️ 导出失败（附原因，可点开看明细）
   *
   * 数据源：`GET /api/print-export/<batch_id>`（只读接口，失败静默）
   */
  let PRINT_BATCH = '';        // 已请求过的批次，避免重复拉取
  let PRINT_TIMER = null;

  async function refreshPrintBadge(force) {
    const badge = $('badge-print');
    if (!badge) return;
    try {
      const st = await api('/api/state');
      const batchId = (st && (st.batch_id || (st.batch && st.batch.id))) || '';
      if (!batchId) { badge.hidden = true; return; }
      if (!force && batchId === PRINT_BATCH) return;
      PRINT_BATCH = batchId;

      const d = await api('/api/print-export/' + encodeURIComponent(batchId));
      if (!d.enabled) { badge.hidden = true; return; }

      const stores = d.stores || [];
      const exported = stores.filter((s) => s.exported);
      const failed = stores.filter((s) => s.status === 'failed'
        || (s.error && s.error.code));

      badge.hidden = false;
      if (failed.length) {
        badge.className = 'badge warn';
        badge.textContent = `⚠️ 印刷导出失败 ${failed.length} 个门店`;
        const reason = (failed[0].error && failed[0].error.message) || '未知原因';
        badge.title = `${reason}\n点击查看明细`;
        badge.style.cursor = 'pointer';
        badge.onclick = () => showPrintPanel(d);
      } else if (exported.length) {
        const total = exported.reduce((n, s) => n + (s.files || []).length, 0);
        badge.className = 'badge ok';
        badge.textContent = `✅ 印刷文件已生成（${exported.length} 店 / ${total} 文件）`;
        badge.title = '点击查看明细';
        badge.style.cursor = 'pointer';
        badge.onclick = () => showPrintPanel(d);
      } else {
        badge.className = 'badge info';
        badge.textContent = '⏳ 印刷文件导出中…';
        badge.title = '批次完成后会自动导出，稍后自动刷新';
        badge.onclick = null;
        badge.style.cursor = 'default';
        // 导出在后台线程跑，10 秒后再看一次
        if (PRINT_TIMER) clearTimeout(PRINT_TIMER);
        PRINT_TIMER = setTimeout(() => { PRINT_BATCH = ''; refreshPrintBadge(true); }, 10000);
      }
    } catch (_) {
      // 静默：印刷状态是附加信息，拿不到就不显示
      if (badge) badge.hidden = true;
    }
  }

  /** 印刷导出明细（用 alert 展示，不引入新弹窗组件） */
  function showPrintPanel(data) {
    const s = data.settings || {};
    const lines = [
      `批次：${data.batch_id}`,
      `尺寸：${s.width_cm}cm · ${s.dpi}DPI · 出血 ${s.bleed_mm}mm`,
      `白墨层：${s.white_ink ? '启用' : '关闭'}　刀模线：${s.dieline ? '启用' : '关闭'}`,
      '',
      ...(data.stores || []).map((store) => {
        const mark = store.status === 'success' ? '✅'
          : (store.status === 'failed' ? '❌' : '⏳');
        const layers = Object.keys(store.layers || {}).length;
        return `${mark} ${store.store}　${(store.files || []).length} 个文件`
          + (layers ? `（${layers} 层）` : '');
      }),
    ];
    const warn = [];
    (data.stores || []).forEach((store) => {
      (store.warnings || []).forEach((w) => warn.push(`${store.store}：${w}`));
    });
    if (warn.length) {
      lines.push('', `⚠️ 警告 ${warn.length} 条：`, ...warn.slice(0, 4));
    }
    if ((data.stores || []).some((store) => store.status === 'failed')) {
      lines.push('', '失败的门店可调用 POST /api/print-export/<批次>/<门店序号> 重试。');
    }
    alert(lines.join('\n'));
  }

  // ================================================================ 观察器
  function observe(id, fn, opts) {
    const el = $(id);
    if (!el) return;
    const mo = new MutationObserver(() => fn());
    mo.observe(el, opts || { childList: true, subtree: true, characterData: true });
  }

  function refreshAll() {
    enhanceMeta();
    enhanceBadges();
    enhanceTabCounts();
    enhanceThumbs();
    renderRunStatus();
  }

  /**
   * 视图切换后重新增强。
   *
   * 为什么要先「拆掉旧角标」：
   *   「模拟」角标只在**效果图视图**下出现。若缩略图被标记为"已增强"，
   *   切到效果图视图后就不会补上模拟角标 —— 恰恰在最需要提示的视图里丢了提示。
   *   所以切换视图时先撤销标记与旧角标，再重新增强。
   */
  function rebuildThumbs() {
    const stores = $('stores');
    if (!stores) return;
    stores.querySelectorAll('[data-ui-enhanced="1"]').forEach((el) => {
      el.querySelectorAll('.tn, .ts, .tm').forEach((n) => n.remove());
      delete el.dataset.uiEnhanced;
    });
    stores.querySelectorAll('.bg-tag').forEach((n) => n.remove());
    enhanceThumbs();
  }

  // ================================================================ 启动
  function boot() {
    initLogToggle();
    initCopyBatch();

    // 状态变化：按钮 disabled 是重要信号
    ['#btn-run', '#btn-stop', '#btn-new-batch'].forEach((sel) => {
      const el = document.querySelector(sel);
      if (el) {
        new MutationObserver(renderRunStatus)
          .observe(el, { attributes: true, attributeFilter: ['disabled'] });
      }
    });

    observe('stores', () => { enhanceThumbs(); renderRunStatus(); });
    observe('meta', () => { enhanceMeta(); renderRunStatus(); });
    observe('image-view-count', () => { enhanceBadges(); enhanceTabCounts(); enhanceThumbs(); });
    observe('log', enhanceLog);
    observe('bar', renderRunStatus, { attributes: true, attributeFilter: ['style'] });
    observe('pct', () => { enhanceTabCounts(); renderRunStatus(); });

    // 视图切换（生成图/效果图）：先拆旧角标再重建（模拟角标只在效果图视图出现）
    ['#image-view-generated', '#image-view-effect'].forEach((sel) => {
      const el = document.querySelector(sel);
      if (el) {
        el.addEventListener('click', () => setTimeout(() => {
          enhanceBadges();
          enhanceTabCounts();
          rebuildThumbs();
        }, 80));
      }
    });

    // 首屏补一次（app.js 可能已经渲染完）
    setTimeout(refreshAll, 300);
    setTimeout(refreshAll, 1200);

    // 印刷导出状态：导出在后台线程跑，给它几秒再首查，之后每 30 秒刷新
    setTimeout(refreshPrintBadge, 3000);
    setInterval(() => refreshPrintBadge(), 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
