const beforeImportEnhance=enhance;
enhance=function(){beforeImportEnhance();if(view!=='products')return;const button=document.createElement('button');button.className='btn';button.textContent='ASIN / 货源导入';button.onclick=showLinkImport;document.querySelector('.titlebar .actions').prepend(button)};
function showLinkImport(){const modal=document.getElementById('modal');modal.style.width='750px';modal.innerHTML=`<div class="wb-title-label"><h2>ASIN / 货源导入</h2>${workbenchHelp('只填货源链接也可导入。1688 统一排队并复用已有资料，遇到验证时需处理原窗口再继续。其他网站可先保存链接，再手动补充资料和图片。已有产品可用“货源采集 · 更新本产品”。','ASIN / 货源导入')}</div><form id="linkForm"><label>站点<select id="importMarket"><option value="US">美国站 · US</option><option value="JP">日本站 · JP</option><option value="UK" selected>英国站 · UK</option><option value="DE">德国站 · DE</option><option value="FR">法国站 · FR</option><option value="IT">意大利站 · IT</option><option value="ES">西班牙站 · ES</option><option value="CA">加拿大站 · CA</option><option value="IN">印度站 · IN</option><option value="MX">墨西哥站 · MX</option></select></label><label>竞品 ASIN（流量研究参考，不作为产品属性）<input id="importAsin" pattern="[A-Za-z0-9]{10}" placeholder="可选，填写 10 位 ASIN"></label><label>货源网址（支持其他网站，与 ASIN 至少填一项）<input id="importSource" type="url" placeholder="https://供应商网站/商品页面"></label><button class="btn primary" id="fetchImport">读取并预览</button><button type="button" class="btn" id="closeImport">关闭</button></form><div id="importPreview"></div>`;modal.showModal();document.getElementById('closeImport').onclick=()=>{modal.close();modal.style.width=''};document.getElementById('linkForm').onsubmit=async e=>{e.preventDefault();const b=document.getElementById('fetchImport');b.disabled=true;b.textContent='正在读取…';try{const r=await fetch('/api/import/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asin:document.getElementById('importAsin').value,market:document.getElementById('importMarket').value,source:document.getElementById('importSource').value})});const d=await r.json();if(!r.ok)throw Error(d.error);const preview=document.getElementById('importPreview');preview.innerHTML=`<div class="notice" style="margin-top:20px">${d.warnings.map(esc).join('<br>')||'已读取公开信息。请核对来源与实际产品。'}<br>不自动导入竞品品牌或将竞品图片绑定为生产素材。</div>${supplierPreviewHtml(d.supplier)}${d.amazon.title?`<section><strong>亚马逊参考资料 · ${esc(d.market)}</strong><p>${esc(d.amazon.title)}</p><div style="display:flex;gap:8px;overflow:auto">${(d.amazon.referenceImages||[]).slice(0,8).map(url=>`<img src="${esc(url)}" style="width:90px;height:90px;object-fit:contain" alt="竞品参考图" referrerpolicy="no-referrer">`).join('')}</div></section>`:''}<label>产品名称（核对后可修改）<input id="importName" value="${esc(d.supplier.title||d.amazon.title)}" style="width:100%"></label><p style="max-height:140px;overflow:auto">${d.amazon.bullets.map(esc).join('<br>')}</p><label>品类<select id="importCat"><option>服装</option><option>日用百货</option></select></label><label>货源补充资料<textarea id="supplierNotes" style="width:100%;height:90px" placeholder="无法自动读取时，粘贴材质、尺寸、颜色尺码和包装内容；在产品资料中确认后才能生成"></textarea></label><button class="btn primary" id="confirmImport">创建待确认产品</button>`;document.getElementById('confirmImport').onclick=()=>{const name=document.getElementById('importName').value.trim();if(!name)return toast('请填写或确认产品名称');const id=d.asin?'IMP-'+d.market+'-'+d.asin:(d.supplier.url.match(/^https:\/\/detail\.1688\.com\/offer\/(\d+)\.html/) ? '1688-'+d.supplier.url.match(/offer\/(\d+)/)[1] : 'SRC-'+crypto.randomUUID());if(state.products.some(p=>p.id===id))return toast('该产品已导入，请在产品档案打开原产品');state.products.unshift({id,name,cat:document.getElementById('importCat').value,owner:'待分配',colors:1,sizes:1,status:'待补资料',color:'#a6b39a',market:d.market,sourceUrl:d.supplier.url,brand:'Generic',manufacturer:'Generic',sourceImport:{...d,supplierNotes:document.getElementById('supplierNotes').value,importedAt:new Date().toISOString()},features:'',material:'',dimensions:'',packageContents:''});applySupplierPreview(state.products[0],d.supplier);if(d.asin){state.products[0].competitorResearch={[d.market]:{market:d.market,input:d.asin,keywords:[],sources:[],structure:'',warnings:'待获取竞品关键词与评论'}}}selected=id;persist();modal.close();modal.style.width='';go('listing');toast(d.supplier.title?'产品已创建，货源资料已填入':d.amazon.title?'产品已创建，亚马逊资料已保存为竞品参考，请补充自有产品事实':'已创建待补资料产品，本次未采集到完整资料')};}catch(err){toast(err.message)}finally{b.disabled=false;b.textContent='读取并预览'}}}
const importFactsEnhance=enhance;
enhance=function(){importFactsEnhance();};

const sourceDisplayEnhance=enhance;
enhance=function(){sourceDisplayEnhance();};
const originalImportedDefaults=defaults;
defaults=function(p){return originalImportedDefaults(p)};

const supplierDetailEnhance=enhance;
enhance=function(){supplierDetailEnhance();};

const beforeSupplierApply=enhance;
enhance=function(){beforeSupplierApply();};

// Product deletion uses an archive so mistakes can be recovered.
const productRowsBeforeDelete=drawRows;
drawRows=function(){productRowsBeforeDelete();for(const edit of document.querySelectorAll('[data-edit]')){const id=edit.dataset.edit;const button=document.createElement('button');button.className='link';button.style.color='#b55060';button.textContent='删除';button.setAttribute('aria-label','删除产品 '+id);button.onclick=()=>showDeleteProduct(id);edit.after(button)}};
let productArchiveBusy = false;

function showDeleteProduct(id) {
 showArchiveProducts([id]);
}

function showArchiveProducts(ids) {
 const account = signedUser?.id;
 const requested = new Set(ids);
 const products = state.products.filter(product => requested.has(product.id));
 if (!account || !products.length || productArchiveBusy) return;

 const modal = document.getElementById('modal');
 modal.style.width = '560px';
 modal.innerHTML = `<h2>删除所选 ${products.length} 款产品</h2>
 <ul id="archiveProductList" style="max-height:240px;overflow:auto;margin:16px 0"></ul>
 <div class="notice">产品将移到“已删除产品”，可在那里恢复。此操作只管理本地产品档案。</div>
 <div class="actions"><button class="btn" id="cancelDelete">取消</button>
 <button class="btn primary" id="confirmDelete" style="background:#b55060;border-color:#b55060">移到已删除产品</button></div>`;
 const list = document.getElementById('archiveProductList');
 for (const product of products) {
  const item = document.createElement('li');
  const publication = publishedProductRecord(product);
  item.textContent = `${product.name} · ${product.sku || product.id}${publication ? ' · 已发布 ' + publication.market : ''}`;
  list.append(item);
 }
 const cancel = document.getElementById('cancelDelete');
 const confirm = document.getElementById('confirmDelete');
 cancel.onclick = () => modal.close();
 confirm.onclick = async () => {
  if (productArchiveBusy || !modal.open || signedUser?.id !== account) return;
  productArchiveBusy = true;
  cancel.disabled = confirm.disabled = true;
  updateProductTableControls();
  let changed = false;
  try {
   await flushWorkspace();
   if (signedUser?.id !== account || !modal.open) return;
   const current = state.products.filter(product => requested.has(product.id));
   if (current.length !== products.length) throw Error('选中的产品已变化，请重新勾选');
   const deletedAt = new Date().toISOString();
   const archived = current.map(product => ({product: structuredClone(product), deletedAt}));
   state.deletedProducts = [...(state.deletedProducts || []), ...archived];
   state.products = state.products.filter(product => !requested.has(product.id));
   if (requested.has(selected)) selected = state.products[0]?.id || '';
   changed = true;
   persist({invalidateReview: false});
   modal.close();
   render();
   await flushWorkspace();
   if (signedUser?.id === account) toast(`已删除 ${archived.length} 款，可在“已删除产品”恢复`);
  } catch (error) {
   if (signedUser?.id === account) toast((changed ? '删除尚未确认保存：' : '未删除产品：') + error.message);
  } finally {
   productArchiveBusy = false;
   cancel.disabled = confirm.disabled = false;
   updateProductTableControls();
  }
 };
 modal.showModal();
}
const beforeDeletedProducts=enhance;
enhance=function(){beforeDeletedProducts();if(view!=='products')return;const button=document.createElement('button');button.className='btn';button.textContent='已删除产品（'+(state.deletedProducts||[]).length+'）';button.onclick=showDeletedProducts;document.querySelector('.titlebar .actions').append(button)};
async function permanentlyDeleteProduct(entry,button){
 const id=entry.product.id;
 if(!(state.deletedProducts||[]).includes(entry))return;
 if(state.products.some(p=>p.id===id))return toast('相同父 SKU 的产品仍在产品档案中，请先处理重复产品');
 if(!confirm('彻底删除“'+entry.product.name+'”（'+id+'）？\n将永久移除该产品的回收记录、Listing 文案、图片方案和审核记录，无法恢复。\n已有生成任务和素材文件保留。'))return;
 button.disabled=true;
 let changed=false;
 try{
  await flushWorkspace();
  if(!(state.deletedProducts||[]).includes(entry)||state.products.some(p=>p.id===id))throw Error('产品状态已变化，请重新打开已删除产品');
  state.deletedProducts=state.deletedProducts.filter(x=>x.product.id!==id);
  for(const key of Object.keys(state.copy||{})){
   if(key.startsWith(id+'-')&&/^(?:[A-Z]{2}|image-\d+)$/.test(key.slice(id.length+1)))delete state.copy[key];
  }
  if(state.reviews)delete state.reviews[id];
  changed=true;
  persist({invalidateReview:false});
  document.getElementById('modal').close();render();
  await flushWorkspace();
  toast('产品已彻底删除，无法恢复');
 }catch(e){toast((changed?'彻底删除尚未确认保存，请勿刷新；可点击顶部保存状态重试：':'未执行彻底删除：')+e.message)}
 finally{button.disabled=false}
}

function publishedProductRecord(product) {
 if(typeof productCompletionRecord==='function')return productCompletionRecord(product);
 const observation = product.erpPublishObservation;
 const draft = product.erpProductionDraft;
 const children = observation?.children;
 if (draft?.published !== true || draft?.verifiedLive !== true ||
     observation?.allChildrenOnline !== true || !Array.isArray(children) ||
     !children.length || !children.every(child => child?.verifiedOnline === true)) return null;
 const market = observation.marketplace || draft.marketplace;
 if (!market || (draft.marketplace && market !== draft.marketplace)) return null;
 return {market, store: draft.store, parentAsin: observation.parentAsin || draft.parentAsin,
         taskId: draft.taskId, checkedAt: observation.checkedAt, count: children.length};
}

function showPublicationRecord(product, record) {
 const modal = document.getElementById('modal');
 modal.style.width = '520px';
 modal.innerHTML = '<h2>ERP 发布回执</h2><div id="publicationFacts" style="margin:16px 0"></div>' +
                   '<button class="btn" id="closePublicationRecord">关闭</button>';
 const facts = document.getElementById('publicationFacts');
 const checked = record.checkedAt ? new Date(record.checkedAt) : null;
 const fields = [
  ['产品', product.name], ['父 SKU', product.sku || product.id],
  ['站点', record.market], ['店铺', record.store], ['父 ASIN', record.parentAsin],
  ['刊登任务号', record.taskId], ['英国任务号',record.ukTaskId], ['交接状态',record.verifiedLive?'所有子SKU在售已核验':'ERP 已受理发布，待核验在售'],
  [record.verifiedLive?'核实在售':'已提交子SKU',record.count?`${record.count} 个子 SKU`:''],
  ['核实时间', checked && !Number.isNaN(checked.valueOf()) ? checked.toLocaleString('zh-CN') : ''],
 ];
 for (const [label, value] of fields) {
  if (!value) continue;
  const line = document.createElement('p');
  line.textContent = `${label}：${value}`;
  facts.append(line);
 }
 document.getElementById('closePublicationRecord').onclick = () => modal.close();
 modal.showModal();
}

function showDeletedProducts(){
 const modal=document.getElementById('modal');modal.style.width='720px';
 modal.innerHTML='<h2>已删除产品</h2><p class="sub" style="margin-top:10px">彻底删除后无法恢复产品、文案及审核记录；已有生成任务和素材文件保留。</p><div id="deletedList" style="max-height:420px;overflow:auto;margin:20px 0"></div><button class="btn" id="closeDeleted">关闭</button>';
 const list=document.getElementById('deletedList');
 if(!(state.deletedProducts||[]).length)list.textContent='暂无已删除产品';
 for(const entry of state.deletedProducts||[]){
  const row=document.createElement('div');row.className='reviewitem';
  const text=document.createElement('span');text.textContent=entry.product.name+' · '+entry.product.id;
  const publication=publishedProductRecord(entry.product);
  if(publication)text.textContent+=' · 已发布 '+publication.market;
  row.append(text);
  const actions=document.createElement('div');actions.className='actions';
  const restore=document.createElement('button');restore.className='btn';restore.textContent='恢复';
  restore.onclick=async()=>{if(state.products.some(p=>p.id===entry.product.id))return toast('相同父 SKU 已存在，请先处理重复产品');state.products.unshift(entry.product);state.deletedProducts=state.deletedProducts.filter(x=>x!==entry);selected=entry.product.id;persist({invalidateReview:false});modal.close();render();try{await flushWorkspace();toast('产品已恢复')}catch(e){toast('恢复尚未确认保存，请勿刷新；可点击顶部保存状态重试：'+e.message)}};
  const purge=document.createElement('button');purge.className='btn';purge.textContent='彻底删除';purge.style.color='#b55060';
  purge.setAttribute('aria-label','彻底删除产品 '+entry.product.id);purge.onclick=()=>permanentlyDeleteProduct(entry,purge);
  actions.append(restore,purge);row.append(actions);list.append(row);
 }
 document.getElementById('closeDeleted').onclick=()=>modal.close();modal.showModal();
}

const navigationBeforeEmptyProducts=go;
go=function(next){if(!state.products.length&&['listing','images','reviews'].includes(next)){toast('产品档案为空，请先创建或恢复产品');return}navigationBeforeEmptyProducts(next)};

const beforeIdentityFacts=enhance;
enhance=function(){beforeIdentityFacts();if(view!=='listing')return;const p=product(),box=document.querySelector('.grid .box');if(!box)return;const field=document.createElement('div');field.className='fact';field.textContent='品牌：'+(p.brand||'Generic')+' / 制造商：'+(p.manufacturer||'Generic');box.append(field)};

const collectedProductSummaryBefore=enhance;
enhance=function(){collectedProductSummaryBefore();};

// Prefer saved workspace photos, including those copied from the shared library.
const productIllustration=art;
function collectedProductImages(p){return [...new Set([...(p.sourceImport?.amazon?.referenceImages||[]),...(p.sourceImport?.supplier?.images||[])])].filter(url=>{try{const u=new URL(url);return u.protocol==='https:'&&['m.media-amazon.com','images-na.ssl-images-amazon.com','cbu01.alicdn.com'].includes(u.hostname)}catch{return false}})}
function productThumbnailImages(p){
 const reference=p.librarySourceId?p.editorAsinReference?.referenceImages?.[0]:'';
 const saved=(p.assets||[]).map(asset=>asset?.url)
  .filter(url=>typeof url==='string'&&/^\/uploads\/[a-f0-9]{32}\.(?:jpg|png|webp)$/.test(url));
 const cover=typeof reference==='string'&&/^https:\/\/(?:m\.media-amazon\.com|images-na\.ssl-images-amazon\.com)\/images\/I\//.test(reference)?[reference]:[];
 return [...new Set([...cover,...saved,...collectedProductImages(p)])];
}
art=function(p){const urls=productThumbnailImages(p);if(!urls.length)return productIllustration(p);return '<span class="collected-thumbnail"><img src="'+esc(urls[0])+'" alt="'+esc(p.name)+'" referrerpolicy="no-referrer" loading="lazy" data-product-photo="'+esc(p.id)+'" data-photo-index="0"><span class="photo-fallback" hidden>'+productIllustration(p)+'</span></span>'};
document.addEventListener('error',event=>{const img=event.target;if(!(img instanceof HTMLImageElement)||!img.hasAttribute('data-product-photo'))return;const p=state.products.find(p=>p.id===img.dataset.productPhoto);const urls=p?productThumbnailImages(p):[];const next=Number(img.dataset.photoIndex)+1;if(next<urls.length){img.dataset.photoIndex=String(next);img.src=urls[next]}else{img.hidden=true;img.nextElementSibling.hidden=false}},true);
function supplierPreviewHtml(s){if(s.captureId)watchSupplierPreview(s);const attrs=s.attributes||{},photos=s.images||[];return `<section id="supplier-import-preview" data-capture-id="${esc(s.captureId||'')}" style="margin:18px 0"><strong>货源采集：${s.title?'已读取 '+Object.keys(attrs).length+' 项属性、'+photos.length+' 张图片':'未获取完整资料'}</strong>${s.captureId?`<p data-capture-message>${s.captureStatus==='paused'?'采集队列已暂停，请在页面上方处理验证后继续。':'已加入统一串行队列，采集完成后自动显示资料。'}</p>`:''}${s.needsBrowser?`<p>该页面需要浏览器登录、验证或动态加载。<a class="btn" href="${esc(s.url)}" target="_blank" rel="noopener">打开货源页面 →</a></p><p class="sub">完成验证后点击已安装的商品采集插件，采集结果会自动保存到产品档案；随后刷新工作台。</p>`:''}<div style="white-space:pre-wrap;margin:12px 0">${Object.entries(attrs).map(([k,v])=>esc(k)+'：'+esc(v)).join('\n')}</div><div style="display:flex;gap:8px;overflow:auto">${photos.slice(0,8).map(url=>`<img src="${esc(url)}" referrerpolicy="no-referrer" style="width:90px;height:90px;object-fit:contain" alt="货源产品图">`).join('')}</div></section>`}
function applySupplierPreview(p,s){const a=s.attributes||{};p.style=a['款式']||s.title||'';p.material=a['材质成分']||a['材质']||a['面料']||'';p.dimensions=a['产品尺寸']||a['商品尺寸']||a['尺寸']||'';p.purchasePrice=a['采购价']||a['价格']||'';p.packageDimensions=a['包装尺寸']||a['包装规格']||'';p.productWeight=a['产品重量']||a['净重']||'';p.packageWeight=a['包装重量']||a['毛重']||'';p.packageContents=a['包装内容']||a['包装清单']||'';p.colorNames=a['颜色']||'';p.sizeNames=a['尺码']||'';p.features=Object.entries(a).filter(([k])=>!['颜色','尺码','采购价','价格','包装尺寸','包装规格','产品重量','净重','包装重量','毛重'].includes(k)).map(([k,v])=>k+'：'+v).join('；');for(const [key,field]of [['colors','colorNames'],['sizes','sizeNames']])p[key]=Math.max(1,new Set(p[field].split(/[,，、]/).map(x=>x.trim()).filter(Boolean)).size)}

function openSupplierForProduct(){
 const p=product();let value=p.sourceUrl||p.sourceImport?.supplier?.url||'';
 value=prompt('填写1688货源链接，采集后更新当前产品：'+p.name+'（'+p.id+'）',value);if(!value)return;
 try{const url=new URL(value);if(url.protocol!=='https:'||url.hostname!=='detail.1688.com'||!/^\/offer\/\d+\.html$/.test(url.pathname))throw Error('请填写1688商品详情链接');
 url.search='';url.hash='';
 captureSupplierForProduct(p,url.href);
 }catch(e){toast(e.message)}
}
function showSupplierQuotes(p){
 const supplier=p.sourceImport?.supplier||{},rows=supplier.variants||[],modal=document.getElementById('modal');
 const value=v=>esc(v===null||v===undefined?'未知':String(v));
 modal.style.width='1000px';modal.innerHTML=`<h2>货源规格与报价 · ${esc(p.name)}</h2><p>供应商页面参考，未核实结算价和包装毛重；不会自动修改采购成本、店铺库存或生产图片。</p><p>${esc(supplier.url||'')} · ${esc(supplier.retrievedAt||'采集时间未知')}</p><div style="max-height:60vh;overflow:auto"><table style="width:100%"><thead><tr><th>规格原文</th><th>参考价 CNY</th><th>供应商库存</th><th>件重尺 cm / g</th><th>核验提示</th></tr></thead><tbody>${rows.map(v=>{const pack=v.itemPackage;return `<tr><td>${esc(v.label)}</td><td>${value(v.displayPriceCny)}</td><td>${value(v.supplierStock)}</td><td>${pack?[pack.lengthCm,pack.widthCm,pack.heightCm].map(value).join(' × ')+' cm / '+value(pack.weightG)+' g':'未知'}</td><td>${esc(v.conflict||'参考价及净/毛重待核实')}<details><summary>报价原文</summary><pre>${esc(v.quoteRaw)}</pre></details></td></tr>`}).join('')}</tbody></table></div><div class="actions"><button class="btn" id="supplierQuotesClose">关闭</button><button class="btn" id="supplierQuotesDownload">下载货源证据 JSON</button></div>`;
 modal.showModal();document.getElementById('supplierQuotesClose').onclick=()=>{modal.close();modal.style.width=''};document.getElementById('supplierQuotesDownload').onclick=()=>download(p.id+'-supplier.json',JSON.stringify(supplier,null,2));
}
const supplierTargetEnhance=enhance;
enhance=function(){supplierTargetEnhance();if(view!=='listing')return;const actions=document.querySelector('.titlebar .actions');if(!actions||document.getElementById('captureCurrentSupplier'))return;const b=document.createElement('button');b.id='captureCurrentSupplier';b.className='btn';b.textContent='货源采集 · 更新本产品';b.onclick=openSupplierForProduct;actions.append(b)};

// Selected product table export. Selection is temporary and scoped to the login.
const productTableSelection=new Set();
let productTableAccount=null;
let productTableExportBusy=false;
function syncProductTableSelection(){
 const account=typeof signedUser==='undefined'?null:signedUser?.id;
 if(account!==productTableAccount){productTableSelection.clear();productTableAccount=account;}
 const existing=new Set(state.products.map(p=>p.id));
 for(const id of productTableSelection)if(!existing.has(id))productTableSelection.delete(id);
}
function updateProductTableControls(){
 const boxes=[...document.querySelectorAll('[data-product-table-check]')],all=document.getElementById('productTableSelectAll');
 if(all){all.disabled=!boxes.length;all.checked=!!boxes.length&&boxes.every(b=>b.checked);all.indeterminate=boxes.some(b=>b.checked)&&!all.checked;}
 const bar=document.getElementById('productTableActions');if(bar)bar.hidden=!productTableSelection.size;
 const count=document.getElementById('productTableSelectedCount');if(count)count.textContent='已选 '+productTableSelection.size+' 款';
 const button=document.getElementById('exportSelectedProductTable');if(button)button.disabled=productTableExportBusy||!productTableSelection.size;
 const archive=document.getElementById('archiveSelectedProducts');if(archive)archive.disabled=productArchiveBusy||!productTableSelection.size;
}
async function exportSelectedProductTable(button){
 if(productTableExportBusy)return;
 syncProductTableSelection();
 const account=productTableAccount,ids=state.products.filter(p=>productTableSelection.has(p.id)).map(p=>p.id);
 if(!account||!ids.length)return toast('请先登录并勾选要导出的产品');
 if(ids.length>200)return toast('每次最多导出200款产品');
 productTableExportBusy=true;button.disabled=true;button.textContent='正在整理图片与表格…';
 try{
  await flushWorkspace();
  if(signedUser?.id!==account)throw Error('登录账号已变化，请重新选择产品');
  const response=await fetch('/api/products/export-table',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
  if(!response.ok)throw Error((await response.json()).error||'导出失败');
  const blob=await response.blob();
  if(signedUser?.id!==account)throw Error('登录账号已变化，请重新选择产品');
  const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='产品表格-'+new Date().toISOString().slice(0,10)+'-'+ids.length+'款.xlsx';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  toast('已导出 '+ids.length+' 款，含产品图片、ASIN和亚马逊标题搜索链接');
 }catch(error){toast(error.message)}finally{productTableExportBusy=false;button.textContent='导出所选Excel表格';syncProductTableSelection();updateProductTableControls()}
}
const drawRowsBeforeProductTable=drawRows;
drawRows=function(){
 drawRowsBeforeProductTable();syncProductTableSelection();
 const rows=document.getElementById('rows');if(!rows)return;
 const table=rows.closest('table'),header=table?.querySelector('thead tr');if(!header)return;
 if(!document.getElementById('productTableSelectAll')){
  const cell=document.createElement('th'),all=document.createElement('input'),label=document.createElement('label'),caption=document.createElement('span');
  all.type='checkbox';all.id='productTableSelectAll';all.setAttribute('aria-label','全选当前筛选产品');
  cell.className='product-table-select-all';cell.scope='col';caption.textContent='全选';
  label.append(all,caption);cell.append(label);header.prepend(cell);
  all.onchange=()=>{for(const box of rows.querySelectorAll('[data-product-table-check]')){box.checked=all.checked;if(all.checked)productTableSelection.add(box.dataset.productTableCheck);else productTableSelection.delete(box.dataset.productTableCheck)}updateProductTableControls()};
 }
 for(const edit of rows.querySelectorAll('[data-edit]')){
  const id=edit.dataset.edit,row=edit.closest('tr'),cell=document.createElement('td'),check=document.createElement('input');
  const product=state.products.find(p=>p.id===id);
  cell.className='product-table-select';cell.setAttribute('role','cell');check.type='checkbox';check.dataset.productTableCheck=id;check.checked=productTableSelection.has(id);check.setAttribute('aria-label','选择产品 '+(product?.name||id));cell.append(check);row.prepend(cell);
  check.onchange=()=>{if(check.checked)productTableSelection.add(id);else productTableSelection.delete(id);updateProductTableControls()};
  const record=product&&publishedProductRecord(product),status=row.querySelector('.pill');
  if(record&&status){
   const badge=document.createElement('button');
   badge.type='button';badge.className='pill';badge.style.cssText='margin:4px 0 0;border:0;cursor:pointer';
   badge.textContent='发布记录 · '+record.market;
   badge.setAttribute('aria-label','查看 '+product.name+' 的 '+record.market+' 发布记录');
   badge.onclick=()=>showPublicationRecord(product,record);
   status.after(badge);
  }
 }
 const empty=rows.querySelector('.empty');if(empty)empty.colSpan=8;
 if(!document.getElementById('exportSelectedProductTable')){
  const bar=document.createElement('div');bar.id='productTableActions';bar.className='actions';bar.hidden=true;
  const count=document.createElement('span');count.id='productTableSelectedCount';count.setAttribute('role','status');
  const button=document.createElement('button');button.className='btn';button.id='exportSelectedProductTable';button.textContent='导出所选Excel表格';button.onclick=()=>exportSelectedProductTable(button);
  const archive=document.createElement('button');archive.className='btn';archive.id='archiveSelectedProducts';archive.textContent='批量删除';archive.style.color='#a93848';
  archive.onclick=()=>{syncProductTableSelection();showArchiveProducts([...productTableSelection]);};
  const clear=document.createElement('button');clear.className='btn';clear.textContent='取消选择';clear.onclick=()=>{productTableSelection.clear();for(const check of rows.querySelectorAll('[data-product-table-check]'))check.checked=false;updateProductTableControls();document.getElementById('productTableSelectAll')?.focus()};
  bar.append(count,button,archive,clear);table.closest('.tablewrap').before(bar);
 }
 updateProductTableControls();
};
