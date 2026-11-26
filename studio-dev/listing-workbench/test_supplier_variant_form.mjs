import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';

const source=await fs.readFile(new URL('./enhancements.js',import.meta.url),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
function load(product={}){
 const controls={modal:{open:false,showModal(){this.open=true},close(){this.open=false}},factsForm:{values:{},elements:{purchasePriceUniformAcrossVariants:{checked:false},packageWeightUniformAcrossVariants:{checked:false}}},cancelFacts:{}};
 const events={saved:0,messages:[]};
 const context=vm.createContext({URL,structuredClone,market:'US',names:{},render(){},persist(){events.saved++},
  window:{addEventListener(){}},product:()=>product,state:{reviews:{}},view:'listing',
  toast:text=>events.messages.push(text),esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  document:{getElementById:id=>controls[id]},FormData:class{constructor(form){return new Map(Object.entries(form.values))}},
  fetch(){throw Error('No network calls allowed in form tests')}});
 vm.runInContext(source,context);
 context.render=()=>{};
 return {context,controls,events};
}
function product(){return {id:'p1',brand:'KIMEETOL',manufacturer:'KIMEETOL',colorNames:'Brown, Light Pink',sizeNames:'S,XL',sourceUrl:'https://detail.1688.com/offer/123.html',status:'待审核',sourceImport:{amazon:{variants:[{color_name:'Brown',size_name:'Small'},{color_name:'Light Pink',size_name:'X-Large'}]}}};}
function values(){return {__svEnabled:'1',__svColours:'Brown, Light Pink',__svSizes:'S, XL',__svSource:'https://detail.1688.com/offer/123.html',__svChecked:'2026-11-05T13:00:00+08:00',__svEvidence:'Supplier page captured with visible colour and size options',__svPairs:'',sourceUrl:'https://detail.1688.com/offer/123.html'};}

test('opening the form never pre-fills Amazon colours as supplier options or fabricates observation time',()=>{
 const {context}=load(),p=product();
 const v=context.supplierVariantFormValues(p);
 assert.equal(v.__svColours,'');assert.equal(v.__svSizes,'');assert.equal(v.__svChecked,'');
 assert.deepEqual(plain(context.supplierVariantPatch(p,v)),{});
});
test('manual option record is product-bound, time-normalised and never claims stock or human image approval',()=>{
 const p=product(),before=structuredClone(p),{context}=load();
 const result=context.supplierVariantPatch(p,values());
 assert.equal(result.supplierVariantCoverage.productId,'p1');
 assert.equal(result.supplierVariantCoverage.checkedAt,'2026-11-05T05:00:00.000Z');
 assert.equal(result.supplierVariantCoverage.humanReviewed,undefined);
 assert.equal(result.supplierVariantCoverage.stockVerified,undefined);
 assert.deepEqual(p,before);
});
test('evidence, timezone and exact current supplier are required before any mutation',()=>{
 const {context}=load(),p=product();
 for(const patch of [{__svEvidence:''},{__svChecked:'2026-11-05T13:00:00'},{__svSource:'https://detail.1688.com/offer/456.html'},{__svSource:'https://user:secret@detail.1688.com/offer/123.html'}]){
  assert.throws(()=>context.supplierVariantPatch(p,{...values(),...patch}));
 }
 assert.equal(p.supplierVariantCoverage,undefined);
});
test('changed observations retain prior evidence without carrying its hash onto new manual assertions',()=>{
 const {context}=load(),p=product();
 p.supplierVariantCoverage={productId:'p1',sourceUrl:p.sourceUrl,checkedAt:'2026-11-04T00:00:00Z',evidence:'old-record.json',evidenceSha256:'originalhash',supplierObservedColors:['Brown'],supplierObservedSizes:['S']};
 const old=structuredClone(p.supplierVariantCoverage);
 const update=context.supplierVariantPatch(p,values());
 assert.deepEqual(plain(update.supplierVariantCoverageHistory),[old]);
 assert.equal(update.supplierVariantCoverage.evidenceSha256,undefined);
 assert.deepEqual(p.supplierVariantCoverage,old);
});
test('existing concrete combinations cannot be erased to hide a sparse supplier table',()=>{
 const {context}=load(),p=product();
 p.supplierVariantCoverage={productId:'p1',sourceUrl:p.sourceUrl,checkedAt:'2026-11-05T05:00:00Z',evidence:'table.json',supplierObservedColors:['Brown','Light Pink'],supplierObservedSizes:['S','XL'],supplierObservedVariants:[{color:'Brown',size:'S'}]};
 assert.throws(()=>context.supplierVariantPatch(p,values()),/不能清空/);
 const updated=context.supplierVariantPatch(p,{...values(),__svPairs:'Brown | S\nLight Pink | XL'});
 assert.equal(updated.supplierVariantCoverage.supplierObservedVariants.length,2);
 assert.equal(p.supplierVariantCoverage.supplierObservedVariants.length,1);
});
test('unrelated edits preserve an unchanged partial observation instead of requiring invented fields',()=>{
 const {context}=load(),p=product();
 p.supplierVariantCoverage={productId:'p1',sourceUrl:p.sourceUrl,supplierObservedColors:['Brown']};
 assert.deepEqual(plain(context.supplierVariantPatch(p,context.supplierVariantFormValues(p))),{});
});
test('untrusted labels and evidence are escaped in the actual form HTML',()=>{
 const {context}=load(),p=product();p.sourceImport.amazon.variants[0].color_name='<img onerror=alert(1)>';
 p.supplierVariantCoverage={supplierObservedColors:['</textarea><script>alert(1)</script>'],evidence:'" autofocus onfocus="bad'};
 const html=context.supplierVariantFields(p);
 assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img onerror'));
 assert.ok(html.includes('&lt;script&gt;'));assert.ok(html.includes('&quot;'));
});
test('actual form submission rejects invalid price JSON before changing supplier observations',()=>{
 const p=product(),before=structuredClone(p),{context,controls,events}=load(p);
 context.editFacts();controls.factsForm.values={...p,...values(),junfeiVariantPrices:'{'};
 controls.factsForm.onsubmit({preventDefault(){},target:controls.factsForm});
 assert.deepEqual(p,before);assert.equal(events.saved,0);assert.equal(controls.modal.open,true);
});
test('actual form submission saves structured coverage once and strips helper inputs from product',()=>{
 const p=product(),{context,controls,events}=load(p);
 context.editFacts();controls.factsForm.values={brand:p.brand,manufacturer:p.manufacturer,colorNames:p.colorNames,sizeNames:p.sizeNames,...values(),junfeiVariantPrices:'{}'};
 controls.factsForm.onsubmit({preventDefault(){},target:controls.factsForm});
 assert.equal(events.saved,1);assert.equal(controls.modal.open,false);
 assert.equal(p.supplierVariantCoverage.productId,p.id);
 assert.equal(Object.keys(p).some(k=>k.startsWith('__sv')),false);
 assert.equal(p.sourceImport.amazon.variants.length,2);
});
