// Providers without percentage updates use an indeterminate progress bar.
const activeListingGeneration=new Map();
function generationProgressHTML(status,label,percent){
 const complete=status==='completed',failed=status==='failed'||status==='uncertain';
 const value=complete?100:(typeof percent==='number'&&Number.isFinite(percent)?Math.max(0,Math.min(99,percent)):null);
 return `<div class="generation-progress ${failed?'failed':complete?'completed':''}" role="status"><strong>${complete?'✓ ':failed?'✕ ':''}${esc(label)}</strong>${failed?'':`<progress max="100" ${value===null?'':`value="${value}"`} aria-label="${esc(label)}"></progress>`}</div>`;
}
function showListingProgress(p,targetMarket,status,message){
 const item={status,message,at:new Date().toISOString()};
 if(status==='running'){activeListingGeneration.set(p.id+'-'+targetMarket,item)}
 else{activeListingGeneration.delete(p.id+'-'+targetMarket);p.listingGeneration||={};p.listingGeneration[targetMarket]=item;persist({invalidateReview:false})}
 if(view==='listing'&&selected===p.id&&market===targetMarket&&listingWorkspaceStep==='copy'){mountListingProgress();updateListingCopyActions()}
}
function mountListingProgress(){
 if(typeof mountDurableListingProgress==='function'&&mountDurableListingProgress())return;
 const p=product(),item=activeListingGeneration.get(p.id+'-'+market)||p.listingGeneration?.[market];
 let box=document.getElementById('listingGenerationProgress');
 if(!item)return;if(!box){box=document.createElement('div');box.id='listingGenerationProgress';document.querySelector('.titlebar').after(box)}
 box.innerHTML=generationProgressHTML(item.status,item.message+(item.status==='running'?' · 等待模型返回，暂无精确百分比':' · '+new Date(item.at).toLocaleString('zh-CN')));
 const button=document.getElementById('generateListing');if(button&&item.status==='running'){button.disabled=true;button.textContent='文案生成中…'}
}
// Local development features: database sync, product facts, variant/material assets.
let serverRevision=0,serverReady=false,syncTimer,syncBusy=false,syncPending=false,syncConflict=false;
const baseRender=render,basePersist=persist;
names.settings='模型设置';
let listingKeyConfigured=false;
let listingTextProvider='文案服务';
function syncLabel(text){let el=document.getElementById('serverStatus');if(!el){el=document.createElement('span');el.id='serverStatus';el.className='badge';document.querySelector('header .user').prepend(el)}el.textContent=text}
persist=function(options){basePersist();if(options?.invalidateReview!==false)invalidateReview();if(serverReady&&!syncConflict){syncPending=true;syncLabel('有修改待保存');clearTimeout(syncTimer);syncTimer=setTimeout(pushState,450)}};
function invalidateReview(){const p=product();if(p&&p.status==='已通过'&&view!=='reviews'){p.status='需修改';state.reviews[p.id]={copy:false,images:false,note:'审核通过后内容发生变化，请重新确认'};basePersist()}}
async function pushState(){if(syncBusy||!serverReady||syncConflict||!syncPending)return;syncBusy=true;syncPending=false;let failed=false;syncLabel('正在保存…');try{const body=JSON.stringify({revision:serverRevision,state});if(new Blob([body]).size>64*1024*1024)throw Error('产品库超过 64 MB 保存上限，请先下载备份并联系管理员');const r=await fetch('/api/state',{method:'POST',headers:{'Content-Type':'application/json'},body});const data=await r.json();if(r.status===409){syncConflict=true;syncPending=true;syncLabel('保存冲突 · 请备份');toast(data.error);showConflict();return}if(!r.ok)throw Error(data.error);serverRevision=data.revision;syncLabel('已保存到数据库')}catch(e){failed=true;syncPending=true;syncLabel('保存失败 · 点击重试');document.getElementById('serverStatus').onclick=pushState;toast('服务端保存失败，修改保留在当前页面，请勿刷新：'+e.message)}finally{syncBusy=false;if(syncPending&&!syncConflict&&!failed)setTimeout(pushState,450)}}
async function flushWorkspace(){
 if(!serverReady)throw Error('尚未连接工作空间，请连接后重试');
 const deadline=Date.now()+15000;
 while(syncBusy){if(Date.now()>deadline)throw Error('保存仍在进行，请稍后重试');await new Promise(resolve=>setTimeout(resolve,50));}
 if(syncConflict)throw Error('存在保存冲突，请先备份并处理冲突');
 if(syncPending)await pushState();
 if(syncConflict||syncPending||syncBusy)throw Error('修改尚未保存成功，请完成保存后重试');
}
function showConflict(){const d=document.createElement('div');d.className='notice';d.style.borderColor='#dca899';d.innerHTML='另一个页面已保存新版本。为避免覆盖，已暂停保存。 <button class="btn" id="backupConflict">下载当前修改备份</button> <button class="btn" id="reloadConflict">刷新读取最新版本</button>';document.querySelector('.content').prepend(d);document.getElementById('backupConflict').onclick=()=>download('冲突修改备份.json',JSON.stringify(state,null,2));document.getElementById('reloadConflict').onclick=()=>{if(confirm('确认已经备份本页修改？刷新会读取服务器的最新版本。'))location.reload()}}
render=function(){if(view==='settings'){baseRender();document.getElementById('crumb').textContent='模型设置';document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.view==='settings'));renderSettings()}else{baseRender();enhance()}if(syncConflict)showConflict()};
function enhance(){if(view==='listing'){const box=document.querySelector('.grid .box');const p=product();const old=box.querySelectorAll('.fact');old[2].innerHTML='<span>已上传素材</span>'+((p.assets||[]).length)+' 张';old[3].innerHTML='<span>已记录资料</span>'+esc(p.material||'材质待填写')+' / '+esc(p.dimensions||'尺寸待填写');const btn=document.createElement('button');btn.className='btn';btn.textContent='编辑资料与变体';btn.onclick=editFacts;box.insertBefore(btn,document.getElementById('toimages'));if(document.getElementById('saveStatus'))document.getElementById('saveStatus').textContent='输入后自动保存';}if(view==='images')addAssets();}
function supplierVariantFormValues(p){
 const c=p.supplierVariantCoverage||{};
 return {__svEnabled:'1',__svColours:(c.supplierObservedColors||[]).join(', '),__svSizes:(c.supplierObservedSizes||[]).join(', '),
  __svSource:c.sourceUrl||p.sourceUrl||'',__svChecked:c.checkedAt||'',__svEvidence:c.evidence||'',
  __svPairs:(c.supplierObservedVariants||[]).map(v=>v.color+' | '+v.size).join('\n')};
}
function supplierVariantFields(p){
 const children=p.sourceImport?.amazon?.variants;
 if(!(Array.isArray(children)&&children.length>1)&&!p.supplierVariantCoverage)return '';
 const f=supplierVariantFormValues(p),colours=[...new Set((children||[]).map(v=>v.color_name).filter(Boolean))],sizes=[...new Set((children||[]).map(v=>v.size_name).filter(Boolean))];
 return `<fieldset style="border:1px solid #dae1d7;border-radius:7px;padding:10px 14px;margin-top:12px"><legend>同一货源的变体选项</legend><input type="hidden" name="__svEnabled" value="1"><p>在同一货源页面核对可见选项，无需逐色另找货源。不要把已采集的Amazon选项直接当成供货范围。选项相符不代表逐SKU库存。</p><p class="sub">已采集颜色：${esc(colours.join(', '))}<br>已采集尺码：${esc(sizes.join(', '))}</p><label>货源可见颜色（按对应的Amazon颜色名记录，用逗号分隔）<textarea name="__svColours" maxlength="12000">${esc(f.__svColours)}</textarea></label><label>货源可见尺码（用逗号分隔）<input name="__svSizes" maxlength="4000" value="${esc(f.__svSizes)}"></label><label>观察的货源链接<input name="__svSource" maxlength="2000" value="${esc(f.__svSource)}"></label><label>观察时间（ISO时间，必须含时区）<input name="__svChecked" placeholder="YYYY-MM-DDTHH:mm:ss+08:00" value="${esc(f.__svChecked)}"></label><label>来源证据（采集记录、页面原文或已保存文件）<textarea name="__svEvidence" maxlength="12000">${esc(f.__svEvidence)}</textarea></label><label>已明确记录的具体组合（可选，每行 颜色 | 尺码；不自动扩展组合）<textarea name="__svPairs" maxlength="50000">${esc(f.__svPairs)}</textarea></label></fieldset>`;
}
function supplierVariantPatch(p,data){
 if(data.__svEnabled!=='1')return {};
 const previous=supplierVariantFormValues(p),keys=Object.keys(previous).filter(k=>k!=='__svEnabled');
 const value=k=>String(data[k]||'').trim();
 if(keys.every(k=>value(k)===previous[k].trim()))return {};
 if(!p.supplierVariantCoverage&&keys.filter(k=>k!=='__svSource').every(k=>!value(k)))return {};
 const list=k=>[...new Set(value(k).split(/[,，\n]+/).map(v=>v.trim()).filter(Boolean))];
 const colours=list('__svColours'),sizes=list('__svSizes');
 if(!colours.length||!sizes.length)throw Error('请填写货源实际可见的颜色和尺码，缺失时保留原观察，不推断。');
 if(colours.length>500||sizes.length>500)throw Error('货源颜色或尺码最多500项');
 const offer=url=>{try{const u=new URL(url);return u.protocol==='https:'&&u.hostname==='detail.1688.com'&&(!u.port||u.port==='443')&&!u.username&&!u.password&&/^\/offer\/[1-9][0-9]*\.html$/.test(u.pathname)?u.pathname:null}catch{return null}};
 const source=offer(value('__svSource'));
 if(!source||source!==offer(data.sourceUrl||p.sourceUrl))throw Error('观察的货源必须与当前产品货源一致');
 const checked=value('__svChecked'),time=new Date(checked);
 if(!/(?:Z|[+-]\d{2}:\d{2})$/i.test(checked)||!Number.isFinite(time.getTime()))throw Error('请填写含时区的实际观察时间');
 const evidence=value('__svEvidence');if(!evidence)throw Error('请填写变体选项的来源证据');
 const coverage={productId:p.id,sourceUrl:value('__svSource'),checkedAt:time.toISOString(),evidence,
  supplierObservedColors:colours,supplierObservedSizes:sizes,status:'user_recorded_visible_options_not_stock_confirmation',
  scope:'User-recorded visible options; not a stock, exact-match or profit attestation.'};
 const pairText=value('__svPairs');
 if(pairText){coverage.supplierObservedVariants=pairText.split(/\n/).filter(v=>v.trim()).map(line=>{const a=line.split(/[|｜]/).map(v=>v.trim());if(a.length!==2||!a[0]||!a[1])throw Error('具体组合请每行填写 颜色 | 尺码');return {color:a[0],size:a[1]}})}
 else if(p.supplierVariantCoverage?.supplierObservedVariants&&source===offer(p.supplierVariantCoverage.sourceUrl))throw Error('同一货源已有具体组合记录，不能清空绕过缺口；请保留或按实际记录修正。');
 const patch={supplierVariantCoverage:coverage};
 if(p.supplierVariantCoverage)patch.supplierVariantCoverageHistory=[...(p.supplierVariantCoverageHistory||[]),structuredClone(p.supplierVariantCoverage)];
 return patch;
}
function editFacts(){
 const p=product(),fields=[['brand','品牌'],['manufacturer','制造商'],['style','款式'],['amazonCategoryPath','Amazon类目路径（推荐有歧义时补充）'],['targetAudience','目标受众（仅填写已确认定位）'],['copyNotes','文案补充要求'],['sourceUrl','货源链接'],['material','材质 / 成分'],['dimensions','产品尺寸 / 尺码表数据'],['purchasePrice','采购价（含币种、起订量）'],['packageDimensions','包装尺寸（含单位）'],['productWeight','产品净重（含单位）'],['packageWeight','包装毛重（含单位）'],['packageContents','包装内容'],['colorNames','颜色，用逗号分隔'],['sizeNames','尺码 / 规格，用逗号分隔'],['keywords','目标关键词'],['features','卖点 / 使用场景']];
 const modal=document.getElementById('modal');
 modal.innerHTML='<h2>产品资料与变体</h2><p style="margin-top:8px">只记录供应商资料或实拍能确认的信息。</p><form id="factsForm">'+fields.map(([k,n])=>`<label>${n}<input name="${k}" value="${esc(p[k]||(k==='brand'?'Generic':''))}" maxlength="2000"></label>`).join('')+'<label>各变体明确售价（'+esc(market)+'站，JSON键为子SKU，值为站点币种金额）<textarea name="junfeiVariantPrices" style="width:100%;min-height:90px">'+esc(JSON.stringify(p.erpDelivery?.[market]?.variantPrices||(market==='US'?p.junfeiVariantPrices:{} )||{},null,2))+'</textarea></label><fieldset style="border:1px solid #dae1d7;border-radius:7px;padding:10px 14px"><legend>变体范围确认</legend><label><input type="checkbox" name="purchasePriceUniformAcrossVariants" '+(p.purchasePriceUniformAcrossVariants?'checked':'')+'> 该采购价适用于全部颜色和尺码</label><label><input type="checkbox" name="packageWeightUniformAcrossVariants" '+(p.packageWeightUniformAcrossVariants?'checked':'')+'> 该包装重量适用于全部颜色和尺码</label></fieldset>'+supplierVariantFields(p)+'<div class="actions"><button type="button" class="btn" id="cancelFacts">取消</button><button class="btn primary">保存资料</button></div></form>';
 modal.showModal();document.getElementById('cancelFacts').onclick=()=>modal.close();
 document.getElementById('factsForm').onsubmit=e=>{
  e.preventDefault();const data=Object.fromEntries(new FormData(e.target));
  data.purchasePriceUniformAcrossVariants=e.target.elements.purchasePriceUniformAcrossVariants.checked;
  data.packageWeightUniformAcrossVariants=e.target.elements.packageWeightUniformAcrossVariants.checked;
  try{data.junfeiVariantPrices=JSON.parse(data.junfeiVariantPrices||'{}');if(!data.junfeiVariantPrices||Array.isArray(data.junfeiVariantPrices)||typeof data.junfeiVariantPrices!=='object')throw Error()}catch{return toast('各变体售价需填写有效JSON对象')}
  data.erpDelivery={...p.erpDelivery,[market]:{...p.erpDelivery?.[market],variantPrices:data.junfeiVariantPrices}};
  if(market!=='US')delete data.junfeiVariantPrices;
  const list=s=>[...new Set(s.split(/[,，]/).map(x=>x.trim()).filter(Boolean))];
  if(list(data.colorNames).length>500||list(data.sizeNames).length>500)return toast('颜色或尺码最多 500 项');
  try{Object.assign(data,supplierVariantPatch(p,data))}catch(error){return toast(error.message)}
  for(const key of Object.keys(data))if(key.startsWith('__sv'))delete data[key];
  data.brand=data.brand.trim()||'Generic';data.manufacturer=data.manufacturer.trim();Object.assign(p,data);
  if(list(p.colorNames).length)p.colors=list(p.colorNames).length;if(list(p.sizeNames).length)p.sizes=list(p.sizeNames).length;
  persist();modal.close();render();toast('产品资料已保存');
 };
}
function addAssets(){const p=product(),section=document.createElement('section');section.className='panel box';section.style.marginBottom='24px';section.innerHTML=`<h2>真实素材库 <span class="sub">${(p.assets||[]).length} 张</span></h2><p>上传 JPG、PNG、WebP，单张最多 15 MB。图片保存到本机服务端，按产品关联。</p><div class="actions" style="margin:15px 0"><select id="assetSource" aria-label="素材来源"><option>实拍图</option><option>供应商图</option></select><input id="assetColor" placeholder="对应颜色 / 规格" aria-label="素材颜色"><input id="assetInput" type="file" accept="image/jpeg,image/png,image/webp" multiple ${serverReady?'':'disabled'}></div><div id="assetGrid" style="display:flex;gap:14px;flex-wrap:wrap">${(p.assets||[]).map(a=>`<div style="width:150px"><img src="${esc(a.url)}" alt="${esc(a.name)}" style="width:150px;height:130px;object-fit:contain;background:#f4f5f0;border-radius:7px"><p class="sub" style="overflow-wrap:anywhere">${esc(a.name)}<br>${esc(a.source)} · ${esc(a.color||'未绑定颜色')}</p></div>`).join('')||'<p class="sub">尚未上传真实素材。下方是图片方案占位示意。</p>'}</div>`;document.querySelector('.gallery').before(section);document.getElementById('assetInput').onchange=async e=>{const files=[...e.target.files],source=document.getElementById('assetSource').value,color=document.getElementById('assetColor').value.trim();e.target.disabled=true;let done=0;for(const file of files){try{if(file.size>15*1024*1024)throw Error('超过 15 MB');const r=await fetch('/api/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});const result=await r.json();if(!r.ok)throw Error(result.error);p.assets||=[];p.assets.push({url:result.url,name:file.name,source,color});persist();done++}catch(error){toast(file.name+'：'+error.message)}}render();toast(`已上传 ${done} / ${files.length} 张图片`)}}
window.addEventListener('beforeunload',e=>{if(syncBusy||syncPending){e.preventDefault();e.returnValue='还有修改未保存'}});
async function initServer(){if(!/^https?:$/.test(location.protocol)){syncLabel('离线模式 · 浏览器保存');enhance();return}try{syncLabel('连接数据库…');const r=await fetch('/api/state');if(!r.ok)throw Error('服务不可用');const data=await r.json();serverRevision=data.revision;state=data.state||{products:[],copy:{},reviews:{}};if(!state.products.some(p=>p.id===selected))selected=state.products[0]?.id||'';serverReady=true;try{const settings=await fetch('/api/settings');if(settings.ok){const active=await settings.json();listingKeyConfigured=!!active.hasKey;listingTextProvider=active.provider||'文案服务'}}catch{}restoreWorkspaceRoute();render();syncLabel(typeof localWorkbenchMode!=='undefined'&&localWorkbenchMode?'已连接本机工作空间':'已连接当前账号工作空间');return true}catch(e){console.error('Workspace initialization failed:',e);syncLabel('工作台加载失败');toast('工作台加载失败：'+e.message);return false}}
function deepSeekCredentialText(settings){
 if(!settings.hasKey)return '尚未配置密钥';
 const health=settings.credentialStatus||{};
 const checked=health.checkedAt ? ' · '+new Date(health.checkedAt).toLocaleString('zh-CN') : '';
 if(health.status==='unauthorized')return '已保存的密钥认证失败（HTTP 401）'+checked+'。请在此更新密钥。';
 if(health.status==='verified')return '密钥已加密保存 · 最近验证通过'+checked;
 return '密钥已加密保存 · 暂无验证记录';
}

function mountModelSettingsLayout(){
 const content=document.getElementById('content');
 const listing=document.getElementById('settingsForm').parentElement;
 const layout=document.createElement('div');
 layout.id='modelSettings';
 layout.className='ds model-settings';
 layout.innerHTML=`<nav class="ds-actions" aria-label="模型设置板块">
   <button type="button" class="ds-button" data-model-group="copy" aria-controls="modelGroup-copy">文案模型</button>
   <button type="button" class="ds-button" data-model-group="images" aria-controls="modelGroup-images">图片模型</button>
   <button type="button" class="ds-button" data-model-group="agent" aria-controls="modelGroup-agent">智能体使用模型</button>
   <button type="button" class="ds-button" data-model-group="seller" aria-controls="modelGroup-seller">卖家精灵接口</button>
 </nav>
 <section id="modelGroup-copy" aria-label="文案模型"></section>
 <section id="modelGroup-images" aria-label="图片模型"></section>
 <section id="modelGroup-agent" aria-label="智能体使用模型">
   <div class="panel box">
     <div class="wb-title-label"><h2>智能体使用模型</h2>${workbenchHelp('默认按固定规则运行。启用 AI 规划时，使用本账号保存的文案模型。','智能体使用模型')}</div>
     <p id="agentModelSummary" role="status">正在读取文案模型…</p>
     <button type="button" class="ds-button" id="agentModelGoToCopy">前往文案模型</button>
   </div>
 </section>
 <section id="modelGroup-seller" aria-label="卖家精灵接口"></section>`;
 content.append(layout);
 layout.querySelector('#modelGroup-copy').append(listing);
 const buttons=[...layout.querySelectorAll('[data-model-group]')];
 function select(group){
  for(const button of buttons){
   const active=button.dataset.modelGroup===group;
   button.setAttribute('aria-pressed',String(active));
   layout.querySelector('#'+button.getAttribute('aria-controls')).hidden=!active;
  }
 }
 buttons.forEach(button=>button.onclick=()=>select(button.dataset.modelGroup));
 layout.querySelector('#agentModelGoToCopy').onclick=()=>{
  select('copy');
  buttons[0].focus();
 };
 select('copy');
 return layout;
}

function appendModelSettingsPanel(group,panel){
 document.getElementById('modelGroup-'+group)?.append(panel);
}

function showAgentModel(layout,config){
 const summary=layout.querySelector('#agentModelSummary');
 summary.textContent=config
  ? '当前 AI 规划模型 · '+(config.model||'未配置')+(config.hasKey?'':'（尚未配置服务密钥）')
  : '文案模型配置读取失败，请刷新后重试';
}
async function renderSettings(){
 const content=document.getElementById('content');
 content.innerHTML=title('模型设置','管理工作台使用的模型与数据接口。')+`<div class="panel box" style="max-width:760px">
 <div class="wb-title-label"><h2>文案模型</h2><span id="textEndpoint"></span></div><form id="settingsForm">
 <label class="field"><span>服务</span><select id="textProvider">
 <option value="deepseek">DeepSeek 直连</option><option value="codex_tools">Codex Tools 本机反代</option></select></label>
 <label class="field"><span>模型 ID</span><input id="textModel" list="textModelOptions" required maxlength="160" style="width:100%"><datalist id="textModelOptions"></datalist></label>
 <label class="field" id="textKeyRow"><span>DeepSeek 密钥</span><input id="apiKey" type="password" autocomplete="off" placeholder="留空保留现有密钥" style="width:100%"></label>
 <p id="keyState" role="status">读取配置中…</p>
 <div class="actions"><button class="btn primary" id="saveSettings">保存配置</button>
 <button class="btn" type="button" id="testKey">测试连接</button>
 <button class="btn" type="button" id="clearKey">清除 DeepSeek 密钥</button></div><p id="testResult" role="status"></p></form></div>`;
 const layout=mountModelSettingsLayout();
 const form=layout.querySelector('#settingsForm');
 const field=(id)=>form.querySelector('#'+id);
 const service=field('textProvider'), model=field('textModel'), key=field('apiKey');
 const status=field('keyState'), saveButton=field('saveSettings'), clearButton=field('clearKey');
 const models={deepseek:'deepseek-flash',codex_tools:'gpt-6-sol'};
 let config,previous;
 function syncFields(){
  const isProxy=service.value==='codex_tools';
  field('textKeyRow').style.display=isProxy?'none':'';
  key.disabled=isProxy;
  clearButton.hidden=isProxy;
  layout.querySelector('#textEndpoint').innerHTML=workbenchHelp(isProxy
   ? 'http://127.0.0.1:8787/v1/chat/completions · 使用图片模型中已配置的本机反代密钥。'
   : 'https://api.deepseek.com/chat/completions · 密钥按当前账号加密保存。','文案模型');
  const active=config?.provider==='Codex Tools'?'codex_tools':'deepseek';
  status.textContent=!config?'模型配置读取失败'
   : service.value!==active?'保存后切换到所选服务'
   : isProxy?(config.hasKey?'已配置本机反代密钥':'请先在图片模型设置中配置本机反代密钥')
   : deepSeekCredentialText(config);
 }
 function loadConfig(value){
  config=value;
  Object.assign(models,value.savedModels||{});
  listingTextProvider=value.provider||'文案服务';
  service.value=value.provider==='Codex Tools'?'codex_tools':'deepseek';
  model.value=value.model||models[service.value];
  previous=service.value;
  key.value='';
  listingKeyConfigured=!!value.hasKey;
  syncFields();
  showAgentModel(layout,value);
 }
 saveButton.disabled=true;
 clearButton.disabled=true;
 try{
  const response=await fetch('/api/settings');
  if(!response.ok)throw Error('模型配置读取失败');
  if(!layout.isConnected)return;
  loadConfig(await response.json());
 }catch(error){
  status.textContent=error.message;
  showAgentModel(layout,null);
  return;
 }
 saveButton.disabled=false;
 clearButton.disabled=false;
 service.onchange=()=>{
  models[previous]=model.value;
  model.value=models[service.value];
  previous=service.value;
  field('textModelOptions').replaceChildren();
  field('testResult').textContent='';
  syncFields();
 };
 async function save(clear){
  saveButton.disabled=true;
  clearButton.disabled=true;
  try{
   const body={provider:service.value,model:model.value.trim(),path:'/chat/completions'};
   if(service.value==='deepseek')Object.assign(body,{api_key:key.value,clearKey:clear,verifyKey:true});
   const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   const result=await response.json();
   if(!response.ok)throw Error(result.error);
   loadConfig(result);
   toast(clear?'DeepSeek 密钥已清除':'文案模型已保存');
  }catch(error){toast(error.message)}
  finally{saveButton.disabled=false;clearButton.disabled=false}
 }
 const testButton=field('testKey'), testResult=field('testResult');
 testButton.onclick=async()=>{
  const selected=service.value, inputKey=key.value;
  testButton.disabled=true;
  testResult.textContent='正在查询模型列表…';
  try{
   const body={provider:selected};
   if(selected==='deepseek')body.api_key=inputKey;
   const response=await fetch('/api/settings/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   const result=await response.json();
   if(!response.ok)throw Error(result.error);
   if(service.value!==selected||key.value!==inputKey)return;
   field('textModelOptions').replaceChildren(...result.models.map(id=>new Option(id,id)));
   testResult.textContent='已连接 · '+result.message;
  }catch(error){if(service.value===selected)testResult.textContent=error.message}
  finally{testButton.disabled=false}
 };
 form.onsubmit=(event)=>{event.preventDefault();void save(false)};
 clearButton.onclick=()=>void save(true);
}
const enhanceBase=enhance;
enhance=function(){enhanceBase();if(view!=='listing')return;const p=product(),notice=document.querySelector('.notice');notice.textContent=serverReady?'先核对产品事实，再生成当前站点文案。生成成功后直接更新当前文案，生成提示和竞品研究可在下方展开。':'离线预览模式：登录并连接本机服务后，才可保存到团队产品库和调用文案模型。';const actions=document.querySelector('.titlebar .actions'),button=document.createElement('button');button.className='btn primary';button.id='generateListing';button.textContent=serverReady?'生成 '+market+' 站文案':'连接服务后生成文案';button.disabled=!serverReady;button.title=serverReady?(listingKeyConfigured?'调用已配置的文案模型':'文案服务密钥未配置'):'请登录并连接本机服务';button.onclick=()=>generateCurrent(p,market,button);actions.prepend(button);if(serverReady&&!listingKeyConfigured){button.classList.remove('primary');button.textContent='配置文案模型';button.disabled=false;button.onclick=()=>{view='settings';render()}}mountListingProgress();const warning=state.copy[p.id+'-'+market]?.warnings;if(warning){const el=document.createElement('div');el.className='notice';el.dataset.foldTitle='生成结果核对';el.style.whiteSpace='pre-wrap';el.textContent='以下为模型自述和软件发现的核对项，并非自动判定：\n'+warning;document.querySelector('.grid').before(el)}};
function keywordVolumeTier(v){return typeof v!=='number'?2:(v>=500&&v<=8000?0:1)}
function listingGenerationProduct(p,targetMarket){const fields=['name','brand','manufacturer','style','material','dimensions','packageDimensions','productWeight','packageWeight','targetAudience','copyNotes','packageContents','colorNames','sizeNames','features','keywords'];const payload=Object.fromEntries(fields.map(k=>[k,p[k]||'']));const report=p.competitorResearch?.[targetMarket];if(report)payload.competitorResearch={[targetMarket]:{structure:String(report.structure||'').slice(0,6000),keywords:(report.keywords||[]).filter(k=>k.selected!==false).map(k=>{const volumes=(k.metrics||[]).map(m=>m.searches).filter(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0);return {phrase:k.phrase,placement:k.placement||'bullets',selected:true,metrics:(k.metrics||[]).map(m=>Object.fromEntries(['asin','seed','searches','products','naturalRank','updatedTime','purchases','purchaseRate','trafficPercentage','month','conversionKeywordType','relevancy'].filter(key=>m[key]!==undefined).map(key=>[key,m[key]]))),monthlySearches:volumes.length?Math.max(...volumes):null}}).sort((a,b)=>{const maximum=(k,key)=>{const values=k.metrics.map(m=>m[key]).filter(v=>typeof v==='number'&&Number.isFinite(v));return values.length?Math.max(...values):-1};return keywordVolumeTier(a.monthlySearches)-keywordVolumeTier(b.monthlySearches)||maximum(b,'purchaseRate')-maximum(a,'purchaseRate')||maximum(b,'purchases')-maximum(a,'purchases')||maximum(b,'trafficPercentage')-maximum(a,'trafficPercentage')||(b.monthlySearches??-1)-(a.monthlySearches??-1)}).slice(0,45)}};const insight=p.reviewResearch?.[targetMarket];if(insight?.analysis)payload.reviewResearch={[targetMarket]:{analysis:insight.analysis}};const source=p.sourceImport;if(source)payload.sourceImport={amazon:{title:source.amazon?.title||''},supplier:{title:source.supplier?.title||''}};payload.supplierScope=p.supplierScope||'';return payload}
function listingBatchReason(p,targetMarket){
 const key=p.id+'-'+targetMarket;
 if(activeListingGeneration.has(key))return '已有文案任务在运行';
 if(listingFields.every(([field])=>listingLocked(state.copy[key],field)))return '全部字段已锁定';
 if(![p.material,p.dimensions,p.features,p.style].some(v=>typeof v==='string'&&v.trim()))return '缺少已核实的产品特征';
 return '';
}
let listingBatchRunning=false;
async function generateCurrent(p,targetMarket,button,options={}){
 if(typeof durableGenerateCurrent!=='function')throw Error('文案任务模块未加载，请刷新工作台');
 return durableGenerateCurrent(p,targetMarket,button,options);
}
function listingBatchDialog(){
 if(listingBatchRunning)return toast('当前批量生成尚未结束');
 const modal=document.getElementById('modal'),owner=signedUser;
 const products=state.products.filter(p=>p.status!=='已归档');
 modal.innerHTML=`<h2>批量生成 Listing 文案</h2><p>每批最多 10 款，按各产品站点顺序生成。成功后逐款更新当前文案。每款可能产生模型 API 费用。</p>
 <div style="max-height:45vh;overflow:auto">${products.map((p,i)=>{
   const site=p.sourceImport?.market||p.market||'US';
   const reason=p.listingGeneration?.[site]?.status==='uncertain'?'上次生成结果待核对':listingBatchReason(p,site);
   return `<label class="field"><input type="checkbox" value="${i}" ${reason?'disabled':''}> ${esc(p.name)} · ${esc(site)}${reason?' · '+esc(reason):''}</label>`;
 }).join('')||'<p>暂无产品</p>'}</div>
 <div id="listingBatchResults" role="status" style="max-height:25vh;overflow:auto"></div>
 <div class="actions"><button class="btn" id="listingBatchClose">关闭</button><button class="btn primary" id="listingBatchStart">生成并更新文案</button></div>`;
 const close=modal.querySelector('#listingBatchClose'),start=modal.querySelector('#listingBatchStart');
 const results=modal.querySelector('#listingBatchResults');
 close.onclick=()=>modal.close();
 modal.oncancel=event=>{if(listingBatchRunning)event.preventDefault()};
 modal.onclose=()=>{modal.oncancel=null;modal.onclose=null};
 start.onclick=async()=>{
  const checked=Array.from(modal.querySelectorAll('input:checked'),input=>products[Number(input.value)]);
  if(listingBatchRunning||signedUser!==owner)return;
  if(!checked.length||checked.length>10)return toast('请选择 1 至 10 款产品');
  if(!confirm('确认向 '+listingTextProvider+' 发送已选 '+checked.length+' 款产品的文字资料，生成成功后直接更新各款文案？'))return;
  listingBatchRunning=true;start.disabled=close.disabled=true;
  modal.querySelectorAll('input').forEach(input=>{input.disabled=true});
  results.replaceChildren();
  try{
   for(const p of checked){
    if(signedUser!==owner||syncConflict)break;
    const site=p.sourceImport?.market||p.market||'US';
    const line=document.createElement('p');
    line.textContent=p.name+' · '+site+' · 处理中';results.append(line);
    const result=await generateCurrent(p,site,start,{batch:true});
    line.textContent=p.name+' · '+site+' · '+result.message;
   }
  }finally{
   listingBatchRunning=false;close.disabled=false;
   if(signedUser===owner&&modal.open&&view==='listing')render();
  }
 };
 modal.showModal();
}
// Session bootstrap in auth.js starts initServer after login.
