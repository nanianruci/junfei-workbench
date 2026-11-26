/* Account-scoped task center. Fixed tools; model plans never become executable code. */
names.agent='任务中心';
const agentPreviousRender=render;
let agentEpoch=0,agentTimer=null,agentSelectedTask=null;
const agentLabels={queued:'待开始',running:'执行中',paused:'已暂停',blocked:'待补齐',needs_review:'需核对',erp_pending:'ERP回执待核对',library_ready:'待存入钧飞',inspection_done:'检查完成',done:'已完成'};
const agentStages={plan:'任务规划',facts:'选品与货源',research:'竞品关键词',listing:'Listing文案',images:'商品图片',export:'交付导出',junfeiPreflight:'钧飞本地预检',publish:'钧飞刊登',verify:'在售核对'};
const agentStageOrder=['plan','facts','research','listing','images','export','junfeiPreflight','publish','verify'];

function agentCanPreviewImages(task){
 return task.executionScope==='selection_to_copy'&&task.status==='blocked'&&task.profile?.mode==='draft';
}
async function agentImagePreparationDialog(task,epoch){
 const owner=typeof signedUser==='undefined'?null:signedUser;
 if(!owner)return toast('请先登录当前账号');
 try{
  const revision=serverRevision;
  if(!Number.isInteger(revision)||revision<0)throw Error('请先保存并刷新工作台资料，再预览图片清单');
  const data=await agentApi('image-preparation?id='+encodeURIComponent(task.id)+'&revision='+revision);
  if(owner!==signedUser||epoch!==agentEpoch||view!=='agent'||agentSelectedTask!==task.id)return;
  const plan=data.preparation,modal=document.getElementById('modal');
  modal.innerHTML=`<h2>文案后图片准备清单</h2><p>仅使用已保存且通过重检的资料。未保存修改不会进入清单；请先保存并更新资料重检。</p><p class="notice">不会生成或下载图片、调用模型、预留费用、启动任务、导出或操作ERP。图片像素、库存与审核未在这里核验。预算不是全流程费用硬上限。</p><p>${plan.readyForManualPreparation?'元数据检查通过，可按清单人工准备素材。':'请先补齐以下元数据或核对原记录。'}计划 ${esc(plan.plannedColorCount)} 色 × ${esc(plan.galleryCountPerColor??'未配置')} 图，合计 ${esc(plan.plannedImageCount??'未知')} 图；这不是已完成图片数。</p>${plan.blockers?.length?`<ul>${plan.blockers.map(issue=>`<li>${esc(issue)}</li>`).join('')}</ul>`:''}<ol>${(plan.sets||[]).map(set=>`<li>${esc(set.color)} · ${set.role==='master'?'先完成整套母版':'母版审核后制作整套换色'} · ${esc(set.children.length)} 个实际子体，${esc(set.references.length)} 条素材映射（未读图）<p>${set.children.map(child=>esc(child.sku||'缺少实际子SKU')+' / '+esc(child.size)).join('；')}</p></li>`).join('')}</ol><details><summary>完整只读清单</summary><pre style="white-space:pre-wrap;max-height:40vh;overflow:auto">${esc(JSON.stringify(plan,null,2))}</pre></details><div class="actions"><button class="btn" id="agentImagePreparationClose">关闭</button><button class="btn" id="agentImagePreparationDownload">下载准备清单</button></div>`;
  modal.showModal();
  document.getElementById('agentImagePreparationClose').onclick=()=>modal.close();
  document.getElementById('agentImagePreparationDownload').onclick=()=>{if(owner!==signedUser||epoch!==agentEpoch||view!=='agent'||agentSelectedTask!==task.id||!modal.open)return;download('image-preparation-'+task.id+'.json',JSON.stringify(plan,null,2));};
 }catch(error){if(owner===signedUser&&epoch===agentEpoch&&view==='agent'&&agentSelectedTask===task.id)toast(error.message)}
}

function agentCanPrepareCopyHandoff(task){
 return task.intake?.source==='selection-report'&&task.taskMode==='production'&&task.status==='blocked'&&task.profile?.mode==='draft'&&!task.copyHandoff&&!task.exportAvailable&&!task.junfeiPlanAvailable&&!['export','junfeiPreflight','publish','verify'].some(key=>task.results?.[key]);
}
function agentTaskStageOrder(task){
 if(task.erpWorkflowVersion===2)return ['plan','junfeiPreflight','publish'];
 if(task.executionScope==='selection_to_copy')return agentStageOrder.slice(0,4);
 return task.libraryDraftOnly?agentStageOrder.slice(0,-1):agentStageOrder;
}
async function agentCopyHandoffDialog(task,epoch){
 const modal=document.getElementById('modal');
 modal.innerHTML=`<h2>原选品任务接续到文案</h2><p>${esc(task.productName)} · ${esc(task.profile.store)} · ${esc(task.profile.marketplace)}</p><p>保存前重新核验当前选品和货源资料。通过后只在原任务排队，点击“开始”才调用研究与文本服务；文案仍须采用和审核。</p><p class="notice">本次范围不含生图、导出、上传、ERP保存或发布。人民币金额只是预算记录，不是账单硬上限；外部服务可能收费，缺报价时不能保证实际费用不超过此数。</p><form id="agentCopyHandoffForm"><label class="field">本任务预算（人民币）<input id="agentCopyHandoffBudget" type="number" min="0.01" step="0.01" required></label><label class="field">已核验竞品 ASIN（可空，最多5个）<input id="agentCopyHandoffAsins" value="${esc((task.researchAsins||[]).join(', '))}"></label><p class="sub">留空时沿用可用研究，或从已采集前台观察中找研究候选。没有候选会停止，不伪造竞品。</p><label><input id="agentCopyHandoffConsent" type="checkbox" required> 我同意此任务使用已配置的研究与文本服务，并理解预算记录不是费用硬上限。</label><div class="actions" style="margin-top:14px"><button class="btn" type="button" id="agentCopyHandoffCancel">取消</button><button class="btn primary" type="submit" id="agentCopyHandoffSave">保存并排队，不立即执行</button></div></form>`;
 let active=true,busy=false;
 const form=document.getElementById('agentCopyHandoffForm'),save=document.getElementById('agentCopyHandoffSave');
 modal.addEventListener('close',()=>{active=false},{once:true});
 modal.showModal();
 document.getElementById('agentCopyHandoffCancel').onclick=()=>modal.close();
 form.onsubmit=async event=>{
  event.preventDefault();
  if(busy||!active)return;
  const budget=Number(document.getElementById('agentCopyHandoffBudget').value),consent=document.getElementById('agentCopyHandoffConsent').checked,asins=document.getElementById('agentCopyHandoffAsins').value;
  if(!Number.isFinite(budget)||budget<=0||!consent)return toast('请填写正数预算并确认费用与执行范围');
  busy=true;save.disabled=true;
  try{
   await flushWorkspace();
   if(!active||epoch!==agentEpoch||view!=='agent'||agentSelectedTask!==task.id)return;
   await agentApi('prepare-copy-handoff',{id:task.id,budgetCny:budget,researchAsins:asins,workspaceRevision:serverRevision,confirmPaidText:true,acknowledgeBudgetNotHardCap:true});
   toast('原任务已排队，尚未调用服务；点击开始后止于文案');
   if(active&&epoch===agentEpoch&&view==='agent'&&agentSelectedTask===task.id){modal.close();await agentRefresh(epoch)}
  }catch(error){if(active)toast(error.message)}
  finally{busy=false;if(save.isConnected)save.disabled=false}
 };
}

