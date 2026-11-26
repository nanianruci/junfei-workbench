import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {automaticColour} from './automatic_colour_workflow.mjs';

export async function runJobs(descriptor){
 const state=JSON.parse(await fs.readFile(descriptor,'utf8'));
 const task=await taskSpace(state.space),page=task.page(state.page);
 const origin=await page.evaluate(()=>location.origin);
 if(origin!==new URL(state.base).origin)throw new Error('当前页面不是目标工作台；请先在同一任务工作区打开已登录工作台');
 const persist=async()=>{
  const temporary=descriptor+'.'+process.pid+'.'+crypto.randomUUID()+'.tmp';
  const handle=await fs.open(temporary,'wx');
  try{await handle.writeFile(JSON.stringify(state,null,2));await handle.sync();}finally{await handle.close();}
  await fs.rename(temporary,descriptor);
 };
 const auth=await page.evaluate(async()=>await(await fetch('/api/auth/status')).json());
 if(!auth.user?.id)throw new Error('请先在当前工作台登录');
 if(state.accountId&&String(state.accountId)!==String(auth.user.id))throw new Error('登录账号与持久任务不同，未查询或提交');
 state.accountId=auth.user.id;
 const api=async(url,data)=>page.evaluate(async({url,data,account})=>{const headers={'X-Workspace-Account':String(account)};if(data!==undefined)headers['content-type']='application/json';const r=await fetch(url,data===undefined?{headers}:{method:'POST',headers,body:JSON.stringify(data)});const body=await r.json();if(!r.ok)throw new Error((body.error||'HTTP '+r.status));return body;},{url,data,account:state.accountId});
 await persist();
 const runtime=fileURLToPath(new URL('../assets/official-colour-runtime.js',import.meta.url));
 const module='data:text/javascript;base64,'+Buffer.from(await fs.readFile(runtime)).toString('base64');
 const upload=async(filename)=>{
  const bytes=await fs.readFile(filename),sha256=crypto.createHash('sha256').update(bytes).digest('hex');
  const result=await page.evaluate(async({data,account})=>{const raw=Uint8Array.from(atob(data),c=>c.charCodeAt(0));const r=await fetch('/api/upload',{method:'POST',headers:{'X-Workspace-Account':String(account)},body:raw});const body=await r.json();if(!r.ok)throw new Error(body.error||r.status);return body;},{data:bytes.toString('base64'),account:state.accountId});
  return {...result,sourceFile:filename,sourceFileSha256:sha256};
 };
 for(const [index,job] of state.jobs.entries()){
  try{
  const input=job.input;
  if(!job.payload){
  if(input.referenceFiles!==undefined&&(!Array.isArray(input.referenceFiles)||input.referenceFiles.length>2))throw Error('最多补充2张原图，与主图合计不超过3张');
  for(const [key,file] of [['source',input.source],...(input.referenceFiles||[]).map((file,i)=>['reference'+i,file]),['mask',input.maskFile],['targetReference',input.targetReferenceFile]]){
   if(!file)continue;
   if(job.uploads?.[key]){
    const current=crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex');
    if(current!==job.uploads[key].sourceFileSha256)throw new Error('持久任务的原图或蒙版已改变；请新建版本目录');
   }else{job.uploads=job.uploads||{};job.uploads[key]=await upload(file);await persist();}
  }
   if(input.variantSource&&!job.association){
    const prepared=await api('/api/image-engine/prepare-variant-source',{productId:input.productId,...input.variantSource});
    job.association=prepared.association;await persist();
   }
   const source=input.generatedColourVariant?job.uploads.source.url:job.association?.sourceUpload||job.uploads?.source?.url;
   let references=[source,...(input.referenceFiles||[]).map((_,i)=>job.uploads['reference'+i].url)],mask,renderedImage,shaderReceipt,target=input.target,qualityCheckId,selectionTaskId;
   if(input.generatedColourVariant)references.push(job.association.sourceUpload);
   if(input.reusePlateTaskId){
    const parent=(await api('/api/image-engine/task?id='+encodeURIComponent(input.reusePlateTaskId))).task;
    if(parent.productId!==input.productId||parent.request.referenceSha256?.[0]!==job.uploads.source.sourceFileSha256)throw new Error('复用背景必须是同账号商品的同一原片哈希');
    const hashes=[job.uploads.source.sourceFileSha256,...(input.referenceFiles||[]).map((_,i)=>job.uploads['reference'+i].sourceFileSha256)];
    if(JSON.stringify(parent.request.referenceSha256)!==JSON.stringify(hashes))throw Error('补充原图与原背景任务不一致');
    references=[...parent.request.references];
   }
   if(input.mode==='colour'){
    const render=async({source,mask,target,settings})=>page.evaluate(async({module,source,mask,target,settings,account})=>{
     const headers={'X-Workspace-Account':String(account)};
     const resources=await(await fetch('/api/image-templates/colour',{headers})).json();const {renderOfficialColour}=await import(module);
     const value=await renderOfficialColour(source,mask,target,resources,settings,{preserveOutsideSelection:true});
     const r=await fetch('/api/upload',{method:'POST',headers,body:value.blob}),body=await r.json();if(!r.ok)throw new Error(body.error||r.status);
     return {renderedImage:body.url,shaderReceipt:value.receipt};
    },{module,source,mask,target,settings,account:state.accountId});
    const result=await automaticColour({job,input,source,manualMask:job.uploads.mask?.url,api,render,persist});
    ({references,mask,target,renderedImage,shaderReceipt,qualityCheckId,selectionTaskId}=result);
   }
   job.payload={requestId:job.requestId,productId:input.productId,mode:input.mode,references,brief:input.brief,
    sourceVariant:job.association?{receiptId:job.association.receiptId}:undefined,
    count:input.count||3,noText:input.noText!==false,layoutTemplateIds:input.layoutTemplateIds,
    preserveSourcePixels:input.preserveSourcePixels===true,neutralBackground:input.neutralBackground===true,repairTemplateId:input.repairTemplateId,editTask:input.editTask||'free',
    generatedColourVariant:input.generatedColourVariant,apparelSuite:input.apparelSuite,needOptimize:input.needOptimize===true,repairSize:input.repairSize||'',repairObjects:input.repairObjects,
    reusePlateTaskId:input.reusePlateTaskId,reusePlanTaskId:input.reusePlanTaskId,reuseGeneratedTaskId:input.reuseGeneratedTaskId,sourceLayerOverrides:input.sourceLayerOverrides,
    mask,target,qualityCheckId,selectionTaskId,polish:input.polish===true,autoMask:false,originalSource:input.mode==='colour'?source:undefined,
    colourRenderer:input.mode==='colour'?'official-glsl':undefined,renderedImage,shaderReceipt};
   // This exact intent is durable before the first submit. Resume never changes it.
   await persist();
  }
  if(!job.taskId){
   let result;
   if(job.submitState==='pending'){
    result=await api('/api/image-engine/request?'+new URLSearchParams({productId:input.productId,requestId:job.requestId}));
   }
   if(!result?.task){
    // The engine registers this unique request before any provider call.
    job.submitState='pending';await persist();
    result=await api('/api/image-engine/start',job.payload);
   }
   job.taskId=result.task.id;job.task=result.task;job.submitState='acknowledged';await persist();
  }
  const deadline=Date.now()+state.timeout*1000;
  while(true){
   const result=await api('/api/image-engine/task?id='+encodeURIComponent(job.taskId));job.task=result.task;await persist();
   if(['completed','needs_revision','failed','submission_unknown','interrupted'].includes(job.task.status))break;
   if(Date.now()>deadline)throw new Error('查询超时；原任务已保存，沿同一输出目录继续，不重发模型');
   await new Promise(r=>setTimeout(r,6000));
  }
  job.downloads=job.downloads||[];
  for(const result of job.task.results){
   const prior=job.downloads.find(d=>d.index===result.index);
   if(prior){try{const existing=await fs.readFile(prior.file);if(crypto.createHash('sha256').update(existing).digest('hex')!==prior.sha256)throw new Error('已下载文件被修改，未覆盖');continue;}
    catch(error){if(error.code!=='ENOENT')throw error;job.downloads=job.downloads.filter(d=>d.index!==result.index);}}
   const bytes=await page.evaluate(async({url,account})=>{const r=await fetch(url,{headers:{'X-Workspace-Account':String(account)}});if(!r.ok)throw new Error('下载HTTP '+r.status);return [...new Uint8Array(await r.arrayBuffer())];},{url:result.image,account:state.accountId});
   const raw=Buffer.from(bytes),sha256=crypto.createHash('sha256').update(raw).digest('hex');
   const expected=result.imageSha256||result.sha256;
   if(expected&&sha256!==expected)throw new Error('服务端结果哈希与下载文件不符');
   const filename=path.join(state.output,String(index+1).padStart(2,'0')+'-'+job.taskId+'-'+String(result.index+1).padStart(2,'0')+'.png');
   try{const old=await fs.readFile(filename);if(crypto.createHash('sha256').update(old).digest('hex')!==sha256)throw new Error('目标文件已存在且内容不同');}
   catch(error){if(error.code!=='ENOENT')throw error;await fs.writeFile(filename,raw);}
   job.downloads.push({index:result.index,file:filename,sha256,bytes:raw.length,url:result.image});await persist();
  }
  // Keep native-resolution composites and model originals as well as delivery
  // PNGs. A later tool revision must not lose the evidence it is compared with.
  job.originalDownloads=job.originalDownloads||[];
  for(const [candidateIndex,candidate]of [...job.task.results,...(job.task.attempts||[])].entries()){
   for(const role of ['raw','rawModelOutput']){
    const url=candidate[role];if(!url)continue;
    const prior=job.originalDownloads.find(d=>d.url===url)||job.downloads.find(d=>d.url===url);
    if(prior){const bytes=await fs.readFile(prior.file);if(crypto.createHash('sha256').update(bytes).digest('hex')!==prior.sha256)throw Error('原尺寸归档文件已修改，未覆盖');if(!job.originalDownloads.some(d=>d.url===url))job.originalDownloads.push({...prior,role});continue;}
    const bytes=await page.evaluate(async({url,account})=>{const r=await fetch(url,{headers:{'X-Workspace-Account':String(account)}});if(!r.ok)throw Error('原尺寸文件下载HTTP '+r.status);return [...new Uint8Array(await r.arrayBuffer())];},{url,account:state.accountId});
    const raw=Buffer.from(bytes),sha256=crypto.createHash('sha256').update(raw).digest('hex');
    const extension=path.extname(new URL(url,state.base).pathname);if(!['.png','.jpg','.webp'].includes(extension))throw Error('原尺寸图片格式未知');
    const filename=path.join(state.output,String(index+1).padStart(2,'0')+'-'+job.taskId+'-original-'+candidateIndex+'-'+role+extension);
    try{const existing=await fs.readFile(filename);if(crypto.createHash('sha256').update(existing).digest('hex')!==sha256)throw Error('原尺寸目标文件已有不同内容，未覆盖');}catch(error){if(error.code!=='ENOENT')throw error;await fs.writeFile(filename,raw);}
    job.originalDownloads.push({index:candidate.index,role,file:filename,sha256,bytes:raw.length,url});await persist();
   }
  }
  await persist();
  console.log(JSON.stringify({job:index+1,taskId:job.taskId,status:job.task.status,files:job.downloads.map(d=>d.file)}));
  if(job.executionError){job.priorExecutionErrors=[...(job.priorExecutionErrors||[]),job.executionError];delete job.executionError;await persist();}
  }catch(error){
   job.executionError={message:String(error.message||error),at:new Date().toISOString()};await persist();
   console.log(JSON.stringify({job:index+1,status:'blocked',taskId:job.taskId,error:job.executionError.message}));
  }
 }
}
