/* Account data only. Existing pages are opened through the shared workspace router. */
const homeMetrics = [
  ['products','产品总数'], ['discoveredToday','今日选品发现'],
  ['listingPending','待生成 Listing'], ['listingRunning','Listing 生成中'],
  ['imagesRunning','图片生成中'],
  ['erpPending','ERP 待处理'], ['failed','失败任务'],
];
const homeStatuses = {
  queued:['待开始',''], ready:['待开始',''], running:['执行中','active'], paused:['已暂停','warning'],
  blocked:['待补齐','warning'], needs_review:['需核对','warning'], uncertain:['提交结果未知','warning'],
  library_ready:['待存入钧飞','warning'], inspection_done:['检查完成','success'],
  done:['已完成','success'], failed:['失败','danger'],
  erp_pending:['ERP 待核对','warning'],
  local_preparing:['本地换色准备中','active'], local_rendering:['本地换色中','active'],
  local_paused:['本地换色已暂停','warning'], awaiting_manual:['待人工确认','warning'],
  approved:['已人工确认','success'], needs_attention:['需处理','danger'],
  validating:['核对参考图','active'], generating:['生成中','active'], reviewing:['AI质检中','active'],
};
const homeStages = {
  selection:'选品', sourcing:'货源', research:'竞品关键词', listing:'Listing',
  images:'图片', export:'交付导出', publish:'ERP 交接', verify:'结果核对', complete:'已完成',
};
const homeTime = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', dateStyle:'short', timeStyle:'short'});

function workbenchLink(event, view) {
  if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  go(view);
}

function showHomeData(node, data) {
  const metrics = homeMetrics.map(([key,label]) => {
    const value = data.metrics[key];
    return `<div><dt>${label}</dt><dd ${value===null?'data-unavailable':''}>${value===null?'未接通':esc(value)}</dd></div>`;
  }).join('');
  const table = data.recent.length ? `<div class="ds-table-wrap" tabindex="0" role="region" aria-label="最近任务表格"><table class="ds-table">
    <thead><tr><th scope="col">任务 / 产品</th><th scope="col">来源</th><th scope="col">阶段</th><th scope="col">状态</th><th scope="col">更新时间</th></tr></thead>
    <tbody>${data.recent.map(task => {
      const [label,tone] = homeStatuses[task.status] || ['待核对','warning'];
      return `<tr><td>${esc(task.title)}</td>
        <td>${esc(task.source)}</td><td>${homeStages[task.stage] || '—'}</td>
        <td><span class="ds-status" data-tone="${tone}">${label}</span></td>
        <td>${task.at===null?'未记录':homeTime.format(task.at * 1000)}</td></tr>`;
    }).join('')}</tbody></table></div>` : '<p class="ds-feedback ds-muted">暂无任务记录。</p>';
  node.innerHTML = `<div><dl class="ds-panel workbench-metrics">${metrics}</dl>
    <div class="ds-muted ds-note wb-title-label"><span><time datetime="${esc(data.date)}">${esc(data.date)}</time> · 北京时间</span>${workbenchHelp('选品按站点和 ASIN 去重。生成中按已记账的任务统计。','首页统计')}</div></div>
    <section class="ds-panel" aria-labelledby="homeRecent"><div class="ds-panel-heading"><h2 id="homeRecent">最近任务</h2>
      <a class="ds-button" href="/tasks" data-home-link="agent">查看任务</a></div>${table}</section>`;
  node.querySelectorAll('[data-home-link]').forEach(link => link.onclick = event => workbenchLink(event, link.dataset.homeLink));
}

window.renderWorkbenchHome = function () {
  const content = document.getElementById('content');
  content.innerHTML = `<section class="ds workbench-home" aria-labelledby="workbenchTitle">
    <header class="ds-header"><div><h1 id="workbenchTitle">工作台</h1></div>
      <div class="ds-actions"><a class="ds-button ds-button-primary" href="/products" data-home-link="products">查看产品档案</a></div></header>
    <div class="workbench-home" data-home-content></div></section>`;
  content.querySelectorAll('[data-home-link]').forEach(link => link.onclick = event => workbenchLink(event, link.dataset.homeLink));
  const node = content.querySelector('[data-home-content]');
  async function load() {
    node.innerHTML = '<p class="ds-panel ds-feedback ds-muted" role="status">正在读取工作台数据…</p>';
    try {
      const response = await fetch('/api/workbench/overview');
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '工作台数据无法读取');
      if (node.isConnected) showHomeData(node, data);
    } catch (error) {
      if (!node.isConnected) return;
      node.innerHTML = '<p class="ds-panel ds-feedback" role="alert"><span></span><button type="button" class="ds-button">重试</button></p>';
      node.querySelector('span').textContent = error.message;
      node.querySelector('button').onclick = load;
    }
  }
  void load();
};

window.addEventListener('popstate', () => {
  if (!serverReady) return;
  restoreWorkspaceRoute();
  render();
  window.scrollTo({top:0,behavior:'instant'});
});

const workbenchNavToggle = document.getElementById('workbenchNavToggle');
workbenchNavToggle.onclick = () => {
  const expanded = workbenchNavToggle.getAttribute('aria-expanded') === 'true';
  workbenchNavToggle.setAttribute('aria-expanded', String(!expanded));
};
document.getElementById('nav').addEventListener('click', event => {
  if (event.target.closest('[data-view]')) workbenchNavToggle.setAttribute('aria-expanded', 'false');
});
