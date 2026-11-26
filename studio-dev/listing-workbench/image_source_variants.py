"""Select a recorded product-colour photo without creating model/SKU jobs."""
import hashlib,io,json,re,time,uuid
from pathlib import Path
from PIL import Image,ImageOps
import app_paths
import variant_colours

def catalog(product):
    return {'productId':product['id'],'colors':[
        {'key':g['key'],'color':g['color'],'children':g['children'],
         'listingEligible':g['listingEligible'],'issues':g['issues'],
         'references':g['references']}
        for g in variant_colours.display_catalog(product).values()]}

def group(product,key,reference):
    groups=variant_colours.display_catalog(product)
    current=groups.get(key)
    if not current or current.get('listingEligible')is not True:raise ValueError('该颜色未纳入当前可用变体，请先核对商品资料')
    ref=next((r for r in current['references']if r['url']==reference),None)
    if not ref:raise ValueError('参考图不是当前商品该颜色已关联的图片')
    identity={'productId':product['id'],'colorKey':key,'color':current['color'],
              'children':current['children'],'reference':ref}
    return identity,hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def folder():return app_paths.DATA/'image-source-variants'


def reviewed_reference_sha(product, key, reference):
    """Read the exact colour source for a review without downloading or writing files."""
    import image_engine as engine

    _, receipt_id = group(product, key, reference)
    saved = folder() / (receipt_id + '.json')
    if saved.is_file():
        row = json.loads(saved.read_text('utf-8'))
        if row['reference'] != reference or row['colorKey'] != key:
            raise ValueError('已保存颜色原图与本款不符，请重新选择')
        return freeze(product, {'receiptId': receipt_id}, row['sourceUpload'], engine.asset)['sourceSha256']
    if reference.startswith('/uploads/'):
        return engine.asset(reference)[1]
    return None


def prepare(product,data):
    import image_engine as engine
    if not isinstance(data,dict)or set(data)-{'productId','colorKey','reference'}:raise ValueError('颜色选图参数无效')
    identity,identity_hash=group(product,data.get('colorKey'),data.get('reference'))
    receipt_id=identity_hash
    saved=folder()/(receipt_id+'.json')
    if saved.is_file():
        row=json.loads(saved.read_text('utf-8'))
        if row['identityHash']!=identity_hash or engine.asset(row['sourceUpload'])[1]!=row['sourceSha256']:
            raise ValueError('已保存颜色原图发生变化，请先核对原文件')
        if row['reference'].startswith('/uploads/') and engine.asset(row['reference'])[1]!=row['originalSha256']:
            raise ValueError('颜色参考图内容已变化，请重新核对资料')
        freeze(product,{'receiptId':receipt_id},row['sourceUpload'],engine.asset)
        return response(row)
    url=identity['reference']['url']
    if url.startswith('/uploads/'):
        image,original_sha=engine.asset(url);source=url
    else:
        import image_provider
        raw,_=image_provider.remote_reference(url)
        original_sha=hashlib.sha256(raw).hexdigest()
        with Image.open(io.BytesIO(raw))as opened:
            if opened.width*opened.height>40_000_000:raise ValueError('颜色参考图超过4000万像素')
            image=ImageOps.exif_transpose(opened).convert('RGBA')
        source=engine.cache(image)['url']
    source_sha=engine.asset(source)[1]
    row={'receiptId':receipt_id,'identityHash':identity_hash,'productId':product['id'],
         'colorKey':identity['colorKey'],'color':identity['color'],'children':identity['children'],
         'reference':url,'referenceType':identity['reference']['referenceType'],
         'sourceAsin':identity['reference'].get('sourceAsin'),'sourceUpload':source,
         'sourceSha256':source_sha,'originalSha256':original_sha,'preparedAt':time.time(),
         'usage':'source_colour_context_only','skuBinding':None}
    folder().mkdir(parents=True,exist_ok=True)
    temporary=saved.with_name(saved.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(row,ensure_ascii=False),'utf-8');temporary.replace(saved)
    return response(row)

def response(row):
    return {'source':{'url':row['sourceUpload'],'sha256':row['sourceSha256']},
            'association':{k:row[k]for k in('receiptId','colorKey','color','reference','sourceUpload','children')}}

def freeze(product,value,source,asset,normalized_source=None):
    if not isinstance(value,dict)or set(value)!={'receiptId'}or not re.fullmatch(r'[a-f0-9]{64}',str(value.get('receiptId'))):
        raise ValueError('颜色来源关联格式无效')
    path=folder()/(value['receiptId']+'.json')
    if not path.is_file():raise ValueError('当前账号没有这份颜色选图记录')
    row=json.loads(path.read_text('utf-8'))
    current,current_hash=group(product,row['colorKey'],row['reference'])
    if row['productId']!=product['id']or row['identityHash']!=current_hash:raise ValueError('商品颜色或子SKU已变化，请重新选择')
    if (row['color']!=current['color']or row['children']!=current['children']
            or row['referenceType']!=current['reference']['referenceType']
            or row.get('sourceAsin')!=current['reference'].get('sourceAsin')):
        raise ValueError('颜色选图记录与当前关联资料不一致')
    if source!=row['sourceUpload']or asset(source)[1]!=row['sourceSha256']:raise ValueError('当前原图与所选颜色来源不一致')
    if normalized_source and normalized_source!=source:
        original=asset(source)[0].convert('RGBA');normalized=asset(normalized_source)[0].convert('RGBA')
        if original.size!=normalized.size or original.tobytes()!=normalized.tobytes():raise ValueError('换色处理原图与所选颜色像素不一致')
    if row['reference'].startswith('/uploads/')and asset(row['reference'])[1]!=row['originalSha256']:raise ValueError('原关联参考图已变化')
    return row
