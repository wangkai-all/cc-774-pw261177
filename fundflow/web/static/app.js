// 资金往来闭合分析前端（原生 JS，无构建、无依赖）
// 语义提醒：链条仅为资金关系的数学解释，不构成违法事实认定。

const state = { data: null, activeSolution: 0, drawerChain: null };

async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const resp = await fetch(path, opt);
  const payload = await resp.json();
  if (!resp.ok) {
    showConflict(payload);
    throw new Error(payload.error || "请求失败");
  }
  return payload;
}

function yuan(cents) {
  const neg = cents < 0;
  const v = Math.abs(cents);
  return (neg ? "-" : "") + Math.floor(v / 100) + "." + String(v % 100).padStart(2, "0");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function load() {
  state.data = await api("/api/state");
  state.activeSolution = 0;
  render();
}

function activeDraft() {
  const d = state.data;
  return d.drafts[state.activeSolution] || d.drafts[0] || null;
}

function render() {
  const d = state.data;
  document.getElementById("version").textContent = d.version;
  document.getElementById("objective").value = d.objective;
  document.getElementById("disclaimer").textContent = d.disclaimer;

  // 冲突与退款错误横幅
  renderBanners(d);
  renderTimeline(d);
  renderSolutions(d);
  renderConfirmed(d);
}

function renderBanners(d) {
  const conflict = document.getElementById("conflict-banner");
  if (d.conflicts && d.conflicts.length) {
    conflict.classList.remove("hidden");
    conflict.innerHTML = "<b>约束冲突：</b><br>" +
      d.conflicts.map(c => "· " + esc(c.message)).join("<br>");
  } else conflict.classList.add("hidden");

  const refund = document.getElementById("refund-banner");
  if (d.refund_errors && d.refund_errors.length) {
    refund.classList.remove("hidden");
    refund.innerHTML = "<b>退款校验未通过（已跳过，不影响其他分析）：</b><br>" +
      d.refund_errors.map(e => "· " + esc(e)).join("<br>");
  } else refund.classList.add("hidden");
}

function renderTimeline(d) {
  const el = document.getElementById("timeline");
  const sol = activeDraft();
  // 当前解下每笔流水的累计已分配额
  const usage = {};
  if (sol) {
    const chainIndex = Object.fromEntries(d.chains.map(c => [c.chain_id, c]));
    for (const a of sol.allocations) {
      const ch = chainIndex[a.chain_id];
      if (!ch) continue;
      for (const t of ch.txn_ids) usage[t] = (usage[t] || 0) + a.amount_cents;
    }
  }
  el.innerHTML = d.transactions.map(t => {
    const used = usage[t.txn_id] || 0;
    const tags = [];
    if (t.is_refund) tags.push('<span class="tag tag-refund">退款</span>');
    if (t.excluded) tags.push('<span class="tag tag-excluded">已排除</span>');
    if (!t.account_allowed) tags.push('<span class="tag tag-blocked">受限账户</span>');
    const cls = "txn" + (t.is_refund ? " refund" : "") + (t.excluded ? " excluded" : "");
    const action = t.is_refund ? "" :
      `<button class="btn btn-sm ${t.excluded ? "btn-ok" : "btn-danger"}"
         data-txn="${esc(t.txn_id)}" data-act="${t.excluded ? "include" : "exclude"}">
         ${t.excluded ? "恢复" : "排除"}</button>`;
    return `<div class="${cls}" data-txn-card="${esc(t.txn_id)}">
      <div class="txn-date">${esc(t.date)}</div>
      <div class="txn-main">
        <div class="flow">${esc(t.from_account)} → ${esc(t.to_account)} ${tags.join("")}</div>
        <div class="meta">${esc(t.txn_id)} · 单据 ${esc(t.doc_ref || "—")}
          · 剩余 ${yuan(t.remaining_cents)} 元 ${esc(t.memo || "")}</div>
      </div>
      <div class="txn-amount">
        <div class="yuan">¥${yuan(t.amount_cents)}</div>
        <div class="usage">已分配 ${yuan(used)} / 剩余 ${yuan(t.remaining_cents)}</div>
        ${action}
      </div>
    </div>`;
  }).join("");

  el.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const txn = btn.dataset.txn;
      try {
        state.data = await api(`/api/${btn.dataset.act}`, { txn_id: txn });
        state.activeSolution = 0;
        render();
      } catch (_) { /* 冲突已在横幅展示 */ }
    });
  });
}

function renderSolutions(d) {
  const tabs = document.getElementById("solution-tabs");
  const list = document.getElementById("chain-list");
  const summary = document.getElementById("solution-summary");
  if (!d.drafts.length) {
    tabs.innerHTML = "";
    summary.textContent = "当前约束下没有可闭合的往来链。";
    list.innerHTML = "";
    return;
  }
  tabs.innerHTML = d.drafts.map((s, i) =>
    `<button class="sol-tab ${i === state.activeSolution ? "active" : ""}" data-i="${i}">
       解释 ${i + 1} · ${s.chain_count} 链</button>`).join("");
  tabs.querySelectorAll(".sol-tab").forEach(b =>
    b.addEventListener("click", () => {
      state.activeSolution = Number(b.dataset.i);
      render();
    }));

  const sol = activeDraft();
  summary.innerHTML =
    `目标：<b>${d.objective === "max_closure" ? "最大闭合金额" : "最少链条"}</b>
     ｜闭合总额 <b>¥${yuan(sol.total_closed_cents)}</b>
     ｜使用 ${sol.chain_count} 条链
     ｜锁定 ${sol.locked_chain_ids.length} 条`;

  const chainIndex = Object.fromEntries(d.chains.map(c => [c.chain_id, c]));
  list.innerHTML = sol.allocations.map(a => {
    const ch = chainIndex[a.chain_id];
    const path = ch ? ch.txn_ids.join(" → ") : "（链已失效）";
    return `<div class="chain-card ${a.locked ? "locked" : ""}" data-chain="${esc(a.chain_id)}">
      <div class="chain-title">
        <span class="cid">${esc(a.chain_id)} ${a.locked ? "🔒" : ""}</span>
        <span class="amt">¥${yuan(a.amount_cents)}</span>
      </div>
      <div class="chain-path">${esc(path)}</div>
    </div>`;
  }).join("");
  list.querySelectorAll(".chain-card").forEach(card =>
    card.addEventListener("click", () => openDrawer(card.dataset.chain)));
}