function agentStageLabel(key,result,task){
 if(task.erpWorkflowVersion===2&&key==='junfeiPreflight')return '正式成品预检';
 if(task.erpWorkflowVersion===2&&key==='publish')return 'ERP草稿交付';
 if(key==='publish'&&task.libraryDraftOnly)return '存入钧飞产品库';
 return key==='facts'&&result?.selection?.skipped?'货源资料（选品未执行）':agentStages[key]||key;
}
function agentGeneratedGalleryStatus(result){
 if(result.gallerySource!=='generated_jobs')return '';
 const images=result.selectedGeneratedImages||[];
 return `<details class="agent-generated-gallery"><summary>已选生成套图：${images.length}张，${result.agentReviewedForDraft?'智能体草稿审核通过':'待审核'}</summary><p>按交付导出顺序检查当前账号与产品的任务。审核绑定商品资料和原图哈希；智能体审核不记作人工审核或在售确认。</p><ol>${images.map(i=>`<li>第${Number(i.slot)+1}图 · ${esc(i.jobId)} · 结果序号${Number(i.imageIndex)+1} · ${i.agentReviewedForDraft?'智能体已核图（仅草稿）':'待审核'}</li>`).join('')}</ol></details>`;
}
function agentStageBar(task){
 const cursor=Number(task.cursor||0), results=task.results||{}, order=agentTaskStageOrder(task);
 const blockedIndex=task.status==='blocked'?order.findLastIndex(key=>results[key]&&!results[key].ok):-1;
 return `<div class="agent-stagebar" aria-label="固定流程进度">${order.map((key,index)=>{
   const done=Boolean(results[key]?.ok)&&!results[key]?.skipped;
   const failed=results[key]&&!results[key].ok;
   const current=!done&&index===(blockedIndex>=0?blockedIndex:cursor)&&['queued','running','paused','blocked','needs_review','library_ready','erp_pending'].includes(task.status);
   return `<div class="agent-stage ${done?'done':''} ${current?'current':''}"><span>${done?'✓':failed?'!':index+1}</span><small>${esc(agentStageLabel(key,results[key],task))}</small></div>`;
 }).join('')}</div>`;
}
function agentPricingPanel(result){
 const rows=Object.entries(result.pricingBasis||{});
 if(!rows.length)return '';
 return `<details><summary>草稿估价与物流依据</summary><p>按已提供报价表计算的基础情景，仅供草稿。尚未核实报价有效期、渠道尺寸限制及额外费用。</p><table><thead><tr><th>SKU</th><th>渠道 / 报价日期</th><th>计费重</th><th>估算运费</th><th>草稿估价</th></tr></thead><tbody>${rows.map(([sku,q])=>`<tr><td>${esc(sku)}</td><td>${esc(q.channelName||q.channel)} / ${esc(q.sourceDate||'未知')}</td><td>${esc(q.chargeableKg)} kg</td><td>¥${esc(q.freightRmb)}</td><td>$${esc(q.priceUsd)} USD</td></tr>`).join('')}</tbody></table><p>未计附加费和其他费用是基础情景假设，不代表这些费用不存在。</p></details>`;
}
function agentNeedsPopup(issue,productId){const modal=document.getElementById('modal');modal.innerHTML=`<h2>需要补充的信息</h2><p style="margin:14px 0;color:var(--ink)">${esc(issue)}</p><p>请只填写供应商资料、实测数据或你已确认的经营参数。补齐后返回任务列表，点击“更新资料重检”，保留原任务记录继续检查。已有交付或刊登结果的任务需先核对，不自动重发。</p><div class="actions" style="margin-top:18px"><button class="btn" id="agentNeedClose">关闭</button>${productId?'<button class="btn primary" id="agentNeedFacts">打开产品资料</button>':''}</div>`;modal.showModal();document.getElementById('agentNeedClose').onclick=()=>modal.close();if(productId)document.getElementById('agentNeedFacts').onclick=()=>{modal.close();openListingWorkspace(productId);setTimeout(()=>document.getElementById('modal')&&typeof editFacts==='function'&&editFacts(),0)}}
async function agentApi(path,data){
 const response=await fetch('/api/agent/'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
 const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;
}
render=function(){
 clearTimeout(agentTimer);agentEpoch++;
 if(view!=='agent')return agentPreviousRender();
 const old=renderGuide;renderGuide=renderAgentCenter;
 try{view='guide';agentPreviousRender()}finally{view='agent';renderGuide=old}
 renderNavigation();
};
function renderAgentCenter(){
 const epoch=agentEpoch;
 document.getElementById('content').innerHTML='<div class="agent-center">'+title('智能体任务中心','现有成品可沿用产品交付页的账号目标、文件和原回执，创建 ERP 草稿交付任务。也可创建资料检查或研究任务。', '<button class="btn" id="agentRefresh">刷新任务</button>')+`
 <section class="panel box"><div class="wb-title-label"><h2>店铺轮转任务</h2>${workbenchHelp('按所填店铺顺序，每店每轮处理 3 款新商品。保存配置后可将启动指令交给 Codex。','店铺轮转任务')}</div><div class="actions"><button class="btn primary" id="agentRotationSetupOpen" type="button">配置轮转店铺</button> <button class="btn" id="agentRotationRunOpen" type="button">打开轮转检查点</button></div><p class="sub" id="agentRotationSetupSummary" aria-live="polite">正在读取轮转配置…</p></section>
 <div class="notice" id="publicationFollowup" hidden></div>
 <section class="panel box" id="publicationTasks"></section>
 <section class="panel box"><div class="wb-title-label"><h2>店铺与站点</h2>${workbenchHelp('店铺档案按账号保存。任务使用创建时的档案快照。','店铺与站点')}</div>
 <form id="agentProfileForm"><div class="agent-fields">
 <label class="field">已有档案<select id="agentSavedProfile"><option value="">新建店铺档案</option></select></label>
 <label class="field">档案名称<input name="name" required maxlength="200" value="示例店铺-美国 · US" placeholder="例如 店铺A · 美国站"></label>
 <label class="field">钧飞ERP实际店铺名<input name="store" required maxlength="200" value="示例店铺-美国"></label>
 <label class="field">站点<select name="marketplace">${['US','UK','DE','FR','IT','ES','JP','CA','AU','IN','MX','BR'].map(s=>`<option ${s==='US'?'selected':''}>${s}</option>`).join('')}</select></label>
 <label class="field">运费模板<input name="shippingTemplate" required maxlength="200" value="Migrated Template"></label>
 <label class="field">每子变体库存<input name="quantity" type="number" value="88" min="0" max="1000000" step="1" required></label>
 <label class="field">刊登目标<select name="mode"><option value="draft">先保存草稿</option><option value="publish" disabled>正式发布请使用产品交付页入口</option></select></label>
 </div><button type="submit" class="btn">保存店铺档案</button><button type="button" class="btn primary" id="agentSaveDefaultProfile" style="margin-left:8px">一键保存默认档案</button></form></section>
 <section class="panel box"><div class="wb-title-label"><h2>创建任务</h2>${workbenchHelp('现有单款的数量填 1。启用 AI 规划、自动竞品研究或自动文案时须填预算。固定规则流程无模型费用。','创建任务')}</div><form id="agentTaskForm"><div class="agent-fields">
 <label class="field agent-field-wide">使用店铺<select name="profileId" required id="agentTaskProfile"><option value="">请先保存店铺档案</option></select></label>
 <label class="field agent-field-wide">正式ERP目标<select name="erpDestinationId" id="agentTaskErpDestination" disabled><option value="">请选择本账号正式采集目标</option></select></label>
 <label class="field agent-field-wide">产品<select name="productId"><option value="">从选品开始（执行器待接通）</option>${state.products.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.sku||p.id)}</option>`).join('')}</select></label>
 <label class="field agent-field-wide">执行范围<select name="taskMode"><option value="erp_draft">正式ERP草稿交付：成品预检 → 采集/原回执核对</option><option value="inspect">准备检查：查缺项，不生成不发布</option><option value="production">研究与文案准备（历史流程兼容）</option></select></label>
 <label class="field">目标款数<input name="targetCount" type="number" min="1" max="30" step="1" value="1" required></label>
 <label class="field">AI 预算（人民币）<input name="budgetCny" type="number" min="0.01" step="0.01" placeholder="启用 AI 时必填"></label>
 </div><label class="field"><span>任务目标</span><textarea name="goal" required maxlength="2000" placeholder="例如：检查这款产品能否制作美国站Listing，整理缺项；合格后导出。"></textarea></label>
 <label style="display:block;margin:12px 0"><input type="checkbox" name="aiPlanning"> 使用AI拆解目标（开始执行后调用一次已配置的文本模型，会产生服务商费用）</label>
 <div class="wb-title-label wb-field-help"><label><input type="checkbox" name="autoSourcing" checked> 自动核查 1688 货源</label>${workbenchHelp('只读检查已填链接，或核查已采集候选的前三条详情。候选采用、价格和重量需另行确认。','货源核查')}</div>
 <label style="display:block;margin:12px 0"><input type="checkbox" name="autoResearch"> 自动获取竞品正文、反查词并分析（生产任务可用；使用卖家精灵额度及付费文本模型，费用待账单核实；已有可用研究不重查）</label>
 <label class="field">竞品ASIN（可留空，先生成核验计划）<input name="researchAsins" maxlength="200" placeholder="核验后填写1–5个，例如 B0XXXXXXXX"></label>
 <label style="display:block;margin:12px 0"><input type="checkbox" name="autoListing"> 生产流程缺文案时自动生成待审核草稿（资料和关键词需齐全；含候选、编辑等多次付费文本请求，费用未知，不硬性封顶；失败不自动重试）</label>
 <label style="display:block;margin:12px 0"><input type="checkbox" name="startImmediately"> 创建后立即开始固定流程（创建成功后自动启动；仍会在每个阶段按资料、预算和证据门槛停止）</label>
 <button class="btn primary" style="margin-top:12px" type="submit">创建任务</button></form></section>
 <section class="panel box"><h2>任务列表</h2><div id="agentSummary" class="sub" aria-live="polite">正在读取任务统计…</div><button class="btn primary" id="agentStartQueued" type="button" style="margin:8px 0">启动全部待开始任务</button><button class="btn" id="agentPauseRunning" type="button" style="margin:8px 0 8px 8px">暂停全部执行中任务</button><div id="agentTasks" aria-live="polite">正在读取…</div><div id="agentCliTasks"></div></section>
 <section class="panel box" id="agentImageSection"><h2>图片与换色任务</h2><div id="agentImageTasks" aria-live="polite">正在读取…</div></section>
 <section class="panel box" id="agentDetail"><h2>执行记录</h2><p>选择一个任务查看逐步结果。</p></section>
 <details class="panel box"><summary>工具接入状态</summary><div id="agentCapabilities"></div></details></div>`;
 const style=document.createElement('style');style.textContent='#agentTasks .reviewitem{padding:16px 0}.agent-stagebar{display:flex;gap:4px;margin:14px 0 4px;overflow:auto;padding-bottom:5px}.agent-stage{display:flex;align-items:center;gap:5px;min-width:82px;color:#8290a0;font-size:11px}.agent-stage span{display:grid;place-items:center;width:22px;height:22px;border:1px solid #cbd5df;border-radius:50%;font-size:11px}.agent-stage.done{color:#22734b}.agent-stage.done span{background:#22734b;color:white;border-color:#22734b}.agent-stage.current{color:#1f5e9c;font-weight:600}.agent-stage.current span{border:2px solid #2979c8;color:#2979c8}.agent-stage small{white-space:nowrap}.agent-next{margin:8px 0;color:#5b6b7d;font-size:12px}#agentDetail pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f5;padding:14px;border-radius:7px;max-height:300px;overflow:auto}.agent-step{padding:12px 0;border-bottom:1px solid var(--line)}';document.getElementById('content').append(style);
 document.getElementById('agentRotationRunOpen').onclick=()=>window.openStoreRotationRun(()=>view==='agent'&&agentEpoch===epoch);
 document.getElementById('agentRotationSetupOpen').onclick=()=>window.openStoreRotationSetup(()=>view==='agent'&&agentEpoch===epoch);
 void window.refreshStoreRotationSetup(()=>view==='agent'&&agentEpoch===epoch);
 const taskForm=document.getElementById('agentTaskForm');
 if(typeof mountPublicationTasks==='function')void mountPublicationTasks(document.getElementById('publicationTasks'));
 let sourcingOptedOut=false;const syncSourcingToggle=()=>{const formal=taskForm.elements.taskMode.value==='erp_draft',production=taskForm.elements.taskMode.value==='production';taskForm.elements.autoSourcing.disabled=!production;taskForm.elements.autoSourcing.checked=production&&!sourcingOptedOut;for(const key of ['aiPlanning','autoResearch','autoListing']){taskForm.elements[key].disabled=formal;if(formal)taskForm.elements[key].checked=false}};
 taskForm.elements.autoSourcing.addEventListener('change',()=>{if(taskForm.elements.taskMode.value!=='inspect')sourcingOptedOut=!taskForm.elements.autoSourcing.checked});
 taskForm.elements.taskMode.addEventListener('change',syncSourcingToggle);syncSourcingToggle();
 if(state.products.some(p=>p.id===selected))taskForm.elements.productId.value=selected;
 taskForm.elements.goal.value='检查产品资料、竞品关键词、Listing文案和图片，列出缺项；合格后按所选范围推进。';
 document.getElementById('agentRefresh').onclick=()=>agentRefresh(epoch);
 document.getElementById('agentProfileForm').onsubmit=async event=>{
  event.preventDefault();const form=event.target,button=form.querySelector('button'),data=Object.fromEntries(new FormData(form));
  data.id=document.getElementById('agentSavedProfile').value||undefined;data.quantity=Number(data.quantity);button.disabled=true;
  try{await agentApi('profile',data);toast('店铺档案已保存');await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}
 };
 document.getElementById('agentSaveDefaultProfile').onclick=async()=>{
  const form=document.getElementById('agentProfileForm'),button=document.getElementById('agentSaveDefaultProfile');
  const data={name:'示例店铺-美国 · US',store:'示例店铺-美国',marketplace:'US',shippingTemplate:'Migrated Template',quantity:88,mode:'draft'};
  button.disabled=true;try{await agentApi('profile',data);toast('默认店铺档案已保存');await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}
 };
 taskForm.onsubmit=async event=>{
  event.preventDefault();const form=event.target,button=form.querySelector('button');button.disabled=true;
  try{
   await flushWorkspace();
   const data=Object.fromEntries(new FormData(form));data.aiPlanning=form.elements.aiPlanning.checked;data.autoListing=form.elements.autoListing.checked;data.autoResearch=form.elements.autoResearch.checked;data.autoSourcing=form.elements.autoSourcing.checked;const startImmediately=form.elements.startImmediately.checked;
   data.targetCount=Number(data.targetCount);data.budgetCny=data.budgetCny?Number(data.budgetCny):null;
   if((data.aiPlanning||data.autoResearch||data.autoListing)&&data.budgetCny===null){
    throw new Error('启用AI规划、自动竞品研究或自动文案前，请先填写任务预算（人民币）');
   }
   const result=await agentApi('create',data);agentSelectedTask=result.task.id;
   if(startImmediately){await agentApi('start',{id:agentSelectedTask});toast('任务已创建并开始执行')}else toast('任务已创建，点击开始执行');
   await agentRefresh(epoch);
  }catch(error){toast(error.message)}finally{button.disabled=false}
 };
 queueMicrotask(()=>agentRefresh(epoch));
}
async function agentRefresh(epoch){
 if(epoch!==agentEpoch||view!=='agent')return;
 clearTimeout(agentTimer);
 try{
  const data=await agentApi('overview');if(epoch!==agentEpoch||view!=='agent')return;
  await agentSyncResearchState(data.tasks,epoch);
  if(epoch!==agentEpoch||view!=='agent')return;
  const profileSelect=document.getElementById('agentSavedProfile'),taskSelect=document.getElementById('agentTaskProfile');
  const previousProfile=profileSelect.value,previousTask=taskSelect.value;
  const options=data.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.marketplace)}</option>`).join('');
  profileSelect.innerHTML='<option value="">新建店铺档案</option>'+options;taskSelect.innerHTML='<option value="">请选择店铺档案</option>'+options;
  const defaultProfile=data.profiles.find(p=>p.store==='示例店铺-美国'&&p.marketplace==='US');
  const defaultButton=document.getElementById('agentSaveDefaultProfile');
  if(defaultButton){defaultButton.disabled=Boolean(defaultProfile);defaultButton.textContent=defaultProfile?'默认档案已保存':'一键保存默认档案';}
  profileSelect.value=previousProfile;taskSelect.value=previousTask||data.profiles[0]?.id||'';
  const erpSelect=document.getElementById('agentTaskErpDestination');
  const syncErpTargets=()=>{
   const formal=document.getElementById('agentTaskForm').elements.taskMode.value==='erp_draft';
   const prior=erpSelect.value,targets=data.erpDestinations||[];
   taskSelect.disabled=formal;taskSelect.required=!formal;
   taskSelect.closest('label').hidden=formal;
   document.getElementById('agentProfileForm').closest('section').hidden=formal;
   erpSelect.innerHTML='<option value="">请选择正式ERP采集目标</option>'+targets.map(d=>`<option value="${esc(d.id)}">${esc(d.name&&d.name!==d.store?d.name+' · ':'')}${esc(d.store)} · ${esc(d.marketplace)}</option>`).join('');
   erpSelect.value=prior;erpSelect.disabled=!formal;erpSelect.required=formal;
  };
  taskSelect.onchange=syncErpTargets;document.getElementById('agentTaskForm').elements.taskMode.onchange=syncErpTargets;syncErpTargets();
  profileSelect.onchange=()=>{
   const profile=data.profiles.find(p=>p.id===profileSelect.value),form=document.getElementById('agentProfileForm');
  for(const key of ['name','store','shippingTemplate','marketplace','quantity','mode'])form.elements[key].value=profile?.[key]??({name:'示例店铺-美国 · US',store:'示例店铺-美国',shippingTemplate:'Migrated Template',marketplace:'US',quantity:88,mode:'draft'}[key]||'');
  };
  const summary=data.summary||{};document.getElementById('agentSummary').textContent=`待开始 ${summary.queued||0} · 执行中 ${summary.running||0} · 暂停 ${summary.paused||0} · 待补齐 ${summary.blocked||0} · 需核对 ${summary.needs_review||0} · 完成 ${summary.done||0}`;
  const followup=data.publicationFollowup,followupBox=document.getElementById('publicationFollowup');
  const followupUnavailable=followup?.enabled&&!followup.executorAvailable;
  followupBox.hidden=!followupUnavailable;
  if(followupUnavailable)followupBox.textContent='跨站发布尚不可用，当前任务止于 ERP 草稿核对。';
  const cliTasks=data.cliTasks||[],cliProducts=new Set(cliTasks.map(task=>task.productId));
  const startable=data.tasks.filter(task=>task.status==='queued'&&!cliProducts.has(task.productId)).length;
  const startQueued=document.getElementById('agentStartQueued');startQueued.textContent='启动下一款待处理任务';startQueued.disabled=!startable;startQueued.onclick=async()=>{if(!window.confirm('确认启动下一款待处理的网页任务？'))return;startQueued.disabled=true;try{const result=await agentApi('start-queued',{});toast(`已启动 ${result.count} 个任务${result.errors?.length?`，${result.errors.length} 个需核对`:''}`);await agentRefresh(epoch)}catch(error){toast(error.message)}finally{startQueued.disabled=!startable}};
  const pauseRunning=document.getElementById('agentPauseRunning');pauseRunning.disabled=!summary.running;pauseRunning.onclick=async()=>{if(!window.confirm(`确认暂停全部 ${summary.running||0} 个执行中任务？当前工具完成后暂停。`))return;pauseRunning.disabled=true;try{const result=await agentApi('pause-running',{});toast(`已请求暂停 ${result.count} 个任务`);await agentRefresh(epoch)}catch(error){toast(error.message)}finally{pauseRunning.disabled=false}};
  const tasks=document.getElementById('agentTasks');tasks.innerHTML=data.tasks.length?data.tasks.map(t=>{const order=agentTaskStageOrder(t),next=order[Number(t.cursor||0)];return `<div class="reviewitem"><div><strong>${esc(t.productName)}</strong><p>${esc(t.profile.name)} · ${esc(t.profile.marketplace)} · ${esc(t.erpWorkflowVersion===2?'正式ERP草稿交付':t.taskMode==='inspect'?'准备检查':'历史生产流程')}</p>${agentStageBar(t)}<p class="agent-next">${t.reason?`当前原因：${esc(t.reason)}`:next?`下一步：${esc(agentStageLabel(next,t.results[next],t))}`:'流程记录已完成'}</p><small>${esc(agentLabels[t.status]||t.status)} · 已记录${Object.keys(t.results).length}/${order.length}步</small></div><div class="actions"><button class="btn" data-task="${esc(t.id)}">查看</button><a class="btn" href="/api/agent/report?id=${encodeURIComponent(t.id)}" download="agent-report.json">下载审计报告</a>${['queued','paused','erp_pending'].includes(t.status)?`<button class="btn primary" data-start="${esc(t.id)}">${t.status==='erp_pending'?'核对原回执':t.status==='paused'?'继续':'开始'}</button>`:''}${t.status==='needs_review'&&!t.libraryDraftAttempted?`<button class="btn primary" data-resume-review="${esc(t.id)}">核对后继续</button>`:''}${['blocked','inspection_done'].includes(t.status)&&!t.exportAvailable&&(!['junfeiPreflight','publish','verify'].some(k=>t.results[k])||t.erpWorkflowVersion===2&&!t.results.publish)?`<button class="btn" data-recheck="${esc(t.id)}">更新资料重检</button>`:''}${t.status==='running'?`<button class="btn" data-pause="${esc(t.id)}">暂停</button>`:''}${t.exportAvailable?`<a class="btn" href="/api/agent/download?id=${encodeURIComponent(t.id)}" download="delivery.zip">下载交付包</a>`:''}${t.junfeiPlanAvailable?`<a class="btn" href="/api/agent/junfei-plan?id=${encodeURIComponent(t.id)}" download="junfei-preflight.zip">下载钧飞预检包</a>`:''}</div></div>`}).join(''):'<div class="empty">还没有任务。先保存店铺档案，再创建第一款准备检查。</div>';
  tasks.querySelectorAll('[data-task]').forEach(button=>button.onclick=()=>{agentSelectedTask=button.dataset.task;agentShowDetail(epoch)});
  for(const button of tasks.querySelectorAll('[data-start]')){
   const task=data.tasks.find(item=>item.id===button.dataset.start);
   if(task&&cliProducts.has(task.productId)){button.disabled=true;button.textContent='由 CLI 执行';}
  }
  const cliStatus={ready:'待领取',running:'执行中',blocked:'待补齐',uncertain:'需对账',done:'台账流程完成',unavailable:'台账暂不可读'};
  const cliStage={selection:'选品',sourcing:'货源',research:'竞品关键词',listing:'Listing',images:'图片',export:'导出',publish:'ERP提交',verify:'结果核对',complete:'已记录全部阶段'};
  document.getElementById('agentCliTasks').innerHTML=cliTasks.length?`<div class="wb-title-label"><h3>CLI 台账 · 关联产品</h3>${workbenchHelp('这里显示 CLI 的原始记录。可从 CLI 续跑或对账。','CLI 台账 · 关联产品')}</div>${cliTasks.map(task=>`<div class="agent-step"><strong>${esc(task.productId)} · ${esc(cliStatus[task.status]||task.status)}</strong><p>${esc(task.marketplace)} · ${esc(task.asin)} · ${esc(cliStage[task.stage]||'阶段待核对')}</p><small>任务 ${esc(task.id)} · 发现批次 ${esc(task.cycleId)}</small></div>`).join('')}`:'';
  for(const action of ['start','pause','recheck'])tasks.querySelectorAll('[data-'+action+']').forEach(button=>button.onclick=async()=>{
   button.disabled=true;try{agentSelectedTask=button.dataset[action];if(action==='recheck')await flushWorkspace();await agentApi(action,{id:agentSelectedTask});await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}
  });
  tasks.querySelectorAll('[data-resume-review]').forEach(button=>button.onclick=async()=>{
   const reconciliation=window.prompt('请填写已核对的外部结果与继续依据（必填）','已核对最后一步结果，确认未发生重复提交；从当前步骤继续');
   if(!reconciliation?.trim())return;
   button.disabled=true;try{agentSelectedTask=button.dataset.resumeReview;await agentApi('resume-after-review',{id:agentSelectedTask,reconciliation});await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}
  });
  document.getElementById('agentCapabilities').innerHTML=data.capabilities.map(c=>`<div class="agent-step"><strong>${c.available?'✓':'待接入'} ${esc(c.name)}</strong>${c.reason?`<p>${esc(c.reason)}</p>`:''}</div>`).join('');
  const imageTasks=data.imageTasks||[],imageNode=document.getElementById('agentImageTasks');
  imageNode.innerHTML=agentImageTaskPanel(imageTasks);
  imageNode.querySelectorAll('[data-image-open]').forEach(b=>b.onclick=()=>openProduct(b.dataset.imageOpen,'images'));
  imageNode.querySelectorAll('[data-image-control]').forEach(b=>b.onclick=async()=>{
   const task=imageTasks.find(t=>t.id===b.dataset.imageId),action=b.dataset.imageControl;
   if(!task||!window.confirm(action==='pause'?'暂停本地处理？正在处理的一张会保存结果，不再启动后续图位。':'从已保存图位继续本地处理？不会调用生图API；完成后的AI质检可能消耗额度。'))return;
   b.disabled=true;
   try{const r=await fetch('/api/colours/local/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({productId:task.productId,id:task.id,confirmed:true})}),d=await r.json();if(!r.ok)throw Error(d.error||'任务操作失败');await agentRefresh(epoch)}catch(e){toast(e.message)}finally{b.disabled=false}
  });
  if(agentSelectedTask)await agentShowDetail(epoch);
  if(epoch===agentEpoch&&view==='agent')agentTimer=setTimeout(()=>agentRefresh(epoch),data.tasks.some(t=>t.status==='running')||imageTasks.some(t=>['local_preparing','local_rendering','reviewing','generating','validating','queued'].includes(t.status))?2500:15000);
 }catch(error){if(epoch===agentEpoch&&view==='agent'){document.getElementById('agentTasks').textContent='读取失败：'+error.message;agentTimer=setTimeout(()=>agentRefresh(epoch),15000)}}
}
function agentImageTaskPanel(tasks){
 if(!tasks.length)return '<p class="empty">暂无当前产品的换色任务。请从产品档案的图片工作台创建。</p>';
 return tasks.map(t=>`<div class="agent-step"><strong>${esc(t.productName)} · ${esc(t.color)}</strong><p>${t.local?'本地像素换色':'API换色记录'} · ${esc(t.statusLabel)} · 质检通过 ${esc(t.completed)}/${esc(t.total)} 张</p>${t.error?`<p role="alert">${esc(t.error)}</p>`:''}<div class="actions"><button class="btn" data-image-open="${esc(t.productId)}">打开产品图片</button>${t.canPause?`<button class="btn" data-image-control="pause" data-image-id="${esc(t.id)}">暂停本地处理</button>`:''}${t.canResume?`<button class="btn" data-image-control="resume" data-image-id="${esc(t.id)}">继续本地处理</button>`:''}</div></div>`).join('');
}
function agentSelectionObservationPanel(items,targetTitle=''){if(!items?.length)return '';const words=s=>new Set(String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').split(/\s+/).filter(x=>x.length>=2));const target=words(targetTitle);return `<details style="margin:12px 0"><summary>选品页面观察记录（${items.length}，未核验）</summary>${items.map(item=>`<div class="agent-step"><a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.fields?.title||item.search?.query||item.sellerSprite?.pageTitle||item.sourcingSearch?.offers?.[0]?.title||item.asin)} · ${esc(item.capturedAt)}</a>${item.captureType==='browser_front_search'?`<p>标题首句搜索：${esc(item.search?.query)} · 页面显示结果数：${esc(item.search?.visibleResultCount)} · 已采集结果：${item.search?.results?.length||0}条</p>${item.matchReview?.candidates?.length?`<p><strong>按标题词重叠排序的人工复核候选：</strong><br>${item.matchReview.candidates.map(x=>`${esc(x.asin)} · ${esc(x.title)} · 词面相似度 ${(x.lexicalScore*100).toFixed(1)}% · 共词 ${esc(x.sharedTerms.join(', ')||'无')}`).join('<br>')}</p><small>${esc(item.matchReview.warning)}</small>`:''}<p>${esc(item.search?.resultText||'')}</p>`:item.captureType==='browser_sellersprite_search'?`<p>SellerSprite 当前页可见结果：${item.sellerSprite?.rows?.length||0}条；请逐行核对其他 ASIN、同款关系和数值排名。</p>${(item.sellerSprite?.rows||[]).map(row=>`<p><strong>${esc(row.asin)}</strong> · ${esc(row.text)}</p>`).join('')}<small>${esc(item.sellerSprite?.warning||'观察不等于已核验排名')}</small>`:item.captureType==='browser_1688_image_search'?(()=>{const offers=(item.sourcingSearch?.offers||[]).map(offer=>{const candidate=words(offer.title),shared=[...target].filter(word=>candidate.has(word)).sort();return {...offer,_score:shared.length/Math.max(target.size,1),_shared:shared}}).sort((a,b)=>b._score-a._score);return `<p>1688 图片搜索候选：${offers.length} 条；以下按产品标题词面相似度排序，仅作核查顺序建议，不证明同款。</p>${offers.map((offer,index)=>`<p><strong>${index+1}. ${esc(offer.title)}</strong> · 词面 ${(offer._score*100).toFixed(1)}% · 匹配词：${esc(offer._shared.join(', ')||'无')}<br><a href="${esc(offer.url)}" target="_blank" rel="noreferrer">打开供应商详情</a><br>展示价：${esc(offer.priceText||'未读取')} · 店铺：${esc(offer.supplierText||'未读取')}<br>${esc(offer.raw||'')}<br><button class="btn" data-source-offer="${esc(offer.url)}">采用此链接到货源核验</button></p>`).join('')}<small>${esc(item.sourcingSearch?.warning||'图片搜索相似结果不等于同款或权利核验')}</small>`})():`<p>品牌：${esc(item.fields?.brand||'未读取')} · 页面价格：${esc(item.fields?.displayedPrice||'未读取')}</p><p>卖家文本：${esc(item.fields?.sellerText||'未读取')}</p><p>配送文本：${esc(item.fields?.fulfillmentText||'未读取')} · 商品首次可售文本：${esc(item.fields?.dateFirstAvailable||'未读取')}</p>`}<small>${esc(item.limitations||'页面观察不能替代选品准入证据')}</small></div>`).join('')}</details>`}
function agentObservedCandidatePanel(plan){const items=plan?.observedCandidates||[];if(!items.length)return '';return `<details style="margin:8px 0"><summary>智能体推荐核查顺序（${items.length}条，仍未核验）</summary><p class="sub">综合标题和详情页中明确可见的材质、尺寸、颜色、尺码信号排序。详情摘要来自公开页面，只读检查不会自动选定供应商；缺失属性不按不匹配处理。</p>${items.map((x,i)=>{const d=x.detailProbe;return `<div class="agent-step"><strong>${x.detailRecommendedRank?`推荐核查 ${x.detailRecommendedRank}`:`候选 ${i+1}`} · ${esc(x.title||x.url)}</strong><br>搜索标题相似度：${esc((Number(x.lexicalScore||0)*100).toFixed(1))}%${x.detailTriageScore!==undefined?` · 详情综合分：${esc((Number(x.detailTriageScore)*100).toFixed(1))}%`:''}${x.detailAttributeCoverage!==null&&x.detailAttributeCoverage!==undefined?` · 详情属性命中率：${esc((Number(x.detailAttributeCoverage)*100).toFixed(1))}%`:''}${x.detailMatchedAttributes?.length?`<br>详情页命中属性：${x.detailMatchedAttributes.map(a=>`${esc(a.label)} ${esc(a.value)}`).join('、')}`:''}${x.detailAttributeConflicts?.length?`<br><strong>详情页明确冲突：</strong>${x.detailAttributeConflicts.map(a=>`${esc(a.label)} ${esc(a.value)} ↔ ${a.supplierAttributes.map(([k,v])=>`${esc(k)}：${esc(v)}`).join('、')}`).join('；')}`:''}<br>卡片报价：${esc(x.priceText||'未读取')} · 店铺：${esc(x.supplierText||'未读取')}${d?.supplier?`<br>详情页标题：${esc(d.supplier.title||'未读取')} · 页面图片：${esc(d.supplier.imageCount??0)}张<br>页面属性：${Object.entries(d.supplier.attributes||{}).slice(0,8).map(([k,v])=>`${esc(k)}：${esc(v)}`).join('；')||'未读取'}<br>详情读取时间：${esc(d.checkedAt||'未知')} · 页面哈希：${esc(d.pageSha256||'未生成')}`:d?`<br>详情读取状态：${esc(d.status)} · ${esc(d.limitations||'')}`:''}<br><a href="${esc(x.url)}" target="_blank" rel="noreferrer">打开并人工核验供应商详情</a></div>`}).join('')}<small>页面观察不证明 exact/strong 同款、可供库存、最终结算价、产品/包装重量或图片权利；明确规格冲突会降低推荐顺序，但最终仍需核验。</small></details>`}
function agentResearchCandidatePanel(items){if(!Array.isArray(items)||!items.length)return '';return `<details style="margin:8px 0"><summary>已选竞品研究候选（${items.length}条，ASIN与来源已留档）</summary><p class="sub">由当前账号、站点的 Amazon 前台搜索观察按标题相似度选出，仅用于关键词研究；不代表同款确认或选品通过。</p>${items.map((x,i)=>`<div class="agent-step"><strong>${i+1}. ${esc(x.title||x.asin)}</strong><br>ASIN：${esc(x.asin)} · 标题相似度：${esc((Number(x.score||0)*100).toFixed(1))}% · ${esc(x.status||'候选')}<br>采集时间：${esc(x.capturedAt||'未记录')}<br><a href="${esc(x.sourceUrl||'#')}" target="_blank" rel="noreferrer">查看前台搜索来源</a></div>`).join('')}</details>`}
async function agentVerifyCandidateThenAdopt(button,task,epoch){if(task.supplierDefaultUse){button.disabled=true;try{await flushWorkspace();await agentApi('adopt-sourcing-candidate',{id:task.id,url:button.dataset.sourceOffer});toast('已按默认规则采用货源页面资料');await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}return}const fields=[['sameProduct','确认是同一产品款式'],['exactVariant','确认颜色/尺码/规格可供'],['priceConfirmed','确认展示价格已打开详情页核对'],['weightConfirmed','确认包装重量已核对'],['imageRights','确认素材可按当前图片使用规则采用（供应商图默认可用，排除竞品图及未授权标识）'],['brandRights','确认无未授权品牌/知识产权风险']];const checks={};for(const [key,label] of fields){if(!window.confirm(`货源核验清单：${label}？\n点击“取消”将保留候选，不采用。`))return;checks[key]=true}const note=window.prompt('请填写核验备注（例如详情页规格、价格、权利证据位置）','已打开详情页逐项核对；证据待归档')||'';if(!note.trim())return toast('需要填写核验备注');button.disabled=true;try{await flushWorkspace();await agentApi('verify-sourcing-candidate',{id:task.id,url:button.dataset.sourceOffer,checks,note});const result=await agentApi('adopt-sourcing-candidate',{id:task.id,url:button.dataset.sourceOffer});toast(result.task?.status==='queued'?'核验清单已保存并采用货源；任务已重置为待开始':'核验清单已保存并采用货源');await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}}
function agentSourceReviewPanel(reviews){if(!reviews?.length)return '';return `<details style="margin:12px 0"><summary>货源核验记录（${reviews.length}条）</summary>${reviews.map((review,i)=>`<div class="agent-step"><strong>${review.status==='user_default_adopted'?'默认采用候选':'已核验候选'} ${i+1}</strong><br><a href="${esc(review.url)}" target="_blank" rel="noreferrer">${esc(review.url)}</a><br><span class="badge">${review.status==='user_default_adopted'?'按用户规则默认采用':'六项清单已确认'}</span> · ${esc(new Date(Number(review.reviewedAt||0)*1000).toLocaleString('zh-CN'))}<p>${esc(review.note)}</p></div>`).join('')}<small>核验记录用于审计；只有明确采用链接后才会写入产品货源字段。</small></details>`}
function agentSelectionRankSuggestionPanel(draft){const rows=draft?._observations?.sellerSpriteRows||[],suggestions=rows.filter(row=>(Array.isArray(row.rankSuggestions)&&row.rankSuggestions.some(value=>Number.isSafeInteger(value)&&value>0))||(Array.isArray(row.productListedDateSuggestions)&&row.productListedDateSuggestions.length)||row.alsoInAmazonSearch);if(!suggestions.length)return '';return `<details style="margin:12px 0"><summary>选品候选交叉复核队列（${suggestions.length}条，均未核验）</summary>${suggestions.map(row=>{const url=String(row.url||''),safeUrl=/^https:\/\/(?:[^/]+\.)?sellersprite\.com\//i.test(url)?url:'';const ranks=(row.rankSuggestions||[]).filter(value=>Number.isSafeInteger(value)&&value>0),dates=(row.productListedDateSuggestions||[]).filter(value=>/^\d{4}-\d{2}-\d{2}$/.test(value));return `<div class="agent-step"><strong>ASIN ${esc(row.asin||'')}</strong>${row.alsoInAmazonSearch?` · <strong>两处页面均出现，优先人工复核</strong>`:' · 单一页面观察'}${ranks.length?` · 标签排名建议 ${ranks.map(esc).join(' / ')}`:''}${dates.length?` · 商品上架日期建议（SellerSprite） ${dates.map(esc).join(' / ')}`:''}${safeUrl?` · <a href="${esc(safeUrl)}" target="_blank" rel="noreferrer">打开SellerSprite原页</a>`:''}${row.amazonSearchTitles?.length?`<p>Amazon搜索页标题：${row.amazonSearchTitles.map(esc).join('；')}</p>`:''}<p>${esc(row.raw||'')}</p></div>`}).join('')}<small>交叉出现与标签数字仅用于安排复核顺序，不证明 exact/strong 同款、销量或准入通过；不按排名大小排序。请核对来源原文和产品图片；建议不会自动写入准入证据。</small></details>`}
async function agentShowDetail(epoch){
 const tid=agentSelectedTask;if(!tid)return;
 try{
  const task=await agentApi('task?id='+encodeURIComponent(tid));if(epoch!==agentEpoch||view!=='agent'||tid!==agentSelectedTask)return;
  document.getElementById('agentDetail').innerHTML=`<h2>${esc(task.productName)} · ${esc(agentLabels[task.status]||task.status)}</h2>${agentStageBar(task)}<p>${esc(task.goal)}</p>${agentSelectionObservationPanel(task.selectionObservations,task.productName)}${agentSelectionRankSuggestionPanel(task.selectionEvidenceDraft)}${agentSourceReviewPanel(task.sourceCandidateReviews)}${agentResearchCandidatePanel(task.researchCandidateSelection)}${task.researchReportAvailable?'<button class="btn" id="agentViewResearch">查看研究报告与来源</button>':''}${task.researchPlanAvailable?'<button class="btn" id="agentViewResearchPlan" style="margin-left:8px">查看/下载竞品核验计划</button>':''}${task.sourcingPlanAvailable?'<button class="btn" id="agentViewSourcingPlan" style="margin-left:8px">查看/下载1688搜索计划</button>':''}${task.listingDraftAvailable?`<div class="actions" style="margin:12px 0"><button class="btn primary" id="agentViewDraft">查看生成文案草稿</button></div>`:''}<p>引擎：${task.aiPlanning?'AI目标规划 + 固定工具执行':'固定规则工具执行'} · ${task.paidSpendCny===null?'文本调用费用待服务商账单核实':'尚无已记录的付费调用'}</p>${task.aiUsage?`<p class="sub">文案模型用量：${esc(task.aiUsage.model||'模型未知')} · 输入 ${esc(task.aiUsage.usage.prompt_tokens??task.aiUsage.usage.input_tokens??'待服务商返回')} tokens · 输出 ${esc(task.aiUsage.usage.completion_tokens??task.aiUsage.usage.output_tokens??'待服务商返回')} tokens · 费用未从账单核实</p>`:''}<div style="margin-top:16px">${Object.entries(task.results).map(([key,result])=>`<div class="agent-step"><strong>${esc(agentStageLabel(key,result,task))} · ${result.skipped?'未执行':result.ok?'完成检查':'待补齐'}</strong>${key==='facts'&&result.selection?.skipped?`<p>沿用既有产品，本任务未执行自动选品，不计为新入选。</p>`:''}${result.summary?`<p>${esc(result.summary)}</p>`:''}${result.issues?.length?`<ul>${result.issues.map((s,i)=>`<li>${esc(s)} <button class="btn" data-agent-need="${key}-${i}">弹窗查看怎么补</button></li>`).join('')}</ul>`:''}${result.note?`<p>${esc(result.note)}</p>`:''}${agentPricingPanel(result)}${agentGeneratedGalleryStatus(result)}${result.imageRuns?.length?`<details><summary>已有图片任务（${result.activeImageRuns||0}个进行中，${result.imageRunsAwaitingAttention||0}个待核对）</summary><pre>${esc(result.imageRuns.map(r=>`任务 ${r.id||'未知'} · 槽位 ${r.slot??'未知'} · ${r.status||'未知'}${r.jobIds?.length?` · 外部任务 ${r.jobIds.join(', ')}`:r.jobId?` · 外部任务 ${r.jobId}`:''}${r.error?` · ${r.error}`:''}`).join('\n'))}</pre><small>这里只显示当前账号已保存的任务，不会重试或再次提交生图。</small></details>`:''}${result.steps?`<details><summary>任务规划</summary><pre>${esc(JSON.stringify(result.steps,null,2))}</pre></details>`:''}${result.sourcingSearchPlan?`<div class="actions" style="margin:10px 0"><a class="btn" href="${esc(result.sourcingSearchPlan.keywordUrl)}" target="_blank" rel="noreferrer">打开1688关键词搜索</a><a class="btn" href="${esc(result.sourcingSearchPlan.imageSearchUrl)}" target="_blank" rel="noreferrer">打开1688以图搜款入口</a></div><small>${esc(result.sourcingSearchPlan.warning)}</small>`:''}${agentObservedCandidatePanel(result.sourcingSearchPlan)}${task.autoResearch&&result.researchSearchPlan?`<div class="actions" style="margin:10px 0"><a class="btn" href="${esc(result.researchSearchPlan.amazonUrl)}" target="_blank" rel="noreferrer">打开Amazon前台竞品搜索</a><a class="btn" href="${esc(result.researchSearchPlan.sellerSpriteUrl)}" target="_blank" rel="noreferrer">打开SellerSprite入口</a></div><small>${esc(result.researchSearchPlan.warning)}</small><button class="btn" id="agentSetResearchAsins" style="margin:8px 0">填写已核验竞品ASIN</button>`:''}</div>`).join('')}</div><details style="margin-top:16px"><summary>完整执行记录（${task.events.length}条）</summary><pre>${esc(task.events.map(e=>new Date(e.at*1000).toLocaleString('zh-CN')+' '+e.message).join('\n'))}</pre></details>`;
  if(agentCanPrepareCopyHandoff(task)||task.executionScope==='selection_to_copy'){
   const panel=document.createElement('section');panel.className='notice';panel.style.margin='12px 0';
   const note=document.createElement('p');note.textContent=task.executionScope==='selection_to_copy'?'本任务只接续到文案。图片、导出与ERP不在本次范围内；预算仅为记录，不是费用硬上限。':'已有选品任务可在当前证据通过后接续研究和文案，保留同一任务的核验记录。';panel.appendChild(note);
   if(agentCanPrepareCopyHandoff(task)){const button=document.createElement('button');button.className='btn primary';button.textContent='接续竞品研究与文案';button.onclick=()=>agentCopyHandoffDialog(task,epoch);panel.appendChild(button)}
   if(agentCanPreviewImages(task)){const button=document.createElement('button');button.className='btn';button.textContent='查看图片准备清单（不作图）';button.onclick=()=>agentImagePreparationDialog(task,epoch);panel.appendChild(button)}
   document.getElementById('agentDetail').querySelector('h2').after(panel);
  }
  if(task.selectionEvidenceTemplate){const host=document.createElement('div');host.style.margin='10px 0';const download=(label,payload,suffix,title)=>{const button=document.createElement('button');button.className='btn';button.style.marginRight='8px';button.textContent=label;button.title=title;button.onclick=()=>{const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`selection-evidence-${payload.marketplace}-${payload.asin}${suffix}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};host.appendChild(button)};if(task.selectionEvidenceDraft)download('下载已整理观察草稿',task.selectionEvidenceDraft,'-observations','仅预填浏览器来源、日期和原文；所有判断项仍需人工核实，不能直接通过');download('下载空白选品证据模板',task.selectionEvidenceTemplate,'','模板不会自动通过核验；请填写真实来源和原文');document.getElementById('agentDetail').querySelector('h2').after(host)}
  if(task.erpWorkflowVersion===2){
   const panel=document.createElement('section');panel.className='notice';panel.style.margin='12px 0';
   const receipt=task.results.publish?.erpCollection,note=document.createElement('p');
   note.textContent=receipt?`正式ERP任务 ${receipt.id} · ${receipt.status==='verified'?'店铺草稿已核对':receipt.phase||receipt.status} · 未发布`:task.reason||'使用正式成品与账号采集目标；执行后保存原回执，不生图或发布。';
   const open=document.createElement('button');open.className='btn';open.textContent='打开正式ERP交付与原回执';open.onclick=()=>openListingWorkspace(task.productId,'delivery');
   panel.append(note,open);document.getElementById('agentDetail').querySelector('h2').after(panel);
  }
  if(task.libraryDraftOnly){
   const panel=document.createElement('section');panel.className='notice';panel.style.margin='12px 0';
   const note=document.createElement('p');
   const receipt=task.results.publish;
   note.textContent=receipt?.librarySaved
    ? `钧飞产品库记录 ${receipt.libraryId} · 已保存并回读 · 未发布`
    : task.reason||'预检通过后可将当前交付存为钧飞产品库草稿。';
   panel.append(note);
   if(task.status==='library_ready'){
    const button=document.createElement('button');button.className='btn primary';
    button.textContent='存入钧飞产品库（仅草稿）';
    button.onclick=async()=>{
     button.disabled=true;
     try{
      await flushWorkspace();
      await agentApi('save-library-draft',{id:task.id});
      await agentRefresh(epoch);
     }catch(error){toast(error.message)}finally{button.disabled=false}
    };
    panel.append(button);
   }
   document.getElementById('agentDetail').querySelector('h2').after(panel);
  }
  if(task.productId&&!task.libraryDraftOnly&&task.erpWorkflowVersion!==2){
   const erpPanel=document.createElement('section');erpPanel.className='notice';erpPanel.style.margin='12px 0';
   const button=document.createElement('button');button.className='btn';button.id='agentObserveErp';button.textContent='重新读取已保存ERP草稿（刷新ERP页）';
   const note=document.createElement('p');note.textContent='仅支持已授权绑定的账号、产品和店铺。此按钮只读取，不保存或发布，不推进生产阶段。';erpPanel.append(button,note);
   const observation=task.erpObservation;
   if(observation){const result=document.createElement('p');const a=observation.actual||{};result.textContent=`最近读取：${observation.observedAt} · 草稿 ${observation.draftId} · ${observation.store} · ERP SKU ${observation.erpSku} · 品牌 ${a.copy?.brand||'未读到'} · 制造商 ${a.copy?.manufacturer||'未读到'} · 售价 ${a.price??'未读到'} · 库存 ${a.quantity??'未读到'} · 文案差异：${observation.differences?.join('、')||'无'}。未保存、未发布。`;erpPanel.append(result);}
   button.onclick=async()=>{button.disabled=true;button.textContent='正在从ego重新读取…';try{await agentApi('observe-erp',{id:task.id});await agentShowDetail(epoch);}catch(error){toast(error.message);button.disabled=false;button.textContent='重新读取已保存ERP草稿（刷新ERP页）';}};
   document.getElementById('agentDetail').querySelector('h2').after(erpPanel);
  }
  const researchButton=document.getElementById('agentViewResearch');if(researchButton)researchButton.onclick=()=>agentResearchDialog(task);
  const researchPlanButton=document.getElementById('agentViewResearchPlan');if(researchPlanButton)researchPlanButton.onclick=()=>agentResearchPlanDialog(task);
  const sourcingPlanButton=document.getElementById('agentViewSourcingPlan');if(sourcingPlanButton)sourcingPlanButton.onclick=()=>agentSourcingPlanDialog(task);
  const asinButton=document.getElementById('agentSetResearchAsins');if(asinButton)asinButton.onclick=()=>agentSetResearchAsins(task,epoch);
  if(task.sourcingReportAvailable){const sourceButton=document.createElement('button');sourceButton.className='btn';sourceButton.textContent='查看货源读取报告';sourceButton.onclick=()=>agentSourcingDialog(task);document.getElementById('agentDetail').querySelector('h2').after(sourceButton)}
  const draftButton=document.getElementById('agentViewDraft');if(draftButton)draftButton.onclick=()=>agentDraftDialog(task);
  document.getElementById('agentDetail').querySelectorAll('[data-source-offer]').forEach(button=>button.onclick=()=>agentVerifyCandidateThenAdopt(button,task,epoch));
  document.getElementById('agentDetail').querySelectorAll('[data-agent-need]').forEach(button=>{const [key,index]=button.dataset.agentNeed.split('-');button.onclick=()=>{const issue=task.results[key]?.issues?.[Number(index)];if(issue)agentNeedsPopup(issue,task.productId)}});
 }catch(error){if(epoch===agentEpoch&&view==='agent')document.getElementById('agentDetail').textContent=error.message}
}

async function agentResearchPlanDialog(task){
 try{const data=await agentApi('research-plan?id='+encodeURIComponent(task.id));const modal=document.getElementById('modal');modal.innerHTML=`<h2>竞品浏览器核验计划</h2><p>这是持久保存的人工核验入口；不会自动认定同款、排名或调用付费服务。</p><pre style="white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(JSON.stringify(data.plan,null,2))}</pre><div class="actions"><button class="btn" id="agentResearchPlanClose">关闭</button><button class="btn" id="agentResearchPlanDownload">下载计划</button></div>`;modal.showModal();document.getElementById('agentResearchPlanClose').onclick=()=>modal.close();document.getElementById('agentResearchPlanDownload').onclick=()=>download(task.productId+'-research-plan.json',JSON.stringify(data.plan,null,2))}catch(error){toast(error.message)}
}

async function agentSourcingPlanDialog(task){
 try{const data=await agentApi('sourcing-plan?id='+encodeURIComponent(task.id));const modal=document.getElementById('modal');modal.innerHTML=`<h2>1688货源搜索计划</h2><p>这是持久保存的人工筛选入口；不会自动认定同款、价格、供货或图片权利。</p><pre style="white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(JSON.stringify(data.plan,null,2))}</pre><div class="actions"><button class="btn" id="agentSourcingPlanClose">关闭</button><button class="btn" id="agentSourcingPlanDownload">下载计划</button></div>`;modal.showModal();document.getElementById('agentSourcingPlanClose').onclick=()=>modal.close();document.getElementById('agentSourcingPlanDownload').onclick=()=>download(task.productId+'-sourcing-plan.json',JSON.stringify(data.plan,null,2))}catch(error){toast(error.message)}
}

async function agentDraftDialog(task){
 try{
  const data=await agentApi('listing-draft?id='+encodeURIComponent(task.id));
  const modal=document.getElementById('modal');
  modal.innerHTML=`<h2>自动生成的文案草稿</h2><p>请核对产品事实、关键词及警告。采用后仍为待审核，不会提交ERP。</p><pre style="white-space:pre-wrap;max-height:55vh;overflow:auto">${esc(JSON.stringify(data.copy,null,2))}</pre><div class="actions"><button class="btn" id="agentDraftClose">关闭</button><button class="btn" id="agentDraftDownload">下载草稿</button>${!task.listingDraftAdopted?'<button class="btn primary" id="agentDraftAdopt">采用到文案工作台</button>':'<span>已采用；请到文案工作台审核</span>'}</div>`;
  modal.showModal();document.getElementById('agentDraftClose').onclick=()=>modal.close();
  document.getElementById('agentDraftDownload').onclick=()=>download(task.productId+'-agent-draft.json',JSON.stringify(data.copy,null,2));
  const adopt=document.getElementById('agentDraftAdopt');if(adopt)adopt.onclick=async()=>{
   adopt.disabled=true;
   try{
    await flushWorkspace();
    const result=await agentApi('adopt-listing',{id:task.id});
    state=result.state;serverRevision=result.revision;
    modal.close();openListingWorkspace(task.productId,'copy');toast('草稿已采用，请审核；审核后返回任务台更新资料重检');
   }catch(error){toast(error.message)}finally{adopt.disabled=false}
  };
 }catch(error){toast(error.message)}
}
async function agentSourcingDialog(task){
 try{const data=await agentApi('sourcing-report?id='+encodeURIComponent(task.id));const modal=document.getElementById('modal');modal.innerHTML=`<h2>货源读取报告</h2><p>已填写链接的公开页面采集，不代表同款、结算价格或图片权利已核实；不自动提交采购或刊登。</p><pre style="white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(JSON.stringify(data.report,null,2))}</pre><div class="actions"><button class="btn" id="agentSourceClose">关闭</button><button class="btn" id="agentSourceDownload">下载报告</button></div>`;modal.showModal();document.getElementById('agentSourceClose').onclick=()=>modal.close();document.getElementById('agentSourceDownload').onclick=()=>download(task.productId+'-source.json',JSON.stringify(data.report,null,2))}catch(error){toast(error.message)}
}

async function agentSyncResearchState(tasks,epoch){
 if(!tasks.some(t=>(t.workspaceRevision||0)>serverRevision)||syncBusy||syncPending||syncConflict)return;
 const before=JSON.stringify(state);
 const response=await fetch('/api/state');if(!response.ok)return;
 const data=await response.json();
 if(epoch!==agentEpoch||view!=='agent'||syncBusy||syncPending||syncConflict||JSON.stringify(state)!==before)return;
 if(data.state&&data.revision>serverRevision){state=data.state;serverRevision=data.revision;syncLabel('智能体研究结果已同步');}
}
async function agentSetResearchAsins(task,epoch){
 const modal=document.getElementById('modal');
 const candidates=[];const add=(asin,label)=>{asin=String(asin||'').toUpperCase();if(/^[A-Z0-9]{10}$/.test(asin)&&!candidates.some(x=>x.asin===asin))candidates.push({asin,label})};
 for(const item of (task.selectionObservations||[])){if(item.captureType==='browser_sellersprite_search')for(const row of (item.sellerSprite?.rows||[]))add(row.asin,'SellerSprite观察');if(item.captureType==='browser_front_search')for(const row of (item.search?.results||[]))add(row.asin,'Amazon前台观察')}
 const observed=candidates.length?`<p>以下是扩展采集到的未核验观察，请你逐项确认同款关系和排名后再勾选：</p><div style="max-height:180px;overflow:auto">${candidates.map(x=>`<label style="display:block;margin:7px 0"><input type="checkbox" data-observed-asin="${x.asin}"> ${x.asin} · ${x.label}</label>`).join('')}</div>`:'';
 modal.innerHTML=`<h2>填写已核验竞品 ASIN</h2><p>只填写你已在当前站点 Amazon 前台和 SellerSprite 人工核验过的 ASIN，1–5个；保存后点击“更新资料重检”才会调用研究服务。</p>${observed}<input id="agentResearchAsinsInput" class="field" style="width:100%;padding:10px" placeholder="也可手动填写，例如 B0XXXXXXXX, B0YYYYYYYY"><div class="actions"><button class="btn" id="agentAsinsClose">取消</button><button class="btn primary" id="agentAsinsSave">保存</button></div>`;
 modal.showModal();
 document.getElementById('agentAsinsClose').onclick=()=>modal.close();
 document.getElementById('agentAsinsSave').onclick=async()=>{
  const manual=document.getElementById('agentResearchAsinsInput').value.trim();const selected=[...modal.querySelectorAll('[data-observed-asin]:checked')].map(x=>x.dataset.observedAsin);const value=[...new Set([...selected,...manual.split(/[\s,，;；]+/).filter(Boolean)])].join(',');
  const button=document.getElementById('agentAsinsSave');button.disabled=true;
  try{await agentApi('research-asins',{id:task.id,researchAsins:value});modal.close();toast('竞品ASIN已保存，请点击“更新资料重检”');await agentRefresh(epoch)}catch(error){toast(error.message)}finally{button.disabled=false}
 };
}
async function agentResearchDialog(task){
 try{
  const data=await agentApi('research-report?id='+encodeURIComponent(task.id));
  const modal=document.getElementById('modal');
  modal.innerHTML=`<h2>竞品研究报告与来源</h2><p>搜索量为实际返回的第三方数据，缺失不等于0。研究结果不能证明同款或本品性能。</p><pre style="white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(JSON.stringify(data.report,null,2))}</pre><div class="actions"><button class="btn" id="agentResearchClose">关闭</button><button class="btn" id="agentResearchDownload">下载报告</button></div>`;
  modal.showModal();document.getElementById('agentResearchClose').onclick=()=>modal.close();
  document.getElementById('agentResearchDownload').onclick=()=>download(task.productId+'-research.json',JSON.stringify(data.report,null,2));
 }catch(error){toast(error.message)}
}


