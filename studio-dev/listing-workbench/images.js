// Each image relay has independent credentials per employee account.
function applyFixedImageDimensions(settings,size,resolution){
 if(settings.activeProvider!=='openai'||settings.openai?.outputSize!=='1600x1600'||!size||!resolution)return;
 size.value='1:1';size.disabled=true;
 resolution.replaceChildren(new Option('1600×1600','1600',true,true));resolution.disabled=true;
}
async function mountImageBudgetPreview(){
 if(view!=='settings')return;
 const box=document.createElement('section');box.className='panel box';box.style.cssText='max-width:760px;margin-top:24px';
 box.innerHTML='<h2>生图预算与报价</h2><p id="imageBudgetLimits">读取当前账号预算…</p><div class="actions"><label>模型 <select id="imageBudgetModel" aria-label="预算报价模型"></select></label><label>比例 <select id="imageBudgetSize" aria-label="预算报价比例"><option>1:1</option><option>4:5</option><option>3:4</option><option>16:9</option></select></label><label>分辨率 <select id="imageBudgetResolution" aria-label="预算报价分辨率"><option>1K</option><option selected>2K</option><option>4K</option></select></label><button class="btn" id="previewImageBudget">查看单张报价（不生成）</button></div><p id="imageBudgetPreviewResult" role="status" style="margin-top:14px;overflow-wrap:anywhere"></p><p class="sub">报价预览不调用图片模型、不预留费用。美元换算只用于预算，最终费用按账单结算；未知任务继续占用预留。</p>';
 appendModelSettingsPanel('images',box);
 const status=box.querySelector('#imageBudgetPreviewResult'),button=box.querySelector('#previewImageBudget');
 button.disabled=true;
 try{
  const [settings,budget]=await Promise.all([imageApi('settings'),imageApi('budget')]);if(!box.isConnected)return;
  box.querySelector('#imageBudgetModel').replaceChildren(...settings.models.map(id=>new Option(id,id)));
  applyFixedImageDimensions(settings,box.querySelector('#imageBudgetSize'),box.querySelector('#imageBudgetResolution'));
  const l=budget.limits;box.querySelector('#imageBudgetLimits').textContent=`当前服务：${settings.provider} · 生图${l.enabled?'已启用':'关闭'} · 每款 ¥${(l.perProductFen/100).toFixed(2)} · 每日 ¥${(l.dailyFen/100).toFixed(2)} · 未结算预留 ${budget.reservations.filter(r=>r.state==='reserved').length} 笔`;
  const overrides=Object.entries(l.perProductOverridesFen||{});if(overrides.length)box.querySelector('#imageBudgetLimits').textContent+=' · 单款授权：'+overrides.map(([id,amount])=>`${state.products.find(p=>p.id===id)?.sku||id} ¥${(amount/100).toFixed(2)}`).join('、');
  if(settings.activeProvider==='openai'&&settings.openai?.baseUrl==='http://127.0.0.1:8787/v1'&&budget.subscriptionPolicyEnabled)box.querySelector('#imageBudgetLimits').textContent=`当前服务：Codex Tools · ChatGPT订阅额度 · 已记录 ${budget.subscriptionRequests.length} 次调用。订阅调用单独记账，不显示为人民币零费用；其他付费接口仍执行原人民币预算。`;
  button.disabled=!settings.models.length;
 }catch(error){status.textContent=error.message;return;}
 button.onclick=async()=>{
  button.disabled=true;status.textContent='读取当前模型报价…';
  try{
   const result=await imageApi('budget-quote',{model:box.querySelector('#imageBudgetModel').value,size:box.querySelector('#imageBudgetSize').value,resolution:box.querySelector('#imageBudgetResolution').value});
   if(!box.isConnected)return;const q=result.quote;
   if(q.reservationBasis==='local_compute'){status.textContent=q.note;return;}
   status.textContent=q.reservationBasis==='usd_fx_budget_estimate'?`美元单张报价 $${q.maximumUsd} · 预算汇率 ${q.fx.rate} · 加 ${q.bufferPercent}% 缓冲后预留 ¥${(q.maximumFen/100).toFixed(2)}/张，七张 ¥${(q.maximumFen*7/100).toFixed(2)}。${q.note}`:`单张人民币报价上限 ¥${(q.maximumFen/100).toFixed(2)}，七张 ¥${(q.maximumFen*7/100).toFixed(2)}。实际账单尚未结算。`;
   if(q.reservationBasis==='usd_fx_budget_estimate'&&!q.conversionPolicyEnabled)status.textContent+=' 此换算规则尚未启用，仅供预览。';
   if(q.reservationBasis==='chatgpt_subscription_quota')status.textContent=q.note;
   status.textContent+=' 本次未提交生成。';
  }catch(error){if(box.isConnected)status.textContent=error.message;}finally{if(box.isConnected)button.disabled=false;}
 };
}
let imageBatchBusy=false;
const imageSettingsRenderer=renderSettings;
renderSettings=async function(){await imageSettingsRenderer();if(view!=='settings')return;const box=document.createElement('section');box.className='panel box';box.style.cssText='max-width:760px;margin-top:24px';box.innerHTML=`<div class="wb-title-label"><h2>图片模型中转站</h2>${workbenchHelp('图片服务密钥按账号加密保存。当前生图服务见下方状态。','图片模型中转站')}</div><details class="guide-section panel"><summary>AIGoCode 配置与接口说明</summary><p>注册地址：<a href="https://www.aigocode.net" target="_blank" rel="noopener noreferrer">AIGoCode ↗</a> · <a href="https://www.aigocode.net/docs/api/openai-compatible" target="_blank" rel="noopener noreferrer">图片接口文档 ↗</a></p><ol style="padding-left:22px;line-height:1.9"><li>注册账号并在平台创建支持 OpenAI / Codex 分组的 API Key。</li><li>将 API Key 填入下方 AIGoCode 密钥并保存，再点测试获取账号可用模型。</li><li>模型名称按服务商实际提供的 ID 填写；认证查询不会生成图片。</li></ol><p class="sub">此适配使用 Images edits 上传产品参考图；实际可用性、价格和额度以所填接口为准。生成前需要有效报价，接口报错或超时不会自动改用其他服务。</p></details><label class="field"><span>OpenAI 兼容 Base URL（例如 https://api.aigocode.dev/v1）</span><input id="aigocodeBase" type="url" value="https://api.aigocode.dev/v1" autocomplete="url" style="width:100%"></label><label class="field"><span>图片模型名称</span><input id="aigocodeModel" type="text" value="gpt-image-2.5-flare" placeholder="填写中转站分组实际提供的 model ID" style="width:100%"></label><label class="field"><span>AIGoCode API Key</span><input id="aigocodeKey" type="password" autocomplete="off" placeholder="填写 AIGoCode Key；留空保留已保存密钥" style="width:100%"></label><div class="actions"><button class="btn primary" id="saveAigocodeKey">保存 AIGoCode 密钥</button><button class="btn" id="clearAigocodeKey">清除 AIGoCode 密钥</button><button class="btn" id="testAigocodeKey">测试并获取模型</button><label><input id="aigocodeActive" type="checkbox"> 设为当前生图服务</label></div><select id="aigocodeModels" aria-label="接口返回的模型列表" style="width:100%;margin-top:10px"><option>测试连接后选择模型，或手动填写 model ID</option></select><p id="aigocodeKeyStatus" role="status" style="margin-top:12px"></p><hr style="margin:22px 0;border-color:var(--line)"><div class="wb-title-label"><h2>APIMart 图片服务</h2>${workbenchHelp('使用“切换回 APIMart”切换服务。保存密钥后继续沿用当前服务。','APIMart 图片服务')}</div><div class="field"><span>APIMart 图片密钥</span><input id="imageKey" type="password" autocomplete="off" placeholder="输入图片服务密钥，留空保留现有密钥" style="width:100%"></div><div class="actions"><button class="btn primary" id="saveImageKey">保存图片密钥</button><button class="btn" id="clearImageKey">清除图片密钥</button><button class="btn" id="useApimart">切换回 APIMart</button></div><p id="imageKeyStatus" role="status" style="margin-top:12px"></p>`;appendModelSettingsPanel('images',box);async function save(clear){try{const r=await fetch('/api/image/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:document.getElementById('imageKey').value,clearKey:clear})});const d=await r.json();if(!r.ok)throw Error(d.error);if(clear)document.getElementById('imageKey').value='';document.getElementById('imageKeyStatus').textContent=d.hasKey?'图片密钥已配置；保存不产生生成调用':'尚未配置图片密钥'}catch(e){toast(e.message)}}document.getElementById('saveImageKey').onclick=()=>save(false);document.getElementById('clearImageKey').onclick=()=>save(true);document.getElementById('useApimart').onclick=async()=>{const r=await fetch('/api/image/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({activeProvider:'apimart'})});const d=await r.json();document.getElementById('imageKeyStatus').textContent=r.ok?'当前生图服务：APIMart':'切换失败：'+(d.error||'请求失败')};async function saveAi(clear,strict=false){try{const key=document.getElementById('aigocodeKey').value;const r=await fetch('/api/image/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:'aigocode',api_key:key,base_url:document.getElementById('aigocodeBase').value,model:document.getElementById('aigocodeModel').value,activate:document.getElementById('aigocodeActive').checked,clearKey:clear})});const d=await r.json();if(!r.ok)throw Error(d.error);if(clear)document.getElementById('aigocodeKey').value='';document.getElementById('aigocodeKeyStatus').textContent=d.aigocode.hasKey?'AIGoCode 密钥已配置；保存不会产生图片调用':'尚未配置 AIGoCode 密钥'}catch(e){document.getElementById('aigocodeKeyStatus').textContent=e.message;if(strict)throw e}}document.getElementById('saveAigocodeKey').onclick=()=>saveAi(false);document.getElementById('clearAigocodeKey').onclick=()=>saveAi(true);document.getElementById('testAigocodeKey').onclick=async()=>{const b=document.getElementById('testAigocodeKey'),s=document.getElementById('aigocodeKeyStatus');b.disabled=true;s.textContent='正在查询模型（不生成图片）…';try{await saveAi(false,true);const r=await fetch('/api/image/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:'aigocode',api_key:document.getElementById('aigocodeKey').value})});const d=await r.json();if(!r.ok)throw Error(d.error);s.innerHTML='<strong style="color:var(--green)">✓ '+esc(d.message)+'</strong><br>账号返回模型：'+d.models.map(esc).join('、');const models=document.getElementById('aigocodeModels');models.replaceChildren(...d.models.map(id=>new Option(id,id)));if(d.models.includes(document.getElementById('aigocodeModel').value))models.value=document.getElementById('aigocodeModel').value;models.onchange=()=>document.getElementById('aigocodeModel').value=models.value;await fetch('/api/image/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:'aigocode',api_key:document.getElementById('aigocodeKey').value,base_url:document.getElementById('aigocodeBase').value,model:document.getElementById('aigocodeModel').value,models:d.models,activate:document.getElementById('aigocodeActive').checked})});}catch(e){s.textContent='测试失败：'+e.message}finally{b.disabled=false}};try{const d=await(await fetch('/api/image/settings')).json();if(box.isConnected){document.getElementById('imageKeyStatus').textContent=d.hasKey?'APIMart 图片密钥已配置':'尚未配置 APIMart 图片密钥';document.getElementById('aigocodeKeyStatus').textContent=d.aigocode?.hasKey?'AIGoCode 密钥已配置':'尚未配置 AIGoCode 密钥';document.getElementById('aigocodeBase').value=d.aigocode?.baseUrl||'https://api.aigocode.dev/v1';document.getElementById('aigocodeModel').value=d.aigocode?.model||'gpt-image-2.5-flare';document.getElementById('aigocodeActive').checked=d.activeProvider==='aigocode';document.getElementById('aigocodeModels').replaceChildren(...(d.aigocode?.models||[]).map(id=>new Option(id,id)))}}catch{}};
const settingsWithoutBudgetPreview=renderSettings;
renderSettings=async function(){await settingsWithoutBudgetPreview();await mountImageBudgetPreview();};
let imagePollTimer,imageJobsBusy=false;
const imagesEnhance=enhance;
enhance=function(){imagesEnhance();clearTimeout(imagePollTimer);if(view==='images')mountImageGeneration()};
async function imageApi(path,data){const r=await fetch('/api/image/'+path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const d=await r.json();if(!r.ok)throw Error(d.error||'图片请求失败');return d}
function mountImageGeneration(){const p=product(),referenceAssets=[...new Map([...(p.assets||[]),...collectedProductImages(p).map(url=>({url,name:'采集产品图',source:url.includes('alicdn.com')?'1688 采集图':'ASIN 采集图'}))].map(a=>[a.url,a])).values()];document.querySelector('.notice').textContent='选择产品真实素材作为参考，按图片方案生成。参考图和图片要求将发送至当前选定图片服务。生成后请检查颜色、版型、结构与配件。下方原有插图是方案占位图。';const section=document.createElement('section');section.className='panel box';section.style.marginBottom='24px';section.innerHTML=`<h2>调用接口生成产品图</h2><div class="actions"><label>图片模型<br><select id="imageModel"><option value="gemini-3-pro-image-preview">Nano Banana Pro · 标准通道</option><option value="gemini-3-pro-image-preview-official">Nano Banana Pro · 官方通道</option></select></label><label>比例<br><select id="imageSize"><option>1:1</option><option>4:5</option><option>3:4</option><option>16:9</option></select></label><label>分辨率<br><select id="imageResolution"><option>1K</option><option>2K</option><option>4K</option></select></label><label>图片用途<br><select id="imageSlot">${['白底主图','生活场景图','核心卖点图','尺寸说明图','材质细节图','配件与包装图','颜色展示图'].map((n,i)=>`<option value="${i}">${n}</option>`).join('')}</select></label></div><div class="field" id="imageReferencePicker"><span>选择1张主体图，可再勾选最多3张同款同色细节图（总计1–4张，最多30 MB）</span><div style="display:flex;gap:14px;flex-wrap:wrap">${referenceAssets.map((a,i)=>`<label style="width:125px"><img src="${esc(a.url)}" referrerpolicy="no-referrer" loading="lazy" alt="${esc(a.name)}" style="width:120px;height:90px;object-fit:contain"><br><input type="checkbox" name="imageRef" value="${i}"> ${esc(a.color||a.source||'产品素材')}</label>`).join('')||'<p>暂无采集或上传的图片，请先采集产品或上传实拍图。</p>'}</div></div><label class="field"><span>图片要求（可编辑）</span><textarea id="imagePrompt" style="width:100%;min-height:110px"></textarea></label><button class="btn primary" id="submitImage" ${serverReady?'':'disabled'}>生成 1 张图片</button><p id="imageSubmitStatus" role="status" style="margin-top:12px"></p><p class="sub" style="margin-top:12px">每次提交生成1张，按当前服务规则消耗费用或订阅额度。任务提交后关闭页面仍可回到对应产品的图片工作台查看进度和结果；结果链接可能约 24 小时过期，请及时另存图片。</p>`;document.querySelector('.gallery').before(section);function updatePrompt(){const i=document.getElementById('imageSlot').value;document.getElementById('imagePrompt').value=`产品：${p.name}。请严格保留参考图中的产品外观、颜色、花纹、结构和配件数量，不添加不存在的部件。\n图片用途：${document.getElementById('imageSlot').selectedOptions[0].textContent}。\n${state.copy[p.id+'-image-'+i]||'画面清晰，产品主体突出。不要生成未经提供的尺寸、认证或功能文字。'}`}updatePrompt();document.getElementById('imageSlot').onchange=updatePrompt;document.getElementById('submitImage').onclick=async e=>{if(imageBatchBusy)return toast('批量提交中，请稍候');const button=e.target,refs=selectedImageReferences();if(refs.length<1||refs.length>4)return toast('请先设定主体图，再勾选1–4张同款同色参考图');if(!confirm('将选中的产品参考图和图片要求发送至当前选定的图片服务，生成1张图片，并消耗对应费用或订阅额度。是否提交？'))return;button.disabled=true;const status=document.getElementById('imageSubmitStatus');status.innerHTML=generationProgressHTML('running','正在准备参考图并提交任务，请勿重复点击');try{const job=await imageApi('generate',{productId:p.id,slot:document.getElementById('imageSlot').selectedOptions[0].textContent,model:document.getElementById('imageModel').value,size:document.getElementById('imageSize').value,resolution:document.getElementById('imageResolution').value,prompt:document.getElementById('imagePrompt').value,references:refs});p.status='制作中';state.reviews[p.id]={copy:false,images:false,note:''};persist();status.dataset.job=job.id;status.innerHTML=generationProgressHTML('running','任务已提交，正在生成 · '+job.id);await refreshImageJobs()}catch(e){status.innerHTML=generationProgressHTML('failed','提交失败：'+e.message)}finally{button.disabled=false}};mountImageJobs(true)}
function mountImageJobs(inProduct){const box=document.createElement('section');box.id='imageJobs';box.className='panel box';box.style.marginTop='24px';box.innerHTML='<h2>图片生成记录</h2><p>正在读取任务…</p>';document.getElementById('content').append(box);refreshImageJobs()}
async function refreshImageJobs(){if(imageJobsBusy)return;const box=document.getElementById('imageJobs');if(!box)return;imageJobsBusy=true;try{let data=await imageApi('jobs');const filtered=data.jobs.filter(j=>j.productId===selected);let pending=false;for(let i=0;i<filtered.length;i++){let job=filtered[i];if(!['completed','failed','cancelled','submission_unknown','submitting'].includes(job.status)){pending=true;try{filtered[i]=await imageApi('poll',{id:job.id})}catch(e){job.queryError=e.message}}}if(!box.isConnected)return;if(view==='images')showGeneratedImageCards(filtered);const submitStatus=document.getElementById('imageSubmitStatus'),submittedJob=filtered.find(j=>j.id===submitStatus?.dataset.job);if(submittedJob&&['completed','failed','cancelled'].includes(submittedJob.status))submitStatus.innerHTML=generationProgressHTML(submittedJob.status==='completed'?'completed':'failed',submittedJob.status==='completed'?'图片生成完成，请在下方查看结果':'图片任务未完成：'+(submittedJob.error||submittedJob.status));const labels={submission_unknown:'提交待核对',submitting:'提交待核对',submitted:'已提交',pending:'排队中',processing:'生成中',completed:'已完成',failed:'失败',cancelled:'已取消'};box.innerHTML='<h2>图片生成记录</h2>'+filtered.map(j=>`<div style="border-top:1px solid var(--line);padding:18px 0"><strong>${esc(j.productId)} · ${esc(j.slot)}</strong> <span class="pill">${labels[j.status]||esc(j.status)}</span>${generationProgressHTML(['cancelled','submission_unknown','submitting'].includes(j.status)?'failed':j.status,['submission_unknown','submitting'].includes(j.status)?'提交结果待核对，请先检查原请求，勿重新生成':['completed','failed','cancelled'].includes(j.status)?(j.status==='completed'?'图片生成完成':j.status==='cancelled'?'任务已取消':'图片生成失败'):(labels[j.status]||'生成中')+(typeof j.progress==='number'?' · '+Math.max(0,Math.min(99,j.progress))+'%':' · 等待服务商更新进度'),j.progress)}<p class="sub">${esc(j.id)} · ${esc(j.model)}${typeof j.progress==='number'?' · '+j.progress+'%':''}</p>${j.proxyDiagnostic?.message?`<p class="proxy-image-error" style="color:#ad6452">${esc(j.proxyDiagnostic.provider)} HTTP ${esc(String(j.proxyDiagnostic.httpStatus))}：${esc(j.proxyDiagnostic.message)}<br>错误记录：${esc(j.proxyDiagnostic.id)}</p>`:''}${j.error||j.queryError?`<p style="color:#ad6452">${esc(j.error||j.queryError)}</p>`:''}<div style="display:flex;gap:15px;flex-wrap:wrap">${(j.images||[]).map(url=>`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer"><img src="${esc(url)}" alt="${esc(j.slot)}生成结果" referrerpolicy="no-referrer" style="width:220px;height:220px;object-fit:contain;background:#f5f7f1"><p class="sub">打开原图并另存 · 请及时保存</p></a>`).join('')}</div></div>`).join('')+(filtered.length?'':'<p style="margin-top:15px">暂无图片任务。到图片工作台选择参考图后生成。</p>')+'<button class="btn" id="refreshImages">刷新任务</button>';document.getElementById('refreshImages').onclick=refreshImageJobs;if(pending)imagePollTimer=setTimeout(refreshImageJobs,10000)}catch(e){if(box.isConnected)box.innerHTML='<h2>图片生成记录</h2><p>'+esc(e.message)+'</p>'}finally{imageJobsBusy=false}}

const settingsWithImageKey=renderSettings;
renderSettings=async function(){await settingsWithImageKey();if(view!=='settings'||!document.getElementById('saveImageKey'))return;const button=document.createElement('button');button.className='btn';button.textContent='测试图片密钥并获取模型';document.getElementById('saveImageKey').after(button);const result=document.createElement('div');result.style.cssText='margin-top:15px;line-height:1.9;overflow-wrap:anywhere';result.setAttribute('role','status');document.getElementById('imageKeyStatus').after(result);const input=document.getElementById('imageKey');input.addEventListener('input',()=>result.textContent='');button.onclick=async()=>{const key=input.value;button.disabled=true;button.textContent='正在测试…';result.style.color='var(--muted)';result.textContent='正在查询 APIMart 图片模型…';try{const data=await imageApi('test',{api_key:key});if(!button.isConnected)return;if(input.value!==key){result.textContent='密钥已修改，请重新测试';return}result.replaceChildren();const note=document.createElement('p');note.style.cssText='color:var(--green);font-weight:600';note.textContent='✓ 测试成功 · '+data.message+' 新输入密钥需要点击保存后用于生成。';result.append(note);const label=document.createElement('p');label.className='sub';label.textContent='接口返回 '+data.models.length+' 个图片模型；当前生成适配支持其中 '+data.supportedModels.length+' 个。';result.append(label);const select=document.createElement('select');select.setAttribute('aria-label','密钥可用图片模型');select.style.cssText='width:100%;margin-top:10px';for(const id of data.models){select.add(new Option(id+(data.supportedModels.includes(id)?' · 已适配':' · 尚未适配生成'),id))}if(!data.models.length)select.add(new Option('未返回图片模型，请检查密钥的模型范围',''));result.append(select);const text=document.createElement('p');text.className='sub';text.textContent='图片工作台使用当前选定服务返回的模型，避免不同接口参数不兼容。';result.append(text)}catch(e){result.style.color='#ad6452';result.textContent='✕ 测试失败 · '+e.message}finally{button.disabled=false;button.textContent='测试图片密钥并获取模型'}}};
const imageGenerationOriginal=mountImageGeneration;
mountImageGeneration=function(){imageGenerationOriginal();const select=document.getElementById('imageModel');if(!select)return;imageApi('settings').then(data=>{if(!select.isConnected)return;applyFixedImageDimensions(data,document.getElementById('imageSize'),document.getElementById('imageResolution'));const previous=select.value;select.replaceChildren(...data.models.map(id=>new Option(id,id)));if(data.models.includes(previous))select.value=previous;if(!data.models.length){select.add(new Option('无已适配的可用模型',''));document.getElementById('submitImage').disabled=true}}).catch(e=>toast(e.message))};

// Reveal is local to the current page; never request saved credentials from the server.
const settingsBeforeReveal=renderSettings;
renderSettings=async function(){await settingsBeforeReveal();if(view!=='settings')return;for(const id of ['apiKey','imageKey']){const input=document.getElementById(id);if(!input||input.parentElement.dataset.secretWrap)continue;const wrap=document.createElement('div');wrap.dataset.secretWrap='true';wrap.style.cssText='display:flex;align-items:center;gap:8px;width:100%';input.before(wrap);wrap.append(input);input.style.flex='1';input.style.minWidth='0';const eye=document.createElement('button');eye.type='button';eye.className='btn';eye.style.cssText='flex:0 0 42px;padding:9px;display:flex;justify-content:center;align-items:center';eye.setAttribute('aria-label','显示密钥');eye.setAttribute('aria-pressed','false');eye.title='显示密钥';eye.innerHTML='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';wrap.append(eye);eye.onclick=()=>{const show=input.type==='password';input.type=show?'text':'password';eye.setAttribute('aria-pressed',String(show));eye.setAttribute('aria-label',show?'隐藏密钥':'显示密钥');eye.title=show?'隐藏密钥':'显示密钥';eye.style.background=show?'#eaf1df':'white'};}}

// Product-specific plans are saved locally; generation remains one image per submission.
function buildProductImagePlans(p){
 const facts=['name','cat','material','style','dimensions','packageContents','features'].filter(k=>p[k]).map(k=>k+': '+p[k]).join('\n');
 const copy=state.copy[p.id+'-'+(p.sourceImport?.market||p.market||'US')]||{};
 const common='PRODUCT IDENTITY LOCK: Use the first reference image as the single source of truth for this exact sellable product. Additional references, if any, are details of the SAME product only; never average, combine, recolor, or borrow design elements across references. Preserve exact silhouette, proportions, surface finish, labels, construction, openings, components and quantity. For apparel also preserve panel seams, hood, zipper, pockets, cuffs and fabric pile; do not invent features absent from the reference. Use only confirmed product facts. Do not invent logos, claims, accessories, or construction. No text, numbers, graphics, badges, watermark, collage, flames or decorative effects. Photorealistic commercial product photography, accurate natural fabric texture, physically plausible folds, clean controlled lighting, sharp focus, intentional composition, no AI artifacts.\nCONFIRMED PRODUCT FACTS: '+facts+'\nExisting listing wording is context only, not evidence: '+String(copy.bullets||'').slice(0,900)+'\n';
 return [
  '白底主图 / AMAZON MAIN IMAGE: Render the SAME exact item as the primary reference, in the explicitly selected color. Product only on solid pure white #FFFFFF (RGB 255,255,255). Centered full product, fills about 85% of the square frame, fully in frame, clean professional soft studio shadow. Use a natural, recognizable product presentation matching the source image; do not redesign, open, close, turn inside out, or change the product. No model, props, text, logo overlay, badge, extra item, border, or graphic. This must look like a faithful product photograph, not concept art.' ,
  '生活场景图 / LIFESTYLE: Place this exact product in one believable, understated real-world setting appropriate to its intended use. Maintain exact selected color and product construction from the primary reference; do not obscure signature details. Natural light, realistic human scale and contact shadows, editorial commercial photography. No fictional performance demonstration, text, badges, extra product parts, or dramatic effects.' ,
  '核心卖点 / FEATURE DETAIL: Create one close, premium photographic view that truthfully shows a single visible confirmed feature from the reference. Product geometry and material remain exact. Simple uncluttered neutral background, clear focal hierarchy. Leave generous empty space on the right for later typesetting; render absolutely no text, icons, arrows, labels, or comparison claims.' ,
  '尺寸说明底图 / SIZE-CHART BACKGROUND: Show the exact full product in a clean front-facing view on a quiet light neutral background. Reserve clear negative space around it for a human to add verified measurements later. Never create measurement arrows, lines, numbers, labels, or infer garment dimensions.' ,
  '材质细节 / MATERIAL MACRO: A sharp macro photograph of the actual visible material surface in the primary reference. Preserve the correct material texture, grain, weave (if present), color and scale. Soft directional light reveals real texture; no fabricated layers, inserts, stitching or claims.' ,
  'PACKAGE / ALTERNATE VIEW: Do not invent any accessories or packaging. When package contents are unconfirmed, show only the exact product from a useful alternate photographic angle on a clean background. Preserve all real details.' ,
  'SELECTED COLOR: Show only the explicitly selected color variant as a single isolated product. Match its color to the primary reference under neutral white-balanced lighting. Never create a multi-color collage, additional variants, swatches, or text.'
 ].map(instruction=>common+instruction);
}
const beforeImagePlans=mountImageGeneration;
mountImageGeneration=function(){beforeImagePlans();const p=product(),slot=document.getElementById('imageSlot'),prompt=document.getElementById('imagePrompt');if(!slot||!prompt)return;
 const section=document.createElement('section');section.className='panel box';section.style.marginBottom='20px';
 section.innerHTML='<h2>整套产品图方案</h2><p style="margin:12px 0">①选择采集图或上传实拍图，并填写对应颜色 → ②勾选1–4张同一颜色参考图 → ③制定方案 → ④逐张生成。方案制定不调用模型、不产生费用；尺寸图仅生成底图，文字与尺寸尚需另行排版。</p><div class="actions"><label>本次颜色 / 规格 <input id="planColor" placeholder="如黑色 / M码"></label><button class="btn primary" id="createImagePlans">一键制定整套方案</button><button class="btn" id="saveCurrentImagePlan">保存当前图片要求</button></div><p id="imagePlanStatus" role="status" style="margin-top:12px"></p>';
 slot.closest('section').before(section);
 const color=section.querySelector('#planColor'),status=section.querySelector('#imagePlanStatus');color.value=p.imagePlanColor||'';
 const originalChange=slot.onchange;const load=()=>{originalChange?.();const saved=state.copy[p.id+'-image-'+slot.value];prompt.value=saved||buildProductImagePlans(p)[Number(slot.value)];prompt.value=prompt.value.replace(/\n本次颜色 \/ 规格：[^\n]*/g,'');if(color.value.trim())prompt.value+='\n本次颜色 / 规格：'+color.value.trim()+'。只使用匹配该颜色的参考图，不混用颜色。'};slot.onchange=load;load();
 color.onchange=()=>{p.imagePlanColor=color.value.trim();persist();load()};
 section.querySelector('#createImagePlans').onclick=()=>{const existing=Array.from({length:7},(_,i)=>state.copy[p.id+'-image-'+i]).some(Boolean);if(existing&&!confirm('重新制定整套方案会替换该产品现有7张图片方案，是否继续？'))return;const plans=buildProductImagePlans(p);plans.forEach((text,i)=>state.copy[p.id+'-image-'+i]=text);p.imagePlanColor=color.value.trim();persist();load();status.textContent='✓ 7张方案已保存。切换下方“图片用途”可查看和修改，再逐张生成。';};
 section.querySelector('#saveCurrentImagePlan').onclick=()=>{if(!prompt.value.trim())return toast('请填写图片要求');state.copy[p.id+'-image-'+slot.value]=prompt.value;p.imagePlanColor=color.value.trim();persist();status.textContent='✓ 当前图片要求已保存';};
};

function showGeneratedImageCards(jobs){
 const slots=['白底主图','生活场景图','核心卖点图','尺寸说明图','材质细节图','配件与包装图','颜色展示图'];
 for(const [i,slot]of slots.entries()){
  const card=document.querySelector(`[data-plan="${i}"]`)?.closest('.imagecard');if(!card)continue;
  const job=jobs.find(j=>j.productId===selected&&j.slot===slot&&j.status==='completed'&&j.images?.length);
  if(!job)continue;
  const artBox=card.querySelector('.art');if(!artBox||artBox.dataset.generatedJob===job.id)continue;
  const url=job.images.find(u=>{try{return new URL(u).protocol==='https:'}catch{return false}});if(!url)continue;
  artBox.className='art generated-product-image';artBox.style.cssText='background:#fff;position:relative;display:block;overflow:hidden';artBox.dataset.generatedJob=job.id;
  const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.style.cssText='display:block;width:100%;height:100%';
  const img=document.createElement('img');img.src=url;img.alt=slot+' · 已生成';img.referrerPolicy='no-referrer';img.style.cssText='width:100%;height:100%;object-fit:contain';
  img.onerror=()=>{badge.textContent='图片链接失效，请查看生成记录';badge.style.background='#a32928'};
  const badge=document.createElement('span');badge.className='artlabel';badge.textContent='✓ '+slot+' · 已生成';badge.style.cssText='position:absolute;bottom:10px;left:10px;background:#176b50;color:#fff;padding:5px 10px;border-radius:5px';
  link.append(img);artBox.replaceChildren(link,badge);
  let note=card.querySelector('.generated-result-note');if(!note){note=document.createElement('p');note.className='sub generated-result-note';card.querySelector('.cardbody').append(note)}note.textContent='显示最近一次成功结果 · 点击图片查看原图，请及时另存';
 }
}

function remainingImageSlots(jobs,productId){
 const names=['白底主图','生活场景图','核心卖点图','尺寸说明图','材质细节图','配件与包装图','颜色展示图'];
 return names.map((name,index)=>({name,index})).filter(slot=>!jobs.some(j=>j.productId===productId&&j.slot===slot.name&&((j.status==='completed'&&j.images?.length)||['submitted','pending','processing','submitting','submission_unknown'].includes(j.status))));
}
const mountBeforeBatchImages=mountImageGeneration;
mountImageGeneration=function(){
 mountBeforeBatchImages();
 const p=product(),single=document.getElementById('submitImage');if(!single)return;
 const batch=document.createElement('button');batch.id='batchRemainingImages';batch.className='btn primary';batch.style.marginLeft='12px';batch.textContent='批量生成剩余图片';batch.disabled=!serverReady||imageBatchBusy;single.after(batch);
 const info=document.createElement('p');info.className='sub';info.style.marginTop='12px';info.textContent='批量使用当前勾选的同色参考图与各用途方案；跳过本产品已成功或正在生成的用途。每种用途生成1张，分别计费，提交期间请保持页面打开。';batch.after(info);
 batch.onclick=async()=>{
  if(imageBatchBusy||single.disabled)return toast('已有任务正在提交，请稍候');
  const refs=selectedImageReferences();
  if(refs.length<1||refs.length>4)return toast('请先设定主体图，再勾选1–4张同款同色参考图');
  const status=document.getElementById('imageSubmitStatus');
  batch.disabled=true;single.disabled=true;imageBatchBusy=true;
  let count=0;
  try{
   const data=await imageApi('jobs'),remaining=remainingImageSlots(data.jobs,p.id);
   if(!remaining.length){toast('各用途都已生成或正在生成，无需重复提交');return}
   const defaults=buildProductImagePlans(p),color=document.getElementById('planColor')?.value.trim()||'';
   const config={productId:p.id,model:document.getElementById('imageModel').value,size:document.getElementById('imageSize').value,resolution:document.getElementById('imageResolution').value,references:refs};
   const plans=remaining.map(slot=>({...config,slot:slot.name,prompt:(state.copy[p.id+'-image-'+slot.index]||defaults[slot.index])+(color?'\n本次颜色 / 规格：'+color+'。仅保留该颜色，不创造其他颜色。':'')}));
   if(!confirm('将依次提交 '+plans.length+' 张图片：'+remaining.map(s=>s.name).join('、')+'。使用当前参考图，每张分别按 APIMart 价格计费。尺寸图仅为底图，文字需另行排版。是否开始？'))return;
   for(const plan of plans){
    status.innerHTML=generationProgressHTML('running','正在提交 '+(count+1)+' / '+plans.length+' · '+plan.slot,Math.round(count/plans.length*100));
    await imageApi('generate',plan);count++;
    if(count===1){p.status='制作中';state.reviews[p.id]={copy:false,images:false,note:''};persist()}
   }
   status.innerHTML=generationProgressHTML('running','已提交 '+count+' 张，等待模型生成；下方查看各张进度');
  }catch(e){status.innerHTML=generationProgressHTML('failed','已提交 '+count+' 张；后续提交已停止：'+e.message+'。请先核对服务商任务，勿立即重复提交。')}
  finally{imageBatchBusy=false;batch.disabled=single.disabled=false;await refreshImageJobs()}
 };
};

function selectedImageReferences(){
 const checked=[...document.querySelectorAll('input[name=imageRef]:checked')];
 const primary=document.querySelector('input[name=imagePrimary]:checked')?.value;
 if(!primary||!checked.some(c=>c.value===primary))return [];
 checked.sort((a,b)=>(a.value===primary?-1:b.value===primary?1:0));
 return checked.map(c=>c.closest('label').querySelector('img').getAttribute('src'));
}
const mountBeforeIdentity=mountImageGeneration;
mountImageGeneration=function(){
 mountBeforeIdentity();
 const p=product(),prompt=document.getElementById('imagePrompt');if(!prompt)return;
 for(const cb of document.querySelectorAll('input[name=imageRef]')){
  const radio=document.createElement('input');radio.type='radio';radio.name='imagePrimary';radio.value=cb.value;radio.setAttribute('aria-label','设为主体参考图 '+(Number(cb.value)+1));
  const caption=document.createElement('span');caption.textContent=' 设为主体图';caption.style.cssText='display:block;font-size:12px;color:#214e7d;margin-top:8px';caption.prepend(radio);cb.closest('label').append(caption);
  radio.onchange=()=>{cb.checked=true;toast('主体图已选择，其他勾选图仅作细节参考')};
 }
 const picker=document.getElementById('imageReferencePicker'),grid=document.getElementById('assetGrid');
 if(picker&&grid){
  grid.replaceChildren(picker);picker.style.width='100%';
  const library=grid.closest('section'),count=library.querySelector('h2 .sub');
  if(count)count.textContent=document.querySelectorAll('input[name=imageRef]').length+' 张 · 上传与采集素材';
  const hint=library.querySelector('p');if(hint)hint.textContent='上传图和采集图统一在这里选择。先设定主体图，再勾选同款同色细节图；下方智能套图和手动作图共用此选择。';
 }
 const brief=document.createElement('section');brief.className='panel box';brief.style.marginBottom='20px';brief.innerHTML='<h2>产品视觉身份与整套风格</h2><p>先指定一张同色、清晰的主体图，它决定产品外形。其余只选同款细节图；不要混用不同颜色。默认不生成图中文字，避免未经核对的卖点和尺寸。</p><label class="field"><span>整套创意要求（场景、受众、光线、背景色）</span><textarea id="visualBrief" style="width:100%;min-height:80px" placeholder="如：英国秋冬户外，简洁自然，浅灰和暖白背景，柔和自然光"></textarea></label><button class="btn" id="compileImageBrief">应用要求并更新7张方案</button><p class="sub" style="margin-top:12px">提交前检查：同款同色、无虚构配件；出图后人工检查版型、口袋、拉链、纹理、色差及文字。修改方案不会改变已生成图片。</p>';
 document.getElementById('planColor').closest('section').before(brief);brief.querySelector('textarea').value=p.visualBrief||'';
 brief.querySelector('button').onclick=()=>{if(!confirm('将按当前创意要求更新7张方案，替换已保存的方案，保留已有生成图。继续？'))return;p.visualBrief=brief.querySelector('textarea').value.trim();buildProductImagePlans(p).forEach((text,i)=>state.copy[p.id+'-image-'+i]=text);persist();document.getElementById('imageSlot').onchange();toast('整套详细方案已更新，请检查后生成')};
};
const plansBeforeVisualBrief=buildProductImagePlans;
buildProductImagePlans=function(p){return plansBeforeVisualBrief(p).map((text,i)=>text+'\nSHARED VISUAL DIRECTION: '+(p.visualBrief||'Understated premium retail photography; soft neutral light, natural white balance, quiet warm-white and light-grey environments, no sci-fi decoration. Keep the product itself unchanged.')+'\n'+(i===0?'Main image background MUST remain pure white, overriding creative styling.':'Distinct composition from the other slots, no repeated generic product pose. Keep an 8% edge-safe margin.'))};


const renderSettingsBeforeComfy = renderSettings;
renderSettings = async function(){
 await renderSettingsBeforeComfy();
 if(view!=='settings')return;
 const panel=document.createElement('section');
 panel.className='panel box';panel.style.cssText='max-width:760px;margin-top:24px';
 panel.innerHTML=`<h2>本地生图 · ComfyUI</h2><p>本机 SD 图生图使用一张主体图。云端和本地通过当前服务设置切换；使用原图片分镜、套图与图位版本。</p>
 <label class="field">本机 ComfyUI 地址<input id="comfyBase" value="http://127.0.0.1:8188"></label>
 <label class="field">SD Checkpoint 文件名<input id="comfyModel" placeholder="例如 model.safetensors"></label>
 <label class="field">重绘强度<input id="comfyDenoise" type="number" min="0.10" max="0.75" step="0.01" value="0.35"></label>
 <div class="actions"><button class="btn" id="comfyProbe">检查本机模型和设备</button><button class="btn primary" id="comfyActivate">保存并切换本地生图</button></div>
 <p id="comfyStatus" role="status"></p><p class="sub">本地输出会保存技术质检，商品外观、颜色与文字由人工复核。切回云端请使用上方相应服务按钮。</p>`;
 appendModelSettingsPanel('images',panel);
 const find=id=>panel.querySelector('#'+id);
 const status=find('comfyStatus'),test=find('comfyProbe'),activate=find('comfyActivate');
 async function send(path,data,button){
  button.disabled=true;
  try{return await imageApi(path,{provider:'comfyui',baseUrl:find('comfyBase').value.trim(),...data})}
  catch(error){if(panel.isConnected)status.textContent=error.message;return null}
  finally{button.disabled=false}
 }
 test.onclick=async()=>{
  const result=await send('test',{},test);if(!result||!panel.isConnected)return;
  const gpu=result.devices.map(d=>d.name+(Number.isFinite(d.vram_total)?' · 总显存 '+Math.round(d.vram_total/1048576)+' MiB':''));
  status.textContent=gpu.concat(result.models.length?'可用 Checkpoint '+result.models.join('、'):'未发现 safetensors Checkpoint').join(' ｜ ');
  if(!find('comfyModel').value&&result.models.length===1)find('comfyModel').value=result.models[0];
 };
 activate.onclick=async()=>{
  const result=await send('settings',{checkpoint:find('comfyModel').value.trim(),denoise:Number(find('comfyDenoise').value),activate:true},activate);
  if(result&&panel.isConnected){status.textContent='当前生图服务 · '+result.provider;render();}
 };
 try{
  const settings=await imageApi('settings');if(!panel.isConnected)return;
  find('comfyBase').value=settings.comfyui.baseUrl;find('comfyModel').value=settings.comfyui.checkpoint;
  find('comfyDenoise').value=settings.comfyui.denoise;status.textContent='当前生图服务 · '+settings.provider;
 }catch(error){if(panel.isConnected)status.textContent=error.message}
};
