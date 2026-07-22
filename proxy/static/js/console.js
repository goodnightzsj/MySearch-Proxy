
// Grok 默认模型字面量散落在本文件 5 处（model 默认值 / quickstart env / settings 回填），
// 必须与 mysearch/config.py:_BUILTIN_GROK_MODELS、proxy/server.py SOCIAL_GATEWAY_MODEL 系列保持同步。
// 退役模型再次出现时优先改 _BUILTIN_GROK_MODELS（registry），proxy/server.py 已经派生；
// 本文件因仅作为客户端兜底显示，仍保留字面量但**必须与 registry 首/次项一致**。

// r5 P2-14: 高频文案抽到字典，便于后续接入 i18n 框架时一次性替换。
// 当前仅抽最高频的鉴权 / 加载 / 复制反馈，未引入 i18next 等 runtime。
const COPY = {
  loginBusy: '登录中...',
  loginEnter: '进入控制台',
  copySuccess: '已复制',
  copySuccessToast: '已复制到剪贴板',
  copyFailed: '复制失败',
};

// r5 P0-1: 全局 password reveal toggle（事件委托，6 个输入框共用）
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.input-reveal-btn');
  if (!btn) return;
  e.preventDefault();
  const targetId = btn.getAttribute('data-reveal-target');
  if (!targetId) return;
  const input = document.getElementById(targetId);
  if (!input) return;
  const currentlyHidden = input.type === 'password';
  input.type = currentlyHidden ? 'text' : 'password';
  btn.setAttribute('aria-pressed', String(currentlyHidden));
  btn.setAttribute('aria-label', currentlyHidden ? '隐藏密码' : '显示密码');
  const iconSpan = btn.querySelector('.reveal-icon');
  if (iconSpan) iconSpan.textContent = currentlyHidden ? '🙈' : '👁';
});

function showToast(message, type = 'info') {
  const root = document.getElementById('toast-root');
  if (!root) return;
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  // r7 U4: 外层 #toast-root 容器已 role=status + aria-live=polite，子 toast
  // 不再重复声明，避免 AT 把同一条消息读两次。
  toast.textContent = message;
  root.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('toast-fade-out');
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}
const STORAGE_KEY = 'multi_service_proxy_pwd';
const LEGACY_STORAGE_KEY = 'tavily_proxy_pwd';
const ACTIVE_SERVICE_KEY = 'multi_service_proxy_active_service';
const THEME_KEY = 'mysearch_proxy_console_theme';
const THEME_CYCLE = ['light', 'dark', 'auto'];
const AUTO_THEME_LIGHT_HOUR_START = 7;
const AUTO_THEME_DARK_HOUR_START = 19;
const API = '';
const PAGE_KIND = window.PAGE_KIND || 'console';
const BUTTON_MIN_BUSY_MS = 320;
const SERVICE_META = {
  tavily: {
    label: 'Tavily',
    emailPrefix: 'tavily-',
    tokenPrefix: 'tvly-',
    keyPlaceholder: 'tvly-xxxxxxxx',
    importPlaceholder: '支持粘贴 email,password,tvly-xxx,timestamp 或仅 tvly-xxx，每行一条',
    quotaSource: '真实额度来自 Tavily 官方 GET /usage',
    routeHint: '代理端点: POST /api/search, POST /api/extract',
    syncButton: '同步 Tavily 额度',
    syncSupported: true,
    panelIntro: '适合新闻、网页线索和基础搜索入口；现在既支持本地 API Key 池，也支持接上游 Tavily Gateway。',
    tokenPoolDesc: '给业务侧发放 Tavily 代理 Token，和 Exa / Firecrawl 完全分开创建、限流、统计。',
    keyPoolDesc: 'Tavily Key 独立存储，导入时只写入 Tavily 池，不会和 Exa 或 Firecrawl 混用。',
    switcherRoute: '/api/search · /api/extract',
    switcherBadges: ['网页发现', '官方同步'],
    switcherFoot: 'API Key 池 + Gateway 双模式',
    spotlightDesc: 'Tavily 继续负责第一层网页发现，这一栏保留现有功能与额度同步逻辑。',
  },
  exa: {
    label: 'Exa',
    emailPrefix: 'exa-',
    tokenPrefix: 'exat-',
    keyPlaceholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
    importPlaceholder: '支持粘贴 email,password,uuid,timestamp 或仅 UUID Key，每行一条',
    quotaSource: 'Exa 实时额度暂时无法查询，控制台当前只统计代理调用',
    routeHint: '代理端点: POST /exa/search',
    syncButton: 'Exa 暂不支持同步',
    syncSupported: false,
    panelIntro: '适合补充网页发现入口，已经独立成 Exa 工作台、Exa Key 池和 Exa Token 池。',
    tokenPoolDesc: '给业务侧发放 Exa 代理 Token，和 Tavily / Firecrawl 完全分开创建、限流、统计。',
    keyPoolDesc: 'Exa Key 独立存储，支持直接导入 UUID key，不和别的服务共用池子。',
    switcherRoute: '/exa/search',
    switcherBadges: ['网页发现', '代理统计'],
    switcherFoot: '独立搜索池',
    spotlightDesc: 'Exa 已经收成单独工作台，现在可以单独导入 Key、签发 Token，并通过 /exa/search 直接代理搜索。',
  },
  firecrawl: {
    label: 'Firecrawl',
    emailPrefix: 'fc-',
    tokenPrefix: 'fctk-',
    keyPlaceholder: 'fc-xxxxxxxx',
    importPlaceholder: '支持粘贴 email,password,fc-xxx,timestamp 或仅 fc-xxx，每行一条',
    quotaSource: '真实额度来自 Firecrawl /v2/team/credit-usage',
    routeHint: '代理端点: /firecrawl/*，例如 POST /firecrawl/v2/scrape',
    syncButton: '同步 Firecrawl 额度',
    syncSupported: true,
    panelIntro: '适合正文抓取、文档页、PDF 和结构化抽取，继续保持独立 Firecrawl 工作台。',
    tokenPoolDesc: '给业务侧发放 Firecrawl 代理 Token，和 Tavily / Exa 完全分开创建、限流、统计。',
    keyPoolDesc: 'Firecrawl Key 独立存储，导入时只写入 Firecrawl 池，不会和其他服务混用。',
    switcherRoute: '/firecrawl/*',
    switcherBadges: ['正文抓取', '官方同步'],
    switcherFoot: '抽取与 credits',
    spotlightDesc: 'Firecrawl 继续负责正文抓取与页面抽取，额度同步仍按 Firecrawl credits 展示。',
  },
};

const WORKSPACE_META = {
  ...SERVICE_META,
  social: {
    label: 'Social / X',
    emailPrefix: 'X search',
    tokenPrefix: 'shared auth',
    routeHint: '代理端点: POST /social/search',
    quotaSource: 'grok2api / xAI-compatible social router',
    switcherRoute: '/social/search',
    switcherBadges: ['X Search', 'g2a_ client key'],
    switcherFoot: '兼容路由 + 统一输出',
    spotlightDesc: 'Social / X 工作台负责舆情路由和 token 池映射，对外统一暴露 /social/search。',
  },
};

let PWD = localStorage.getItem(STORAGE_KEY) || localStorage.getItem(LEGACY_STORAGE_KEY) || '';
let activeService = localStorage.getItem(ACTIVE_SERVICE_KEY) || 'tavily';
let latestServices = {};
let latestSocial = {};
let latestMySearch = {};
let latestSettings = {};
let latestStatsMeta = {};
let activeTheme = localStorage.getItem(THEME_KEY) || 'light';
let effectiveTheme = 'light';
let appDialogResolver = null;
let autoThemeIntervalId = 0;
const tableControls = { tokens: {}, keys: {} };
const KEY_PAGE_SIZE = 20;
const MOBILE_KEY_PAGE_SIZE = 5;
let activeQuickstartTab = 'config';
const overlayFocusMemory = {};
const OVERLAY_PRIORITY = ['app-dialog', 'detail-drawer', 'settings-modal'];

function isShellVisible(id) {
  const element = document.getElementById(id);
  return Boolean(element && !element.classList.contains('hidden'));
}

function syncOverlayState() {
  const topOverlayId = getTopOpenOverlayId();
  const overlayOpen = Boolean(topOverlayId);
  document.body.classList.toggle('modal-open', overlayOpen);
  const dashboard = document.getElementById('dashboard');
  if (dashboard && 'inert' in dashboard) {
    dashboard.inert = overlayOpen;
  }
  OVERLAY_PRIORITY.forEach((id) => {
    const shell = document.getElementById(id);
    if (!shell) return;
    const isOpen = isShellVisible(id);
    shell.setAttribute('aria-hidden', isOpen && id === topOverlayId ? 'false' : 'true');
    if ('inert' in shell) {
      shell.inert = isOpen && id !== topOverlayId;
    }
  });
}

function getFocusableElements(root) {
  if (!root) return [];
  return Array.from(root.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((item) => !item.classList.contains('hidden') && item.offsetParent !== null);
}

function rememberOverlayFocus(id) {
  if (document.activeElement instanceof HTMLElement) {
    overlayFocusMemory[id] = document.activeElement;
  }
}

function restoreOverlayFocus(id) {
  const target = overlayFocusMemory[id];
  delete overlayFocusMemory[id];
  if (target && target.isConnected) {
    target.focus({ preventScroll: true });
  }
}

function focusOverlay(id) {
  const shell = document.getElementById(id);
  if (!shell) return;
  requestAnimationFrame(() => {
    const candidates = getFocusableElements(shell);
    const target = candidates.find((item) => item.hasAttribute('data-overlay-autofocus')) || candidates[0];
    target?.focus({ preventScroll: true });
  });
}

function getTopOpenOverlayId() {
  return OVERLAY_PRIORITY.find((id) => isShellVisible(id)) || '';
}

function trapOverlayFocus(event) {
  if (event.key !== 'Tab') return false;
  const overlayId = getTopOpenOverlayId();
  if (!overlayId) return false;
  const shell = document.getElementById(overlayId);
  const focusable = getFocusableElements(shell);
  if (!focusable.length) return false;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey) {
    if (active === first || !shell.contains(active)) {
      event.preventDefault();
      last.focus({ preventScroll: true });
      return true;
    }
    return false;
  }
  if (active === last || !shell.contains(active)) {
    event.preventDefault();
    first.focus({ preventScroll: true });
    return true;
  }
  return false;
}

function handleSegmentedControlKey(event) {
  const trigger = event.target.closest('.mini-switch-btn, .mode-switch-btn, .settings-tab, .workspace-tab');
  if (!trigger) return false;
  const isPrev = ['ArrowLeft', 'ArrowUp'].includes(event.key);
  const isNext = ['ArrowRight', 'ArrowDown'].includes(event.key);
  const isHome = event.key === 'Home';
  const isEnd = event.key === 'End';
  if (!isPrev && !isNext && !isHome && !isEnd) return false;

  const container = trigger.closest('.mini-switch, .mode-switch, .settings-tabs, .workspace-tabs');
  if (!container) return false;
  const selector = trigger.classList.contains('workspace-tab')
    ? '.workspace-tab'
    : trigger.classList.contains('settings-tab')
    ? '.settings-tab'
    : trigger.classList.contains('mode-switch-btn')
      ? '.mode-switch-btn'
      : '.mini-switch-btn';
  const buttons = Array.from(container.querySelectorAll(selector)).filter((item) => !item.disabled);
  if (!buttons.length) return false;

  const currentIndex = Math.max(0, buttons.indexOf(trigger));
  let targetIndex = currentIndex;
  if (isHome) {
    targetIndex = 0;
  } else if (isEnd) {
    targetIndex = buttons.length - 1;
  } else if (isPrev) {
    targetIndex = (currentIndex - 1 + buttons.length) % buttons.length;
  } else if (isNext) {
    targetIndex = (currentIndex + 1) % buttons.length;
  }

  const next = buttons[targetIndex];
  if (!next) return false;
  event.preventDefault();
  next.focus({ preventScroll: true });
  if (next !== trigger) {
    next.click();
  }
  return true;
}

function getServiceDisplayLabel(service) {
  if (service === 'mysearch') return 'MySearch';
  return WORKSPACE_META[service]?.label || SERVICE_META[service]?.label || service;
}

function getServicePayload(service) {
  if (service === 'mysearch') return latestMySearch || {};
  if (service === 'social') return latestSocial || {};
  return latestServices[service] || {};
}

function getTokenTableState(service) {
  if (!tableControls.tokens[service]) {
    tableControls.tokens[service] = { search: '', sort: 'risk' };
  }
  return tableControls.tokens[service];
}

function getKeyTableState(service) {
  if (!tableControls.keys[service]) {
    tableControls.keys[service] = { search: '', filter: 'all', sort: 'risk', page: 1 };
  }
  return tableControls.keys[service];
}

function getKeyPageSize() {
  return window.matchMedia('(max-width: 720px)').matches ? MOBILE_KEY_PAGE_SIZE : KEY_PAGE_SIZE;
}

function parseTimeValue(value) {
  if (!value) return 0;
  const stamp = Date.parse(value);
  return Number.isFinite(stamp) ? stamp : 0;
}

function getTokenActivity(token) {
  const stats = token?.stats || {};
  return Number(stats.hour_count || 0) * 1000000
    + (Number(stats.today_success || 0) + Number(stats.today_failed || 0)) * 1000
    + Number(stats.month_success || 0)
    + Number(stats.month_failed || 0);
}

function getKeyRemaining(key) {
  if (key.usage_key_remaining !== null && key.usage_key_remaining !== undefined) {
    return Number(key.usage_key_remaining || 0);
  }
  if (key.usage_account_remaining !== null && key.usage_account_remaining !== undefined) {
    return Number(key.usage_account_remaining || 0);
  }
  return Number.POSITIVE_INFINITY;
}

function getTokenRiskScore(token) {
  const stats = token?.stats || {};
  const failed = Number(stats.today_failed || 0);
  const success = Number(stats.today_success || 0);
  const today = failed + success;
  const hour = Number(stats.hour_count || 0);
  let score = failed * 1000;
  if (failed > 0 && failed >= Math.max(success, 1)) {
    score += 300000;
  } else if (failed > 0) {
    score += 120000;
  }
  if (today >= 120 || hour >= 24) {
    score += 40000;
  }
  score += hour * 100 + today;
  return score;
}

function getKeyRiskScore(service, key) {
  let score = 0;
  const remaining = getKeyRemaining(key);
  const failed = Number(key.total_failed || 0);
  const used = Number(key.total_used || 0);
  if (String(key.usage_sync_error || '').trim()) {
    score += 500000;
  }
  if (!getKeyAvailability(key).schedulable) {
    score += 300000;
  }
  if (Number.isFinite(remaining)) {
    score += Math.max(0, 200000 - Math.min(200000, remaining));
  }
  if (failed > used && failed > 0) {
    score += 80000;
  }
  if (service === 'exa' && used >= 24) {
    score += 30000;
  }
  score += failed * 1000;
  score += Math.max(0, 200 - Math.min(200, used));
  return score;
}

function hasKeyIssue(service, key) {
  return Boolean(getKeyRowClass(service, key));
}

function getTokenRowClass(token) {
  const stats = token?.stats || {};
  const failed = Number(stats.today_failed || 0);
  const success = Number(stats.today_success || 0);
  const today = failed + success;
  const hour = Number(stats.hour_count || 0);
  if (failed > 0 && failed >= Math.max(success, 1)) {
    return 'is-danger';
  }
  if (today >= 120 || hour >= 24) {
    return 'is-busy';
  }
  if (failed > 0) {
    return 'is-warn';
  }
  return '';
}

function getKeyRowClass(service, key) {
  if (String(key.usage_sync_error || '').trim()) {
    return 'is-danger';
  }
  const availability = getKeyAvailability(key);
  if (!availability.active) {
    return 'is-off';
  }
  if (!availability.schedulable) {
    return 'is-warn';
  }
  const remaining = getKeyRemaining(key);
  if (Number.isFinite(remaining) && remaining <= 100) {
    return 'is-warn';
  }
  if (Number(key.total_failed || 0) > Number(key.total_used || 0) && Number(key.total_failed || 0) > 0) {
    return 'is-warn';
  }
  if (service === 'exa' && Number(key.total_used || 0) >= 24) {
    return 'is-busy';
  }
  return '';
}

function getKeyAvailability(key) {
  const active = Number(key?.active) === 1;
  const reason = String(key?.disabled_reason || '').trim();
  const scheduleUntil = String(key?.schedule_until || '').trim();
  const untilMs = scheduleUntil ? Date.parse(scheduleUntil) : Number.NaN;
  const coolingDown = active && Number.isFinite(untilMs) && untilMs > Date.now();
  if (coolingDown) {
    return {
      active,
      schedulable: false,
      reason: reason || 'rate_limited',
      label: '限流冷却',
      detail: `恢复 ${formatTime(scheduleUntil)}`,
      actionLabel: '立即恢复',
    };
  }
  if (active) {
    return {
      active,
      schedulable: true,
      reason: '',
      label: '正常',
      detail: '可参与调度',
      actionLabel: '禁用',
    };
  }
  const labels = {
    quota_exhausted: '额度耗尽',
    auth_rejected: '凭证失效',
    manual: '手动禁用',
    repeated_failure: '连续失败',
  };
  return {
    active,
    schedulable: false,
    reason,
    label: labels[reason] || '禁用',
    detail: key?.disabled_at ? `隔离 ${formatTime(key.disabled_at)}` : '不参与调度',
    actionLabel: '启用',
  };
}

function getFilteredTokens(service, tokens) {
  const state = getTokenTableState(service);
  const keyword = (state.search || '').trim().toLowerCase();
  let items = [...(tokens || [])];
  if (keyword) {
    items = items.filter((token) => {
      const haystack = [
        token.id,
        token.name,
        token.token,
        token.created_at,
      ].map((value) => String(value || '').toLowerCase()).join(' ');
      return haystack.includes(keyword);
    });
  }

  items.sort((left, right) => {
    if (state.sort === 'risk') {
      const riskDelta = getTokenRiskScore(right) - getTokenRiskScore(left);
      if (riskDelta !== 0) return riskDelta;
      const todayDelta = (Number(right?.stats?.today_failed || 0) + Number(right?.stats?.today_success || 0))
        - (Number(left?.stats?.today_failed || 0) + Number(left?.stats?.today_success || 0));
      if (todayDelta !== 0) return todayDelta;
      return parseTimeValue(right.created_at) - parseTimeValue(left.created_at);
    }
    if (state.sort === 'name') {
      return String(left.name || left.token || '').localeCompare(String(right.name || right.token || ''), 'zh-CN');
    }
    if (state.sort === 'today') {
      const leftToday = Number(left?.stats?.today_success || 0) + Number(left?.stats?.today_failed || 0);
      const rightToday = Number(right?.stats?.today_success || 0) + Number(right?.stats?.today_failed || 0);
      if (rightToday !== leftToday) return rightToday - leftToday;
      return parseTimeValue(right.created_at) - parseTimeValue(left.created_at);
    }
    const delta = getTokenActivity(right) - getTokenActivity(left);
    if (delta !== 0) return delta;
    return parseTimeValue(right.created_at) - parseTimeValue(left.created_at);
  });
  return items;
}

function getFilteredKeys(service, keys) {
  const state = getKeyTableState(service);
  const keyword = (state.search || '').trim().toLowerCase();
  let items = [...(keys || [])];

  if (keyword) {
    items = items.filter((key) => {
      const haystack = [
        key.id,
        key.key,
        key.key_masked,
        key.email,
        key.last_used_at,
      ].map((value) => String(value || '').toLowerCase()).join(' ');
      return haystack.includes(keyword);
    });
  }

  if (state.filter === 'active') {
    items = items.filter((key) => getKeyAvailability(key).schedulable);
  } else if (state.filter === 'disabled') {
    items = items.filter((key) => Number(key.active) !== 1);
  } else if (state.filter === 'error') {
    items = items.filter((key) => Boolean((key.usage_sync_error || '').trim()));
  } else if (state.filter === 'issue') {
    items = items.filter((key) => hasKeyIssue(service, key));
  }

  items.sort((left, right) => {
    if (state.sort === 'risk') {
      const riskDelta = getKeyRiskScore(service, right) - getKeyRiskScore(service, left);
      if (riskDelta !== 0) return riskDelta;
      return parseTimeValue(right.last_used_at) - parseTimeValue(left.last_used_at);
    }
    if (state.sort === 'usage') {
      const usageDelta = Number(right.total_used || 0) - Number(left.total_used || 0);
      if (usageDelta !== 0) return usageDelta;
      return parseTimeValue(right.last_used_at) - parseTimeValue(left.last_used_at);
    }
    if (state.sort === 'quota') {
      const quotaDelta = getKeyRemaining(left) - getKeyRemaining(right);
      if (quotaDelta !== 0) return quotaDelta;
      return parseTimeValue(right.last_used_at) - parseTimeValue(left.last_used_at);
    }
    return parseTimeValue(right.last_used_at) - parseTimeValue(left.last_used_at);
  });
  return items;
}

function handleTableRowKey(event, kind, service, id) {
  if (!['Enter', ' '].includes(event.key)) return;
  event.preventDefault();
  if (kind === 'token') {
    openTokenDetail(service, id);
    return;
  }
  openKeyDetail(service, id);
}

function closeAppDialog(result = false) {
  const shell = document.getElementById('app-dialog');
  if (!shell) return;
  shell.classList.add('hidden');
  syncOverlayState();
  restoreOverlayFocus('app-dialog');
  if (appDialogResolver) {
    const resolve = appDialogResolver;
    appDialogResolver = null;
    resolve(result);
  }
}

function showConfirmDialog({
  title = '请确认操作',
  message = '确认后会继续执行当前操作。',
  confirmText = '确认',
  cancelText = '取消',
  tone = 'info',
  kicker = 'Action Required',
} = {}) {
  const shell = document.getElementById('app-dialog');
  if (!shell) return Promise.resolve(false);
  rememberOverlayFocus('app-dialog');
  document.getElementById('app-dialog-kicker').textContent = kicker;
  document.getElementById('app-dialog-title').textContent = title;
  document.getElementById('app-dialog-message').textContent = message;
  shell.dataset.tone = tone;
  document.getElementById('app-dialog-actions').innerHTML = `
    ${cancelText ? `<button class="btn btn-soft" type="button" onclick="closeAppDialog(false)">${escapeHtml(cancelText)}</button>` : ''}
    <button class="btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}" type="button" data-overlay-autofocus="true" onclick="closeAppDialog(true)">${escapeHtml(confirmText)}</button>
  `;
  shell.classList.remove('hidden');
  syncOverlayState();
  focusOverlay('app-dialog');
  return new Promise((resolve) => {
    appDialogResolver = resolve;
  });
}

function showAlertDialog({
  title = '提示',
  message = '请查看当前状态。',
  confirmText = '知道了',
  tone = 'info',
  kicker = 'Notice',
} = {}) {
  return showConfirmDialog({
    title,
    message,
    confirmText,
    cancelText: '',
    tone,
    kicker,
  });
}

function openDetailDrawer({
  kicker = 'Detail',
  title = '查看详情',
  subtitle = '',
  tone = 'info',
  summaryHtml = '',
  bodyHtml = '',
  actionsHtml = '',
} = {}) {
  const shell = document.getElementById('detail-drawer');
  if (!shell) return;
  rememberOverlayFocus('detail-drawer');
  shell.dataset.tone = tone;
  document.getElementById('detail-drawer-kicker').textContent = kicker;
  document.getElementById('detail-drawer-title').textContent = title;
  document.getElementById('detail-drawer-subtitle').textContent = subtitle;
  document.getElementById('detail-drawer-summary').innerHTML = summaryHtml;
  document.getElementById('detail-drawer-body').innerHTML = bodyHtml;
  document.getElementById('detail-drawer-actions').innerHTML = actionsHtml;
  shell.classList.remove('hidden');
  syncOverlayState();
  // r7 U3: 焦点落到标题（programmatic focus）而非关闭按钮，避免 Tab 立即跳到末尾。
  // 标题节点加 tabindex=-1 才能 .focus()，但不出现在 Tab 序列里。
  const titleEl = document.getElementById('detail-drawer-title');
  if (titleEl) {
    titleEl.setAttribute('tabindex', '-1');
    titleEl.focus();
  } else {
    focusOverlay('detail-drawer');
  }
}

