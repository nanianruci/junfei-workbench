"""Explicit generative colour variants of a checked artwork; never pixel-only recolour."""
import copy,hashlib,json,re
VERSION='generated-colour-variant-v1'
REQUIRED=('identityPreserved','sameProductVariant','targetColourMatched','materialPreserved',
          'allGarmentInstancesRecoloured','protectedContentPreserved','sourceLayoutPreserved','textCorrect')

def freeze(config,recipe,product_id,get_task,asset,bind_template):
    if not isinstance(config,dict) or set(config)-{'sourceTaskId','sourceResultIndex','targetColour'}:
        raise ValueError('生成颜色变体配置无效')
    if recipe['mode']!='repair' or recipe.get('preserveSourcePixels') or recipe.get('repairTemplateId') or recipe.get('repairRequestedOptimize'):
        raise ValueError('生成颜色变体仅适用于明确的生成式编辑，不能冒充原像素换色或混用修图模板')
    if len(recipe['references'])!=2:raise ValueError('颜色变体需要原母版和一张真实同款目标照片')
    label=config.get('targetColour','')
    if not isinstance(label, str) or not 1 <= len(label) <= 60 or not label.isprintable() or not any(c.isalpha() for c in label):
        raise ValueError('目标颜色名称无效')
    source_variant=recipe.get('sourceVariant')
    if (not source_variant or source_variant.get('color')!=label
            or not source_variant.get('children')):
        raise ValueError('目标颜色必须与当前商品中已核对的真实颜色及子SKU完全一致')
    index=config.get('sourceResultIndex')
    if type(index)is not int or index<0:raise ValueError('缺少原母版图位')
    parent=get_task(config.get('sourceTaskId'))
    if parent.get('productId')!=product_id or parent.get('status')!='completed':raise ValueError('原母版必须是当前账号同款已完成的任务，未知或失败任务不可采用')
    result=next((r for r in parent['results']if r.get('index')==index),None)
    if not result or result.get('review',{}).get('blocking')is not False or result.get('review',{}).get('identityPreserved')is not True:
        raise ValueError('母版图没有明确通过独立质检')
    if result.get('shot',{}).get('apparelKind')=='size' or result.get('productionMethod')=='verified-size-data-typesetting':
        raise ValueError('共用尺码图不允许生成颜色变体')
    source_sha=asset(result['image'])[1]
    if source_sha!=recipe['referenceSha256'][0]:raise ValueError('上传母版与原任务图像哈希不一致')
    if recipe['referenceSha256'][0]==recipe['referenceSha256'][1]:raise ValueError('目标照片不能用母版代替')
    old_template=result.get('shot',{}).get('officialTemplate')
    template=bind_template(old_template['id']) if old_template else None
    if template and any(template.get(k)!=old_template.get(k)for k in ('id','sha256','promptSha256')):
        raise ValueError('母版所用原模板配图或原文已改变')
    copy_strings=copy.deepcopy(result.get('shot',{}).get('copy',[]))
    if not isinstance(copy_strings,list)or any(not isinstance(v,str)for v in copy_strings):raise ValueError('原母版文字记录无效')
    source_label = (parent.get('request', {}).get('sourceVariant') or {}).get('color')
    pattern = re.escape(source_label) if source_label else r'PALE PINK'
    copy_strings = [re.sub(r'(?<!\w)' + pattern + r'(?!\w)', lambda _: label.upper(), value, flags=re.I)
                    for value in copy_strings]
    return {'version':VERSION,'sourceTaskId':parent['id'],'sourceResultIndex':index,
      'sourceSha256':source_sha,'targetReferenceSha256':recipe['referenceSha256'][1],
      'targetColour':label,'slotKind':result.get('shot',{}).get('apparelKind'),
      'expectedCopy':copy_strings,'template':template,'manualSelectionProvided':False,
      'productionMethod':'codex-generated-colour-variant','preservesOutsidePixels':False,
      'referenceRoles':['checked_master_artwork','actual_same_product_target_colour']+(['original_template_layout_only']if template else[])}

def references(recipe):
    v=recipe['generatedColourVariant'];return list(recipe['references'])+([v['template']['url']]if v.get('template')else[])

