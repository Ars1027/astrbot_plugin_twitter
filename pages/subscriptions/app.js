import { parseBatch, BatchQueue } from "./batch.mjs";
import { element as el, emptyState } from "./dom.mjs";

const bridge = window.AstrBotPluginPage;

const elements = {
  workspace: document.querySelector(".workspace"),
  runtimeStatus: document.getElementById("runtime-status"),
  refreshButton: document.getElementById("refresh-button"),
  errorNotice: document.getElementById("error-notice"),
  sessionList: document.getElementById("session-list"),
  sessionSearch: document.getElementById("session-search"),
  detailView: document.getElementById("detail-view"),
  intervalForm: document.getElementById("interval-form"),
  pollInterval: document.getElementById("poll-interval"),
  removeDialog: document.getElementById("remove-dialog"),
  removeDialogText: document.getElementById("remove-dialog-text"),
  confirmRemove: document.getElementById("confirm-remove"),
  toast: document.getElementById("toast"),
  metrics: {
    groups: document.getElementById("metric-groups"),
    authors: document.getElementById("metric-authors"),
    subscriptions: document.getElementById("metric-subscriptions"),
    active: document.getElementById("metric-active"),
  },
};

const state = {
  overview: null,
  view: "groups",
  selectedUmo: null,
  search: "",
  authorSearch: "",
  loading: true,
  saving: false,
  removeTarget: null,
  toastTimer: null,
  batch: null,
  batchTarget: null,
  batchPreparing: false,
  leaving: false,
  historyTarget: null,
  historyRequest: 0,
};

const batch = Object.fromEntries([
  "dialog", "target", "form", "input", "r18", "media", "summary", "progress",
  "entries", "error", "start", "stop", "retry", "reset", "close",
].map((key) => [key, document.getElementById(`batch-${key}`)]));

function busy() {
  return state.saving || state.loading || state.batchPreparing || state.batch?.running;
}

function syncBusy() {
  document.querySelectorAll(".page-shell button, .page-shell input").forEach((control) => {
    // Dialogs manage their own controls, including the active batch stop button.
    if (!control.closest("dialog")) control.disabled = Boolean(busy());
  });
}

function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.classList.remove("is-hidden");
  state.toastTimer = window.setTimeout(() => {
    elements.toast.classList.add("is-hidden");
  }, 3200);
}

function setError(message = "") {
  elements.errorNotice.textContent = message;
  elements.errorNotice.classList.toggle("is-hidden", !message);
}

function setSaving(saving) {
  state.saving = saving;
  syncBusy();
}

function currentSessions() {
  if (!state.overview) return [];
  return state.view === "groups"
    ? state.overview.groups
    : state.overview.other_sessions;
}

function sessionSearchText(session) {
  if (state.view === "groups") {
    return `${session.group_name} ${session.group_id} ${session.platform_id}`;
  }
  return `${session.session_name} ${session.session_id} ${session.platform_id}`;
}

function filteredSessions() {
  const query = state.search.trim().toLocaleLowerCase();
  if (!query) return currentSessions();
  return currentSessions().filter((session) =>
    sessionSearchText(session).toLocaleLowerCase().includes(query),
  );
}

function ensureSelection() {
  const sessions = currentSessions();
  if (!sessions.some((session) => session.umo === state.selectedUmo)) {
    state.selectedUmo = sessions[0]?.umo ?? null;
  }
}

function selectedSession() {
  return currentSessions().find((session) => session.umo === state.selectedUmo) ?? null;
}

function renderRuntime() {
  if (!state.overview) return;
  const { provider, polling, totals } = state.overview;
  elements.runtimeStatus.classList.toggle("is-ready", provider.ready && polling.running);
  elements.runtimeStatus.classList.toggle("is-error", !provider.ready);
  const providerLabel = provider.name === "fxtwitter" ? "FxTwitter" : "Nitter";
  elements.runtimeStatus.lastElementChild.textContent = provider.ready
    ? `${providerLabel} · ${polling.running ? "轮询中" : "已就绪"}`
    : `${providerLabel} · 不可用`;

  for (const [key, target] of Object.entries(elements.metrics)) {
    target.textContent = totals[key] ?? 0;
  }
  if (document.activeElement !== elements.pollInterval) {
    elements.pollInterval.value = polling.interval_minutes;
  }

  const failedSources = state.overview.group_sources.filter((source) => !source.available);
  if (failedSources.length) {
    setError(
      `${failedSources.length} 个 QQ 实例暂时无法读取群列表；已有订阅仍可管理。`,
    );
  } else {
    setError("");
  }
}

