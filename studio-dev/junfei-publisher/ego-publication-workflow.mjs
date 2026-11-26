// Deliberately split at US acceptance: the host reuses the same ERP lease and
// establishes the exact UK store/plugin session before resuming phase 'uk'.
import {publishNative,observePublishedNative} from './ego-publish-native.mjs';
import {copyNative} from './ego-copy-native.mjs';
import {prepareUkDraft} from './ego-uk-draft.mjs';
import {prepareUsDraft,assertUsPreparedIdentity} from './ego-us-draft.mjs';
import {recoverNativeMediaPublication} from './ego-publication-recovery.mjs';

export async function runPublicationWorkflow(page,input,record,persist,{phase='all',adapters={},mediaEpisodeObserveOnly=false,ukMediaObserveOnly=false}={}){
 if(!['us','uk','all','observe','uk_required_attributes','us_media_episode','uk_identifiers','uk_media_recovery'].includes(phase))throw Error('发布阶段无效');
 const usOnly=input.publicationMarkets?.length===1&&input.publicationMarkets[0]==='US';
 if(usOnly&&!['us','observe','us_media_episode'].includes(phase))throw Error('仅美国发布任务不能执行跨站发布或恢复');
 const expected={accountId:input.accountId,productId:input.productId,snapshotSha256:input.sha256};
 for(const [key,value]of Object.entries(expected)){
  if(!value||(record[key]!==undefined&&record[key]!==value))throw Error('发布流程回执不属于本账号产品快照');record[key]=value;
 }
 if(input.jobId){if(record.jobId&&record.jobId!==input.jobId)throw Error('发布流程回执属于其他任务');record.jobId=input.jobId;}
 record.usPublish??={};record.ukCopy??={};record.ukPrepare??={};record.ukPublish??={};
 const api={publish:publishNative,observe:observePublishedNative,recoverMedia:recoverNativeMediaPublication,
  repairMainImage:async(...args)=>(await import('./ego-publication-main-image.mjs')).repairNativeMainImage(...args),
  repairRequiredAttributes:async(...args)=>(await import('./ego-publication-required-attributes.mjs')).repairNativeRequiredAttributes(...args),
  repairIdentifiers:async(...args)=>(await import('./ego-publication-identifiers.mjs')).repairNativeIdentifiers(...args),
  retryMediaEpisode:async(...args)=>(await import('./ego-publication-media-episode.mjs')).retryNativeMediaEpisode(...args),
  retryUkMedia:async(...args)=>(await import('./ego-publication-uk-media.mjs')).retryNativeUkMedia(...args),
  copy:copyNative,prepareUk:prepareUkDraft,prepareUs:prepareUsDraft,...adapters};
 const save=async()=>{record.updatedAt=new Date().toISOString();await persist();};
 try{
  if(phase==='uk_media_recovery'){
   const uk=record.ukPrepare,p=uk?.preparedInput,copy=record.ukCopy;
   if(!input.publicationUkMediaRecovery||uk?.phase!=='uk_draft_verified'||copy?.phase!=='copy_verified'||!p||
      String(uk.draftId)!==String(copy.draftId)||String(p.destination?.draftId)!==String(copy.draftId)||
      p.accountId!==input.accountId||p.productId!==input.productId||p.sha256!==input.sha256||p.jobId!==input.jobId||p.destination.countryCode!=='GB'||
      p.store!==input.ukTarget?.storeName||p.destination.destinationId!==(input.ukTarget?.id||input.ukTarget?.destinationId)||
      p.goods.parentSku!==input.goods.parentSku||JSON.stringify(p.goodsItems.map(v=>v.sku).sort())!==JSON.stringify(input.goodsItems.map(v=>v.sku).sort()))throw Error('英国媒体恢复缺少原冻结草稿和SKU身份');
   const ukInput={...p,urls:uk.urls};record.phase='uk_media_recovery';await save();
   await api.retryUkMedia(page,ukInput,record.ukPublish,save,{amendment:input.publicationUkMediaRecovery,
    schemaObservation:uk.schemaObservation,observeOnly:ukMediaObserveOnly,...adapters.ukMediaAdapters});
   await api.publish(page,ukInput,record.ukPublish,save,adapters.publishAdapters);
   record.phase='uk_media_reconciled';delete record.error;await save();return record;
  }
  if(input.publicationUkMediaRecovery&&['us','uk','all'].includes(phase))throw Error('原英国媒体恢复须专用检查点，不能重放旧修复或复制');
  if(phase==='uk_identifiers'){
   const uk=record.ukPrepare,p=uk?.preparedInput,copy=record.ukCopy;
   if(!input.publicationIdentifierRepair||uk?.phase!=='uk_draft_verified'||copy?.phase!=='copy_verified'||!p||
      String(uk.draftId)!==String(copy.draftId)||String(p.destination?.draftId)!==String(copy.draftId)||
      p.accountId!==input.accountId||p.productId!==input.productId||p.sha256!==input.sha256||p.destination.countryCode!=='GB'||
      p.store!==input.ukTarget?.storeName||p.destination.destinationId!==(input.ukTarget?.id||input.ukTarget?.destinationId)||
      p.goods.parentSku!==input.goods.parentSku||JSON.stringify(p.goodsItems.map(v=>v.sku).sort())!==JSON.stringify(input.goodsItems.map(v=>v.sku).sort()))throw Error('编码纠正缺少原英国冻结草稿及SKU身份');
   const ukInput={...p,urls:uk.urls};record.phase='uk_identifiers_repair';await save();
   await api.repairIdentifiers(page,ukInput,record.ukPublish,save,{amendment:input.publicationIdentifierRepair,
    sourceEvidence:copy.sourceEvidence?.data||copy.sourceEvidence,exemptionRecord:uk.brandExemption,schemaObservation:uk.schemaObservation,...adapters.identifierRepairAdapters});
   await api.publish(page,ukInput,record.ukPublish,save,adapters.publishAdapters);
   record.phase='uk_identifiers_reconciled';delete record.error;await save();return record;
  }
  if(phase==='us_media_episode'){
   if(!Array.isArray(input.publicationMediaEpisodes)||input.publicationMediaEpisodes.length!==1||
      !/^[1-9]\d*$/.test(String(record.usPublish.publishId||''))||!record.usPrepare?.preparedInput)throw Error('媒体episode缺少唯一原美国任务和冻结输入');
   assertUsPreparedIdentity(input,record.usPrepare.preparedInput);
   const prepared=record.usPrepare.preparedInput;record.phase='us_media_episode';await save();
   await api.retryMediaEpisode(page,prepared,record.usPublish,save,{amendment:input.publicationMediaEpisodes[0],
    schemaObservation:record.usPrepare.schemaObservation,observeOnly:mediaEpisodeObserveOnly,...adapters.mediaEpisodeAdapters});
   await api.publish(page,prepared,record.usPublish,save,adapters.publishAdapters);
   record.phase='us_media_episode_reconciled';delete record.error;await save();return record;
  }
  if(input.publicationMediaEpisodes?.length&&['us','all'].includes(phase))throw Error('原媒体episode须专用检查点继续，不能重放旧MAIN或媒体恢复');
  // This original-UK repair deliberately does not reconcile, copy, or write US.
  // Its full scope and independent once-only intents were frozen by the host.
  if(phase==='uk_required_attributes'){
   const uk=record.ukPrepare,p=uk?.preparedInput,copy=record.ukCopy;
   if(!input.publicationRequiredAttributeRepair||uk?.phase!=='uk_draft_verified'||copy?.phase!=='copy_verified'||!p||
      String(uk.draftId)!==String(copy.draftId)||String(p.destination?.draftId)!==String(copy.draftId)||
      p.accountId!==input.accountId||p.productId!==input.productId||p.sha256!==input.sha256||p.destination.countryCode!=='GB'||
      p.store!==input.ukTarget?.storeName||p.destination.destinationId!==(input.ukTarget?.id||input.ukTarget?.destinationId)||
      p.goods.parentSku!==input.goods.parentSku||JSON.stringify(p.goodsItems.map(v=>v.sku).sort())!==JSON.stringify(input.goodsItems.map(v=>v.sku).sort()))throw Error('必填属性修复缺少原英国冻结草稿及SKU身份');
   const ukInput={...p,urls:uk.urls};record.phase='uk_required_attributes_repair';await save();
   await api.repairRequiredAttributes(page,ukInput,record.ukPublish,save,{amendment:input.publicationRequiredAttributeRepair,schemaObservation:uk.schemaObservation,...adapters.requiredAttributeRepairAdapters});
   await api.publish(page,ukInput,record.ukPublish,save,adapters.publishAdapters);
   record.phase='uk_required_attributes_reconciled';delete record.error;await save();return record;
  }
  if(record.usPrepare?.preparedInput)assertUsPreparedIdentity(input,record.usPrepare.preparedInput);
  // A completed job can acquire later Amazon errors. An explicit observation
  // reads the two original IDs, without login, preparation, copy or recovery.
  if (usOnly && phase === 'observe') {
    if (!/^[1-9]\d*$/.test(String(record.usPublish.publishId || ''))) throw Error('缺少原美国发布任务号');
    const prepared = record.usPrepare?.preparedInput;
    if (prepared) assertUsPreparedIdentity(input, prepared);
    await api.observe(page, prepared || input, record.usPublish, save, adapters.publishAdapters);
    record.phase = 'us_submitted';
    record.allOnline = record.usPublish.latestObservation?.allChildrenOnline === true;
    delete record.error;
    await save();
    return record;
  }
  if(phase==='observe'){
   const uk=record.ukPrepare,prepared=uk?.preparedInput;
   if(!/^[1-9]\d*$/.test(String(record.usPublish.publishId||''))||
      !/^[1-9]\d*$/.test(String(record.ukPublish.publishId||'')))throw Error('只读核对需要原美国及英国发布任务号');
   if(uk?.phase!=='uk_draft_verified'||record.ukCopy.phase!=='copy_verified'||!prepared||
      String(uk.draftId)!==String(record.ukCopy.draftId)||String(prepared.destination?.draftId)!==String(record.ukCopy.draftId)||
      prepared.accountId!==input.accountId||prepared.productId!==input.productId||prepared.sha256!==input.sha256||
      prepared.destination.countryCode!=='GB'||prepared.store!==input.ukTarget?.storeName||
      prepared.destination.destinationId!==(input.ukTarget?.id||input.ukTarget?.destinationId)||
      prepared.goods?.parentSku!==input.goods.parentSku||
      JSON.stringify(prepared.goodsItems?.map(v=>v.sku).sort())!==JSON.stringify(input.goodsItems.map(v=>v.sku).sort()))throw Error('只读核对缺少原英国已冻结输入及SKU身份');
   record.phase='observing_original_tasks';await save();const failures=[];
   for(const [label,preparedInput,publication]of [['美国',record.usPrepare?.preparedInput||input,record.usPublish],
     ['英国',{...prepared,urls:uk.urls},record.ukPublish]]){
    try{
     await api.observe(page,preparedInput,publication,save,adapters.publishAdapters);
     const evidence=publication.latestObservation;
     if(evidence?.verification==='partial_online_readback'){
      const cover=evidence.exactCoverUrl?'封面 URL 一致':'Amazon CDN 封面 URL 有差异，图片内容待核验';
      failures.push(`${label}原发布 ${evidence.publishId} 已匹配 ERP 线上父体 ${evidence.recordId}，${evidence.childrenWithAsin}/${evidence.childCount} 个子SKU有ASIN，${evidence.childrenWithStatus}/${evidence.childCount} 个有在售状态，${cover}`);
     }
    }
    catch(error){failures.push(`${label}原发布 ${publication.publishId} ${error.message}`);}
   }
   if(failures.length){record.allOnline=false;throw Error(failures.join('；'));}
   record.phase='both_submitted';record.allOnline=Boolean(record.usPublish.latestObservation?.allChildrenOnline&&record.ukPublish.latestObservation?.allChildrenOnline);
   delete record.error;await save();return record;
  }
  if(phase==='us'||phase==='all'){
   // Accepted/uncertain publications retain their original prepared input.
   // Never reopen a consumed draft or change facts while reconciling a submit.
   if(!record.usPublish.intent&&!record.usPublish.publishId){
    record.usPrepare??={};record.phase='us_preparation';await save();
    await api.prepareUs(page,input,record.usPrepare,save,adapters.prepareUsAdapters);
    if(record.usPrepare.phase!=='us_draft_verified'||!record.usPrepare.preparedInput)throw Error('美国原草稿必填属性尚未保存回读');
   }
   record.phase='us_publication';await save();
   if(record.usPrepare?.preparedInput)assertUsPreparedIdentity(input,record.usPrepare.preparedInput);
   if(input.publicationMainImageRepair){
    await api.repairMainImage(page,record.usPrepare?.preparedInput||input,record.usPublish,save,
     {amendment:input.publicationMainImageRepair,schemaObservation:record.usPrepare?.schemaObservation,...adapters.mainImageRepairAdapters});
   }
   if(input.publicationRecovery){
    await api.recoverMedia(page,record.usPrepare?.preparedInput||input,record.usPublish,save,
     {amendment:input.publicationRecovery,...adapters.recoveryAdapters});
   }
   await api.publish(page,record.usPrepare?.preparedInput||input,record.usPublish,save,adapters.publishAdapters);
   if(!record.usPublish.publishId||record.usPublish.phase!=='publishing_verified')throw Error('美国发布任务尚未明确受理');
   record.phase=usOnly?'us_submitted':'awaiting_uk_session';await save();
   if(phase==='us')return record;
  }
  if(!record.usPublish.publishId||record.usPublish.phase!=='publishing_verified')throw Error('缺少原美国真实发布中回执，不创建英国草稿');
  const p=record.usPrepare?.preparedInput;
  const source=p?{...input,goods:p.goods,destination:p.destination,publicationFacts:p.publicationFacts}:input;
  if(!input.ukTarget||!input.ukCommerce)throw Error('缺少已配置的英国目标与经营参数');
  const prepare=record.ukPrepare;
  const reconcilePublication=Boolean(record.ukPublish.intent||record.ukPublish.publishId);
  if(!reconcilePublication){
   record.phase='uk_copy';await save();
   await api.copy(page,{source,target:source,sourcePublication:record.usPublish},record.ukCopy,save,adapters.copyAdapters);
   if(!record.ukCopy.draftId||record.ukCopy.phase!=='copy_verified')throw Error('英国复制来源尚未明确');
   if(prepare.draftId&&String(prepare.draftId)!==String(record.ukCopy.draftId))throw Error('英国准备记录草稿与原复制不符');
   Object.assign(prepare,{draftId:record.ukCopy.draftId,sourceEvidence:record.ukCopy.sourceEvidence,urls:record.ukCopy.urls});
   record.phase='uk_preparation';await save();
   await api.prepareUk(page,source,prepare,save,adapters.prepareAdapters);
  }
  // A submitted/uncertain UK publish can consume its original draft. Resume
  // from the already verified frozen UK input, never reopen or recreate that
  // draft and never repeat its source copy to reconcile the publish task.
  if(prepare.phase!=='uk_draft_verified'||!prepare.preparedInput)throw Error('英国草稿未通过SKU、图序及站点经营参数回读');
  if(!record.ukCopy.draftId||record.ukCopy.phase!=='copy_verified'||String(prepare.draftId)!==String(record.ukCopy.draftId)||String(prepare.preparedInput.destination.draftId)!==String(record.ukCopy.draftId)||prepare.preparedInput.accountId!==input.accountId||prepare.preparedInput.productId!==input.productId||prepare.preparedInput.destination.countryCode!=='GB'||prepare.preparedInput.store!==input.ukTarget.storeName||prepare.preparedInput.destination.destinationId!==(input.ukTarget.id||input.ukTarget.destinationId))throw Error('英国发布输入与原复制、账号或目标店铺不符');
  if(prepare.preparedInput.goods.parentSku!==input.goods.parentSku||JSON.stringify(prepare.preparedInput.goodsItems.map(v=>v.sku).sort())!==JSON.stringify(input.goodsItems.map(v=>v.sku).sort()))throw Error('英国父子SKU发生变化，禁止发布');
  record.phase='uk_publication';await save();
  const ukInput={...prepare.preparedInput,urls:prepare.urls};
  await api.publish(page,ukInput,record.ukPublish,save,adapters.publishAdapters);
  if(!record.ukPublish.publishId||record.ukPublish.phase!=='publishing_verified')throw Error('英国发布任务尚未明确受理');
  record.phase='both_submitted';record.allOnline=Boolean(record.usPublish.identity?.allChildrenOnline&&record.ukPublish.identity?.allChildrenOnline);delete record.error;await save();return record;
 }catch(error){record.error=String(error.message);await save();throw error;}
}
