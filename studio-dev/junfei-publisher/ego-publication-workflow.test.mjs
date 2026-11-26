import test from 'node:test';
import assert from 'node:assert/strict';
import {runPublicationWorkflow} from './ego-publication-workflow.mjs';

const input=()=>({accountId:2,productId:'mock-product',jobId:'mock-job',sha256:'fixed-snapshot',
 goods:{parentSku:'exact-parent',brand:'Actual Brand'},goodsItems:[{sku:'exact-child'}],destination:{countryCode:'US'},
 ukTarget:{id:'uk-target',storeName:'Exact UK'},ukCommerce:{currency:'GBP'}});
function adapters(events){return{
 prepareUs:async(_page,i,r,save)=>{r.phase='us_draft_verified';r.preparedInput=structuredClone(i);await save();},
 publish:async(_page,i,r,save)=>{events.push('publish-'+i.destination.countryCode);Object.assign(r,{publishId:i.destination.countryCode==='US'?'11':'22',phase:'publishing_verified',status:'accepted',identity:{allChildrenOnline:false}});await save();},
 copy:async(_page,i,r,save)=>{assert.equal(i.sourcePublication.publishId,'11');events.push('copy-original');Object.assign(r,{draftId:'33',phase:'copy_verified',sourceEvidence:{id:'11'},urls:{}});await save();},
 prepareUk:async(_page,i,r,save)=>{assert.equal(r.draftId,'33');events.push('prepare-UK');r.phase='uk_draft_verified';r.preparedInput={...i,store:'Exact UK',destination:{countryCode:'GB',draftId:'33',destinationId:'uk-target'}};await save();}
};}

test('dedicated US episode uses only original prepared US and never old recovery MAIN prepare copy or UK',async()=>{
 const i=input(),events=[];i.publicationMediaEpisodes=[{id:'episode-1'}];
 const r={usPrepare:{preparedInput:structuredClone(i),schemaObservation:{hash:'us-schema'}},
  usPublish:{publishId:'11',phase:'publication_failed'},ukCopy:{draftId:'33'},ukPrepare:{existing:'keep'},ukPublish:{publishId:'22',phase:'publication_failed'}};
 const oldUK=structuredClone({copy:r.ukCopy,prepare:r.ukPrepare,publish:r.ukPublish});
 const forbidden=async()=>assert.fail('separate original US scope only');
 const a={prepareUs:forbidden,prepareUk:forbidden,copy:forbidden,repairMainImage:forbidden,recoverMedia:forbidden,repairRequiredAttributes:forbidden,
  retryMediaEpisode:async(_p,prepared,publication,_save,options)=>{assert.deepEqual(prepared,r.usPrepare.preparedInput);assert.equal(publication.publishId,'11');assert.equal(options.amendment.id,'episode-1');assert.equal(options.observeOnly,true);assert.equal(options.schemaObservation.hash,'us-schema');events.push('episode-original-US');},
  publish:async(_p,prepared,publication)=>{assert.equal(prepared.destination.countryCode,'US');assert.equal(publication.publishId,'11');events.push('read-original-US');}};
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us_media_episode',mediaEpisodeObserveOnly:true,adapters:a});
 assert.deepEqual(events,['episode-original-US','read-original-US']);assert.deepEqual({copy:r.ukCopy,prepare:r.ukPrepare,publish:r.ukPublish},oldUK);
 assert.equal(r.phase,'us_media_episode_reconciled');
 await assert.rejects(()=>runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a}),/专用检查点/);
});

