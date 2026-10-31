/* Selark Dashboard: local sessions, explicit model approvals, no provider calls. */
"use strict";
let bootstrap = new URLSearchParams(location.hash.slice(1)).get("login");
if (location.hash || location.search) history.replaceState(null, "", location.pathname);

const $ = (s) => document.querySelector(s);
const escapeHTML = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const h = escapeHTML;
const number = (v) => v == null ? "未知" : new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(v);
const money = (v) => new Intl.NumberFormat("en", {maximumFractionDigits:9}).format(v);
const compact = (v) => v == null ? "—" : new Intl.NumberFormat("en", {notation:"compact", maximumFractionDigits:1}).format(v);
const time = (v) => v == null ? "尚无记录" : Number.isFinite(new Date(v).getTime()) ? new Date(v).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "时间未知";
const percent = (v) => v == null ? "—" : `${(v * 100).toFixed(1)}%`;
const duration = (v) => v == null ? "—" : v < 1000 ? `${Math.round(v)} ms` : `${(v/1000).toFixed(2)} s`;
const icons = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  resources:'<path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  chart:'<path d="M4 3v17h17M8 15v-4m5 4V7m5 8v-6"/>',
  apps:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 5V3m8 2V3M3 10h18M8 14h2m4 0h2m-8 3h2"/>',
  pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  moon:'<path d="M20 14a8 8 0 0 1-10-10 9 9 0 1 0 10 10Z"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18M8 15h2m4 0h2"/>',
  tokens:'<path d="m12 2 8 5v10l-8 5-8-5V7l8-5ZM4 7l8 5 8-5M12 12v10"/>',
  cost:'<circle cx="12" cy="12" r="9"/><path d="M15 8h-5a2 2 0 0 0 0 4h4a2 2 0 0 1 0 4H9m3-10v12"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
};
const icon = (name, size=18) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.resources}</svg>`;
document.querySelectorAll("[data-icon]").forEach(el => { el.innerHTML = icon(el.dataset.icon, 17); });

let theme = "system";
try { theme = localStorage.getItem("selark-theme") || "system"; } catch { /* Storage can be disabled. */ }
if (!["light","dark","system"].includes(theme)) theme = "system";
const media = matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  document.documentElement.dataset.theme = theme === "system" ? (media.matches ? "dark" : "light") : theme;
  $("#theme-label").textContent = {system:"跟随系统",light:"浅色主题",dark:"深色主题"}[theme];
}
applyTheme();
media.addEventListener("change", applyTheme);
$("#theme").addEventListener("click", () => {
  theme = {system:"light",light:"dark",dark:"system"}[theme];
  try { localStorage.setItem("selark-theme", theme); } catch { /* Preference only. */ }
  applyTheme();
});

const pages = {
  overview:["资源概览","YOUR AI, AT A GLANCE","连接、用量与运行状态，都在这里。"],
  resources:["连接与模型","ONE PLACE FOR EVERY CONNECTION","清楚了解每份资源的授权、历史验证与额度状态。"],
  usage:["用量分析","MAKE EVERY TOKEN VISIBLE","从真实调用记录，了解你的 AI 使用方式。"],
  applications:["应用管理","A CLEAR VIEW OF EVERY APPLICATION","各应用的访问权限、并发限制与本地配额。"],
  monitor:["运行监控","KEEP A PULSE ON YOUR WORKSPACE","观察本机服务、队列、错误与备份记录。"],
};
const labels = {api:"API",token_plan:"Token Plan",chatgpt_subscription:"ChatGPT 订阅",success:"成功",failed:"失败",cancelled:"已取消",interrupted:"已中断",running:"进行中",unknown:"未知",never:"从未验证",stale:"配置已变化",connected:"已授权",not_connected:"未授权",disconnected:"未授权",unreadable:"不可读",reported:"历史已报告",not_checked:"未检查",not_configured:"未配置",unreachable_or_invalid:"未响应 / 响应无效",degraded:"运行异常",sent:"已发送",pending:"待完成",estimated:"估算",not_priced:"未定价",blocked_unknown_usage:"未知用量，已暂停",exhausted:"已耗尽",unlimited:"未设上限",limited:"有限额",inconsistent:"计数矛盾",regressed:"计数回退",uncontradicted:"未发现矛盾"};
const label = (v) => labels[v] || v || "未知";
const stateColor = (v) => ["success","connected"].includes(v) ? "green" : ["failed","unreadable","degraded"].includes(v) ? "red" : ["stale","cancelled","interrupted","blocked_unknown_usage","exhausted"].includes(v) ? "amber" : "";
const badge = (text, color="") => `<span class="badge ${color}">${h(text)}</span>`;
const empty = (title, description, name="chart") => `<div class="empty"><span class="empty-icon">${icon(name,22)}</span><h3>${h(title)}</h3><p>${h(description)}</p></div>`;
let view="overview", metric="tokens", currency="", snapshot=null, requestRows=null, logs=null, backups=null;
let offset=0, errorsOnly=false, authorized=false, controller=null, requestVersion=0;
let detailConnection=null, modelBusy=false, modelSearch="", modelScope="all";
const canManageModels=()=>Boolean(snapshot?.capabilities?.model_approval);
let activeQuery = new URLSearchParams({period:"7d"});
const appName = (id) => snapshot?.applications.find(a => a.id === id)?.name || id || "未知应用";

async function api(path, signal) {
  const response = await fetch(`/api/${path}`, {headers:{"X-Selark-Dashboard":"1"}, credentials:"same-origin", cache:"no-store", signal});
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || "本地数据读取失败。");
    error.status = response.status;
    throw error;
  }
  return body;
}
function notice(message) { $("#notice").textContent=message; $("#notice").hidden=!message; }
function loginRequired(message="从运行 selark dashboard 的本机终端，打开一次性管理链接。") {
  authorized=false; snapshot=null; requestRows=null; logs=null; backups=null;
  $("#filters").hidden=true;
  if ($("#details").open) $("#details").close();
  $("#detail-content").replaceChildren();
  $("#content").innerHTML=`<section class="panel error-state">${empty("需要本机管理会话",message,"lock")}<div class="empty"><code>selark dashboard</code><p>链接有效期 5 分钟，只可使用一次。管理会话最长 8 小时。</p></div></section>`;
  $("#freshness").textContent="未登录";
}
function setOptions(id, values, text) {
  const element=$(`#${id}`), current=element.value;
  const options = `<option value="">${text}</option>` + values.map(([value,name]) => `<option value="${h(value)}">${h(name)}</option>`).join("");
  if (element.innerHTML !== options) { element.innerHTML=options; element.value=current; }
}
function populateFilters() {
  setOptions("caller_id",snapshot.applications.map(a => [a.id,a.name]),"全部应用");
  const connectionIds=[...new Set([...snapshot.connections.map(c=>c.id), ...snapshot.options.connections])].sort();
  setOptions("connection_id",connectionIds.map(id=>[id,snapshot.connections.find(c=>c.id===id)?.name || id]),"全部连接");
  const models=[...new Set([...snapshot.connections.flatMap(c=>c.models),...snapshot.options.models])].sort();
  setOptions("model",models.map(m=>[m,m]),"全部模型");
}
async function refresh() {
  if (!authorized) return;
  controller?.abort(); controller=new AbortController();
  const currentController=controller, signal=controller.signal, version=++requestVersion;
  const deadline=setTimeout(()=>currentController.abort(new DOMException("本地读取超时，请稍后刷新。", "TimeoutError")),20000);
  $("#refresh").disabled=true;
  $("#freshness").textContent="读取中…";
  const query=activeQuery.toString();
  try {
    const next = await api(`snapshot?${query}`,signal);
    if (version !== requestVersion) return;
    snapshot=next;
    $("#dashboard-mode").textContent=canManageModels()?"模型管理":"只读模式";
    $("#access-mode").textContent=canManageModels()?"独立管理会话 · 模型启用管理":"独立管理会话 · 只读访问";
    populateFilters();
    $("#version").textContent=`v${snapshot.version}`;
    notice(snapshot.issues.join(" "));
    if (["overview","usage","monitor"].includes(view)) {
      const recordFilters=new URLSearchParams(activeQuery);
      if(next.usage){recordFilters.set("period","custom");recordFilters.set("since",next.usage.filters.since);recordFilters.set("until",next.usage.filters.until);}
      recordFilters.set("offset",offset);recordFilters.set("errors",errorsOnly);
      const recordsQuery=recordFilters.toString();
      const results = await Promise.allSettled([
        api(`records?${recordsQuery}`,signal),
        ...(view === "monitor" ? [api("logs",signal),api("backups",signal)] : []),
      ]);
      if (version !== requestVersion) return;
      const expired=results.find(r=>r.status==="rejected" && r.reason.status===401);
      if (expired) throw expired.reason;
      requestRows=results[0].status==="fulfilled" ? results[0].value : null;
      if (view === "monitor") {
        logs=results[1].status==="fulfilled" ? results[1].value : null;
        backups=results[2].status==="fulfilled" ? results[2].value : null;
      }
      if (results.some(r=>r.status==="rejected")) notice("部分记录暂时不可读；可稍后刷新，或在终端检查 selark status --offline。");
    }
    render();
    $("#freshness").textContent=`快照 ${time(snapshot.checked_at)}`;
    return true;
  } catch (error) {
    if (error.name === "AbortError" || version !== requestVersion) return;
    if (error.status===401) loginRequired(error.message);
    else {
      notice(`${error.message} ${snapshot ? "下方保留的是上次成功读取的快照。" : ""}`);
      $("#freshness").textContent="刷新失败 · 数据可能过期";
      if (!snapshot) $("#content").innerHTML=`<section class="panel">${empty("暂时无法读取工作空间","请确认 Dashboard 进程仍在运行，再点击刷新。","pulse")}</section>`;
    }
  } finally {
    clearTimeout(deadline);
    if (version === requestVersion) $("#refresh").disabled=false;
  }
  return false;
}
function metrics() {
  const s=snapshot.usage?.summary;
  if (!s) return "";
  const costs=Object.entries(s.estimated_cost_by_currency);
  const cost=costs.length ? costs.map(([c,v])=>`${h(c)} ${h(money(v))}`).join("<br>") : "—";
  const values=[
    ["已报告 Token",h(compact(s.known_tokens.total_tokens)),"tokens",`${number(s.known_usage_requests)} 次完整用量 · ${number(s.unknown_consumption_requests)} 次消耗未知`,"accent"],
    ["调用次数",h(number(s.requests)),"chart",`${number(s.upstream_attempts)} 次已记录上游尝试`,""],
    ["费用估算",cost,"cost",costs.length ? `${number(s.unknown_cost_requests)} 次费用未知 · 非账单` : "尚无可估算费用 · 非零费用",""],
    ["完成成功率",h(percent(s.success_rate)),"check",`${number(s.completed_requests)} 次已知完成状态`,""],
    ["平均耗时",h(duration(s.average_latency_ms)),"clock",`${number(s.latency_samples)} 条有效延迟记录`,""],
  ];
  return `<section class="metrics" aria-label="所选范围的用量汇总">${values.map(([name,value,glyph,note,accent])=>`<article class="metric ${accent}"><div class="metric-label">${h(name)}${icon(glyph)}</div><div class="metric-value ${glyph==="cost" ? "cost" : ""}">${value}</div><div class="metric-note">${h(note)}</div></article>`).join("")}</section>`;
}
const metricNames={tokens:"Token",requests:"调用",cost:"费用估算",success:"成功率",latency:"耗时"};
function pointValue(point) {
  return metric==="tokens" ? point.known_tokens.total_tokens : metric==="requests" ? point.requests : metric==="cost" ? (point.estimated_cost_by_currency[currency] == null ? null : Number(point.estimated_cost_by_currency[currency])) : metric==="success" ? (point.success_rate==null ? null : point.success_rate*100) : point.average_latency_ms;
}
function valueLabel(value) { return value==null ? "未知 / 无记录" : metric==="success" ? `${value.toFixed(1)}%` : metric==="latency" ? duration(value) : metric==="cost" ? `${currency} ${money(value)}` : number(value); }
function chartSVG(points) {
  const values=points.map(pointValue), available=values.filter(v=>v!=null && Number.isFinite(v));
  if (!available.length) return empty("此指标暂无已知数据","所选范围内没有可绘制的值，未知值不会按零处理。");
  const mainStyle=getComputedStyle($("#main"));
  const mainWidth=$("#main").clientWidth-parseFloat(mainStyle.paddingLeft)-parseFloat(mainStyle.paddingRight);
  const width=Math.max(280,Math.min(900,(innerWidth>1000?(mainWidth-22)*.64:mainWidth)-40));
  const height=240,left=48,right=15,top=20,bottom=38,plotW=width-left-right,plotH=height-top-bottom;
  const max=Math.max(...available,1)*(metric==="success" ? 1 : 1.15), step=plotW/points.length;
  const y=(v)=>top+plotH*(1-v/max), x=(i)=>left+step*(i+.5);
  let content="";
  for (let i=0;i<5;i++) {
    const value=max*i/4, pos=y(value);
    content+=`<line x1="${left}" x2="${width-right}" y1="${pos}" y2="${pos}" class="chart-grid"/><text x="${left-10}" y="${pos+3}" class="chart-text" text-anchor="end">${h(metric==="success" ? `${Math.round(value)}%` : metric==="cost" ? money(value) : compact(value))}</text>`;
  }
  let path="",connected=false;
  values.forEach((value,i)=>{
    if (value==null || !Number.isFinite(value)) { connected=false; return; }
    const title=`${points[i].day} UTC · ${metricNames[metric]} ${valueLabel(value)} · ${points[i].requests} 次调用`;
    if (["tokens","requests","cost"].includes(metric)) {
      const bar=Math.max(1,Math.min(36,step*.55)), barH=plotH*(value/max);
      content+=`<rect x="${x(i)-bar/2}" y="${y(value)}" width="${bar}" height="${Math.max(1,barH)}" rx="${Math.min(3,bar/3)}" class="chart-bar" tabindex="0" role="img" aria-label="${h(title)}"><title>${h(title)}</title></rect>`;
    } else {
      path+=`${connected ? "L" : "M"}${x(i)} ${y(value)} `; connected=true;
      content+=`<circle cx="${x(i)}" cy="${y(value)}" r="3.5" class="chart-dot" tabindex="0" role="img" aria-label="${h(title)}"><title>${h(title)}</title></circle>`;
    }
  });
  if (path) content=`<path d="${path}" class="chart-path"/>`+content;
  const stride=Math.max(1,Math.ceil(points.length/7));
  points.forEach((point,i)=> { if(i%stride===0 || i===points.length-1 && points.length%stride>1) content+=`<text x="${x(i)}" y="${height-12}" class="chart-text" text-anchor="middle">${h(point.day.slice(5).replace("-","/"))}</text>`; });
  return `<svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${h(metricNames[metric])}每日历史趋势，按 UTC 分组"><title>${h(metricNames[metric])}历史趋势；完整数值见下方数据表</title>${content}</svg>`;
}
function chart() {
  const usage=snapshot.usage;
  if (!usage) return `<section class="panel">${empty("用量账本暂不可读","Dashboard 不会自动迁移或修复账本，请在终端检查现有配置。")}</section>`;
  const currencies=Object.keys(usage.summary.estimated_cost_by_currency);
  if (!currencies.includes(currency)) currency=currencies[0] || "USD";
  return `<section class="panel"><div class="panel-header"><div><h2>使用趋势</h2><p class="panel-subtitle">每一次调用，都有迹可循</p></div>${badge("历史记录")}</div><div class="chart-heading"><div class="segments" role="group" aria-label="图表指标">${Object.entries(metricNames).map(([key,name])=>`<button data-metric="${key}" aria-pressed="${metric===key}" class="${metric===key?"active":""}">${name}</button>`).join("")}</div><div class="chart-meta"><span class="local-dot"></span>${h(metricNames[metric])}${metric==="cost" && currencies.length ? `<select id="currency" aria-label="费用币种">${currencies.map(c=>`<option ${c===currency?"selected":""}>${h(c)}</option>`).join("")}</select>` : ""}<span>UTC / 天</span></div></div><div class="chart-wrap">${usage.summary.requests ? chartSVG(usage.trend) : empty("这个时段还没有调用记录","开始使用 Selark 后，这里会呈现真实的使用趋势。")}</div><div class="chart-foot"><span>${metric==="cost" ? "按请求时价格估算；币种独立统计，未定价不计为零。" : metric==="success" ? "成功 ÷ 已知完成状态；不包含进行中与状态未知。" : metric==="latency" ? "网关端到端平均耗时，包含排队；不代表首 Token 延迟。" : "仅汇总账本已知值；未知消耗保留，不推算。"}</span><span>${h(usage.unknown_time_excluded)} 条时间未知记录未纳入</span></div><details class="chart-table"><summary>查看图表数据与统计口径</summary><p class="panel-subtitle">时间区间包含开始、不包含结束。首尾日期可能是不完整的一天。空日期无记录；未知值保持空缺。成功率分母包含成功、失败、取消和中断。</p><div class="table-wrap"><table><thead><tr><th>日期 · UTC</th><th>${h(metricNames[metric])}</th><th>调用次数</th><th>未知消耗</th></tr></thead><tbody>${usage.trend.map(p=>`<tr><td>${h(p.day)}</td><td>${h(valueLabel(pointValue(p)))}</td><td>${h(number(p.requests))}</td><td>${h(number(p.unknown_consumption_requests))}</td></tr>`).join("")}</tbody></table></div></details></section>`;
}
function availability(c) { return c.availability.state==="blocked_locally" ? "本地受阻" : "实时可用性未知"; }
function authorization(c) { const a=c.authorization; return a.state==="unreadable" ? "本地状态不可读" : a.kind==="oauth" ? a.access_expired ? "已到期，待刷新" : a.ready ? "本地授权就绪" : "尚未授权 / 未就绪" : "已配置 Key"; }
function resourceList() {
  const resources=snapshot.connections;
  return `<section class="panel"><div class="panel-header"><div><h2>资源池<span class="count-badge">${resources.length}</span></h2><p class="panel-subtitle">全部连接 · 与用量筛选无关${resources.length>5?" · 可滚动查看":""}</p></div><button class="text-button" data-navigate="resources">查看全部 ${icon("arrow",14)}</button></div>${resources.length ? `<div class="resource-list">${resources.map((c)=>`<div class="resource-row"><div class="provider-icon ${c.resource_type==="chatgpt_subscription"?"subscription":""}">${h(c.provider.slice(0,2).toUpperCase())}</div><div class="resource-info"><button class="text-button resource-name" data-connection="${h(c.id)}">${h(c.name)}</button><div class="resource-sub">${h(label(c.resource_type))} · 已发现 ${c.discovery?.discovered_at ? c.discovery.count : "—"} / 已启用 ${c.models.length}</div></div><div class="resource-state">${badge(c.enabled ? "已启用" : "已停用", c.enabled?"green":"")}<span>${h(availability(c))}</span></div></div>`).join("")}</div>` : empty("尚未配置连接","已有资源会自动出现在这里。","resources")}<div class="chart-foot">本地授权与历史成功，均不代表上游当前可用。</div></section>`;
}
function recordsPanel() {
  const items=view==="overview" ? (requestRows?.items || []).slice(0,6) : requestRows?.items || [];
  return `<section class="panel"><div class="panel-header"><div><h2>${errorsOnly ? "错误记录" : "最近调用"}${requestRows?`<span class="count-badge">${number(requestRows.total)}</span>`:""}</h2><p class="panel-subtitle">当前筛选范围 · 时间按浏览器本地时区显示</p></div><button id="toggle-errors" class="text-button">${errorsOnly?"查看全部调用":"只看异常"}</button></div>${items.length ? `<div class="table-wrap"><table><thead><tr><th>模型 / 连接</th><th>应用</th><th>状态</th><th>已知 Token</th><th>耗时</th><th>时间</th><th>记录</th></tr></thead><tbody>${items.map((r,i)=>`<tr><td><span class="mono">${h(r.model || "未知模型")}</span><span class="secondary">${h(r.connection_id || "未知连接")}</span></td><td>${h(appName(r.caller_id))}</td><td>${badge(label(r.status),stateColor(r.status))}</td><td>${h(number(r.total_tokens))}${r.total_tokens==null ? "" : !r.usage_known ? `<span class="secondary">部分用量</span>`:""}</td><td>${h(duration(r.duration_ms))}</td><td class="muted">${h(time(r.started_at))}</td><td><button class="text-button" data-record="${i}">详情</button></td></tr>`).join("")}</tbody></table></div>` : empty(requestRows ? "没有匹配的记录" : "调用记录不可读",requestRows ? "试试调整时间范围或筛选条件。" : "请稍后刷新，或在终端检查账本状态。","calendar")}${view==="overview" ? `<div class="pagination"><span class="page-count">最近 ${items.length} 条 · 共 ${number(requestRows?.total ?? 0)} 条匹配记录</span><button class="text-button" data-navigate="usage">查看全部调用 ${icon("arrow",14)}</button></div>` : `<div class="pagination"><span class="page-count">${requestRows?.total ? `${offset+1}–${Math.min(offset+25,requestRows.total)} / ${number(requestRows.total)}` : "0 条可显示记录"}</span><button id="previous" ${offset===0?"disabled":""}>上一页</button><button id="next" ${!requestRows || offset+25>=requestRows.total?"disabled":""}>下一页</button></div>`}</section>`;
}
function ranking() {
  const models=snapshot.usage?.models || [], max=models[0]?.requests || 1;
  return `<section class="panel"><div class="panel-header"><div><h2>模型使用分布</h2><p class="panel-subtitle">按调用次数 · 前 10 个连接 / 模型组合</p></div>${icon("chart",17)}</div>${models.length ? `<div class="ranking">${models.map(m=>`<div class="ranking-item"><div class="ranking-title"><span title="${h(m.model)}">${h(m.model || "未知模型")}</span><span>${number(m.requests)} 次</span></div><meter min="0" max="${max}" value="${m.requests}" aria-label="${h(m.model)} ${m.requests} 次调用"></meter><small>${h(m.connection_id || "未知连接")} · ${number(m.known_tokens.total_tokens)} 已报告 tokens</small></div>`).join("")}</div>` : empty("暂无模型用量","模型分布会随真实调用记录更新。")}</section>`;
}
function modelLayers(c) {
  const n=c.model_catalog?.counts || {};
  return `<div class="model-layers" aria-label="模型四层状态"><div><span>01 发现目录</span><strong>${c.discovery?.discovered_at ? number(n.discovered) : "—"}</strong><small>${h(({never:"尚未查询",success:"目录已获取",failure:"最近发现失败",stale:"来源已变更"})[c.discovery?.state] || "未知")}</small></div><div><span>02 明确启用</span><strong>${number(n.approved ?? c.models.length)}</strong><small>本机允许列表</small></div><div><span>03 应用获权</span><strong>${number(n.application_authorized)}</strong><small>至少一个有效应用</small></div><div><span>04 历史能力</span><strong>${number(n.historically_verified)}</strong><small>当前配置 · 历史</small></div></div>`;
}
function modelTable(c) {
  const models=c.model_catalog?.models || [], editable=canManageModels();
  return `<div class="detail-section"><div class="model-heading"><h3>模型目录与权限</h3>${badge(editable?"可管理启用状态":"只读模式")}</div>${modelLayers(c)}<p class="detail-note">启用只改变本地资源池，应用策略继续独立生效。禁用后不再接收该模型的新调用；已派发请求继续结算。</p><div class="model-toolbar"><label>查找模型<input id="model-search" type="search" placeholder="搜索模型名称" value="${h(modelSearch)}" autocomplete="off"></label><label>启用状态<select id="model-scope"><option value="all" ${modelScope==="all"?"selected":""}>全部模型</option><option value="approved" ${modelScope==="approved"?"selected":""}>已启用</option><option value="disabled" ${modelScope==="disabled"?"selected":""}>未启用</option></select></label><span id="model-match-count" class="muted"></span></div><div id="model-action-status" class="model-action-status" role="status" aria-live="polite" hidden></div><div id="model-confirmation" class="model-confirmation" hidden></div><div class="table-wrap model-table"><table><thead><tr><th>模型</th><th>发现</th><th>启用</th><th>应用授权</th><th>历史协议能力</th>${editable?"<th>操作</th>":""}</tr></thead><tbody>${models.map(m=>`<tr data-model-row="${h(m.id)}" data-approved="${m.approved}"><td class="mono">${h(m.id)}${m.default?'<span class="secondary">默认模型</span>':""}</td><td>${h(m.discovery==="listed" ? c.discovery.current ? "目录中" : "旧目录" : m.discovery==="not_in_catalog" ? "目录未列出" : "未知")}</td><td>${badge(m.approved?"已启用":"未启用",m.approved?"green":"")}</td><td>${m.effective_applications.map(appName).map(h).join("、") || "无有效授权"}<span class="secondary">策略匹配 ${m.authorized_applications.length} 个应用</span></td><td>${m.verification.length ? m.verification.map(v=>`<p>${h(v.capability==="text_stream"?"文本 SSE":v.capability==="text_nonstream"?"文本普通":"旧记录 · 能力范围未知")} · ${h(v.state==="unscoped"?"范围未知":label(v.state))}<span class="secondary">${h(v.protocol || "协议未知")} · ${h(time(v.verified_at))}</span><span class="secondary">账户权限历史：${h(v.account_access_observed==="allowed"?"该次调用获准":v.account_access_observed==="rejected"?"该次被拒绝":"未知")} · 当前可用性未知</span></p>`).join("") : "尚无模型级证据"}</td>${editable?`<td><button class="model-action ${m.approved?"":"enable"}" data-model-action="${h(m.id)}" aria-label="${m.approved?"禁用":"启用"} ${h(m.id)}" ${modelBusy?"disabled":""}>${m.approved?"禁用":"启用"}</button></td>`:""}</tr>`).join("")}</tbody></table></div><p id="model-no-match" class="muted" hidden>没有匹配的模型。</p>${models.length ? "" : '<p class="muted">尚无发现或启用的模型；可先在终端执行 connection discover。</p>'}</div>`;
}
function filterModels() {
  let visible=0;
  document.querySelectorAll("[data-model-row]").forEach(row=>{
    row.hidden=!row.dataset.modelRow.toLowerCase().includes(modelSearch.toLowerCase()) || (modelScope!=="all" && (row.dataset.approved==="true")!==(modelScope==="approved"));
    if(!row.hidden)visible++;
  });
  if($("#model-match-count"))$("#model-match-count").textContent=`${visible} 个模型`;
  if($("#model-no-match"))$("#model-no-match").hidden=visible!==0;
}
function modelMessage(message, failed=false) {
  const element=$("#model-action-status");if(!element)return;
  element.textContent=message;element.hidden=!message;element.classList.toggle("error",failed);
}
async function changeModel(modelId, clearDefault=false) {
  if(modelBusy || !canManageModels())return;
  const id=detailConnection,c=snapshot.connections.find(c=>c.id===id),m=c?.model_catalog.models.find(m=>m.id===modelId);
  if(!m)return;
  if(m.approved && m.default && !clearDefault){
    $("#model-confirmation").innerHTML=`<p><strong>${h(modelId)}</strong> 是当前默认模型。禁用会清除默认值，之后需显式指定模型或在 CLI 选择新默认。</p><div><button class="model-action" data-model-confirm="${h(modelId)}">禁用并清除默认</button><button class="text-button" id="cancel-model-change">取消</button></div>`;
    $("#model-confirmation").hidden=false;$("[data-model-confirm]").focus();return;
  }
  modelBusy=true;$("#model-confirmation").hidden=true;
  document.querySelectorAll("[data-model-action],[data-model-confirm]").forEach(b=>b.disabled=true);
  modelMessage("正在保存模型启用设置…");
  const requestController=new AbortController(),deadline=setTimeout(()=>requestController.abort(),20000);
  let saved=false;
  try {
    const response=await fetch("/api/model-approval",{method:"POST",headers:{"Content-Type":"application/json","X-Selark-Dashboard":"1"},credentials:"same-origin",cache:"no-store",signal:requestController.signal,body:JSON.stringify({connection_id:id,model:modelId,enabled:!m.approved,revision:c.model_catalog.approval_revision,clear_default:clearDefault})});
    const result=await response.json();
    if(!response.ok){const error=new Error(result.error || "模型设置保存失败。");error.status=response.status;throw error;}
    saved=true;
    const fresh=await refresh();
    if(!fresh)throw new Error("设置已保存，快照暂时未刷新；请刷新确认后再操作。");
    if($("#details").open && detailConnection===id){connectionDetails(id);modelMessage(`${modelId} 已${result.enabled?"启用":"禁用"}。应用权限保持不变。`);}
  }catch(error){
    if(error.status===401)loginRequired(error.message);
    else if($("#details").open && detailConnection===id){
      if(error.status===409 && await refresh())connectionDetails(id);
      modelMessage(error.name==="AbortError"?"请求超时，结果尚未确认。请刷新查看，操作不会自动重试。":error.message || "连接中断，请刷新确认操作结果。",true);
    }else notice(saved?"模型设置已保存，请刷新查看。":"模型操作结果未确认，请刷新查看。");
  }finally{
    clearTimeout(deadline);modelBusy=false;
    document.querySelectorAll("[data-model-action],[data-model-confirm]").forEach(b=>b.disabled=false);
  }
}
function resourcesPage() {
  return `<div class="section-intro"><p>共 ${snapshot.connections.length} 个连接 · ${snapshot.connections.reduce((n,c)=>n+c.models.length,0)} 个已启用的连接内模型</p>${badge(canManageModels()?"模型启用管理":"全部资源 · 只读")}</div><div class="connection-cards">${snapshot.connections.map(c=>`<article class="connection-card"><div class="card-title"><div class="provider-icon">${h(c.provider.slice(0,2).toUpperCase())}</div><div><h3>${h(c.name)}</h3><div class="card-type">${h(label(c.resource_type))} / ${h(c.id)}</div></div>${badge(c.enabled?"已启用":"已停用",c.enabled?"green":"")}</div><div class="card-facts"><div><span class="fact-label">供应商凭据 · 本地</span><span class="fact-value">${h(authorization(c))}</span></div><div><span class="fact-label">连接历史调用</span>${badge(label(c.verification.state),stateColor(c.verification.state))}</div><div><span class="fact-label">当前可用性</span><span class="fact-value">${h(availability(c))}</span></div><div><span class="fact-label">供应商当前剩余额度</span><span class="fact-value">未知</span></div></div>${modelLayers(c)}<div class="model-chips">${c.models.slice(0,4).map(m=>`<span class="chip">${h(m)}</span>`).join("")}${c.models.length>4?`<span class="chip">+${c.models.length-4}</span>`:""}${!c.models.length?`<span class="muted">尚未启用模型</span>`:""}</div><div class="card-footer"><span>最近历史验证：${h(time(c.verification.verified_at))}</span><button class="text-button" data-connection="${h(c.id)}">${canManageModels()?"管理模型":"查看详情"} ${icon("arrow",14)}</button></div></article>`).join("")}</div>${snapshot.connections.length?"":`<section class="panel">${empty("还没有可显示的连接","请先在本机 CLI 配置连接，再在这里管理模型启用状态。","resources")}</section>`}`;
}
function applicationsPage() {
  return `<div class="section-intro"><p>本地应用配额按 UTC 今日计算，不受用量筛选影响。</p>${badge(`${snapshot.applications.length} 个应用`)}</div><div class="application-cards">${snapshot.applications.map(a=>{
    const q=a.quota,p=a.policy,limit=p.daily_token_limit;
    const remaining=q.token_quota_state==="unlimited" ? "未设上限" : q.remaining_tokens==null ? "未知，已暂停" : `${number(q.remaining_tokens)} 剩余`;
    return `<article class="application-card"><div class="card-title"><div class="provider-icon">${h(a.name.slice(0,1).toUpperCase())}</div><div><h3>${h(a.name)}</h3><div class="card-type mono">${h(a.id)}</div></div>${badge(a.active?"令牌有效":"已撤销",a.active?"green":"")}</div><div class="card-facts"><div><span class="fact-label">连接权限</span><span class="fact-value">${p.allowed_connections==null?"未限定连接列表":p.allowed_connections.length ? `${p.allowed_connections.length} 个指定连接`:"不允许任何连接"}</span></div><div><span class="fact-label">应用并发上限</span><span class="fact-value">${p.max_concurrent_requests==null?"未单独限制":number(p.max_concurrent_requests)}</span></div><div><span class="fact-label">Token Plan / ChatGPT</span><span class="fact-value">${p.allow_token_plans?"允许":"未授权"} / ${p.allow_chatgpt_subscriptions?"允许":"未授权"}</span></div><div><span class="fact-label">每分钟请求上限</span><span class="fact-value">${p.requests_per_minute==null?"未单独限制":number(p.requests_per_minute)}</span></div></div><div class="quota-line"><span>UTC 今日 Token 占用</span><strong>${h(remaining)}</strong></div>${limit!=null ? `<progress max="${limit}" value="${Math.min(q.charged_or_reserved_tokens,limit)}" aria-label="今日 Token 实际或预留占用"></progress>`:""}<p class="quota-note">${h(number(q.charged_or_reserved_tokens))} 已用或预留${limit!=null?` / ${number(limit)}`:""} · ${number(q.unknown_token_requests)} 次未知用量</p><div class="card-footer"><span>配额日期 ${h(q.day)} UTC</span><button class="text-button" data-application="${h(a.id)}">权限与配额详情 ${icon("arrow",14)}</button></div></article>`;
  }).join("")}</div>${snapshot.applications.length?"":`<section class="panel">${empty("尚无可显示的应用","只展示普通调用方的身份与权限，不显示调用方令牌。","apps")}</section>`}`;
}
function serviceStrip() {
  const service=snapshot.service, state=service.state;
  return `<div class="service-strip">${icon("pulse",17)}<strong>本机网关</strong>${badge(state==="running"?"运行中":label(state),state==="running"?"green":state==="degraded"?"red":"")}<small>${service.reachable?`执行 ${number(service.capacity?.active)} · 排队 ${number(service.capacity?.queued)}`:"队列实时状态未知"}</small><span class="spacer"></span><button class="text-button" data-navigate="monitor">运行详情 ${icon("arrow",14)}</button></div>`;
}
function monitorPage() {
  const service=snapshot.service, c=service.capacity || {}, storage=service.storage || {};
  return `<div class="section-intro"><p>本机健康快照 · ${h(time(snapshot.checked_at))} · 上游可用性未探测</p>${badge(service.state==="running"?"网关运行中":label(service.state),service.state==="running"?"green":service.state==="degraded"?"red":"")}</div><div class="monitor-grid"><article class="monitor-tile"><h3>正在执行</h3><div class="monitor-value">${h(number(c.active))}<span class="metric-unit"> / ${h(number(c.max_active))}</span></div><p>健康接口报告的全局并发</p>${c.max_active?`<progress max="${c.max_active}" value="${c.active}" aria-label="实时执行并发"></progress>`:""}</article><article class="monitor-tile"><h3>等待队列</h3><div class="monitor-value">${h(number(c.queued))}<span class="metric-unit"> / ${h(number(c.max_queued))}</span></div><p>配置上限不等于实时可用容量</p>${c.max_queued?`<progress max="${c.max_queued}" value="${c.queued}" aria-label="实时等待队列"></progress>`:""}</article><article class="monitor-tile"><h3>落账状态</h3><div class="monitor-value">${storage.degraded==null?"未知":storage.degraded?"降级":"正常"}</div><p>待处理 ${h(number(storage.pending_records))} · 无效 ${h(number(storage.invalid_records))}</p>${badge(storage.accepting==null?"接收状态未知":storage.accepting?"可接收请求":"暂停接收",storage.accepting?"green":"")}</article></div>${recordsPanel()}<div class="split-grid"><section class="panel"><div class="panel-header"><div><h2>运行日志</h2><p class="panel-subtitle">最近 150 行窗口 · 仅展示结构化安全字段</p></div>${badge("实时日志快照")}</div>${logs?.items.length?`<div class="log-list">${logs.items.map(e=>`<div class="log-line"><time>${h(time(e.timestamp))}</time><span class="mono">${h(e.path)}</span>${badge(e.status==null?"未知":`HTTP ${e.status}`,e.status>=400?"amber":"")}<span class="muted">${h(duration(e.duration_ms))}</span></div>`).join("")}</div>`:empty(logs?"暂无结构化日志":"日志暂不可读",logs?"运行日志会在网关服务记录事件后出现。":"请检查日志权限与路径。","pulse")}<div class="chart-foot">${logs?`窗口内省略 ${logs.omitted} 行非结构化日志。`:"日志读取失败。"} 日志内容不受用量筛选影响。</div></section><section class="panel"><div class="panel-header"><div><h2>本地备份</h2><p class="panel-subtitle">最近 20 份 · ${backups?number(backups.total):"未知"} 份可识别备份</p></div>${icon("shield",17)}</div><div class="panel-body">${backups?.items.length?backups.items.map(b=>`<div class="backup-item"><div><strong>${h(time(b.created_at))}</strong><p>${b.manifest==="sha256_present"?"含 SHA-256 清单":b.manifest==="legacy"?"旧格式清单":"清单不可读"}</p></div>${badge("完整性未检查")}</div>`).join(""):empty(backups?"尚无备份记录":"备份目录不可读","此处仅列出已有备份；不会执行备份或恢复。","shield")}</div><div class="chart-foot">存在清单不代表备份健康。校验请使用终端 backup verify 命令。</div></section></div>`;
}
function render() {
  if (!snapshot) return;
  $("#filters").hidden=!["overview","usage","monitor"].includes(view);
  const page = view==="overview" ? `${metrics()}<div class="split-grid">${chart()}${resourceList()}</div>${serviceStrip()}${recordsPanel()}` : view==="resources" ? resourcesPage() : view==="usage" ? `${metrics()}<div class="split-grid">${chart()}${ranking()}</div>${recordsPanel()}` : view==="applications" ? applicationsPage() : monitorPage();
  $("#content").innerHTML=page;
}
function navigate(next) {
  if (!pages[next]) return;
  view=next; offset=0; errorsOnly=next==="monitor";
  $("#page-title").innerHTML=`${pages[next][0]}<span class="heading-dot">.</span>`;
  $("#breadcrumb").textContent=pages[next][0]; $("#eyebrow").textContent=pages[next][1]; $("#page-description").textContent=pages[next][2];
  document.querySelectorAll("[data-view]").forEach(b=>{b.classList.toggle("active",b.dataset.view===view);if(b.dataset.view===view)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");});
  requestRows=null; render(); refresh();
}
function pairs(items) { return `<dl class="detail-list">${items.map(([key,value])=>`<dt>${h(key)}</dt><dd>${h(value ?? "未知")}</dd>`).join("")}</dl>`; }
function detail(title,description,body) {
  detailConnection=null;
  $("#detail-content").innerHTML=`<h2 class="detail-title" id="detail-title">${h(title)}</h2><p class="detail-description">${h(description)}</p>${body}`;
  $("#details").showModal();
}
function connectionDetails(id) {
  const c=snapshot.connections.find(c=>c.id===id); if(!c)return;
  if(detailConnection!==id){modelSearch="";modelScope="all";}
  const a=c.authorization,v=c.verification,q=c.provider_quota;
  const blockers={connection_disabled:"连接已停用",models_not_configured:"未启用模型",authorization_not_ready:"授权尚未就绪",access_expired_refresh_required:"访问凭据已过期，需要刷新",plan_preset_only:"仅预设，不支持生成",local_state_unavailable:"本地状态不可读"};
  detail(c.name,`${label(c.resource_type)} · ${c.id}`,modelTable(c)+`<div class="detail-section"><h3>连接与授权</h3></div>`+pairs([["本地授权",authorization(c)],["远端凭据有效性","未知；未向供应商验证"],["当前可用性",availability(c)],["本地阻碍",c.availability.blockers?.map(b=>blockers[b]||b).join("；") || "未发现已知本地阻碍"],["历史验证",label(v.state)],["验证时间",v.verified_at || "尚无记录"],["历史验证模型",v.model || "未知"],["默认模型",c.default_model],["OAuth 访问到期",a.expires_at==null?"不适用 / 未知":new Date(a.expires_at).toLocaleString("zh-CN")],["可用刷新凭据",a.refresh_available==null?"不适用 / 未知":a.refresh_available?"本地存在":"未提供"]])+`<div class="detail-section"><h3>已启用模型 · ${c.models.length}</h3><div class="model-chips">${c.models.map(m=>`<span class="chip">${h(m)}</span>`).join("") || "无"}</div><p class="policy-list">当前 CLI 调用方获准模型：${h(c.client_allowed_models.join("、")||"无")}</p></div><div class="detail-section"><h3>供应商额度</h3>${pairs([["当前剩余 Token","未知"],["历史快照",q.historical_snapshot?label(q.state):"未提供"],["快照时间",q.checked_at || "尚无记录"]])}${q.windows?.length?`<div class="table-wrap"><table><thead><tr><th>历史窗口</th><th>已使用比例</th><th>重置时间</th></tr></thead><tbody>${q.windows.map(w=>`<tr><td>${h(w.name)}</td><td>${h(percent(w.used_ratio))}</td><td>${h(w.reset_at || "未知")}</td></tr>`).join("")}</tbody></table></div>`:""}${q.reported_counters?.length?`<div class="table-wrap"><table><thead><tr><th>历史窗口</th><th>单位</th><th>上限 / 已用 / 剩余</th></tr></thead><tbody>${q.reported_counters.map(w=>`<tr><td>${h(w.window)}</td><td>${h(w.unit)}</td><td>${h(w.limit??"未知")} / ${h(w.used??"未知")} / ${h(w.remaining??"未知")}</td></tr>`).join("")}</tbody></table></div>`:""}</div><p class="detail-note">历史成功仅对应当时的配置与模型。套餐快照是历史证据，供应商单位不换算为 Token；Dashboard 不刷新 OAuth、不发现模型、不查询套餐或发起模型调用。</p>`);
  detailConnection=id;filterModels();
}
function applicationDetails(id) {
  const a=snapshot.applications.find(a=>a.id===id); if(!a)return;
  const p=a.policy,q=a.quota, allowed=(v)=>v==null?"未限定列表（仍受资源类型授权约束）":v.length?v.join("、"):"空列表：全部拒绝";
  const effective=`<div class="detail-section"><h3>当前配置下获准的模型</h3><div class="policy-list">${a.effective_resources.map(r=>`<p><strong>${h(r.name)}</strong> · ${h(label(r.resource_type))}<br>${r.models.map(h).join("、")}</p>`).join("") || "无获准模型"}</div><p class="quota-note">此处为连接开启、模型启用与应用策略的交集；不代表供应商可用或额度足够。</p></div>`;
  detail(a.name,`${a.id} · ${a.active?"令牌有效":"令牌已撤销"}`,effective+pairs([["创建时间",a.created_at],["撤销时间",a.revoked_at || "未撤销"],["允许连接",allowed(p.allowed_connections)],["允许模型",allowed(p.allowed_models)],["Token Plan",p.allow_token_plans?"明确允许":"未授权"],["ChatGPT 订阅",p.allow_chatgpt_subscriptions?"明确允许":"未授权"],["并发上限",p.max_concurrent_requests??"未单独限制"],["每分钟请求",p.requests_per_minute??"未单独限制"],["单次输出上限",p.max_output_tokens??"未单独限制"],["估算输入上限",p.max_estimated_input_tokens??"未单独限制"],["订阅输出预留",p.chatgpt_output_reservation??"未配置"]])+`<div class="detail-section"><h3>本地配额 · ${h(q.day)} UTC</h3>${pairs([["日 Token 上限",p.daily_token_limit??"未设上限"],["配额状态",label(q.token_quota_state)],["今日实际或预留",number(q.charged_or_reserved_tokens)],["保守预留 Token",number(q.estimated_reserved_tokens)],["剩余 Token",q.token_quota_state==="unlimited"?"未设上限":number(q.remaining_tokens)],["日估算预算",p.daily_budget==null?"未设上限":`${p.budget_currency} ${p.daily_budget}`],["估算费用或预留",Object.entries(q.estimated_cost_or_reservation).map(([c,v])=>`${c} ${v}`).join("；") || "无已知金额"],["未知费用请求",q.unknown_cost_requests],["账本在途请求",q.in_flight_requests],["未纳入配额的旧记录",q.historical_requests_excluded]])}</div><p class="detail-note">本地配额包含实际消耗与保守预留，不等于供应商额度；无单独并发上限仍受全局限制。账本在途状态并非实时队列。实际用量请查看用量分析。</p>`);
}
function recordDetails(index) {
  const r=requestRows?.items[index]; if(!r)return;
  detail(r.model || "调用记录",r.request_id,pairs([["应用",appName(r.caller_id)],["连接",r.connection_id],["资源类型",label(r.resource_type)],["开始时间",r.started_at],["结束时间",r.finished_at],["最终状态",label(r.status)],["HTTP 状态",r.http_status],["上游状态",r.upstream_status],["上游结果",label(r.upstream_result)],["下游交付",label(r.delivery_status)],["端到端耗时",duration(r.duration_ms)],["排队耗时",duration(r.queue_ms)],["已记录上游尝试",r.upstream_attempts],["输入 / 输出 Token",`${number(r.prompt_tokens)} / ${number(r.completion_tokens)}`],["总 Token",number(r.total_tokens)],["用量一致性",label(r.usage_consistency)],["费用估算",r.cost.estimated_cost==null?"未知 / 未定价":`${r.cost.currency} ${r.cost.estimated_cost}`],["费用口径",label(r.cost.cost_status)],["错误代码",[r.error_code,r.upstream_error_code,r.delivery_error_code].filter(Boolean).join(" / ") || "无已记录错误"],["记录来源",r.origin==="import"?"导入历史":"网关记录"]])+`<p class="detail-note">仅展示调用元数据；原始错误正文、请求内容、响应内容与敏感凭据不会发送到浏览器。费用为历史价格快照估算，不是供应商账单。</p>`);
}
document.addEventListener("click", (event)=>{
  const b=event.target.closest("button"); if(!b)return;
  if(b.dataset.modelAction)changeModel(b.dataset.modelAction);
  if(b.dataset.modelConfirm)changeModel(b.dataset.modelConfirm,true);
  if(b.id==="cancel-model-change")$("#model-confirmation").hidden=true;
  if(b.dataset.view) navigate(b.dataset.view);
  if(b.dataset.navigate) navigate(b.dataset.navigate);
  if(b.dataset.metric){metric=b.dataset.metric;render();$(`[data-metric="${metric}"]`).focus();}
  if(b.dataset.connection)connectionDetails(b.dataset.connection);
  if(b.dataset.application)applicationDetails(b.dataset.application);
  if(b.dataset.record!=null)recordDetails(Number(b.dataset.record));
  if(b.id==="toggle-errors"){errorsOnly=!errorsOnly;offset=0;refresh();}
  if(b.id==="previous"){offset=Math.max(0,offset-25);refresh();}
  if(b.id==="next"){offset+=25;refresh();}
});
$("#content").addEventListener("change",(event)=>{if(event.target.id==="currency"){currency=event.target.value;render();}});
$("#detail-content").addEventListener("input",event=>{if(event.target.id==="model-search"){modelSearch=event.target.value;filterModels();}});
$("#detail-content").addEventListener("change",event=>{if(event.target.id==="model-scope"){modelScope=event.target.value;filterModels();}});
$("#details").addEventListener("close",()=>{detailConnection=null;});
$("#close-details").addEventListener("click",()=>$("#details").close());
$("#details").addEventListener("click",(event)=>{if(event.target===$("#details")){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}});
$("#refresh").addEventListener("click",refresh);
function applyFilters() {
  const params=new URLSearchParams();
  ["period","caller_id","connection_id","model"].forEach(key=>{const v=$(`#${key}`).value;if(v)params.set(key,v);});
  if(params.get("period")==="custom") {
    const start=new Date($("#since").value),end=new Date($("#until").value);
    if(!Number.isFinite(start.getTime())||!Number.isFinite(end.getTime())||start>=end||end-start>366*86400000){notice("请选择有效的开始和结束时间，范围不超过 366 天。");return;}
    params.set("since",start.toISOString());params.set("until",end.toISOString());
  }
  activeQuery=params;offset=0;refresh();
}
["period","caller_id","connection_id","model"].forEach(key=>$(`#${key}`).addEventListener("change",()=>{
  $("#custom-range").hidden=$("#period").value!=="custom";
  if($("#period").value!=="custom" || key!=="period")applyFilters();
  else notice("设置开始和结束时间后，点击应用时间。当前图表仍使用上一次的筛选范围。");
}));
$("#apply-range").addEventListener("click",applyFilters);
$("#reset-filters").addEventListener("click",()=>{["caller_id","connection_id","model"].forEach(key=>$(`#${key}`).value="");$("#period").value="7d";$("#custom-range").hidden=true;applyFilters();});
document.addEventListener("visibilitychange",()=>{if(!document.hidden && authorized && !$("#details").open)refresh();});
let resizeTimer;
window.addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(snapshot && !$("#details").open)render();},150);});
setInterval(()=>{if(!document.hidden && authorized && !$("#refresh").disabled && !$("#details").open && !$("#content").contains(document.activeElement))refresh();},30000);

async function start() {
  try {
    if(bootstrap) {
      const body=JSON.stringify({token:bootstrap});bootstrap=null;
      const response=await fetch("/api/session",{method:"POST",headers:{"Content-Type":"application/json"},body,credentials:"same-origin",cache:"no-store"});
      if(!response.ok)throw new Error((await response.json()).error);
    }
    authorized=true;await refresh();
  } catch(error) { bootstrap=null;loginRequired(error.message); }
}
start();
