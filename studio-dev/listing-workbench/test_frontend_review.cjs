const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync(__dirname+'/enhancements.js','utf8');
const context={};vm.createContext(context);
for(const name of ['keywordVolumeTier','listingGenerationProduct'])vm.runInContext(script.split('\n').find(l=>l.startsWith('function '+name+'(')),context);
const metrics={asin:'B000000001',searches:600,purchaseRate:0.25,purchases:150,trafficPercentage:12,month:'202609'};
const keywords=Array.from({length:50},(_,i)=>({phrase:'word '+i,selected:true,metrics:[{...metrics,purchaseRate:i/100}]}));keywords.push({phrase:'excluded',selected:false,metrics:[metrics]});
const result=context.listingGenerationProduct({name:'jacket',competitorResearch:{US:{keywords}}},'US').competitorResearch.US.keywords;
assert.equal(result.length,45);assert.equal(result[0].phrase,'word 49');assert.equal(result[0].metrics[0].purchaseRate,0.49);assert.equal(result[0].metrics[0].month,'202609');assert.equal(context.keywordVolumeTier(500),0);assert.equal(context.keywordVolumeTier(8000),0);assert.equal(context.keywordVolumeTier(499),1);assert.equal(context.keywordVolumeTier(null),2);
const index=fs.readFileSync(__dirname+'/index.html','utf8');const elements={rows:{},count:{}};
const c={state:{products:[{id:'source-id',sku:'ABC20261028120000',name:'jacket',cat:'服装',colors:1,sizes:1,market:'UK'}]},filter:'全部',query:'abc20261028120000',$:s=>elements[s.slice(1)],esc:s=>s||'',art:()=>'',document:{querySelectorAll:()=>[]}};vm.createContext(c);vm.runInContext(index.split('\n').find(l=>l.startsWith('function productIdentity(')),c);vm.runInContext(index.split('\n').find(l=>l.startsWith('function drawRows()')),c);c.drawRows();assert.match(elements.rows.innerHTML,/jacket/);assert.match(elements.rows.innerHTML,/>UK</);assert.doesNotMatch(elements.rows.innerHTML,/>US</);
const status={id:'serverStatus'};const header={items:[status],replaceChildren(){this.items=[]},append(x){this.items.push(x)}};const a={localWorkbenchMode:false,signedUser:{name:'Test',role:'staff'},document:{querySelector:()=>header,getElementById:()=>status,createElement:()=>({})},showAccountPanel(){}};vm.createContext(a);const auth=fs.readFileSync(__dirname+'/auth.js','utf8');const authStart=auth.indexOf('function accountHeader()'),authEnd=auth.indexOf('\n}',authStart)+2;vm.runInContext(auth.slice(authStart,authEnd),a);a.accountHeader();assert.equal(header.items[0],status);
console.log('PASS: SKU search, marketplace, conversion metrics and ranking, save status');
const agentScript=fs.readFileSync(__dirname+'/agent-center.js','utf8');
const agentContext={esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};vm.createContext(agentContext);
const observationStart=agentScript.indexOf('function agentSelectionObservationPanel('),observationEnd=agentScript.indexOf('\nfunction agentObservedCandidatePanel',observationStart);
vm.runInContext(agentScript.slice(observationStart,observationEnd),agentContext);
const panel=agentContext.agentSelectionObservationPanel([{captureType:'browser_1688_image_search',sourcingSearch:{offers:[
 {title:'other cotton bag',url:'https://detail.1688.com/offer/2.html'},
 {title:'45mm silicone door seal',url:'https://detail.1688.com/offer/1.html',priceText:'¥5'}]}}],'45mm silicone door seal');
assert.ok(panel.indexOf('<strong>1. 45mm silicone door seal')<panel.indexOf('<strong>2. other cotton bag'));
assert.match(panel,/未核验|不证明同款/);assert.match(panel,/¥5/);
console.log('PASS: 1688 image-search observations rank by title overlap and retain unverified status');
const rankStart=agentScript.indexOf('function agentSelectionRankSuggestionPanel('),rankEnd=agentScript.indexOf('\nasync function agentShowDetail',rankStart);
vm.runInContext(agentScript.slice(rankStart,rankEnd),agentContext);
const rankPanel=agentContext.agentSelectionRankSuggestionPanel({_observations:{sellerSpriteRows:[
 {asin:'B087654321',raw:'BSR: 12,345 & $8.99 · Listing date: 2026-10-29',rankSuggestions:[12345],productListedDateSuggestions:['2026-10-29'],url:'https://www.sellersprite.com/search'},
 {asin:'B000000001',raw:'rank suggestion',rankSuggestions:[999],url:'https://evil.example'}]}});
assert.match(rankPanel,/ASIN B087654321/);assert.match(rankPanel,/12,345/);assert.match(rankPanel,/2026-10-29/);assert.match(rankPanel,/打开SellerSprite原页/);assert.match(rankPanel,/&amp;/);assert.doesNotMatch(rankPanel,/evil\.example/);assert.match(rankPanel,/不会自动写入准入证据/);
console.log('PASS: SellerSprite rank/date suggestions stay unverified and only link to SellerSprite');
const start=script.indexOf('async function flushWorkspace('),end=script.indexOf('\nfunction showConflict',start);
const f={serverReady:true,syncBusy:false,syncPending:true,syncConflict:false,Date,setTimeout};f.pushState=async()=>{f.syncPending=false};vm.createContext(f);vm.runInContext(script.slice(start,end),f);
(async()=>{await f.flushWorkspace();assert.equal(f.syncPending,false);f.syncConflict=true;await assert.rejects(()=>f.flushWorkspace(),/冲突/);f.syncConflict=false;f.syncPending=true;f.pushState=async()=>{};await assert.rejects(()=>f.flushWorkspace(),/尚未保存/);console.log('PASS: export/logout wait for saves and block on conflict/failure')})().catch(e=>{console.error(e);process.exitCode=1});