function renderSessionList() {
  const sessions = filteredSessions();
  const isGroup = state.view === "groups";
  elements.sessionSearch.placeholder = isGroup ? "搜索群名或群号" : "搜索会话";
  if (!sessions.length) {
    elements.sessionList.replaceChildren(emptyState(
      state.search ? "没有匹配结果" : isGroup ? "暂无群聊" : "暂无其他会话",
      state.search ? "请尝试其他关键词" : "当前没有可管理的会话", "list-empty"));
    return;
  }
  elements.sessionList.replaceChildren(...sessions.map((session) => {
    const active = session.umo === state.selectedUmo;
    return el("button", {
      class: `session-item ${active ? "is-active" : ""}`, type: "button",
      "aria-current": String(active), "data-umo": session.umo,
    },
    el("span", { class: "session-avatar", "aria-hidden": "true" }, isGroup ? "#" : "@"),
    el("span", { class: "session-copy" },
      el("span", { class: "session-name" }, isGroup ? session.group_name : session.session_name),
      el("span", { class: "session-meta" },
        `${isGroup ? session.group_id : session.session_id} · ${session.platform_id}`,
        isGroup && !session.available ? el("span", { class: "availability-mark" }, " · 未连接") : null)),
    el("span", { class: "session-count" }, session.subscriptions.length));
  }));
}

function subscriptionRow(session, subscription) {
  const target = { "data-umo": session.umo, "data-username": subscription.username };
  return el("div", { class: "subscription-row", "data-username": subscription.username },
    el("button", { class: "author-cell author-open", type: "button", "data-show-history": true,
      ...target, "aria-label": `查看 @${subscription.username} 的最近推送` },
      el("span", { class: "author-avatar", "aria-hidden": "true" },
        Array.from(subscription.screen_name || subscription.username)[0]),
      el("span", { class: "author-copy" },
        el("span", { class: "author-name" }, subscription.screen_name),
        el("span", { class: "author-handle" }, `@${subscription.username} `,
          el("span", { class: "history-link" }, "最近推送 ↗")))),
    ...[["enabled", "推送"], ["r18", "R18"], ["media_only", "仅媒体"]].map(([field, label]) =>
      el("label", { class: "switch-field" },
        el("input", { type: "checkbox", "data-subscription-field": field,
          ...target, checked: Boolean(subscription[field]) }), el("span", {}, label))),
    el("button", { class: "remove-button", type: "button", "data-remove-subscription": true,
      ...target, "data-screen-name": subscription.screen_name, title: "移除订阅",
      "aria-label": `移除 @${subscription.username}` }, "×"));
}

function renderDetail() {
  const session = selectedSession();
  if (!session) {
    elements.detailView.replaceChildren(emptyState("未选择会话", "从列表中选择一个会话"));
    return;
  }
  const isGroup = state.view === "groups";
  const name = isGroup ? session.group_name : session.session_name;
  const id = isGroup ? session.group_id : session.session_id;
  const allEnabled = session.subscriptions.length > 0 && session.subscriptions.every((item) => item.enabled);
  const groupSwitch = isGroup && session.subscriptions.length
    ? el("label", { class: "group-switch" },
      el("input", { id: "group-status", type: "checkbox", "data-umo": session.umo, checked: allEnabled }),
      el("span", {}, "全部推送")) : null;
  let addSection = null;
  if (isGroup) {
    addSection = session.available
      ? el("section", { class: "add-section" },
        el("div", { class: "section-title" }, "添加推主",
          el("span", { class: "muted" }, "订阅从最新推文开始")),
        el("form", { class: "add-form", id: "add-form", "data-umo": session.umo },
          el("input", { name: "username", type: "text", maxlength: "16", autocomplete: "off",
            placeholder: "推主用户名，例如 elonmusk", "aria-label": "推主用户名", required: true }),
          ...[["r18", "R18"], ["media_only", "仅媒体"]].map(([field, label]) =>
            el("label", { class: "option-check" },
              el("input", { name: field, type: "checkbox" }), el("span", {}, label))),
          el("button", { class: "primary-button", type: "submit" }, "添加订阅"),
          el("button", { class: "secondary-button", type: "button", id: "open-batch" }, "批量添加")))
      : el("section", { class: "add-section" },
        el("p", { class: "unavailable-note" }, "机器人当前未连接到这个群，暂不能新增订阅。"));
  }
  elements.detailView.replaceChildren(
    el("header", { class: "detail-header" },
      el("div", { class: "detail-heading" }, el("h2", {}, name),
        el("p", {}, el("span", { class: "type-badge" }, isGroup ? "群聊" : "其他会话"),
          `${id} · ${session.platform_id}`)), groupSwitch),
    ...(addSection ? [addSection] : []),
    el("section", { class: "subscription-section" },
      el("div", { class: "subscription-heading" },
        el("div", { class: "section-title" }, "订阅名单 ",
          el("span", { class: "count-badge" }, session.subscriptions.length)),
        el("label", { class: "author-search" }, el("span", { class: "visually-hidden" }, "搜索推主"),
          el("input", { id: "author-search", type: "search", placeholder: "搜索名称或 @用户名", value: state.authorSearch }))),
      el("div", { class: "subscription-list", id: "subscription-list" })));
  renderSubscriptions();
  syncBusy();
}

