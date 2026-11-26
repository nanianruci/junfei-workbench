// Display ERP handoff completion without rewriting product facts or reviews.
let productPublicationJobs=[];
let productProductionProgress=[];
let productPublicationOwner=null;
let productPublicationRequest=null;
let productPublicationTimer;
const publicationNumber=value=>/^[1-9]\d*$/.test(String(value||''));
const publicationAcceptedStatuses=new Set(['accepted','publishing','submitted','online','active','buyable','discoverable']);

function productPublicationJobsFor(product){
 const owner=typeof signedUser==='undefined'?null:signedUser?.id;
 return productPublicationOwner===owner?productPublicationJobs.filter(job=>
  job.productId===product.id&&job.parentSku===product.sku):[];
}

function publicationObservationAllowsCompletion(observation,taskId){
 if(observation==null)return true; // Older, accepted receipts have no later observation.
 return String(observation.publishId)===String(taskId)&&observation.failed!==true&&
  (observation.verification===undefined||observation.verification==='accepted')&&publicationAcceptedStatuses.has(observation.status);
}

function publicationJobCompleted(job){
 return job.status==='submitted'&&job.publishSubmitted===true&&
  publicationNumber(job.usPublishId)&&publicationObservationAllowsCompletion(job.usPublicationObservation,job.usPublishId)&&
  (job.markets?.join() === 'US' || publicationNumber(job.ukPublishId)&&
   publicationObservationAllowsCompletion(job.ukPublicationObservation,job.ukPublishId));
}

function publicationReceiptSuperseded(jobs,taskId){
 // A single-market historical receipt cannot complete its still-unfinished
 // formal US/UK task. A later failure of that same task also takes precedence.
 return jobs.some(job=>[job.usPublishId,job.ukPublishId].some(id=>
  publicationNumber(id)&&String(id)===String(taskId))&&!publicationJobCompleted(job));
}

function productCompletionRecord(product){
 const owner=typeof signedUser==='undefined'?null:signedUser?.id;
 const jobs=productPublicationJobsFor(product),formal=jobs.find(publicationJobCompleted);
 if(formal)return{market:formal.markets?.join(' / ')||'US / UK',store:[formal.sourceStore,formal.targetStore].filter(Boolean).join(' / '),taskId:formal.usPublishId,
  ukTaskId:formal.ukPublishId,checkedAt:formal.submittedAt?new Date(formal.submittedAt*1000).toISOString():null,verifiedLive:formal.onlineVerified===true};
 const accepted=publicationAcceptedStatuses;
 const p=product.erpPublication;
 if(p&&publicationNumber(p.recordId||p.publishId||p.taskId)&&accepted.has(p.status)&&
    !publicationReceiptSuperseded(jobs,p.recordId||p.publishId||p.taskId)&&
    publicationObservationAllowsCompletion(p.latestObservation??p.failureObservation,p.recordId||p.publishId||p.taskId)&&
    (!p.productId||p.productId===product.id)&&(!p.parentSku||p.parentSku===product.sku)&&
    (!p.accountId||String(p.accountId)===String(owner))&&
    Array.isArray(p.children)&&p.children.length&&p.children.every(c=>accepted.has(c.status)&&
      typeof c.sku==='string'&&(!product.sku||c.sku.startsWith(product.sku+'-')))){
  return{market:p.marketplace||p.market||product.market||product.sourceImport?.market||'US',store:p.store,
   taskId:p.recordId||p.publishId||p.taskId,parentAsin:p.parentAsin,count:p.children.length,
   checkedAt:p.checkedAt,verifiedLive:p.allChildrenOnline===true};
 }
 const d=product.erpProductionDraft,o=product.erpPublishObservation;
 if(d?.published===true&&publicationNumber(d.taskId)&&!d.libraryDraftOnly&&
    !publicationReceiptSuperseded(jobs,d.taskId)&&
    publicationObservationAllowsCompletion(d.latestObservation??d.failureObservation,d.taskId)&&
    (d.verifiedLive===true||accepted.has(d.status))&&(!d.parentSku||d.parentSku===product.sku)){
  const market=o?.marketplace||d.marketplace;
  if(market&&(!d.marketplace||d.marketplace===market))return{market,store:d.store,taskId:d.taskId,
   parentAsin:o?.parentAsin||d.parentAsin,checkedAt:o?.checkedAt,
   count:o?.children?.length,verifiedLive:d.verifiedLive===true&&o?.allChildrenOnline===true};
 }
 return null;
}