test('dedicated UK identifier correction uses original copy evidence and UK prepared input without US or another exemption',async()=>{
 const i=input(),r={},events=[],a=adapters(events);await runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a});events.length=0;
 i.publicationIdentifierRepair={id:'identifiers-1'};i.publicationMediaEpisodes=[{id:'old-US-keep'}];
 r.ukPrepare.brandExemption={id:'old-single-application'};r.ukPrepare.schemaObservation={hash:'GB-schema'};
 const originalUs=structuredClone(r.usPublish),copy=structuredClone(r.ukCopy),oldPrepare=structuredClone(r.ukPrepare);
 const forbidden=async()=>assert.fail('UK identifier scope cannot replay any other action');
 Object.assign(a,{copy:forbidden,prepareUs:forbidden,prepareUk:forbidden,repairMainImage:forbidden,recoverMedia:forbidden,retryMediaEpisode:forbidden,repairRequiredAttributes:forbidden});
 a.repairIdentifiers=async(_p,prepared,publication,_save,options)=>{assert.deepEqual(prepared,{...oldPrepare.preparedInput,urls:oldPrepare.urls});assert.equal(publication.publishId,'22');assert.deepEqual(options.sourceEvidence,copy.sourceEvidence);assert.equal(options.exemptionRecord.id,'old-single-application');assert.equal(options.schemaObservation.hash,'GB-schema');assert.equal(options.amendment.id,'identifiers-1');events.push('repair-original-UK');};
 a.publish=async(_p,prepared,publication)=>{assert.equal(prepared.destination.countryCode,'GB');assert.equal(publication.publishId,'22');events.push('read-original-UK');};
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'uk_identifiers',adapters:a});
 assert.deepEqual(events,['repair-original-UK','read-original-UK']);assert.deepEqual(r.usPublish,originalUs);assert.deepEqual(r.ukCopy,copy);assert.deepEqual(r.ukPrepare,oldPrepare);assert.equal(r.phase,'uk_identifiers_reconciled');
});

test('UK identifier failure or changed draft identity cannot fall through to publish or US recovery',async()=>{
 const i=input(),r={},events=[],a=adapters(events);await runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a});events.length=0;i.publicationIdentifierRepair={id:'identifiers-1'};
 a.repairIdentifiers=async()=>{events.push('preflight-UK');throw Error('original IDs differ');};a.publish=async()=>assert.fail('no publish after failed correction');
 await assert.rejects(()=>runPublicationWorkflow({},i,r,async()=>{},{phase:'uk_identifiers',adapters:a}),/original IDs differ/);assert.deepEqual(events,['preflight-UK']);
 r.ukPrepare.preparedInput.destination.draftId='foreign';events.length=0;
 await assert.rejects(()=>runPublicationWorkflow({},i,r,async()=>{},{phase:'uk_identifiers',adapters:a}),/冻结草稿/);assert.deepEqual(events,[]);
});

test('targeted original UK fact repair bypasses incompatible US observations and never copies or prepares another draft',async()=>{
 const i=input(),events=[];i.publicationRequiredAttributeRepair={id:'separate-required-amendment'};
 const p={...i,store:'Exact UK',destination:{countryCode:'GB',draftId:'33',destinationId:'uk-target'}};
 const r={usPublish:{publishId:'11',phase:'publication_failed'},usPrepare:{preparedInput:{productId:'historical-incompatible-us'}},
  ukCopy:{draftId:'33',phase:'copy_verified'},ukPrepare:{phase:'uk_draft_verified',draftId:'33',preparedInput:p,urls:{},schemaObservation:{hash:'actual-gb'}},ukPublish:{publishId:'22'}};
 const a={copy:async()=>assert.fail('copy'),prepareUs:async()=>assert.fail('US prepare'),prepareUk:async()=>assert.fail('UK prepare'),
  repairRequiredAttributes:async(_page,uk,pub,_save,options)=>{events.push('repair-original-UK');assert.equal(uk.store,'Exact UK');assert.equal(pub.publishId,'22');assert.equal(options.amendment.id,'separate-required-amendment');assert.equal(options.schemaObservation.hash,'actual-gb');},
  publish:async(_page,uk)=>{assert.equal(uk.destination.countryCode,'GB');events.push('read-original-UK');}};
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'uk_required_attributes',adapters:a});
 assert.deepEqual(events,['repair-original-UK','read-original-UK']);assert.equal(r.usPublish.phase,'publication_failed');assert.equal(r.phase,'uk_required_attributes_reconciled');
});