function renderSubscriptions() {
  const session = selectedSession();
  const list = document.getElementById("subscription-list");
  if (!session || !list) return;
  const query = state.authorSearch.trim().replace(/^@/, "").toLowerCase();
  const matches = session.subscriptions.filter((item) =>
    `${item.username} ${item.screen_name}`.toLowerCase().includes(query));
  if (matches.length) list.replaceChildren(...matches.map((item) => subscriptionRow(session, item)));
  else {
    const empty = emptyState(query ? "没有匹配的推主" : "从第一位推主开始",
      query ? "试试其他名称或用户名" : "添加订阅后，就能在这里管理推送偏好。");
    empty.prepend(el("span", { class: "empty-icon", "aria-hidden": "true" }, query ? "⌕" : "+"));
    list.replaceChildren(empty);
  }
  syncBusy();
}

function render() {
  if (!state.overview) return;
  ensureSelection();
  renderRuntime();
  renderSessionList();
  renderDetail();
  elements.workspace.setAttribute("aria-busy", "false");
}

async function loadOverview({ announce = false } = {}) {
  if (!bridge) {
    setError("当前页面未运行在 AstrBot Dashboard 中。" );
    return false;
  }
  state.loading = true;
  syncBusy();
  elements.refreshButton.classList.add("is-loading");
  try {
    state.overview = await bridge.apiGet("overview");
    render();
    if (announce) showToast("订阅数据已刷新");
    return true;
  } catch (error) {
    setError(error?.message || "读取订阅数据失败");
    if (!state.overview) {
      elements.sessionList.replaceChildren(emptyState("暂时无法读取会话", "点击顶部刷新重试", "list-empty"));
      elements.detailView.replaceChildren(emptyState("订阅数据加载失败", "请检查连接后刷新页面。"));
    }
    if (announce) showToast("刷新失败", true);
    return false;
  } finally {
    state.loading = false;
    elements.refreshButton.classList.remove("is-loading");
    elements.workspace.setAttribute("aria-busy", "false");
    syncBusy();
  }
}

async function saveAndReload(endpoint, payload, successMessage) {
  if (busy()) return false;
  setSaving(true);
  try {
    await bridge.apiPost(endpoint, payload);
    await loadOverview();
    showToast(successMessage);
    return true;
  } catch (error) {
    showToast(error?.message || "保存失败", true);
    return false;
  } finally {
    setSaving(false);
  }
}

elements.refreshButton.addEventListener("click", () => {
  if (!busy()) loadOverview({ announce: true });
});

elements.sessionSearch.addEventListener("input", (event) => {
  state.search = event.target.value;
  renderSessionList();
});

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    if (busy()) return;
    state.view = button.dataset.view;
    state.search = "";
    state.authorSearch = "";
    state.selectedUmo = null;
    elements.sessionSearch.value = "";
    document.querySelectorAll("[data-view]").forEach((item) => {
      const active = item === button;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
    });
    render();
  });
});

elements.sessionList.addEventListener("click", (event) => {
  if (busy()) return;
  const item = event.target.closest("[data-umo]");
  if (!item) return;
  state.selectedUmo = item.dataset.umo;
  state.authorSearch = "";
  renderSessionList();
  renderDetail();
});