function closeDetailDrawer() {
  const shell = document.getElementById('detail-drawer');
  if (!shell) return;
  shell.classList.add('hidden');
  document.getElementById('detail-drawer-summary').innerHTML = '';
  document.getElementById('detail-drawer-body').innerHTML = '';
  document.getElementById('detail-drawer-actions').innerHTML = '';
  syncOverlayState();
  restoreOverlayFocus('detail-drawer');
}

function summaryCard(label, value, hint = '', options = {}) {
  const valueClass = options.valueClass ? ` ${options.valueClass}` : '';
  const hintClass = options.hintClass ? ` ${options.hintClass}` : '';
  return `
    <div class="settings-summary-card">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value${valueClass}">${escapeHtml(value)}</div>
      <div class="hint${hintClass}">${escapeHtml(hint)}</div>
    </div>
  `;
}

function drawerMetric(label, value, hint = '') {
  return `
    <div class="drawer-metric">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
      <div class="hint">${escapeHtml(hint)}</div>
    </div>
  `;
}

function drawerSection(title, body) {
  return `
    <section class="drawer-section">
      <h4>${escapeHtml(title)}</h4>
      <div class="drawer-section-body">${body}</div>
    </section>
  `;
}

function socialAdminVersionLabel(version) {
  const normalized = String(version || '?').trim().replace(/^v/i, '');
  return `v${normalized || '?'}`;
}

function renderSettingsSummaries(settings = latestSettings) {
  const tavily = settings?.tavily || {};
  const social = settings?.social || {};
  const consoleSummary = document.getElementById('settings-console-summary');
  if (consoleSummary) {
    consoleSummary.innerHTML = [
      summaryCard('当前主题', getThemePreferenceLabel(), getThemeSummaryHint()),
      summaryCard('当前工作台', getServiceDisplayLabel(activeService), '保存设置后会回到这个工作台'),
      summaryCard('会话身份', 'admin', '当前控制面使用单管理员入口'),
    ].join('');
  }

  const tavilySummary = document.getElementById('settings-tavily-summary');
  if (tavilySummary) {
    tavilySummary.innerHTML = [
      summaryCard('配置模式', tavilyModeLabel(tavily.mode || 'auto'), tavilyModeSourceLabel(tavily.mode_source || 'auto_pending')),
      summaryCard('当前实际', tavilyModeLabel(tavily.effective_mode || tavily.mode || 'auto'), tavily.effective_mode === 'upstream' ? '当前请求直接转发到上游' : '当前请求从 API Key 池轮询'),
      summaryCard(
        '上游地址',
        tavily.upstream_base_url || '未配置',
        tavily.upstream_search_path || '/search',
        { valueClass: 'mono is-address', hintClass: 'mono is-address-hint' },
      ),
      summaryCard('凭证状态', tavily.upstream_api_key_configured ? '已配置' : '未配置', tavily.upstream_api_key_masked || `本地活跃 Key ${fmtNum(tavily.local_key_count || 0)}`),
    ].join('');
  }

  const socialSummary = document.getElementById('settings-social-summary');
  if (socialSummary) {
    const adminLabel = social.admin_connected
      ? `已连接 ${socialAdminVersionLabel(social.admin_api_version)}`
      : (social.admin_auth_mode === 'v3_credentials' ? 'v3 待验证' : '未连接');
    const mode = social.mode || 'local';
    socialSummary.innerHTML = [
      summaryCard('工作模式', socialModeLabel(mode), mode === 'local' ? '只使用本地 Social/X Key 池' : '只使用上游连接凭证'),
      summaryCard('Key 来源', mode === 'local' ? `${fmtNum(social.local_keys?.length || 0)} 个本地 Key` : socialTokenSourceLabel(social.token_source || ''), social.admin_connected ? '上游管理后台已连通' : '按当前模式独立运行'),
      summaryCard('默认模型', social.model || 'grok-4.20-0309', social.fallback_model ? `Fallback ${social.fallback_model}` : '未配置 fallback'),
      summaryCard('上游管理', mode === 'local' ? '未参与当前路由' : adminLabel, mode === 'local' ? '切换上游模式后才会读取' : (social.admin_username || '未配置管理员用户名')),
    ].join('');
  }
}

function getLocalThemeClock() {
  const now = new Date();
  let timeZone = '';
  try {
    timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (error) {
    timeZone = '';
  }
  return {
    hour: Number.isFinite(now.getHours()) ? now.getHours() : 12,
    timeZone: timeZone || 'browser-local',
  };
}

function resolveEffectiveTheme(theme = activeTheme) {
  if (theme === 'dark') return 'dark';
  if (theme === 'auto') {
    const { hour } = getLocalThemeClock();
    return hour >= AUTO_THEME_LIGHT_HOUR_START && hour < AUTO_THEME_DARK_HOUR_START ? 'light' : 'dark';
  }
  return 'light';
}

function getThemePreferenceLabel(theme = activeTheme) {
  if (theme === 'auto') return '自动模式';
  return theme === 'dark' ? '夜间模式' : '浅色模式';
}

function getThemeEffectiveLabel(theme = activeTheme) {
  return resolveEffectiveTheme(theme) === 'dark' ? '夜间模式' : '浅色模式';
}

function getThemeSummaryHint(theme = activeTheme) {
  if (theme === 'auto') {
    const { hour, timeZone } = getLocalThemeClock();
    return `按浏览器本地时间自动切换 · ${timeZone} ${String(hour).padStart(2, '0')}:00 当前${getThemeEffectiveLabel(theme)}`;
  }
  return '控制台会记住你的偏好';
}

function getNextTheme(theme = activeTheme) {
  const index = THEME_CYCLE.indexOf(theme);
  if (index < 0) return THEME_CYCLE[0];
  return THEME_CYCLE[(index + 1) % THEME_CYCLE.length];
}

function syncAutoThemeWatcher() {
  if (autoThemeIntervalId) {
    window.clearInterval(autoThemeIntervalId);
    autoThemeIntervalId = 0;
  }
  if (activeTheme === 'auto') {
    autoThemeIntervalId = window.setInterval(() => {
      refreshAutoThemeFromClock();
    }, 60_000);
  }
}

function applyTheme(theme, options = {}) {
  const normalized = THEME_CYCLE.includes(theme) ? theme : 'light';
  const persist = options.persist !== false;
  activeTheme = normalized;
  effectiveTheme = resolveEffectiveTheme(activeTheme);
  document.body.classList.toggle('theme-dark', effectiveTheme === 'dark');
  document.body.dataset.themePreference = activeTheme;
  document.body.dataset.themeEffective = effectiveTheme;
  if (persist) {
    localStorage.setItem(THEME_KEY, activeTheme);
  }
  syncAutoThemeWatcher();
  syncThemeToggle();
  renderSettingsSummaries();
}

function refreshAutoThemeFromClock(force = false) {
  if (activeTheme !== 'auto') return;
  const nextEffective = resolveEffectiveTheme('auto');
  if (!force && nextEffective === effectiveTheme) return;
  effectiveTheme = nextEffective;
  document.body.classList.toggle('theme-dark', effectiveTheme === 'dark');
  document.body.dataset.themePreference = activeTheme;
  document.body.dataset.themeEffective = effectiveTheme;
  syncThemeToggle();
  renderSettingsSummaries();
}

function syncThemeToggle() {
  const label = document.getElementById('theme-toggle-label');
  const button = document.getElementById('theme-toggle');
  if (label) {
    label.textContent = getThemePreferenceLabel();
  }
  if (button) {
    button.dataset.theme = activeTheme;
    button.dataset.effectiveTheme = effectiveTheme;
    const nextLabel = getThemePreferenceLabel(getNextTheme());
    button.title = activeTheme === 'auto'
      ? getThemeSummaryHint()
      : `点击切换到${nextLabel}`;
    button.setAttribute('aria-label', `当前${getThemePreferenceLabel()}，切换到${nextLabel}`);
  }
}

function toggleTheme() {
  applyTheme(getNextTheme());
}

function scrollToCurrentPanel() {
  if (PAGE_KIND === 'mysearch') {
    window.location.href = '/';
    return;
  }
  const panel = document.querySelector(`.service-panel[data-service="${activeService}"]`);
  if (panel) {
    const behavior = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
    panel.scrollIntoView({ behavior, block: 'start' });
  }
}

function scrollToQuickstart() {
  if (PAGE_KIND !== 'mysearch') {
    window.location.href = '/mysearch';
    return;
  }
  const shell = document.getElementById('mysearch-quickstart');
  if (shell) {
    const behavior = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
    shell.scrollIntoView({ behavior, block: 'start' });
  }
}

function openMySearchAccess() {
  scrollToQuickstart();
}

function clearStoredPasswords() {
  localStorage.removeItem(STORAGE_KEY);
  localStorage.removeItem(LEGACY_STORAGE_KEY);
}

function setLoginBusy(isBusy) {
  const input = document.getElementById('pwd-input');
  const button = document.getElementById('login-submit');
  if (input) input.disabled = isBusy;
  if (button) {
    button.disabled = isBusy;
    button.classList.toggle('is-busy', isBusy);
    if (isBusy) {
      button.classList.remove('is-success', 'is-error');
    }
    button.textContent = isBusy ? COPY.loginBusy : (PAGE_KIND === 'mysearch' ? '进入接入台' : COPY.loginEnter);
  }
}

function showDashboard(options = {}) {
  const animate = Boolean(options.animate);
  document.getElementById('login-err').classList.add('hidden');
  const loginBox = document.getElementById('login-box');
  const dashboard = document.getElementById('dashboard');
  loginBox.classList.add('hidden');
  dashboard.classList.remove('hidden');
  dashboard.classList.remove('is-entering');
  if (animate) {
    dashboard.classList.add('is-entering');
    setTimeout(() => {
      dashboard.classList.remove('is-entering');
    }, 760);
  }
  renderSettingsSummaries();
}

function showLogin() {
  const dashboard = document.getElementById('dashboard');
  const loginBox = document.getElementById('login-box');
  dashboard.classList.remove('is-entering');
  dashboard.classList.add('hidden');
  loginBox.classList.remove('hidden');
  closeSettingsModal();
  closeDetailDrawer();
  closeAppDialog(false);
  setLoginBusy(false);
}

async function fetchSession(method, path, body) {
  const options = {
    method,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
    },
  };
  if (body !== undefined) {
    options.body = JSON.stringify(body);
  }
  const response = await fetch(API + path, options);
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = text ? { detail: text } : {};
  }
  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return payload;
}

async function loginWithPassword(password) {
  await fetchSession('POST', '/api/session/login', { password });
}

async function hasServerSession() {
  try {
    await fetchSession('GET', '/api/session');
    return true;
  } catch {
    return false;
  }
}

async function migrateStoredPasswordIfNeeded() {
  if (!PWD) return false;
  try {
    await loginWithPassword(PWD);
    PWD = '';
    clearStoredPasswords();
    return true;
  } catch {
    PWD = '';
    clearStoredPasswords();
    return false;
  }
}

function socialModeLabel(mode) {
  return mode === 'upstream' ? '上游模式' : '本地模式';
}

function tavilyModeLabel(mode) {
  if (mode === 'upstream') return '上游 Gateway';
  if (mode === 'pool') return 'API Key 池';
  return '自动识别';
}

function tavilyModeSourceLabel(source) {
  if (source === 'manual_upstream') return '手动固定上游';
  if (source === 'manual_pool') return '手动固定本地池';
  if (source === 'auto_upstream') return '自动识别到上游凭证';
  if (source === 'auto_pool') return '自动识别到本地可用 Key';
  return '等待识别';
}

function socialTokenSourceLabel(source) {
  if (source === 'grok2api app.api_key') return '后台自动继承';
  if (source === 'SOCIAL_GATEWAY_LOCAL_API_KEY') return '本地 Social/X Key 池';
  if (source === 'SOCIAL_GATEWAY_UPSTREAM_API_KEY') return '手动上游 API key';
  if (source === 'manual SOCIAL_GATEWAY_TOKEN') return '手动客户端 token';
  return '尚未配置';
}

function socialStatusLabel(social) {
  const state = getSocialUpstreamState(social || {});
  if (social?.mode === 'local' && state.canProxySearch) return '本地模式已就绪';
  if (state.canProxySearch && state.adminConnected) return '推理与管理均已接通';
  if (state.canProxySearch) return '已可转发搜索';
  if (state.adminConnected) return '管理已接通，推理待配置';
  return '等待配置';
}

function getTavilyRuntimeState(payload) {
  const routing = payload?.routing || {};
  const settings = latestSettings?.tavily || {};
  return {
    configuredMode: routing.mode || settings.mode || 'auto',
    effectiveMode: routing.effective_mode || settings.effective_mode || settings.mode || 'auto',
    modeSource: routing.mode_source || settings.mode_source || 'auto_pending',
    upstreamConfigured: Boolean(
      (routing.upstream_api_key_configured ?? settings.upstream_api_key_configured) || false,
    ),
    localKeyCount: Number(
      routing.local_key_count
      ?? settings.local_key_count
      ?? payload?.keys_active
      ?? 0,
    ),
  };
}

function getTavilyUpstreamSummary(payload) {
  const summary = payload?.upstream_summary || {};
  const activeKeys = Number(summary.active_keys || 0);
  const exhaustedKeys = Number(summary.exhausted_keys || 0);
  const quarantinedKeys = Number(summary.quarantined_keys || 0);
  const totalKeys = Number(summary.total_keys || (activeKeys + exhaustedKeys + quarantinedKeys));
  return {
    available: Boolean(summary.available),
    detail: summary.detail || '',
    sourceLabel: summary.source_label || '',
    capabilityLevel: summary.capability_level || 'unavailable',
    keyDetailSupported: Boolean(summary.key_detail_supported),
    requestTarget: summary.request_target || '',
    adminConfigured: Boolean(summary.admin_configured),
    adminConnected: Boolean(summary.admin_connected),
    adminDetail: summary.admin_detail || '',
    adminRequestTarget: summary.admin_request_target || '',
    activeKeys,
    exhaustedKeys,
    quarantinedKeys,
    totalKeys,
    totalRequests: Number(summary.total_requests || 0),
    successCount: Number(summary.success_count || 0),
    errorCount: Number(summary.error_count || 0),
    quotaExhaustedCount: Number(summary.quota_exhausted_count || 0),
    totalQuotaLimit: Number(summary.total_quota_limit || 0),
    totalQuotaRemaining: Number(summary.total_quota_remaining || 0),
    lastActivity: summary.last_activity || null,
  };
}

function getSocialUpstreamState(social) {
  const visibility = social?.upstream_visibility || {};
  const upstreamApiKeyCount = Number(
    visibility.upstream_api_key_count
    ?? social?.upstream_api_key_count
    ?? 0,
  );
  const acceptedTokenCount = Number(
    visibility.accepted_token_count
    ?? social?.accepted_token_count
    ?? 0,
  );
  const upstreamAvailableKeyCount = Number(
    visibility.upstream_available_key_count
    ?? social?.upstream_available_key_count
    ?? upstreamApiKeyCount,
  );
  const upstreamUnavailableKeyCount = Number(
    visibility.upstream_unavailable_key_count
    ?? social?.upstream_unavailable_key_count
    ?? Math.max(0, upstreamApiKeyCount - upstreamAvailableKeyCount),
  );
  const level = visibility.level
    || (social?.admin_connected
      ? 'full'
      : ((upstreamApiKeyCount > 0 || acceptedTokenCount > 0)
        ? 'basic'
        : 'none'));
  return {
    level,
    detail: visibility.detail || '',
    canProxySearch: Boolean(
      visibility.can_proxy_search
      ?? (social?.upstream_key_configured && social?.client_auth_configured),
    ),
    upstreamApiKeyCount,
    upstreamAvailableKeyCount,
    upstreamUnavailableKeyCount,
    upstreamUnavailableReasons: visibility.upstream_unavailable_reasons
      ?? social?.upstream_unavailable_reasons
      ?? [],
    acceptedTokenCount,
    adminConnected: Boolean(visibility.admin_connected ?? social?.admin_connected),
    tokenSource: visibility.token_source || social?.token_source || 'not_configured',
  };
}

function isSocialUpstreamManaged(social) {
  const state = getSocialUpstreamState(social || {});
  return Boolean(
    state.adminConnected
    || state.canProxySearch
    || social?.upstream_key_configured
    || (String(social?.upstream_base_url || '').trim() && state.upstreamApiKeyCount > 0),
  );
}

function getBlankServicePayload() {
  return {
    tokens: [],
    keys: [],
    overview: {},
    real_quota: {},
    usage_sync: {},
    keys_total: 0,
    keys_active: 0,
  };
}

function normalizeRefreshScope(scope) {
  if (!scope) {
    return {
      core: true,
      mysearch: true,
      social: true,
      services: Object.keys(SERVICE_META),
    };
  }
  const services = new Set(Array.isArray(scope.services) ? scope.services : []);
  if (scope.service) {
    services.add(scope.service);
  }
  return {
    core: scope.core !== false,
    mysearch: Boolean(scope.mysearch),
    social: Boolean(scope.social),
    services: [...services].filter((service) => SERVICE_META[service]),
  };
}

function getRefreshScopeForService(service, options = {}) {
  const scope = {
    core: options.core !== false,
    mysearch: options.mysearch !== false,
    social: false,
    services: [],
  };
  if (service === 'mysearch') {
    scope.mysearch = true;
    return scope;
  }
  if (service === 'social') {
    scope.social = true;
    return scope;
  }
  if (SERVICE_META[service]) {
    scope.services.push(service);
  }
  return scope;
}

function getQuickstartProviderCards(services = latestServices, social = latestSocial) {
  const tavilyPayload = services?.tavily || {};
  const tavilyState = getTavilyRuntimeState(tavilyPayload);
  const tavilyUpstream = getTavilyUpstreamSummary(tavilyPayload);
  const tavilyKeysActive = Number(tavilyPayload.keys_active || 0);
  const tavilyKeysTotal = Number(tavilyPayload.keys_total || 0);
  const socialState = getSocialUpstreamState(social || {});
  const cards = [];

  if (tavilyState.effectiveMode === 'upstream') {
    cards.push({
      label: 'Tavily',
      tone: tavilyState.upstreamConfigured ? 'ok' : 'danger',
      title: tavilyState.upstreamConfigured ? '上游 Gateway' : '待配置上游',
      desc: tavilyState.upstreamConfigured
        ? (tavilyUpstream.available
          ? `上游活跃 ${fmtNum(tavilyUpstream.activeKeys)} / 总 ${fmtNum(tavilyUpstream.totalKeys)} · 剩余 ${fmtNum(tavilyUpstream.totalQuotaRemaining)}${tavilyUpstream.keyDetailSupported ? ' · 已接通 admin 明细' : ' · 当前为公共摘要'}`
          : `${tavilyModeSourceLabel(tavilyState.modeSource)} · 当前直接转发 /api/search，也可回退本地池`)
        : '当前已切上游模式，但还没有可用的上游凭证',
    });
  } else if (tavilyKeysActive > 0) {
    cards.push({
      label: 'Tavily',
      tone: 'ok',
      title: `API Key 池 · ${fmtNum(tavilyKeysActive)} Key`,
      desc: tavilyKeysTotal > tavilyKeysActive
        ? `活跃 ${fmtNum(tavilyKeysActive)} / 总数 ${fmtNum(tavilyKeysTotal)}`
        : `${tavilyModeSourceLabel(tavilyState.modeSource)} · 默认从本地池轮询，也可切上游 Gateway`,
    });
  } else if (tavilyKeysTotal > 0) {
    cards.push({
      label: 'Tavily',
      tone: 'warn',
      title: 'Key 全部停用',
      desc: `已导入 ${fmtNum(tavilyKeysTotal)} 个 Key，但当前没有活跃 Key；也可以直接改走上游 Gateway`,
    });
  } else {
    cards.push({
      label: 'Tavily',
      tone: 'danger',
      title: '待配置上游 / 待导入 Key',
      desc: 'Tavily 现在既可配置上游 Gateway，也可直接导入 API Key；auto 会优先识别上游',
    });
  }

  ['exa', 'firecrawl'].forEach((service) => {
    const payload = services?.[service] || {};
    const active = Number(payload.keys_active || 0);
    const total = Number(payload.keys_total || 0);
    const remaining = service === 'exa'
      ? null
      : Number(payload.real_quota?.total_remaining ?? Number.NaN);
    if (total <= 0) {
      cards.push({
        label: getServiceDisplayLabel(service),
        tone: 'danger',
        title: '待导入 Key',
        desc: service === 'exa'
          ? '导入后即可启用独立网页发现路由'
          : '导入后即可启用正文抓取与抽取链路',
      });
      return;
    }
    if (active <= 0) {
      cards.push({
        label: getServiceDisplayLabel(service),
        tone: 'warn',
        title: 'Key 全部停用',
        desc: `已导入 ${fmtNum(total)} 个 Key，但当前没有活跃 Key`,
      });
      return;
    }
    if (Number.isFinite(remaining) && remaining <= 100) {
      cards.push({
        label: getServiceDisplayLabel(service),
        tone: 'warn',
        title: `额度偏低 · ${fmtNum(remaining)}`,
        desc: `活跃 ${fmtNum(active)} / 总数 ${fmtNum(total)}`,
      });
      return;
    }
    cards.push({
      label: getServiceDisplayLabel(service),
      tone: 'ok',
      title: service === 'exa' ? '独立搜索池' : '抽取线路就绪',
      desc: `活跃 ${fmtNum(active)} / 总数 ${fmtNum(total)}`,
    });
  });

  if (socialState.canProxySearch && (social?.mode === 'local' || social?.admin_connected)) {
    cards.push({
      label: 'Social / X',
      tone: 'ok',
      title: social?.mode === 'local'
        ? '本地 Key 池已就绪'
        : (social?.admin_api_version === 'v3' ? 'v3 管理统计已连接' : 'v2 上游已连接'),
      desc: `${socialTokenSourceLabel(social?.token_source || '')} · /social/search 已就绪`,
    });
  } else if (socialState.canProxySearch) {
    cards.push({
      label: 'Social / X',
      tone: 'warn',
      title: '已可转发搜索',
      desc: `上游 key ${fmtNum(socialState.upstreamApiKeyCount)} · 客户端 token ${fmtNum(socialState.acceptedTokenCount)} · 后台统计未接通`,
    });
  } else if (social?.admin_connected) {
    cards.push({
      label: 'Social / X',
      tone: 'danger',
      title: '管理已连接，推理待配置',
      desc: '补充 grok2api v3 g2a_ client key 后，/social/search 才会就绪',
    });
  } else {
    cards.push({
      label: 'Social / X',
      tone: 'danger',
      title: '待配置上游',
      desc: '补 grok2api v3 client key 或兼容上游 key 后，统一 token 会自动复用',
    });
  }

  return cards;
}

function getQuickstartInstallHint(tokenCount, routeCards) {
  const readyProviders = routeCards.filter((card) => card.tone === 'ok').map((card) => card.label);
  const pendingProviders = routeCards.filter((card) => card.tone !== 'ok').map((card) => card.label);
  if (!tokenCount) {
    return {
      title: '先创建通用 token',
      detail: '创建后控制台会立刻刷新可复制的 .env，并把当前 provider 接线结果一起写进去。',
    };
  }
  if (pendingProviders.length) {
    return {
      title: `先接入 ${readyProviders.join(' / ') || '已就绪路由'}`,
      detail: `${pendingProviders.join(' / ')} 还没完全接通，但后续补线后会自动复用同一个通用 token。`,
    };
  }
  return {
    title: '复制 .env → 执行 ./install.sh → 验收 mysearch_health',
    detail: '当前统一 token 已可覆盖控制台里的全部路由，按最短路径安装即可完成接入。',
  };
}

