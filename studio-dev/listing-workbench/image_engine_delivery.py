"""Explicit approval of reviewed, product-bound Image Engine galleries."""
import hashlib
import json
import re
import time
import uuid
from contextlib import closing

import creative_skills
import generated_gallery
import image_composition
import image_engine
import image_provider
import image_source_variants
import shared_size_chart
import variant_colours
from supplier_variants import _size

ROLES = ('white', 'seller', 'seller', 'ins', 'ins', 'ins', 'size')
PRODUCT_SLOTS = tuple(map(str, range(len(ROLES) - 1)))


def _receipt(db, product_id):
    db.execute('CREATE TABLE IF NOT EXISTS engine_delivery (product_id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    row = db.execute('SELECT body FROM engine_delivery WHERE product_id=?', (product_id,)).fetchone()
    return json.loads(row[0]) if row else None


def selection(product_id):
    with closing(image_engine.db()) as db, db:
        return _receipt(db, product_id)


def scope(product):
    if creative_skills.select(product)['category'] != 'apparel':
        raise ValueError('此交接入口适用于服装正式七图')
    groups = variant_colours.catalog(product)
    master = variant_colours.resolve(groups, product.get('masterColor'))
    if not master or not groups or any(not g['ready'] or not g['children'] for g in groups.values()):
        raise ValueError('请先核对母版颜色、真实子SKU及货源对应关系')
    return groups, master


def _candidate(task):
    rows = task.get('results') or []
    request = task.get('request') or {}
    roles = tuple(row.get('shot', {}).get('apparelKind') for row in rows)
    if (task.get('status') != 'completed' or request.get('mode') != 'suite'
            or roles != ROLES or [row.get('index') for row in rows] != list(range(7))
            or not request.get('sourceVariant')
            or task.get('lifestyleSetReview', {}).get('blocking') is not False
            or any(row.get('aspectValid') is not True or row.get('review', {}).get('blocking') is not False
                   or row.get('review', {}).get('identityPreserved') is not True for row in rows)):
        raise ValueError('请选择已完成原生质检的七图套图，并使用真实关联颜色原图')
    if request.get('templateExecutionPolicy') != 'official-artwork-v1':
        raise ValueError('原套图缺少正式模板执行依据')
    for row in rows[1:6]:
        template = row.get('templateExecution') or {}
        if request.get('preserveSourcePixels') is True:
            if template.get('mode') != 'source-pixel-layout':
                raise ValueError('原片合成缺少实际模板版式回执')
        elif (template.get('artworkReferenceSubmitted') is not True
                or template.get('completeOriginalPromptSubmitted') is not True
                or row['review'].get('templateFollowed') is not True):
            raise ValueError('细节和种草图须通过原模板配图与完整原文的执行检查')
    return request, rows


def _derived_candidate(task, product_id, source_task_id, color_key, slot):
    request = task.get('request') or {}
    variant = request.get('generatedColourVariant') or {}
    rows = task.get('results') or []
    source = request.get('sourceVariant') or {}
    if (task.get('status') != 'completed' or request.get('mode') != 'repair'
            or task.get('productId') != product_id or source.get('colorKey') != color_key
            or variant.get('sourceTaskId') != source_task_id or variant.get('sourceResultIndex') != slot
            or variant.get('targetColour') != source.get('color')
            or len(rows) != 1 or rows[0].get('index') != 0
            or rows[0].get('aspectValid') is not True
            or not re.fullmatch(r'[a-f0-9]{64}', str(rows[0].get('imageSha256') or ''))
            or rows[0].get('productionMethod') != 'codex-generated-colour-variant'):
        raise ValueError('派生图片未绑定到当前颜色套图和图位')
    review = rows[0].get('review') or {}
    import generated_colour_variant
    if review.get('blocking') is not False or review.get('identityPreserved') is not True:
        raise ValueError('派生图片没有通过独立质检')
    if any(review.get(key) is not True for key in generated_colour_variant.REQUIRED if key != 'identityPreserved'):
        raise ValueError('派生图片的颜色、材质、布局或文字质检不完整')
    if variant.get('slotKind') != ROLES[slot]:
        raise ValueError('派生图片用途与原图位不一致')
    if (variant.get('slotKind') == 'white' and review.get('whiteBackgroundCorrect') is not True
            or variant.get('template') and review.get('templateFollowed') is not True):
        raise ValueError('派生图片缺少白底或原模板检查')
    return request, rows[0]


def view(product, revision):
    current = selection(product['id'])
    try:
        groups, master = scope(product)
    except ValueError as error:
        return {'revision': revision, 'groups': [], 'selection': current, 'reason': str(error)}
    reason = None
    if current:
        try:
            _verify_selection(product, current)
        except (ValueError, OSError) as error:
            reason = str(error)
    choices = {key: [] for key in groups}
    all_tasks = image_engine.list_tasks(product['id'], limit=None, completed_only=True)
    derived_by_suite = {}
    for task in all_tasks:
        request = task.get('request') or {}
        variant = request.get('generatedColourVariant') or {}
        slot = variant.get('sourceResultIndex')
        key = (request.get('sourceVariant') or {}).get('colorKey')
        suite_id = variant.get('sourceTaskId')
        if key not in groups or type(slot) is not int or not 0 <= slot < 6:
            continue
        try:
            _, result = _derived_candidate(task, product['id'], suite_id, key, slot)
        except ValueError:
            continue
        slots = derived_by_suite.setdefault((suite_id, key), {})
        slots.setdefault(str(slot), []).append({'id': task['id'], 'image': result['image']})
    for task in all_tasks:
        try:
            request, rows = _candidate(task)
        except ValueError:
            continue
        key = request['sourceVariant'].get('colorKey')
        if key not in choices:
            continue
        for target in choices if key == master else (key,):
            slots = derived_by_suite.get((task['id'], target), {})
            if target != key and not slots:
                continue
            choices[target].append({'id': task['id'], 'images': [row['image'] for row in rows],
                                    'usesMaster': target != key, 'slotCandidates': slots})
    return {'revision': revision, 'selection': current, 'reason': reason, 'groups': [
        {'key': key, 'color': group['color'], 'master': key == master, 'candidates': choices[key]}
        for key, group in groups.items()]}


def _resolve(product, task_ids):
    groups, master = scope(product)
    if not isinstance(task_ids, dict) or set(task_ids) != set(groups):
        raise ValueError('请逐一选择母版和每个真实颜色的套图')
    expected_chart = shared_size_chart.from_product(product)
    output = {}
    bindings = {}
    chart_image = None
    master_task_id = None
    for key in [master, *(key for key in groups if key != master)]:
        choice = task_ids[key]
        if isinstance(choice, str):
            task_id, slot_tasks = choice, {}
        elif (isinstance(choice, dict) and set(choice) == {'suiteTaskId', 'slotTaskIds'}
              and isinstance(choice['suiteTaskId'], str) and isinstance(choice['slotTaskIds'], dict)):
            task_id, slot_tasks = choice['suiteTaskId'], choice['slotTaskIds']
        else:
            raise ValueError('每个颜色请选择套图并绑定可选的派生图位')
        if not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('原作图任务号无效')
        task = image_engine.get(task_id)
        if task.get('productId') != product['id']:
            raise ValueError('原作图任务不属于当前账号和商品')
        request, results = _candidate(task)
        source = request['sourceVariant']
        current = image_source_variants.freeze(product, {'receiptId': source.get('receiptId')},
                                               request['references'][0], image_engine.asset)
        if current != source:
            raise ValueError('原图的颜色或子SKU关联已变化，请核对原图')
        uses_master = current.get('colorKey') != key
        if uses_master and (key == master or task_id != master_task_id):
            raise ValueError('其他颜色的派生图须沿本次选定的母版生成')
        if uses_master and set(slot_tasks) != set(PRODUCT_SLOTS):
            raise ValueError('请选齐此颜色的六个过检图位，尺码图沿用母版')
        if key == master:
            master_task_id = task_id
        color_source = None if uses_master else source
        approved = []
        if any(slot not in PRODUCT_SLOTS
               or not isinstance(derived_id, str) or not re.fullmatch(r'[a-f0-9]{32}', derived_id)
               for slot, derived_id in slot_tasks.items()):
            raise ValueError('派生图位选择无效')
        derived_bindings = {}
        for slot, result in enumerate(results):
            if slot == 6 and key != master:
                approved.append(chart_image)
                continue
            source_task_id = task_id
            if str(slot) in slot_tasks:
                derived = image_engine.get(slot_tasks[str(slot)])
                if derived.get('productId') != product['id']:
                    raise ValueError('派生图片任务不属于当前商品')
                derived_request, result = _derived_candidate(derived, product['id'], task_id, key, slot)
                original_hash = image_engine.asset(results[slot]['image'])[1]
                if (derived_request['generatedColourVariant'].get('sourceSha256') != original_hash
                        or results[slot].get('imageSha256') != original_hash):
                    raise ValueError('派生图片母版哈希与所选正式套图不一致')
                derived_source = derived_request['sourceVariant']
                checked_source = image_source_variants.freeze(product,
                    {'receiptId': derived_source.get('receiptId')}, derived_request['references'][1], image_engine.asset)
                if checked_source != derived_source or checked_source.get('colorKey') != key:
                    raise ValueError('派生图片的真实颜色来源或子SKU已变化')
                if color_source is not None and checked_source != color_source:
                    raise ValueError('各图位须使用同一真实颜色来源')
                color_source = checked_source
                source_task_id = derived['id']
                derived_bindings[str(slot)] = derived['id']
            image = result['image']
            raw = image_provider.local_image(image)
            sha = hashlib.sha256(raw).hexdigest()
            if result.get('imageSha256') and result['imageSha256'] != sha:
                raise ValueError('候选文件已变化，请核对原质检记录')
            if slot == 6:
                chart = request.get('apparelSuite', {}).get('sizeChart') or {}
                if (chart.get('sourceEvidence') != expected_chart['sourceEvidence']
                        or shared_size_chart.table(chart) != shared_size_chart.table(expected_chart)
                        or result.get('productionMethod') != 'verified-size-data-typesetting'
                        or result.get('sizeDataSha256') != hashlib.sha256(
                            json.dumps(chart, sort_keys=True, ensure_ascii=False).encode()).hexdigest()):
                    raise ValueError('第7张图与当前真实尺码表不一致')
                sizes = {_size(str(child.get('size') or '')) for group in groups.values() for child in group['children']}
                if '' in sizes or not sizes <= {_size(str(row['size'])) for row in chart['rows']}:
                    raise ValueError('真实尺码表须覆盖所有实际子SKU')
            image_composition.require(raw, slot=slot, product=product, detail=slot in (1, 2), size_chart=slot == 6)
            delivered, info = generated_gallery.prepare_delivery(raw)
            if info['mime'] not in ('image/png', 'image/jpeg'):
                raise ValueError('正式图库只接受符合ERP要求的PNG或JPEG')
            if slot == 6 and delivered != raw:
                raise ValueError('尺码图必须使用已质检且符合交付大小的原始文件')
            approved.append((delivered, {**info, 'slot': slot, 'engineTaskId': source_task_id,
                                'sourceImageSha256': sha, 'status': 'approved', 'humanReviewed': False}))
        if len({info['sha256'] for _, info in approved}) != len(ROLES):
            raise ValueError('正式图库存在重复图位，请逐图核对')
        if key == master:
            chart_image = approved[6]
        output[key] = approved
        bindings[key] = {'taskId': task_id,
                         'sourceReceiptId': color_source['receiptId'],
                         'children': groups[key]['children'],
                         'images': [info['sha256'] for _, info in approved]}
        if derived_bindings:
            bindings[key]['derivedTaskIds'] = derived_bindings
    return output, {'master': master, 'bindings': bindings}


def approve(owner, product, data):
    if not isinstance(data, dict) or data.get('confirmed') is not True or set(data) != {
            'productId', 'expectedRevision', 'taskIds', 'confirmed'}:
        raise ValueError('请明确核对并确认每个颜色的正式图库')
    _, reviewed = _resolve(product, data['taskIds'])
    with closing(image_engine.db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = _receipt(db, product['id'])
        if prior and all(prior.get(key) == value for key, value in reviewed.items()):
            return prior
        db.execute('''CREATE TABLE IF NOT EXISTS engine_delivery_versions (
            product_id TEXT NOT NULL, version_id TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(product_id, version_id))''')
        receipt = {**reviewed, 'accountId': owner, 'productId': product['id'],
                   'versionId': uuid.uuid4().hex, 'approvedAt': time.time(), 'taskIds': data['taskIds']}
        for version in (prior, receipt):
            if version:
                payload = json.dumps(version, ensure_ascii=False, sort_keys=True)
                version_id = version.get('versionId') or hashlib.sha256(payload.encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO engine_delivery_versions VALUES (?,?,?)',
                           (product['id'], version_id, payload))
        db.execute('INSERT OR REPLACE INTO engine_delivery VALUES (?, ?)',
                   (product['id'], json.dumps(receipt, ensure_ascii=False)))
    return receipt


def galleries(product):
    receipt = selection(product['id'])
    if receipt is None:
        return None
    return _verify_selection(product, receipt)


def _verify_selection(product, receipt):
    output, reviewed = _resolve(product, receipt['taskIds'])
    if any(receipt.get(key) != value for key, value in reviewed.items()):
        raise ValueError('已确认的图库、颜色、子SKU或文件已变化，请重新核对正式图库')
    return output
