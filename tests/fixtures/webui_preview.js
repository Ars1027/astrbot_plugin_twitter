// Development fixture only. Never loaded by the production page.
(() => {
  const author = (username, screen_name = username) => ({username, screen_name, enabled: true, r18: false, media_only: false});
  const groups = [
    {umo: "demo:GroupMessage:10001", group_id: "10001", group_name: "设计与开发交流组", platform_id: "QQ · 主机器人", available: true,
      subscriptions: [author("AstrBot", "AstrBot 开发动态"), author("OpenAI", "OpenAI"), author("design_notes", "设计手记 · Design Notes"),
        ...Array.from({length: 112}, (_, i) => author(`creator_${i + 1}`, i ? `创作者 ${String(i + 1).padStart(3, "0")}` : '超长名称 <img src=x onerror=alert(1)> & 🌿 设计、科技与日常灵感记录'))]},
    {umo: "demo:GroupMessage:10002", group_id: "10002", group_name: "灵感收集室", platform_id: "QQ · 主机器人", available: true, subscriptions: []},
    {umo: "backup:GroupMessage:10003", group_id: "10003", group_name: "未连接群聊", platform_id: "QQ · 备用机器人", available: false, subscriptions: [author("archive", "历史订阅")]},
  ];
  const other_sessions = [{umo: "demo:FriendMessage:20001", session_id: "20001", session_name: "私聊会话", platform_id: "QQ · 主机器人", subscriptions: [author("personal", "个人关注")]}];
  let interval = 10;
  let isDark = false;
  let contextHandler;
  const attempts = new Map();
  const delay = () => new Promise((resolve) => setTimeout(resolve, 600));
  window.AstrBotPluginPage = {
    ready: async () => ({isDark}),
    onContext: (handler) => { contextHandler = handler; },
    apiGet: async () => {
      await delay();
      const subs = [...groups, ...other_sessions].flatMap((g) => g.subscriptions);
      return structuredClone({groups, other_sessions, group_sources: [], provider: {name: "fxtwitter", ready: true}, polling: {running: true, interval_minutes: interval},
        totals: {groups: groups.length, authors: new Set(subs.map((s) => s.username)).size, subscriptions: subs.length, active: subs.filter((s) => s.enabled).length}});
    },
    apiPost: async (endpoint, payload) => {
      await delay();
      if (endpoint === "settings/poll-interval") { interval = payload.minutes; return {saved: true}; }
      const group = [...groups, ...other_sessions].find((g) => g.umo === payload.umo);
      if (!group) throw new Error("会话不存在");
      const existing = group.subscriptions.find((s) => s.username.toLowerCase() === payload.username?.toLowerCase());
      if (endpoint === "subscriptions/recent") {
        if (!existing) throw new Error("当前会话不存在这个订阅");
        if (existing.username === "design_notes" && !attempts.has("history-error")) {
          attempts.set("history-error", 1);
          throw new Error("模拟记录读取失败，请重试");
        }
        const items = group.group_id === "10001" && ["AstrBot", "OpenAI"].includes(existing.username)
          ? Array.from({length: existing.username === "AstrBot" ? 5 : 1}, (_, i) => ({
              tweet_id: String(1234567890000000 + i),
              delivered_at: new Date(Date.now() - i * 3600000).toISOString(),
              text: i === 1 ? "这是一条较长的推文摘要，包含界面中的安全文本 <img src=x> 与 Emoji 🌿。".repeat(12).slice(0, 500)
                : ["新版本开发进展：更清晰的订阅管理，以及面向每个会话的最近推送记录。", "", "今天的灵感记录：把复杂的事情做得简单一些。", "分享一组新的设计与开发工具。", "欢迎关注 AstrBot 社区动态。"][i],
              truncated: i === 1, is_retweet: i === 3,
            })) : [];
        return {items};
      }
      if (endpoint === "subscriptions/add") {
        if (!group.available) throw new Error("群聊不可用");
        if (existing) throw new Error("该推主已经订阅");
        attempts.set(payload.username, (attempts.get(payload.username) || 0) + 1);
        if (payload.username === "retry_demo" && attempts.get(payload.username) === 1) throw new Error("模拟请求超时 <请核对>");
        group.subscriptions.push({...author(payload.username), r18: payload.r18, media_only: payload.media_only});
        return {saved: true};
      }
      if (endpoint === "subscriptions/remove") group.subscriptions = group.subscriptions.filter((s) => s !== existing);
      if (endpoint === "subscriptions/update" && existing) Object.assign(existing, payload);
      if (endpoint === "subscriptions/group-status") group.subscriptions.forEach((s) => { s.enabled = payload.enabled; });
      return {saved: true};
    },
  };
  const bar = document.createElement("div");
  bar.style.cssText = "position:fixed;bottom:8px;left:8px;z-index:30;display:flex;gap:6px;padding:5px 8px;background:#17243b;color:white;border-radius:8px;font:11px system-ui";
  const label = document.createElement("span");
  label.textContent = "本地模拟 · 不连接真实群";
  const button = document.createElement("button");
  button.textContent = "切换明暗";
  button.addEventListener("click", () => { isDark = !isDark; contextHandler?.({isDark}); });
  bar.append(label, button);
  document.body.append(bar);
})();
