const $ = (selector) => document.querySelector(selector);

const state = {
  busy: false,
};

function setToday() {
  const now = new Date();
  const text = now.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
  $("#todayText").textContent = text;

  const hour = Number(now.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", hour12: false }));
  let mode = "盘前";
  if (hour >= 15) mode = "盘后";
  if (hour >= 9 && hour < 15) mode = "盘中";
  $("#marketMode").textContent = mode;
}

function tag(label) {
  const span = document.createElement("span");
  span.className = "tag";
  span.textContent = label;
  return span;
}

function warn(messages) {
  if (!messages || !messages.length) return;
  $("#warningPanel").querySelector("p").textContent = messages.filter(Boolean).slice(0, 3).join("；");
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json();
}

async function loadPreMarket(refresh = false) {
  $("#refreshPre").classList.add("loading");
  try {
    const data = await getJson(`/api/pre-market${refresh ? "?refresh=1" : ""}`);
    $("#preSummary").textContent = data.summary || "暂无摘要。";
    $("#preUpdated").textContent = data.updated_at ? `更新：${data.updated_at}` : "等待更新";
    $("#positiveCount").textContent = Array.isArray(data.positive) ? data.positive.length : 0;
    $("#negativeCount").textContent = Array.isArray(data.negative) ? data.negative.length : 0;
    $("#stockCount").textContent = Array.isArray(data.stocks) ? data.stocks.length : 0;
    $("#aiStatus").textContent = data.ai_enabled ? "AI已连接" : "未配置AI";

    const tags = $("#sectorTags");
    tags.innerHTML = "";
    const sectors = Array.isArray(data.sectors) ? data.sectors : [];
    const stocks = Array.isArray(data.stocks) ? data.stocks : [];
    [...sectors.slice(0, 6), ...stocks.slice(0, 4)].forEach((item) => tags.appendChild(tag(item)));
    if (!tags.children.length) tags.appendChild(tag("等待主线"));
    warn(data.warnings);
  } catch (error) {
    $("#preSummary").textContent = error.message;
  } finally {
    $("#refreshPre").classList.remove("loading");
  }
}

function money(value) {
  const num = Number(value || 0);
  if (Math.abs(num) >= 100000000) return `${(num / 100000000).toFixed(2)}亿`;
  if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(0)}万`;
  return `${num.toFixed(0)}`;
}

function renderStocks(items, warnings) {
  const list = $("#stockList");
  list.innerHTML = "";
  if (!items || !items.length) {
    const div = document.createElement("div");
    div.className = "empty-state";
    div.textContent = warnings && warnings.length ? warnings[0] : "当前严格条件未筛出股票。";
    list.appendChild(div);
    return;
  }

  items.forEach((item) => {
    const row = document.createElement("article");
    row.className = "stock-row";
    const changeClass = Number(item.change_pct) >= 0 ? "change-up" : "change-down";
    row.innerHTML = `
      <div class="stock-main">
        <div>
          <div class="stock-name">${item.name}</div>
          <div class="stock-code">${item.code}${item.lhb ? " · 龙虎榜" : ""}</div>
        </div>
        <div class="${changeClass}">${Number(item.change_pct).toFixed(2)}%</div>
      </div>
      <div class="metric-row">
        <div class="metric"><span>主力净流入</span><strong>${money(item.main_net_inflow)}</strong></div>
        <div class="metric"><span>量比</span><strong>${Number(item.volume_ratio).toFixed(2)}</strong></div>
        <div class="metric"><span>PE</span><strong>${Number(item.pe).toFixed(1)}</strong></div>
      </div>
      <p class="comment">${item.ai_comment || "等待AI点评。"}</p>
      <p class="risk">${item.risk || "严格止损，不追高。"}</p>
    `;
    list.appendChild(row);
  });
}

async function runScreen() {
  if (state.busy) return;
  state.busy = true;
  $("#runScreen").classList.add("loading");
  $("#stockList").innerHTML = `<div class="empty-state">正在抓取行情、资金、龙虎榜和业绩数据，可能需要几十秒。</div>`;
  try {
    const data = await getJson("/api/screen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sort: $("#sortSelect").value }),
    });
    $("#screenUpdated").textContent = data.updated_at ? `更新：${data.updated_at}` : "已完成";
    renderStocks(data.items, data.warnings);
    warn(data.warnings);
  } catch (error) {
    renderStocks([], [error.message]);
  } finally {
    state.busy = false;
    $("#runScreen").classList.remove("loading");
  }
}

async function runAfterHours() {
  $("#runAfter").classList.add("loading");
  try {
    const data = await getJson("/api/after-hours?refresh=1");
    const box = $("#afterContent");
    if (!data.available) {
      box.textContent = data.message || "盘后复盘暂不可用。";
      return;
    }
    $("#afterUpdated").textContent = data.updated_at ? `更新：${data.updated_at}` : "已完成";
    const perf = Array.isArray(data.performance) ? data.performance : [];
    const perfHtml = perf.length
      ? perf.map((item) => `<div class="review-item">${item.name} ${item.code}：${Number(item.change_pct).toFixed(2)}%，收盘 ${item.close_price}</div>`).join("")
      : `<div class="review-item">今日没有可统计的推荐股票。</div>`;
    box.innerHTML = `
      <div class="review-item"><strong>大盘：</strong>${data.market?.summary || "暂无"}</div>
      ${perfHtml}
      <div class="review-item"><strong>结论：</strong>${data.ai?.conclusion || "先观察。"}</div>
      <div class="review-item"><strong>明日：</strong>${data.ai?.tomorrow_plan || "空仓优先。"}</div>
      <div class="review-item"><strong>风险：</strong>${data.ai?.risk || "不构成投资建议。"}</div>
    `;
    warn(data.warnings);
  } catch (error) {
    $("#afterContent").textContent = error.message;
  } finally {
    $("#runAfter").classList.remove("loading");
  }
}

setToday();
loadPreMarket(false);
$("#refreshPre").addEventListener("click", () => loadPreMarket(true));
$("#runScreen").addEventListener("click", runScreen);
$("#runAfter").addEventListener("click", runAfterHours);
$("#sortSelect").addEventListener("change", runScreen);