function getWorkspaceSnapshot(service, services, social) {
  if (service === 'social') {
    const stats = social?.stats || {};
    const socialState = getSocialUpstreamState(social || {});
    const hasFullStats = socialState.level === 'full';
    const isV3Admin = social?.admin_api_version === 'v3' || stats.schema === 'grok2api_v3_accounts';
    if (hasFullStats && isV3Admin) {
      return {
        keysTotal: Number(stats.account_total || 0),
        keysActive: Number(stats.account_available || 0),
        tokensCount: socialState.acceptedTokenCount,
        todayCount: Number(stats.requests_24h || 0),
        remaining: null,
        remainingLabel: '24h 请求',
        primaryMetricLabel: '可用账号',
        primaryMetricValue: Number(stats.account_available || 0),
        quaternaryMetricLabel: '24h 请求',
        quaternaryMetricValue: Number(stats.requests_24h || 0),
        modeLabel: socialModeLabel(social?.mode || 'local'),
      };
    }
    return {
      keysTotal: hasFullStats ? Number(stats.token_total || 0) : socialState.acceptedTokenCount,
      keysActive: hasFullStats ? Number(stats.token_normal || 0) : socialState.upstreamApiKeyCount,
      tokensCount: hasFullStats ? Number(stats.token_total || 0) : socialState.acceptedTokenCount,
      todayCount: hasFullStats ? Number(stats.total_calls || 0) : 0,
      remaining: hasFullStats ? Number(stats.chat_remaining || 0) : null,
      remainingLabel: hasFullStats ? 'Chat 剩余' : '客户端 Token',
      primaryMetricLabel: hasFullStats ? '正常 Token' : '上游 Key',
      primaryMetricValue: hasFullStats ? Number(stats.token_normal || 0) : socialState.upstreamApiKeyCount,
      quaternaryMetricLabel: hasFullStats ? 'Chat 剩余' : '客户端 Token',
      quaternaryMetricValue: hasFullStats ? Number(stats.chat_remaining || 0) : socialState.acceptedTokenCount,
      modeLabel: socialModeLabel(social?.mode || 'local'),
    };
  }

  const payload = services?.[service] || {};
  const quota = payload.real_quota || {};
  const tavilyState = service === 'tavily' ? getTavilyRuntimeState(payload) : null;
  const tavilyUpstream = service === 'tavily' ? getTavilyUpstreamSummary(payload) : null;
  const tavilyUsingUpstream = tavilyState?.effectiveMode === 'upstream';
  return {
    keysTotal: tavilyUsingUpstream
      ? (tavilyUpstream?.available ? tavilyUpstream.totalKeys : (tavilyState.upstreamConfigured ? 1 : 0))
      : Number(payload.keys_total || 0),
    keysActive: tavilyUsingUpstream
      ? (tavilyUpstream?.available ? tavilyUpstream.activeKeys : (tavilyState.upstreamConfigured ? 1 : 0))
      : Number(payload.keys_active || 0),
    tokensCount: Number((payload.tokens || []).length),
    todayCount: Number(payload.overview?.today_count || 0),
    remaining: service === 'exa' || tavilyUsingUpstream
      ? (tavilyUsingUpstream && tavilyUpstream?.available ? tavilyUpstream.totalQuotaRemaining : null)
      : (quota.total_remaining ?? null),
    remainingLabel: tavilyUsingUpstream
      ? '上游剩余'
      : (service === 'exa' ? '实时额度' : '真实剩余'),
    primaryMetricLabel: tavilyUsingUpstream ? '上游活跃 Key' : '活跃 Key',
    primaryMetricValue: tavilyUsingUpstream
      ? (tavilyUpstream?.available ? tavilyUpstream.activeKeys : (tavilyState.upstreamConfigured ? 1 : 0))
      : Number(payload.keys_active || 0),
    quaternaryMetricLabel: tavilyUsingUpstream
      ? (tavilyUpstream?.available ? '上游剩余' : '本地 Key')
      : (service === 'exa' ? '实时额度' : '真实剩余'),
    quaternaryMetricValue: tavilyUsingUpstream
      ? (tavilyUpstream?.available ? tavilyUpstream.totalQuotaRemaining : tavilyState.localKeyCount)
      : (service === 'exa' ? null : (quota.total_remaining ?? null)),
    modeLabel: service === 'tavily'
      ? tavilyModeLabel(tavilyState.effectiveMode)
      : '独立池',
  };
}

function workspaceSignal(service, services, social) {
  const snapshot = getWorkspaceSnapshot(service, services, social);
  const meta = WORKSPACE_META[service] || {};

  if (service === 'social') {
    const socialState = getSocialUpstreamState(social || {});
    if (!socialState.canProxySearch) {
      return {
        tone: 'danger',
        label: socialState.adminConnected ? '推理待配置' : '待配置',
        summary: socialState.adminConnected
          ? `${meta.label} 管理统计已连接，但还缺少可用推理凭据。`
          : `${meta.label} 还没有接通上游兼容路由。`,
        snapshot,
      };
    }
    if (snapshot.keysActive <= 0) {
      return {
        tone: 'warn',
        label: '需要关注',
        summary: `${meta.label} 已接通，但当前没有正常 token。`,
        snapshot,
      };
    }
    return {
      tone: 'ok',
      label: '运行中',
      summary: `${meta.label} 已接通，可直接向外转发 /social/search。`,
      snapshot,
    };
  }

  if (service === 'tavily') {
    const tavilyState = getTavilyRuntimeState(services?.[service] || {});
    if (tavilyState.effectiveMode === 'upstream') {
      if (!tavilyState.upstreamConfigured) {
        return {
          tone: 'danger',
          label: '待配置上游',
          summary: `${meta.label} 当前切到了上游模式，但还没有可用的上游凭证。`,
          snapshot,
        };
      }
      return {
        tone: 'ok',
        label: '上游转发中',
        summary: `${meta.label} 当前通过上游 Gateway 转发；本地 API Key 池只作为备用库存。`,
        snapshot,
      };
    }
  }

  if (snapshot.keysTotal <= 0) {
    return {
      tone: 'danger',
      label: '待导入 Key',
        summary: service === 'tavily'
          ? `${meta.label} 还没有接通；你可以配置上游 Gateway，也可以直接导入 API Key 池。`
          : `${meta.label} 还没有导入可用 Key。`,
        snapshot,
      };
  }

  if (snapshot.keysActive <= 0) {
    return {
      tone: 'warn',
      label: 'Key 全部停用',
      summary: `${meta.label} 当前没有活跃 Key，请先启用或重新导入。`,
      snapshot,
    };
  }

  if (snapshot.remaining !== null && Number.isFinite(Number(snapshot.remaining)) && Number(snapshot.remaining) <= 100) {
    return {
      tone: 'warn',
      label: '额度偏低',
      summary: `${meta.label} 剩余额度较低，建议尽快同步或补充 Key。`,
      snapshot,
    };
  }

  return {
    tone: 'ok',
    label: '运行中',
    summary: `${meta.label} 当前工作台状态稳定，可以继续签发 Token 或同步额度。`,
    snapshot,
  };
}

function renderHeroFocus(services, social) {
  const root = document.getElementById('hero-focus');
  if (!root) return;

  const signal = workspaceSignal(activeService, services, social);
  const snapshot = signal.snapshot;
  const meta = WORKSPACE_META[activeService] || {};
  const focusName = root.querySelector('.hero-focus-name');
  const focusStatus = root.querySelector('.hero-focus-status');
  const focusDesc = root.querySelector('.hero-focus-desc');
  const focusStamp = document.getElementById('hero-focus-stamp');
  const signalDot = document.getElementById('hero-focus-signal');

  if (focusName) focusName.textContent = meta.label || '未知工作台';
  if (focusStatus) {
    focusStatus.textContent = signal.label;
    focusStatus.className = `hero-focus-status is-${signal.tone}`;
  }
  if (focusDesc) {
    focusDesc.textContent = `${signal.summary} 当前模式：${snapshot.modeLabel}。`;
  }
  if (focusStamp) {
    focusStamp.textContent = latestStatsMeta.generated_at
      ? `最近刷新 ${formatTime(latestStatsMeta.generated_at)}`
      : '等待刷新';
  }
  if (signalDot) {
    signalDot.className = `hero-focus-signal is-${signal.tone}`;
  }
}

function buildSocialProxyEnv(social) {
  const mode = social?.mode || 'local';
  const baseUrl = mode === 'local'
    ? (social.local_base_url || 'https://api.x.ai/v1')
    : (social.upstream_base_url || 'https://media.example.com/v1');
  const adminBaseUrl = social.admin_base_url || (social.upstream_base_url || baseUrl).replace(/\/v1$/, '');
  const adminUsername = social.admin_username || 'admin';
  const model = social.model || 'grok-4.20-0309';
  const fallbackModel = social.fallback_model || 'grok-4.3';
  if (mode === 'local') {
    return `# 本地模式：只使用本地 Social/X API key 池
SOCIAL_GATEWAY_LOCAL_BASE_URL=${baseUrl}
SOCIAL_GATEWAY_LOCAL_RESPONSES_PATH=${social.local_responses_path || '/responses'}
SOCIAL_GATEWAY_LOCAL_API_KEY=YOUR_XAI_API_KEY
SOCIAL_GATEWAY_MODEL=${model}
SOCIAL_GATEWAY_FALLBACK_MODEL=${fallbackModel}

# 可选：单独保护 /social/search；留空时复用 Proxy 通用 token
# SOCIAL_GATEWAY_TOKEN=`;
  }
  return `# 上游模式：在上游客户端密钥页面创建 g2a_ key
SOCIAL_GATEWAY_UPSTREAM_BASE_URL=${baseUrl}
SOCIAL_GATEWAY_UPSTREAM_API_KEY=YOUR_GROK2API_G2A_CLIENT_KEY
SOCIAL_GATEWAY_MODEL=${model}
SOCIAL_GATEWAY_FALLBACK_MODEL=${fallbackModel}

# 可选：接通 grok2api v3 管理统计
SOCIAL_GATEWAY_ADMIN_BASE_URL=${adminBaseUrl}
SOCIAL_GATEWAY_ADMIN_USERNAME=${adminUsername}
SOCIAL_GATEWAY_ADMIN_PASSWORD=YOUR_GROK2API_ADMIN_PASSWORD

# 仅 grok2api v2-compatible legacy 部署需要后台 app_key 自动继承
# SOCIAL_GATEWAY_ADMIN_APP_KEY=YOUR_GROK2API_V2_APP_KEY
# 可选：单独保护 /social/search；留空时复用 Proxy 通用 token
# SOCIAL_GATEWAY_TOKEN=`;
}

function buildSocialMySearchEnv(social) {
  const baseUrl = location.origin;
  const socialReady = getSocialUpstreamState(social || {}).canProxySearch;
  return `# 推荐：直接用 MySearch 通用 token，一次接上 Tavily / Firecrawl / Exa / Social
MYSEARCH_PROXY_BASE_URL=${baseUrl}
MYSEARCH_PROXY_API_KEY=YOUR_MYSEARCH_PROXY_TOKEN

# 当前 Social / X ${socialReady ? '已经接通，可直接复用上面的通用 token。' : '还没完全接通；上面的通用 token 先可用于 Tavily / Firecrawl / Exa。'}

# 如果你只想单独接 Social / X，也可以显式写 compatible 模式：
MYSEARCH_XAI_SEARCH_MODE=compatible
MYSEARCH_XAI_SOCIAL_BASE_URL=${baseUrl}
MYSEARCH_XAI_SOCIAL_SEARCH_PATH=/social/search
MYSEARCH_XAI_API_KEY=YOUR_MYSEARCH_PROXY_TOKEN`;
}

function buildMySearchEnv(mysearch, social, { includeSecret = false } = {}) {
  const baseUrl = location.origin;
  const token = includeSecret && mysearch?.tokens?.[0]?.token
    ? mysearch.tokens[0].token
    : 'YOUR_MYSEARCH_PROXY_TOKEN';
  const routeCards = getQuickstartProviderCards(latestServices, social || {});
  const readyProviders = routeCards.filter((card) => card.tone === 'ok').map((card) => card.label);
  const pendingProviders = routeCards.filter((card) => card.tone !== 'ok').map((card) => `${card.label}: ${card.title}`);
  const socialReady = getSocialUpstreamState(social || {}).canProxySearch;
  return `# 最省事的接法：只填这两项，MySearch 会默认走当前 proxy
MYSEARCH_PROXY_BASE_URL=${baseUrl}
MYSEARCH_PROXY_API_KEY=${token}

# 当前路由状态：
${routeCards.map((card) => `# - ${card.label}: ${card.title}${card.desc ? ` · ${card.desc}` : ''}`).join('\n')}

# 说明：
# - 当前已就绪 provider：${readyProviders.length ? readyProviders.join(' / ') : '暂无，先在控制台接线'}
# - Social / X ${socialReady ? '当前已接通，会默认复用同一个 token' : '当前还没完全接通，后续接好后也会自动复用同一个 token'}
# - ${pendingProviders.length ? `仍需关注：${pendingProviders.join('；')}` : '当前统一 token 已可覆盖控制台里的所有路由'}

# 可选：如果你想把 MCP 额外暴露成远程 HTTP，再补这一段
# MYSEARCH_MCP_HOST=0.0.0.0
# MYSEARCH_MCP_PORT=8000
# MYSEARCH_MCP_STREAMABLE_HTTP_PATH=/mcp`;
}

function buildMySearchInstall() {
  return `git clone https://github.com/skernelx/MySearch-Proxy.git
cd MySearch-Proxy
cp mysearch/.env.example mysearch/.env

# 把上面的 MYSEARCH_PROXY_* 粘进去后执行
./install.sh

# 如果你想作为远程 MCP 提供给别的客户端：
./venv/bin/python -m mysearch \\
  --transport streamable-http \\
  --host 0.0.0.0 \\
  --port 8000 \\
  --streamable-http-path /mcp`;
}

function renderSocialBoard(social) {
  const stats = social?.stats || {};
  const isV3Admin = social?.admin_api_version === 'v3' || stats.schema === 'grok2api_v3_accounts';
  const socialState = getSocialUpstreamState(social || {});
  const localMode = social?.mode === 'local';
  const mode = socialModeLabel(social?.mode || 'local');
  const statusText = socialStatusLabel(social);
  const tokenSource = socialTokenSourceLabel(social?.token_source || '');
  const authText = social?.client_auth_configured ? '已允许客户端调用 /social/search' : '还没有设置客户端 token';
  const videoValue = stats.video_remaining === null
    ? '<div class="value muted is-text">无法统计</div>'
    : `<div class="value muted">${fmtNum(stats.video_remaining)}</div>`;
  let foot = localMode
    ? '本地模式仅使用本地 Social/X Key 池，不连接上游管理后台。'
    : '上游模式尚未接通。请补齐上游 client key；如需管理统计，再填写后台管理员账号。';
  if (localMode && social?.upstream_key_configured) {
    foot = '本地模式已接通；请求只从本地 Social/X Key 池轮询。';
  } else if (social?.admin_connected) {
    foot = isV3Admin
      ? '当前通过 grok2api v3 管理 API 同步账号状态与请求统计；推理仍独立使用 g2a_ client key。'
      : '当前通过 legacy v2 后台同步 token 状态和剩余额度。对外仍统一提供 /social/search 结果结构。';
  } else if (social?.upstream_key_configured) {
    foot = '当前已经能转发 Social 搜索，但还没有管理统计。v3 请补管理员用户名和密码；v2-compatible 才使用 app key。';
  }
  const errorLine = social?.error
    ? `<div class="social-board-foot is-error">最近错误：${escapeHtml(social.error)}</div>`
    : '';

  if (socialState.level !== 'full') {
    document.getElementById('social-board').innerHTML = `
      <div class="social-board-top">
        <div>
          <div class="social-board-kicker">Social / X 状态</div>
          <div class="social-board-title">Social / X Router</div>
        </div>
      </div>
      <div class="social-board-desc">
        ${localMode ? '当前明确使用本地 Key 池，只展示本地调度和客户端鉴权状态。' : '当前还没有拿到完整后台 token 面板，所以这里只展示基础接线状态。'}
      </div>
      <div class="social-board-grid">
        <div class="social-metric">
          <div class="label">可调度 Key</div>
          <div class="value ${socialState.upstreamAvailableKeyCount ? 'ok' : 'danger'}">${fmtNum(socialState.upstreamAvailableKeyCount)} <span class="muted">/ ${fmtNum(socialState.upstreamApiKeyCount)}</span></div>
        </div>
        <div class="social-metric">
          <div class="label">客户端 Token 数</div>
          <div class="value ok">${fmtNum(socialState.acceptedTokenCount)}</div>
        </div>
        <div class="social-metric">
          <div class="label">后台状态</div>
          <div class="value muted is-text">${localMode ? '不参与' : (socialState.adminConnected ? '已接通' : '未接通')}</div>
        </div>
        <div class="social-metric">
          <div class="label">搜索转发</div>
          <div class="value ${socialState.canProxySearch ? 'ok' : 'warn'} is-text">${socialState.canProxySearch ? '已可转发' : '待补鉴权'}</div>
        </div>
      </div>
      <div class="social-board-summary">
        <div class="social-board-summary-item">
          <div class="label">当前状态</div>
          <div class="value">${escapeHtml(statusText)}</div>
        </div>
        <div class="social-board-summary-item">
          <div class="label">Token 来源</div>
          <div class="value">${escapeHtml(tokenSource)}</div>
        </div>
        <div class="social-board-summary-item">
          <div class="label">客户端访问</div>
          <div class="value">${escapeHtml(authText)}</div>
        </div>
        <div class="social-board-summary-item">
          <div class="label">工作模式</div>
          <div class="value">${escapeHtml(mode)}</div>
        </div>
      </div>
      <div class="social-board-foot">
        <strong>${statusText}</strong> ${socialState.detail || foot}
      </div>
      ${errorLine}
    `;
    return;
  }

  const metricGrid = isV3Admin ? `
      <div class="social-metric">
        <div class="label">账号总数</div>
        <div class="value">${fmtNum(stats.account_total || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">账号可用</div>
        <div class="value ok">${fmtNum(stats.account_available || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">恢复中</div>
        <div class="value warn">${fmtNum(stats.account_recovering || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">需要处理</div>
        <div class="value danger">${fmtNum(stats.account_attention || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">24h 请求</div>
        <div class="value info">${fmtNum(stats.requests_24h || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">历史请求</div>
        <div class="value">${fmtNum(stats.total_calls || 0)}</div>
      </div>
    ` : `
      <div class="social-metric">
        <div class="label">Token 总数</div>
        <div class="value">${fmtNum(stats.token_total || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">Token 正常</div>
        <div class="value ok">${fmtNum(stats.token_normal || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">Token 限流</div>
        <div class="value warn">${fmtNum(stats.token_limited || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">Token 失效</div>
        <div class="value danger">${fmtNum(stats.token_invalid || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">Chat 剩余</div>
        <div class="value">${fmtNum(stats.chat_remaining || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">Image 剩余</div>
        <div class="value info">${fmtNum(stats.image_remaining || 0)}</div>
      </div>
      <div class="social-metric">
        <div class="label">Video 剩余</div>
        ${videoValue}
      </div>
      <div class="social-metric">
        <div class="label">总调用次数</div>
        <div class="value">${fmtNum(stats.total_calls || 0)}</div>
      </div>
    `;

  document.getElementById('social-board').innerHTML = `
    <div class="social-board-top">
      <div>
        <div class="social-board-kicker">Social / X 状态</div>
        <div class="social-board-title">Social / X Router</div>
      </div>
    </div>
    <div class="social-board-desc">
      ${isV3Admin ? '这里展示 grok2api v3 账号可用性和请求统计；额度口径由上游账号类型决定，不再套用旧 token quota 字段。' : '这里展示 legacy token 池状态；对外仍是一套统一的 Social / X 结果结构。'}
    </div>
    <div class="social-board-grid">
      ${metricGrid}
    </div>
    <div class="social-board-summary">
      <div class="social-board-summary-item">
        <div class="label">当前状态</div>
        <div class="value">${escapeHtml(statusText)}</div>
      </div>
      <div class="social-board-summary-item">
        <div class="label">Token 来源</div>
        <div class="value">${escapeHtml(tokenSource)}</div>
      </div>
      <div class="social-board-summary-item">
        <div class="label">客户端访问</div>
        <div class="value">${escapeHtml(authText)}</div>
      </div>
      <div class="social-board-summary-item">
        <div class="label">工作模式</div>
        <div class="value">${escapeHtml(mode)}</div>
      </div>
    </div>
    <div class="social-board-foot">
      <strong>${statusText}</strong> ${foot}
    </div>
    ${errorLine}
  `;
}

function renderSocialIntegration(social) {
  const socialState = getSocialUpstreamState(social || {});
  const localMode = social?.mode === 'local';
  const mode = socialModeLabel(social?.mode || 'local');
  const source = socialTokenSourceLabel(social?.token_source || '');
  const proxyConfigured = Boolean(social?.upstream_key_configured);
  const clientConfigured = Boolean(social?.client_auth_configured);
  const authLabel = clientConfigured ? '已就绪' : '未配置';
  const statusLabel = socialStatusLabel(social);
  const upstreamBase = localMode
    ? (social?.local_base_url || 'https://api.x.ai/v1')
    : (social?.upstream_base_url || 'https://media.example.com/v1');
  const adminBase = localMode ? '当前模式不参与' : (social?.admin_base_url || '未设置');
  let note = localMode
    ? '本地模式使用本地 Key 池，所有上游 Admin 配置均不参与当前请求。'
    : '上游模式请填写网关地址和可用于推理的 client key。';
  if (localMode && social?.upstream_key_configured) {
    note = '本地 Key 池已配置，当前请求不会读取上游管理后台。';
  } else if (social?.admin_connected) {
    note = social?.admin_api_version === 'v3'
      ? 'v3 管理统计已连通；推理继续使用独立 g2a_ client key，不复用管理员会话。'
      : '当前 legacy v2 后台自动继承已连通；推理凭据来自旧 app key 合同。';
  } else if (social?.upstream_key_configured) {
    note = '当前上游调用凭据已配置，可以直接使用 /social/search。';
  }
  const noteClass = social?.error ? 'integration-note is-error' : 'integration-note';

  document.getElementById('social-integration').innerHTML = `
    <div class="social-integration-head">
      <div>
        <div class="service-brief-note">Compatibility Layer</div>
        <h3>Social / X 接入</h3>
      </div>
      <span class="detail-pill">摘要优先</span>
    </div>
    <p class="desc">${localMode ? '本地模式直接维护 Social/X Key 池；上游模式连接 grok2api 或兼容网关。两套来源互斥，不会合并调度。' : '上游模式使用独立 client key 调用 /responses，并可通过 Admin API 读取账号和请求统计。'}</p>
    <div class="integration-summary integration-summary-compact">
      <div class="integration-summary-item">
        <div class="label">当前状态</div>
        <div class="value">${escapeHtml(statusLabel)}</div>
      </div>
      <div class="integration-summary-item">
        <div class="label">工作模式</div>
        <div class="value">${escapeHtml(mode)}</div>
      </div>
      <div class="integration-summary-item">
        <div class="label">Token 来源</div>
        <div class="value is-tight">${escapeHtml(source)}</div>
      </div>
      <div class="integration-summary-item">
        <div class="label">客户端鉴权</div>
        <div class="value">${escapeHtml(authLabel)}</div>
      </div>
    </div>
    <details class="integration-fold" open>
      <summary>接线详情</summary>
      <div class="integration-summary integration-summary-detail">
        <div class="integration-summary-item integration-summary-item-wide">
          <div class="label">${localMode ? '本地接口' : '上游接口'}</div>
          <div class="value mono">${escapeHtml(upstreamBase)}</div>
        </div>
        <div class="integration-summary-item integration-summary-item-wide">
          <div class="label">后台地址</div>
          <div class="value mono">${escapeHtml(adminBase)}</div>
        </div>
        <div class="integration-summary-item">
          <div class="label">接入结果</div>
          <div class="value">${proxyConfigured ? `已拿到可用${localMode ? '本地' : '上游'} key` : `尚未拿到${localMode ? '本地' : '上游'} key`}</div>
        </div>
        <div class="integration-summary-item">
          <div class="label">可调度 / 隔离 Key</div>
          <div class="value">${fmtNum(socialState.upstreamAvailableKeyCount)} / ${fmtNum(socialState.upstreamUnavailableKeyCount)}</div>
        </div>
        <div class="integration-summary-item">
          <div class="label">客户端 Token 数</div>
          <div class="value">${fmtNum(socialState.acceptedTokenCount)}</div>
        </div>
        <div class="integration-summary-item">
          <div class="label">兼容形态</div>
          <div class="value is-tight">X Search Router</div>
          <div class="value-meta">compatible route</div>
        </div>
      </div>
    </details>
    <div class="${noteClass}">
      <div class="integration-note-copy">
        <strong>${escapeHtml(statusLabel)}</strong>
        <span>${escapeHtml(note)}</span>
        ${social?.error ? `<span class="integration-note-error">最近错误：${escapeHtml(social.error)}</span>` : ''}
      </div>
    </div>
    <div class="code-toolbar">
      <div class="endpoint">Proxy 端环境变量。先补 g2a_ client key；需要管理统计时再补管理员密码。</div>
      <button class="btn btn-sm" type="button" onclick="copyCode('social-proxy-env', this)">复制 Proxy 配置</button>
    </div>
    <pre class="code-block mono" id="social-proxy-env"></pre>
    <div class="code-toolbar" style="margin-top:12px">
      <div class="endpoint">MySearch / MCP / Skill 端环境变量。现在更推荐直接使用 MySearch 通用 token，一次接上全部路由。</div>
      <button class="btn btn-sm" type="button" onclick="copyCode('social-mysearch-env', this)">复制 MySearch 配置</button>
    </div>
    <pre class="code-block mono" id="social-mysearch-env"></pre>
  `;

  document.getElementById('social-proxy-env').textContent = buildSocialProxyEnv(social || {});
  document.getElementById('social-mysearch-env').textContent = buildSocialMySearchEnv(social || {});
}

