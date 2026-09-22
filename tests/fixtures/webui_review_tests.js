// Browser DOM regression harness; injected only by the local preview server.
(() => {
  const attack = '\"><img data-xss-probe src=x onerror="document.body.dataset.xss=1"> & <script>bad()</script>';
  const group = {umo: `bot:GroupMessage:100'${attack}`, group_id: "100", group_name: attack,
    platform_id: attack, available: true,
    subscriptions: [{username: "AstrBot", screen_name: attack, enabled: true, r18: false, media_only: false}]};
  const overview = {groups: [group], other_sessions: [], group_sources: [],
    provider: {name: "fxtwitter", ready: true}, polling: {running: true, interval_minutes: 5},
    totals: {groups: 1, authors: 1, subscriptions: 1, active: 1}};
  let finishRequest;
  let failNext = false;
  const calls = [];
  window.AstrBotPluginPage = {
    ready: async () => ({isDark: false}),
    apiGet: async () => structuredClone(overview),
    apiPost: async (endpoint, payload) => {
      if (endpoint === "subscriptions/recent") return {items: [
        {tweet_id: "123", text: attack, delivered_at: "2026-09-22T00:00:00Z"},
        {tweet_id: `123\" onclick=\"bad()`, text: attack, delivered_at: "invalid"},
      ]};
      if (endpoint === "subscriptions/add") {
        calls.push(payload);
        if (failNext) throw new Error(attack);
        await new Promise((resolve) => { finishRequest = resolve; });
        group.subscriptions.push({username: payload.username, screen_name: payload.username, enabled: true});
      }
      return {saved: true};
    },
  };
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const waitFor = async (predicate) => {
    const deadline = Date.now() + 5000;
    while (!predicate()) {
      if (Date.now() > deadline) throw new Error("等待页面状态超时");
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  };
  const select = (query) => document.querySelector(query);
  const fill = (query, value) => {
    select(query).value = value;
    select(query).dispatchEvent(new Event("input", {bubbles: true}));
  };
  const safe = () => {
    assert(!select("[data-xss-probe]"), "外部内容被解析成 HTML 元素");
    assert(!document.body.dataset.xss, "注入代码被执行");
    assert(!select(".page-shell [onerror], .page-shell [onclick], dialog [onerror], dialog [onclick]"), "外部属性逃逸");
  };

  window.addEventListener("DOMContentLoaded", async () => {
    const results = document.createElement("pre");
    results.id = "review-test-results";
    results.style.cssText = "position:fixed;left:8px;bottom:8px;background:#142238;color:#fff;padding:16px;z-index:100;max-width:95vw;white-space:pre-wrap";
    document.body.append(results);
    let passed = 0;
    const pass = (label) => { results.textContent += `PASS ${++passed}: ${label}\n`; };
    try {
      await waitFor(() => select("#open-batch") && !select("#open-batch").disabled);
      safe();
      assert(select(".session-name").textContent === attack, "会话名称应显示原始文本");
      assert(select(".session-item").dataset.umo === group.umo, "UMO 属性应保持完整");
      assert(select(".detail-heading h2").textContent === attack, "详情名称应显示原始文本");
      assert(select(".author-name").textContent === attack, "推主名称应显示原始文本");
      assert(select("[data-remove-subscription]").dataset.screenName === attack, "删除按钮属性应保持完整");
      pass("会话、详情与订阅行中的外部名称及属性安全渲染");

      fill("#author-search", attack);
      select("#refresh-button").click();
      await waitFor(() => !select("#refresh-button").disabled);
      assert(select("#author-search").value === attack, "搜索值在重绘后应保持原文");
      safe();
      pass("搜索值不会逃逸 value 属性");

      select("[data-show-history]").click();
      await waitFor(() => select(".history-entry"));
      safe();
      assert(select(".history-text").textContent === attack, "历史正文应显示原始文本");
      assert(document.querySelectorAll(".history-original").length === 1, "非法 ID 不应生成链接");
      assert(select(".history-original").getAttribute("href") === "https://x.com/i/status/123", "原帖链接只能指向固定站点和数字 ID");
      select("#history-close").click();
      pass("推送历史安全渲染，恶意 ID 不生成链接");

      select("#open-batch").click();
      fill("#batch-input", attack);
      safe();
      assert(select("#batch-start").disabled, "恶意输入不得成为有效账号");
      pass("批量输入预览只显示文本");

      fill("#batch-input", "one two three");
      select("#batch-start").click();
      await waitFor(() => Boolean(finishRequest));
      assert(!select("#batch-stop").disabled, "当前请求运行期间必须能停止后续添加");
      assert(select("#batch-input").disabled && select("#batch-r18").disabled && select("#batch-media").disabled, "批量输入及选项应锁定");
      assert(select(".session-item").disabled, "执行期间应禁止切换会话");
      select("#batch-stop").click();
      assert(select("#batch-stop").disabled, "已请求停止后避免重复点击");
      finishRequest();
      await waitFor(() => !select("#batch-retry").hidden);
      assert(calls.length === 1, "停止后不得调度第二条请求");
      assert(document.querySelectorAll('.batch-entry[data-status="pending"]').length === 2, "未执行项必须保留");
      assert(calls[0].umo === group.umo, "请求应保持原目标会话");
      pass("真实页面停止按钮可用，只完成当前请求，保留后续项");

      select("#batch-reset").click();
      // Guard future layout changes that move the dialog under the page shell.
      select(".page-shell").append(select("#batch-dialog"));
      fill("#batch-input", "four five");
      finishRequest = null;
      select("#batch-start").click();
      await waitFor(() => Boolean(finishRequest));
      assert(!select("#batch-stop").disabled, "弹窗移入 page-shell 后停止按钮也应保持可用");
      select("#batch-stop").click();
      finishRequest();
      await waitFor(() => !select("#batch-retry").hidden);
      assert(calls.length === 2, "移动布局后停止仍不能调度后续请求");
      pass("弹窗内嵌布局下仍保留停止能力");

      for (const settleWhileHidden of [true, false]) {
        select("#batch-reset").click();
        fill("#batch-input", settleWhileHidden ? "hidden_one hidden_two" : "return_one return_two");
        finishRequest = null;
        const beforeCalls = calls.length;
        select("#batch-start").click();
        await waitFor(() => Boolean(finishRequest));
        window.dispatchEvent(new PageTransitionEvent("pagehide", {persisted: true}));
        if (settleWhileHidden) {
          finishRequest();
          // Let the request and executeBatch finally settle while UI updates are suppressed.
          await new Promise((resolve) => setTimeout(resolve, 0));
        }
        window.dispatchEvent(new PageTransitionEvent("pageshow", {persisted: true}));
        if (!settleWhileHidden) {
          assert(select("#batch-close").disabled, "返回时仍在请求中应继续锁定弹窗");
          finishRequest();
        }
        await waitFor(() => !select("#batch-close").disabled && !select(".session-item").disabled);
        assert(!select("#batch-retry").hidden, "恢复后应能重试未执行项");
        assert(calls.length === beforeCalls + 1, "页面恢复不能自动重启队列");
        assert(document.querySelectorAll('.batch-entry[data-status="pending"]').length === 1, "恢复后保留未执行项");
        pass(settleWhileHidden ? "隐藏期间请求完成后恢复控件" : "恢复后等待当前请求结束再解锁");
      }

      select("#batch-reset").click();
      fill("#batch-input", "failed_user");
      failNext = true;
      select("#batch-start").click();
      await waitFor(() => !select("#batch-retry").hidden);
      safe();
      assert(select(".batch-entry p").textContent.includes(attack), "接口错误应显示原始文本");
      select("#batch-close").click();
      pass("接口错误内容只显示为文本");
      results.dataset.result = "passed";
      results.textContent += `${passed} 项浏览器 DOM 回归全部通过`;
    } catch (error) {
      results.dataset.result = "failed";
      results.textContent += `FAIL: ${error.message}`;
    }
  });
})();
