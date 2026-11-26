const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(__dirname+'/extension/collector.js','utf8');
const photo=(src,width=750)=>({src,currentSrc:src,naturalWidth:width,naturalHeight:1000,getClientRects:()=>[{}]});
const url=n=>'https://cbu01.alicdn.com/img/ibank/'+n+'.jpg';
function run(tag='V-DETAIL-Z',recommendations=[],lazy=false){
 const gallery=photo(url('gallery')),detail=photo(url('detail'));
 const nested={querySelectorAll:s=>s==='img'?[detail,photo(url('gallery')),photo('https://user:pass@cbu01.alicdn.com/img/ibank/private.jpg'),photo('https://cbu01.alicdn.com.evil.test/img/ibank/evil.jpg'),...recommendations]:[]};
 const detailRoot={querySelectorAll:s=>s==='img'?[detail]:s==='*'?[{tagName:'DIV',shadowRoot:nested}]:[]};
 const toolbox={querySelectorAll:s=>s==='img'?[photo(url('toolbox'))]:[]};
 let mounted=!lazy;
 const host={tagName:tag,shadowRoot:detailRoot,classList:{contains:()=>tag.startsWith('V-DETAIL-')},closest:()=>section};
 const section={scrollIntoView(){mounted=true;},querySelectorAll:()=>[host]};
 const heading={innerText:'商品详情',getClientRects:()=>[{}],closest:()=>section};
 const document={title:'透明PVC桌面垫商品测试标题 - 阿里巴巴',querySelectorAll:s=>s==='h1'?[heading]:s==='img'?[gallery]:s==='*'?[...(mounted?[host]:[]),{tagName:'CHANNEL-TOOLBOX',shadowRoot:toolbox}]:[]};
 const window={scrollX:0,scrollY:350,scrollTo(position){assert.equal(position.left,0);assert.equal(position.top,350);}};
 return vm.runInNewContext(source,{document,window,setTimeout,location:{href:'https://detail.1688.com/offer/952542058876.html'},getComputedStyle:()=>({visibility:'visible'}),URL,Date,Set});
}
test('capture includes nested supplier detail images while excluding toolbox and credential/foreign URLs',async()=>{
 const result=JSON.parse(JSON.stringify(await run()));assert.deepEqual(result.images,[url('gallery'),url('detail')]);assert.equal(result.imageObservations[1].origin,'supplier_detail');assert.equal(result.imageObservations[1].visuallyReviewed,false);assert.equal(result.imageObservations[1].width,750);
});

for(const tag of ['V-DETAIL-2','V-DETAIL-U','V-DETAIL-W'])test('capture reads the observed '+tag+' description component',async()=>{
 const result=JSON.parse(JSON.stringify(await run(tag)));assert.deepEqual(result.images,[url('gallery'),url('detail')]);
});

test('description recommendations for other offers are excluded while own-offer links remain',async()=>{
 const other={...photo(url('other-offer')),closest:()=>({href:'http://detail.1688.com/offer/1070234763802.html'})};
 const own={...photo(url('own-offer')),closest:()=>({href:'https://detail.1688.com/offer/952542058876.html'})};
 const result=JSON.parse(JSON.stringify(await run('V-DETAIL-2',[other,own])));
 assert.deepEqual(result.images,[url('gallery'),url('detail'),url('own-offer')]);
});

test('unknown shadow components do not become supplier descriptions',async()=>{
 const result=JSON.parse(JSON.stringify(await run('CHANNEL-TOOLBOX')));assert.deepEqual(result.images,[url('gallery')]);
});

test('capture loads lazy description content before collecting and restores the scroll position',async()=>{
 const result=JSON.parse(JSON.stringify(await run('V-DETAIL-U',[],true)));assert.deepEqual(result.images,[url('gallery'),url('detail')]);
});