elements.intervalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const minutes = Number(elements.pollInterval.value);
  if (!Number.isInteger(minutes) || minutes < 1) {
    showToast("轮询间隔必须是不少于 1 的整数", true);
    elements.pollInterval.focus();
    return;
  }
  await saveAndReload(
    "settings/poll-interval",
    { minutes },
    `轮询间隔已更新为 ${minutes} 分钟`,
  );
});

elements.detailView.addEventListener("submit", async (event) => {
  if (event.target.id !== "add-form") return;
  event.preventDefault();
  if (busy()) return;
  const form = event.target;
  const formData = new FormData(form);
  const username = String(formData.get("username") || "").trim().replace(/^@/, "");
  if (!/^[A-Za-z0-9_]{1,15}$/.test(username)) {
    showToast("请输入有效的推主用户名", true);
    form.elements.username.focus();
    return;
  }
  const saved = await saveAndReload(
    "subscriptions/add",
    {
      umo: form.dataset.umo,
      username,
      r18: formData.get("r18") === "on",
      media_only: formData.get("media_only") === "on",
    },
    `已订阅 @${username}`,
  );
  if (saved) form.reset();
});

elements.detailView.addEventListener("change", async (event) => {
  if (busy()) return;
  const field = event.target.dataset.subscriptionField;
  if (field) {
    await saveAndReload(
      "subscriptions/update",
      {
        umo: event.target.dataset.umo,
        username: event.target.dataset.username,
        [field]: event.target.checked,
      },
      "订阅选项已保存",
    );
    return;
  }
  if (event.target.id === "group-status") {
    await saveAndReload(
      "subscriptions/group-status",
      {
        umo: event.target.dataset.umo,
        enabled: event.target.checked,
      },
      event.target.checked ? "已开启本群全部推送" : "已关闭本群全部推送",
    );
  }
});

elements.detailView.addEventListener("click", (event) => {
  if (busy()) return;
  const author = event.target.closest("[data-show-history]");
  if (author) {
    openHistory(author);
    return;
  }
  if (event.target.closest("#open-batch")) {
    openBatch();
    return;
  }
  const button = event.target.closest("[data-remove-subscription]");
  if (!button) return;
  state.removeTarget = {
    umo: button.dataset.umo,
    username: button.dataset.username,
  };
  elements.removeDialogText.textContent = `确定移除 @${button.dataset.username}（${button.dataset.screenName}）吗？`;
  elements.removeDialog.returnValue = "";
  elements.removeDialog.showModal();
});

elements.removeDialog.addEventListener("close", async () => {
  if (elements.removeDialog.returnValue !== "confirm" || !state.removeTarget) {
    state.removeTarget = null;
    return;
  }
  const target = state.removeTarget;
  state.removeTarget = null;
  await saveAndReload(
    "subscriptions/remove",
    target,
    `已移除 @${target.username}`,
  );
});

elements.detailView.addEventListener("input", (event) => {
  if (event.target.id !== "author-search") return;
  state.authorSearch = event.target.value;
  renderSubscriptions();
});

function batchSession() {
  return state.overview?.groups.find((session) => session.umo === state.batchTarget?.umo);
}

function batchPreview() {
  return parseBatch(batch.input.value, batchSession()?.subscriptions.map((item) => item.username) || []);
}

function setBatchError(message = "") {
  batch.error.textContent = message;
  batch.error.classList.toggle("is-hidden", !message);
}