test('targeted fact repair failure or foreign original UK input cannot fall through to publication',async()=>{
 const i=input();i.publicationRequiredAttributeRepair={id:'required'};
 const p={...i,store:'Exact UK',destination:{countryCode:'GB',draftId:'33',destinationId:'uk-target'}},r={ukCopy:{draftId:'33',phase:'copy_verified'},ukPrepare:{phase:'uk_draft_verified',draftId:'33',preparedInput:p,urls:{}},ukPublish:{publishId:'22'}};
 let calls=0;const a={repairRequiredAttributes:async()=>{throw Error('schema blocked');},publish:async()=>calls++};
 await assert.rejects(()=>runPublicationWorkflow({},i,r,async()=>{},{phase:'uk_required_attributes',adapters:a}),/schema blocked/);assert.equal(calls,0);
 r.ukPrepare.preparedInput={...p,productId:'foreign'};await assert.rejects(()=>runPublicationWorkflow({},i,r,async()=>{},{phase:'uk_required_attributes',adapters:a}),/冻结草稿/);assert.equal(calls,0);
});

test('US phase stops for UK plugin session and UK phase retains separate receipts',async()=>{
 const i=input(),r={},events=[],saved=[];const a=adapters(events),persist=async()=>saved.push(structuredClone(r));
 await runPublicationWorkflow({},i,r,persist,{phase:'us',adapters:a});
 assert.deepEqual(events,['publish-US']);assert.equal(r.phase,'awaiting_uk_session');assert.equal(r.usPublish.publishId,'11');
 await runPublicationWorkflow({},i,r,persist,{phase:'uk',adapters:a});
 assert.deepEqual(events,['publish-US','copy-original','prepare-UK','publish-GB']);assert.equal(r.phase,'both_submitted');
 assert.equal(r.usPublish.publishId,'11');assert.equal(r.ukPublish.publishId,'22');assert.equal(r.ukCopy.draftId,'33');assert.equal(r.allOnline,false);assert.equal(r.jobId,'mock-job');assert(saved.length>5);
});

test('UK phase requires original US evidence and exact unchanged parent/child SKUs',async()=>{
 const i=input(),events=[],a=adapters(events);
 await assert.rejects(runPublicationWorkflow({},i,{},async()=>{},{phase:'uk',adapters:a}),/原美国/);assert.deepEqual(events,[]);
 const r={usPublish:{publishId:'11',phase:'publishing_verified'}};
 a.prepareUk=async(_p,i,r)=>{r.phase='uk_draft_verified';r.preparedInput={...i,store:'Exact UK',goods:{parentSku:'changed'},destination:{countryCode:'GB',draftId:'33',destinationId:'uk-target'}};};
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>{},{phase:'uk',adapters:a}),/SKU发生变化/);assert(!events.includes('publish-GB'));
});

test('receipt account/product/snapshot/job ownership fails before any adapter call',async()=>{
 const i=input(),events=[],a=adapters(events);
 for(const mismatch of [{accountId:3},{productId:'other'},{snapshotSha256:'other'},{jobId:'other'}])await assert.rejects(runPublicationWorkflow({},i,mismatch,async()=>{},{phase:'us',adapters:a}));
 assert.deepEqual(events,[]);
});

test('uncertain UK publication resumes original task without reopening consumed draft or recopying US',async()=>{
 const i=input(),events=[],r={usPublish:{publishId:'11',phase:'publishing_verified'},ukCopy:{draftId:'33',phase:'copy_verified'},
  ukPrepare:{draftId:'33',phase:'uk_draft_verified',urls:{},preparedInput:{...i,store:'Exact UK',destination:{countryCode:'GB',draftId:'33',destinationId:'uk-target'}}},
  ukPublish:{phase:'publish_unknown',intent:{draftId:'33',beforePublishIds:[]}}};
 const a=adapters(events);a.copy=async()=>assert.fail('recopy');a.prepareUk=async()=>assert.fail('reopen consumed draft');
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'uk',adapters:a});assert.deepEqual(events,['publish-GB']);assert.equal(r.phase,'both_submitted');
});