function renderConfirmed(d) {
  const el = document.getElementById("confirmed-list");
  if (!d.confirmed.length) {
    el.innerHTML = '<div class="confirmed-item">暂无已确认解释</div>';
    return;
  }
  el.innerHTML = d.confirmed.map(s =>
    `<div class="confirmed-item">
       ${esc(s.solution_id)} ｜ ${esc(s.objective)} ｜
       闭合 ¥${yuan(s.total_closed_cents)} ｜ ${s.chain_count} 链 ｜ v${s.version}
       ${s.note ? "<br>备注：" + esc(s.note) : ""}
     </div>`).join("");
}

async function openDrawer(chainId) {
  let detail;
  try {
    detail = await api(`/api/chains/${encodeURIComponent(chainId)}`);
  } catch (_) { return; }
  state.drawerChain = chainId;
  document.getElementById("drawer-mask").classList.remove("hidden");
  document.getElementById("drawer").classList.remove("hidden");
  document.getElementById("drawer-title").textContent =
    `链 ${chainId} 详情 ${detail.locked ? "🔒 已锁定" : ""}`;

  const body = document.getElementById("drawer-body");
  body.innerHTML = `
    <p class="hint">闭合链上每笔流水由该链使用的金额相同（流量守恒）；
       剩余金额 = 抵销退款后的额度，还可被其他链继续分配。</p>
    <div class="step" style="background:var(--accent-soft)">
      <div class="row"><span>本链分配金额</span><span class="used">¥${yuan(detail.allocation_cents)}</span></div>
      <div class="row"><span>所属解</span><span>${esc(detail.solution_id)}</span></div>
    </div>
    ${detail.steps.map((s, i) => `
      <div class="step">
        <div class="row"><span>第 ${i + 1} 笔</span><span>${esc(s.date)}</span></div>
        <div class="row"><span>流水</span><span>${esc(s.txn_id)}
          (${esc(s.from_account)} → ${esc(s.to_account)})</span></div>
        <div class="row"><span>原始金额</span><span>¥${yuan(s.amount_cents)}</span></div>
        <div class="row"><span>本链使用</span><span class="used">¥${yuan(s.used_by_this_chain_cents)}</span></div>
        <div class="row"><span>当前剩余</span><span class="remain">¥${yuan(s.remaining_cents)}</span></div>
      </div>`).join("")}
    <div class="lock-form">
      <input id="lock-amount" type="number" min="1" step="1"
             placeholder="锁定金额（整数分），当前 ${detail.allocation_cents}"
             value="${detail.allocation_cents || ""}">
      <button class="btn btn-lock" id="btn-lock">${detail.locked ? "修改锁定" : "锁定分配"}</button>
      ${detail.locked ? '<button class="btn btn-danger" id="btn-unlock">解除</button>' : ""}
    </div>
    <button class="btn btn-ok" id="btn-confirm" style="width:100%">确认当前解释（重算不可覆盖）</button>
  `;

  document.getElementById("btn-lock").addEventListener("click", async () => {
    const amt = Number(document.getElementById("lock-amount").value);
    try {
      state.data = await api("/api/lock", { chain_id: chainId, amount_cents: amt });
      state.activeSolution = 0;
      render();
      closeDrawer();
    } catch (_) {}
  });
  const unlock = document.getElementById("btn-unlock");
  if (unlock) unlock.addEventListener("click", async () => {
    try {
      state.data = await api("/api/unlock", { chain_id: chainId });
      render(); closeDrawer();
    } catch (_) {}
  });
  document.getElementById("btn-confirm").addEventListener("click", async () => {
    const sol = activeDraft();
    try {
      await api("/api/confirm", { solution_id: sol.solution_id, note: "网页端人工确认" });
      await load(); closeDrawer();
    } catch (_) {}
  });
}

function closeDrawer() {
  document.getElementById("drawer-mask").classList.add("hidden");
  document.getElementById("drawer").classList.add("hidden");
}

function showConflict(payload) {
  const el = document.getElementById("conflict-banner");
  const lines = payload.conflicts
    ? payload.conflicts.map(c => "· " + c.message)
    : ["· " + (payload.error || "未知错误")];
  el.classList.remove("hidden");
  el.innerHTML = "<b>操作被拒绝：</b><br>" + lines.map(esc).join("<br>");
}

document.getElementById("drawer-close").addEventListener("click", closeDrawer);
document.getElementById("drawer-mask").addEventListener("click", closeDrawer);
document.getElementById("objective").addEventListener("change", async (e) => {
  try {
    state.data = await api("/api/objective", { objective: e.target.value });
    state.activeSolution = 0;
    render();
  } catch (_) {}
});
document.getElementById("btn-recompute").addEventListener("click", async () => {
  try {
    state.data = await api("/api/recompute");
    state.activeSolution = 0;
    render();
  } catch (_) {}
});

load().catch(err => {
  document.getElementById("timeline").innerHTML =
    `<p style="color:var(--bad)">加载失败：${esc(err.message)}</p>`;
});
