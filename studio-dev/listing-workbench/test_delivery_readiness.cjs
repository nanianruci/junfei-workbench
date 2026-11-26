const assert=require('node:assert/strict');
const fs=require('node:fs');
const test=require('node:test');
const ts=require('./visual-ui/node_modules/typescript');
const compile=name=>ts.transpileModule(fs.readFileSync(__dirname+'/visual-ui/'+name,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const helpers={};new Function('exports',compile('delivery-readiness.ts'))(helpers);
const {colourDeliveryProgress,newCollectionBlocked,masterDeliveryProgress}=helpers;
const group=key=>({key,color:key,ready:true,listingEligible:true});
const set=key=>({colorKey:key,status:'done',validated:true,skuBinding:{status:'linked'},items:Array.from({length:7},(_,slot)=>({slot,image:key+'-'+slot,status:'awaiting_manual',qc:{pass:true,issues:[]}}))});
const fixture=()=>({colors:['black','white','pink','blue','green'].map(group),masterColorKey:'black',masterReferenceMatches:true,galleryCount:7,sets:['white','pink','blue','green'].map(set),errors:[]});

test('four reviewing colours show 28 images without authorizing a new collection',()=>{
  const data=fixture();data.sets.forEach(record=>{record.status='reviewing';delete record.skuBinding;record.items.forEach(item=>{item.status='reviewing';delete item.qc;});});
  const progress=colourDeliveryProgress(data);
  assert.equal(progress.totalColors,4);assert.equal(progress.totalImages,28);assert.equal(progress.images,28);
  assert.equal(progress.checked,0);assert.equal(progress.linked,0);assert.equal(progress.ready,false);
  assert.ok(progress.rows.every(row=>row.state==='检查中'));assert.equal(newCollectionBlocked(progress.ready),true);
});

test('complete current colour sets pass only after full QC and automatic SKU binding',()=>{
  const data=fixture();assert.equal(colourDeliveryProgress(data).ready,true);
  data.sets[0].status='reviewing';assert.equal(colourDeliveryProgress(data).ready,false);
  data.sets[0].status='done';delete data.sets[0].skuBinding;
  const progress=colourDeliveryProgress(data);assert.equal(progress.checked,28);assert.equal(progress.linked,3);assert.equal(progress.ready,false);
});

test('missing, duplicate, failed and stale slots block even when counts or linked flags look complete',()=>{
  for(const change of [
    data=>data.sets.pop(),
    data=>data.sets.push(set('white')),
    data=>data.sets[0].items[6].slot=5,
    data=>data.sets[0].items[0].qc.pass=false,
    data=>data.sets[0].items[0].qc.issues.push('unreadable text'),
    data=>data.sets[0].validated=false,
    data=>data.masterReferenceMatches=false,
    data=>data.errors.push({error:'unreadable record'}),
    data=>data.colors[0].ready=false,
  ]){const data=fixture();change(data);assert.equal(colourDeliveryProgress(data).ready,false);}
  assert.equal(colourDeliveryProgress().ready,false);
});

test('excluded colours need no gallery and a verified single master needs no recolour',()=>{
  const data=fixture();data.colors.push({...group('excluded'),listingEligible:false,ready:false});
  assert.equal(colourDeliveryProgress(data).totalColors,4);assert.equal(colourDeliveryProgress(data).ready,true);
  data.colors=[group('black')];data.sets=[];
  assert.equal(colourDeliveryProgress(data).ready,true);assert.equal(colourDeliveryProgress(data).totalImages,0);
});

const selectedMaster=()=>{
  const selection={version:1,sourceStudioProjectId:'current-plan',items:Array.from({length:7},(_,i)=>({jobId:'job-'+i,imageIndex:0,sha256:String(i).repeat(64)}))};
  return {product:{id:'p',generatedGallerySelection:selection},summary:{productId:'p',selection:structuredClone(selection),imageCount:7,ready:true,reviewScope:'draft_only'}};
};

test('reviewed selected seven-image master is eligible without native run or set QA',()=>{
  const {product,summary}=selectedMaster(),progress=masterDeliveryProgress(product,{_projectId:'current-plan',images:[]},summary);
  assert.equal(progress.ready,true);assert.equal(progress.imagesReady,true);assert.equal(progress.setReady,true);
  assert.match(progress.imageLabel,/已选母版.*草稿审核/);
});

test('selected master never borrows native green QA and rejects old, changed or missing summary',()=>{
  const native={_projectId:'current-plan',images:Array.from({length:7},()=>({final_url:'native',render_qa:{ready:true}})),set_qa:{ready:true}};
  const {product,summary}=selectedMaster();assert.equal(masterDeliveryProgress({id:'p'},native).ready,true);
  assert.equal(masterDeliveryProgress(product,native).ready,false);
  for(const change of [
    view=>view.summary.ready=false,
    view=>view.summary.productId='other',
    view=>view.summary.selection.items.reverse(),
    view=>view.summary.selection.items[0].sha256='f'.repeat(64),
    view=>view.summary.imageCount=6,
    view=>view.summary.reviewScope='publish',
    view=>view.product.generatedGallerySelection.sourceStudioProjectId='old-plan',
    view=>view.product.generatedGallerySelection.items.pop(),
    view=>view.product.generatedGallerySelection.items[0].sha256=undefined,
  ]){const view=selectedMaster();change(view);assert.equal(masterDeliveryProgress(view.product,native,view.summary).ready,false);}
});

function erp({job,ready=false}){
  const states=[[{id:'us',name:'US',marketplace:'US'}],'us',job,false,false,'',job?[job]:[],[],''];let index=0;
  const exports={},requests=[],prepares=[],effects=[],verified=[];
  const onVerified=id=>verified.push(id);
  const react={useState:()=>{const i=index++;return [states[i],value=>{states[i]=typeof value==='function'?value(states[i]):value;}];},useRef:value=>({current:value}),useEffect:(run,deps)=>effects.push({run,deps})};
  const requireModule=name=>name==='react'?react:name==='./delivery-readiness'?helpers:name==='./HelpTip'?{default:()=>null}:require('./visual-ui/node_modules/'+name);
  new Function('require','exports',compile('ErpCollection.tsx'))(requireModule,exports);
  const previousFetch=global.fetch;
  let respond=async()=>({ok:true,json:async()=>({job:job||{id:'created',destinationId:'us'}})});
  global.fetch=async(url,options)=>{requests.push({url,options});return respond(url,options);};
  const tree=exports.default({productId:'ribbed',accountId:2,market:'US',copy:{title:'Saved title'},online:true,beforeStart:async()=>{prepares.push(true);},deliveryReady:ready,deliveryBlockReason:'变体检查中',onVerified});
  function find(node){if(!node||typeof node!=='object')return;if(node.props?.id==='wbCollectErp')return node;for(const child of [node.props?.children].flat(Infinity)){const result=find(child);if(result)return result;}}
  return {button:find(tree),requests,prepares,verified,states,pollEffect:effects.find(effect=>effect.deps[0]===job?.id&&effect.deps[2]===2),setResponse:value=>{respond=value;},notifyVerified:()=>effects.find(effect=>effect.deps.includes(onVerified)).run(),restore:()=>{global.fetch=previousFetch;}};
}

function pollingTimers(){
  const previousSet=global.setInterval,previousClear=global.clearInterval,callbacks=[],cleared=[];
  global.setInterval=(callback,delay)=>{assert.equal(delay,1500);callbacks.push(callback);return callbacks.length;};
  global.clearInterval=id=>cleared.push(id);
  return {callbacks,cleared,restore:()=>{global.setInterval=previousSet;global.clearInterval=previousClear;}};
}

test('an externally resumed original collection refreshes without submission or destination changes',async()=>{
  for(const status of ['needs_reconciliation','failed']){
    const original={id:'original',destinationId:'us',status,phase:'old checkpoint',error:'old error'};
    const view=erp({job:original}),timers=pollingTimers();let cleanup;
    try{
      view.states[4]=true;view.states[5]='user action error';
      cleanup=view.pollEffect.run();assert.equal(timers.callbacks.length,1);
      const resumed={...original,status:'running',phase:'fresh checkpoint',error:undefined};
      view.setResponse(async()=>({ok:true,json:async()=>({job:resumed})}));
      await timers.callbacks[0]();
      assert.equal(view.requests.length,1);assert.equal(view.requests[0].url,'/api/erp-collection/status?id=original');
      assert.equal(view.requests[0].options.method,undefined);assert.equal(view.requests[0].options.headers['X-Workspace-Account'],'2');
      assert.strictEqual(view.states[2],resumed);assert.strictEqual(view.states[6][0],resumed);
      assert.equal(view.states[1],'us');assert.equal(view.states[4],true);assert.equal(view.states[5],'user action error');
      assert.equal(view.prepares.length,0);
    }finally{cleanup?.();timers.restore();view.restore();}
  }
});

test('collection polling clears transient read failures and aborts stale product or account responses',async()=>{
  const original={id:'original',destinationId:'us',status:'needs_reconciliation'};
  const view=erp({job:original}),timers=pollingTimers();let cleanup;
  try{
    assert.deepEqual(view.pollEffect.deps,['original','needs_reconciliation',2,'ribbed']);
    cleanup=view.pollEffect.run();
    view.setResponse(async()=>{throw Error('temporary read error');});await timers.callbacks[0]();
    assert.equal(view.states[8],'temporary read error');
    view.setResponse(async()=>({ok:true,json:async()=>({job:original})}));await timers.callbacks[0]();assert.equal(view.states[8],'');
    let resolve;view.setResponse(()=>new Promise(done=>{resolve=done;}));
    const pending=timers.callbacks[0](),request=view.requests.at(-1);cleanup();cleanup=undefined;
    assert.equal(request.options.signal.aborted,true);assert.deepEqual(timers.cleared,[1]);
    resolve({ok:true,json:async()=>({job:{...original,status:'verified'}})});await pending;
    assert.strictEqual(view.states[2],original);assert.equal(view.prepares.length,0);
  }finally{cleanup?.();timers.restore();view.restore();}
});

test('collection polling stops for verified receipts and does not create missing jobs',()=>{
  for(const job of [undefined,{id:'original',destinationId:'us',status:'verified'}]){
    const view=erp({job}),timers=pollingTimers();
    try{assert.equal(view.pollEffect.run(),undefined);assert.equal(timers.callbacks.length,0);assert.equal(view.requests.length,0);assert.equal(view.prepares.length,0);}finally{timers.restore();view.restore();}
  }
});

test('new collection button and its handler both block incomplete variants without POST',async()=>{
  const view=erp({});try{assert.equal(view.button.props.disabled,true);await view.button.props.onClick();assert.equal(view.requests.length,0);assert.equal(view.prepares.length,0);}finally{view.restore();}
});

test('existing verified receipt stays readable with pending variants and refresh uses GET only',async()=>{
  const view=erp({job:{id:'original',destinationId:'us',status:'verified'}});
  try{assert.equal(view.button.props.disabled,false);await view.button.props.onClick();assert.equal(view.prepares.length,0);assert.equal(view.requests.length,1);assert.equal(view.requests[0].url,'/api/erp-collection/status?id=original');assert.equal(view.requests[0].options.method,undefined);}finally{view.restore();}
});

test('unknown task continuation remains available on its original identity',async()=>{
  const view=erp({job:{id:'original',destinationId:'us',status:'needs_reconciliation'}});
  try{assert.equal(view.button.props.disabled,false);await view.button.props.onClick();assert.equal(view.prepares.length,1);assert.equal(view.requests.length,1);assert.equal(view.requests[0].url,'/api/erp-collection/start');assert.equal(view.requests[0].options.method,'POST');assert.equal(JSON.parse(view.requests[0].options.body).destinationId,'us');}finally{view.restore();}
});

test('complete new collection remains available and running original tasks cannot be resubmitted',async()=>{
  const ready=erp({ready:true});try{assert.equal(ready.button.props.disabled,false);await ready.button.props.onClick();assert.equal(ready.requests.length,1);}finally{ready.restore();}
  const running=erp({job:{id:'original',destinationId:'us',status:'running'}});try{assert.equal(running.button.props.disabled,true);await running.button.props.onClick();assert.equal(running.requests.length,0);}finally{running.restore();}
});

test('only a verified ERP job notifies the publication readiness dependency',()=>{
  for(const status of ['running','needs_reconciliation','verified']){
    const view=erp({job:{id:'original-collection',destinationId:'us',status}});
    try{view.notifyVerified();assert.deepEqual(view.verified,status==='verified'?['original-collection']:[]);assert.equal(view.requests.length,0);}finally{view.restore();}
  }
});

test('ERP verification refreshes publication readiness with GET while preserving unknown original job and edits',async()=>{
  const originalJob={id:'original-publication',destinationId:'us',status:'needs_reconciliation',phase:'retain original intent'};
  const oldView={targets:[{id:'us',store:'US',target:{storeName:'UK',shippingTemplate:{name:'Saved template'}},ready:false,missing:['source not verified']}],jobs:[originalJob],commerce:{price:'8.19'},skuCount:30};
  const states=[oldView,'us','original error',true,'unsaved UK store','unsaved shipping','brand','9.99','88','3',false,true];
  const effects=[],requests=[];let index=0;
  const react={useState:()=>{const i=index++;return [states[i],value=>{states[i]=typeof value==='function'?value(states[i]):value;}];},useRef:value=>({current:value}),useEffect:(run,deps)=>effects.push({run,deps})};
  const exports={};new Function('require','exports',compile('PublicationFlow.tsx'))(name=>name==='react'?react:name==='./HelpTip'?{default:()=>null}:require('./visual-ui/node_modules/'+name),exports);
  const previousFetch=global.fetch;
  global.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({...oldView,targets:[{...oldView.targets[0],ready:true,missing:[]}],jobs:[]})};};
  try{
    exports.default({productId:'ribbed',accountId:2,online:true,beforeStart:()=>{throw Error('Unexpected save');},saveCommerce:()=>{throw Error('Unexpected mutation');},sourceReadyKey:'verified-collection'});
    const cleanup=effects.find(effect=>effect.deps.includes('verified-collection')).run();
    await new Promise(setImmediate);
    assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/publication/view?productId=ribbed');
    assert.equal(requests[0].options.method,undefined);assert.equal(requests[0].options.headers['X-Workspace-Account'],'2');
    assert.equal(states[0].targets[0].ready,true);assert.strictEqual(states[0].jobs[0],originalJob);
    assert.strictEqual(states[0].commerce,oldView.commerce);assert.equal(states[3],true);
    assert.equal(states[4],'unsaved UK store');assert.equal(states[5],'unsaved shipping');assert.equal(states[7],'9.99');assert.equal(states[11],true);
    assert.equal(states[2],'original error');cleanup();
  }finally{global.fetch=previousFetch;}
});