test('UK amendments survive the frozen US preparation while keeping its synced brand',async()=>{
 const i=input(),r={},events=[],a=adapters(events);
 a.prepareUs=async(_p,i,r)=>{r.phase='us_draft_verified';r.preparedInput=structuredClone(i);r.preparedInput.goods.brand='Synced Store Brand';};
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a});
 i.ukPreparation={shippingTemplate:{name:'Observed replacement'}};i.ukTarget.categoryHints={path:'Observed UK category'};
 const original=a.prepareUk;a.prepareUk=async(p,source,record,save)=>{assert.equal(source.goods.brand,'Synced Store Brand');assert.deepEqual(source.ukPreparation,i.ukPreparation);assert.deepEqual(source.ukTarget.categoryHints,i.ukTarget.categoryHints);return original(p,source,record,save);};
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'uk',adapters:a});assert.equal(r.phase,'both_submitted');
});

test('explicit media recovery uses the frozen US input before original-task verification',async()=>{
 const i=input(),r={},events=[],a=adapters(events);
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a});
 const frozen=structuredClone(r.usPrepare.preparedInput);
 i.publicationRecovery={id:'recovery-1',publishId:'11',scope:'retry_original_failed_media'};
 r.usPublish.phase='publication_failed';r.usPublish.status='failed';events.length=0;
 a.prepareUs=async()=>assert.fail('must not reopen consumed draft');
 a.recoverMedia=async(_p,prepared,record,save,{amendment})=>{
  assert.deepEqual(prepared,frozen);assert.equal(record.publishId,'11');assert.deepEqual(amendment,i.publicationRecovery);
  events.push('recover-original-US');record.mediaRecovery={amendmentId:amendment.id,requestAccepted:true};await save();
 };
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a});
 assert.deepEqual(events,['recover-original-US','publish-US']);assert.equal(r.phase,'awaiting_uk_session');
 assert.equal(r.usPublish.mediaRecovery.amendmentId,'recovery-1');assert.equal(r.usPublish.publishId,'11');
});

test('ordinary continuation has no media retry; failed verification stops before UK copy',async()=>{
 const i=input(),r={usPublish:{publishId:'11',phase:'publication_failed'}},events=[],a=adapters(events);
 a.recoverMedia=async()=>assert.fail('ordinary continue must be read-only');
 a.publish=async()=>{events.push('read-original-US');throw Error('original US still failed');};
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a}),/still failed/);
 assert.deepEqual(events,['read-original-US']);assert.equal(r.ukCopy.draftId,undefined);
});

test('failed recovery preflight cannot fall through to publication or UK copy',async()=>{
 const i={...input(),publicationRecovery:{id:'recovery-1'}},events=[],a=adapters(events);
 a.recoverMedia=async()=>{events.push('check-media');throw Error('media SHA changed');};
 const r={usPublish:{publishId:'11',phase:'publication_failed'}};
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a}),/media SHA changed/);
 assert.deepEqual(events,['check-media']);assert.equal(r.ukCopy.draftId,undefined);
});

test('explicit observation rereads both original IDs and ignores recovery authorization',async()=>{
 const i=input(),r={},events=[],a=adapters(events);
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a});events.length=0;
 i.publicationRecovery={id:'old-recovery'};i.publicationMainImageRepair={id:'main-repair'};
 a.repairMainImage=a.recoverMedia=a.copy=a.prepareUs=a.prepareUk=async()=>assert.fail('observation must not prepare, copy or recover');
 a.publish=async()=>assert.fail('observation must use the read-only entrypoint');
 a.observe=async(_p,prepared,record)=>{events.push([prepared.destination.countryCode,record.publishId]);record.latestObservation={allChildrenOnline:false};};
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'observe',adapters:a});
 assert.deepEqual(events,[['US','11'],['GB','22']]);assert.equal(r.phase,'both_submitted');
});

test('explicit MAIN repair runs on frozen original US input before prior recovery readback',async()=>{
 const i=input(),r={},events=[],a=adapters(events);
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a});events.length=0;
 const frozen=structuredClone(r.usPrepare.preparedInput);
 i.publicationMainImageRepair={id:'main-1',publishId:'11'};i.publicationRecovery={id:'old-media'};
 a.prepareUs=async()=>assert.fail('no consumed draft');
 a.repairMainImage=async(_p,published,record,save,{amendment})=>{assert.deepEqual(published,frozen);assert.equal(record.publishId,'11');assert.equal(amendment.id,'main-1');events.push('repair-MAIN');record.mainImageRepair={amendmentId:'main-1'};await save();};
 a.recoverMedia=async()=>events.push('read-old-media-intent');
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a});
 assert.deepEqual(events,['repair-MAIN','read-old-media-intent','publish-US']);assert.equal(r.usPublish.publishId,'11');
});