function renderMySearchQuickstart(mysearch, social) {
  const root = document.getElementById('mysearch-quickstart');
  if (!root) return;

  const tokens = mysearch?.tokens || [];
  const tokenCount = tokens.length;
  const todayCount = mysearch?.overview?.today_count || 0;
  const monthCount = mysearch?.overview?.month_count || 0;
  const routeCards = getQuickstartProviderCards(latestServices, social || {});
  const readyProviders = routeCards.filter((card) => card.tone === 'ok');
  const pendingProviders = routeCards.filter((card) => card.tone !== 'ok');
  const installHint = getQuickstartInstallHint(tokenCount, routeCards);
  const noteClass = tokenCount > 0 ? 'integration-note' : 'integration-note is-error';
  const note = tokenCount > 0
    ? pendingProviders.length
      ? `这里创建的是 MySearch 通用 token。它已经能被当前已接通的 provider 直接复用；${pendingProviders.map((card) => card.label).join(' / ')} 还没完全接通，后续补齐后也会自动纳入统一入口。`
      : '这里创建的是 MySearch 通用 token。它会同时被 Tavily / Firecrawl / Exa / Social 路由接受。'
    : '先创建一个 MySearch 通用 token。创建后控制台会自动生成可直接复制的 .env 配置。';
  const noteAction = tokenCount > 0
    ? ''
    : '<button class="btn btn-primary btn-sm" type="button" onclick="createMySearchBootstrapToken(this)">立即创建并生成 .env</button>';

  root.innerHTML = `
    <div class="service-head">
      <div class="service-head-copy">
        <span class="service-chip" data-service="mysearch">Unified Client Entry</span>
        <h2>统一接入配置</h2>
        <p>这块不是 provider 池，而是给 Codex / Claude Code / 其他 MCP 客户端准备的统一接入层。目标是把连接方式、安装命令和通用 token 收成一条稳定入口，减少手动区分底层服务。</p>
        <div class="service-head-route">
          <span class="service-head-route-label">Route</span>
          <span class="mono">统一入口: MYSEARCH_PROXY_BASE_URL + MYSEARCH_PROXY_API_KEY</span>
        </div>
      </div>
      <div class="service-tools">
        <div class="service-tool-kicker">Client Ready · ${fmtNum(readyProviders.length)}/${fmtNum(routeCards.length)}</div>
        <div class="service-sync-meta">推荐直接复制下面的 <span class="mono">MYSEARCH_PROXY_*</span> 配置，不再手写一堆 provider 地址。</div>
        <div class="service-sync-meta">当前已就绪：${escapeHtml(readyProviders.map((card) => card.label).join(' / ') || '暂无')}</div>
        <div class="service-sync-meta">通用 Token 前缀：<span class="mono">${escapeHtml(mysearch?.token_prefix || 'mysp-')}</span></div>
      </div>
    </div>
    <div class="service-body">
      <div class="quickstart-tabs workspace-tabs" role="tablist" aria-label="MySearch 接入任务">
        <button type="button" class="workspace-tab quickstart-tab is-active" id="quickstart-tab-config" role="tab" aria-selected="true" aria-controls="quickstart-panel-config" tabindex="0" data-quickstart-tab="config" onclick="setQuickstartTab('config')">配置</button>
        <button type="button" class="workspace-tab quickstart-tab" id="quickstart-tab-install" role="tab" aria-selected="false" aria-controls="quickstart-panel-install" tabindex="-1" data-quickstart-tab="install" onclick="setQuickstartTab('install')">安装</button>
        <button type="button" class="workspace-tab quickstart-tab" id="quickstart-tab-tokens" role="tab" aria-selected="false" aria-controls="quickstart-panel-tokens" tabindex="-1" data-quickstart-tab="tokens" onclick="setQuickstartTab('tokens')">Token</button>
      </div>
      <div class="quickstart-grid">
        <section class="subcard api-shell quickstart-card quickstart-primary-card" id="quickstart-panel-config" role="tabpanel" aria-labelledby="quickstart-tab-config" aria-hidden="false" data-quickstart-panel="config">
          <div class="quickstart-card-head">
            <div class="quickstart-card-copy">
              <div class="service-brief-note">Access Config</div>
              <h3>一键配置</h3>
              <p class="desc">推荐把 MySearch 统一接到当前 proxy。这样客户端只认一个 base URL 和一个通用 token，底层 Tavily / Firecrawl / Exa / Social 都由 proxy 负责收口。</p>
            </div>
            <span class="detail-pill">统一入口</span>
          </div>
          <div class="quickstart-primary-layout">
            <div class="quickstart-visual-col">
              <div class="quickstart-visual-head">
                <div class="label">Provider Readiness</div>
                <strong>${fmtNum(readyProviders.length)} / ${fmtNum(routeCards.length)} 已接通</strong>
                <span>${pendingProviders.length ? `待补：${pendingProviders.map((card) => card.label).join(' / ')}` : '四条路由都已接通，可直接复制统一接入配置。'}</span>
              </div>
              <div class="quickstart-route-strip">
                ${routeCards.map((card) => `
                  <div class="quickstart-route-card is-${card.tone}">
                    <div class="label">${escapeHtml(card.label)}</div>
                    <strong>${escapeHtml(card.title)}</strong>
                    <span>${escapeHtml(card.desc)}</span>
                  </div>
                `).join('')}
              </div>
              <div class="integration-summary">
                <div class="integration-summary-item integration-summary-item-wide">
                  <div class="label">Proxy Base URL</div>
                  <div class="value mono">${escapeHtml(location.origin)}</div>
                </div>
                <div class="integration-summary-item">
                  <div class="label">通用 Token</div>
                  <div class="value">${fmtNum(tokenCount)}</div>
                </div>
                <div class="integration-summary-item">
                  <div class="label">今日 / 本月调用</div>
                  <div class="value">${fmtNum(todayCount)} <span class="muted">/ ${fmtNum(monthCount)}</span></div>
                </div>
                <div class="integration-summary-item">
                  <div class="label">Provider Ready</div>
                  <div class="value">${fmtNum(readyProviders.length)} / ${fmtNum(routeCards.length)}</div>
                </div>
              </div>
            </div>
            <div class="quickstart-config-col">
              <div class="${noteClass}">
                <div class="integration-note-copy">
                  <strong>${tokenCount > 0 ? '可以直接复制配置了' : '还差一个通用 token'}</strong>
                  <span>${escapeHtml(note)}</span>
                </div>
                ${noteAction}
              </div>
              <div class="code-toolbar">
                <div class="endpoint">复制到 <span class="mono">mysearch/.env</span> 就能用。默认已经包含统一 proxy 接法。</div>
                <button class="btn btn-sm" type="button" onclick="copyMySearchEnv(this)">复制含 Token 的 .env</button>
              </div>
              <pre class="code-block mono" id="mysearch-proxy-env"></pre>
            </div>
          </div>
        </section>
        <section class="subcard api-shell quickstart-card hidden" id="quickstart-panel-install" role="tabpanel" aria-labelledby="quickstart-tab-install" aria-hidden="true" data-quickstart-panel="install">
          <div class="quickstart-card-head">
            <div class="quickstart-card-copy">
              <div class="service-brief-note">Install Path</div>
              <h3>安装路径</h3>
              <p class="desc">把接入配置和安装命令分开看。先确认 token 与 <span class="mono">.env</span>，再决定走本机安装还是远程 streamable-http。</p>
            </div>
            <span class="detail-pill">Shortest Path</span>
          </div>
          <div class="quickstart-install-layout">
            <div class="quickstart-install-strip">
              <div class="label">最短安装路径</div>
              <strong>${escapeHtml(installHint.title)}</strong>
              <span>${escapeHtml(installHint.detail)}</span>
              <div class="quickstart-install-meta">
                <div class="quickstart-install-meta-item">
                  <div class="label">默认形态</div>
                  <strong>stdio</strong>
                </div>
                <div class="quickstart-install-meta-item">
                  <div class="label">远程备选</div>
                  <strong>streamable-http</strong>
                </div>
              </div>
              <div class="quickstart-install-steps">
                <span class="quickstart-install-step ${tokenCount > 0 ? 'is-done' : 'is-active'}">1. ${tokenCount > 0 ? '已具备通用 token' : '创建通用 token'}</span>
                <span class="quickstart-install-step ${tokenCount > 0 ? 'is-active' : ''}">2. 复制 .env</span>
                <span class="quickstart-install-step">3. 执行 ./install.sh</span>
              </div>
              <div class="quickstart-install-actions">
                <button class="btn btn-soft btn-sm" type="button" onclick="copyEnvAndRevealInstall(this)">复制 .env 并定位命令</button>
              </div>
            </div>
            <div class="quickstart-command-col">
              <div class="quickstart-command-shell">
                <div class="code-toolbar">
                  <div class="endpoint">本地安装 / 远程启动命令，按仓库默认流程直接走。</div>
                  <button class="btn btn-sm" type="button" onclick="copyCode('mysearch-install-cmd', this)">复制命令</button>
                </div>
                <pre class="code-block mono" id="mysearch-install-cmd"></pre>
              </div>
            </div>
          </div>
        </section>
        <section class="subcard detail-card detail-card-static hidden" id="quickstart-panel-tokens" role="tabpanel" aria-labelledby="quickstart-tab-tokens" aria-hidden="true" data-quickstart-panel="tokens">
          <div class="detail-card-static-head">
            <div>
              <h3>MySearch 通用 Token</h3>
              <p>这个 token 专门给上层 MCP / Skill 用。和 Tavily / Firecrawl / Exa 各自的服务 token 分开管理，但调用时会被三条 provider 路由一起接受。</p>
            </div>
            <span class="detail-pill">统一入口</span>
          </div>
          <div class="detail-body">
            <div class="form-row token-create-row">
              <input class="input-grow" type="text" id="token-name-mysearch" aria-label="MySearch Token 备注" placeholder="可选备注，例如 Codex / Claude Code / Team Demo">
              <button class="btn btn-primary" type="button" onclick="createToken('mysearch', this)">创建通用 Token</button>
            </div>
            <div class="detail-glance" id="token-glance-mysearch"></div>
            <div class="detail-caption">表格只保留摘要。点击任一行，可在右侧查看掩码、调用统计和维护动作；完整值仅通过复制获取。</div>
            ${renderTableLegend('token')}
            <div class="table-tools">
              <div class="table-tools-row">
                <input class="input-grow" type="text" id="token-search-mysearch" aria-label="搜索 MySearch Token" placeholder="搜索 token / 名称 / 创建时间" oninput="setTokenSearch('mysearch', this.value)">
              </div>
              <div class="table-tools-row">
                <div class="mini-switch" role="group" aria-label="MySearch Token 排序">
                  <button class="mini-switch-btn is-active" type="button" data-token-sort="risk" data-service="mysearch" aria-pressed="true" onclick="setTokenSort('mysearch', 'risk')">失败优先</button>
                  <button class="mini-switch-btn" type="button" data-token-sort="activity" data-service="mysearch" aria-pressed="false" onclick="setTokenSort('mysearch', 'activity')">按活跃度</button>
                  <button class="mini-switch-btn" type="button" data-token-sort="today" data-service="mysearch" aria-pressed="false" onclick="setTokenSort('mysearch', 'today')">按今日调用</button>
                  <button class="mini-switch-btn" type="button" data-token-sort="name" data-service="mysearch" aria-pressed="false" onclick="setTokenSort('mysearch', 'name')">按名称</button>
                </div>
              </div>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Token</th>
                    <th scope="col">名称</th>
                    <th scope="col">运行摘要</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody id="tokens-body-mysearch"></tbody>
              </table>
            </div>
          </div>
        </section>
      </div>
    </div>
  `;

  document.getElementById('mysearch-proxy-env').textContent = buildMySearchEnv(mysearch || {}, social || {});
  document.getElementById('mysearch-install-cmd').textContent = buildMySearchInstall();
  renderTokens('mysearch', tokens);
  renderPoolGlance('mysearch', mysearch || {});
  setQuickstartTab(activeQuickstartTab, false);
}

async function createMySearchBootstrapToken(button) {
  await runWithBusyButton(button, {
    busyLabel: '创建中...',
    successLabel: '已创建',
    errorLabel: '创建失败',
    minBusyMs: 560,
  }, async () => {
    await api('POST', '/api/tokens', {
      service: 'mysearch',
      name: 'MySearch General Token',
    });
  });
  await sleep(180);
  showToast('已创建 MySearch 通用 token，下面的 .env 已自动更新。', 'success');
  await refresh({ force: true, scope: getRefreshScopeForService('mysearch') });
  const envBlock = document.getElementById('mysearch-proxy-env');
  if (envBlock) {
    setQuickstartTab('config', false);
    envBlock.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function collectTavilySettingsForm() {
  const body = {
    mode: document.getElementById('settings-tavily-mode').value,
    upstream_base_url: document.getElementById('settings-tavily-upstream-base-url').value.trim(),
    upstream_search_path: document.getElementById('settings-tavily-upstream-search-path').value.trim(),
    upstream_extract_path: document.getElementById('settings-tavily-upstream-extract-path').value.trim(),
    upstream_admin_base_url: document.getElementById('settings-tavily-upstream-admin-base-url').value.trim(),
  };
  const upstreamApiKey = document.getElementById('settings-tavily-upstream-api-key').value.trim();
  const upstreamAdminHeaders = document.getElementById('settings-tavily-upstream-admin-headers').value.trim();
  const upstreamAdminCookie = document.getElementById('settings-tavily-upstream-admin-cookie').value.trim();
  if (upstreamApiKey) body.upstream_api_key = upstreamApiKey;
  if (upstreamAdminHeaders) body.upstream_admin_headers = upstreamAdminHeaders;
  if (upstreamAdminCookie) body.upstream_admin_cookie = upstreamAdminCookie;
  return body;
}

function collectSocialSettingsForm() {
  const body = {
    mode: document.getElementById('settings-social-mode').value,
    local_base_url: document.getElementById('settings-social-local-base-url').value.trim(),
    local_responses_path: document.getElementById('settings-social-local-responses-path').value.trim(),
    upstream_base_url: document.getElementById('settings-social-upstream-base-url').value.trim(),
    upstream_responses_path: document.getElementById('settings-social-upstream-responses-path').value.trim(),
    admin_base_url: document.getElementById('settings-social-admin-base-url').value.trim(),
    admin_username: document.getElementById('settings-social-admin-username').value.trim(),
    admin_verify_path: document.getElementById('settings-social-admin-verify-path').value.trim(),
    admin_config_path: document.getElementById('settings-social-admin-config-path').value.trim(),
    admin_tokens_path: document.getElementById('settings-social-admin-tokens-path').value.trim(),
    model: document.getElementById('settings-social-model').value.trim(),
    fallback_model: document.getElementById('settings-social-fallback-model').value.trim(),
    cache_ttl_seconds: document.getElementById('settings-social-cache-ttl-seconds').value.trim(),
    fallback_min_results: document.getElementById('settings-social-fallback-min-results').value.trim(),
  };

  const adminAppKey = document.getElementById('settings-social-admin-app-key').value.trim();
  const adminPassword = document.getElementById('settings-social-admin-password').value.trim();
  const localApiKey = document.getElementById('settings-social-local-api-key').value.trim();
  const upstreamApiKey = document.getElementById('settings-social-upstream-api-key').value.trim();
  const gatewayToken = document.getElementById('settings-social-gateway-token').value.trim();
  const clearLocalApiKey = document.getElementById('settings-social-clear-local-api-key').checked;
  const clearUpstreamApiKey = document.getElementById('settings-social-clear-upstream-api-key').checked;

  if (adminAppKey) body.admin_app_key = adminAppKey;
  if (adminPassword) body.admin_password = adminPassword;
  if (clearLocalApiKey) body.clear_local_api_key = true;
  else if (localApiKey) body.local_api_key = localApiKey;
  if (clearUpstreamApiKey) body.clear_upstream_api_key = true;
  else if (upstreamApiKey) body.upstream_api_key = upstreamApiKey;
  if (gatewayToken) body.gateway_token = gatewayToken;
  return body;
}

function renderSettingsProbeMessage(kind, payload = {}) {
  if (kind === 'tavily') {
    const mode = payload.effective_mode === 'upstream' ? '上游 Gateway' : 'API Key 池';
    const detail = payload.detail || payload.summary || '诊断已完成。';
    return `Tavily ${payload.ok ? '接线正常' : '接线失败'}：当前实际 ${mode}，${detail}`;
  }
  const detail = payload.detail || payload.token_source || '诊断已完成。';
  return `Social / X ${payload.ok ? '接线正常' : '接线失败'}：${detail}`;
}

function clearSettingsProbe(kind) {
  const shell = document.getElementById(`settings-${kind}-probe`);
  if (!shell) return;
  shell.innerHTML = '';
  shell.classList.add('hidden');
  shell.classList.remove('is-error');
}

function getSettingsProbeMeta(kind, payload = {}) {
  if (kind === 'tavily') {
    const mode = payload.effective_mode === 'upstream' ? '上游 Gateway' : 'API Key 池';
    const requestTarget = payload.request_target || payload.probe_url || '未配置';
    const authSource = payload.auth_source || (payload.effective_mode === 'upstream'
      ? '上游 API key / Gateway token'
      : `本地 API Key 池（活跃 ${fmtNum(payload.local_key_count || 0)}）`);
    const returnStatus = payload.status_label || (payload.status_code ? `HTTP ${payload.status_code}` : '未执行 live probe');
    const failureReason = payload.ok ? '无' : (payload.failure_reason || payload.error || payload.detail || '未通过诊断');
    let recommendation = payload.recommendation || '';
    if (!recommendation) {
      if (payload.ok) {
        recommendation = payload.effective_mode === 'upstream'
          ? '当前链路可用；如果想固定行为，可以保持 upstream，或者切回 auto 让控制台自动识别。'
          : '当前本地 API Key 池可用；如果想统一上游维护，可以继续配置 Tavily Gateway。';
      } else if ((payload.local_key_count || 0) <= 0 && payload.effective_mode !== 'upstream') {
        recommendation = '导入至少一个 Tavily API Key，或者改成上游 Gateway 并补上凭证。';
      } else if (payload.effective_mode === 'upstream') {
        recommendation = '检查上游 Base URL、Search Path 和上游 token 是否有效。';
      } else {
        recommendation = '检查本地 API Key 是否可用，必要时切到上游 Gateway 做统一接线。';
      }
    }
    return {
      tone: payload.ok ? 'ok' : 'error',
      title: `Tavily ${payload.ok ? '测试通过' : '测试失败'}`,
      eyebrow: 'Latest Probe',
      pills: [mode, tavilyModeSourceLabel(payload.mode_source || 'auto_pending')],
      items: [
        { label: '请求目标', value: requestTarget, mono: true },
        { label: '鉴权来源', value: authSource },
        { label: '返回状态', value: returnStatus },
        { label: '失败原因', value: failureReason },
        { label: '建议动作', value: recommendation, wide: true },
      ],
    };
  }

  const requestTarget = payload.request_target || `${payload.upstream_base_url || '未配置'}${payload.upstream_responses_path || '/responses'}`;
  const authSource = payload.auth_source || payload.token_source || '未解析到可用鉴权';
  const returnStatus = payload.status_label || (payload.mode === 'local'
    ? (payload.ok ? '本地模式可用' : '本地模式待配置')
    : (payload.admin_connected ? '后台已连通' : (payload.ok ? '已解析到可用凭证' : '诊断失败')));
  const failureReason = payload.ok ? '无' : (payload.failure_reason || payload.error || payload.detail || '未通过诊断');
  let recommendation = payload.recommendation || '';
  if (!recommendation) {
    if (payload.mode === 'local') {
      recommendation = payload.ok
        ? '当前仅使用本地 Social/X Key 池；上游 Admin 不参与请求。'
        : '补充本地 Social/X Key 和客户端访问 token。';
    } else if (payload.ok && payload.admin_connected) {
      recommendation = '当前后台自动继承正常，可以直接下发 MySearch 通用 token 给客户端。';
    } else if (payload.ok) {
      recommendation = '当前已能转发 Social / X 搜索；如果要更完整的 token 元数据，继续补 grok2api 后台。';
    } else {
      recommendation = '优先检查 grok2api /v1 地址与 g2a_ client key；只有 v2-compatible legacy 部署才检查后台 app key。';
    }
  }
  return {
    tone: payload.ok ? 'ok' : 'error',
    title: `Social / X ${payload.ok ? '测试通过' : '测试失败'}`,
    eyebrow: 'Latest Probe',
    pills: [socialModeLabel(payload.mode || 'local'), socialTokenSourceLabel(payload.token_source || '')],
    items: [
      { label: '请求目标', value: requestTarget, mono: true },
      { label: '鉴权来源', value: authSource },
      { label: '返回状态', value: returnStatus },
      { label: '失败原因', value: failureReason },
      { label: '建议动作', value: recommendation, wide: true },
    ],
  };
}

function renderSettingsProbe(kind, payload = {}) {
  const shell = document.getElementById(`settings-${kind}-probe`);
  if (!shell) return;
  const meta = getSettingsProbeMeta(kind, payload);
  shell.classList.remove('hidden');
  shell.classList.toggle('is-error', meta.tone === 'error');
  shell.innerHTML = `
    <div class="settings-probe-head">
      <div>
        <div class="settings-probe-eyebrow">${escapeHtml(meta.eyebrow)}</div>
        <strong>${escapeHtml(meta.title)}</strong>
      </div>
      <div class="settings-probe-pills">
        ${meta.pills.map((pill) => `<span class="settings-probe-pill">${escapeHtml(pill)}</span>`).join('')}
      </div>
    </div>
    <div class="settings-probe-grid">
      ${meta.items.map((item) => `
        <div class="settings-probe-card ${item.wide ? 'is-wide' : ''}">
          <div class="label">${escapeHtml(item.label)}</div>
          <div class="value ${item.mono ? 'mono is-address' : ''}">${escapeHtml(item.value)}</div>
        </div>
      `).join('')}
    </div>
  `;
}

async function testTavilySettings(button) {
  setStatus('settings-tavily-status', '');
  clearSettingsProbe('tavily');
  try {
    await runWithBusyButton(button, {
      busyLabel: '测试中...',
      successLabel: '测试通过',
      errorLabel: '测试失败',
      minBusyMs: 640,
    }, async () => {
      const payload = await api('POST', '/api/settings/test/tavily', collectTavilySettingsForm());
      renderSettingsProbe('tavily', payload);
      setStatus('settings-tavily-status', renderSettingsProbeMessage('tavily', payload), !payload.ok);
      showToast(payload.ok ? 'Tavily 测试通过' : 'Tavily 测试失败', payload.ok ? 'success' : 'warn');
    });
  } catch (error) {
    clearSettingsProbe('tavily');
    setStatus('settings-tavily-status', `Tavily 测试失败：${error.message}`, true);
  }
}

async function testSocialSettings(button) {
  setStatus('settings-social-status', '');
  clearSettingsProbe('social');
  try {
    await runWithBusyButton(button, {
      busyLabel: '测试中...',
      successLabel: '测试通过',
      errorLabel: '测试失败',
      minBusyMs: 640,
    }, async () => {
      const payload = await api('POST', '/api/settings/test/social', collectSocialSettingsForm());
      renderSettingsProbe('social', payload);
      setStatus('settings-social-status', renderSettingsProbeMessage('social', payload), !payload.ok);
      showToast(payload.ok ? 'Social / X 测试通过' : 'Social / X 测试失败', payload.ok ? 'success' : 'warn');
    });
  } catch (error) {
    clearSettingsProbe('social');
    setStatus('settings-social-status', `Social / X 测试失败：${error.message}`, true);
  }
}

function headers() {
  const base = {
    'Content-Type': 'application/json',
  };
  if (PWD) {
    base['X-Admin-Password'] = PWD;
  }
  return base;
}

async function api(method, path, body) {
  const options = { method, headers: headers(), credentials: 'same-origin' };
  if (body !== undefined) {
    options.body = JSON.stringify(body);
  }

  const response = await fetch(API + path, options);
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = text ? { detail: text } : {};
  }

  if (response.status === 401) {
    logout();
    throw new Error('Unauthorized');
  }

  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }

  return payload;
}

