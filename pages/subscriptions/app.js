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
  loading: true,
  saving: false,
  removeTarget: null,
  toastTimer: null,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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
  document.querySelectorAll("button, input").forEach((control) => {
    if (!control.closest("dialog") || elements.removeDialog.open) {
      control.disabled = saving;
    }
  });
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
  elements.sessionSearch.placeholder = state.view === "groups"
    ? "搜索群名或群号"
    : "搜索会话";

  if (!sessions.length) {
    const title = state.search ? "没有匹配结果" : state.view === "groups" ? "暂无群聊" : "暂无其他会话";
    elements.sessionList.innerHTML = `
      <div class="list-empty">
        <strong>${title}</strong>
        <span>${state.search ? "请尝试其他关键词" : "当前没有可管理的会话"}</span>
      </div>
    `;
    return;
  }

  elements.sessionList.innerHTML = sessions
    .map((session) => {
      const isGroup = state.view === "groups";
      const name = isGroup ? session.group_name : session.session_name;
      const id = isGroup ? session.group_id : session.session_id;
      const unavailable = isGroup && !session.available;
      return `
        <button
          class="session-item ${session.umo === state.selectedUmo ? "is-active" : ""}"
          type="button"
          data-umo="${escapeHtml(session.umo)}"
        >
          <span class="session-copy">
            <span class="session-name">${escapeHtml(name)}</span>
            <span class="session-meta">
              ${escapeHtml(id)}${unavailable ? ' · <span class="availability-mark">未连接</span>' : ""}
            </span>
          </span>
          <span class="session-count">${session.subscriptions.length}</span>
        </button>
      `;
    })
    .join("");
}

function subscriptionRow(session, subscription) {
  return `
    <div class="subscription-row" data-username="${escapeHtml(subscription.username)}">
      <div class="author-cell">
        <span class="author-name">${escapeHtml(subscription.screen_name)}</span>
        <span class="author-handle">@${escapeHtml(subscription.username)}</span>
      </div>
      <label class="switch-field">
        <input
          type="checkbox"
          data-subscription-field="enabled"
          data-umo="${escapeHtml(session.umo)}"
          data-username="${escapeHtml(subscription.username)}"
          ${subscription.enabled ? "checked" : ""}
        />
        <span>推送</span>
      </label>
      <label class="switch-field">
        <input
          type="checkbox"
          data-subscription-field="r18"
          data-umo="${escapeHtml(session.umo)}"
          data-username="${escapeHtml(subscription.username)}"
          ${subscription.r18 ? "checked" : ""}
        />
        <span>R18</span>
      </label>
      <label class="switch-field">
        <input
          type="checkbox"
          data-subscription-field="media_only"
          data-umo="${escapeHtml(session.umo)}"
          data-username="${escapeHtml(subscription.username)}"
          ${subscription.media_only ? "checked" : ""}
        />
        <span>仅媒体</span>
      </label>
      <button
        class="remove-button"
        type="button"
        data-remove-subscription
        data-umo="${escapeHtml(session.umo)}"
        data-username="${escapeHtml(subscription.username)}"
        data-screen-name="${escapeHtml(subscription.screen_name)}"
        title="移除订阅"
        aria-label="移除 @${escapeHtml(subscription.username)}"
      >×</button>
    </div>
  `;
}

function renderDetail() {
  const session = selectedSession();
  if (!session) {
    elements.detailView.innerHTML = `
      <div class="detail-empty">
        <strong>未选择会话</strong>
        <span>从列表中选择一个会话</span>
      </div>
    `;
    return;
  }

  const isGroup = state.view === "groups";
  const name = isGroup ? session.group_name : session.session_name;
  const id = isGroup ? session.group_id : session.session_id;
  const allEnabled = session.subscriptions.length > 0 && session.subscriptions.every((item) => item.enabled);
  const groupSwitch = isGroup && session.subscriptions.length
    ? `
      <label class="group-switch">
        <input
          id="group-status"
          type="checkbox"
          data-umo="${escapeHtml(session.umo)}"
          ${allEnabled ? "checked" : ""}
        />
        <span>全部推送</span>
      </label>
    `
    : "";

  let addSection = "";
  if (isGroup) {
    addSection = session.available
      ? `
        <section class="add-section">
          <div class="section-title">新增订阅</div>
          <form class="add-form" id="add-form" data-umo="${escapeHtml(session.umo)}">
            <input
              name="username"
              type="text"
              maxlength="16"
              autocomplete="off"
              placeholder="推主用户名，例如 elonmusk"
              aria-label="推主用户名"
              required
            />
            <label class="option-check">
              <input name="r18" type="checkbox" />
              <span>R18</span>
            </label>
            <label class="option-check">
              <input name="media_only" type="checkbox" />
              <span>仅媒体</span>
            </label>
            <button class="primary-button" type="submit">添加订阅</button>
          </form>
        </section>
      `
      : `
        <section class="add-section">
          <p class="unavailable-note">机器人当前未连接到这个群，暂不能新增订阅。</p>
        </section>
      `;
  }

  const rows = session.subscriptions.length
    ? session.subscriptions.map((item) => subscriptionRow(session, item)).join("")
    : `
      <div class="detail-empty">
        <strong>暂无订阅</strong>
        <span>${isGroup ? "可以在上方添加推主" : "这个会话还没有 Twitter 订阅"}</span>
      </div>
    `;

  elements.detailView.innerHTML = `
    <header class="detail-header">
      <div class="detail-heading">
        <h2>${escapeHtml(name)}</h2>
        <p>${escapeHtml(id)} · ${escapeHtml(session.platform_id)}</p>
      </div>
      ${groupSwitch}
    </header>
    ${addSection}
    <section class="subscription-section">
      <div class="subscription-heading">
        <div class="section-title">订阅名单</div>
        <span>${session.subscriptions.length} 项</span>
      </div>
      <div class="subscription-list">${rows}</div>
    </section>
  `;
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
    return;
  }
  state.loading = true;
  elements.refreshButton.classList.add("is-loading");
  try {
    state.overview = await bridge.apiGet("overview");
    render();
    if (announce) showToast("订阅数据已刷新");
  } catch (error) {
    setError(error?.message || "读取订阅数据失败");
    if (announce) showToast("刷新失败", true);
  } finally {
    state.loading = false;
    elements.refreshButton.classList.remove("is-loading");
  }
}

async function saveAndReload(endpoint, payload, successMessage) {
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

elements.refreshButton.addEventListener("click", () => loadOverview({ announce: true }));

elements.sessionSearch.addEventListener("input", (event) => {
  state.search = event.target.value;
  renderSessionList();
});

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    state.view = button.dataset.view;
    state.search = "";
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
  const item = event.target.closest("[data-umo]");
  if (!item) return;
  state.selectedUmo = item.dataset.umo;
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
  const button = event.target.closest("[data-remove-subscription]");
  if (!button) return;
  state.removeTarget = {
    umo: button.dataset.umo,
    username: button.dataset.username,
  };
  elements.removeDialogText.textContent = `确定移除 @${button.dataset.username}（${button.dataset.screenName}）吗？`;
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

async function start() {
  if (!bridge) {
    setError("当前页面未运行在 AstrBot Dashboard 中。" );
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
});