function renderBatch() {
  const draft = batchPreview();
  const entries = state.batch?.entries || draft.entries;
  const counts = Object.fromEntries(["pending", "existing", "duplicate", "invalid", "adding", "added", "failed"]
    .map((status) => [status, entries.filter((entry) => entry.status === status).length]));
  const locked = Boolean(state.batchPreparing || state.batch?.running);
  const labels = { pending: "待添加", existing: "已订阅 · 跳过", duplicate: "重复 · 跳过", invalid: "格式无效", adding: "添加中", added: "已添加", failed: "未确认" };
  batch.summary.textContent = state.batch
    ? `${state.batchPreparing ? "正在核对群聊与订阅… · " : ""}已添加 ${counts.added} · 已订阅 ${counts.existing} · 未确认 ${counts.failed} · 待处理 ${counts.pending + counts.adding}`
    : `待添加 ${counts.pending} · 已订阅 ${counts.existing} · 重复 ${counts.duplicate} · 无效 ${counts.invalid}`;
  const current = entries.find((entry) => entry.status === "adding");
  if (current) batch.summary.textContent += ` · 正在添加 @${current.username}`;
  else if (state.batch?.stopRequested && !state.batchPreparing) batch.summary.textContent += " · 已停止后续添加";
  if (!state.batch && draft.overLimit) batch.summary.textContent += " · 超过 100 个账号，请分批添加";
  batch.summary.classList.toggle("is-error", !state.batch && draft.overLimit);
  batch.entries.replaceChildren(...(entries.length ? entries.map((entry) =>
    el("div", { class: "batch-entry", "data-status": entry.status },
      el("span", { class: "batch-entry-name" }, entry.input),
      el("span", { class: "result-badge" }, labels[entry.status]),
      entry.error ? el("p", {}, `${entry.error}；请核对订阅后再重试。`) : null))
    : [el("div", { class: "batch-placeholder" }, "粘贴用户名后，会在这里自动检查并去重。")]));
  batch.progress.hidden = !state.batch;
  batch.progress.max = Math.max(1, entries.filter((entry) => !["invalid", "duplicate"].includes(entry.status)).length);
  batch.progress.value = counts.added + counts.existing + counts.failed;
  batch.input.disabled = batch.r18.disabled = batch.media.disabled = locked || Boolean(state.batch);
  batch.close.disabled = locked;
  batch.start.hidden = Boolean(state.batch);
  batch.start.disabled = locked || draft.overLimit || !counts.pending;
  batch.start.textContent = `开始添加 ${counts.pending} 个推主`;
  batch.stop.hidden = !state.batch?.running;
  batch.stop.disabled = Boolean(state.batch?.stopRequested);
  batch.stop.textContent = state.batch?.stopRequested ? "等待当前请求结束…" : "停止后续添加";
  batch.retry.hidden = !state.batch || locked || !(counts.failed + counts.pending);
  batch.retry.disabled = locked;
  batch.reset.hidden = !state.batch || locked;
  syncBusy();
}

function resetBatch() {
  if (state.batchPreparing || state.batch?.running) return;
  state.batch = null;
  batch.input.value = "";
  batch.r18.checked = batch.media.checked = false;
  setBatchError();
  renderBatch();
}

function openBatch() {
  const session = selectedSession();
  if (state.view !== "groups" || !session?.available || busy()) return;
  if (state.batchTarget?.umo !== session.umo) resetBatch();
  state.batchTarget = { umo: session.umo, name: session.group_name, id: session.group_id, platform: session.platform_id };
  batch.target.textContent = `${session.group_name} · ${session.group_id} · ${session.platform_id}`;
  renderBatch();
  batch.dialog.showModal();
  if (!state.batch) batch.input.focus();
}

async function executeBatch() {
  if (busy() || !state.batchTarget || state.leaving) return;
  const draft = batchPreview();
  if (!state.batch && (draft.overLimit || !draft.entries.some((entry) => entry.status === "pending"))) return;
  if (!state.batch) {
    state.batch = new BatchQueue({
      entries: draft.entries, umo: state.batchTarget.umo,
      r18: batch.r18.checked, media_only: batch.media.checked,
      send: (payload) => bridge.apiPost("subscriptions/add", payload),
      onChange: () => { if (!state.leaving) renderBatch(); },
    });
  }
  state.batchPreparing = true;
  setBatchError();
  renderBatch();
  try {
    if (!await loadOverview()) throw new Error("无法刷新订阅，尚未开始添加。请稍后重试。");
    if (!batchSession()?.available) throw new Error("目标群当前不可用，尚未开始添加。");
    if (state.leaving) return;
    state.batchPreparing = false;
    await state.batch.run(batchSession().subscriptions.map((item) => item.username));
    state.batchPreparing = true;
    renderBatch();
    if (state.leaving) return;
    if (!await loadOverview()) setBatchError("处理结果已保留，但列表刷新失败。请刷新核对后再重试。");
  } catch (error) {
    setBatchError(error?.message || "批量添加未能开始，请稍后重试");
  } finally {
    state.batchPreparing = false;
    if (!state.leaving) renderBatch();
  }
}