function setStatus(id, message, isError = false) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!message) {
    el.textContent = '';
    el.classList.add('hidden');
    el.classList.remove('is-error');
    return;
  }
  el.textContent = message;
  el.classList.remove('hidden');
  el.classList.toggle('is-error', Boolean(isError));
}

function describeConfiguredSecret(masked, configured) {
  if (!configured) return '当前未配置。';
  return `当前已配置 ${masked || 'secret'}，留空表示保持不变。`;
}

function setTavilyMode(mode) {
  const nextMode = ['auto', 'pool', 'upstream'].includes(mode) ? mode : 'auto';
  const input = document.getElementById('settings-tavily-mode');
  if (input) {
    input.value = nextMode;
  }
  document.querySelectorAll('.mode-switch-btn[data-tavily-mode]').forEach((button) => {
    const active = button.dataset.tavilyMode === nextMode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-checked', active ? 'true' : 'false');
    button.setAttribute('tabindex', active ? '0' : '-1');
  });
  const tavily = latestSettings?.tavily || {};
  const runtimeMode = nextMode === 'auto'
    ? (tavily.effective_mode || (tavily.upstream_api_key_configured ? 'upstream' : ((tavily.local_key_count || 0) > 0 ? 'pool' : 'auto')))
    : nextMode;
  const runtimeSource = nextMode === 'auto'
    ? (tavily.mode_source || (tavily.upstream_api_key_configured ? 'auto_upstream' : ((tavily.local_key_count || 0) > 0 ? 'auto_pool' : 'auto_pending')))
    : (nextMode === 'upstream' ? 'manual_upstream' : 'manual_pool');
  const hint = document.getElementById('settings-tavily-mode-hint');
  if (hint) {
    if (nextMode === 'upstream') {
      hint.textContent = '手动固定到上游 Gateway，请求不再消耗本地 API Key 池。';
    } else if (nextMode === 'pool') {
      hint.textContent = '手动固定到 API Key 池，请求会从导入的 Tavily keys 中轮询。';
    } else {
      hint.textContent = '自动模式会先检测上游凭证；如果你只是导入 Tavily key，就会默认回到 API Key 池。';
    }
  }
  const runtimeStrip = document.getElementById('settings-tavily-runtime-strip');
  if (runtimeStrip) {
    runtimeStrip.textContent = `当前实际：${tavilyModeLabel(runtimeMode)} · ${tavilyModeSourceLabel(runtimeSource)}`;
  }
  document.querySelectorAll('[data-tavily-upstream-field]').forEach((field) => {
    field.classList.toggle('is-muted', nextMode === 'pool');
    field.classList.toggle('is-emphasis', nextMode !== 'pool');
  });
}

function setSocialMode(mode) {
  const nextMode = ['local', 'upstream'].includes(mode) ? mode : 'local';
  const input = document.getElementById('settings-social-mode');
  if (input) input.value = nextMode;
  document.querySelectorAll('.mode-switch-btn[data-social-mode]').forEach((button) => {
    const active = button.dataset.socialMode === nextMode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-checked', active ? 'true' : 'false');
    button.setAttribute('tabindex', active ? '0' : '-1');
  });
  document.querySelectorAll('[data-social-mode-field]').forEach((field) => {
    const modes = String(field.dataset.socialModeField || '').split(/\s+/).filter(Boolean);
    const visible = modes.includes(nextMode);
    field.classList.toggle('hidden', !visible);
    field.setAttribute('aria-hidden', visible ? 'false' : 'true');
  });
  const hint = document.getElementById('settings-social-mode-hint');
  if (hint) {
    hint.textContent = nextMode === 'local'
      ? '只使用本地 Social/X Key 池；上游 Admin 配置会保持保存，但不会被调用。'
      : '只使用上游连接凭证；本地 Social/X Key 池不会参与请求。';
  }
  const runtimeStrip = document.getElementById('settings-social-runtime-strip');
  if (runtimeStrip) {
    runtimeStrip.textContent = `当前选择：${socialModeLabel(nextMode)} · 保存后生效`;
  }
  const footerTitle = document.getElementById('settings-social-footer-title');
  const footerDetail = document.getElementById('settings-social-footer-detail');
  if (footerTitle) {
    footerTitle.textContent = nextMode === 'local'
      ? '本地模式只使用本地 Social/X Key 池。'
      : '上游模式只使用上游连接凭证。';
  }
  if (footerDetail) {
    footerDetail.textContent = nextMode === 'local'
      ? '上游 Admin 配置会保留，但不会被调用；保存后刷新本地池状态。'
      : '本地 Social/X Key 池不会参与请求；保存后刷新上游连接和管理统计。';
  }
}

function renderSocialKeySchedule(keys = [], mode = 'upstream', active = true) {
  const shellId = mode === 'local'
    ? 'settings-social-local-key-list'
    : 'settings-social-upstream-key-list';
  const shell = document.getElementById(shellId);
  if (!shell) return;
  shell.innerHTML = keys.map((key) => {
    const reason = String(key.disabled_reason || '').trim();
    const labels = {
      rate_limited: '限流冷却',
      quota_exhausted: '额度耗尽',
      auth_rejected: '凭证失效',
    };
    const status = key.schedulable
      ? (active ? '正常' : '已保存，当前未启用')
      : (labels[reason] || '不可调度');
    const recovery = key.schedule_until ? ` · ${formatTime(key.schedule_until)} 自动恢复` : '';
    const action = key.schedulable || !active
      ? ''
      : `<button class="btn btn-sm btn-soft" type="button" onclick="resumeSocialUpstreamKey('${escapeHtml(key.id)}', this)">立即恢复</button>`;
    return `
      <div class="settings-key-schedule-row">
        <div class="settings-key-schedule-copy">
          <strong>#${fmtNum(key.index)}</strong> · ${escapeHtml(key.key_masked || '')} · ${escapeHtml(status)}${escapeHtml(recovery)}
        </div>
        ${action}
      </div>
    `;
  }).join('');
}

function renderSocialUpstreamKeySchedule(keys = []) {
  renderSocialKeySchedule(keys, 'upstream', true);
}

function fillSettingsForm(settings) {
  const tavily = settings?.tavily || {};
  const social = settings?.social || {};
  setTavilyMode(tavily.mode || 'auto');
  document.getElementById('settings-tavily-upstream-base-url').value = tavily.upstream_base_url || '';
  document.getElementById('settings-tavily-upstream-search-path').value = tavily.upstream_search_path || '/search';
  document.getElementById('settings-tavily-upstream-extract-path').value = tavily.upstream_extract_path || '/extract';
  document.getElementById('settings-tavily-upstream-admin-base-url').value = tavily.upstream_admin_base_url || '';
  document.getElementById('settings-tavily-upstream-api-key').value = '';
  document.getElementById('settings-tavily-upstream-admin-headers').value = '';
  document.getElementById('settings-tavily-upstream-admin-cookie').value = '';
  document.getElementById('settings-tavily-upstream-api-key-hint').textContent =
    describeConfiguredSecret(tavily.upstream_api_key_masked, tavily.upstream_api_key_configured);
  document.getElementById('settings-tavily-upstream-admin-headers-hint').textContent =
    tavily.upstream_admin_headers_configured
      ? (tavily.upstream_admin_headers_summary || '当前已配置 admin headers，留空表示保持不变。')
      : '当前未配置 admin headers。';
  document.getElementById('settings-tavily-upstream-admin-cookie-hint').textContent =
    describeConfiguredSecret(tavily.upstream_admin_cookie_masked, tavily.upstream_admin_cookie_configured);
  document.getElementById('settings-tavily-meta').textContent = [
    `配置模式：${tavilyModeLabel(tavily.mode || 'auto')}`,
    `当前实际：${tavilyModeLabel(tavily.effective_mode || tavily.mode || 'auto')}`,
    `来源：${tavilyModeSourceLabel(tavily.mode_source || 'auto_pending')}`,
    tavily.upstream_base_url ? `Base URL：${tavily.upstream_base_url}` : '',
    tavily.upstream_api_key_configured ? '已配置上游凭证' : `本地活跃 Key ${fmtNum(tavily.local_key_count || 0)}`,
    tavily.upstream_admin_headers_configured || tavily.upstream_admin_cookie_configured ? '已配置上游 Admin 认证' : '当前只读 public summary',
  ].filter(Boolean).join(' · ');

  setSocialMode(social.mode || 'local');
  document.getElementById('settings-social-local-base-url').value = social.local_base_url || 'https://api.x.ai/v1';
  document.getElementById('settings-social-local-responses-path').value = social.local_responses_path || '/responses';
  document.getElementById('settings-social-local-api-key').value = '';
  document.getElementById('settings-social-upstream-base-url').value = social.upstream_base_url || '';
  document.getElementById('settings-social-upstream-responses-path').value = social.upstream_responses_path || '/responses';
  document.getElementById('settings-social-admin-base-url').value = social.admin_base_url || '';
  document.getElementById('settings-social-admin-username').value = social.admin_username || '';
  document.getElementById('settings-social-admin-verify-path').value = social.admin_verify_path || '/v1/admin/verify';
  document.getElementById('settings-social-admin-config-path').value = social.admin_config_path || '/admin/api/config';
  document.getElementById('settings-social-admin-tokens-path').value = social.admin_tokens_path || '/admin/api/tokens';
  document.getElementById('settings-social-model').value = social.model || 'grok-4.20-0309';
  document.getElementById('settings-social-fallback-model').value = social.fallback_model || 'grok-4.3';
  document.getElementById('settings-social-cache-ttl-seconds').value = String(social.cache_ttl_seconds || 60);
  document.getElementById('settings-social-fallback-min-results').value = String(social.fallback_min_results || 3);

  document.getElementById('settings-social-admin-app-key').value = '';
  document.getElementById('settings-social-admin-password').value = '';
  document.getElementById('settings-social-upstream-api-key').value = '';
  document.getElementById('settings-social-clear-local-api-key').checked = false;
  document.getElementById('settings-social-clear-upstream-api-key').checked = false;
  document.getElementById('settings-social-gateway-token').value = '';

  document.getElementById('settings-social-admin-app-key-hint').textContent =
    describeConfiguredSecret(social.admin_app_key_masked, social.admin_app_key_configured);
  document.getElementById('settings-social-admin-password-hint').textContent =
    describeConfiguredSecret(social.admin_password_masked, social.admin_password_configured);
  const socialLocalKeys = Array.isArray(social.local_keys) ? social.local_keys : [];
  const socialUpstreamKeys = Array.isArray(social.upstream_keys) ? social.upstream_keys : [];
  document.getElementById('settings-social-local-api-key-hint').textContent = social.local_api_key_configured
    ? `${describeConfiguredSecret(social.local_api_key_masked, true)} 已配置 ${fmtNum(socialLocalKeys.length)} 个本地 key。`
    : '支持逗号分隔多个本地 Social/X key；429 临时冷却，额度耗尽或失效后手工恢复。';
  const socialAvailableKeys = socialUpstreamKeys.filter((key) => key.schedulable).length;
  const socialUnavailableReasons = [...new Set(
    socialUpstreamKeys.map((key) => key.disabled_reason).filter(Boolean),
  )];
  renderSocialKeySchedule(socialLocalKeys, 'local', social.active_key_scope === 'local');
  renderSocialKeySchedule(socialUpstreamKeys, 'upstream', social.active_key_scope === 'upstream');
  document.getElementById('settings-social-upstream-api-key-hint').textContent = social.upstream_api_key_configured
    ? `${describeConfiguredSecret(social.upstream_api_key_masked, true)} 已配置 ${fmtNum(socialUpstreamKeys.length)} 个，当前可调度 ${fmtNum(socialAvailableKeys)} 个${socialUnavailableReasons.length ? `；隔离原因：${socialUnavailableReasons.join('、')}` : ''}。保存新 key 池会替换现有值并恢复调度。`
    : '上游模式可配置多个 client key；当前模式不是上游时，这些凭证不会参与请求。';
  document.getElementById('settings-social-gateway-token-hint').textContent =
    describeConfiguredSecret(social.gateway_token_masked, social.gateway_token_configured);

  const socialMode = social.mode || 'local';
  const footerTitle = document.getElementById('settings-social-footer-title');
  const footerDetail = document.getElementById('settings-social-footer-detail');
  if (footerTitle) {
    footerTitle.textContent = socialMode === 'local'
      ? '本地模式只使用本地 Social/X Key 池。'
      : '上游模式只使用上游连接凭证。';
  }
  if (footerDetail) {
    footerDetail.textContent = socialMode === 'local'
      ? '上游 Admin 配置会保留，但不会被调用；保存后刷新本地池状态。'
      : '本地 Social/X Key 池不会参与请求；保存后刷新上游连接和管理统计。';
  }

  const bits = [
    `当前模式：${socialModeLabel(socialMode)}`,
    social.model ? `主模型：${social.model}` : '',
    social.fallback_model ? `Fallback：${social.fallback_model} (< ${social.fallback_min_results || 3})` : '',
    social.token_source ? `Token 来源：${social.token_source}` : '',
    social.admin_connected ? `后台连通正常（${socialAdminVersionLabel(social.admin_api_version)}）` : '',
    social.admin_auth_mode === 'v3_credentials' ? `管理员：${social.admin_username || '未命名'}` : '',
  ].filter(Boolean);
  if (social.error) {
    bits.push(`最近错误：${social.error}`);
  }
  document.getElementById('settings-social-meta').textContent = bits.join(' · ');
  renderSettingsSummaries(settings);
}

async function loadSettings() {
  const payload = await api('GET', '/api/settings');
  latestSettings = payload || {};
  fillSettingsForm(latestSettings);
  setStatus('settings-password-status', '');
  setStatus('settings-tavily-status', '');
  setStatus('settings-social-status', '');
  clearSettingsProbe('tavily');
  clearSettingsProbe('social');
}

async function openSettingsModal() {
  rememberOverlayFocus('settings-modal');
  document.getElementById('settings-modal').classList.remove('hidden');
  setActiveSettingsTab(document.querySelector('.settings-tab.is-active')?.dataset.settingsTab || 'console');
  syncOverlayState();
  focusOverlay('settings-modal');
  try {
    await loadSettings();
  } catch (error) {
    showAlertDialog({
      title: '读取设置失败',
      message: `控制台没能读取当前配置：${error.message}`,
      tone: 'danger',
      kicker: 'Settings Error',
    });
  }
}

function closeSettingsModal() {
  document.getElementById('settings-modal').classList.add('hidden');
  syncOverlayState();
  restoreOverlayFocus('settings-modal');
}

function logoutFromSettings() {
  closeSettingsModal();
  logout();
}

function renderServiceShells() {
  const servicesRoot = document.getElementById('services-root');
  if (!servicesRoot) return;
  const providerHtml = Object.keys(SERVICE_META).map((service) => {
    const meta = SERVICE_META[service];
    return `
      <section class="service-panel card" data-service="${service}">
        <div class="service-head">
          <div class="service-head-copy">
            <span class="service-chip" data-service="${service}">${meta.label}</span>
            <h2>${meta.label} 工作台</h2>
            <p>${meta.panelIntro} 账号前缀 ${meta.emailPrefix}，代理 Token 前缀 ${meta.tokenPrefix}。${meta.quotaSource}。</p>
            <div class="service-head-route">
              <span class="service-head-route-label">Route</span>
              <span class="mono">${meta.routeHint}</span>
            </div>
          </div>
            <div class="service-tools">
              <div class="service-tool-kicker">Live Status</div>
              <div id="sync-meta-${service}" class="service-sync-meta" aria-live="polite">等待同步状态...</div>
            <button class="btn btn-soft" id="sync-btn-${service}" onclick="syncUsage('${service}', true, this)">${meta.syncButton}</button>
          </div>
        </div>
        <div class="workspace-tabs" role="tablist" aria-label="${meta.label} 工作台分区">
          <button type="button" class="workspace-tab is-active" id="workspace-${service}-tab-overview" role="tab" aria-selected="true" aria-controls="workspace-${service}-overview" tabindex="0" data-workspace-tab="overview" onclick="setWorkspaceTab('${service}', 'overview')">概览</button>
          <button type="button" class="workspace-tab" id="workspace-${service}-tab-tokens" role="tab" aria-selected="false" aria-controls="workspace-${service}-tokens" tabindex="-1" data-workspace-tab="tokens" onclick="setWorkspaceTab('${service}', 'tokens')">Token</button>
          <button type="button" class="workspace-tab" id="workspace-${service}-tab-keys" role="tab" aria-selected="false" aria-controls="workspace-${service}-keys" tabindex="-1" data-workspace-tab="keys" onclick="setWorkspaceTab('${service}', 'keys')">API Key</button>
        </div>
        <div class="service-body">
          <section class="workspace-tab-panel is-active" id="workspace-${service}-overview" role="tabpanel" aria-labelledby="workspace-${service}-tab-overview" aria-hidden="false" data-workspace-panel="overview">
            <div class="stats-grid" id="overview-${service}"></div>

            <div class="section-grid service-intake-grid">
              <div class="subcard api-shell">
              <h3>调用方式</h3>
              <p class="desc">${meta.routeHint}</p>
              <div class="inline-meta">
                <span class="inline-meta-base-url">Base URL: <b class="mono" id="base-url-${service}"></b></span>
                <span>代理 Token 前缀: <b class="mono">${meta.tokenPrefix}</b></span>
              </div>
              <div class="code-toolbar">
                <div class="endpoint">${meta.quotaSource}</div>
                <button class="btn btn-sm" type="button" onclick="copyCode('curl-example-${service}', this)">复制示例</button>
              </div>
              <pre class="code-block mono" id="curl-example-${service}"></pre>
            </div>

              <div class="subcard service-brief">
              <div class="service-brief-head">
                <h3>接线摘要</h3>
                <span class="service-brief-note">摘要优先</span>
              </div>
              <p class="desc">先在这里看当前 provider 的可用状态，再决定是否下钻到 Token、Key 和批量导入细节。</p>
              <div class="brief-list">
                <div class="brief-item">
                  <span>代理 Token 前缀</span>
                  <strong class="mono">${meta.tokenPrefix}</strong>
                </div>
                <div class="brief-item">
                  <span>账号前缀</span>
                  <strong class="mono">${meta.emailPrefix}</strong>
                </div>
                <div class="brief-item">
                  <span>额度来源</span>
                  <strong>${meta.quotaSource}</strong>
                </div>
                <div class="brief-item">
                  <span>控制面行为</span>
                  <strong>${meta.switcherFoot}</strong>
                </div>
              </div>
              </div>
            </div>
          </section>

          <section class="subcard detail-card detail-card-static workspace-tab-panel hidden" id="workspace-${service}-tokens" role="tabpanel" aria-labelledby="workspace-${service}-tab-tokens" aria-hidden="true" data-workspace-panel="tokens">
              <div class="detail-card-static-head">
                <div>
                  <h3>Token 池</h3>
                  <p>${meta.tokenPoolDesc}</p>
                </div>
                <span class="detail-pill">创建与分发</span>
              </div>
	              <div class="detail-body">
	                <div class="form-row">
		                  <input class="input-grow" type="text" id="token-name-${service}" aria-label="${meta.label} Token 备注" placeholder="Token 备注（可选）">
                  <button class="btn btn-primary" type="button" onclick="createToken('${service}', this)">创建 ${meta.label} Token</button>
	                </div>
                  <div class="detail-glance" id="token-glance-${service}"></div>
                <div class="table-tools">
                  <input
                      class="input-grow"
                      type="text"
                      id="token-search-${service}"
                      aria-label="搜索 ${meta.label} Token"
                      placeholder="搜索备注 / token / ID"
                      oninput="setTokenSearch('${service}', this.value)"
                    >
                    <div class="mini-switch" role="group" aria-label="${meta.label} Token 排序">
                      <button type="button" class="mini-switch-btn is-active" data-service="${service}" data-token-sort="risk" aria-pressed="true" onclick="setTokenSort('${service}', 'risk')">失败优先</button>
                      <button type="button" class="mini-switch-btn" data-service="${service}" data-token-sort="activity" aria-pressed="false" onclick="setTokenSort('${service}', 'activity')">活跃度</button>
                      <button type="button" class="mini-switch-btn" data-service="${service}" data-token-sort="today" aria-pressed="false" onclick="setTokenSort('${service}', 'today')">今日调用</button>
                      <button type="button" class="mini-switch-btn" data-service="${service}" data-token-sort="name" aria-pressed="false" onclick="setTokenSort('${service}', 'name')">名称</button>
                    </div>
                  </div>
                  <div class="detail-caption">表格只保留摘要。点击任一行，在右侧抽屉查看完整 token、配额策略和使用统计。</div>
                  ${renderTableLegend('token')}
	                <div class="table-wrap">
	                  <table>
	                    <thead>
	                      <tr>
	                        <th scope="col">Token</th>
	                        <th scope="col">备注</th>
	                        <th scope="col">运行摘要</th>
	                        <th scope="col">操作</th>
	                      </tr>
	                    </thead>
	                    <tbody id="tokens-body-${service}"></tbody>
	                  </table>
                </div>
              </div>
          </section>

          <section class="subcard detail-card detail-card-static workspace-tab-panel hidden" id="workspace-${service}-keys" role="tabpanel" aria-labelledby="workspace-${service}-tab-keys" aria-hidden="true" data-workspace-panel="keys">
              <div class="detail-card-static-head">
                <div>
                  <h3>API Key 池</h3>
                  <p>${meta.keyPoolDesc}</p>
                </div>
                <span class="detail-pill">导入与维护</span>
              </div>
	              <div class="detail-body">
	                <div class="form-row">
		                  <input class="input-grow mono" type="text" id="single-key-${service}" aria-label="${meta.label} API Key" placeholder="${meta.keyPlaceholder}">
		                  <button class="btn btn-primary" type="button" onclick="addSingleKey('${service}', this)">添加 ${meta.label} Key</button>
		                  <button class="btn btn-soft" type="button" aria-expanded="false" aria-controls="import-wrap-${service}" onclick="toggleImport('${service}', this)">批量导入</button>
		                </div>
		                  <div id="import-wrap-${service}" class="toggle-area hidden">
		                    <textarea id="import-text-${service}" aria-label="批量导入 ${meta.label} API Key" placeholder="${meta.importPlaceholder}"></textarea>
		                    <div class="form-row form-row-end">
		                      <button class="btn btn-primary" type="button" onclick="importKeys('${service}', this)">导入到 ${meta.label} 池</button>
	                    </div>
	                  </div>
                  <div class="detail-glance" id="key-glance-${service}"></div>
                  <div class="table-tools table-tools-stack">
                    <input
                      class="input-grow"
                      type="text"
                      id="key-search-${service}"
                      aria-label="搜索 ${meta.label} API Key"
                      placeholder="搜索邮箱 / key / ID"
                      oninput="setKeySearch('${service}', this.value)"
                    >
                    <div class="table-tools-row">
                      <div class="mini-switch" role="group" aria-label="${meta.label} API Key 筛选">
                        <button type="button" class="mini-switch-btn is-active" data-service="${service}" data-key-filter="all" aria-pressed="true" onclick="setKeyFilter('${service}', 'all')">全部</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-filter="issue" aria-pressed="false" onclick="setKeyFilter('${service}', 'issue')">待处理</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-filter="active" aria-pressed="false" onclick="setKeyFilter('${service}', 'active')">正常</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-filter="disabled" aria-pressed="false" onclick="setKeyFilter('${service}', 'disabled')">禁用</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-filter="error" aria-pressed="false" onclick="setKeyFilter('${service}', 'error')">异常</button>
                      </div>
                      <div class="mini-switch" role="group" aria-label="${meta.label} API Key 排序">
                        <button type="button" class="mini-switch-btn is-active" data-service="${service}" data-key-sort="risk" aria-pressed="true" onclick="setKeySort('${service}', 'risk')">异常优先</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-sort="recent" aria-pressed="false" onclick="setKeySort('${service}', 'recent')">最近使用</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-sort="usage" aria-pressed="false" onclick="setKeySort('${service}', 'usage')">成功调用</button>
                        <button type="button" class="mini-switch-btn" data-service="${service}" data-key-sort="quota" aria-pressed="false" onclick="setKeySort('${service}', 'quota')">低额度优先</button>
                      </div>
                    </div>
                  </div>
                  <div class="detail-caption">主表只保留同步状态和代理摘要。点击行可在右侧查看额度、账户层级信息和维护动作。</div>
                  ${renderTableLegend('key')}
	                <div class="table-wrap">
	                  <table>
	                    <thead>
	                      <tr>
	                        <th scope="col">ID</th>
	                        <th scope="col">Key</th>
	                        <th scope="col">邮箱</th>
	                        <th scope="col">同步 / 状态</th>
	                        <th scope="col">代理摘要</th>
	                        <th scope="col">状态</th>
	                        <th scope="col">操作</th>
	                      </tr>
	                    </thead>
                    <tbody id="keys-body-${service}"></tbody>
                  </table>
                </div>
                <div class="table-pagination" id="key-pagination-${service}" aria-live="polite"></div>
              </div>
          </section>
        </div>
      </section>
    `;
  }).join('');

  const socialHtml = `
    <section class="service-panel card" data-service="social">
      <div class="service-head">
        <div class="service-head-copy">
          <span class="service-chip" data-service="social">Social / X</span>
          <h2>Social / X 工作台</h2>
          <p>这里收口的是 X / Social 搜索路由，不再把底层实现名字放成主标题。你看到的是 MySearch 的 Social 工作台，底层可以复用 grok2api 后台，也可以兼容别的 xAI-compatible 上游。</p>
          <div class="service-head-route">
            <span class="service-head-route-label">Route</span>
            <span class="mono">代理端点: POST /social/search</span>
          </div>
        </div>
        <div class="service-tools">
          <div class="service-tool-kicker">Live Status</div>
          <div id="sync-meta-social" class="service-sync-meta" aria-live="polite">等待 Social 状态...</div>
          <div class="service-sync-meta">用于查看 token 池、剩余额度、调用次数和客户端接线方式。</div>
        </div>
      </div>
      <div class="workspace-tabs" role="tablist" aria-label="Social / X 工作台分区">
        <button type="button" class="workspace-tab is-active" id="workspace-social-tab-overview" role="tab" aria-selected="true" aria-controls="workspace-social-overview" tabindex="0" data-workspace-tab="overview" onclick="setWorkspaceTab('social', 'overview')">状态</button>
        <button type="button" class="workspace-tab" id="workspace-social-tab-integration" role="tab" aria-selected="false" aria-controls="workspace-social-integration" tabindex="-1" data-workspace-tab="integration" onclick="setWorkspaceTab('social', 'integration')">接线</button>
      </div>
      <div class="service-body">
        <section class="workspace-tab-panel is-active" id="workspace-social-overview" role="tabpanel" aria-labelledby="workspace-social-tab-overview" aria-hidden="false" data-workspace-panel="overview">
          <div class="stats-grid" id="overview-social"></div>
          <div class="subcard social-board" id="social-board"></div>
        </section>
        <section class="workspace-tab-panel hidden" id="workspace-social-integration" role="tabpanel" aria-labelledby="workspace-social-tab-integration" aria-hidden="true" data-workspace-panel="integration">
          <div class="subcard api-shell" id="social-integration"></div>
        </section>
      </div>
    </section>
  `;

  servicesRoot.innerHTML = providerHtml + socialHtml;
  renderSocialBoard({});
  renderSocialIntegration({});
  renderSocialWorkspace({});
  renderServiceSwitcher({}, {});
  applyActiveService();
}

