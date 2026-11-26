"""Read-only checked mother artworks for the current account's image workflow."""
import json
from contextlib import closing

def list_sources(product_id):
    import image_engine as engine
    items=[]
    with closing(engine.db())as connection:
        rows=connection.execute("SELECT body FROM tasks WHERE product_id=? AND json_extract(body,'$.status')='completed' AND json_extract(body,'$.request.mode')='suite' ORDER BY updated DESC LIMIT 100",(product_id,)).fetchall()
    for row in rows:
        task=json.loads(row[0])
        for result in task.get('results',[]):
            review=result.get('review',{});shot=result.get('shot',{})
            if review.get('blocking')is not False or review.get('identityPreserved')is not True:continue
            if shot.get('apparelKind')=='size' or result.get('productionMethod')=='verified-size-data-typesetting':continue
            if type(result.get('index'))is not int or not result.get('image'):continue
            try:_,digest=engine.asset(result['image'])
            except(ValueError,OSError):continue
            expected = result.get('imageSha256') or result.get('sha256')
            if expected and expected != digest:
                continue
            template=shot.get('officialTemplate')or{}
            receipt=result.get('templateExecution')or{}
            is_main=shot.get('apparelKind')=='white' or shot.get('layout')=='main'
            if not is_main and (not template.get('id') or receipt.get('artworkReferenceSubmitted')is not True or receipt.get('completeOriginalPromptSubmitted')is not True):continue
            items.append({'taskId':task['id'],'resultIndex':result['index'],'image':result['image'],'sha256':digest,
              'kind':shot.get('apparelKind')or shot.get('layout')or'gallery','created':task.get('created'),
              'templateStyle':template.get('style'),'brief':task['request'].get('brief','')[:180],
              'warnings':review.get('warnings',[])})
    return items