def compile_prompt(recipe):
    v=recipe['generatedColourVariant'];t=v.get('template')
    prompt=('Create ONE native square finished colour variant of REFERENCE 1, the checked master artwork. '
      'REFERENCE 2 is the ACTUAL SAME-PRODUCT TARGET COLOUR PHOTOGRAPH, not a generic colour name. '
      'Change only the featured garment to the actual target shade and supported material response. '
      'Apply it to EVERY depiction of this garment, including all wearers, insets and detail views. '
      'Keep the original seams, cut, fit, fabric structure and textile highlights. Match the actual '
      'material and hardware from reference 2 without adding any garment features. '
      'Keep original people, faces, skin, hair, hands, separate clothes, gaps, background, pose, camera, crop and artwork layout. '
      'Keep all exposed skin, hair, background and separate garments unchanged. Preserve visible gaps. '
      'Do not flatten black fabric into uniform ink or turn pale fabric into clipped white/metallic/nonnatural blue shadows. '
      'Reference 1 controls this exact composition. Reference 3, when present, is the ORIGINAL TEMPLATE ARTWORK: '
      'use its design hierarchy only, never its product, colours, brand, claims or wording. '
      'Do not create new scenes, models, feature claims, logos, borders or watermarks. '
      'This is an expressly authorised GENERATIVE variant, not a claim of unchanged exterior pixels.\n'
      '[TARGET COLOUR LABEL; REAL PHOTO IS COLOUR TRUTH] '+v['targetColour']+'\n'
      '[EXACT ARTWORK COPY] '+json.dumps(v['expectedCopy'],ensure_ascii=False)+'\n'
      'Retain these exact strings and their original typographic hierarchy. Empty copy means no added text.\n'
      '[USER REQUEST] '+recipe['brief'])
    if t:
        if hashlib.sha256(t['prompt'].encode('utf8')).hexdigest()!=t['promptSha256']:raise ValueError('模板原文哈希改变')
        prompt+='\n[COMPLETE ORIGINAL TEMPLATE PROMPT; REFERENCE 3 IS ITS ARTWORK]\n'+t['prompt']+'\n[/COMPLETE ORIGINAL TEMPLATE PROMPT]\n'
        prompt+='Keep the checked MASTER layout and verified exact copy above; do not transfer the template example product or text.'
    return prompt

def inspect(recipe,result,source,json_model):
    v=recipe['generatedColourVariant'];t=v.get('template')
    required=REQUIRED+(('templateFollowed',)if t else())+(('whiteBackgroundCorrect',)if v['slotKind']=='white'else())
    instruction=('Independently compare the ACTUAL images, at full view and about 300px thumbnail. '
      'Image 1 is checked master artwork (layout and product structure); image 2 is the new generated output; '
      'image 3 is the real same-product target colour photograph. Image 4, when present, is the original template artwork (layout only). '
      'Judge the garment colour against image 3. '
      'All original product identity, seams, pockets, quilting, fit, textile detail, people/skin/hair, unrelated clothing, '
      'negative gaps, scene, crop, layout and exact allowed copy must remain coherent. '
      'Check every repeated person/garment and macro/inset photo, inner collar, cuffs, zipper, hands and hair. '
      'Wrong cut, altered construction, missed garment instances, dye spill, flattened quilting, clipped pale highlights, '
      'inconsistent shade/material or stale colour wording are blocking; minor antialiasing alone is a warning. '
      'A different product in target photo is blocking even when its colour matches. '
      'No numerical average, model completion, different template IDs or generic colour name grants approval. '
      'Return JSON {blocking:bool,issues:[str],warnings:[str],identityPreserved:bool,composition:str,colour:str,edges:str,text:str,'
      +','.join(k+':bool'for k in required if k!='identityPreserved')+'}. '
      'Every required boolean must be explicit. Do not claim human review.')
    context={'mode':'generated_colour_variant','brief':recipe['brief'],'targetColour':v['targetColour'],
      'expectedCopy':v['expectedCopy'],'sourceArtworkSha256':v['sourceSha256'],
      'targetReferenceSha256':v['targetReferenceSha256'],'requiredFlags':list(required),
      'assignedOriginalTemplate':{k:t[k]for k in ('id','style','sha256','prompt','promptSha256')}if t else None,
      'referenceOrder':['checked_master','actual_output','actual_target_colour_photo']+(['original_template']if t else[])}
    images=[source,result['image'],recipe['references'][1]]+([t['url']]if t else[])
    checked=json_model(instruction,context,images,'independent_generated_colour_variant_review')
    if not isinstance(checked,dict)or type(checked.get('blocking'))is not bool or any(type(checked.get(k))is not bool for k in required):
        raise ValueError('生成颜色变体质检缺少明确结论，保留成图且不放行')
    if any(not isinstance(checked.get(k),list)or any(not isinstance(s,str)for s in checked[k])for k in ('issues','warnings')):
        raise ValueError('生成颜色变体质检缺少完整问题记录')
    for key in required:
        if checked[key]is False:
            checked['blocking']=True;checked['issues'].append('颜色变体独立检查未通过：'+key)
    if checked['issues']:checked['blocking']=True
    return checked