function setWorkspaceTab(service, tabName) {
  const panel = document.querySelector(`.service-panel[data-service="${service}"]`);
  if (!panel) return;
  const target = panel.querySelector(`.workspace-tab-panel[data-workspace-panel="${tabName}"]`);
  if (!target) return;

  panel.querySelectorAll('.workspace-tab').forEach((button) => {
    const isActive = button.dataset.workspaceTab === tabName;
    button.classList.toggle('is-active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
    button.setAttribute('tabindex', isActive ? '0' : '-1');
  });
  panel.querySelectorAll('.workspace-tab-panel').forEach((item) => {
    const isActive = item.dataset.workspacePanel === tabName;
    item.classList.toggle('hidden', !isActive);
    item.classList.toggle('is-active', isActive);
    item.setAttribute('aria-hidden', isActive ? 'false' : 'true');
  });
}

function renderServiceSwitcher(services, social) {
  const root = document.getElementById('service-switcher');
  if (!root) return;
  const html = Object.entries(WORKSPACE_META).map(([service, meta]) => {
    const isSocial = service === 'social';
    const snapshot = getWorkspaceSnapshot(service, services, social);
    const signal = workspaceSignal(service, services, social);
    const metricOneLabel = snapshot.primaryMetricLabel || (isSocial ? '可用 Token' : '活跃 Key');
    const metricTwoLabel = isSocial ? '今日调用' : 'Token';
    const metricTwoValue = isSocial ? fmtNum(snapshot.todayCount) : fmtNum(snapshot.tokensCount);
    const footnote = snapshot.remaining !== null && snapshot.remaining !== undefined
      ? `${snapshot.remainingLabel} ${fmtNum(snapshot.remaining)}`
      : (meta.switcherFoot || (isSocial ? '统一 Social 路由' : '独立 Provider 池'));

    return `
      <button
        type="button"
        class="service-toggle ${activeService === service ? 'is-active' : ''}"
        data-service="${service}"
        aria-pressed="${activeService === service ? 'true' : 'false'}"
        aria-current="${activeService === service ? 'page' : 'false'}"
        onclick="setActiveService('${service}')"
      >
        <div class="service-toggle-top">
          <div class="service-toggle-title">
            <span class="service-toggle-mark" data-service="${service}" aria-hidden="true">${isSocial ? 'X' : meta.label.slice(0, 1)}</span>
            <div>
              <strong>${meta.label}</strong>
              <span>${escapeHtml(footnote)}</span>
            </div>
          </div>
          <div class="service-toggle-status is-${signal.tone}">
            <span class="service-toggle-signal"></span>
            <span>${signal.label}</span>
          </div>
        </div>
        <div class="service-toggle-meta">
          <span>${escapeHtml(metricOneLabel)} <strong>${fmtNum(snapshot.primaryMetricValue ?? snapshot.keysActive)}</strong></span>
          <span>${escapeHtml(metricTwoLabel)} <strong>${metricTwoValue}</strong></span>
        </div>
      </button>
    `;
  }).join('');

  root.innerHTML = html;
}

function applyActiveService() {
  if (!WORKSPACE_META[activeService]) {
    activeService = 'tavily';
  }

  for (const service of Object.keys(WORKSPACE_META)) {
    const panel = document.querySelector(`.service-panel[data-service="${service}"]`);
    if (!panel) continue;
    panel.classList.toggle('is-inactive', service !== activeService);
  }

  document.querySelectorAll('.service-toggle').forEach((item) => {
    const isActive = item.dataset.service === activeService;
    item.classList.toggle('is-active', isActive);
    item.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    item.setAttribute('aria-current', isActive ? 'page' : 'false');
    const status = item.querySelector('.service-toggle-status');
    if (status) {
      const label = status.querySelector('span:last-child');
      if (label) {
        const signal = workspaceSignal(item.dataset.service, latestServices, latestSocial);
        label.textContent = signal.label;
      }
    }
  });

  const switcherNote = document.getElementById('switcher-note');
  if (switcherNote) {
    switcherNote.textContent = `当前工作台：${WORKSPACE_META[activeService].label} · 已记住你的切换偏好`;
  }

  const activeToggle = document.querySelector(`.service-toggle[data-service="${activeService}"]`);
  if (activeToggle && window.innerWidth <= 1024) {
    const navigator = activeToggle.closest('.service-switcher');
    if (navigator) {
      const left = activeToggle.offsetLeft - ((navigator.clientWidth - activeToggle.offsetWidth) / 2);
      navigator.scrollTo({ left: Math.max(0, left), behavior: 'auto' });
    }
  }
}

function animateWorkspacePanel(service) {
  const main = document.querySelector('.services-root');
  const panel = document.querySelector(`.service-panel[data-service="${service}"]`);
  if (main) {
    main.classList.remove('is-switching');
    void main.offsetWidth;
    main.classList.add('is-switching');
    setTimeout(() => {
      main.classList.remove('is-switching');
    }, 320);
  }
  if (panel) {
    panel.classList.remove('is-activating');
    void panel.offsetWidth;
    panel.classList.add('is-activating');
    setTimeout(() => {
      panel.classList.remove('is-activating');
    }, 320);
  }
}

function setActiveService(service) {
  if (!WORKSPACE_META[service]) return;
  activeService = service;
  localStorage.setItem(ACTIVE_SERVICE_KEY, service);
  applyActiveService();
  animateWorkspacePanel(service);
  renderHeroFocus(latestServices, latestSocial);
  renderGlobalSummary(latestServices, latestSocial);
  renderSettingsSummaries();
  const panel = document.querySelector(`.service-panel[data-service="${service}"]`);
  if (panel) {
    const behavior = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
    panel.scrollIntoView({ behavior, block: 'start' });
  }
}

function doLogin(event) {
  event?.preventDefault?.();
  const input = document.getElementById('pwd-input');
  const password = input.value.trim();
  if (!password) {
    document.getElementById('login-err').textContent = '请输入管理密码。';
    document.getElementById('login-err').classList.remove('hidden');
    return;
  }
  setLoginBusy(true);
  loginWithPassword(password)
    .then(async () => {
      PWD = '';
      clearStoredPasswords();
      showDashboard({ animate: true });
      await refresh();
    })
    .catch((error) => {
      document.getElementById('login-err').textContent = error.message === 'Unauthorized'
        ? '密码错误。'
        : '登录失败，请检查管理 API 是否可用。';
      document.getElementById('login-err').classList.remove('hidden');
    })
    .finally(() => {
      setLoginBusy(false);
    });
}

function logout() {
  PWD = '';
  clearStoredPasswords();
  showLogin();
  closeSettingsModal();
  closeDetailDrawer();
  closeAppDialog(false);
  fetch(API + '/api/session/logout', {
    method: 'POST',
    credentials: 'same-origin',
  }).catch(() => {});
}

function fmtNum(value) {
  if (value === null || value === undefined || value === '') {
    return '--';
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString() : String(value);
}

// r5 P2-11: 在 stat-box .value 内的纯数字节点上做 rAF countUp 动画。
// 调用方在 innerHTML 渲染完成后 animateNumericChildren(containerEl) 即可。
// 受 prefers-reduced-motion 控制——开启时跳过动画直接显示终值。
function animateNumericChildren(rootEl, durationMs = 600) {
  if (!rootEl) return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return;
  }
  const valueEls = rootEl.querySelectorAll('.stat-box .value');
  valueEls.forEach((el) => {
    // 取 .value 直接 textNode 里的第一个数字（容忍后跟 <span class="muted">/ X</span>）
    const firstTextNode = Array.from(el.childNodes).find((n) => n.nodeType === Node.TEXT_NODE);
    if (!firstTextNode) return;
    const raw = firstTextNode.textContent.trim();
    const cleaned = raw.replace(/[,，]/g, '');
    const target = Number(cleaned);
    if (!Number.isFinite(target) || target <= 0) return;
    const start = performance.now();
    const initial = 0;
    function tick(now) {
      const elapsed = now - start;
      const progress = Math.min(1, elapsed / durationMs);
      const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
      const current = Math.round(initial + (target - initial) * eased);
      firstTextNode.textContent = current.toLocaleString() + ' ';
      if (progress < 1) requestAnimationFrame(tick);
      else firstTextNode.textContent = raw + ' ';
    }
    firstTextNode.textContent = '0 ';
    requestAnimationFrame(tick);
  });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatTime(iso) {
  if (!iso) return '未同步';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '未同步';
  return date.toLocaleString();
}

function quotaBar(used, limit) {
  const safeLimit = Number(limit || 0);
  const safeUsed = Number(used || 0);
  if (!safeLimit) return '';
  const pct = Math.min(100, (safeUsed / safeLimit) * 100);
  const cls = pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : '';
  return `
    <div class="quota-bar">
      <div class="quota-bar-fill ${cls}" style="width:${pct}%"></div>
    </div>
  `;
}

function buildCurlExample(service, tokenValue) {
  const baseUrl = location.origin;
  const token = tokenValue || 'YOUR_PROXY_TOKEN';
  if (service === 'firecrawl') {
    return `# Firecrawl Scrape
curl -X POST ${baseUrl}/firecrawl/v2/scrape \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${token}" \\
  -d '{"url":"https://example.com","formats":["markdown"]}'

# Firecrawl 额度查询
curl -X GET ${baseUrl}/firecrawl/v2/team/credit-usage \\
  -H "Authorization: Bearer ${token}"

# 也支持 body 里传 api_key
curl -X POST ${baseUrl}/firecrawl/v2/scrape \\
  -H "Content-Type: application/json" \\
  -d '{"api_key":"${token}","url":"https://example.com"}'`;
  }

  if (service === 'exa') {
    return `# Exa Search
curl -X POST ${baseUrl}/exa/search \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${token}" \\
  -d '{"query":"OpenAI latest model","numResults":3,"contents":{"text":true}}'

# 也支持 body 里传 api_key
curl -X POST ${baseUrl}/exa/search \\
  -H "Content-Type: application/json" \\
  -d '{"api_key":"${token}","query":"OpenAI latest model","numResults":3}'`;
  }

  return `# Tavily Search
curl -X POST ${baseUrl}/api/search \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${token}" \\
  -d '{"query":"hello world","max_results":1}'

# Tavily Extract
curl -X POST ${baseUrl}/api/extract \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${token}" \\
  -d '{"urls":["https://example.com"]}'

# 也支持 body 里传 api_key
curl -X POST ${baseUrl}/api/search \\
  -H "Content-Type: application/json" \\
  -d '{"api_key":"${token}","query":"hello world"}'`;
}

function renderGlobalSummary(services, social) {
  const root = document.getElementById('global-summary');
  if (!root) return;
  const list = Object.values(services || {});
  const todayCount = list.reduce((sum, item) => sum + Number(item.overview?.today_count || 0), 0);
  const monthCount = list.reduce((sum, item) => sum + Number(item.overview?.month_count || 0), 0);
  const activeSignal = workspaceSignal(activeService, services, social);
  const activeMeta = WORKSPACE_META[activeService] || {};
  const routeCards = getQuickstartProviderCards(services, social);
  const totalWorkspaces = routeCards.length;
  const connectedWorkspaces = routeCards.filter((card) => card.tone !== 'danger').length;
  const tavilyPayload = services?.tavily || {};
  const tavilyState = getTavilyRuntimeState(tavilyPayload);
  const tavilyUsesUpstream = tavilyState.effectiveMode === 'upstream';
  const socialUsesUpstream = isSocialUpstreamManaged(social || {});
  const localProviderTokenSources = [
    ...(!tavilyUsesUpstream ? [{ label: 'Tavily', count: Number((tavilyPayload.tokens || []).length) }] : []),
    { label: 'Exa', count: Number((services?.exa?.tokens || []).length) },
    { label: 'Firecrawl', count: Number((services?.firecrawl?.tokens || []).length) },
    ...(!socialUsesUpstream ? [{ label: 'Social / X', count: Number(getSocialUpstreamState(social || {}).acceptedTokenCount || 0) }] : []),
  ];
  const localProviderTokenTotal = localProviderTokenSources.reduce((sum, item) => sum + Number(item.count || 0), 0);
  const localProviderTokenLabels = localProviderTokenSources.map((item) => item.label);

  root.innerHTML = `
    <div class="summary-box summary-box-accent">
      <div class="label">当前工作台</div>
      <div class="value">${escapeHtml(activeMeta.label || '未知')}</div>
      <div class="hint">${escapeHtml(activeSignal.label)} · ${escapeHtml(activeSignal.snapshot.modeLabel)}</div>
    </div>
    <div class="summary-box">
      <div class="label">已接通工作台</div>
      <div class="value">${fmtNum(connectedWorkspaces)} <span class="muted">/ ${fmtNum(totalWorkspaces)}</span></div>
      <div class="hint">按当前接线状态自动统计全部工作台</div>
    </div>
    <div class="summary-box">
      <div class="label">Provider 代理 Token</div>
      <div class="value">${fmtNum(localProviderTokenTotal)}</div>
      <div class="hint">${localProviderTokenLabels.length ? `${escapeHtml(localProviderTokenLabels.join(' / '))} 当前走本地代理池` : '当前没有启用本地 provider 代理池'}</div>
    </div>
    <div class="summary-box">
      <div class="label">今日调用</div>
      <div class="value">${fmtNum(todayCount)}</div>
      <div class="hint">来自本地 usage_logs 聚合</div>
    </div>
    <div class="summary-box">
      <div class="label">本月调用</div>
      <div class="value">${fmtNum(monthCount)}</div>
      <div class="hint">来自本地 usage_logs 聚合，不含上游后台自己的历史请求总量</div>
    </div>
    <div class="summary-box">
      <div class="label">MySearch Token</div>
      <div class="value">${fmtNum(latestMySearch?.token_count || 0)}</div>
      <div class="hint">给 MCP / Skill / 客户端统一接入</div>
    </div>
  `;
}

function renderSyncMeta(service, payload) {
  if (service === 'exa') {
    const detail = payload.usage_sync?.detail || 'Exa 实时额度暂时无法查询';
    document.getElementById(`sync-meta-${service}`).textContent = [
      `已导入 ${fmtNum(payload.keys_total || 0)} 个 Key`,
      `已签发 ${fmtNum((payload.tokens || []).length)} 个 Token`,
      detail,
    ].join(' · ');
    return;
  }

  const quota = payload.real_quota || {};
  const usageSync = payload.usage_sync || {};
  const parts = [];
  const tavilyUpstream = service === 'tavily' ? getTavilyUpstreamSummary(payload) : null;

  if (service === 'tavily' && payload.routing?.effective_mode) {
    parts.push(`配置 ${tavilyModeLabel(payload.routing.mode || 'auto')}`);
    parts.push(`当前走 ${tavilyModeLabel(payload.routing.effective_mode)}`);
    parts.push(tavilyModeSourceLabel(payload.routing.mode_source || 'auto_pending'));
    if (payload.routing.effective_mode === 'upstream') {
      if (tavilyUpstream?.available) {
        parts.push(`上游活跃 ${fmtNum(tavilyUpstream.activeKeys)} / 总 ${fmtNum(tavilyUpstream.totalKeys)}`);
        parts.push(`上游剩余 ${fmtNum(tavilyUpstream.totalQuotaRemaining)}`);
        parts.push(tavilyUpstream.keyDetailSupported ? 'admin 明细已接通' : '当前为公共摘要');
      } else if (tavilyUpstream?.detail) {
        parts.push(tavilyUpstream.detail);
      }
    }
  }

  parts.push(`已同步 ${fmtNum(quota.synced_keys || 0)} / ${fmtNum(quota.total_keys || 0)} 个 Key`);
  if ((quota.key_level_count || 0) > 0) {
    parts.push(`Key 级额度 ${fmtNum(quota.key_level_count)}`);
  }
  if ((quota.account_fallback_count || 0) > 0) {
    parts.push(`账户正常数量：${fmtNum(quota.account_fallback_count)}`);
  }
  if (quota.last_synced_at) {
    parts.push(`最近同步 ${formatTime(quota.last_synced_at)}`);
  }
  if ((quota.error_keys || 0) > 0) {
    parts.push(`错误 ${fmtNum(quota.error_keys)}`);
  }
  if ((usageSync.synced || 0) > 0 || (usageSync.errors || 0) > 0) {
    parts.push(`本轮同步 ${fmtNum(usageSync.synced || 0)} 成功 / ${fmtNum(usageSync.errors || 0)} 失败`);
  }

  document.getElementById(`sync-meta-${service}`).textContent = parts.join(' · ');
}

function renderOverview(service, payload) {
  const overview = payload.overview || {};
  const quota = payload.real_quota || {};
  const tavilyUpstream = service === 'tavily' ? getTavilyUpstreamSummary(payload) : null;

  if (service === 'exa') {
    const todayCount = Number(overview.today_count || 0);
    const todaySuccess = Number(overview.today_success || 0);
    const successRate = todayCount ? `${Math.round((todaySuccess / todayCount) * 100)}%` : '暂无';

    const overviewEl = document.getElementById(`overview-${service}`);
    overviewEl.innerHTML = `
      <div class="stat-box">
        <div class="label">实时额度</div>
        <div class="value">暂时无法查询</div>
        <div class="hint">控制台当前只统计 Exa 代理调用</div>
      </div>
      <div class="stat-box">
        <div class="label">Key 池状态</div>
        <div class="value">${fmtNum(payload.keys_active || 0)} <span class="muted">/ ${fmtNum(payload.keys_total || 0)}</span></div>
        <div class="hint">活跃 / 总数</div>
      </div>
      <div class="stat-box">
        <div class="label">Token 池状态</div>
        <div class="value">${fmtNum((payload.tokens || []).length)}</div>
        <div class="hint">Exa 独立代理 Token 池</div>
      </div>
      <div class="stat-box">
        <div class="label">今日代理调用</div>
        <div class="value">${fmtNum(overview.today_count || 0)}</div>
        <div class="hint">成功 ${fmtNum(overview.today_success || 0)} / 失败 ${fmtNum(overview.today_failed || 0)}</div>
      </div>
      <div class="stat-box">
        <div class="label">本月代理调用</div>
        <div class="value">${fmtNum(overview.month_count || 0)}</div>
        <div class="hint">本月成功 ${fmtNum(overview.month_success || 0)}</div>
      </div>
      <div class="stat-box">
        <div class="label">今日成功率</div>
        <div class="value">${successRate}</div>
        <div class="hint">${payload.usage_sync?.detail || 'Exa 实时额度暂时无法查询，后续如果接入官方读取会补充显示。'}</div>
      </div>
    `;
    // r5 P2-11: 数字滚动微动效（reduced-motion 时跳过）
    animateNumericChildren(overviewEl);
    return;
  }

  if (service === 'tavily' && payload.routing?.effective_mode === 'upstream') {
    const upstreamAvailable = Boolean(tavilyUpstream?.available);
    const upstreamRemainStyle = upstreamAvailable && tavilyUpstream.totalQuotaLimit > 0
      ? (tavilyUpstream.totalQuotaRemaining / tavilyUpstream.totalQuotaLimit <= 0.1
        ? 'color: var(--danger)'
        : tavilyUpstream.totalQuotaRemaining / tavilyUpstream.totalQuotaLimit <= 0.3
          ? 'color: var(--warn)'
          : 'color: var(--ok)')
      : '';
    document.getElementById(`overview-${service}`).innerHTML = `
      <div class="stat-box">
        <div class="label">上游 Key 状态</div>
        <div class="value">${upstreamAvailable ? `${fmtNum(tavilyUpstream.activeKeys)} <span class="muted">/ ${fmtNum(tavilyUpstream.totalKeys)}</span>` : '未读取到'}</div>
        <div class="hint">${upstreamAvailable ? `耗尽 ${fmtNum(tavilyUpstream.exhaustedKeys)} · 隔离 ${fmtNum(tavilyUpstream.quarantinedKeys)} · ${escapeHtml(tavilyUpstream.sourceLabel || 'Hikari 公共摘要')}` : escapeHtml(tavilyUpstream?.detail || '当前上游没有提供公开摘要接口。')}</div>
      </div>
      <div class="stat-box">
        <div class="label">上游剩余额度</div>
        <div class="value" style="${upstreamRemainStyle}">${upstreamAvailable ? fmtNum(tavilyUpstream.totalQuotaRemaining) : '未读取到'}</div>
        <div class="hint">${upstreamAvailable ? `上限 ${fmtNum(tavilyUpstream.totalQuotaLimit)} · 来自 ${escapeHtml(tavilyUpstream.sourceLabel || 'Hikari 公共摘要')}` : '当前仍可继续使用本地池作为回退库存。'}</div>
      </div>
      <div class="stat-box">
        <div class="label">上游累计请求</div>
        <div class="value">${upstreamAvailable ? fmtNum(tavilyUpstream.totalRequests) : '未读取到'}</div>
        <div class="hint">${upstreamAvailable ? `成功 ${fmtNum(tavilyUpstream.successCount)} · 错误 ${fmtNum(tavilyUpstream.errorCount)} · 配额耗尽 ${fmtNum(tavilyUpstream.quotaExhaustedCount)}${tavilyUpstream.adminConfigured && !tavilyUpstream.adminConnected && tavilyUpstream.adminDetail ? ` · admin: ${escapeHtml(tavilyUpstream.adminDetail)}` : ''}` : '当前只确认了 Gateway 可转发，未拿到上游请求统计。'}</div>
      </div>
      <div class="stat-box">
        <div class="label">本地回退 Key</div>
        <div class="value">${fmtNum(payload.keys_active || 0)} <span class="muted">/ ${fmtNum(payload.keys_total || 0)}</span></div>
        <div class="hint">这些 Key 在 Tavily upstream 模式下不参与转发，只作为回退库存保留。</div>
      </div>
      <div class="stat-box">
        <div class="label">今日代理调用</div>
        <div class="value">${fmtNum(overview.today_count || 0)}</div>
        <div class="hint">成功 ${fmtNum(overview.today_success || 0)} / 失败 ${fmtNum(overview.today_failed || 0)}</div>
      </div>
      <div class="stat-box">
        <div class="label">工作模式</div>
        <div class="value">${escapeHtml(tavilyModeLabel(payload.routing?.effective_mode || 'upstream'))}</div>
        <div class="hint">${escapeHtml(tavilyModeSourceLabel(payload.routing?.mode_source || 'auto_pending'))}</div>
      </div>
    `;
    return;
  }

  const totalLimit = Number(quota.total_limit || 0);
  const totalUsed = Number(quota.total_used || 0);
  const totalRemaining = Number(quota.total_remaining || 0);
  const remainStyle = totalLimit && totalUsed / totalLimit >= 0.9
    ? 'color: var(--danger)'
    : totalLimit && totalUsed / totalLimit >= 0.7
      ? 'color: var(--warn)'
      : 'color: var(--ok)';

  document.getElementById(`overview-${service}`).innerHTML = `
    <div class="stat-box">
      <div class="label">真实总额度</div>
      <div class="value">${fmtNum(totalLimit)}</div>
      <div class="hint">${SERVICE_META[service].quotaSource}</div>
    </div>
    <div class="stat-box">
      <div class="label">真实已用</div>
      <div class="value">${fmtNum(totalUsed)}</div>
      <div class="hint">按已同步 Key 汇总</div>
    </div>
    <div class="stat-box">
      <div class="label">真实剩余</div>
      <div class="value" style="${remainStyle}">${fmtNum(totalRemaining)}</div>
      <div class="hint">${quotaBar(totalUsed, totalLimit) || '尚未获得完整上限信息'}</div>
    </div>
    <div class="stat-box">
      <div class="label">Key 池状态</div>
      <div class="value">${fmtNum(payload.keys_active || 0)} <span class="muted">/ ${fmtNum(payload.keys_total || 0)}</span></div>
      <div class="hint">活跃 / 总数</div>
    </div>
    <div class="stat-box">
      <div class="label">今日代理调用</div>
      <div class="value">${fmtNum(overview.today_count || 0)}</div>
      <div class="hint">成功 ${fmtNum(overview.today_success || 0)} / 失败 ${fmtNum(overview.today_failed || 0)}</div>
    </div>
    <div class="stat-box">
      <div class="label">本月代理调用</div>
      <div class="value">${fmtNum(overview.month_count || 0)}</div>
      <div class="hint">成功 ${fmtNum(overview.month_success || 0)}</div>
    </div>
  `;
}

function renderSocialWorkspace(social) {
  const stats = social?.stats || {};
  const isV3Admin = social?.admin_api_version === 'v3' || stats.schema === 'grok2api_v3_accounts';
  const socialState = getSocialUpstreamState(social || {});
  const mode = socialModeLabel(social?.mode || 'local');
  const source = socialTokenSourceLabel(social?.token_source || '');
  const keyScopeLabel = social?.mode === 'local' ? '本地 key' : '上游 key';
  const syncLine = socialState.level === 'full'
    ? (isV3Admin ? [
      mode,
      `账号 ${fmtNum(stats.account_available || 0)} / ${fmtNum(stats.account_total || 0)}`,
      `24h 请求 ${fmtNum(stats.requests_24h || 0)}`,
      `历史请求 ${fmtNum(stats.total_calls || 0)}`,
    ] : [
      mode,
      `Token ${fmtNum(stats.token_total || 0)}`,
      `Chat ${fmtNum(stats.chat_remaining || 0)}`,
      `总调用 ${fmtNum(stats.total_calls || 0)}`,
    ]).join(' · ')
    : [
      mode,
      `${keyScopeLabel} ${fmtNum(socialState.upstreamAvailableKeyCount)} 可调度 / ${fmtNum(socialState.upstreamApiKeyCount)} 总数`,
      `客户端 token ${fmtNum(socialState.acceptedTokenCount)}`,
      socialState.canProxySearch ? '已可转发搜索' : '待补鉴权',
    ].join(' · ');

  const syncMeta = document.getElementById('sync-meta-social');
  if (syncMeta) {
    syncMeta.textContent = social?.error ? `${syncLine} · 最近错误 ${social.error}` : syncLine;
  }

  if (socialState.level !== 'full') {
    document.getElementById('overview-social').innerHTML = `
      <div class="stat-box">
        <div class="label">工作模式</div>
        <div class="value">${escapeHtml(mode)}</div>
        <div class="hint">当前 Social / X 工作台的路由状态</div>
      </div>
      <div class="stat-box">
        <div class="label">可调度 ${social?.mode === 'local' ? '本地 Key' : '上游 Key'}</div>
        <div class="value">${fmtNum(socialState.upstreamAvailableKeyCount)} <span class="muted">/ ${fmtNum(socialState.upstreamApiKeyCount)}</span></div>
        <div class="hint">隔离 ${fmtNum(socialState.upstreamUnavailableKeyCount)}；429 自动冷却，额度或鉴权失败需手工替换</div>
      </div>
      <div class="stat-box">
        <div class="label">客户端 Token 数</div>
        <div class="value">${fmtNum(socialState.acceptedTokenCount)}</div>
        <div class="hint">可被 /social/search 接受的客户端 token 数量</div>
      </div>
      <div class="stat-box">
        <div class="label">Token 来源</div>
        <div class="value">${escapeHtml(source)}</div>
        <div class="hint">${escapeHtml(socialState.detail || '当前只有基础接线可视化，完整 token 统计需要后台 tokens 面板。')}</div>
      </div>
    `;
    return;
  }

  if (isV3Admin) {
    document.getElementById('overview-social').innerHTML = `
      <div class="stat-box">
        <div class="label">工作模式</div>
        <div class="value">${escapeHtml(mode)}</div>
        <div class="hint">v3 管理统计与 g2a_ 推理凭据相互独立</div>
      </div>
      <div class="stat-box">
        <div class="label">账号可用 / 总数</div>
        <div class="value">${fmtNum(stats.account_available || 0)} <span class="muted">/ ${fmtNum(stats.account_total || 0)}</span></div>
        <div class="hint">恢复中 ${fmtNum(stats.account_recovering || 0)} · 需处理 ${fmtNum(stats.account_attention || 0)}</div>
      </div>
      <div class="stat-box">
        <div class="label">24h 请求</div>
        <div class="value">${fmtNum(stats.requests_24h || 0)}</div>
        <div class="hint">成功 ${fmtNum(stats.successful_requests_24h || 0)} · 失败 ${fmtNum(stats.failed_requests_24h || 0)}</div>
      </div>
      <div class="stat-box">
        <div class="label">鉴权分工</div>
        <div class="value">v3 管理员会话</div>
        <div class="hint">推理仍使用 ${escapeHtml(source)}</div>
      </div>
    `;
    return;
  }

  document.getElementById('overview-social').innerHTML = `
    <div class="stat-box">
      <div class="label">工作模式</div>
      <div class="value">${escapeHtml(mode)}</div>
      <div class="hint">当前 Social / X 工作台的路由状态</div>
    </div>
    <div class="stat-box">
      <div class="label">Token 正常 / 总数</div>
      <div class="value">${fmtNum(stats.token_normal || 0)} <span class="muted">/ ${fmtNum(stats.token_total || 0)}</span></div>
      <div class="hint">兼容上游 token 池实时汇总</div>
    </div>
    <div class="stat-box">
      <div class="label">Chat 剩余</div>
      <div class="value">${fmtNum(stats.chat_remaining || 0)}</div>
      <div class="hint">Image ${fmtNum(stats.image_remaining || 0)} · Video ${stats.video_remaining === null ? '无法统计' : fmtNum(stats.video_remaining)}</div>
    </div>
    <div class="stat-box">
      <div class="label">Token 来源</div>
      <div class="value">${escapeHtml(source)}</div>
      <div class="hint">${social?.mode === 'local' ? '当前只使用本地 Key 池' : (social?.admin_connected ? '当前已接入上游管理后台' : '当前使用上游 client key')}</div>
    </div>
  `;
}

function renderApiExample(service) {
  document.getElementById(`base-url-${service}`).textContent = location.origin;
  document.getElementById(`curl-example-${service}`).textContent = buildCurlExample(service, 'YOUR_PROXY_TOKEN');
}

function renderTokenQuota(token) {
  return '<div class="table-note"><b style="color: var(--ok)">无限制</b><br><span class="muted">已关闭小时 / 日 / 月限流</span></div>';
}

function renderGlanceCard(label, value, hint = '') {
  return `
    <div class="glance-card">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
      <div class="hint">${escapeHtml(hint)}</div>
    </div>
  `;
}

function renderTableLegend(kind = 'token') {
  const items = kind === 'key'
    ? [
      ['danger', '同步异常'],
      ['warn', '额度偏低 / 失败偏多'],
      ['busy', '调用偏高'],
      ['off', '已停用'],
    ]
    : [
      ['danger', '失败偏多'],
      ['warn', '近期有失败'],
      ['busy', '调用偏高'],
    ];
  return `
    <div class="table-legend" aria-label="row state legend">
      ${items.map(([tone, label]) => `
        <span class="legend-chip is-${tone}">
          <span class="legend-dot"></span>
          <span>${escapeHtml(label)}</span>
        </span>
      `).join('')}
    </div>
  `;
}

function renderDrawerActionGroup(title, body, tone = 'neutral') {
  return `
    <div class="drawer-action-group ${tone === 'danger' ? 'is-danger' : ''}">
      <div class="drawer-action-kicker">${escapeHtml(title)}</div>
      <div class="drawer-action-row">${body}</div>
    </div>
  `;
}

function renderPoolGlance(service, payload = {}) {
  const tokenRoot = document.getElementById(`token-glance-${service}`);
  const tokens = payload?.tokens || [];
  if (tokenRoot) {
    const tokenStats = tokens.reduce((acc, token) => {
      const stats = token.stats || {};
      acc.today += Number(stats.today_success || 0) + Number(stats.today_failed || 0);
      acc.month += Number(stats.month_success || 0) + Number(stats.month_failed || 0);
      acc.hour += Number(stats.hour_count || 0);
      return acc;
    }, { today: 0, month: 0, hour: 0 });
    tokenRoot.innerHTML = [
      renderGlanceCard('Token 总数', fmtNum(tokens.length), `${getServiceDisplayLabel(service)} 当前可发放的访问凭证`),
      renderGlanceCard('今日调用', fmtNum(tokenStats.today), `小时 ${fmtNum(tokenStats.hour)} · 本月 ${fmtNum(tokenStats.month)}`),
      renderGlanceCard('默认策略', '无限制', '当前保持开放限流策略'),
    ].join('');
  }

  const keyRoot = document.getElementById(`key-glance-${service}`);
  if (keyRoot) {
    const keys = payload?.keys || [];
    const activeKeys = keys.filter((key) => getKeyAvailability(key).schedulable).length;
    const limitedKeys = keys.filter((key) => !getKeyAvailability(key).schedulable).length;
    const syncedKeys = keys.filter((key) => Boolean(key.usage_synced_at)).length;
    const erroredKeys = keys.filter((key) => Boolean(key.usage_sync_error)).length;
    keyRoot.innerHTML = [
      renderGlanceCard('可调度 Key', fmtNum(activeKeys), `隔离 ${fmtNum(limitedKeys)} · 总数 ${fmtNum(keys.length)}`),
      renderGlanceCard('已同步', fmtNum(syncedKeys), '已有官方或账户级额度信息'),
      renderGlanceCard('同步异常', fmtNum(erroredKeys), erroredKeys ? '建议点击行检查失败原因' : '当前没有同步异常'),
    ].join('');
  }
}

function renderTokenSummary(token) {
  const stats = token.stats || {};
  return `
    <div class="table-note">今日 ${fmtNum(Number(stats.today_success || 0) + Number(stats.today_failed || 0))}</div>
    <div class="table-note">本月 ${fmtNum(Number(stats.month_success || 0) + Number(stats.month_failed || 0))}</div>
    <div class="table-note muted">小时 ${fmtNum(stats.hour_count || 0)}</div>
  `;
}

function renderKeyStatusSummary(service, key) {
  const availability = getKeyAvailability(key);
  const remain = key.usage_key_remaining ?? key.usage_account_remaining;
  const remainLabel = remain === null || remain === undefined ? '剩余待同步' : `剩余 ${fmtNum(remain)}`;
  return `
    <div class="table-note"><span class="tag ${availability.schedulable ? 'tag-ok' : 'tag-off'}">${escapeHtml(availability.label)}</span></div>
    <div class="table-note muted">${escapeHtml(availability.detail)}</div>
    <div class="table-note">${remainLabel}</div>
    <div class="table-note muted">${key.usage_synced_at ? `同步 ${formatTime(key.usage_synced_at)}` : (service === 'exa' ? '实时额度暂不可查' : '尚未同步')}</div>
  `;
}

function renderKeyUsageSummary(key) {
  return `
    <div class="table-note">成功 ${fmtNum(key.total_used || 0)}</div>
    <div class="table-note">失败 ${fmtNum(key.total_failed || 0)}</div>
    <div class="table-note muted">最近 ${formatTime(key.last_used_at)}</div>
  `;
}

function syncTokenToolbar(service) {
  const state = getTokenTableState(service);
  const search = document.getElementById(`token-search-${service}`);
  if (search && search.value !== state.search) {
    search.value = state.search;
  }
  document.querySelectorAll(`[data-token-sort][data-service="${service}"]`).forEach((button) => {
    const active = button.dataset.tokenSort === state.sort;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
    button.setAttribute('tabindex', active ? '0' : '-1');
  });
}

function syncKeyToolbar(service) {
  const state = getKeyTableState(service);
  const search = document.getElementById(`key-search-${service}`);
  if (search && search.value !== state.search) {
    search.value = state.search;
  }
  document.querySelectorAll(`[data-key-filter][data-service="${service}"]`).forEach((button) => {
    const active = button.dataset.keyFilter === state.filter;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
    button.setAttribute('tabindex', active ? '0' : '-1');
  });
  document.querySelectorAll(`[data-key-sort][data-service="${service}"]`).forEach((button) => {
    const active = button.dataset.keySort === state.sort;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
    button.setAttribute('tabindex', active ? '0' : '-1');
  });
}

function setTokenSearch(service, value) {
  getTokenTableState(service).search = value || '';
  renderTokens(service, getServicePayload(service).tokens || []);
}

function setTokenSort(service, value) {
  getTokenTableState(service).sort = value || 'risk';
  renderTokens(service, getServicePayload(service).tokens || []);
}

function setKeySearch(service, value) {
  const state = getKeyTableState(service);
  state.search = value || '';
  state.page = 1;
  renderKeys(service, getServicePayload(service).keys || []);
}

function setKeyFilter(service, value) {
  const state = getKeyTableState(service);
  state.filter = value || 'all';
  state.page = 1;
  renderKeys(service, getServicePayload(service).keys || []);
}

function setKeySort(service, value) {
  const state = getKeyTableState(service);
  state.sort = value || 'risk';
  state.page = 1;
  renderKeys(service, getServicePayload(service).keys || []);
}

function setKeyPage(service, page) {
  const state = getKeyTableState(service);
  state.page = Math.max(1, Number(page) || 1);
  renderKeys(service, getServicePayload(service).keys || []);
  document.getElementById(`key-search-${service}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderTokens(service, tokens) {
  const tbody = document.getElementById(`tokens-body-${service}`);
  syncTokenToolbar(service);
  if (!tokens || tokens.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">
      <div class="empty-state-icon" aria-hidden="true">🪙</div>
      <strong>还没有 Token</strong>
      <p>创建一个 Token，下游 MCP / Agent 就能用它接入当前 service。</p>
      <button type="button" class="btn btn-soft btn-sm empty-state-cta" onclick="document.getElementById('token-name-${service}')?.focus()">去顶部创建 Token</button>
    </div></td></tr>`;
    return;
  }

  const filtered = getFilteredTokens(service, tokens);
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">
      <div class="empty-state-icon" aria-hidden="true">🔍</div>
      <strong>没有符合当前筛选条件的 Token</strong>
      <p>调整上方"显示禁用"或筛选词，或者新建一个再试。</p>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((token) => {
    const rowClass = getTokenRowClass(token);
    return `
      <tr
        class="table-row-clickable ${rowClass}"
        tabindex="0"
        role="button"
        aria-label="查看 ${escapeHtml(getServiceDisplayLabel(service))} token ${escapeHtml(token.name || String(token.id))} 详情"
        onclick="openTokenDetail('${service}', ${token.id})"
        onkeydown="handleTableRowKey(event, 'token', '${service}', ${token.id})"
      >
        <td class="mono" data-label="Token">
          ${maskToken(token.token)}
          <div class="row-meta">点击查看详情</div>
        </td>
        <td data-label="名称">${escapeHtml(token.name || '-')}</td>
        <td data-label="运行摘要">${renderTokenSummary(token)}</td>
        <td data-label="操作">
          <div class="table-actions">
            <!-- r5 P0-2: 删除按钮已挪到抽屉的 Danger Zone（点击行展开），避免误删。 -->
            <button class="btn btn-sm" onclick="event.stopPropagation(); copyTokenById('${service}', ${token.id}, this)">复制</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

function renderKeyQuota(service, key) {
  if (service === 'exa') {
    return `
      <div class="table-note muted">Exa 实时额度暂时无法查询。</div>
      <div class="table-note muted">当前只展示代理层调用统计。</div>
      ${key.usage_sync_error ? `<div class="table-note" style="color: var(--warn); margin-top: 6px">最近错误: ${escapeHtml(key.usage_sync_error)}</div>` : ''}
    `;
  }

  if (key.usage_key_limit !== null && key.usage_key_used !== null) {
    const remain = key.usage_key_remaining ?? Math.max(0, key.usage_key_limit - key.usage_key_used);
    return `
      <div class="table-note">已用 ${fmtNum(key.usage_key_used)} / ${fmtNum(key.usage_key_limit)}</div>
      <div class="table-note"><b style="color:${remain > 0 ? 'var(--ok)' : 'var(--danger)'}">剩余 ${fmtNum(remain)}</b></div>
      ${quotaBar(key.usage_key_used, key.usage_key_limit)}
      <div class="table-note muted" style="margin-top:6px">同步 ${formatTime(key.usage_synced_at)}</div>
      ${key.usage_sync_error ? `<div class="table-note" style="color: var(--warn); margin-top: 6px">最近错误: ${escapeHtml(key.usage_sync_error)}</div>` : ''}
    `;
  }

  if (service === 'firecrawl' && key.usage_synced_at) {
    return `
      <div class="table-note muted">Firecrawl 当前主要返回账户级 credits。</div>
      <div class="table-note muted">单 Key 独立限额请看右侧账户额度。</div>
      ${key.usage_sync_error ? `<div class="table-note" style="color: var(--warn); margin-top: 6px">最近错误: ${escapeHtml(key.usage_sync_error)}</div>` : ''}
    `;
  }

  if (key.usage_sync_error) {
    return `<div class="table-note" style="color: var(--warn)">同步失败：${escapeHtml(key.usage_sync_error)}</div>`;
  }

  return '<span class="muted">未同步</span>';
}

function renderAccountQuota(service, key) {
  if (service === 'exa') {
    return '<span class="muted">Exa 账户实时额度暂时无法查询</span>';
  }

  if (key.usage_account_limit !== null && key.usage_account_used !== null) {
    const remain = key.usage_account_remaining ?? Math.max(0, key.usage_account_limit - key.usage_account_used);
    const plan = key.usage_account_plan || (service === 'firecrawl' ? 'Firecrawl Credits' : '未知计划');
    return `
      <div class="table-note">${escapeHtml(plan)}</div>
      <div class="table-note">已用 ${fmtNum(key.usage_account_used)} / ${fmtNum(key.usage_account_limit)}</div>
      <div class="table-note"><b style="color:${remain > 0 ? 'var(--ok)' : 'var(--danger)'}">剩余 ${fmtNum(remain)}</b></div>
      ${quotaBar(key.usage_account_used, key.usage_account_limit)}
    `;
  }
  return '<span class="muted">未返回</span>';
}

function renderKeys(service, keys) {
  const tbody = document.getElementById(`keys-body-${service}`);
  const pagination = document.getElementById(`key-pagination-${service}`);
  syncKeyToolbar(service);
  if (!keys || keys.length === 0) {
    if (pagination) pagination.innerHTML = '';
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">
      <div class="empty-state-icon" aria-hidden="true">🔑</div>
      <strong>当前 service 还没有导入 Key</strong>
      <p>在上方输入框单条或批量导入上游 key，proxy 会自动做 round-robin。</p>
    </div></td></tr>`;
    return;
  }

  const filtered = getFilteredKeys(service, keys);
  if (!filtered.length) {
    if (pagination) pagination.innerHTML = '';
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">
      <div class="empty-state-icon" aria-hidden="true">🔍</div>
      <strong>没有符合当前筛选条件的 Key</strong>
      <p>调整筛选词或切换"显示禁用"再试。</p>
    </div></td></tr>`;
    return;
  }

  const state = getKeyTableState(service);
  const pageSize = getKeyPageSize();
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  state.page = Math.min(Math.max(1, state.page || 1), pageCount);
  const pageStart = (state.page - 1) * pageSize;
  const visibleKeys = filtered.slice(pageStart, pageStart + pageSize);

  tbody.innerHTML = visibleKeys.map((key) => {
    const availability = getKeyAvailability(key);
    const rowClass = getKeyRowClass(service, key);
    return `
      <tr
        class="table-row-clickable ${rowClass}"
        tabindex="0"
        role="button"
        aria-label="查看 ${escapeHtml(getServiceDisplayLabel(service))} key ${escapeHtml(key.email || String(key.id))} 详情"
        onclick="openKeyDetail('${service}', ${key.id})"
        onkeydown="handleTableRowKey(event, 'key', '${service}', ${key.id})"
      >
        <td data-label="ID">${fmtNum(key.id)}</td>
        <td class="mono" data-label="Key">
          ${escapeHtml(key.key_masked || maskToken(key.key))}
          <div class="row-meta">点击查看详情</div>
        </td>
        <td data-label="邮箱">${escapeHtml(key.email || '-')}</td>
        <td data-label="同步 / 状态">${renderKeyStatusSummary(service, key)}</td>
        <td data-label="代理摘要">${renderKeyUsageSummary(key)}</td>
        <td data-label="状态"><span class="tag ${availability.schedulable ? 'tag-ok' : 'tag-off'}">${escapeHtml(availability.label)}</span></td>
        <td data-label="操作">
          <div class="table-actions">
            <!-- r5 P0-2: 删除按钮已挪到抽屉的 Danger Zone，行内只保留启用/禁用。 -->
            <button class="btn btn-sm" onclick="event.stopPropagation(); toggleKey('${service}', ${key.id}, ${availability.schedulable ? 0 : 1})">${escapeHtml(availability.actionLabel)}</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');

  if (pagination) {
    pagination.innerHTML = `
      <span class="table-pagination-count">第 ${fmtNum(state.page)} / ${fmtNum(pageCount)} 页 · 共 ${fmtNum(filtered.length)} 个 Key</span>
      <div class="table-pagination-actions">
        <button class="btn btn-soft btn-sm" type="button" ${state.page <= 1 ? 'disabled' : ''} onclick="setKeyPage('${service}', ${state.page - 1})">上一页</button>
        <button class="btn btn-soft btn-sm" type="button" ${state.page >= pageCount ? 'disabled' : ''} onclick="setKeyPage('${service}', ${state.page + 1})">下一页</button>
      </div>
    `;
  }
}

let lastKeyPageSize = getKeyPageSize();
window.addEventListener('resize', () => {
  const nextPageSize = getKeyPageSize();
  if (nextPageSize === lastKeyPageSize) return;
  lastKeyPageSize = nextPageSize;
  Object.keys(tableControls.keys).forEach((service) => {
    const tbody = document.getElementById(`keys-body-${service}`);
    if (!tbody) return;
    getKeyTableState(service).page = 1;
    renderKeys(service, getServicePayload(service).keys || []);
  });
});

function openTokenDetail(service, tokenId) {
  const payload = getServicePayload(service);
  const token = (payload?.tokens || []).find((item) => Number(item.id) === Number(tokenId));
  if (!token) {
    showToast('没有找到这个 token 的最新数据。', 'warn');
    return;
  }
  const stats = token.stats || {};
  const label = getServiceDisplayLabel(service);
  openDetailDrawer({
    kicker: `${label} Token`,
    title: token.name || `${label} Token #${token.id}`,
    subtitle: `ID ${fmtNum(token.id)} · 给客户端分发的统一访问凭证`,
    tone: service === 'mysearch' ? 'info' : 'ok',
    summaryHtml: [
      drawerMetric('今日成功', fmtNum(stats.today_success || 0), `失败 ${fmtNum(stats.today_failed || 0)}`),
      drawerMetric('本月成功', fmtNum(stats.month_success || 0), `失败 ${fmtNum(stats.month_failed || 0)}`),
      drawerMetric('小时调用', fmtNum(stats.hour_count || 0), '当前 token 的近一小时请求量'),
    ].join(''),
    bodyHtml: [
      drawerSection('访问凭据', `
        <div class="secret-preview">
          <code class="mono">${escapeHtml(maskToken(token.token))}</code>
          <span>完整值默认隐藏。需要使用时，通过下方维护动作直接复制。</span>
        </div>
      `),
      drawerSection('配额策略', renderTokenQuota(token)),
      drawerSection('代理统计', `
        <div class="drawer-grid drawer-grid-compact">
          <div class="drawer-inline-card"><span>今日总调用</span><strong>${fmtNum(Number(stats.today_success || 0) + Number(stats.today_failed || 0))}</strong></div>
          <div class="drawer-inline-card"><span>本月总调用</span><strong>${fmtNum(Number(stats.month_success || 0) + Number(stats.month_failed || 0))}</strong></div>
        </div>
      `),
    ].join(''),
    actionsHtml: [
      renderDrawerActionGroup('维护动作', `
        <button class="btn btn-soft" type="button" onclick="copyTokenById('${service}', ${token.id}, this)">复制 Token</button>
      `),
      renderDrawerActionGroup('危险动作', `
        <button class="btn btn-danger" type="button" onclick="closeDetailDrawer(); delToken('${service}', ${token.id})">删除 Token</button>
      `, 'danger'),
    ].join(''),
  });
}

function openKeyDetail(service, keyId) {
  const payload = getServicePayload(service);
  const key = (payload?.keys || []).find((item) => Number(item.id) === Number(keyId));
  if (!key) {
    showToast('没有找到这个 Key 的最新数据。', 'warn');
    return;
  }
  const availability = getKeyAvailability(key);
  const label = getServiceDisplayLabel(service);
  openDetailDrawer({
    kicker: `${label} Key`,
    title: key.email || `${label} Key #${key.id}`,
    subtitle: `${escapeHtml(key.key_masked || maskToken(key.key))} · ${escapeHtml(availability.label)}`,
    tone: availability.schedulable ? 'ok' : 'danger',
    summaryHtml: [
      drawerMetric('Key 状态', availability.label, availability.detail),
      drawerMetric('成功调用', fmtNum(key.total_used || 0), `失败 ${fmtNum(key.total_failed || 0)}`),
      drawerMetric('最近使用', formatTime(key.last_used_at), key.usage_sync_error ? '存在同步异常' : '统计正常'),
    ].join(''),
    bodyHtml: [
      drawerSection('Key 配额', renderKeyQuota(service, key)),
      drawerSection('账户额度', renderAccountQuota(service, key)),
      key.disabled_detail ? drawerSection('调度原因', `<div class="table-note danger">${escapeHtml(key.disabled_detail)}</div>`) : '',
      drawerSection('代理统计', `
        <div class="drawer-grid drawer-grid-compact">
          <div class="drawer-inline-card"><span>成功</span><strong>${fmtNum(key.total_used || 0)}</strong></div>
          <div class="drawer-inline-card"><span>失败</span><strong>${fmtNum(key.total_failed || 0)}</strong></div>
          <div class="drawer-inline-card"><span>最近使用</span><strong>${escapeHtml(formatTime(key.last_used_at))}</strong></div>
        </div>
        ${key.usage_sync_error ? `<div class="table-note danger" style="margin-top: 10px;">同步异常：${escapeHtml(key.usage_sync_error)}</div>` : ''}
      `),
    ].join(''),
    actionsHtml: [
      renderDrawerActionGroup('维护动作', `
        <button class="btn btn-soft" type="button" onclick="closeDetailDrawer(); toggleKey('${service}', ${key.id}, ${availability.schedulable ? 0 : 1})">${escapeHtml(availability.actionLabel)} Key</button>
      `),
      renderDrawerActionGroup('危险动作', `
        <button class="btn btn-danger" type="button" onclick="closeDetailDrawer(); delKey('${service}', ${key.id})">删除 Key</button>
      `, 'danger'),
    ].join(''),
  });
}

function renderProviderWorkspace(service, servicePayload) {
  const payload = servicePayload || getBlankServicePayload();
  const meta = SERVICE_META[service];
  renderSyncMeta(service, payload);
  renderOverview(service, payload);
  renderApiExample(service);
  renderTokens(service, payload.tokens || []);
  renderKeys(service, payload.keys || []);
  renderPoolGlance(service, payload);
  const syncButton = document.getElementById(`sync-btn-${service}`);
  if (syncButton) {
    const syncSupported = payload.usage_sync?.supported !== false && meta.syncSupported !== false;
    syncButton.textContent = syncSupported ? meta.syncButton : '暂不支持同步';
    syncButton.disabled = !syncSupported;
    syncButton.title = syncSupported ? '' : (payload.usage_sync?.detail || meta.quotaSource);
  }
}

function renderDashboardScope(scope) {
  const nextScope = normalizeRefreshScope(scope);
  if (nextScope.core) {
    if (PAGE_KIND === 'console') {
      renderGlobalSummary(latestServices, latestSocial);
      renderHeroFocus(latestServices, latestSocial);
      renderServiceSwitcher(latestServices, latestSocial);
    }
    renderSettingsSummaries();
  }
  if (nextScope.mysearch) {
    renderMySearchQuickstart(latestMySearch, latestSocial);
  }
  if (PAGE_KIND === 'console' && nextScope.social) {
    renderSocialBoard(latestSocial);
    renderSocialIntegration(latestSocial);
    renderSocialWorkspace(latestSocial);
  }
  if (PAGE_KIND === 'console') {
    nextScope.services.forEach((service) => {
      renderProviderWorkspace(service, latestServices[service] || getBlankServicePayload());
    });
    applyActiveService();
  }
}

async function refresh(options = {}) {
  const force = options.force ? '?force=1' : '';
  const payload = await api('GET', `/api/stats${force}`);
  const services = payload.services || {};
  const social = payload.social || {};
  const mysearch = payload.mysearch || {};
  latestStatsMeta = payload.meta || {};
  latestServices = services;
  latestSocial = social;
  latestMySearch = mysearch;
  renderDashboardScope(options.scope);
}

function toggleImport(service, button) {
  const shell = document.getElementById(`import-wrap-${service}`);
  if (!shell) return;
  const expanded = shell.classList.contains('hidden');
  shell.classList.toggle('hidden', !expanded);
  button?.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (expanded) {
    document.getElementById(`import-text-${service}`)?.focus({ preventScroll: true });
  }
}

async function createToken(service, button) {
  const input = document.getElementById(`token-name-${service}`);
  const tokenName = input.value.trim();
  await runWithBusyButton(button, {
    busyLabel: '创建中...',
    successLabel: '已创建',
    errorLabel: '创建失败',
    minBusyMs: service === 'mysearch' ? 560 : BUTTON_MIN_BUSY_MS,
  }, async () => {
    await api('POST', '/api/tokens', {
      service,
      name: tokenName,
    });
    input.value = '';
  });
  await sleep(service === 'mysearch' ? 180 : 80);
  await refresh({ force: true, scope: getRefreshScopeForService(service) });
}

async function delToken(service, id) {
  const confirmed = await showConfirmDialog({
    title: '删除 Token',
    message: '删除后这个 token 会立即失效，下游客户端会立刻无法继续调用。',
    confirmText: '确认删除',
    cancelText: '取消',
    tone: 'danger',
    kicker: 'Danger Zone',
  });
  if (!confirmed) return;
  await api('DELETE', `/api/tokens/${id}`);
  await refresh({ force: true, scope: getRefreshScopeForService(service) });
}

async function addSingleKey(service, button) {
  const input = document.getElementById(`single-key-${service}`);
  const key = input.value.trim();
  if (!key) return;
  const result = await runWithBusyButton(button, {
    busyLabel: '添加中...',
    successLabel: '已处理',
    errorLabel: '添加失败',
  }, async () => {
    const payload = await api('POST', '/api/keys', { service, key });
    input.value = '';
    return payload;
  });
  showToast(describeKeyIngestionResult(result, service), result.imported ? 'success' : 'warn');
  await refresh({ force: true, scope: getRefreshScopeForService(service) });
}

function describeKeyIngestionResult(result, service) {
  const label = SERVICE_META[service].label;
  const parts = [
    ['新增', Number(result.inserted || 0)],
    ['恢复', Number(result.reactivated || 0)],
    ['重复', Number(result.duplicates || 0)],
    ['仍停用', Number(result.disabled || 0)],
    ['格式无效', Number(result.invalid || 0)],
  ].filter(([, count]) => count > 0).map(([name, count]) => `${name} ${count}`);
  if (!parts.length) return `${label} Key 未发生变更`;
  return `${label} Key：${parts.join('，')}`;
}

async function importKeys(service, button) {
  const textarea = document.getElementById(`import-text-${service}`);
  const text = textarea.value.trim();
  if (!text) return;
  const result = await runWithBusyButton(button, {
    busyLabel: '导入中...',
    successLabel: '已处理',
    errorLabel: '导入失败',
  }, async () => {
    return api('POST', '/api/keys', { service, file: text });
  });
  if (!(result.invalid || result.disabled)) {
    textarea.value = '';
    document.getElementById(`import-wrap-${service}`).classList.add('hidden');
  }
  showToast(describeKeyIngestionResult(result, service), result.imported ? 'success' : 'warn');
  await refresh({ force: true, scope: getRefreshScopeForService(service) });
}

async function delKey(service, id) {
  const confirmed = await showConfirmDialog({
    title: '删除 API Key',
    message: '删除后这个上游 Key 会从当前服务池中移除，额度同步和代理调用都会停止使用它。',
    confirmText: '确认删除',
    cancelText: '取消',
    tone: 'danger',
    kicker: 'Danger Zone',
  });
  if (!confirmed) return;
  await api('DELETE', `/api/keys/${id}`);
  await refresh({ force: true, scope: getRefreshScopeForService(service) });
}

async function toggleKey(service, id, active) {
  await api('PUT', `/api/keys/${id}/toggle`, { active });
  await refresh({ force: true, scope: getRefreshScopeForService(service) });
}

async function syncUsage(service, force, button) {
  if (SERVICE_META[service]?.syncSupported === false) {
    showToast(SERVICE_META[service].quotaSource, 'warn');
    return;
  }
  const actionButton = button || document.getElementById(`sync-btn-${service}`);
  try {
    await runWithBusyButton(actionButton, {
      busyLabel: '同步中...',
      successLabel: '已同步',
      errorLabel: '同步失败',
    }, async () => {
      await api('POST', '/api/usage/sync', { service, force });
    });
    await refresh({ force: true, scope: getRefreshScopeForService(service) });
  } catch (error) {
    showToast(`同步 ${SERVICE_META[service].label} 额度失败: ${error.message}`, 'error');
  }
}

async function changePwd(event) {
  event?.preventDefault?.();
  const button = event?.submitter;
  const input = document.getElementById('settings-new-pwd');
  const password = input.value.trim();
  if (password.length < 4) {
    setStatus('settings-password-status', '密码至少 4 位。', true);
    return;
  }
  try {
    await runWithBusyButton(button, {
      busyLabel: '保存中...',
      successLabel: '已保存',
      errorLabel: '保存失败',
    }, async () => {
      await api('PUT', '/api/password', { password });
      PWD = password;
      localStorage.setItem(STORAGE_KEY, password);
      localStorage.removeItem(LEGACY_STORAGE_KEY);
      input.value = '';
      setStatus('settings-password-status', '密码已更新，当前会话也已同步。');
    });
  } catch (error) {
    setStatus('settings-password-status', `保存密码失败：${error.message}`, true);
  }
}

async function saveSocialSettings(event) {
  event?.preventDefault?.();
  const button = event?.submitter;
  const body = collectSocialSettingsForm();
  clearSettingsProbe('social');

  try {
    await runWithBusyButton(button, {
      busyLabel: '保存中...',
      successLabel: '已保存',
      errorLabel: '保存失败',
    }, async () => {
      const payload = await api('PUT', '/api/settings/social', body);
      latestSettings = payload || {};
      fillSettingsForm(latestSettings);
      setStatus('settings-social-status', 'Social / X 设置已保存，当前控制台状态已刷新。');
    });
    await refresh({ force: true, scope: getRefreshScopeForService('social') });
  } catch (error) {
    setStatus('settings-social-status', `保存 Social / X 设置失败：${error.message}`, true);
  }
}

async function resumeSocialUpstreamKey(keyId, button) {
  try {
    await runWithBusyButton(button, {
      busyLabel: '恢复中...',
      successLabel: '已恢复',
      errorLabel: '恢复失败',
    }, async () => {
      const payload = await api('PUT', `/api/settings/social/keys/${encodeURIComponent(keyId)}/resume`);
      latestSettings = payload || {};
      fillSettingsForm(latestSettings);
    });
    await refresh({ force: true, scope: getRefreshScopeForService('social') });
  } catch (error) {
    setStatus('settings-social-status', `恢复 Social / X key 失败：${error.message}`, true);
  }
}

function flashButtonLabel(button, label) {
  flashButtonState(button, label);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms || 0)));
}

function ensureButtonLabel(button) {
  if (!button) return '';
  if (!button.dataset.originalLabel) {
    button.dataset.originalLabel = button.textContent.trim();
  }
  return button.dataset.originalLabel;
}

function resetButtonState(button, label = '') {
  if (!button) return;
  const original = ensureButtonLabel(button);
  button.disabled = false;
  button.removeAttribute('aria-busy');
  button.classList.remove('is-busy', 'is-success', 'is-error');
  button.textContent = label || original;
}

function flashButtonState(button, label, state = 'success', duration = 1400) {
  if (!button) return;
  const original = ensureButtonLabel(button);
  button.disabled = true;
  button.classList.remove('is-busy', 'is-success', 'is-error');
  button.classList.add(`is-${state}`);
  button.textContent = label;
  setTimeout(() => {
    resetButtonState(button, original);
  }, duration);
}

async function runWithBusyButton(button, {
  busyLabel = '处理中...',
  successLabel = '已完成',
  errorLabel = '失败',
  minBusyMs = BUTTON_MIN_BUSY_MS,
} = {}, task) {
  if (!button) {
    return task();
  }
  const original = ensureButtonLabel(button);
  const startedAt = Date.now();
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.classList.remove('is-success', 'is-error');
  button.classList.add('is-busy');
  button.textContent = busyLabel;
  try {
    const result = await task();
    const remaining = minBusyMs - (Date.now() - startedAt);
    if (remaining > 0) {
      await sleep(remaining);
    }
    if (button.isConnected) {
      flashButtonState(button, successLabel, 'success');
    }
    return result;
  } catch (error) {
    const remaining = minBusyMs - (Date.now() - startedAt);
    if (remaining > 0) {
      await sleep(remaining);
    }
    if (button.isConnected) {
      flashButtonState(button, errorLabel, 'error');
    }
    throw error;
  } finally {
    button.removeAttribute('aria-busy');
    if (button.classList.contains('is-busy')) {
      resetButtonState(button, original);
    }
  }
}

async function writeClipboardText(text) {
  const value = String(text ?? '');

  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (error) {
      console.warn('Clipboard API failed, falling back to execCommand copy.', error);
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '0';
  textarea.style.left = '-9999px';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';

  document.body.appendChild(textarea);

  const selection = document.getSelection();
  const ranges = [];
  if (selection) {
    for (let index = 0; index < selection.rangeCount; index += 1) {
      ranges.push(selection.getRangeAt(index).cloneRange());
    }
  }

  textarea.focus({ preventScroll: true });
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  let copied = false;
  try {
    copied = document.execCommand('copy');
  } finally {
    textarea.remove();
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
  }

  if (!copied) {
    throw new Error('Clipboard copy command was rejected');
  }

  return true;
}

async function copyCode(elementId, button) {
  const source = document.getElementById(elementId);
  if (!source) {
    flashButtonState(button, '未找到', 'error');
    return;
  }

  try {
    await writeClipboardText(source.textContent);
    flashButtonState(button, '已复制', 'success');
  } catch (error) {
    console.error(`Copy failed for #${elementId}`, error);
    flashButtonState(button, '复制失败', 'error');
  }
}

async function copyMySearchEnv(button) {
  try {
    const value = buildMySearchEnv(latestMySearch || {}, latestSocial || {}, { includeSecret: true });
    await writeClipboardText(value);
    flashButtonState(button, '已复制', 'success');
  } catch (error) {
    console.error('Copy failed for MySearch .env', error);
    flashButtonState(button, '复制失败', 'error');
  }
}

async function copyEnvAndRevealInstall(button) {
  const installBlock = document.getElementById('mysearch-install-cmd');
  try {
    const value = buildMySearchEnv(latestMySearch || {}, latestSocial || {}, { includeSecret: true });
    await writeClipboardText(value);
    if (installBlock) {
      setQuickstartTab('install', false);
      installBlock.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    flashButtonState(button, '已复制并定位', 'success');
  } catch (error) {
    console.error('Copy-and-scroll failed for MySearch quickstart', error);
    flashButtonState(button, '操作失败', 'error');
  }
}

async function copyText(value, button) {
  try {
    await writeClipboardText(value);
    flashButtonState(button, '已复制', 'success');
  } catch (error) {
    console.error('Copy failed for inline value', error);
    flashButtonState(button, '复制失败', 'error');
  }
}

async function copyTokenById(service, tokenId, button) {
  const token = (getServicePayload(service)?.tokens || []).find((item) => Number(item.id) === Number(tokenId));
  if (!token?.token) {
    flashButtonState(button, '未找到', 'error');
    return;
  }
  await copyText(token.token, button);
}

document.addEventListener('keydown', (event) => {
  if (handleSegmentedControlKey(event)) {
    return;
  }
  if (trapOverlayFocus(event)) {
    return;
  }
  if (event.key !== 'Escape') return;
  if (isShellVisible('app-dialog')) {
    closeAppDialog(false);
    return;
  }
  if (isShellVisible('detail-drawer')) {
    closeDetailDrawer();
    return;
  }
  if (isShellVisible('settings-modal')) {
    closeSettingsModal();
  }
});

window.addEventListener('focus', () => {
  refreshAutoThemeFromClock();
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    refreshAutoThemeFromClock();
  }
});

applyTheme(activeTheme);
renderServiceShells();

async function initConsole() {
  if (INITIAL_AUTHENTICATED) {
    showDashboard();
    try {
      await refresh();
    } catch (error) {
      if (error.message === 'Unauthorized') {
        showLogin();
        return;
      }
      document.getElementById('login-err').textContent = `控制台加载失败：${error.message}`;
      document.getElementById('login-err').classList.remove('hidden');
    }
    return;
  }

  const migrated = await migrateStoredPasswordIfNeeded();
  const hasSession = migrated || await hasServerSession();
  if (!hasSession) {
    showLogin();
    return;
  }
  showDashboard();
  try {
    await refresh();
  } catch (error) {
    if (error.message === 'Unauthorized') {
      showLogin();
      return;
    }
    document.getElementById('login-err').textContent = `控制台加载失败：${error.message}`;
    document.getElementById('login-err').classList.remove('hidden');
  }
}

initConsole();

function setActiveSettingsTab(tabName) {
  document.querySelectorAll('.settings-tab').forEach(btn => {
    btn.classList.toggle('is-active', btn.dataset.settingsTab === tabName);
    btn.setAttribute('aria-selected', btn.dataset.settingsTab === tabName ? 'true' : 'false');
    btn.setAttribute('tabindex', btn.dataset.settingsTab === tabName ? '0' : '-1');
  });
  document.querySelectorAll('.settings-tab-panel').forEach(panel => {
    const isActive = panel.dataset.settingsPanel === tabName;
    panel.classList.toggle('hidden', !isActive);
    panel.classList.toggle('is-active', isActive);
    panel.setAttribute('aria-hidden', isActive ? 'false' : 'true');
  });
}

function setQuickstartTab(tabName, focus = true) {
  const allowed = ['config', 'install', 'tokens'];
  const nextTab = allowed.includes(tabName) ? tabName : 'config';
  activeQuickstartTab = nextTab;
  document.querySelectorAll('.quickstart-tab').forEach((button) => {
    const isActive = button.dataset.quickstartTab === nextTab;
    button.classList.toggle('is-active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
    button.setAttribute('tabindex', isActive ? '0' : '-1');
    if (isActive && focus) button.focus({ preventScroll: true });
  });
  document.querySelectorAll('[data-quickstart-panel]').forEach((panel) => {
    const isActive = panel.dataset.quickstartPanel === nextTab;
    panel.classList.toggle('hidden', !isActive);
    panel.setAttribute('aria-hidden', isActive ? 'false' : 'true');
  });
}


function maskToken(token) {
  if (!token) return '';
  if (token.length <= 12) return '****';
  return token.slice(0, 5) + '****' + token.slice(-4);
}


async function saveTavilySettings(event) {
  event?.preventDefault?.();
  const button = event?.submitter;
  const body = collectTavilySettingsForm();
  clearSettingsProbe('tavily');

  try {
    await runWithBusyButton(button, {
      busyLabel: '保存中...',
      successLabel: '已保存',
      errorLabel: '保存失败',
    }, async () => {
      const payload = await api('PUT', '/api/settings/tavily', body);
      latestSettings = payload || {};
      fillSettingsForm(latestSettings);
      setStatus('settings-tavily-status', 'Tavily 设置已保存。');
    });
    await refresh({ force: true, scope: getRefreshScopeForService('tavily') });
  } catch (error) {
    setStatus('settings-tavily-status', `保存失败：${error.message}`, true);
  }
}