test('MAIN repair failure cannot fall through to old recovery, publish or UK copy',async()=>{
 const i={...input(),publicationMainImageRepair:{id:'main-1'},publicationRecovery:{id:'old-media'}},r={usPublish:{publishId:'11',phase:'publication_failed'}},events=[],a=adapters(events);
 a.repairMainImage=async()=>{events.push('check-original-MAIN');throw Error('source fields changed');};
 a.recoverMedia=async()=>assert.fail('no old retry');
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a}),/source fields changed/);
 assert.deepEqual(events,['check-original-MAIN']);assert.equal(r.ukCopy.draftId,undefined);
});

test('observation cannot submit a missing station and persists later US failure while still reading UK',async()=>{
 const i=input(),r={},events=[],a=adapters(events);
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a});events.length=0;
 const missing=structuredClone(r);delete missing.ukPublish.publishId;
 a.observe=async(_p,prepared,record)=>{events.push(prepared.destination.countryCode);if(prepared.destination.countryCode==='US'){
  record.latestObservation={failed:true};record.phase='publication_failed';throw Error('later US failed');
 }};
 await assert.rejects(runPublicationWorkflow({},i,missing,async()=>{},{phase:'observe',adapters:a}),/原美国及英国发布任务号/);
 assert.deepEqual(events,[]);
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>{},{phase:'observe',adapters:a}),/later US failed/);
 assert.deepEqual(events,['US','GB']);assert.equal(r.usPublish.latestObservation.failed,true);assert.equal(r.usPublish.publishId,'11');
});

test('observation reports partial evidence by station and still reads the other original task',async()=>{
 const i=input(),r={},events=[],a=adapters(events),saved=[];
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'all',adapters:a});events.length=0;r.allOnline=true;
 a.observe=async(_p,prepared,record,save)=>{
  events.push(prepared.destination.countryCode);
  if(prepared.destination.countryCode==='US'){
   record.onlineReadback=record.latestObservation={verification:'partial_online_readback',publishId:'11',recordId:'444',
    childrenWithAsin:2,childrenWithStatus:0,childCount:2,exactCoverUrl:false};await save();return;
  }
  record.latestObservation={verification:'failed',publishId:'22',failed:true};await save();throw Error('原任务仍有错误');
 };
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>saved.push(structuredClone(r)),{phase:'observe',adapters:a}),
  /美国原发布 11 已匹配 ERP 线上父体 444，2\/2 个子SKU有ASIN，0\/2 个有在售状态.*英国原发布 22 原任务仍有错误/);
 assert.deepEqual(events,['US','GB']);assert.equal(saved.at(-1).usPublish.onlineReadback.publishId,'11');
 assert.equal(saved.at(-1).ukPublish.latestObservation.failed,true);assert.equal(saved.at(-1).allOnline,false);
});

test('US-only jobs never copy to UK and can observe their original US task separately',async()=>{
 const i={...input(),publicationMarkets:['US']},r={},events=[],a=adapters(events);
 a.copy=async()=>assert.fail('single-market job must not copy');
 a.prepareUk=async()=>assert.fail('single-market job must not prepare a UK draft');
 a.observe=async(_p,prepared,publication,save)=>{
  assert.equal(prepared.destination.countryCode,'US');
  assert.equal(publication.publishId,'11');
  publication.latestObservation={allChildrenOnline:true,verification:'accepted'};
  events.push('observe-US');await save();
 };
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'us',adapters:a});
 assert.deepEqual(events,['publish-US']);
 assert.equal(r.phase,'us_submitted');
 await assert.rejects(runPublicationWorkflow({},i,r,async()=>{},{phase:'uk',adapters:a}),/仅美国/);
 await runPublicationWorkflow({},i,r,async()=>{},{phase:'observe',adapters:a});
 assert.deepEqual(events,['publish-US','observe-US']);
 assert.equal(r.allOnline,true);
});
