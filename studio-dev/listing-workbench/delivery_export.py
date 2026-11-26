"""Export the saved listing and current generated image sets."""
import io,json,zipfile,re,csv,hashlib
import studio_bridge
import product_sku
import colour_sets
import generated_gallery
import colour_master
import variant_colours
import image_budget
import image_composition
from ivyea_pipeline import required_size_chart
FIELDS=('title','highlights','bullets','description','terms')

def variant_rows(product, sku):
    refs=product.get('supplierColorReferences',[])
    observed=product.get('sourceImport',{}).get('amazon',{}).get('variants')
    if isinstance(observed,list):
        colors=[s.strip() for s in re.split(r'[,，、;；\n]',str(product.get('colorNames') or '')) if s.strip()]
        aliases={'Small':'S','Medium':'M','Large':'L','X-Large':'XL','XX-Large':'XXL','3X-Large':'3XL','4X-Large':'4XL','5X-Large':'5XL'}
        seen=set()
        for row in observed:
            color=str(row.get('color_name') or '').strip();size=str(row.get('size_name') or '').strip()
            asin=str(row.get('asin') or '').strip()
            if not color or not size:raise ValueError('已采集子体缺少颜色或尺码，不能导出虚构组合')
            if (color,size) in seen:raise ValueError('已采集子体颜色尺码重复，需先核对 SKU 映射')
            seen.add((color,size))
            if color not in colors:colors.append(color)
            size=aliases.get(size,size)
            child=row.get('sku') or sku+'-C'+str(colors.index(color)+1).zfill(2)+'-'+size
            key=variant_colours.colour_key(color)
            ref=next((r for r in refs if key in {variant_colours.colour_key(name) for name in
                (product.get('assetColorBindings',{}).get(r['url']),r.get('color'),r.get('english'))}),{})
            yield child,color,ref.get('english',color),size,ref.get('url',''),'observed_amazon_child; supplier availability unverified',asin
        return
    sizes=[s.strip() for s in re.split(r'[,，、;；\n]',str(product.get('sizeNames') or '')) if s.strip()]
    for n,ref in enumerate(refs,1):
        color=product.get('assetColorBindings',{}).get(ref['url']) or ref.get('color','')
        for size in sizes or ['']:
            yield sku+'-C'+str(n).zfill(2)+('-'+size if size else ''),color,ref.get('english',''),size,ref.get('url',''),'draft-combination; availability unverified',''

def export_variant_scope(product):
    """Keep historical observations separate from the backup's child SKU rows.

    Do not change variant_rows' compatibility contract: ERP also scopes its input
    before calling it. Explicit eligibility, including an empty list, wins over
    all observed colours; no SKU or colour/size combination is regenerated here.
    """
    source=product.get('sourceImport') or {};amazon=source.get('amazon') or {}
    observed=amazon.get('variants');eligible=amazon.get('listingEligibleVariants')
    if not isinstance(observed,list) or not isinstance(eligible,list):
        return product,[]
    asins={row.get('asin') for row in eligible if isinstance(row,dict) and row.get('asin')}
    kept=[row for row in observed if isinstance(row,dict) and row.get('asin') in asins]
    reasons={row.get('asin'):row.get('reason') for row in amazon.get('excludedVariants') or [] if isinstance(row,dict)}
    excluded=[{'sku':row.get('sku'),'sourceAsin':row.get('asin'),
               'color':row.get('color_name') or row.get('color'),
               'size':row.get('size_name') or row.get('size'),
               'reason':reasons.get(row.get('asin')) or 'not_in_listing_eligible_variants'}
              for row in observed if isinstance(row,dict) and row.get('asin') not in asins]
    return {**product,'sourceImport':{**source,'amazon':{**amazon,'variants':kept}}},excluded
def image_colour(product,plan):
    color=product.get('masterColor') or ''
    primary=plan.get('product_source_url') or product.get('imageReferenceSelection',{}).get('primary')
    if not color:
        color=product.get('assetColorBindings',{}).get(primary) or next(
            (a['color'] for a in product.get('assets',[]) if a.get('url')==primary and a.get('color')),'')
    if not color:
        colors=plan.get('product_profile',{}).get('product_colours',[])
        if len(colors)==1:
            known={'original_reference_olive_brown':'橄榄褐色','black':'黑色','white':'白色','brown':'褐色','blue':'蓝色','red':'红色','green':'绿色','grey':'灰色','gray':'灰色','navy':'藏蓝色'}
            color=known.get(str(colors[0]).lower(),'')
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',str(color)).strip(' .')[:60] or ''

