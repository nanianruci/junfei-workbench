/* Name-only task setup for Codex-led rotation; never starts a publisher. */
(() => {
 const empty=()=>({ziniaoStoreName:'',erpAccount:'',usStoreName:'',ukStoreName:''});
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const sameAccount=id=>typeof signedUser!=='undefined'&&signedUser?.id===id;
 async function request(owner,data){
  const response=await fetch('/api/agent/rotation-setup',{headers:{'X-Workspace-Account':String(owner),'Content-Type':'application/json'},...(data===undefined?{}:{method:'POST',body:JSON.stringify(data)})});
  const result=await response.json();if(!response.ok)throw Object.assign(Error(result.error||'任务配置读取失败'),{status:response.status});return result;
 }
 window.refreshStoreRotationSetup=async function(current=()=>true){
  const owner=signedUser?.id,node=document.getElementById('agentRotationSetupSummary');if(!owner||!node)return;
  try{const result=await request(owner);if(!sameAccount(owner)||!current()||!node.isConnected)return;
   node.textContent=result.setup?`已保存 ${result.setup.targets.length} 家店 · 每店3款新商品 · 版本 ${result.revision} · 名称待核对，待Codex接续`:'尚未配置。填写店铺顺序后保存，交给Codex按总工作流接续。';
  }catch(error){if(sameAccount(owner)&&current()&&node.isConnected)node.textContent='配置读取失败：'+error.message;}
 };
 window.openStoreRotationRun=async function(current=()=>true){
  const owner=signedUser?.id,modal=document.getElementById('modal');
  if(!owner)return toast('请先登录当前工作空间');
  const headers={'X-Workspace-Account':String(owner),'Content-Type':'application/json'};
  const alive=()=>sameAccount(owner)&&current()&&modal.open;
  const url='/api/agent/rotation';
  async function read(path,body){
   const response=await fetch(path,{headers,...(body?{method:'POST',body:JSON.stringify(body)}:{})});
   const result=await response.json();
   if(!response.ok)throw Error(result.error||'无法读取轮转检查点');
   return result;
  }
  let busy=false;
  async function render(run,setup,history=[]){
   if(!alive())return;
   const target=run?.targets[run.index],product=run?.current;
   const jobs=product?(await read('/api/publication/list?productId='+encodeURIComponent(product.productId))).jobs
    .filter(job=>job.productId===product.productId&&job.destinationId===target.destinationId):[];
   if(!alive())return;
   modal.innerHTML='<h2>店铺轮转检查点</h2><p>每家店每轮三款。这里只分配商品和核对原任务，发布须在商品交付页单独确认。</p>';
   const body=document.createElement('div'),errorNode=document.createElement('p');
   errorNode.setAttribute('role','alert');
   modal.append(body,errorNode);
   function button(label,action){
    const item=document.createElement('button');
    item.type='button';item.className='btn';item.textContent=label;
    item.onclick=async()=>{
     if(busy||!alive())return;
     busy=true;item.disabled=true;errorNode.textContent='';
     try{await action();}
     catch(error){if(alive())errorNode.textContent=error.message;}
     finally{busy=false;if(item.isConnected)item.disabled=false;}
    };
    body.append(item);
   }
   const submit=async(action,data)=>{await read(url,{action,...data});const updated=await read(url);await render(updated.run,setup,updated.history);};
   if(!run||run.archivedAt){
    if(run?.archivedAt){const archived=document.createElement('p');archived.textContent='原轮转已归档 · 第 '+run.round+' 轮 · 已完成 '+run.count+' 款 · '+new Date(run.archivedAt*1000).toLocaleString();body.append(archived);}
    if(setup)button('开始轮转记录',()=>submit('start',{setupId:setup.id,...(run?{afterRunId:run.id}:{})}));
    else body.append('请先保存有序店铺配置。');
   }else{
    const summary=document.createElement('p');
    summary.textContent='第 '+run.round+' 轮 · '+target.store+(target.ukStore?' / '+target.ukStore:'')+' · '+run.count+'/3 款'+(run.paused?' · 已暂停':'');
    body.append(summary);
    button(run.paused?'恢复轮转':'暂停轮转',()=>submit(run.paused?'resume':'pause',{runId:run.id}));
    if(product){
     const name=document.createElement('p');name.textContent='当前商品 '+(product.name||product.productId);body.append(name);
     button('打开原商品交付页',()=>{modal.close();openListingWorkspace(product.productId,'delivery');});
     if(jobs.length===1)button('核对原发布回执并计数',()=>submit('reconcile',{runId:run.id,publicationId:jobs[0].id}));
     else if(jobs.length)body.append('发现原发布任务，须先核对其状态。');
     else {
      body.append('尚未关联原正式发布任务。');
      const reason=document.createElement('textarea');reason.maxLength=500;reason.placeholder='仅用于尚未创建任何发布任务的商品';reason.setAttribute('aria-label','淘汰商品原因');body.append(reason);
      button('淘汰当前商品并继续本店',()=>{
       if(!reason.value.trim())throw Error('请填写淘汰原因');
       if(!window.confirm('仅当商品尚无任何发布任务时才能淘汰。系统将保留商品身份与淘汰记录，不增加完成数。是否继续？'))return;
       return submit('withdraw',{runId:run.id,productId:product.productId,reason:reason.value.trim()});
      });
     }
    }else if(!run.paused){
     const label=document.createElement('label');label.textContent='下一款商品 ';
     const select=document.createElement('select');select.add(new Option('请选择商品',''));
     for(const item of state.products)select.add(new Option(item.name||item.id,item.id));
     label.append(select);body.append(label);
     button('分配到当前店铺',()=>select.value?submit('assign',{runId:run.id,productId:select.value}):null);
    }
    if(!product)button('结束并归档轮转',()=>{
     if(!window.confirm('归档会保留已完成数量、店铺顺序和历史记录。以后可从已保存配置开始新一轮。是否归档？'))return;
     return submit('archive',{runId:run.id});
    });
   }
   if(history.length){
    const archived=document.createElement('details'),caption=document.createElement('summary');
    caption.textContent='归档轮转';archived.append(caption);
    for(const item of history){
     const line=document.createElement('p');
     line.textContent=new Date(item.archivedAt*1000).toLocaleString()+' · 第 '+item.round+' 轮 · '+item.targets.map(row=>row.store).join(' → ')+' · '+item.id;
     archived.append(line);
    }
    body.append(archived);
   }
   button('刷新',async()=>{const data=await read(url);await render(data.run,setup,data.history);});
   button('关闭',()=>modal.close());
  }
  try{
   const [data,setup]=await Promise.all([read(url),request(owner)]);
   if(!sameAccount(owner)||!current())return;
   if(!modal.open)modal.showModal();
   await render(data.run,setup.setup,data.history);
  }catch(error){if(sameAccount(owner)&&current())toast(error.message);}
 };
 window.openStoreRotationSetup=async function(current=()=>true){
  const owner=signedUser?.id;if(!owner)return toast('请先登录当前工作空间');
  const modal=document.getElementById('modal'),storageKey=`junfei-store-rotation-pending:${owner}`;
  let state;
  try{state=await request(owner)}catch(error){if(sameAccount(owner)&&current())toast(error.message);return;}
  if(!sameAccount(owner)||!current())return;
  let pending=null;
  try{pending=JSON.parse(sessionStorage.getItem(storageKey)||'null')}catch{toast('本地请求记录无法读取，请先核对已保存配置');return;}
  if(pending&&(!Array.isArray(pending.targets)||!Array.isArray(pending.markets)||typeof pending.revision!=='number')){toast('本地请求记录格式无效，请先核对已保存配置');return;}
  let markets=pending?.markets||state.setup?.markets||['US','UK'];
  let rows=(pending?.targets||state.setup?.targets||[empty()]).map(row=>({...row}));
  let busy=false,closed=false;modal.classList.add('store-rotation-modal');
  modal.innerHTML=`<div class="rotation-setup"><div class="wb-title-label"><h2>启动任务 · 店铺配置</h2>${workbenchHelp('按下方顺序，每店每轮制作并发布 3 款新商品。ERP 账号只填登录名，Codex 执行前会核对实际店铺与 ID。','启动任务 · 店铺配置')}</div><form id="rotationSetupForm"><label class="field">目标站点<select id="rotationMarkets"><option value="USUK">美国＋英国</option><option value="US">仅美国</option></select></label><div id="rotationRows"></div><button class="btn" type="button" id="rotationAdd">＋ 添加店铺</button><p id="rotationError" role="alert"></p><div class="actions"><button class="btn" type="button" id="rotationClose">取消</button><button class="btn primary" type="submit" id="rotationSave">${pending?'沿原请求保存 / 核对':'保存配置并生成启动指令'}</button></div></form><div id="rotationResult" hidden></div></div>`;
  if(!document.getElementById('rotationSetupStyle')){const style=document.createElement('style');style.id='rotationSetupStyle';style.textContent='#modal.store-rotation-modal{width:min(980px,94vw);max-width:94vw;box-sizing:border-box;overflow:hidden}.rotation-setup{width:100%;box-sizing:border-box;max-height:80vh;overflow:auto;padding:4px}.rotation-row{margin:14px 0;padding:14px;border:1px solid #d5deea;border-radius:8px}.rotation-row-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.rotation-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.rotation-fields label{min-width:0}.rotation-fields input{width:100%;box-sizing:border-box}.rotation-row .actions{gap:6px}.rotation-row button{font-size:12px}.rotation-setup #rotationError{color:#a13f35}.rotation-setup textarea{box-sizing:border-box;width:100%;min-height:180px}.rotation-setup .field{margin:10px 0}@media(max-width:650px){.rotation-setup{width:100%}.rotation-fields{grid-template-columns:1fr}.rotation-row-heading{align-items:flex-start;flex-direction:column}}';document.head.append(style);}
  const form=modal.querySelector('#rotationSetupForm'),select=modal.querySelector('#rotationMarkets'),container=modal.querySelector('#rotationRows'),add=modal.querySelector('#rotationAdd'),save=modal.querySelector('#rotationSave'),errorNode=modal.querySelector('#rotationError');
  const alive=()=>!closed&&modal.open&&sameAccount(owner)&&current();
  const collect=()=>{rows=[...container.querySelectorAll('.rotation-row')].map(row=>Object.fromEntries([...row.querySelectorAll('input')].map(input=>[input.name,input.value])));};
  function renderRows(){
   const uk=markets.includes('UK');
   container.innerHTML=rows.map((row,index)=>`<fieldset class="rotation-row"><div class="rotation-row-heading"><strong>第 ${index+1} 家店 · 本轮3款新商品</strong><div class="actions"><button type="button" class="btn" data-move="${index}" data-delta="-1" aria-label="第${index+1}家店上移" ${index===0?'disabled':''}>上移</button><button type="button" class="btn" data-move="${index}" data-delta="1" aria-label="第${index+1}家店下移" ${index===rows.length-1?'disabled':''}>下移</button><button type="button" class="btn" data-remove="${index}" aria-label="删除第${index+1}家店" ${rows.length===1?'disabled':''}>删除</button></div></div><div class="rotation-fields">${[['ziniaoStoreName','紫鸟店铺名称'],['erpAccount','ERP账号'],['usStoreName','ERP内美国店铺名称'],['ukStoreName','ERP内英国店铺名称']].map(([key,label])=>`<label class="field" ${key==='ukStoreName'&&!uk?'hidden':''}>${label}<input name="${key}" aria-label="第${index+1}家店${label}" value="${escape(row[key])}" maxlength="160" ${key!=='ukStoreName'||uk?'required':''}></label>`).join('')}</div></fieldset>`).join('');
   container.querySelectorAll('[data-move]').forEach(button=>button.onclick=()=>{collect();const i=+button.dataset.move,j=i+(+button.dataset.delta);[rows[i],rows[j]]=[rows[j],rows[i]];renderRows();});
   container.querySelectorAll('[data-remove]').forEach(button=>button.onclick=()=>{collect();rows.splice(+button.dataset.remove,1);renderRows();});
   lock();
  }
  function lock(){form.querySelectorAll('input,select,button[data-move],button[data-remove]').forEach(el=>{el.disabled=busy||!!pending});add.disabled=busy||!!pending||rows.length>=20;save.disabled=busy;if(!busy&&!pending){container.querySelector('[data-move="0"][data-delta="-1"]')?.setAttribute('disabled','');container.querySelector(`[data-move="${rows.length-1}"][data-delta="1"]`)?.setAttribute('disabled','');if(rows.length===1)container.querySelector('[data-remove]')?.setAttribute('disabled','');}}
  select.value=markets.includes('UK')?'USUK':'US';
  select.onchange=()=>{collect();markets=select.value==='USUK'?['US','UK']:['US'];renderRows();};
  add.onclick=()=>{collect();rows.push(empty());renderRows();container.lastElementChild.querySelector('input').focus();};
  modal.querySelector('#rotationClose').onclick=()=>{if(!busy)modal.close();};
  modal.addEventListener('close',()=>{closed=true;modal.classList.remove('store-rotation-modal')},{once:true});
  form.onsubmit=async event=>{
   event.preventDefault();if(busy||!alive())return;errorNode.textContent='';
   if(!pending&&!form.reportValidity())return;
   busy=true;
   try{
    if(!pending){collect();pending={requestId:crypto.randomUUID(),revision:state.revision,markets,targets:rows};sessionStorage.setItem(storageKey,JSON.stringify(pending));}
    lock();save.textContent='正在保存配置…';
    const result=await request(owner,pending);
    sessionStorage.removeItem(storageKey);pending=null;
    if(!alive())return;
    form.hidden=true;const output=modal.querySelector('#rotationResult');output.hidden=false;
    output.innerHTML=`<h3>配置已保存，待Codex接续</h3><p>${result.setup.targets.length}家店 · 每店3款新商品 · 版本${result.revision}${result.currentRevision!==result.revision?'（另有较新版本，请重新打开查看）':''}。名称尚未核对，未启动发布。</p><label class="field">复制给Codex的任务说明<textarea id="rotationInstruction" readonly></textarea></label><div class="actions"><button class="btn primary" id="rotationCopy">复制任务说明</button><button class="btn" id="rotationDownload">下载配置</button><button class="btn" id="rotationDone">完成</button></div><p id="rotationCopyStatus" role="status"></p>`;
    output.querySelector('textarea').value=result.setup.codexInstruction;
    output.querySelector('#rotationCopy').onclick=async()=>{if(!alive())return;try{await navigator.clipboard.writeText(result.setup.codexInstruction);output.querySelector('#rotationCopyStatus').textContent='已复制，粘贴到Codex后下达开始指令。';}catch{const input=output.querySelector('textarea');input.focus();input.select();output.querySelector('#rotationCopyStatus').textContent='请按 Ctrl+C 复制选中的说明。';}};
    output.querySelector('#rotationDownload').onclick=()=>{if(alive())download('store-rotation-'+result.setup.id+'.json',JSON.stringify(result.setup,null,2));};
    output.querySelector('#rotationDone').onclick=()=>modal.close();
    void window.refreshStoreRotationSetup(current);
   }catch(error){
    if(error.status===400||error.status===409){pending=null;sessionStorage.removeItem(storageKey);}
    if(alive())errorNode.textContent=error.message+(pending?'。原请求已保留，请沿原请求核对。':'');
   }finally{busy=false;if(alive()&&!form.hidden){lock();save.textContent=pending?'沿原请求保存 / 核对':'保存配置并生成启动指令';}}
  };
  renderRows();modal.showModal();
 };
})();
