/* Durable Listing tasks. Polling only reads an existing job; it never starts a model. */
const durableListingJobs=new Map(),durableListingPolls=new Map();
const durableListingActive=new Set(['queued','running']);
function durableListingKey(owner,pid,site){return String(owner?.id)+':'+pid+':'+site}
function durableListingCurrent(p,site){return durableListingJobs.get(durableListingKey(signedUser,p.id,site))}
function durableListingMessage(job){
 return job.message||({queued:'文案任务已排队',running:'后台生成中，关页后可继续查看',
  applied:'已更新当前 Listing 文案',ready:'生成完成，资料已变化；结果已保留，请核对',
  needs_review:'上次执行结果待核对，未自动重新生成',failed:'文案生成失败'}[job.status]||'文案结果已保存');
}
async function durableListingApi(path,data){
 const response=await fetch('/api/listing-jobs/'+path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{cache:'no-store'});
 const result=await response.json();
 if(!response.ok){const error=Error(result.error||'无法读取文案任务');error.status=response.status;throw error}
 return result;
}
function rememberDurableListing(owner,job){
 if(signedUser!==owner)return;
 durableListingJobs.set(durableListingKey(owner,job.productId,job.market),job);
 const key=job.productId+'-'+job.market;
 if(durableListingActive.has(job.status))activeListingGeneration.set(key,{status:'running',message:durableListingMessage(job),jobId:job.id,ownerId:owner.id,at:new Date().toISOString()});
 else activeListingGeneration.delete(key);
}
async function refreshDurableListingWorkspace(owner,job){
 if(signedUser!==owner||syncPending||syncBusy||syncConflict)return false;
 if(job.workspaceRevision&&job.workspaceRevision<=serverRevision)return true;
 const before=serverRevision;
 const response=await fetch('/api/state',{cache:'no-store'});
 if(!response.ok)throw Error('结果已保存在服务端，暂时无法读取最新文案');
 const data=await response.json();
 if(signedUser!==owner||syncPending||syncBusy||syncConflict||serverRevision!==before)return false;
 state=data.state;serverRevision=data.revision;
 syncLabel('已保存到数据库');
 if(['listing','images','products'].includes(view))render();
 return true;
}
function paintDurableListing(owner,job){
 if(signedUser!==owner)return;
 rememberDurableListing(owner,job);
 if(view==='listing'&&selected===job.productId&&market===job.market){mountListingProgress();updateListingCopyActions()}
}
async function waitDurableListing(owner,first){
 const pollKey=durableListingKey(owner,first.productId,first.market)+':'+first.id;
 if(durableListingPolls.has(pollKey))return durableListingPolls.get(pollKey);
 const promise=(async()=>{
  let job=first;
  while(signedUser===owner){
   paintDurableListing(owner,job);
   if(!durableListingActive.has(job.status)){
    const refreshed=await refreshDurableListingWorkspace(owner,job);
    if(!refreshed&&view==='listing'&&selected===job.productId&&signedUser===owner)render();
    return job;
   }
   await new Promise(resolve=>setTimeout(resolve,2000));
   if(signedUser!==owner)return null;
   try{job=(await durableListingApi('status?id='+encodeURIComponent(job.id))).job}
   catch(error){
    // A lost status response is not permission to create another paid request.
    if(signedUser!==owner)return null;
    const key=job.productId+'-'+job.market;
    activeListingGeneration.set(key,{status:'running',message:'任务已保存在服务端，连接恢复后继续读取',jobId:job.id,ownerId:owner.id,at:new Date().toISOString()});
    if(view==='listing'&&selected===job.productId)mountListingProgress();
   }
  }
  return null;
 })();
 durableListingPolls.set(pollKey,promise);
 try{return await promise}finally{
  durableListingPolls.delete(pollKey);
  const key=first.productId+'-'+first.market;
  if(signedUser!==owner&&activeListingGeneration.get(key)?.jobId===first.id)activeListingGeneration.delete(key);
 }
}
async function restoreListingJobs(){
 const owner=signedUser;
 if(!owner||!serverReady)return;
 for(const [key,item] of activeListingGeneration)if(item.ownerId&&item.ownerId!==owner.id)activeListingGeneration.delete(key);
 try{
  const {jobs}=await durableListingApi('list');
  if(signedUser!==owner)return;
  const seen=new Set();
  for(const job of jobs||[]){
   const key=durableListingKey(owner,job.productId,job.market);
   if(seen.has(key))continue;
   seen.add(key);rememberDurableListing(owner,job);
   if(durableListingActive.has(job.status))void waitDurableListing(owner,job).catch(error=>{if(signedUser===owner)toast(error.message)});
   else if(job.status==='applied'&&job.workspaceRevision>serverRevision)await refreshDurableListingWorkspace(owner,job);
  }
  if(['listing','images','products'].includes(view))render();
 }catch(error){if(signedUser===owner)toast('文案生成记录暂时无法读取，未发起新请求：'+error.message)}
}
async function showDurableListingResult(job){
 const owner=signedUser;
 try{
  const current=(await durableListingApi('status?id='+encodeURIComponent(job.id))).job;
  if(signedUser!==owner)return;
  rememberDurableListing(owner,current);
  const modal=document.getElementById('modal');
  modal.replaceChildren();
  const heading=document.createElement('h2');heading.textContent='已保存的文案生成结果';
  const note=document.createElement('p');note.textContent=durableListingMessage(current);
  const area=document.createElement('textarea');area.readOnly=true;area.rows=18;area.style.cssText='width:100%;margin:16px 0;box-sizing:border-box';
  const copy=current.result?.copy;
  area.value=copy?listingFields.map(([field,label])=>label+'\n'+String(copy[field]||'')).join('\n\n'):'任务 '+current.id+'\n阶段：'+(current.stage||current.status)+'\n'+(current.error||'此任务尚无可恢复的最终稿，请先核对原模型回执。');
  const actions=document.createElement('div');actions.className='actions';
  if(copy){const save=document.createElement('button');save.className='btn';save.textContent='下载结果';save.onclick=()=>download(current.productId+'-'+current.market+'-生成结果.json',JSON.stringify(current.result,null,2));actions.append(save)}
  const close=document.createElement('button');close.className='btn';close.textContent='关闭';close.onclick=()=>modal.close();actions.append(close);
  modal.append(heading,note,area,actions);modal.showModal();
 }catch(error){if(signedUser===owner)toast(error.message)}
}
async function applyDurableListing(job){
 const owner=signedUser;
 try{
  await flushWorkspace();
  if(signedUser!==owner)return;
  const current=(await durableListingApi('apply',{id:job.id})).job;
  if(signedUser!==owner)return;
  paintDurableListing(owner,current);
  if(current.status==='applied')await refreshDurableListingWorkspace(owner,current);
  toast(durableListingMessage(current));
 }catch(error){if(signedUser===owner)toast(error.message)}
}
function mountDurableListingProgress(){
 const p=product();if(!p)return false;
 const actionSlot=document.getElementById('listingGenerationActions');
 const job=durableListingCurrent(p,market);
 if(!job){actionSlot?.replaceChildren();return false}
 let box=document.getElementById('listingGenerationProgress');
 if(!box){const anchor=document.querySelector('.titlebar');if(!anchor)return true;box=document.createElement('div');box.id='listingGenerationProgress';anchor.after(box)}
 const status=job.status==='applied'?'completed':job.status==='needs_review'?'uncertain':job.status==='failed'?'failed':durableListingActive.has(job.status)?'running':'completed';
 const active=activeListingGeneration.get(p.id+'-'+market);
 const label=active?.message||durableListingMessage(job);
 box.innerHTML=generationProgressHTML(status,label);
 const actions=document.createElement('div');actions.className='actions';actions.style.marginTop='8px';
 if(job.resultAvailable||job.status==='needs_review'){
  const result=document.createElement('button');result.type='button';result.className='btn';result.textContent=job.resultAvailable?'查看已保存结果':'查看生成记录';result.onclick=()=>void showDurableListingResult(job);actions.append(result);
 }
 if(job.status==='ready'){
  const apply=document.createElement('button');apply.type='button';apply.className='btn';apply.textContent='尝试恢复';apply.onclick=()=>void applyDurableListing(job);actions.append(apply);
 }
 if(job.status==='applied'){
  const refresh=document.createElement('button');refresh.type='button';refresh.className='btn';refresh.textContent='读取最新文案';refresh.onclick=()=>void refreshDurableListingWorkspace(signedUser,job).then(ok=>{if(!ok)toast('本页有未保存修改，请先保存或备份后读取最新文案')}).catch(error=>toast(error.message));actions.append(refresh);
 }
 if(actionSlot)actionSlot.replaceChildren(...actions.childNodes);
 else if(actions.childNodes.length)box.append(actions);
 return true;
}
async function durableGenerateCurrent(p,targetMarket,button,{batch=false}={}){
 const owner=signedUser;
 const existing=durableListingCurrent(p,targetMarket);
 if(existing&&['ready','needs_review'].includes(existing.status)){
  if(!batch)await showDurableListingResult(existing);
  return {status:'skipped',message:durableListingMessage(existing)};
 }
 const blocker=listingBatchReason(p,targetMarket);
 if(blocker){if(!batch)toast(blocker);return {status:'skipped',message:blocker}}
 if(p.listingGeneration?.[targetMarket]?.status==='uncertain')return {status:'skipped',message:'上次生成结果待核对，未重新调用模型'};
 if(!batch&&!confirm('将当前已保存资料发送至 '+listingTextProvider+' 生成 '+targetMarket+' 站文案，成功后直接更新当前文案。关页后仍可恢复，可能产生 API 费用。是否开始？'))return {status:'skipped',message:'已取消'};
 button.disabled=true;
 let job;
 try{
  await flushWorkspace();
  if(!owner||signedUser!==owner||!state.products.some(item=>item.id===p.id))throw Error('账号或产品已变化');
  const requestId=globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  job=(await durableListingApi('start',{productId:p.id,market:targetMarket,requestId,expectedRevision:serverRevision})).job;
  if(signedUser!==owner)return {status:'background',message:'任务已保存在原账号'};
  rememberDurableListing(owner,job);
  await refreshDurableListingWorkspace(owner,job);
  if(!batch&&view==='listing')render();
  const done=await waitDurableListing(owner,job);
  if(!done)return {status:'background',message:'任务已保存在原账号'};
  const status=done.status==='applied'?'saved':done.status==='ready'?'retained':done.status==='needs_review'?'uncertain':done.status;
  const message=durableListingMessage(done);
  if(!batch)toast(message);
  return {status,message};
 }catch(error){
  // Start itself may have reached the server. Reconcile by list; never retry POST here.
  if(signedUser===owner){await restoreListingJobs();if(!batch)toast(error.message+'；请查看已保存任务，未自动重试')}
  return {status:job?'background':error.status&&error.status<500?'failed':'uncertain',message:error.message};
 }finally{if(signedUser===owner){button.disabled=false;mountListingProgress();updateListingCopyActions()}}
}

const initServerBeforeDurableListings=initServer;
initServer=async function(){const ready=await initServerBeforeDurableListings();if(ready)void restoreListingJobs();return ready};
