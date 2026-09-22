/* ============================================================
   AI图片生成 —— 前端逻辑
   ============================================================ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let STATE = null;
  let SETTINGS = null;
  let CATALOG = null;
  let PROMPT_VERSIONS = null;
  let REFERENCE_ASSETS = [];
  let REFERENCE_ROOT = '';
  let EFFECT_BACKGROUND_ASSETS = [];
  let EFFECT_BACKGROUND_ROOT = '';
  let SELECTED_EFFECT_BACKGROUND_ID = null;
  let EFFECT_PARAMS = null;
  let ACTIVE_IMAGE_MODE = 'text';
  let IMAGE_VIEW = localStorage.getItem('image-agent-view') === 'effect' ? 'effect' : 'generated';
  const ITEM_STATE = new Map();   // "01-03" -> status

  // ---------------------------------------------------------- 工具
  const imgUrl = (dir, file, view = IMAGE_VIEW) =>
    `/api/image?store=${encodeURIComponent(dir)}&file=${encodeURIComponent(file)}&view=${encodeURIComponent(view)}`;

  function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.className = 'toast'; }, 2800);
  }

  function logLine(text, cls) {
    const el = $('log');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    const span = document.createElement('div');
    if (cls) span.className = cls;
    span.textContent = text;
    el.appendChild(span);
    while (el.childElementCount > 400) el.removeChild(el.firstChild);
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  const api = async (url, opts) => {
    const r = await fetch(url, opts);
    let data = null;
    try { data = await r.json(); } catch { /* 非 JSON */ }
    if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
    return data;
  };

  // ============================================================ 主界面
  function itemDone(it) {
    return it.status === 'success' || it.status === 'skipped' || it.exists;
  }

  function renderStore(s) {
    const openaiHouseMode = STATE && STATE.config && STATE.config.provider === 'openai';
    const effectView = IMAGE_VIEW === 'effect';
    const done = s.items.filter(itemDone).length;
    const badge = done === s.items.length
      ? '<span class="badge ok">✅ 完成</span>'
      : `<span class="badge">${done} / ${s.items.length}</span>`;

    const thumbs = s.items.map((it) => {
      const uid = `${s.folder_index}-${it.pic_index}`;
      ITEM_STATE.set(uid, it.status);
      const generated = itemDone(it);
      const imageFile = it.image_file || it.file_name;
      const effectReady = !!it.effect_exists;
      const displayFile = effectView ? it.effect_file : imageFile;
      const displayReady = effectView ? effectReady : generated;
      const displayStatus = effectView ? (it.effect_status || 'pending') : it.status;
      const missingText = effectView
        ? (it.effect_error ? '效果图合成失败' : '等待生成效果图')
        : `${it.pic_index}<br>${it.theme}`;
      const inner = displayReady
        ? `<img src="${imgUrl(s.output_dir, displayFile, effectView ? 'effect' : 'generated')}" alt="${esc(it.theme)} ${effectView ? '玻璃效果图' : '生成图'}" loading="lazy">`
        : `<div class="ph">${esc(missingText)}</div>`;
      const actions = effectView ? '' : `<div class="thumb-actions">
                  <button class="btn-regen" data-store-idx="${esc(s.folder_index)}"
                          data-pic="${esc(it.pic_index)}">↻ 重生成</button>
                  <button class="btn-history" data-store="${esc(s.output_dir)}"
                          data-file="${esc(it.file_name)}"
                          data-label="${esc(s.folder_name)} / ${esc(it.theme)}">🕘 历史</button>
                </div>`;
      return `<div class="thumb" id="thumb-${uid}" data-uid="${uid}"
                   data-store="${esc(s.output_dir)}" data-file="${esc(displayFile || '')}"
                   data-view="${effectView ? 'effect' : 'generated'}"
                   data-logical-file="${esc(it.file_name)}"
                   data-store-idx="${esc(s.folder_index)}" data-pic="${esc(it.pic_index)}"
                   title="${esc(it.theme)}">
                ${inner}<span class="dot ${esc(displayStatus)}"></span>
                ${actions}
              </div>`;
    }).join('');

    const houseAction = openaiHouseMode && s.folder_index === '01'
      ? '<button class="btn small primary btn-house-regenerate" type="button">✨ 生成 6 张新图</button>'
      : '';
    return `<div class="card" id="card-${s.folder_index}">
      <div class="card-head">
        <div class="card-title">
          <span class="idx">${esc(s.folder_index)}</span>
          <span class="name">${esc(s.folder_name)}</span>
        </div>
        <div class="card-actions">${houseAction}${badge}</div>
      </div>
      <div class="card-sub">
        主标题 <b>${esc(s.main_title)}</b> · 副标题 <b>${esc(s.sub_title)}</b><br>
        配色 ${esc(s.color_theme)}
      </div>
      <div class="thumbs">${thumbs}</div>
    </div>`;
  }

  function renderAll() {
    // The main workspace represents the current batch, rather than every
    // store available in Settings.  Settings keeps the full 23-store list so
    // the user can prepare the next batch without changing this snapshot.
    const active = new Set((STATE.scope && STATE.scope.store_indexes) || []);
    const visibleStores = active.size
      ? STATE.stores.filter((store) => active.has(store.folder_index))
      : [];
    $('stores').innerHTML = visibleStores.length
      ? visibleStores.map(renderStore).join('')
      : '<div class="empty-state">当前批次没有选择门店。请在“设置 → 生成范围”勾选门店后创建新批次。</div>';
    renderMeta();
    renderProgress();
    renderCurrent();
    renderRecovery();
    renderImageViewToolbar(visibleStores);
  }

  function renderImageViewToolbar(stores) {
    const items = stores.flatMap((store) => store.items);
    const generated = items.filter(itemDone).length;
    const effects = items.filter((item) => item.effect_exists).length;
    $('image-view-generated').classList.toggle('active', IMAGE_VIEW === 'generated');
    $('image-view-effect').classList.toggle('active', IMAGE_VIEW === 'effect');
    const effectBackground = (STATE.config && STATE.config.effect_background) || {};
    const effectScene = effectBackground.id ? `实拍背景：${effectBackground.file_name}` : '模拟门店背景';
    const realism = (STATE.config && STATE.config.realism_iteration) || 1;
    $('image-view-count').textContent = IMAGE_VIEW === 'effect'
      ? `已完成 ${effects} / ${generated} 张效果图 · ${effectScene} · 真实感 V${realism}`
      : `已生成 ${generated} / ${items.length} 张贴纸`;
    const build = $('btn-generate-effects');
    const missing = Math.max(0, generated - effects);
    build.hidden = !generated;
    build.disabled = !!(STATE.running || STATE.effects_running || !missing);
    build.textContent = STATE.effects_running ? '正在合成效果图…' : (missing ? `🪟 补齐 ${missing} 张效果图` : '✓ 效果图已齐全');
  }

  function renderProgress() {
    const p = STATE.progress;
    $('bar').style.width = `${p.percent}%`;
    $('pct').textContent = `${p.percent}%`;
    $('counts').textContent = `${p.done} / ${p.total} 张`;
  }

  function renderMeta(extra) {
    const c = STATE.config;
    const batch = STATE.batch || {};
    const rateText = c.rpm_limit
      ? `（${c.rpm_limit} RPM${c.provider === 'qwen' ? '；安全调度 16 RPM' : ''}）`
      : '（无 QPS 限制）';
    $('meta').innerHTML =
      `服务商 <b>${esc(c.provider_label)}</b> · 模型 <b>${esc(c.model)}</b> · ` +
      `并发 <b>${c.concurrency}</b>` +
      rateText +
      ` · 批次 <b>${esc(batch.label || '原始输出目录')}</b>` +
      ` · 比例 <b>${esc(c.size || '1024x1024')}</b>` +
      ` · 输出 <b>${esc(c.output_root)}</b>` +
      (extra ? ` · ${esc(extra)}` : '');
  }

  function renderCurrent() {
    const c = STATE.config;
    const el = $('current-config');
    const scoped = (STATE.scope && STATE.scope.image_count) || STATE.progress.total || 0;
    const limitTxt = c.run_limit ? `试跑 ${c.run_limit} 张` : `当前范围 ${scoped} 张`;
    $('current-text').innerHTML =
      `<b>${esc(c.provider_label)}</b> · ${esc(c.model)} · ${esc(c.size || '1024x1024')} · ${limitTxt}`;
    const ok = c.api_key_ok;
    el.className = 'current' + (ok ? '' : ' warn');
    el.title = ok ? '点击右上角「设置」可修改' : '⚠️ 未配置 API Key —— 点击右上角「设置」填写';
  }

  function renderRecovery() {
    const banner = $('recovery-banner');
    const recovery = (STATE && STATE.recovery) || {};
    if (!recovery.required) {
      banner.hidden = true;
      banner.textContent = '';
      banner.className = 'recovery-banner';
      return;
    }
    banner.hidden = false;
    if (recovery.verified) {
      banner.className = 'recovery-banner ready';
      banner.innerHTML = '<strong>✅ 图片出图权限已验证。</strong> 可点击“开始/继续当前批次”，系统只补未完成图片，不会创建新文件夹。';
      return;
    }
    banner.className = 'recovery-banner';
    banner.innerHTML = '<strong>⚠️ 当前批次已因账户欠费或额度不足暂停。</strong> 充值后请打开“设置”，先点击“测试连接”，再点击“验证图片出图权限”；验证成功后继续当前批次。';
  }

  function setThumb(uid, status) {
    const el = $('thumb-' + uid);
    if (!el) return;
    const dot = el.querySelector('.dot');
    if (dot) dot.className = 'dot ' + status;
    el.classList.toggle('failed', status === 'failed');
    ITEM_STATE.set(uid, status);
  }

  function refreshThumbImage(uid, imageFile) {
    const el = $('thumb-' + uid);
    if (!el) return;
    if (imageFile) el.dataset.file = imageFile;
    const { store, file, view } = el.dataset;
    const url = imgUrl(store, file, view || 'generated') + '&t=' + Date.now();
    const img = el.querySelector('img');
    if (img) { img.src = url; return; }
    const ph = el.querySelector('.ph');
    if (ph) ph.remove();
    const i = document.createElement('img');
    i.src = url; i.loading = 'lazy';
    el.insertBefore(i, el.firstChild);
  }

  function bumpProgress() {
    let done = 0;
    const selected = new Set((STATE.scope && STATE.scope.store_indexes) || []);
    ITEM_STATE.forEach((st, uid) => {
      const store = uid.split('-')[0];
      if ((!selected.size || selected.has(store)) && (st === 'success' || st === 'skipped')) done++;
    });
    STATE.progress.done = done;
    STATE.progress.percent = STATE.progress.total
      ? Math.round((done / STATE.progress.total) * 1000) / 10 : 0;
    renderProgress();
  }

  function updateCardBadge(storeIndex) {
    const card = $('card-' + storeIndex);
    const store = STATE.stores.find((s) => s.folder_index === storeIndex);
    if (!card || !store) return;
    let done = 0;
    store.items.forEach((it) => {
      const st = ITEM_STATE.get(`${storeIndex}-${it.pic_index}`);
      if (st === 'success' || st === 'skipped') done++;
    });
    const badge = card.querySelector('.badge');
    if (done === store.items.length) {
      badge.className = 'badge ok';
      badge.textContent = '✅ 完成';
      card.classList.add('done');
    } else {
      badge.className = 'badge';
      badge.textContent = `${done} / ${store.items.length}`;
    }
  }

  async function loadState() {
    STATE = await api('/api/state');
    ITEM_STATE.clear();
    renderAll();
    setRunning(STATE.running);
  }

  function isFullBatchProvider() {
    const provider = STATE && STATE.config && STATE.config.provider;
    return provider !== 'flux_local';
  }

  function setRunning(running) {
    const fullBatch = isFullBatchProvider();
    const complete = !!(STATE && STATE.progress && STATE.progress.complete);
    const imageCount = (STATE && STATE.scope && STATE.scope.image_count) || 0;
    const nextImageCount = (STATE && STATE.next_scope && STATE.next_scope.image_count) || imageCount;
    const run = $('btn-run');
    const newBatch = $('btn-new-batch');
    const recovery = (STATE && STATE.recovery) || {};
    const needsTest = recovery.required && !recovery.verified;

    run.textContent = needsTest
      ? '⚠ 充值后验证出图权限'
      : fullBatch && complete
      ? '✓ 当前批次已完成'
      : '▶ 开始/继续当前批次';
    run.disabled = running || needsTest || !imageCount || (fullBatch && complete);
    newBatch.hidden = !fullBatch;
    newBatch.disabled = running || needsTest || !nextImageCount;
    newBatch.textContent = `✨ 重新生成（新批次 ${nextImageCount} 张）`;
    $('btn-stop').disabled = !running;
    $('btn-settings').disabled = false;
  }

  // ============================================================ 操作
  async function start() {
    try {
      const localFlux = STATE && STATE.config && STATE.config.provider === 'flux_local';
      const data = await api(localFlux ? '/api/local-validation/run' : '/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      setRunning(true);
      toast(localFlux ? '已启动本地 01 / V8 / 6 张验证' : (data.resumed_after_recharge ? '账户已恢复：正在补齐当前批次未完成图片' : `已启动：${data.provider}，范围 ${data.scope && data.scope.image_count ? data.scope.image_count : ''} 张`));
    } catch (e) { toast(e.message, true); }
  }

  async function startNewBatch() {
    try {
      const data = await api('/api/run/new-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      // A fresh batch has an empty output directory.  Rebuild the cards before
      // job events arrive, otherwise success events repaint old batch cards.
      await loadState();
      const batch = data.batch || {};
      const count = (data.scope && data.scope.image_count) || (STATE.scope && STATE.scope.image_count) || 0;
      toast(`已创建 ${batch.label || '新批次'}，开始生成 ${count} 张新图`);
    } catch (e) { toast(e.message, true); }
  }

  function setImageView(view) {
    IMAGE_VIEW = view === 'effect' ? 'effect' : 'generated';
    localStorage.setItem('image-agent-view', IMAGE_VIEW);
    renderAll();
  }

  async function generateMissingEffects() {
    const btn = $('btn-generate-effects');
    btn.disabled = true;
    try {
      const data = await api('/api/effects/generate', { method: 'POST' });
      toast(data.started ? `开始本地合成 ${data.missing} 张玻璃效果图` : data.message || '效果图已经齐全');
      if (data.started) logLine(`🪟 开始本地合成 ${data.missing} 张玻璃效果图，不调用图像模型`, 'ok');
      await loadState();
    } catch (e) {
      toast(e.message, true);
      await loadState();
    }
  }

  async function stop() {
    try {
      await api('/api/stop', { method: 'POST' });
      toast('正在停止…');
    } catch (e) { toast(e.message, true); }
  }

  // ============================================================ 设置弹窗
  async function openSettings() {
    try {
      const [s, c, pv, references, effectBackgrounds] = await Promise.all([
        api('/api/settings'),
        CATALOG ? Promise.resolve(CATALOG) : api('/api/catalog'),
        api('/api/prompts/versions').catch(() => null),
        api('/api/reference-assets').catch(() => ({ assets: [], root: '' })),
        api('/api/effect-background-assets').catch(() => ({ assets: [], root: '' })),
      ]);
      SETTINGS = s; CATALOG = c; PROMPT_VERSIONS = pv;
      REFERENCE_ASSETS = references.assets || [];
      REFERENCE_ROOT = references.root || '';
      EFFECT_BACKGROUND_ASSETS = effectBackgrounds.assets || [];
      EFFECT_BACKGROUND_ROOT = effectBackgrounds.root || '';
      SELECTED_EFFECT_BACKGROUND_ID = null;
      renderProviderCards();
      renderPromptVersions();
      fillForm();
      selectSettingsPane('model');
      renderScope();
      renderQualityWorkspace();
      renderReferenceAssets();
      renderEffectBackgroundAssets();
      renderFileSummary();
      loadLocalValidation();
      loadProfessionalValidation();
      loadPending();
      loadRuns();
      loadEffectParams();
      $('settings-msg').textContent = '';
      $('settings-msg').className = 'msg';
      $('settings-modal').classList.add('show');
    } catch (e) { toast('读取设置失败：' + e.message, true); }
  }

  function renderPromptVersions() {
    const sel = $('s-prompt-version');
    if (!sel) return;
    if (!PROMPT_VERSIONS || !PROMPT_VERSIONS.versions) {
      sel.innerHTML = '<option value="">（读取失败）</option>';
      return;
    }
    sel.innerHTML = PROMPT_VERSIONS.versions.map((v) => {
      const mark = v.is_current ? ' ← 当前生产版' : '';
      const dis = v.available ? '' : ' disabled';
      return `<option value="${esc(v.version)}"${dis}>${esc(v.version)}　${esc(v.note)}${mark}</option>`;
    }).join('');
    sel.addEventListener('change', () => {
      const v = sel.value;
      const pv = $('prompt-preview');
      pv.textContent = (PROMPT_VERSIONS.sample && PROMPT_VERSIONS.sample[v]) || '（无预览）';
    });
  }

  function closeSettings() {
    $('settings-modal').classList.remove('show');
  }

  function selectSettingsPane(pane) {
    document.querySelectorAll('.settings-nav-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.pane === pane);
    });
    document.querySelectorAll('.settings-pane').forEach((el) => {
      el.hidden = el.dataset.pane !== pane;
    });
  }

  function orderedStores() {
    return (STATE && STATE.stores) || [];
  }

  function selectedScopeIndexes() {
    return [...document.querySelectorAll('#scope-list input[type=checkbox]:checked')]
      .map((el) => el.value);
  }

  function scopeSelectionSet() {
    const scope = (SETTINGS && SETTINGS.scope) || {};
    return new Set(scope.store_indexes || orderedStores().map((s) => s.folder_index));
  }

  function updateScopeSummary() {
    const selected = selectedScopeIndexes();
    const total = selected.length * 6;
    $('scope-summary').textContent = selected.length
      ? `已选 ${selected.length} 个门店文件夹，共 ${total} 张图片。保存后用于新的批次。`
      : '尚未选择门店。新批次至少需要选择 1 个门店。';
  }

  function renderScope() {
    const list = $('scope-list');
    if (!list) return;
    const selected = scopeSelectionSet();
    list.innerHTML = orderedStores().map((s) => `
      <label class="scope-row scope-item" data-scope-search="${esc(`${s.folder_index} ${s.folder_name}`)}">
        <input type="checkbox" value="${esc(s.folder_index)}"${selected.has(s.folder_index) ? ' checked' : ''}>
        <span class="scope-index">${esc(s.folder_index)}</span>
        <span class="scope-name">${esc(s.folder_name)}</span><small>6 张</small>
      </label>`).join('');
    list.querySelectorAll('input').forEach((input) => input.addEventListener('change', updateScopeSummary));
    filterScope();
    updateScopeSummary();
  }

  function filterScope() {
    const query = (($('scope-search') && $('scope-search').value) || '').trim().toLowerCase();
    document.querySelectorAll('#scope-list .scope-item').forEach((el) => {
      el.hidden = !!query && !el.dataset.scopeSearch.toLowerCase().includes(query);
    });
  }

  function selectedReferenceIds() {
    return [...document.querySelectorAll('#reference-assets input[type=checkbox]:checked')]
      .map((el) => el.value);
  }

  function selectedReferenceMetadata() {
    const wanted = new Set(selectedReferenceIds());
    return REFERENCE_ASSETS.filter((asset) => wanted.has(asset.id)).map((asset) => ({
      id: asset.id, sha256: asset.sha256, file_name: asset.file_name,
      mime_type: asset.mime_type, bytes: asset.bytes,
    }));
  }

  function selectedEffectBackgroundId() {
    const selected = document.querySelector('#effect-background-assets input[type=radio]:checked');
    return selected ? selected.value : (SELECTED_EFFECT_BACKGROUND_ID || '');
  }

  function selectedEffectBackgroundMetadata() {
    const selected = selectedEffectBackgroundId();
    return EFFECT_BACKGROUND_ASSETS.find((asset) => asset.id === selected) || {};
  }

  function qualityTemplateInUse() {
    const optimized = $('pq-optimized').value.trim();
    return $('pq-use-optimized').checked && optimized ? optimized : $('pq-template').value.trim();
  }

  function renderQualityPreview() {
    const store = orderedStores()[0];
    const item = store && store.items && store.items[0];
    if (!store || !item) return;
    const vars = {
      '{{main_title}}': store.main_title || '',
      '{{sub_title}}': store.sub_title || '',
      '{{subject}}': item.subject || item.theme || '',
      '{{theme}}': item.theme || '',
      '{{color_theme}}': store.color_theme || '',
      '{{aspect_ratio}}': $('s-size').value || '1024x1024',
    };
    let rendered = qualityTemplateInUse();
    Object.entries(vars).forEach(([key, value]) => { rendered = rendered.split(key).join(value); });
    $('pq-variables').innerHTML = Object.entries(vars).map(([key, value]) =>
      `<div class="variable-row"><code>${esc(key)}</code><span>${esc(value)}</span></div>`).join('');
    const realism = parseInt($('pq-realism').value, 10) || 1;
    $('pq-preview').textContent = `原始行业提示词：${item.prompt_preview || item.theme || ''}\n\n质量模板：\n${rendered}\n\n真实感：V${realism}；创建新批次时会自动提升一级（最高 V3）。\n文字锁定：主标题“${store.main_title || ''}”；副标题“${store.sub_title || ''}”。`;
  }

  // ---- 服务商生成能力（文生图/图生图/多图生图）----
  //
  // 不是所有服务商都支持图生图（如 FLUX 本地验证只做文生图），
  // 支持的也各有参考图张数上限（qwen 3 张、OpenAI 1 张）。
  // 能力从 `GET /api/providers/capabilities` 拉，**界面据此禁用不支持的 tab** ——
  // 否则用户选了不支持的模式，要么跑到一半报错，要么（更糟）参考图被静默丢弃。
  let PROVIDER_CAPS = null;

  async function loadProviderCaps(force) {
    if (PROVIDER_CAPS && !force) return PROVIDER_CAPS;
    try {
      PROVIDER_CAPS = await api('/api/providers/capabilities');
    } catch (_) {
      PROVIDER_CAPS = null;   // 拿不到就不限制，保持向后兼容
    }
    return PROVIDER_CAPS;
  }

  function currentCaps() {
    if (!PROVIDER_CAPS) return null;
    return PROVIDER_CAPS.current_caps || null;
  }

  /**
   * 按能力启用/禁用模式 tab；当前模式若不被支持则自动回落到文生图。
   *
   * @param {string} [providerName] 指定服务商（用于设置页切换 radio 时预览效果，
   *   因为此时选择**尚未保存**，`current_caps` 还是旧值）。
   */
  function applyModeAvailability(providerName) {
    let caps = null;
    if (providerName && PROVIDER_CAPS && PROVIDER_CAPS.providers) {
      caps = PROVIDER_CAPS.providers[providerName] || null;
    } else {
      caps = currentCaps();
    }
    if (!caps) return null;
    const modes = caps.modes || ['text'];

    document.querySelectorAll('[data-image-mode]').forEach((el) => {
      const m = el.dataset.imageMode;
      const ok = modes.includes(m);
      el.disabled = !ok;
      el.classList.toggle('unsupported', !ok);
      if (ok) {
        el.removeAttribute('title');
      } else {
        const name = m === 'image' ? '图生图' : '多图生图';
        el.title = `${caps.label || '当前服务商'}不支持${name}`;
      }
    });

    if (!modes.includes(ACTIVE_IMAGE_MODE)) {
      const wasName = ACTIVE_IMAGE_MODE === 'image' ? '图生图' : '多图生图';
      setImageMode('text');
      toast(`${caps.label || '当前服务商'}不支持${wasName}，已切换为文生图`, true);
    }
    return modes;
  }

  /** 当前模式允许的参考图张数上限。 */
  function referenceLimit(mode) {
    const caps = currentCaps();
    if (mode === 'text') return 0;
    const fallback = mode === 'image' ? 1 : 3;
    if (!caps) return fallback;
    const cap = Number(caps.max_references || 0);
    if (!cap) return fallback;
    return mode === 'image' ? Math.min(1, cap) : cap;
  }

  function setImageMode(mode) {
    // 防御：即使被程序化调用，也不允许切到不支持的模式
    const caps = currentCaps();
    if (caps && !(caps.modes || ['text']).includes(mode)) {
      mode = 'text';
    }
    ACTIVE_IMAGE_MODE = mode;
    document.querySelectorAll('[data-image-mode]').forEach((el) => {
      el.classList.toggle('active', el.dataset.imageMode === mode);
    });
    const _lim = referenceLimit(mode);
    const note = mode === 'text'
      ? '文生图不使用参考图。'
      : mode === 'image'
        ? `图生图需要勾选 1 张参考图，作为本次所有选中门店的统一视觉参考。`
        : `多图生图需要勾选 2–${_lim} 张参考图，统一应用于本次所有选中门店。`;
    $('image-mode-note').textContent = note;
    $('reference-controls').hidden = mode === 'text';
    renderReferenceAssets();
  }

  function renderReferenceAssets() {
    const wrap = $('reference-assets');
    if (!wrap) return;
    const workflow = (SETTINGS && SETTINGS.image_workflow) || {};
    const savedIds = new Set((workflow.reference_assets || []).map((asset) => asset.id));
    const selected = new Set(selectedReferenceIds().length ? selectedReferenceIds() : savedIds);
    if (!REFERENCE_ASSETS.length) {
      wrap.innerHTML = '<p class="snote">尚未保存参考图。选择图片后会保存到输出根目录的 <code>_references</code>。</p>';
      return;
    }

    // ---- 已选 N/M 计数器 ----
    // 之前选超了只在点击时弹一次 toast，用户看不到"现在是 3/1"这种状态。
    const lim = referenceLimit(ACTIVE_IMAGE_MODE);
    const n = selected.size;
    const over = lim > 0 && n > lim;
    const counter = ACTIVE_IMAGE_MODE === 'text'
      ? '<p class="snote">文生图不使用参考图，切换模式后可用。</p>'
      : `<div class="ref-counter${over ? ' over' : (n && lim && n === lim ? ' full' : (n ? ' ok' : ''))}">
           <span>已选 <b>${n}</b> / ${lim} 张</span>
           ${over ? `<span class="ref-warn">⚠️ 超出 ${n - lim} 张，请取消勾选后保存</span>`
                  : (n && n >= 2 && lim === 1
                      ? '<span class="ref-warn">⚠️ 图生图只接受 1 张，多余的会被忽略</span>'
                      : '')}
         </div>`;

    wrap.innerHTML = counter + REFERENCE_ASSETS.map((asset) => {
      // 后端压缩过的话，把"原尺寸 → 新尺寸"标出来，让用户知道传的图被缩了
      const c = asset.compressed;
      const shrink = c && c.from && c.to
        ? `<small class="ref-shrink">已压缩 ${c.from[0]}×${c.from[1]} → ${c.to[0]}×${c.to[1]}</small>`
        : '';
      // 缩略图：内容以 SHA-256 命名，天然不可变，可放心缓存
      const thumb = `<img class="ref-thumb" loading="lazy" alt=""
        src="/api/reference-assets/${encodeURIComponent(asset.id)}/raw">`;
      return `
      <label class="reference-card reference-item${selected.has(asset.id) ? ' selected' : ''}">
        <input type="checkbox" value="${esc(asset.id)}"${selected.has(asset.id) ? ' checked' : ''}>
        ${thumb}
        <span class="reference-name">${esc(asset.file_name)}<small>${esc(asset.mime_type || '')} · ${Math.max(1, Math.round((asset.bytes || 0) / 1024))} KB</small>${shrink}</span>
      </label>`;
    }).join('');
    wrap.querySelectorAll('input').forEach((input) => input.addEventListener('change', () => {
      const selectedCount = selectedReferenceIds().length;
      // 上限来自服务商能力，不再硬编码
      const max = referenceLimit(ACTIVE_IMAGE_MODE);
      if (max && selectedCount > max) {
        // 超限：撤销这次勾选，并立即刷新计数器（否则界面上的 N/M 不会更新）
        input.checked = false;
        toast(`${ACTIVE_IMAGE_MODE === 'image' ? '图生图' : '多图生图'}最多选择 ${max} 张参考图`, true);
      }
      renderReferenceAssets();
    }));
  }

  function renderEffectBackgroundAssets() {
    const wrap = $('effect-background-assets');
    if (!wrap) return;
    const workflow = (SETTINGS && SETTINGS.effect_workflow) || {};
    const savedId = (workflow.background_asset || {}).id || '';
    const current = selectedEffectBackgroundId() || SELECTED_EFFECT_BACKGROUND_ID || savedId;
    const simulated = `<label class="reference-card reference-item${!current ? ' selected' : ''}">
      <input type="radio" name="effect-background" value=""${!current ? ' checked' : ''}>
      <span class="reference-name">使用模拟门店<small>本机算法生成；<b>不是实拍</b>，清单会明确标记</small></span>
    </label>`;
    const assets = EFFECT_BACKGROUND_ASSETS.map((asset) => {
      const isAi = asset.kind === 'ai_generated_background';
      const kb = Math.max(1, Math.round((asset.bytes || 0) / 1024));
      const tag = isAi
        ? '<span class="bg-kind ai">AI 生成</span><b>非实拍</b>'
        : '<span class="bg-kind real">实拍</span>';
      return `
      <label class="reference-card reference-item${current === asset.id ? ' selected' : ''}${isAi ? ' ai-generated' : ''}">
        <input type="radio" name="effect-background" value="${esc(asset.id)}"${current === asset.id ? ' checked' : ''}>
        <span class="reference-name">${esc(asset.file_name)}<small>${tag} · ${kb} KB${asset.store_hint ? ' · ' + esc(asset.store_hint) : ''}</small></span>
      </label>`;
    }).join('');
    wrap.innerHTML = simulated + assets;
    wrap.querySelectorAll('input').forEach((input) => input.addEventListener('change', () => {
      SELECTED_EFFECT_BACKGROUND_ID = input.value || '';
      renderEffectBackgroundAssets();
      renderFileSummary();
    }));
  }

  async function generateAiBackground() {
    const btn = $('btn-gen-ai-background');
    const note = $('ai-bg-note');
    if (btn) btn.disabled = true;
    note.className = 'snote';
    note.textContent = '正在生成真实感背景（约 5~10 秒）…';
    try {
      const scopeIdx = (STATE && STATE.scope && STATE.scope.store_indexes) || [];
      const storeObj = ((STATE && STATE.stores) || []).find((s) => s.folder_index === scopeIdx[0]);
      const store = (storeObj && storeObj.main_title) || '通用门店';
      const d = await api('/api/effect-backgrounds/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ store_name: store, count: 1, door: 'single' }),
      });
      if (d.saved) {
        (d.assets || []).forEach((a) => {
          const i = EFFECT_BACKGROUND_ASSETS.findIndex((x) => x.id === a.id);
          if (i >= 0) EFFECT_BACKGROUND_ASSETS.splice(i, 1, a);
          else EFFECT_BACKGROUND_ASSETS.unshift(a);
        });
        SELECTED_EFFECT_BACKGROUND_ID = (d.assets[0] || {}).id || '';
        renderEffectBackgroundAssets();
        renderFileSummary();
        note.className = 'snote ok';
        note.textContent = `✅ 已生成 ${d.saved} 张 AI 背景并自动选中（标记为「AI 生成 · 非实拍」）`;
      } else {
        note.className = 'snote err';
        note.textContent = '❌ ' + ((d.errors || [])[0] || '未生成任何背景');
      }
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ ' + e.message;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function reloadEffectBackgrounds() {
    try {
      const d = await api('/api/effect-background-assets');
      EFFECT_BACKGROUND_ASSETS = d.assets || [];
      EFFECT_BACKGROUND_ROOT = d.root || '';
      renderEffectBackgroundAssets();
      $('ai-bg-note').className = 'snote';
      $('ai-bg-note').textContent = `已刷新，共 ${EFFECT_BACKGROUND_ASSETS.length} 张背景`;
    } catch (e) {
      $('ai-bg-note').className = 'snote err';
      $('ai-bg-note').textContent = '❌ ' + e.message;
    }
  }

  function renderFileSummary() {
    const c = (STATE && STATE.config) || {};
    const batch = (STATE && STATE.batch) || {};
    const references = REFERENCE_ROOT || ((c.output_base_root || c.output_root || '') + '\\_references');
    const effectBackgrounds = EFFECT_BACKGROUND_ROOT || ((c.output_base_root || c.output_root || '') + '\\_effect_backgrounds');
    const background = selectedEffectBackgroundMetadata();
    $('file-summary').innerHTML = `
      <div><b>输出根目录</b><code>${esc(c.output_base_root || c.output_root || '')}</code></div>
      <div><b>当前批次目录</b><code>${esc(c.output_root || '')}</code></div>
      <div><b>命名预览</b><code>01_房屋中介门店/01_楼房线稿.png</code></div>
      <div><b>参考图片目录</b><code>${esc(references)}</code>（按校验值去重）</div>
      <div><b>实拍背景目录</b><code>${esc(effectBackgrounds)}</code>（${background.id ? esc(background.file_name) : '当前使用模拟门店'}）</div>
      <div><b>历史批次</b><span>${esc(batch.label || '原始输出目录')}；每个新批次使用独立时间戳目录。</span></div>`;
  }

  function renderQualityWorkspace() {
    const quality = (SETTINGS && SETTINGS.prompt_quality) || {};
    const workflow = (SETTINGS && SETTINGS.image_workflow) || {};
    $('pq-template').value = quality.template || '';
    $('pq-optimized').value = quality.optimized_template || '';
    $('pq-use-optimized').checked = !!quality.use_optimized;
    $('pq-realism').value = String(quality.realism_iteration || 1);
    $('s-size').value = ((SETTINGS && SETTINGS.generation) || {}).size || '1024x1024';
    setImageMode(workflow.mode || 'text');
    // 拉取服务商能力后，禁用不支持的模式 tab（拿不到就保持原样）
    loadProviderCaps().then(() => applyModeAvailability()).catch(() => {});
    renderQualityPreview();
  }

  function modelOptions(provider, selected) {
    const cat = CATALOG[provider] || {};
    const models = cat.models || [];
    if (!models.length) return '';
    return models.map((m) => {
      const bits = [];
      if (m.price_unknown) bits.push('价格以控制台为准');
      else if (m.price) bits.push(`¥${m.price}/张`); else bits.push('免费');
      bits.push(m.negative ? '负向词✓' : '负向词✗');
      if (m.rpm) bits.push(`${m.rpm}次/分`);
      if (m.max_n > 1) bits.push(`n≤${m.max_n}`);
      const tags = (m.tags || []).join(' ');
      const label = `${m.id}　—　${bits.join(' · ')}${tags ? '　' + tags : ''}`;
      return `<option value="${esc(m.id)}"${m.id === selected ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
  }

  function modelMetaText(provider, modelId) {
    const cat = CATALOG[provider] || {};
    const m = (cat.models || []).find((x) => x.id === modelId);
    if (!m) return '';
    const bits = [];
    if (m.price_unknown) bits.push('价格以控制台为准');
    else if (m.price) bits.push(`<b>¥${m.price}</b>/张`);
    bits.push(m.negative ? '支持负向提示词' : '<b>不支持</b>负向提示词');
    if (m.rpm) bits.push(`限流 ${m.rpm} 次/分`);
    else bits.push('无 QPS 限制');
    if (m.free_quota) bits.push(`免费额度 ${m.free_quota} 张`);
    let html = bits.join(' · ');
    if (m.note) html += `<br>${esc(m.note)}`;
    return html;
  }

  function renderProviderCards() {
    const wrap = $('provider-cards');
    const active = SETTINGS.active_provider;
    const order = ['openai', 'qwen', 'gemini', 'seedream', 'kling', 'zhipu', 'flux_local', 'custom', 'mock'];

    wrap.innerHTML = order.filter((n) => CATALOG[n]).map((name) => {
      const cat = CATALOG[name];
      const cfg = SETTINGS.providers[name] || {};
      const isActive = name === active;
      const isBuiltinLocal = cat.kind === 'local';
      const isFlux = cat.kind === 'local_flux';
      const isLocal = isBuiltinLocal || isFlux;
      const hasKey = cfg.has_key || isLocal;

      const badges = [
        cat.recommended ? '<span class="pcard-badge rec">推荐</span>' : '',
        cat.badge ? `<span class="pcard-badge">${esc(cat.badge)}</span>` : '',
      ].join('');

      const state = isBuiltinLocal
        ? '<span style="color:#047857">✅ 无需配置</span>'
        : (isFlux ? '<span style="color:#b45309">需检测本地服务</span>'
          : (hasKey ? '<span style="color:#047857">已配置</span>' : '<span style="color:#b45309">未配置</span>'));

      const warns = (cat.warnings || []).length
        ? `<div class="pcard-warn">${cat.warnings.map((w) => `<div>⚠️ ${w}</div>`).join('')}</div>`
        : '';

      const body = isBuiltinLocal ? `
        <p class="pcard-summary">${esc(cat.summary || '')}</p>
      ` : isFlux ? `
        <p class="pcard-summary">${esc(cat.summary || '')}</p>
        <div class="pcard-row">
          <span>本地端点</span>
          <input class="p-url" type="text" value="${esc(cfg.base_url || cat.base_url || '')}" spellcheck="false">
        </div>
        <div class="pcard-row">
          <span>模型</span>
          <div><select class="p-model">${modelOptions(name, cfg.model)}</select><div class="model-meta"></div></div>
        </div>
        ${warns}
        <div class="pcard-row" style="margin-top:10px">
          <span></span><div style="display:flex;align-items:center;gap:12px">
            <button class="btn small btn-test" type="button">测试本地服务</button><span class="testmsg"></span>
          </div>
        </div>
      ` : `
        <p class="pcard-summary">${esc(cat.summary || '')}</p>
        <div class="pcard-row">
          <span>API Key</span>
          <div class="keywrap">
            <input class="p-key" type="password" placeholder="粘贴 API Key"
                   value="${esc(cfg.api_key || '')}" autocomplete="off">
            <button class="eye" type="button" title="显示/隐藏">👁</button>
          </div>
        </div>
        <div class="pcard-row">
          <span>Base URL</span>
          <input class="p-url" type="text" value="${esc(cfg.base_url || cat.base_url || '')}" spellcheck="false">
        </div>
        <div class="pcard-row">
          <span>${name === 'custom' ? '模型名' : '模型'}</span>
          <div>
            ${cat.models && cat.models.length
              ? `<select class="p-model">${modelOptions(name, cfg.model)}</select>`
              : `<input class="p-model" type="text" placeholder="填写模型 ID，如 gpt-image-1"
                        value="${esc(cfg.model || '')}" spellcheck="false">`}
            <div class="model-meta"></div>
          </div>
        </div>
        ${warns}
        <div class="pcard-row" style="margin-top:10px">
          <span></span>
          <div style="display:flex;align-items:center;gap:12px">
            <button class="btn small btn-test" type="button">测试连接</button>
            ${name === 'qwen' ? '<button class="btn small btn-test-image" type="button" title="会发起 1 张验证图请求，可能产生一次模型费用">验证图片出图权限</button>' : ''}
            <span class="testmsg"></span>
          </div>
        </div>
      `;

      return `<div class="pcard${isActive ? ' active' : ''}" data-provider="${name}">
        <label class="pcard-head">
          <input type="radio" name="active-provider" value="${name}"${isActive ? ' checked' : ''}>
          <span class="pcard-name">${esc(cat.label)}</span>
          ${badges}
          <span class="pcard-state">${state}</span>
        </label>
        <div class="pcard-body">${body}</div>
      </div>`;
    }).join('');

    // 模型说明联动
    wrap.querySelectorAll('.pcard').forEach((card) => {
      const name = card.dataset.provider;
      const sel = card.querySelector('.p-model');
      const meta = card.querySelector('.model-meta');
      const update = () => { if (meta && sel) meta.innerHTML = modelMetaText(name, sel.value); };
      if (sel) { sel.addEventListener('change', update); update(); }

      const eye = card.querySelector('.eye');
      const keyInput = card.querySelector('.p-key');
      if (eye && keyInput) {
        eye.addEventListener('click', () => {
          keyInput.type = keyInput.type === 'password' ? 'text' : 'password';
        });
      }

      const testBtn = card.querySelector('.btn-test');
      if (testBtn) testBtn.addEventListener('click', () => testConnection(name, card));
      const imageTestBtn = card.querySelector('.btn-test-image');
      if (imageTestBtn) imageTestBtn.addEventListener('click', () => testImageCapability(name, card));
    });

    // 单选切换高亮
    wrap.querySelectorAll('input[name=active-provider]').forEach((radio) => {
      radio.addEventListener('change', () => {
        wrap.querySelectorAll('.pcard').forEach((c) => c.classList.remove('active'));
        radio.closest('.pcard').classList.add('active');
        // 切换服务商 → 立即按其能力刷新模式 tab 的可用性
        // （此时选择尚未保存，所以要显式传入服务商名）
        loadProviderCaps().then(() => applyModeAvailability(radio.value)).catch(() => {});
      });
    });
  }

  function fillForm() {
    const g = SETTINGS.generation || {};
    const o = SETTINGS.output || {};
    const q = SETTINGS.qc || {};
    const gd = SETTINGS.guard || {};
    const pr = SETTINGS.prompt || {};
    const optimizer = SETTINGS.prompt_optimizer || {};
    const po = SETTINGS.postprocess || {};

    $('s-limit').value = String(g.limit ?? 0);
    $('s-size').value = g.size || '1024x1024';
    $('s-concurrency').value = g.concurrency ?? 4;
    $('s-retry').value = g.retry_max ?? 3;
    $('s-timeout').value = g.timeout ?? 120;

    $('s-output').value = o.root || '';
    $('s-timestamp').checked = !!o.timestamp_prefix;
    $('s-overwrite').checked = !!o.overwrite;

    $('s-qc').checked = !!q.enabled;
    $('s-qc-sim').value = q.min_similarity ?? 0.8;
    $('s-budget').value = gd.budget_limit ?? 400;

    // 提示词版本
    const selVer = $('s-prompt-version');
    if (selVer && PROMPT_VERSIONS) {
      const v = pr.version || PROMPT_VERSIONS.current;
      selVer.value = v;
      $('prompt-preview').textContent =
        (PROMPT_VERSIONS.sample && PROMPT_VERSIONS.sample[v]) || '';
    }
    $('s-whiten').checked = po.whiten_background !== false;

    // 印刷 TIF 导出
    const pp = SETTINGS.print || {};
    if ($('s-print-enabled')) $('s-print-enabled').checked = pp.enabled !== false;
    if ($('s-print-width')) $('s-print-width').value = pp.width_cm != null ? pp.width_cm : 60;
    if ($('s-print-dpi')) $('s-print-dpi').value = pp.dpi != null ? pp.dpi : 300;
    if ($('s-print-bleed')) $('s-print-bleed').value = pp.bleed_mm != null ? pp.bleed_mm : 3;
    if ($('s-print-cutout')) {
      // 非法值回落到 auto（与后端 _cutout_mode 保持一致）
      const m = String(pp.cutout || 'auto').toLowerCase();
      $('s-print-cutout').value = ['auto', 'rembg', 'fallback'].includes(m) ? m : 'auto';
    }
    if ($('s-print-white')) $('s-print-white').checked = pp.white_ink !== false;
    if ($('s-print-dieline')) $('s-print-dieline').checked = pp.dieline !== false;
    $('s-deepseek-enabled').checked = optimizer.enabled !== false;
    $('s-deepseek-key').value = optimizer.api_key || '';
    $('s-deepseek-url').value = optimizer.base_url || 'https://api.deepseek.com';
    $('s-deepseek-model').value = optimizer.model || 'deepseek-flash';

    updateConcurrencyNote();
    $('s-output').placeholder = (STATE && STATE.config.output_root) || '';
  }

  function updateConcurrencyNote() {
    const name = document.querySelector('input[name=active-provider]:checked')?.value;
    const cat = CATALOG && CATALOG[name];
    const modelId = document.querySelector(`.pcard[data-provider="${name}"] .p-model`)?.value;
    const m = cat && (cat.models || []).find((x) => x.id === modelId);
    const note = $('concurrency-note');
    if (!m) { note.textContent = ''; note.className = 'snote'; return; }
    if (m.rpm) {
      note.className = 'snote warn';
      note.textContent = `⚠️ 该模型限流 ${m.rpm} 次/分钟，并发数请勿超过此值（正式限流按主账号合并计算，充值不提升）。`;
    } else {
      note.className = 'snote ok';
      note.textContent = '✅ 该模型无 QPS 限制，可适当提高并发（如 8）。';
    }
  }

  function collectSettings() {
    const providers = {};
    document.querySelectorAll('.pcard').forEach((card) => {
      const name = card.dataset.provider;
      const k = card.querySelector('.p-key');
      const u = card.querySelector('.p-url');
      const m = card.querySelector('.p-model');
      if (!k && !u && !m) return;
      providers[name] = {
        api_key: k ? k.value.trim() : '',
        base_url: u ? u.value.trim() : '',
        model: m ? m.value.trim() : '',
      };
    });

    return {
      active_provider: document.querySelector('input[name=active-provider]:checked')?.value || 'mock',
      providers,
      generation: {
        limit: parseInt($('s-limit').value, 10) || 0,
        size: $('s-size').value || '1024x1024',
        concurrency: parseInt($('s-concurrency').value, 10) || 4,
        retry_max: parseInt($('s-retry').value, 10) || 0,
        timeout: parseFloat($('s-timeout').value) || 120,
      },
      output: {
        root: $('s-output').value.trim(),
        timestamp_prefix: $('s-timestamp').checked,
        overwrite: $('s-overwrite').checked,
        on_path_change: 'keep',
      },
      qc: {
        enabled: $('s-qc').checked,
        min_similarity: parseFloat($('s-qc-sim').value) || 0.8,
      },
      guard: { budget_limit: parseInt($('s-budget').value, 10) || 400 },
      prompt: {
        version: $('s-prompt-version') ? $('s-prompt-version').value : '',
      },
      scope: { store_indexes: selectedScopeIndexes() },
      image_workflow: {
        mode: ACTIVE_IMAGE_MODE,
        reference_assets: ACTIVE_IMAGE_MODE === 'text' ? [] : selectedReferenceMetadata(),
      },
      prompt_quality: {
        template: $('pq-template').value.trim(),
        optimized_template: $('pq-optimized').value.trim(),
        use_optimized: $('pq-use-optimized').checked,
        realism_iteration: parseInt($('pq-realism').value, 10) || 1,
      },
      effect_workflow: { background_asset: selectedEffectBackgroundMetadata() },
      prompt_optimizer: {
        enabled: $('s-deepseek-enabled').checked,
        api_key: $('s-deepseek-key').value.trim(),
        base_url: $('s-deepseek-url').value.trim(),
        model: $('s-deepseek-model').value.trim(),
      },
      postprocess: {
        whiten_background: $('s-whiten').checked,
      },
      print: {
        enabled: $('s-print-enabled') ? $('s-print-enabled').checked : true,
        width_cm: parseFloat(($('s-print-width') || {}).value) || 60,
        dpi: parseInt(($('s-print-dpi') || {}).value, 10) || 300,
        bleed_mm: parseFloat(($('s-print-bleed') || {}).value) || 0,
        cutout: ($('s-print-cutout') || {}).value || 'auto',
        white_ink: $('s-print-white') ? $('s-print-white').checked : true,
        dieline: $('s-print-dieline') ? $('s-print-dieline').checked : true,
      },
    };
  }

  async function saveSettings() {
    const btn = $('btn-save-settings');
    const msg = $('settings-msg');
    btn.disabled = true;
    msg.className = 'msg';
    msg.textContent = '保存中…';
    try {
      const d = await api('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectSettings()),
      });
      SETTINGS = d.settings;
      renderScope();
      renderQualityWorkspace();
      renderFileSummary();
      if (d.problems && d.problems.length) {
        msg.className = 'msg err';
        msg.textContent = '已保存，但：' + d.problems.join('；');
      } else {
        msg.className = 'msg ok';
        msg.textContent = '✅ 已保存并生效。测试结果会保留在当前窗口；请按需继续验证图片出图权限。';
      }
      await loadState();
      toast('设置已保存');
    } catch (e) {
      msg.className = 'msg err';
      msg.textContent = '保存失败：' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function testConnection(name, card) {
    const msg = card.querySelector('.testmsg');
    const btn = card.querySelector('.btn-test');
    const keyInput = card.querySelector('.p-key');
    const urlInput = card.querySelector('.p-url');
    msg.className = 'testmsg busy';
    msg.textContent = '测试中…';
    btn.disabled = true;
    try {
      const d = await api('/api/settings/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: name,
          api_key: keyInput ? keyInput.value : '',
          base_url: urlInput ? urlInput.value : '',
        }),
      });
      const cls = d.ok ? (['rate_limited', 'unverified', 'connection_only'].includes(d.status) ? 'warn' : 'ok') : 'err';
      msg.className = 'testmsg ' + cls;
      msg.textContent = (d.ok ? '✅ ' : '❌ ') + d.message;
      if (d.status === 'ok') await loadState();
    } catch (e) {
      msg.className = 'testmsg err';
      msg.textContent = '❌ ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function testImageCapability(name, card) {
    const msg = card.querySelector('.testmsg');
    const btn = card.querySelector('.btn-test-image');
    const keyInput = card.querySelector('.p-key');
    const urlInput = card.querySelector('.p-url');
    const modelInput = card.querySelector('.p-model');
    msg.className = 'testmsg busy';
    msg.textContent = '正在验证图片出图权限（会发起 1 张验证图请求）…';
    btn.disabled = true;
    try {
      const d = await api('/api/settings/test-image', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: name,
          api_key: keyInput ? keyInput.value : '',
          base_url: urlInput ? urlInput.value : '',
          model: modelInput ? modelInput.value : '',
        }),
      });
      msg.className = 'testmsg ' + (d.ok ? 'ok' : 'err');
      msg.textContent = (d.ok ? '✅ ' : '❌ ') + d.message;
      if (d.ok) await loadState();
    } catch (e) {
      msg.className = 'testmsg err';
      msg.textContent = '❌ ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function testDeepSeekConnection() {
    const msg = $('deepseek-testmsg');
    const btn = $('btn-deepseek-test');
    msg.className = 'testmsg busy';
    msg.textContent = '测试中…';
    btn.disabled = true;
    try {
      const d = await api('/api/settings/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: 'deepseek', api_key: $('s-deepseek-key').value,
          base_url: $('s-deepseek-url').value,
        }),
      });
      msg.className = 'testmsg ' + (d.ok ? 'ok' : 'err');
      msg.textContent = (d.ok ? '✅ ' : '❌ ') + d.message;
    } catch (e) {
      msg.className = 'testmsg err';
      msg.textContent = '❌ ' + e.message;
    } finally { btn.disabled = false; }
  }

  async function uploadReferenceFiles(files) {
    const picked = [...files];
    if (!picked.length) return;
    for (const file of picked) {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
        toast(`${file.name} 不是 PNG、JPG 或 WEBP 图片`, true);
        continue;
      }
      if (file.size > 10 * 1024 * 1024) {
        toast(`${file.name} 超过 10MB`, true);
        continue;
      }
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error('读取文件失败'));
          reader.readAsDataURL(file);
        });
        const result = await api('/api/reference-assets', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data_url: dataUrl, file_name: file.name }),
        });
        const existing = REFERENCE_ASSETS.findIndex((asset) => asset.id === result.asset.id);
        if (existing >= 0) REFERENCE_ASSETS.splice(existing, 1, result.asset);
        else REFERENCE_ASSETS.unshift(result.asset);
        toast(result.deduplicated ? `${file.name} 已使用已有参考图` : `已保存参考图：${file.name}`);
      } catch (e) {
        toast(`上传 ${file.name} 失败：${e.message}`, true);
      }
    }
    renderReferenceAssets();
  }

  async function uploadEffectBackgroundFiles(files) {
    const picked = [...files];
    if (!picked.length) return;
    for (const file of picked) {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
        toast(`${file.name} 不是 PNG、JPG 或 WEBP 图片`, true);
        continue;
      }
      if (file.size > 10 * 1024 * 1024) {
        toast(`${file.name} 超过 10MB`, true);
        continue;
      }
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error('读取文件失败'));
          reader.readAsDataURL(file);
        });
        const result = await api('/api/effect-background-assets', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data_url: dataUrl, file_name: file.name }),
        });
        const existing = EFFECT_BACKGROUND_ASSETS.findIndex((asset) => asset.id === result.asset.id);
        if (existing >= 0) EFFECT_BACKGROUND_ASSETS.splice(existing, 1, result.asset);
        else EFFECT_BACKGROUND_ASSETS.unshift(result.asset);
        SELECTED_EFFECT_BACKGROUND_ID = result.asset.id;
        toast(`已保存实拍背景：${file.name}；仅用于本机效果图`);
      } catch (e) {
        toast(`上传 ${file.name} 失败：${e.message}`, true);
      }
    }
    renderEffectBackgroundAssets();
    renderFileSummary();
  }

  async function optimizeQualityTemplate() {
    const btn = $('btn-quality-optimize');
    const msg = $('quality-msg');
    const template = $('pq-template').value.trim();
    if (!template) { toast('请先填写项目质量模板', true); return; }
    btn.disabled = true;
    msg.className = 'snote';
    msg.textContent = '正在用 DeepSeek 优化通用模板，只调用一次…';
    try {
      const d = await api('/api/prompt-profile/optimize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template }),
      });
      $('pq-optimized').value = d.template || '';
      $('pq-use-optimized').checked = true;
      msg.className = 'snote ok';
      msg.textContent = '✅ 已生成优化预览。请查看“上下文”和“图像”，再确认创建新批次。';
      renderQualityPreview();
    } catch (e) {
      msg.className = 'snote err';
      msg.textContent = '❌ 优化失败：' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function confirmPromptProfileAndRun() {
    const indexes = selectedScopeIndexes();
    const referenceIds = ACTIVE_IMAGE_MODE === 'text' ? [] : selectedReferenceIds();
    if (!indexes.length) { toast('至少选择 1 个门店文件夹', true); return; }
    if (ACTIVE_IMAGE_MODE === 'image' && referenceIds.length !== 1) {
      toast('图生图必须选择 1 张参考图', true); return;
    }
    if (ACTIVE_IMAGE_MODE === 'multi' && (referenceIds.length < 2 || referenceIds.length > 3)) {
      toast('多图生图必须选择 2–3 张参考图', true); return;
    }
    const btn = $('btn-quality-confirm-run');
    btn.disabled = true;
    try {
      const d = await api('/api/prompt-profile/confirm-run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          store_indexes: indexes,
          image_mode: ACTIVE_IMAGE_MODE,
          reference_asset_ids: referenceIds,
          template: $('pq-template').value.trim(),
          optimized_template: $('pq-optimized').value.trim(),
          use_optimized: $('pq-use-optimized').checked,
          size: $('s-size').value || '1024x1024',
          realism_iteration: parseInt($('pq-realism').value, 10) || 1,
          effect_background_asset_id: selectedEffectBackgroundId(),
        }),
      });
      await loadState();
      closeSettings();
      toast(`已保存提示词与范围，已创建 ${d.batch && d.batch.label ? d.batch.label : '新批次'}，开始生成 ${d.scope && d.scope.image_count ? d.scope.image_count : indexes.length * 6} 张`);
    } catch (e) {
      toast('无法创建新批次：' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  const localImageUrl = (file) =>
    `/api/local-validation/image?file=${encodeURIComponent(file)}`;

  async function loadLocalValidation() {
    const note = $('local-validation-status');
    const grid = $('local-validation-images');
    if (!note || !grid) return;
    note.className = 'snote';
    note.textContent = '读取本地模型状态…';
    try {
      const d = await api('/api/local-validation/state');
      const h = d.health || {};
      const device = h.health && h.health.gpu
        ? ` · ${h.health.gpu} · ${h.health.vram_total_mib} MiB` : '';
      note.className = 'snote ' + (h.ok ? 'ok' : 'err');
      note.textContent = (h.ok ? '✅ ' : '❌ ') + (h.message || '状态未知') + device + ` · 输出：${d.output_root}`;
      const rows = (d.report && d.report.rows) || [];
      grid.innerHTML = rows.map((row) => {
        const file = row['文件名'];
        const theme = row['主题'];
        const ok = row.png_valid && row.status === 'success';
        const image = ok ? `<img src="${localImageUrl(file)}&t=${Date.now()}" alt="${esc(theme)}" data-lv-file="${esc(file)}">` : '';
        const peak = row.runtime_metrics && row.runtime_metrics.peak_vram_mib;
        const caption = ok
          ? `${esc(theme)}<br>${esc(row.elapsed || '?')}s${peak ? ` · ${esc(peak)} MiB` : ''}`
          : `${esc(theme)}<br>${esc(row.error || row.status || '未生成')}`;
        return `<div class="lv-image${ok ? '' : ' failed'}">${image}<div class="lv-caption">${caption}</div></div>`;
      }).join('') || '<p class="snote">尚无本地样图。启动模型服务后运行固定验证。</p>';
      grid.querySelectorAll('img[data-lv-file]').forEach((img) => img.addEventListener('click', () => {
        $('lightbox-img').src = img.src;
        $('lightbox-cap').textContent = img.alt;
        $('lightbox').classList.add('show');
      }));
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ 读取本地验证状态失败：' + e.message;
    }
  }

  async function runLocalValidation() {
    const btn = $('btn-local-validation-run');
    btn.disabled = true;
    try {
      const d = await api('/api/local-validation/run', { method: 'POST' });
      setRunning(true);
      toast('已启动：' + d.scope);
      loadLocalValidation();
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  const professionalImageUrl = (file) =>
    `/api/professional-validation/image?file=${encodeURIComponent(file)}`;

  async function loadProfessionalValidation() {
    const note = $('professional-status');
    const grid = $('professional-images');
    if (!note || !grid) return;
    note.className = 'snote';
    note.textContent = '读取 V9 本地状态…';
    try {
      const d = await api('/api/professional-validation/state');
      const h = d.health || {};
      const report = d.report || {};
      const device = h.health && h.health.gpu ? ` · ${h.health.gpu}` : '';
      note.className = 'snote ' + (h.ok ? 'ok' : 'err');
      note.textContent = (h.ok ? '✅ ' : '❌ ') + (h.message || '状态未知') + device +
        ` · ${d.scope.images} 主题 × ${d.scope.candidates_per_theme} 候选 · 输出：${d.output_root}`;
      const rows = report.rows || [];
      grid.innerHTML = rows.map((row) => {
        const selected = row.selected || {};
        const ok = selected && !selected.error;
        const image = ok ? `<img src="${professionalImageUrl(row.file)}&t=${Date.now()}" alt="${esc(row.theme)}" data-prof-file="${esc(row.file)}">` : '';
        const caption = ok ? `${esc(row.theme)}<br>优选 ${esc(selected.score)} 分 · 种子 ${esc(selected.seed)}` : `${esc(row.theme)}<br>未完成`;
        return `<div class="lv-image${ok ? '' : ' failed'}">${image}<div class="lv-caption">${caption}</div></div>`;
      }).join('') || '<p class="snote">尚无 V9 专业样图。运行后会保留每主题的最高分成品。</p>';
      grid.querySelectorAll('img[data-prof-file]').forEach((img) => img.addEventListener('click', () => {
        $('lightbox-img').src = img.src;
        $('lightbox-cap').textContent = img.alt;
        $('lightbox').classList.add('show');
      }));
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ 读取 V9 状态失败：' + e.message;
    }
  }

  async function runProfessionalValidation() {
    const btn = $('btn-professional-run');
    btn.disabled = true;
    try {
      const d = await api('/api/professional-validation/run', { method: 'POST' });
      setRunning(true);
      toast('已启动：' + d.scope);
      loadProfessionalValidation();
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function validatePath() {
    const note = $('path-note');
    note.className = 'snote';
    note.textContent = '校验中…';
    try {
      const d = await api('/api/settings/validate-path', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: $('s-output').value.trim() }),
      });
      note.className = 'snote ' + (d.ok ? 'ok' : 'err');
      note.textContent = (d.ok ? '✅ ' : '❌ ') + d.message;
      if (d.ok && d.resolved) $('s-output').value = d.resolved;
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ ' + e.message;
    }
  }

  async function openDir() {
    try {
      const d = await api('/api/settings/open-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: $('s-output').value.trim() }),
      });
      toast('已打开：' + d.path);
    } catch (e) { toast('打开失败：' + e.message, true); }
  }

  async function regenerate(storeIdx, picIdx, btn) {
    const old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
    try {
      await api('/api/regenerate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ store: storeIdx, pic: picIdx }),
      });
      toast(`正在重新生成 ${storeIdx}-${picIdx}…`);
    } catch (e) {
      toast('重新生成失败：' + e.message, true);
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  async function regenerateOpenAIHouse(btn) {
    const old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
    try {
      const d = await api('/api/openai/house-regenerate', { method: 'POST' });
      setRunning(true);
      toast(d.scope || '正在生成房屋中介 6 张新图');
    } catch (e) {
      toast('生成失败：' + e.message, true);
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  async function exportFile(kind) {
    const note = $('export-note');
    note.className = 'snote';
    note.textContent = '导出中…';
    const urls = {
      titles: '/api/export/titles',
      checklist: '/api/export/checklist',
      package: '/api/export/package',
    };
    try {
      const d = await api(urls[kind], { method: 'POST' });
      const extra = d.images ? `（${d.images} 张，${d.size_mb} MB）`
                  : (d.rows ? `（${d.rows} 行）` : '');
      note.className = 'snote ok';
      note.textContent = `✅ ${d.path} ${extra}`;
      toast('导出完成');
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ ' + e.message;
    }
  }

  // ---------------------------------------------------------- 版本对比
  async function openHistory(store, file, label) {
    $('history-title').textContent = '历史版本对比 — ' + (label || file);
    $('compare-grid').innerHTML = '<p class="snote">读取中…</p>';
    $('history-modal').classList.add('show');
    try {
      const d = await api(`/api/history?store=${encodeURIComponent(store)}&file=${encodeURIComponent(file)}`);
      const cur = `/api/image?store=${encodeURIComponent(store)}&file=${encodeURIComponent(file)}`;
      const cards = [
        `<div class="compare-item current">
           <img src="${cur}&t=${Date.now()}" alt="当前版本">
           <div class="compare-cap">
             <b>当前版本</b>
             <span class="compare-tag">使用中</span>${esc(STATE?.config?.provider_label || '')} · ${esc(STATE?.config?.model || '')}
           </div>
         </div>`,
      ];
      (d.items || []).forEach((it) => {
        const u = `/api/history/image?store=${encodeURIComponent(store)}&name=${encodeURIComponent(it.file_name)}`;
        cards.push(`<div class="compare-item">
           <img src="${u}" alt="${esc(it.tag)}" loading="lazy">
           <div class="compare-cap">
             <b>${esc(it.tag || '历史版本')}</b>
             <span class="compare-tag">已归档</span>${esc(it.timestamp || '')} · ${(it.size / 1024).toFixed(0)} KB
             <button class="btn small btn-history-restore" type="button"
                     data-store="${esc(store)}" data-file="${esc(file)}" data-history-file="${esc(it.file_name)}">恢复此版</button>
           </div>
         </div>`);
      });
      $('compare-grid').innerHTML = cards.join('');
      $('history-note').textContent = d.count
        ? `共 ${d.count} 个历史版本（不含当前版本）。左侧为当前使用中的图片。`
        : '暂无历史版本。重新生成该图后，旧图会自动归档到这里。';
    } catch (e) {
      $('compare-grid').innerHTML = `<p class="snote err">读取失败：${esc(e.message)}</p>`;
    }
  }

  function closeHistory() { $('history-modal').classList.remove('show'); }

  async function restoreHistory(btn) {
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = '恢复中…';
    try {
      await api('/api/history/restore', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          store: btn.dataset.store, file: btn.dataset.file, history_file: btn.dataset.historyFile,
        }),
      });
      toast('已恢复历史版本；恢复前的当前图已归档');
      await loadState();
      await openHistory(btn.dataset.store, btn.dataset.file, btn.dataset.file);
    } catch (e) {
      toast('恢复失败：' + e.message, true);
      btn.disabled = false; btn.textContent = old;
    }
  }

  // ---------------------------------------------------------- 待处理项
  async function loadPending() {
    const list = $('pending-list');
    const badge = $('pending-badge');
    list.innerHTML = '<p class="snote">读取中…</p>';
    try {
      const d = await api('/api/failures');
      const total = d.total_pending || 0;
      badge.textContent = total ? `${total} 项` : '全部完成';
      badge.className = 'tag-count ' + (total ? 'has' : 'ok');

      const rows = [];
      (d.failed || []).forEach((f) => {
        const paused = f.status === 'paused';
        rows.push(`<div class="pending-row">
          <span class="pid">${esc(f.store)}-${esc(f.pic)}</span>
          <span class="ptheme">${esc(f.store_name)} / ${esc(f.theme)}</span>
          <span class="perr" title="${esc(f.error)}">${esc(f.error)}</span>
          <span class="pstat${paused ? ' missing' : ''}">${paused ? '已暂停，充值后继续' : `失败 ${f.attempts}次`}</span>
        </div>`);
      });
      (d.missing || []).slice(0, 60).forEach((m) => {
        rows.push(`<div class="pending-row">
          <span class="pid">${esc(m.store)}-${esc(m.pic)}</span>
          <span class="ptheme">${esc(m.store_name)} / ${esc(m.theme)}</span>
          <span class="pstat missing">未生成</span>
        </div>`);
      });

      list.innerHTML = rows.length ? rows.join('')
        : '<p class="snote ok">✅ 138 张全部已生成，无待处理项</p>';
      $('pending-note').textContent = total
        ? `失败 ${d.failed_count} 项，未生成 ${d.missing_count} 项。点击「一键重试全部」可批量补跑。`
        : '';
    } catch (e) {
      list.innerHTML = `<p class="snote err">读取失败：${esc(e.message)}</p>`;
    }
  }

  async function retryFailures() {
    const btn = $('btn-retry-failures');
    btn.disabled = true;
    $('pending-note').className = 'snote';
    $('pending-note').textContent = '正在启动重试…';
    try {
      const d = await api('/api/retry-failures', { method: 'POST' });
      $('pending-note').className = 'snote ok';
      $('pending-note').textContent = d.count
        ? `✅ 已启动重试 ${d.count} 项`
        : '✅ ' + (d.message || '没有需要重试的项');
      toast(d.count ? `正在重试 ${d.count} 项` : '无待处理项');
      if (d.count) setRunning(true);
    } catch (e) {
      $('pending-note').className = 'snote err';
      $('pending-note').textContent = '❌ ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------- 运行历史

  // ---------------------------------------------------------- 效果图参数微调
  function currentStoreName() {
    const idx = (STATE && STATE.scope && STATE.scope.store_indexes) || [];
    const obj = ((STATE && STATE.stores) || []).find((s) => s.folder_index === idx[0]);
    return (obj && obj.main_title) || '';
  }

  async function loadEffectParams() {
    const note = $('effect-param-note');
    try {
      const d = await api('/api/effect-params');
      EFFECT_PARAMS = d;
      renderEffectParams();
      note.className = 'snote';
      note.textContent = d.has_source
        ? '预览源：最近的生成图；拖动滑块后点「预览」'
        : '⚠️ 尚无真实生成图，无法预览（可先用 mock 跑一批）';
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '读取参数失败：' + e.message;
    }
  }

  function renderEffectPresets() {
    const wrap = $('effect-param-presets');
    if (!wrap || !EFFECT_PARAMS || !EFFECT_PARAMS.presets) return;
    const items = Object.entries(EFFECT_PARAMS.presets);
    wrap.innerHTML = '<span class="ep-presets-label">快速预设：</span>'
      + items.map((pair) => {
        const key = pair[0], p = pair[1];
        return '<button type="button" class="btn small ep-preset" data-preset="'
          + esc(key) + '" title="' + esc(p.note) + '">' + esc(p.label) + '</button>';
      }).join('');
    wrap.querySelectorAll('.ep-preset').forEach((btn) => {
      btn.addEventListener('click', () => applyEffectPreset(btn.dataset.preset));
    });
  }

  function applyEffectPreset(key) {
    if (!EFFECT_PARAMS || !EFFECT_PARAMS.presets || !EFFECT_PARAMS.presets[key]) return;
    const preset = EFFECT_PARAMS.presets[key];
    Object.entries(preset.params).forEach((pair) => {
      const k = pair[0], v = pair[1];
      const range = document.querySelector('.ep-range[data-key="' + k + '"]');
      if (range) {
        range.value = v;
        const t = $('ep-val-' + k);
        if (t) t.textContent = v;
      }
    });
    const note = $('effect-param-note');
    note.className = 'snote';
    note.textContent = '已套用预设「' + preset.label + '」：' + preset.note
      + '（点「预览」查看，满意后「保存参数」）';
  }

  function renderEffectParams() {
    const wrap = $('effect-param-groups');
    if (!wrap || !EFFECT_PARAMS) return;
    const groups = {};
    Object.entries(EFFECT_PARAMS.spec).forEach((pair) => {
      const key = pair[0], spec = pair[1];
      if (!groups[spec.group]) groups[spec.group] = [];
      groups[spec.group].push([key, spec]);
    });
    wrap.innerHTML = Object.entries(groups).map((entry) => {
      const group = entry[0], items = entry[1];
      const rows = items.map((pair) => {
        const key = pair[0], spec = pair[1];
        const val = EFFECT_PARAMS.current[key];
        const def = EFFECT_PARAMS.defaults[key];
        return '<label class="ep-row" title="' + esc(spec.hint) + '">'
          + '<span class="ep-label">' + esc(spec.label) + '</span>'
          + '<input type="range" class="ep-range" data-key="' + esc(key) + '"'
          + ' min="' + spec.min + '" max="' + spec.max + '" step="' + spec.step + '"'
          + ' value="' + val + '">'
          + '<span class="ep-value" id="ep-val-' + esc(key) + '">' + val + '</span>'
          + '<button type="button" class="ep-reset" data-key="' + esc(key) + '"'
          + ' data-def="' + def + '" title="恢复默认 ' + def + '">↺</button>'
          + '</label>';
      }).join('');
      return '<div class="ep-group"><div class="ep-group-title">' + esc(group)
        + '</div>' + rows + '</div>';
    }).join('');

    wrap.querySelectorAll('.ep-range').forEach((el) => {
    renderEffectPresets();

      el.addEventListener('input', () => {
        const t = $('ep-val-' + el.dataset.key);
        if (t) t.textContent = el.value;
      });
    });
    wrap.querySelectorAll('.ep-reset').forEach((el) => {
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        const k = el.dataset.key;
        const range = wrap.querySelector('.ep-range[data-key="' + k + '"]');
        if (range) {
          range.value = el.dataset.def;
          const t = $('ep-val-' + k);
          if (t) t.textContent = el.dataset.def;
        }
      });
    });
  }

  function collectEffectParams() {
    const out = {};
    document.querySelectorAll('.ep-range').forEach((el) => {
      out[el.dataset.key] = parseFloat(el.value);
    });
    return out;
  }

  async function previewEffectParams() {
    const btn = $('btn-effect-preview');
    const note = $('effect-param-note');
    if (btn) btn.disabled = true;
    note.className = 'snote';
    note.textContent = '合成中…';
    try {
      const d = await api('/api/effect-params/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ params: collectEffectParams(), store_name: currentStoreName() }),
      });
      const img = $('effect-preview-img');
      img.src = d.url + '?t=' + Date.now();
      img.hidden = false;
      $('effect-preview-empty').hidden = true;
      note.className = 'snote ok';
      note.textContent = '✅ ' + d.width + '×' + d.height + ' · 背景 ' + d.background_mode
        + ' · 源图 ' + d.source;
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ ' + e.message;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function saveEffectParams() {
    const note = $('effect-param-note');
    try {
      await api('/api/effect-params', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ params: collectEffectParams() }),
      });
      note.className = 'snote ok';
      note.textContent = '✅ 参数已保存，批量合成将使用这组参数';
      toast('效果图参数已保存');
    } catch (e) {
      note.className = 'snote err';
      note.textContent = '❌ ' + e.message;
    }
  }

  function resetEffectParams() {
    if (!EFFECT_PARAMS) return;
    const defs = EFFECT_PARAMS.defaults;
    document.querySelectorAll('.ep-range').forEach((el) => {
      const k = el.dataset.key;
      if (defs[k] !== undefined) {
        el.value = defs[k];
        const t = $('ep-val-' + k);
        if (t) t.textContent = defs[k];
      }
    });
    const note = $('effect-param-note');
    note.className = 'snote';
    note.textContent = '已恢复为实测默认值（尚未保存，点「保存参数」生效）';
  }

  async function loadRuns() {
    const list = $('runs-list');
    list.innerHTML = '<p class="snote">读取中…</p>';
    try {
      const d = await api('/api/runs');
      const s = d.summary || {};
      $('runs-summary').innerHTML = s.runs
        ? `累计运行 <b>${s.runs}</b> 次，成功 <b>${s.total_success}</b> 张，`
          + `调用 <b>${s.total_calls}</b> 次，花费约 <b>¥${s.total_cost}</b>，`
          + `总耗时 <b>${(s.total_elapsed / 60).toFixed(1)}</b> 分钟`
        : '暂无运行记录。执行一次生成后会在此显示。';

      list.innerHTML = (d.runs || []).slice(0, 20).map((r) => `
        <div class="run-row">
          <span class="rtime">${esc(r.finished_at)}</span>
          <span class="rmodel">${esc(r.model || r.provider)}</span>
          <span class="rtag">${esc(r.prompt_version || '')}</span>
          <span class="rtag">${esc(r.scope || '')}</span>
          <span class="rstat">
            成功 <b>${r.success}</b> · 失败 <b>${r.failed}</b> ·
            跳过 <b>${r.skipped}</b> · ${r.elapsed}s · ¥${r.cost}
          </span>
        </div>`).join('') || '<p class="snote">暂无记录</p>';
    } catch (e) {
      list.innerHTML = `<p class="snote err">读取失败：${esc(e.message)}</p>`;
    }
  }

  async function resetSettings() {
    if (!confirm('恢复默认设置？\n（已填写的 API Key 会保留）')) return;
    try {
      const d = await api('/api/settings/reset', { method: 'POST' });
      SETTINGS = d.settings;
      renderProviderCards();
      fillForm();
      toast('已恢复默认设置');
    } catch (e) { toast('恢复失败：' + e.message, true); }
  }

  // ============================================================ SSE
  function connectSSE() {
    const es = new EventSource('/api/events');
    es.onmessage = (ev) => {
      let e; try { e = JSON.parse(ev.data); } catch { return; }
      handleEvent(e);
    };
    es.onerror = () => {
      logLine('⚠️ 事件流中断，3 秒后重连…', 'warn');
      es.close();
      setTimeout(connectSSE, 3000);
    };
  }

  function handleEvent(e) {
    switch (e.type) {
      case 'run_started':
        setRunning(true);
        logLine(`▶ 开始：共 ${e.total} 张，待生成 ${e.pending}，跳过 ${e.skipped}`, 'ok');
        renderMeta(`运行中 · ${e.provider}`);
        break;
      case 'job_started':
        setThumb(e.uid, 'running');
        break;
      case 'job_success':
        setThumb(e.uid, 'success');
        if (IMAGE_VIEW === 'generated') refreshThumbImage(e.uid, e.file);
        bumpProgress();
        updateCardBadge(e.store);
        logLine(`✓ [${e.uid}] ${e.file}（${e.elapsed}s${e.attempts > 1 ? `，第${e.attempts}次` : ''}）`, 'ok');
        break;
      case 'job_retry':
        logLine(`↻ [${e.uid}] 第 ${e.attempt} 次失败：${e.error}`, 'warn');
        break;
      case 'job_failed':
        setThumb(e.uid, 'failed');
        logLine(`✗ [${e.uid}] 失败：${e.error}`, 'err');
        break;
      case 'job_paused':
        setThumb(e.uid, 'paused');
        logLine(`⚠ [${e.uid}] 因账户额度不足暂停：${e.error}`, 'warn');
        break;
      case 'run_paused':
        setRunning(false);
        logLine('⚠ 当前批次已暂停。充值后在设置中验证图片出图权限成功，再继续当前批次。', 'warn');
        loadState();
        break;
      case 'run_finished':
        setRunning(false);
        logLine(`■ 结束：成功 ${e.success}，失败 ${e.failed}，跳过 ${e.skipped}，耗时 ${e.elapsed}s`,
          e.failed ? 'warn' : 'ok');
        renderMeta();
        loadState();
        break;
      case 'run_cancelled':
        setRunning(false);
        logLine('■ 当前批次已停止；可继续当前批次，或点击“重新生成（新批次）”按当前范围创建新目录。', 'warn');
        loadState();
        break;
      case 'openai_house_finished':
        setRunning(false);
        logLine(`■ OpenAI 房屋中介新图完成：成功 ${e.success}，失败 ${e.failed}，变体轮次 ${e.variation_cycle}`, e.failed ? 'warn' : 'ok');
        loadState();
        break;
      case 'openai_house_cancelled':
        setRunning(false);
        logLine('■ OpenAI 房屋中介新图已中断，未完成主题保持原图', 'warn');
        loadState();
        break;
      case 'local_validation_finished':
        setRunning(false);
        logLine(`■ 本地样图验证完成：成功 ${e.success}，失败 ${e.failed}，评级 ${e.grade}`, e.failed ? 'warn' : 'ok');
        loadLocalValidation();
        break;
      case 'professional_finished':
        setRunning(false);
        logLine(`■ V9 专业贴纸完成：入选 ${e.selected} 张，平均评分 ${e.mean_score ?? '?'}`, e.technical_pass ? 'ok' : 'warn');
        loadProfessionalValidation();
        break;
      case 'professional_cancelled':
        setRunning(false);
        logLine('■ V9 专业贴纸已停止', 'warn');
        loadProfessionalValidation();
        break;
      case 'run_error':
        setRunning(false);
        logLine('✗ 运行异常：' + e.error, 'err');
        toast('运行异常：' + e.error, true);
        break;
      case 'regen_finished':
        setRunning(false);
        logLine(`↻ 单张重生成完成：[${e.store}-${e.pic}] 成功 ${e.success}，失败 ${e.failed}`, 'ok');
        refreshThumbImage(`${e.store}-${e.pic}`);
        loadState();
        break;
      case 'stopping':
        logLine('… 正在中断当前请求，不会启动后续图片', 'warn');
        break;
      case 'settings_saved':
        logLine('⚙ 设置已更新');
        break;
      case 'history_restored':
        logLine(`🕘 已恢复历史版本：${e.store} / ${e.file}`, 'ok');
        loadState();
        break;
      case 'effect_run_started':
        logLine(`🪟 开始本地合成 ${e.pending} 张玻璃效果图，不调用图像模型`, 'ok');
        loadState();
        break;
      case 'effect_ready':
        if (IMAGE_VIEW === 'effect') loadState();
        break;
      case 'effect_failed':
        logLine(`⚠ 效果图合成失败：${e.error}`, 'warn');
        break;
      case 'effect_run_finished':
        logLine(`■ 玻璃效果图完成：新增 ${e.success}，已有 ${e.skipped}，失败 ${e.failed}`, e.failed ? 'warn' : 'ok');
        loadState();
        break;
      case 'log':
        logLine(e.line);
        break;
    }
  }

  // ============================================================ 初始化
  function init() {
    $('btn-run').addEventListener('click', start);
    $('btn-new-batch').addEventListener('click', startNewBatch);
    $('btn-stop').addEventListener('click', stop);
    $('image-view-generated').addEventListener('click', () => setImageView('generated'));
    $('image-view-effect').addEventListener('click', () => setImageView('effect'));
    $('btn-generate-effects').addEventListener('click', generateMissingEffects);
    $('btn-clear-log').addEventListener('click', () => { $('log').innerHTML = ''; });

    // 设置
    $('btn-settings').addEventListener('click', openSettings);
    $('btn-close-settings').addEventListener('click', closeSettings);
    $('btn-save-settings').addEventListener('click', saveSettings);
    $('btn-reset-settings').addEventListener('click', resetSettings);
    $('settings-nav').addEventListener('click', (ev) => {
      const item = ev.target.closest('.settings-nav-item');
      if (item) selectSettingsPane(item.dataset.pane);
    });
    $('btn-scope-all').addEventListener('click', () => {
      document.querySelectorAll('#scope-list input[type=checkbox]').forEach((input) => { input.checked = true; });
      updateScopeSummary();
    });
    $('btn-scope-none').addEventListener('click', () => {
      document.querySelectorAll('#scope-list input[type=checkbox]').forEach((input) => { input.checked = false; });
      updateScopeSummary();
    });
    $('btn-scope-invert').addEventListener('click', () => {
      document.querySelectorAll('#scope-list input[type=checkbox]').forEach((input) => { input.checked = !input.checked; });
      updateScopeSummary();
    });
    $('scope-search').addEventListener('input', filterScope);
    $('quality-tabs').addEventListener('click', (ev) => {
      const tab = ev.target.closest('[data-quality-tab]');
      if (!tab) return;
      document.querySelectorAll('[data-quality-tab]').forEach((el) => el.classList.toggle('active', el === tab));
      document.querySelectorAll('[data-quality-panel]').forEach((el) => { el.hidden = el.dataset.qualityPanel !== tab.dataset.qualityTab; });
    });
    $('image-mode-tabs').addEventListener('click', (ev) => {
      const tab = ev.target.closest('[data-image-mode]');
      if (tab) setImageMode(tab.dataset.imageMode);
    });
    $('reference-upload').addEventListener('change', async (ev) => {
      await uploadReferenceFiles(ev.target.files || []);
      ev.target.value = '';
    });
    if ($('btn-gen-ai-background')) {
      $('btn-gen-ai-background').addEventListener('click', generateAiBackground);
    }
    if ($('btn-reload-backgrounds')) {
      $('btn-reload-backgrounds').addEventListener('click', reloadEffectBackgrounds);
    }
    if ($('btn-effect-preview')) {
      $('btn-effect-preview').addEventListener('click', previewEffectParams);
    }
    if ($('btn-effect-save')) {
      $('btn-effect-save').addEventListener('click', saveEffectParams);
    }
    if ($('btn-effect-reset')) {
      $('btn-effect-reset').addEventListener('click', resetEffectParams);
    }
    $('effect-background-upload').addEventListener('change', async (ev) => {
      await uploadEffectBackgroundFiles(ev.target.files || []);
      ev.target.value = '';
    });
    $('btn-quality-optimize').addEventListener('click', optimizeQualityTemplate);
    $('btn-quality-confirm-run').addEventListener('click', confirmPromptProfileAndRun);
    $('pq-template').addEventListener('input', renderQualityPreview);
    $('pq-optimized').addEventListener('input', renderQualityPreview);
    $('pq-use-optimized').addEventListener('change', renderQualityPreview);
    $('pq-realism').addEventListener('change', renderQualityPreview);
    $('s-size').addEventListener('change', renderQualityPreview);
    $('btn-validate-path').addEventListener('click', validatePath);
    $('btn-open-dir').addEventListener('click', openDir);
    $('btn-export-titles').addEventListener('click', () => exportFile('titles'));
    $('btn-export-checklist').addEventListener('click', () => exportFile('checklist'));
    $('btn-export-package').addEventListener('click', () => exportFile('package'));
    $('btn-close-history').addEventListener('click', closeHistory);
    $('compare-grid').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.btn-history-restore');
      if (btn) restoreHistory(btn);
    });
    $('btn-refresh-pending').addEventListener('click', loadPending);
    $('btn-retry-failures').addEventListener('click', retryFailures);
    $('btn-refresh-runs').addEventListener('click', loadRuns);
    $('btn-local-validation-run').addEventListener('click', runLocalValidation);
    $('btn-local-validation-refresh').addEventListener('click', loadLocalValidation);
    $('btn-professional-run').addEventListener('click', runProfessionalValidation);
    $('btn-professional-refresh').addEventListener('click', loadProfessionalValidation);
    $('btn-deepseek-eye').addEventListener('click', () => {
      const input = $('s-deepseek-key');
      input.type = input.type === 'password' ? 'text' : 'password';
    });
    $('btn-deepseek-test').addEventListener('click', testDeepSeekConnection);
    $('history-modal').addEventListener('click', (ev) => {
      if (ev.target === $('history-modal')) closeHistory();
    });
    $('settings-modal').addEventListener('click', (ev) => {
      if (ev.target === $('settings-modal')) closeSettings();
    });
    document.addEventListener('change', (ev) => {
      if (ev.target.name === 'active-provider') updateConcurrencyNote();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if ($('settings-modal').classList.contains('show')) closeSettings();
        if ($('history-modal').classList.contains('show')) closeHistory();
        $('lightbox').classList.remove('show');
      }
    });

    // 图片放大 / 单张重新生成
    $('stores').addEventListener('click', (ev) => {
      const houseBtn = ev.target.closest('.btn-house-regenerate');
      if (houseBtn) {
        ev.stopPropagation();
        regenerateOpenAIHouse(houseBtn);
        return;
      }
      const regenBtn = ev.target.closest('.btn-regen');
      if (regenBtn) {
        ev.stopPropagation();
        regenerate(regenBtn.dataset.storeIdx, regenBtn.dataset.pic, regenBtn);
        return;
      }
      const histBtn = ev.target.closest('.btn-history');
      if (histBtn) {
        ev.stopPropagation();
        openHistory(histBtn.dataset.store, histBtn.dataset.file, histBtn.dataset.label);
        return;
      }
      const th = ev.target.closest('.thumb');
      if (!th) return;
      const img = th.querySelector('img');
      if (!img) return;
      $('lightbox-img').src = img.src;
      $('lightbox-cap').textContent = th.title;
      $('lightbox').classList.add('show');
    });
    $('lightbox').addEventListener('click', () => $('lightbox').classList.remove('show'));

    loadState().then(() => {
      connectSSE();
      fetch('/api/logs').then((r) => r.json()).then((d) => {
        (d.lines || []).forEach((l) => logLine(l));
      }).catch(() => {});
    }).catch((e) => {
      logLine('✗ 加载状态失败：' + e.message, 'err');
      toast('加载状态失败：' + e.message, true);
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