batch.input.addEventListener("input", renderBatch);
batch.form.addEventListener("submit", (event) => { event.preventDefault(); executeBatch(); });
batch.retry.addEventListener("click", executeBatch);
batch.reset.addEventListener("click", () => { resetBatch(); batch.input.focus(); });
batch.stop.addEventListener("click", () => state.batch?.stop());
batch.close.addEventListener("click", () => {
  if (!state.batchPreparing && !state.batch?.running) batch.dialog.close();
});
batch.dialog.addEventListener("cancel", (event) => {
  if (state.batchPreparing || state.batch?.running) event.preventDefault();
});
batch.dialog.addEventListener("close", () => document.getElementById("open-batch")?.focus());
window.addEventListener("pagehide", () => {
  state.leaving = true;
  state.batch?.stop();
});
window.addEventListener("pageshow", () => { state.leaving = false; });
document.querySelectorAll("[data-view]").forEach((button, index, tabs) => {
  button.addEventListener("keydown", (event) => {
    if (busy() || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const next = tabs[(index + 1) % tabs.length];
    next.focus();
    next.click();
  });
});

const historyDialog = document.getElementById("history-dialog");
const historyContent = document.getElementById("history-content");
const historyRefresh = document.getElementById("history-refresh");

function openHistory(button) {
  const session = selectedSession();
  state.historyTarget = { umo: button.dataset.umo, username: button.dataset.username };
  document.getElementById("history-target").textContent = `${session.group_name || session.session_name} · ${session.group_id || session.session_id} · ${session.platform_id} · @${button.dataset.username}`;
  historyDialog.showModal();
  loadHistory();
}

async function loadHistory() {
  const requestId = ++state.historyRequest;
  historyRefresh.disabled = true;
  historyContent.setAttribute("aria-busy", "true");
  historyContent.replaceChildren(emptyState("正在读取推送记录", ""));
  try {
    const result = await bridge.apiPost("subscriptions/recent", { ...state.historyTarget });
    if (requestId !== state.historyRequest) return;
    historyContent.replaceChildren(...(result.items.length ? result.items.map((item, index) => {
      const deliveredAt = new Date(item.delivered_at);
      const time = Number.isNaN(deliveredAt.getTime()) ? "时间未知" : deliveredAt.toLocaleString();
      const link = /^[0-9]+$/.test(item.tweet_id)
        ? el("a", { class: "history-original", href: `https://x.com/i/status/${item.tweet_id}`,
          target: "_blank", rel: "noopener noreferrer" }, "查看原帖 ↗") : null;
      return el("article", { class: "history-entry" },
        el("div", { class: "history-meta" },
          el("span", { class: "type-badge" }, index ? `第 ${index + 1} 条` : "最新推送"),
          el("time", {}, time), item.is_retweet ? el("span", { class: "muted" }, "转帖") : null),
        el("p", { class: "history-text" }, item.text || "这条推文没有文字内容，请打开原帖查看。"),
        item.truncated ? el("p", { class: "field-hint" }, "此处仅显示前 500 字符摘要。") : null, link);
    }) : [emptyState("暂无推送记录", "该订阅后续成功推送后，记录会显示在这里。旧游标和已处理 ID 不会补作历史推送。")]));
  } catch (error) {
    if (requestId !== state.historyRequest) return;
    historyContent.textContent = error?.message || "读取推送记录失败，请重试";
  } finally {
    if (requestId === state.historyRequest) {
      historyRefresh.disabled = false;
      historyContent.setAttribute("aria-busy", "false");
    }
  }
}

historyRefresh.addEventListener("click", loadHistory);
document.getElementById("history-close").addEventListener("click", () => historyDialog.close());
historyDialog.addEventListener("close", () => {
  state.historyRequest++;
  // Native dialog restores focus to the subscription that opened it.
});

async function start() {
  if (!bridge) {
    setError("当前页面未运行在 AstrBot Dashboard 中。" );
    elements.workspace.setAttribute("aria-busy", "false");
    elements.detailView.replaceChildren(emptyState("请从 AstrBot Dashboard 打开", "在插件详情中进入「Twitter 订阅管理」。"));
    syncBusy();
    return;
  }
  const context = await bridge.ready();
  document.title = bridge.t?.("pages.subscriptions.title", "Twitter 订阅管理")
    || "Twitter 订阅管理";
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
  bridge.onContext?.((nextContext) => {
    const current = nextContext || bridge.getContext?.();
    document.documentElement.dataset.theme = current?.isDark ? "dark" : "light";
  });
  await loadOverview();
}

start().catch((error) => {
  setError(error?.message || "页面初始化失败");
  elements.workspace.setAttribute("aria-busy", "false");
});
