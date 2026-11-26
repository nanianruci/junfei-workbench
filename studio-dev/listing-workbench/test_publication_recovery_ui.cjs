const assert=require('node:assert/strict');
const fs=require('node:fs');
const test=require('node:test');
const ts=require('./visual-ui/node_modules/typescript');
const source=ts.transpileModule(fs.readFileSync(__dirname+'/visual-ui/PublicationFlow.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const failed=()=>({id:'original',destinationId:'us',status:'needs_reconciliation',phase:'原发布任务后续失败',usPublishId:'14991361',variantCount:30,
  usFailure:{publishId:'14991361',failedCount:3,totalCount:30,errorCodes:['300060','18320']},
  mediaRecovery:{supported:true,canRetry:true,authorized:false}});
function render(job,{online=true,busy=false}={}){
  const data={targets:[{id:'us',store:'US',target:{storeName:'UK'},ready:true,missing:[]}],jobs:[job],commerce:{},skuCount:30};
  const states=[data,'us','',busy,'US-UK','UK','template','brand','8.19','88','3',false,false],requests=[],prepares=[],saves=[];
  let index=0;const react={useState:()=>{const i=index++;return [states[i],value=>{states[i]=typeof value==='function'?value(states[i]):value;}];},useRef:value=>({current:value}),useEffect:()=>{}};
  const exports={};new Function('require','exports',source)(name=>name==='react'?react:name==='./HelpTip'?{default:()=>null}:require('./visual-ui/node_modules/'+name),exports);
  const previousFetch=global.fetch,previousWindow=global.window;
  global.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({job:{...job,status:'queued'}})};};
  global.window={confirmPublicationOperation:async(accountId,action,body)=>{
    const response=await global.fetch('/api/publication/'+action,{headers:{'X-Workspace-Account':String(accountId)},body:JSON.stringify(body)});
    return response.json();
  }};
  const tree=exports.default({productId:'ribbed',accountId:2,online,beforeStart:async()=>{prepares.push(true);},saveCommerce:async details=>{saves.push(details);}});
  function all(node){if(!node||typeof node!=='object')return [];return [node,...[node.props?.children].flat(Infinity).flatMap(all)];}
  function text(node){if(Array.isArray(node))return node.map(text).join('');if(node==null||typeof node==='boolean')return '';if(typeof node!=='object')return String(node);return text(node.props?.children);}
  const nodes=all(tree);
  return {requests,prepares,saves,states,text:text(tree),find:id=>nodes.find(node=>node.props?.id===id),
    primary:nodes.find(node=>node.type==='button'&&node.props.className==='pe-primary'),
    checkboxes:nodes.filter(node=>node.type==='input'&&node.props.type==='checkbox'),restore:()=>{global.fetch=previousFetch;global.window=previousWindow;}};
}
async function click(button){assert.ok(button);button.props.onClick();await new Promise(setImmediate);}

test('explicit media recovery sends only original job ID plus retry action',async()=>{
  const view=render(failed());try{
    await click(view.find('wbRetryUsMediaFailures'));
    assert.equal(view.prepares.length,1);assert.equal(view.saves.length,0);assert.equal(view.requests.length,1);
    assert.equal(view.requests[0].url,'/api/publication/continue');assert.equal(view.requests[0].options.headers['X-Workspace-Account'],'2');
    assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original',confirmed:true,retryMediaFailures:true});
  }finally{view.restore();}
});

test('ordinary continuation requires no new consent and creates no recovery action',async()=>{
  const view=render(failed());try{
    assert.equal(view.primary.props.disabled,false);await click(view.primary);
    assert.equal(view.requests.length,1);assert.equal(view.requests[0].url,'/api/publication/continue');
    assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original',confirmed:true});
  }finally{view.restore();}
});

test('media retry blocks old server, active jobs, offline state and unsupported failure scopes',async()=>{
  for(const mutate of [job=>delete job.mediaRecovery,job=>job.mediaRecovery.canRetry=false,job=>job.status='running']){
    const job=failed();mutate(job);const view=render(job);try{
      const button=view.find('wbRetryUsMediaFailures');assert.equal(button.props.disabled,true);await click(button);
      assert.equal(view.requests.length,0);assert.equal(view.prepares.length,0);
    }finally{view.restore();}
  }
  const view=render(failed(),{online:false});try{assert.equal(view.find('wbRetryUsMediaFailures').props.disabled,true);await click(view.find('wbRetryUsMediaFailures'));assert.equal(view.requests.length,0);}finally{view.restore();}
});

test('submitted original task observation is a separate read action without save or consent',async()=>{
  const job={...failed(),status:'submitted',usFailure:undefined,ukPublishId:'14991362',ukDraftId:'172678941',observeSupported:true,canObserveLatestReceipts:true};
  const view=render(job);try{
    const button=view.find('wbObservePublicationReceipts');assert.equal(button.props.disabled,false);assert.equal(view.primary,undefined);
    await click(button);assert.equal(view.prepares.length,0);assert.equal(view.saves.length,0);assert.equal(view.requests.length,1);
    assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original'});
    assert.equal(view.requests[0].url,'/api/publication/observe');
  }finally{view.restore();}
});