def build(product,listing,view=None):
    view=view if view is not None else studio_bridge.get_view(product)
    public={k:listing.get(k,'') for k in FIELDS}
    if not any(public.values()):raise ValueError('该产品尚未保存文案')
    public['translations']=listing.get('translations',{})
    sku=product_sku.folder(product);prefix=sku+'/'
    public['sku']=sku
    buffer=io.BytesIO();manifest=[];missing=[];excluded=[]
    observed=isinstance(variant_colours.observed_children(product),list)
    groups=variant_colours.catalog(product)
    scoped_product,excluded_variants=export_variant_scope(product)
    child_rows=list(variant_rows(scoped_product,sku))
    master_key=variant_colours.resolve(groups,product.get('masterColor'))
    gallery=view.get('sets',{}).get('gallery',{})
    master_reference=gallery.get('product_source_url') or ((gallery.get('images') or [{}])[0].get('product_source_url'))
    master_matches=variant_colours.has_reference(groups.get(master_key),master_reference)
    def key_for(color):
        return variant_colours.resolve(groups,color) if observed else variant_colours.colour_key(color)
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        source_url = str(product.get('sourceUrl') or product.get('sourceImport', {}).get('supplier', {}).get('url') or '').strip()
        text = '\n\n'.join(k.upper()+'\n'+public[k] for k in FIELDS)
        text += '\n\n货源网址（内部资料，不用于刊登）\n' + (source_url or '未填写')
        z.writestr(prefix+sku+'-listing.txt', text)
        z.writestr(prefix+sku+'-listing.json',json.dumps(public,ensure_ascii=False,indent=2))
        z.writestr(prefix+'keyword-research.json',json.dumps(product.get('competitorResearch',{}),ensure_ascii=False,indent=2))
        z.writestr(prefix+'review-research.json',json.dumps(product.get('reviewResearch',{}),ensure_ascii=False,indent=2))
        keywords=io.StringIO();kw=csv.writer(keywords);kw.writerow(['market','phrase','selected','placement','monthlySearches','purchases','purchaseRate','month','source'])
        for market,research in product.get('competitorResearch',{}).items():
            for row in research.get('keywords',[]):
                for metric in row.get('metrics',[]) or [{}]:
                    kw.writerow([market,row.get('phrase',''),row.get('selected',False),row.get('placement',''),metric.get('searches',''),metric.get('purchases',''),metric.get('purchaseRate',''),metric.get('month',''),metric.get('source','')])
        z.writestr(prefix+'keywords.csv','\ufeff'+keywords.getvalue())
        counts={}
        current_master=view.get('sets',{}).get('gallery',{}).get('_projectId')
        import image_engine_delivery
        engine_galleries = image_engine_delivery.galleries(product)
        generated = generated_gallery.export_images(product,current_master) if engine_galleries is None else None
        for key, rows in (engine_galleries or {}).items():
            color = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', groups[key]['color']).strip(' .')[:60]
            for raw, info in rows:
                ext = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[info['mime']]
                name = prefix + color + str(info['slot'] + 1).zfill(2) + ext
                z.writestr(name, raw)
                counts[color] = info['slot'] + 1
                manifest.append({**info, 'file': name, 'color': color, 'colorKey': key, 'kind': 'gallery',
                                 'source': 'image_engine_approved', 'skuBindingReady': True,
                                 'draftBindingEligible': True, 'setId': 'engine-' + key})
        if generated is not None:
            color=image_colour(product,{})
            for raw,info in generated:
                image_composition.require(raw,slot=info['slot'],product=product,
                                          detail=bool(info['slot']<len(gallery.get('images',[])) and gallery['images'][info['slot']].get('shot_type')=='detail'),
                                          size_chart=bool(info['slot']==6 and len(gallery.get('images',[]))==7 and gallery['images'][6].get('size_chart')))
                number=info['slot']+1
                ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}[info['mime']]
                name=prefix+(color+str(number).zfill(2) if color else 'A'+str(number))+ext
                z.writestr(name,raw)
                counts[color]=number
                manifest.append({**info,'file':name,'color':color,'colorKey':master_key if observed else key_for(color),
                    'kind':'gallery','source':'completed_image_job_selection'})
        for kind,plan in view.get('sets',{}).items():
            if kind=='gallery' and (generated is not None or engine_galleries is not None):continue
            color=image_colour(product,plan)
            for index,image in enumerate(plan.get('images',[])):
                rid=image.get('_runId')
                if not rid or not image.get('final_url'):
                    missing.append(kind+'/'+str(index+1));continue
                if image.get('_status') not in ('approved','awaiting_manual') or image.get('render_qa',{}).get('ready') is False:
                    missing.append(kind+'/'+str(index+1))
                    excluded.append({'kind':kind,'slot':index,'runId':rid,'reason':'image_review_not_passed'})
                    continue
                raw,mime=studio_bridge.result_image(rid)
                if kind=='gallery':
                    image_composition.require(raw,slot=index,product=product,size_chart=image.get('size_chart') is True,detail=image.get('shot_type')=='detail')
                ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}[mime]
                if kind=='aplus':
                    name=prefix+'aplus/'+(color or 'Aplus')+str(index+1).zfill(2)+ext
                else:
                    counts[color]=max(counts.get(color,0)+1,index+1)
                    name=prefix+(color+str(counts[color]).zfill(2) if color else 'A'+str(counts[color]))+ext
                z.writestr(name,raw)
                manifest.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest(),'color':color,'colorKey':(master_key if master_matches else '') if observed else key_for(color),
                    'kind':kind,'slot':index,'runId':rid,'status':image.get('_status'),'humanReviewed':image.get('human_reviewed',False)})
        # A replacement storyboard must not silently export superseded variants.
        for variant in ([] if engine_galleries is not None else colour_sets.list_sets(product['id'])['sets']):
            if generated is not None and variant.get('masterId')!=colour_master.selection_id(product):
                excluded.append({'setId':variant['id'],'reason':'generated_gallery_selected_colour_set_not_rebound'})
                continue
            if generated is None and current_master and variant.get('masterId')!=current_master:
                continue
            color=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',str(variant.get('color') or '')).strip(' .')[:60]
            key=variant.get('colorKey') or key_for(variant.get('color'))
            group=groups.get(key)
            automatic=bool(master_matches and current_master) and colour_sets.binding_ready(variant,product)
            approved=variant.get('manual') is True and variant.get('status')=='approved'
            if observed:
                approved=approved and bool(master_matches and current_master and group and group['ready'] and key!=master_key and
                    variant.get('colorKey')==key and variant_colours.has_reference(group,variant.get('reference')))
            for item in sorted(variant.get('items',[]),key=lambda i:i['slot']):
                if not item.get('image'):
                    missing.append(variant['color']+'/'+str(item['slot']+1));continue
                if item.get('status') not in ('approved','awaiting_manual') or (item.get('qc') or {}).get('pass') is False:
                    missing.append(variant['color']+'/'+str(item['slot']+1))
                    excluded.append({'setId':variant['id'],'slot':item['slot'],'reason':'variant_review_not_passed'})
                    continue
                raw,mime=colour_sets.image_bytes(variant['id'],item['slot'])
                image_composition.require(raw,slot=item['slot'],product=product,
                                          detail=bool(item['slot']<len(gallery.get('images',[])) and gallery['images'][item['slot']].get('shot_type')=='detail'),
                                          size_chart=bool(item['slot']==6 and len(gallery.get('images',[]))==7 and gallery['images'][6].get('size_chart')))
                ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}[mime]
                counts[color]=max(counts.get(color,0)+1,item['slot']+1)
                name=prefix+(color+str(counts[color]).zfill(2) if color else 'A'+str(counts[color]))+ext
                z.writestr(name,raw)
                draft_eligible=bool(not observed or master_matches and current_master and group and group['ready'] and key!=master_key and variant.get('validated') is True and variant_colours.has_reference(group,variant.get('reference')))
                manifest.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest(),'color':variant.get('color'),'colorKey':key,'kind':'gallery','slot':item['slot'],'draftBindingEligible':draft_eligible,
                    'setId':variant['id'],'status':item['status'],'humanReviewed':bool(approved and item['status']=='approved'),
                    'skuBindingReady':bool(automatic),'skuBinding':variant.get('skuBinding') if automatic else None,
                    'qc':item.get('qc'),'referenceWarnings':variant.get('referenceWarnings',[])})
        bound={}
        if observed:
            count=image_budget.configuration().get('gallery_count',7)
            for key,group in groups.items():
                sets={}
                if group['ready']:
                    for image in manifest:
                        if image['kind']=='gallery' and image.get('colorKey')==key and (image['humanReviewed'] or image.get('skuBindingReady')):
                            sets.setdefault(image.get('setId'),[]).append(image)
                complete=[sorted(rows,key=lambda image:image['slot']) for rows in sets.values()
                          if sorted(image['slot'] for image in rows)==list(range(count))]
                if len(complete)==1:
                    bound[key]=complete[0]
        # Draft associations are separate from approved import fields. A model
        # warning does not silently turn into human approval or publish-ready.
        draft_bound={};count=image_budget.configuration().get('gallery_count',7)
        for key,group in groups.items():
            options={}
            if not group['ready']:continue
            for image in manifest:
                eligible=image.get('draftBindingEligible',image.get('colorKey')==master_key and master_matches)
                checked=(image.get('qc') or {}).get('pass') is True if image.get('setId') else True
                if eligible and checked and image['kind']=='gallery' and image.get('colorKey')==key:
                    options.setdefault(image.get('setId'),[]).append(image)
            complete=[sorted(rows,key=lambda row:row['slot']) for rows in options.values() if sorted(row['slot'] for row in rows)==list(range(count))]
            if len(complete)==1:draft_bound[key]=complete[0]
        # The selected original colour has no recolour skuBinding record. Reuse
        # its already validated complete master mapping in both CSVs, without
        # relabelling model checks or source selection as human approval.
        if observed and master_key in draft_bound:
            bound[master_key]=draft_bound[master_key]
            for image in bound[master_key]:
                image['bindingSource']={'mode':'current_master_gallery','masterId':current_master,
                                        'colorKey':master_key,'reference':master_reference}
        draft_csv=io.StringIO();draft_writer=csv.writer(draft_csv)
        draft_writer.writerow(['parent_sku','sku','color','size','source_asin','color_key','draft_main_image','draft_generated_images','image_count','review_scope','human_reviewed','warnings'])
        variants=io.StringIO();writer=csv.writer(variants)
        writer.writerow(['parent_sku','sku','color','color_en','size','source_image','status','main_image','generated_images','image_count','source_asin','color_key'])
        row_image_gaps=[]
        for child_sku,color,english,size,source,status,asin in child_rows:
            key=key_for(color)
            images=bound.get(key,[]) if observed else sorted(
                [image for image in manifest if image['colorKey']==key and image['kind']=='gallery'],key=lambda image:image['slot'])
            files=[image['file'] for image in images]
            main=next((image['file'] for image in images if image['slot']==0),'')
            if len(files)!=count or not main:
                row_image_gaps.append({'sku':child_sku,'colorKey':key,'imageCount':len(files),'expectedCount':count})
            if observed and isinstance(product.get('sourceImport',{}).get('amazon',{}).get('listingEligibleVariants'),list):
                status='listing_eligible_original_child; supplier_coverage_recorded' if groups.get(key,{}).get('ready') else 'listing_eligible_original_child; supplier_coverage_incomplete'
            writer.writerow([sku,child_sku,color,english,size,source,status,main,'|'.join(files),len(files),asin,key])
            draft_images=draft_bound.get(key,[])
            draft_files=[image['file'] for image in draft_images]
            warnings=[warning for image in draft_images for warning in (image.get('qc') or {}).get('warnings',[])]
            draft_writer.writerow([sku,child_sku,color,size,asin,key,draft_files[0] if draft_files else '', '|'.join(draft_files),len(draft_files),'draft_only',all(image.get('humanReviewed') for image in draft_images) if draft_images else False,' | '.join(dict.fromkeys(warnings))])
        z.writestr(prefix+'variants.csv','\ufeff'+variants.getvalue())
        z.writestr(prefix+'draft-variant-image-bindings.csv','\ufeff'+draft_csv.getvalue())
        review_issues=[]
        try:
            required_size_chart(product,gallery,(row[3] for row in child_rows))
        except ValueError as error:
            review_issues.append({'code':'required_apparel_size_chart','message':str(error)})
        if missing:review_issues.append({'code':'missing_images','items':missing})
        if row_image_gaps:review_issues.append({'code':'incomplete_child_image_bindings','items':row_image_gaps})
        if not child_rows:review_issues.append({'code':'no_listing_eligible_children'})
        if observed:
            for key,group in groups.items():
                if not group['ready']:review_issues.append({'code':'variant_evidence_incomplete','colorKey':key,'issues':group['issues']})
            if not master_matches:review_issues.append({'code':'master_reference_mismatch'})
        elif child_rows:
            review_issues.append({'code':'unverified_draft_combinations'})
        z.writestr(prefix+'image-manifest.json',json.dumps({'images':manifest,'missing':missing,'excluded':excluded,
            'excludedVariants':excluded_variants,'parentSku':sku,'parentSkuIsGroupingOnly':True,
            'childSkuCount':len(child_rows),'variantRowScope':('listing_eligible_original_children'
                if isinstance(product.get('sourceImport',{}).get('amazon',{}).get('listingEligibleVariants'),list)
                else 'original_observed_children') if observed else 'legacy_draft_combinations',
            'exportPurpose':'backup','reviewRequired':bool(review_issues),'reviewIssues':review_issues,
            'humanReviewed':bool(manifest) and all(image.get('humanReviewed') for image in manifest)},ensure_ascii=False,indent=2))
        z.writestr(prefix+'备份说明.txt','当前已保存文案、图片及子SKU配图的本地备份。两份CSV仅列当前纳入刊登范围的原子SKU；parent_sku为分组标识，不另计一条父SKU销售行。被排除的历史变体及原因保留在image-manifest.json的excludedVariants，不作为正式CSV行。主色沿用已核对母版图序，其他颜色保留真实自动关联或人工确认来源；自动检查不写成人工审核。reviewRequired只表示清单中记录的实际资料、缺图或配图缺口；轻微检查提醒仍保留。备份不改变工作台发布授权和业务检查。')
    return buffer.getvalue()