function productDisplayStatus(product){
 if(productCompletionRecord(product))return '已完成';
 const owner=typeof signedUser==='undefined'?null:signedUser?.id;
 if(productPublicationJobsFor(product).some(job=>['needs_reconciliation','failed','blocked'].includes(job.status)||
   [job.usPublicationObservation,job.ukPublicationObservation].some(o=>o&&(o.failed===true||o.verification==='failed'))))return '需修改';
 if(productPublicationOwner===owner&&productProductionProgress.some(p=>
  p.productId===product.id&&p.parentSku===(product.sku||'')&&p.needsAttention===true))return '需修改';
 if(['待审核','待质检','待人工审核','已通过','已完成'].includes(product.status))return '制作中';
 return product.status||'待补资料';
}

async function refreshProductPublicationStatus(){
 clearTimeout(productPublicationTimer);
 if(typeof signedUser==='undefined'||!signedUser||!serverReady||!['products','home'].includes(view))return;
 const owner=signedUser;
 if(productPublicationRequest===owner)return;
 if(productPublicationOwner!==owner.id){productPublicationOwner=owner.id;productPublicationJobs=[];productProductionProgress=[];}
 productPublicationRequest=owner;
 try{
  const results=await Promise.allSettled(['/api/publication/list','/api/products/progress'].map(async path=>{
   const response=await fetch(path,{headers:{'X-Workspace-Account':String(owner.id)}});
   const data=await response.json();if(!response.ok)throw Error(data.error||'任务状态读取失败');return data;
  }));
  if(signedUser!==owner)return;
  const before=JSON.stringify(state.products.map(p=>[productCompletionRecord(p),productDisplayStatus(p)]));
  const publication=results[0].status==='fulfilled'?results[0].value:null;
  const progress=results[1].status==='fulfilled'?results[1].value:null;
  if(Array.isArray(publication?.jobs))productPublicationJobs=publication.jobs;
  if(Array.isArray(progress?.products))productProductionProgress=progress.products;
  const after=JSON.stringify(state.products.map(p=>[productCompletionRecord(p),productDisplayStatus(p)]));
  if(before!==after&&view==='products')render();
  if((productPublicationJobs.some(j=>['queued','running'].includes(j.status))||productProductionProgress.some(p=>p.running===true))&&['products','home'].includes(view))
   productPublicationTimer=setTimeout(()=>void refreshProductPublicationStatus(),5000);
 }catch{ /* Existing saved receipts still render; a failed read never completes a product. */ }
 finally{if(productPublicationRequest===owner)productPublicationRequest=null;}
}

/* Publication ledger is separate from draft-only ERP delivery tasks. */
let publicationTaskTimer;
async function mountPublicationTasks(host){
 clearTimeout(publicationTaskTimer);
 const owner=signedUser;
 if(!host||!owner)return;
 const current=()=>signedUser===owner&&host.isConnected&&view==='agent';
 try{
  const r=await fetch('/api/publication/list',{headers:{'X-Workspace-Account':String(owner.id)}}),data=await r.json();
  if(!current())return;if(!r.ok)throw Error(data.error||'发布记录读取失败');
  host.innerHTML=`<div class="wb-title-label"><h2>亚马逊发布任务</h2>${workbenchHelp('从商品交付页启动正式发布。美英联动时先提交美国，取得任务号后再复制到英国，核对店铺、原 SKU、GBP 售价和豁免。此处记录各阶段的原回执。','亚马逊发布任务')}</div>`+(data.jobs.length?data.jobs.map(j=>`<div class="reviewitem"><div><strong>${esc(j.productName||j.parentSku)}</strong><p>${esc(j.sourceStore)} → ${esc(j.targetStore)}</p><p>${esc(j.phase)}</p><small>美国发布 ${esc(j.usPublishId||'未取得')} · 英国草稿 ${esc(j.ukDraftId||'未取得')} · 英国发布 ${esc(j.ukPublishId||'未取得')}</small>${j.error?`<p>${esc(j.error)}</p>`:''}</div><div class="actions"><button class="btn" data-publication-product="${esc(j.productId)}">查看产品</button>${j.status==='needs_reconciliation'?`<button class="btn" data-publication-resume="${esc(j.id)}">核对原回执并继续</button>`:''}</div></div>`).join(''):'<p>暂无发布任务，未自动处理现有产品。</p>');
  host.querySelectorAll('[data-publication-product]').forEach(button=>button.onclick=()=>openListingWorkspace(button.dataset.publicationProduct,'delivery'));
  host.querySelectorAll('[data-publication-resume]').forEach(button=>button.onclick=async()=>{
   if(!current())return;
   button.disabled=true;
   try{
    const result=await window.confirmPublicationOperation(owner.id,'continue',{id:button.dataset.publicationResume,confirmed:true});
    if(result&&current())await mountPublicationTasks(host);
   }
   catch(e){if(current())toast(e.message)}finally{if(button.isConnected)button.disabled=false}
  });
  publicationTaskTimer=setTimeout(()=>{if(current())void mountPublicationTasks(host)},3000);
 }catch(e){if(current())host.textContent='发布记录暂时无法读取：'+e.message;}
}