test('observation requires server capability and both known linked tasks',async()=>{
  for(const mutate of [job=>delete job.observeSupported,job=>job.canObserveLatestReceipts=false,job=>job.status='running']){
    const job={...failed(),ukPublishId:'14991362',observeSupported:true,canObserveLatestReceipts:true};mutate(job);
    const view=render(job);try{
      const button=view.find('wbObservePublicationReceipts');assert.equal(button.props.disabled,true);await click(button);
      assert.equal(view.requests.length,0);assert.equal(view.prepares.length,0);
    }finally{view.restore();}
  }
  const view=render(failed());try{assert.equal(view.find('wbObservePublicationReceipts'),undefined);}finally{view.restore();}
});

const mainFailure=()=>({...failed(),usFailure:{publishId:'14991361',failedCount:1,totalCount:30,errorCodes:['18320']},
  mediaRecovery:{supported:true,canRetry:false,authorized:true},mainImageRepair:{supported:true,canRepair:true,authorized:false}});

test('original main field repair is a separate explicit action and never sends media retry or observe flags',async()=>{
  const view=render(mainFailure());try{
    const button=view.find('wbRepairOriginalMainImage');assert.equal(button.props.disabled,false);await click(button);
    assert.equal(view.requests.length,1);assert.equal(view.requests[0].url,'/api/publication/continue');
    assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original',confirmed:true,repairMainImage:true});
    assert.equal(view.prepares.length,1);assert.equal(view.saves.length,0);
  }finally{view.restore();}
});

test('main field repair refuses old service or used authorization while ordinary continue preserves its checkpoint',async()=>{
  for(const mutate of [job=>delete job.mainImageRepair,job=>job.mainImageRepair.canRepair=false,
    job=>job.mainImageRepair.authorized=true,job=>job.status='running']){
    const job=mainFailure();mutate(job);const view=render(job);try{
      const button=view.find('wbRepairOriginalMainImage');assert.equal(button.props.disabled,true);await click(button);
      assert.equal(view.requests.length,0);assert.equal(view.prepares.length,0);
    }finally{view.restore();}
  }
  const job=mainFailure();job.mainImageRepair={supported:true,canRepair:false,authorized:true,checkpoint:'原主图保存结果待核对'};
  const view=render(job);try{
    await click(view.primary);assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original',confirmed:true});
    assert.equal(view.find('wbRetryUsMediaFailures').props.disabled,true);
  }finally{view.restore();}
});

const unknownMainSave=()=>({...mainFailure(),mainImageRepair:{supported:true,canRepair:false,authorized:true,
  saveIntended:true,saveReceiptUnknown:true,canCorrectSave:true,authorizedCorrectiveSave:false}});

test('one explicit idempotent main resave sends its own action and plain continue does not authorize it',async()=>{
  const view=render(unknownMainSave());try{
    const button=view.find('wbRetryOriginalMainImageSave');assert.equal(button.props.disabled,false);await click(button);
    assert.equal(view.requests.length,1);assert.equal(view.requests[0].url,'/api/publication/continue');
    assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original',confirmed:true,retryMainImageSave:true});
  }finally{view.restore();}
  const ordinary=render(unknownMainSave());try{
    await click(ordinary.primary);assert.deepEqual(JSON.parse(ordinary.requests[0].options.body),{id:'original',confirmed:true});
  }finally{ordinary.restore();}
});

test('corrective save cannot be invoked without capability or after its authorization was frozen',async()=>{
  for(const mutate of [job=>delete job.mainImageRepair.canCorrectSave,job=>job.mainImageRepair.authorizedCorrectiveSave=true,
    job=>job.status='running']){
    const job=unknownMainSave();mutate(job);const view=render(job);try{
      const button=view.find('wbRetryOriginalMainImageSave');assert.equal(button.props.disabled,true);await click(button);
      assert.equal(view.requests.length,0);assert.equal(view.prepares.length,0);
    }finally{view.restore();}
  }
});

test('verified corrective save removes stale unknown-save instructions and continues the original UK checkpoint',async()=>{
  for(const uk of [false,true]){
    const job={...unknownMainSave(),usFailure:undefined,stage:uk?'uk_preparation':'us_publication',ukDraftId:uk?'172692703':undefined};
    job.mainImageRepair={...job.mainImageRepair,saveReceiptUnknown:false,canCorrectSave:false,
      authorizedCorrectiveSave:true,phase:'retry_request_accepted'};
    const view=render(job);try{
      assert.equal(view.find('wbRetryOriginalMainImageSave'),undefined);
      assert.doesNotMatch(view.text,/旧保存回执仍未知|最近回读仍为原资料|继续原主图重新保存检查点/);
      assert.match(view.text,/原主图字段已保存并核对/);
      assert.equal(view.primary.props.children,uk?'核对原英国草稿并继续':'核对原回执并继续');
      await click(view.primary);assert.deepEqual(JSON.parse(view.requests[0].options.body),{id:'original',confirmed:true});
    }finally{view.restore();}
  }
});
