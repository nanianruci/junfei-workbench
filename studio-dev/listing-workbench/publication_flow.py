"""Explicit, durable US publishing -> UK copy/prepare/publish continuation.

Collection stays draft-only. This module consumes its verified frozen snapshot;
it never uploads images or silently enrolls existing products.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, closing
from urllib.parse import urlsplit
from datetime import datetime

import app_paths
import erp_collection as collection
import erp_collection_inputs as collection_inputs
import publication_inputs
import publication_policy
import publication_required_attributes as required_attributes
import publication_identifiers as identifiers
import publication_media_episode as media_episode
import publication_uk_media as uk_media
from publication_browser_result import current_browser_error

ROOT = app_paths.DATA / 'publication-flow'
SETTINGS = app_paths.DATA / 'publication-settings.json'
PROJECT = Path(__file__).resolve().parent.parent
MARKET_DOM_AUTHORIZATIONS = PROJECT / 'automation/ziniao-market-authorizations.json'
LOCK = threading.RLock()
ACTIVE = set()
RUNTIME = uuid.uuid4().hex
MEDIA_RETRY_CODES = frozenset(('300060', '18320'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


@contextmanager
def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ROOT / 'jobs.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
        product_id TEXT NOT NULL,destination_id TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,
        UNIQUE(account_id,product_id,destination_id))''')
    try:
        yield db
    finally:
        db.close()


def pairs(user):
    if not SETTINGS.is_file():
        return []
    value = json.loads(SETTINGS.read_text(encoding='utf-8'))
    return [p for p in value.get('pairs', []) if p.get('accountId') == user]


def save_settings(user, data):
    allowed = {'sourceDestinationId', 'storeName', 'shippingTemplate', 'brand'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('英国站设置参数无效')
    source = [d for d in collection.destinations(user)
              if d['id'] == data.get('sourceDestinationId') and d.get('marketplace') == 'US']
    if len(source) != 1:
        raise ValueError('请选择本账号已配置的美国ERP目标')
    for key in ('storeName', 'shippingTemplate'):
        if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 200:
            raise ValueError('请填写英国ERP店铺和实际运输模板名称')
    if data['storeName'].strip() == source[0]['store']:
        raise ValueError('英国目标不能是美国店铺')
    pair = {'accountId': user, 'id': source[0]['id'] + '-UK', 'sourceDestinationId': source[0]['id'],
            'storeName': data['storeName'].strip(), 'countryCode': 'GB', 'currency': 'GBP',
            'languageTag': 'en_GB', 'shippingTemplate': {'name': data['shippingTemplate'].strip()},
            'updatedAt': time.time()}
    if data.get('brand'):
        if not isinstance(data['brand'], str) or len(data['brand']) > 100:
            raise ValueError('英国品牌名称无效')
        pair['brand'] = data['brand'].strip()
    with LOCK:
        settings = json.loads(SETTINGS.read_text(encoding='utf-8')) if SETTINGS.is_file() else {'pairs': []}
        old = next((p for p in settings['pairs'] if p.get('accountId') == user
                    and p.get('sourceDestinationId') == source[0]['id']), None)
        if old:
            settings.setdefault('history', []).append(copy.deepcopy(old))
        settings['pairs'] = [p for p in settings['pairs'] if not (p.get('accountId') == user
                             and p.get('sourceDestinationId') == source[0]['id'])] + [pair]
        write_json(SETTINGS, settings)
    return pair


def source_receipt(user, pid, destination_id):
    with collection.connect() as db:
        row = db.execute('SELECT body FROM jobs WHERE account_id=? AND product_id=? AND destination_id=?',
                         (user, pid, destination_id)).fetchone()
    if not row:
        raise ValueError('请先完成本款美国站一键采集与草稿回读')
    job = json.loads(row[0])
    if job.get('status') != 'verified' or job.get('engine') != 'native-v2':
        raise ValueError('美国原生草稿尚未回读通过，请先核对原采集任务')
    if not re.fullmatch('[a-f0-9]{32}', job['id']):
        raise ValueError('采集任务编号无效')
    folder = collection.ROOT / job['id']
    try:
        source = json.loads((folder / 'collection.json').read_text(encoding='utf-8'))
        receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError('原美国草稿回执不可读，不能新建代替') from error
    if (source.get('accountId') != user or source.get('productId') != pid
            or source.get('marketplace') != 'US' or receipt.get('accountId') != user
            or receipt.get('productId') != pid or receipt.get('snapshotSha256') != source.get('sha256')
            or str(receipt.get('draftId')) != str(job.get('draftId'))):
        raise ValueError('美国草稿身份、快照或原回执不一致')
    collection.verified_result({}, receipt, source)
    return job, source, receipt


def _nonblocking_media_advisory(message):
    """Only the observed US apparel quality notice; never ignore arbitrary warnings."""
    return (message.get('code') == '18448' and message.get('severity') == 'WARNING'
            and message.get('message') == '您提交的内容缺少一些关键属性：material, pattern, special_feature。 缺少这些属性不会阻止您的商品在亚马逊上展示。 然而，这会影响您的商品信息的整体质量。'
            and message.get('attributeNames') == ['material', 'pattern', 'special_feature']
            and isinstance(message.get('categories'), list)
            and sorted(message['categories']) == ['MISSING_ATTRIBUTE', 'PRODUCT']
            and 'enforcements' not in message and message.get('solution', '') == ''
            and not set(message) - {'code', 'message', 'severity', 'attributeNames', 'categories', 'sku', 'solution', 'childId', 'sources'})


def _media_retry_scope(job, record, source):
    """Authorize only a persisted, identity-checked failure of this US task.

    This freezes permission to retry the original parent task. The native
    adapter still rereads current failures and every image before any write.
    """
    us = record.get('usPublish') or {}
    observation = us.get('latestObservation') or us.get('failureObservation') or {}
    publish_id = str(us.get('publishId') or '')
    expected = {'accountId': job['accountId'], 'productId': job['productId'],
                'snapshotSha256': source.get('sha256'), 'destinationId': job['destinationId'],
                'draftId': str(source.get('destination', {}).get('draftId') or ''),
                'erpAccountId': str(source.get('destination', {}).get('erpAccountId') or ''),
                'marketId': str(source.get('destination', {}).get('marketId') or ''),
                'countryCode': 'US', 'store': source.get('store'),
                'parentSku': source.get('goods', {}).get('parentSku'), 'publishId': publish_id}
    if (source.get('accountId') != job['accountId'] or source.get('productId') != job['productId']
            or source.get('jobId') != job['id'] or record.get('jobId') != job['id']
            or source.get('destination', {}).get('destinationId') != job['destinationId']
            or record.get('accountId') != job['accountId'] or record.get('productId') != job['productId']
            or record.get('snapshotSha256') != source.get('sha256')
            or not re.fullmatch(r'[1-9]\d*', publish_id)
            or observation.get('identityVerified') is not True or observation.get('failed') is not True
            or observation.get('verification') != 'failed'
            or any(observation.get(key) != value for key, value in expected.items())):
        raise ValueError('尚无属于原美国任务的完整失败身份回读，请先只读核对原回执')
    fingerprint = observation.get('responseFingerprint')
    failed_ids, failed_skus = observation.get('failedChildIds'), observation.get('skus')
    children, variants = observation.get('children'), source.get('goodsItems')
    if (not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint)
            or not isinstance(failed_ids, list) or not failed_ids
            or any(not isinstance(value, str) or not re.fullmatch(r'[1-9]\d*', value) for value in failed_ids)
            or len(set(failed_ids)) != len(failed_ids) or not isinstance(failed_skus, list)
            or len(set(failed_skus)) != len(failed_skus) or len(failed_skus) != len(failed_ids)
            or observation.get('count') != len(failed_ids)
            or not isinstance(children, list) or not isinstance(variants, list) or not variants
            or len(children) != len(variants) or observation.get('childCount') != len(variants)):
        raise ValueError('原任务失败子体范围或观察指纹不完整，不能重试发布')
    expected_variants = {row['sku']: row for row in variants}
    if (len(expected_variants) != len(variants) or {row.get('sku') for row in children} != set(expected_variants)
            or len({row.get('childId') for row in children}) != len(children)):
        raise ValueError('失败回读的子SKU集合与原快照不同')
    children_by_id = {}
    for child in children:
        variant = expected_variants[child['sku']]
        images = [source['urls'][source['assets'][index]['sha256']] for index in variant['imageIndices']]
        if (not isinstance(child.get('childId'), str) or not re.fullmatch(r'[1-9]\d*', child['childId'])
                or (child.get('color'), child.get('size')) != (variant['color'], variant['size'])
                or child.get('imagesSource') != 'child_gallery' or len(images) != 7
                or child.get('images') != images or child.get('main') != images[0]):
            raise ValueError('失败回读的原SKU、颜色尺码或独立七图不完整')
        children_by_id[child['childId']] = child
    if not set(failed_ids).issubset(children_by_id) or {children_by_id[value]['sku'] for value in failed_ids} != set(failed_skus):
        raise ValueError('失败子体编号与原SKU对应不一致')
    messages = observation.get('messages')
    if (not isinstance(messages, list) or not messages or observation.get('messageCount') != len(messages)
            or observation.get('unscopedMessageCount') != 0
            or {message.get('childId') for message in messages} != set(failed_ids)
            or {message.get('childId') for message in messages if str(message.get('code')) in MEDIA_RETRY_CODES} != set(failed_ids)
            or any((str(message.get('code')) not in MEDIA_RETRY_CODES and not _nonblocking_media_advisory(message))
                   or message.get('childId') not in children_by_id
                   or message.get('sku') != children_by_id[message['childId']]['sku'] for message in messages)):
        raise ValueError('仅支持已核对的300060或18320图片错误；其他错误须另行处理')
    return {**expected, 'failureResponseFingerprint': fingerprint,
            'failedChildIds': copy.deepcopy(failed_ids), 'failedSkus': copy.deepcopy(failed_skus)}


def _main_image_repair_scope(job, record, source):
    """Freeze only the original main image of one canonical 18320 child."""
    scope = _media_retry_scope(job, record, source)
    us = record.get('usPublish') or {}
    observation = us.get('latestObservation') or us.get('failureObservation') or {}
    canonical = us.get('failureObservation') or {}
    if (observation.get('status') != 'failed' or observation.get('count') != 1
            or len(scope['failedChildIds']) != 1 or len(scope['failedSkus']) != 1
            or any(str(message.get('code')) != '18320' for message in observation['messages'])
            or canonical.get('responseFingerprint') != scope['failureResponseFingerprint']
            or {key: value for key, value in canonical.items() if key != 'observedAt'}
                != {key: value for key, value in observation.items() if key != 'observedAt'}):
        raise ValueError('主图字段修复仅支持原任务单个子SKU的18320错误，请先核对最新完整回执')
    failed_id, failed_sku = scope['failedChildIds'][0], scope['failedSkus'][0]
    child = next(row for row in observation['children'] if row['childId'] == failed_id)
    if (child.get('status') != 'failed' or observation.get('acceptedCount') != len(source['goodsItems']) - 1
            or any(row.get('status') != ('failed' if row['childId'] == failed_id else 'accepted')
                   for row in observation['children'])):
        raise ValueError('其余原子SKU尚未完整核对，不能单独修复主图字段')
    variant = next(row for row in source['goodsItems'] if row['sku'] == failed_sku)
    asset = source['assets'][variant['imageIndices'][0]]
    sha, url = asset.get('sha256'), source['urls'].get(asset.get('sha256'))
    parsed = urlsplit(url or '')
    if (not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)
            or not isinstance(url, str) or child['main'] != url
            or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or sum(source['urls'].get(row['sha256']) == url for row in source['assets']) != 1):
        raise ValueError('原主图URL与原文件SHA256不能唯一对应，不建立修复授权')
    return {key: value for key, value in scope.items() if key not in ('failedChildIds', 'failedSkus')} | {
        'failedChildId': failed_id, 'failedSku': failed_sku, 'mainImageUrl': url, 'assetSha256': sha,
        'retryOriginalTaskAfterVerifiedSave': True}


def _validate_main_image_amendment(job, record, source):
    amendment = source.get('publicationMainImageRepair')
    if not isinstance(amendment, dict):
        raise ValueError('既有原主图修复授权格式需核对')
    expected = {'scope': 'repair_original_failed_main_image', 'marketplace': 'US',
                'jobId': job['id'], 'accountId': job['accountId'], 'productId': job['productId'],
                'snapshotSha256': source.get('sha256'), 'publishId': str((record.get('usPublish') or {}).get('publishId') or ''),
                'destinationId': job['destinationId'], 'draftId': str(source.get('destination', {}).get('draftId') or ''),
                'erpAccountId': str(source.get('destination', {}).get('erpAccountId') or ''),
                'marketId': str(source.get('destination', {}).get('marketId') or ''), 'countryCode': 'US',
                'store': source.get('store'), 'parentSku': source.get('goods', {}).get('parentSku'),
                'retryOriginalTaskAfterVerifiedSave': True}
    variants = [row for row in source.get('goodsItems', []) if row.get('sku') == amendment.get('failedSku')]
    if (source.get('accountId') != job['accountId'] or source.get('productId') != job['productId'] or source.get('jobId') != job['id']
            or record.get('accountId') != job['accountId'] or record.get('productId') != job['productId']
            or record.get('jobId') != job['id'] or record.get('snapshotSha256') != source.get('sha256')
            or any(amendment.get(key) != value for key, value in expected.items())
            or not re.fullmatch(r'[a-f0-9]{32}', str(amendment.get('id') or ''))
            or not re.fullmatch(r'[a-f0-9]{64}', str(amendment.get('failureResponseFingerprint') or ''))
            or not re.fullmatch(r'[1-9]\d*', str(amendment.get('failedChildId') or ''))
            or len(variants) != 1 or len(variants[0].get('imageIndices', [])) != 7):
        raise ValueError('既有原主图修复授权与原任务不符，只能核对原记录')
    asset = source['assets'][variants[0]['imageIndices'][0]]
    if amendment.get('assetSha256') != asset.get('sha256') or amendment.get('mainImageUrl') != source['urls'].get(asset.get('sha256')):
        raise ValueError('既有原主图修复授权不再对应原图片，不能替换或重建')
    journal = (record.get('usPublish') or {}).get('mainImageRepair')
    if journal and (journal.get('amendmentId') != amendment['id'] or str(journal.get('publishId')) != amendment['publishId']):
        raise ValueError('原主图修复检查点属于另一份授权，不覆盖或另建')
    if amendment.get('correctiveSave'):
        _validate_corrective_main_image_amendment(amendment, journal or {})
    return amendment


def _validate_corrective_main_image_amendment(amendment, journal):
    corrective = amendment.get('correctiveSave')
    identity_keys = ('accountId', 'productId', 'jobId', 'snapshotSha256', 'publishId', 'destinationId',
                     'draftId', 'erpAccountId', 'marketId', 'countryCode', 'store', 'parentSku', 'marketplace',
                     'failedChildId', 'failedSku', 'mainImageUrl', 'assetSha256', 'failureResponseFingerprint',
                     'retryOriginalTaskAfterVerifiedSave')
    if (not isinstance(corrective, dict)
            or corrective.get('scope') != 'explicit_idempotent_original_main_resave'
            or corrective.get('originalMainAmendmentId') != amendment['id']
            or not re.fullmatch(r'[a-f0-9]{32}', str(corrective.get('id') or ''))
            or any(corrective.get(key) != amendment.get(key) for key in identity_keys)
            or not isinstance(corrective.get('originalSaveIntentArmedAt'), str)
            or corrective['originalSaveIntentArmedAt'] != (journal.get('saveIntent') or {}).get('armedAt')):
        raise ValueError('原主图重新保存授权与原身份或旧保存意图不符，不建立另一份检查点')
    checkpoint = journal.get('correctiveSave')
    if checkpoint and checkpoint.get('amendmentId') != corrective['id']:
        raise ValueError('原主图重新保存检查点属于另一份授权，不覆盖或重建')
    return corrective


def _main_image_corrective_save_scope(job, record, source):
    amendment = _validate_main_image_amendment(job, record, source)
    journal = (record.get('usPublish') or {}).get('mainImageRepair') or {}
    intent = journal.get('saveIntent')
    if (job.get('status') not in ('needs_reconciliation', 'failed', 'submitted')
            or amendment.get('correctiveSave') or 'correctiveSave' in journal
            or not isinstance(intent, dict) or not isinstance(intent.get('armedAt'), str) or not intent['armedAt']
            or intent.get('publishId') != amendment['publishId'] or intent.get('amendmentId') != amendment['id']
            or intent.get('failedChildId') != amendment['failedChildId']
            or journal.get('phase') != 'save_unknown' or journal.get('nativeRequests') != []
            or journal.get('guardError') or journal.get('saveVerified') or journal.get('consumedWithoutWrite')
            or 'intent' in (journal.get('retry') or {})
            or not isinstance(journal.get('lastSaveError'), str)
            or not re.search(r'timed out after [1-9]\d*ms', journal['lastSaveError'], re.IGNORECASE)):
        raise ValueError('旧主图保存不符合一次明确重新保存的条件；未知发布或已有请求不能重放')
    scope = _main_image_repair_scope(job, record, source)
    if any(scope.get(key) != amendment.get(key) for key in scope):
        raise ValueError('最新单个主图错误与原授权或原图不一致，不能重新保存')
    return {**scope, 'jobId': job['id'], 'marketplace': 'US', 'originalMainAmendmentId': amendment['id'],
            'originalSaveIntentArmedAt': intent['armedAt']}


def _observation_scope(job, record, source):
    """Only already-linked US/UK tasks can enter the no-write observation mode."""
    us, uk = record.get('usPublish') or {}, record.get('ukPublish') or {}
    if source.get('publicationMarkets') == ['US']:
        if (source.get('accountId') != job['accountId'] or source.get('productId') != job['productId']
                or source.get('jobId') != job['id'] or record.get('jobId') != job['id']
                or record.get('accountId') != job['accountId'] or record.get('productId') != job['productId']
                or not source.get('sha256') or record.get('snapshotSha256') != source['sha256']
                or not re.fullmatch(r'[1-9]\d*', str(us.get('publishId') or ''))):
            raise ValueError('美国原发布任务号或冻结身份不符')
        return
    preparation, copied = record.get('ukPrepare') or {}, record.get('ukCopy') or {}
    prepared = preparation.get('preparedInput') or {}
    if (source.get('accountId') != job['accountId'] or source.get('productId') != job['productId']
            or source.get('jobId') != job['id'] or record.get('jobId') != job['id']
            or record.get('accountId') != job['accountId'] or record.get('productId') != job['productId']
            or record.get('snapshotSha256') != source.get('sha256')
            or any(not re.fullmatch(r'[1-9]\d*', str(publication.get('publishId') or '')) for publication in (us, uk))
            or preparation.get('phase') != 'uk_draft_verified'
            or copied.get('phase') != 'copy_verified'
            or prepared.get('accountId') != job['accountId'] or prepared.get('productId') != job['productId']
            or prepared.get('sha256') != source.get('sha256')
            or prepared.get('store') != source.get('ukTarget', {}).get('storeName')
            or prepared.get('destination', {}).get('countryCode') != 'GB'
            or prepared.get('destination', {}).get('destinationId') != (source.get('ukTarget', {}).get('id') or source.get('ukTarget', {}).get('destinationId'))
            or str(preparation.get('draftId') or '') != str(copied.get('draftId') or '')
            or str(prepared.get('destination', {}).get('draftId') or '') != str(copied.get('draftId') or '')
            or not re.fullmatch(r'[1-9]\d*', str(copied.get('draftId') or ''))
            or prepared.get('goods', {}).get('parentSku') != source.get('goods', {}).get('parentSku')
            or [(row.get('sku'), row.get('color'), row.get('size')) for row in prepared.get('goodsItems', [])]
                != [(row.get('sku'), row.get('color'), row.get('size')) for row in source.get('goodsItems', [])]):
        raise ValueError('两站原任务号或英国冻结草稿身份未齐全，只能核对原检查点')


def public(job):
    result = {k: job.get(k) for k in ('id', 'productId', 'productName', 'destinationId', 'sourceCollectionId',
              'sourceStore', 'targetStore', 'parentSku', 'variantCount', 'status', 'phase', 'error',
              'createdAt', 'updatedAt', 'submittedAt', 'usPublishId', 'ukDraftId', 'ukPublishId', 'reused', 'observationOnly',
              'mediaEpisodeOnly', 'mediaEpisodeObserveOnly', 'ukMediaOnly', 'ukMediaObserveOnly', 'markets') if k in job}
    result.update(observeSupported=True, canObserveLatestReceipts=False,
                  mediaEpisode={'supported': True, 'authorized': False, 'canRetry': False},
                  ukMediaRecovery={'supported': True, 'authorized': False, 'canRetry': False},
                  mainImageRepair={'supported': True, 'canRepair': False, 'authorized': False,
                                   'canCorrectSave': False, 'authorizedCorrectiveSave': False})
    if job.get('status') in ('queued', 'running') and job['id'] not in ACTIVE:
        if not collection.process_alive(job.get('browserPid')) and (job.get('runOwner') != RUNTIME
                or not collection.process_alive(job.get('ownerPid'))):
            result.update(status='needs_reconciliation', phase='执行中断，从原回执对账继续')
    scoped_observation = False
    path = ROOT / job['id'] / 'receipt.json'
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('accountId') == job.get('accountId') and record.get('productId') == job.get('productId'):
                result['usPublishId'] = record.get('usPublish', {}).get('publishId')
                result['ukDraftId'] = record.get('ukCopy', {}).get('draftId')
                result['ukPublishId'] = record.get('ukPublish', {}).get('publishId')
                result['stage'] = record.get('phase')
                try:
                    observation_packet = json.loads((path.parent / 'input.json').read_text(encoding='utf-8'))
                    _observation_scope(job, record, observation_packet['input'])
                    scoped_observation = True
                    result['canObserveLatestReceipts'] = result.get('status') not in ('queued', 'running')
                except (ValueError, OSError, KeyError, TypeError):
                    pass
                current_failures = []
                try:
                    media_source = json.loads((path.parent / 'input.json').read_text(encoding='utf-8'))['input']
                    if media_source.get('publicationUkMediaRecovery'):
                        a = uk_media.validate(job, record, media_source)
                        j = record.get('ukPublish', {}).get('mediaRecovery') or {}
                        result['ukMediaRecovery'].update(authorized=True, amendmentId=a['id'],
                            phase=j.get('phase'), requestAccepted=j.get('requestAccepted') is True,
                            intentArmed=bool(j.get('intent')), consumedWithoutWrite=j.get('consumedWithoutWrite') is True)
                    else:
                        uk_media.scope(job, record, media_source)
                        result['ukMediaRecovery']['canRetry'] = result['status'] not in ('queued', 'running')
                except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
                    result['ukMediaRecovery']['reason'] = str(error)
                for station, key in (('us', 'usPublish'), ('uk', 'ukPublish')):
                    publication = record.get(key) or {}
                    observation = publication.get('latestObservation') or publication.get('failureObservation')
                    if not isinstance(observation, dict):
                        continue
                    result[station + 'PublicationObservation'] = {field: copy.deepcopy(observation[field]) for field in
                        ('publishId', 'status', 'failed', 'verification', 'observedAt', 'count', 'childCount', 'allChildrenOnline') if field in observation}
                    if observation.get('failed') is True or observation.get('verification') == 'failed':
                        current_failures.append(station)
                        if station == 'us':
                            result['usFailure'] = {'publishId': publication.get('publishId'),
                                'failedCount': observation.get('count'), 'totalCount': observation.get('childCount'),
                                'errorCodes': sorted({str(message.get('code')) for message in observation.get('messages', [])
                                                      if message.get('code') is not None})}
                if current_failures and result.get('status') not in ('queued', 'running'):
                    result.update(status='needs_reconciliation', phase='原发布任务后续失败，需核对修复')
                result['mediaRecovery'] = {'supported': True, 'canRetry': False, 'authorized': False}
                try:
                    packet = json.loads((path.parent / 'input.json').read_text(encoding='utf-8'))
                    journal = (record.get('usPublish') or {}).get('mainImageRepair') or {}
                    main = result['mainImageRepair']
                    if packet['input'].get('publicationMainImageRepair'):
                        amendment = _validate_main_image_amendment(job, record, packet['input'])
                        corrective = journal.get('correctiveSave') or {}
                        checkpoint = ('原状态已恢复，未重复保存或重试' if journal.get('consumedWithoutWrite') else
                                      '已记录原任务重试，仅沿原回执核对' if journal.get('retry', {}).get('intent') else
                                      '再次保存的原主图字段已核对' if corrective.get('saveVerified') else
                                      '再次保存结果待核对，原未知回执已保留' if corrective.get('saveIntent') else
                                      '原主图字段已保存并核对' if journal.get('saveVerified') else
                                      '原主图保存结果待核对' if journal.get('saveIntent') else '原主图修复授权已保存')
                        main.update(authorized=True, repairId=amendment['id'], phase=journal.get('phase'),
                                    saveIntended=bool(journal.get('saveIntent')), retryIntended=bool(journal.get('retry', {}).get('intent')),
                                    checkpoint=checkpoint, reason='继续时仅沿这一份原主图修复检查点核对，不建立另一份授权')
                        main['saveReceiptUnknown'] = bool(journal.get('saveIntent')) and journal.get('nativeRequests') == [] and not journal.get('saveVerified') and not journal.get('consumedWithoutWrite')
                        if amendment.get('correctiveSave'):
                            main.update(authorizedCorrectiveSave=True, correctiveId=amendment['correctiveSave']['id'])
                        else:
                            try:
                                _main_image_corrective_save_scope(job, record, packet['input'])
                                main['canCorrectSave'] = result['status'] not in ('queued', 'running')
                            except (ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
                                main['correctiveSaveReason'] = str(error) if isinstance(error, ValueError) else '重新保存资料不完整，请先核对原记录'
                    elif journal:
                        main['reason'] = '已有原主图修复检查点，不能建立另一份授权'
                    elif 'usFailure' in result:
                        _main_image_repair_scope(job, record, packet['input'])
                        main['canRepair'] = result['status'] not in ('queued', 'running')
                except (ValueError, OSError, KeyError, TypeError, IndexError, StopIteration) as error:
                    result['mainImageRepair']['reason'] = (str(error) if isinstance(error, ValueError) else '原主图修复资料不完整，请先核对原回执')
                if 'usFailure' in result:
                    try:
                        packet = json.loads((path.parent / 'input.json').read_text(encoding='utf-8'))
                        amendment = packet['input'].get('publicationRecovery')
                        if amendment:
                            if not isinstance(amendment, dict):
                                raise ValueError('原图片恢复授权格式需核对')
                            result['mediaRecovery'].update(authorized=True, recoveryId=amendment.get('id'),
                                reason='原图片恢复授权已保存；继续时沿原检查点核对，不创建另一份重试授权')
                        elif packet['input'].get('publicationMainImageRepair'):
                            result['mediaRecovery']['reason'] = '原主图修复已另存明确检查点，不再次启用旧图片重试'
                        else:
                            _media_retry_scope(job, record, packet['input'])
                            result['mediaRecovery']['canRetry'] = result['status'] not in ('queued', 'running')
                    except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
                        result['mediaRecovery']['reason'] = str(error)
                try:
                    packet=json.loads((path.parent/'input.json').read_text(encoding='utf-8'))
                    episodes=packet['input'].get('publicationMediaEpisodes')
                    if episodes:
                        if not isinstance(episodes,list) or len(episodes)!=1:raise ValueError('原媒体episode不是唯一授权')
                        amendment=media_episode.validate_media_episode(job,record,packet['input'],episodes[0])
                        journals=(record.get('usPublish') or {}).get('mediaEpisodes') or []
                        journal=journals[0] if len(journals)==1 else {}
                        result['mediaEpisode'].update(authorized=True,episodeId=amendment['id'],
                            phase=journal.get('phase'),nativeScope='original_parent_family',variantCount=30,
                            retryIntended=bool(journal.get('intent')),requestAccepted=journal.get('requestAccepted') is True,
                            consumedWithoutWrite=journal.get('consumedWithoutWrite') is True,
                            checkpoint='原父任务单次重试已记录，继续仅核对回执' if journal.get('intent') else '独立媒体episode已保存，普通继续仅只读核对')
                        result['mediaRecovery']['canRetry']=False
                        result['mainImageRepair'].update(canRepair=False,canCorrectSave=False)
                except (ValueError,OSError,KeyError,TypeError,IndexError) as error:
                    result['mediaEpisode']['reason']=str(error) if isinstance(error,ValueError) else '原媒体episode资料需核对'
        except (ValueError, OSError):
            pass
    diagnostic = result.get('error')
    if isinstance(diagnostic, str) and re.match(
            r'^(?:Error:\s*)?(?:EGO_PAGE_[A-Z0-9_]+\b|page\.(?:click|waitForFunction) timed out\b|ElementResolutionError\b)',
            diagnostic.strip()):
        result['error'] = '浏览器操作未取得完整回执，请核对原记录后继续；不会自动重发'
    def online(station):
        observation = result.get(station + 'PublicationObservation') or {}
        return (str(observation.get('publishId')) == str(result.get(station + 'PublishId'))
                and observation.get('verification') == 'accepted' and observation.get('failed') is False
                and observation.get('status') in ONLINE_STATUSES and observation.get('allChildrenOnline') is True
                and type(observation.get('childCount')) is int and observation['childCount'] > 0
                and observation['childCount'] == len(observation_packet['input']['goodsItems']))

    submitted = result.get('status') == 'submitted'
    verified = submitted and scoped_observation and all(online(m.lower()) for m in job.get('markets', ['US', 'UK']))
    result.update(publishSubmitted=submitted, onlineVerified=verified)
    if verified:
        result['phase'] = '目标站点全部子SKU在售已核验'
    return result


ONLINE_STATUSES = {'online', 'active', 'buyable', 'discoverable'}


def online_receipt(user, product, market, job_id, publish_id):
    """Read the existing ERP observation from the account's original task."""
    if market not in ('US', 'UK') or not re.fullmatch(r'[1-9]\d*', publish_id):
        raise ValueError('缺少支持站点的原 ERP 发布编号')
    job = owned(user, job_id)
    if job['productId'] != product or public(job)['status'] in ('queued', 'running'):
        raise ValueError('任务不属于当前商品，或该发布任务仍在读取回执')
    folder = ROOT / job['id']
    try:
        record = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
        source = json.loads((folder / 'input.json').read_text(encoding='utf-8'))['input']
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError('工作台缺少原发布回读资料') from exc
    if (any(data.get(key) != value for data in (source, record) for key, value in
            (('accountId', user), ('productId', product), ('jobId', job_id)))
            or not source.get('sha256') or record.get('snapshotSha256') != source['sha256']):
        raise ValueError('工作台原任务冻结身份不符')
    if market == 'UK':
        _observation_scope(job, record, source)
        source = record['ukPrepare']['preparedInput']
    publication = record.get(market.lower() + 'Publish') or {}
    observation = publication.get('latestObservation') or {}
    online = publication.get('onlineObservation') or {}
    identity = online.get('identity') or {}
    target = source.get('destination') or {}
    parent = source.get('goods', {}).get('parentSku')
    record_id = str(observation.get('recordId') or '')
    store = source.get('store')
    if (str(publication.get('publishId')) != publish_id or str(observation.get('publishId')) != publish_id
            or observation.get('verification') != 'accepted' or observation.get('failed') is not False
            or observation.get('status') not in ONLINE_STATUSES or observation.get('allChildrenOnline') is not True
            or not re.fullmatch(r'[1-9]\d*', record_id) or not store or not parent):
        raise ValueError('原 ERP 发布任务尚无完整在售回执，请只读核对原任务')
    if (str(online.get('recordId')) != record_id or str(identity.get('publishId')) != record_id
            or identity.get('accountId') != user or identity.get('productId') != product
            or identity.get('parentSku') != parent or identity.get('store') != store
            or any(str(identity.get(key)) != str(target.get(key)) for key in ('marketId', 'countryCode'))
            or identity.get('countryCode') != ('US' if market == 'US' else 'GB')):
        raise ValueError('在售回执身份不完整，请只读刷新原工作台任务')
    expected = {row['sku'] for row in source['goodsItems']}
    rows = identity.get('children') or []
    if (not expected or len(rows) != len(expected) or observation.get('childCount') != len(expected)
            or {row.get('sku') for row in rows} != expected
            or any(row.get('status') not in ONLINE_STATUSES
                   or not re.fullmatch(r'[A-Z0-9]{10}', str(row.get('asin') or '')) for row in rows)):
        raise ValueError('在售回执缺少原子体状态或 ASIN，请只读刷新原工作台任务')
    return {'workbench_publication_id': job_id, 'publish_id': publish_id, 'online_record_id': record_id,
            'marketplace': market, 'store': store, 'parent_sku': parent, 'observed_at': observation['observedAt'],
            'children': [{key: row[key] for key in ('sku', 'asin', 'status')} for row in rows]}


def owned(user, jid):
    with connect() as db:
        row = db.execute('SELECT body FROM jobs WHERE account_id=? AND id=?', (user, jid)).fetchone()
    if not row:
        raise ValueError('当前账号找不到此发布任务')
    return json.loads(row[0])


def persist(job, *, handoff=None):
    job['updatedAt'] = time.time()
    with connect() as db, db:
        db.execute('UPDATE jobs SET status=?,body=? WHERE id=? AND account_id=?',
                   (job['status'], json.dumps(job, ensure_ascii=False), job['id'], job['accountId']))
        if handoff:
            import codex_handoff
            codex_handoff.record(db,handoff,job['id'],'observation_queued')


def list_jobs(user, pid=''):
    with connect() as db:
        rows = db.execute('SELECT body FROM jobs WHERE account_id=? ORDER BY rowid DESC', (user,)).fetchall()
    return [public(json.loads(row[0])) for row in rows if not pid or json.loads(row[0])['productId'] == pid]


def view(user, workspace, pid):
    product = collection.product_for(workspace, pid)
    targets = []
    policy = publication_policy.default_followup('US')
    pairs_by_id = {pair['sourceDestinationId']: pair for pair in pairs(user)}
    for destination in collection.destinations(user):
        if destination.get('marketplace') != 'US':
            continue
        pair = pairs_by_id.get(destination['id'], {})
        us_missing = []
        source = None
        try:
            _, source, _ = source_receipt(user, pid, destination['id'])
        except ValueError as error:
            us_missing.append(str(error))
        try:
            us_missing.extend(publication_inputs.us_publication_missing_facts(publication_inputs.publication_facts(product)))
        except ValueError as error:
            us_missing.append(str(error))
        missing = list(us_missing)
        if not policy:
            missing.append('默认跨站发布规则未启用')
        if source is not None:
            missing.extend(publication_inputs.uk_publication_requirements(source, product, pair)['missing'])
        targets.append({'id': destination['id'], 'store': destination['store'], 'target': pair,
                        'sourceReady': source is not None, 'ready': not missing, 'missing': missing,
                        'usReady': not us_missing, 'usMissing': us_missing})
    return {'targets': targets, 'jobs': list_jobs(user, pid), 'policy': policy,
            'commerce': copy.deepcopy(product.get('erpDelivery', {}).get('UK', {})),
            'skuCount': len(collection.eligible_variant_rows(product))}


def update_commerce(workspace, data):
    """Called inside the server's workspace revision transaction; preserve history."""
    if not isinstance(data, dict) or set(data) - {'productId', 'revision', 'details'}:
        raise ValueError('英国经营资料参数无效')
    product = collection.product_for(workspace, data.get('productId'))
    details = data.get('details')
    allowed = {'price', 'variantPrices', 'quantity', 'variantQuantities', 'inventoryPolicy', 'handlingDays'}
    if not isinstance(details, dict) or set(details) - allowed:
        raise ValueError('英国经营资料格式无效')
    skus = [r[0] for r in collection.eligible_variant_rows(product)]
    source = {'marketplace': 'US', 'productId': product['id'], 'store': 'US',
              'goodsItems': [{'sku': sku, 'quantity': '0'} for sku in skus]}
    candidate = copy.deepcopy(product)
    candidate.setdefault('erpDelivery', {})['UK'] = copy.deepcopy(details)
    publication_inputs.uk_publication_inputs(source, candidate,
        {'id': 'validation-only-UK', 'storeName': 'UK', 'shippingTemplate': {'name': 'validation-only'}})
    for key in ('variantPrices', 'variantQuantities'):
        if set(details.get(key, {})) - set(skus):
            raise ValueError('英国经营资料包含本款以外的子SKU')
    old = copy.deepcopy(product.get('erpDelivery', {}).get('UK'))
    product.setdefault('erpDeliveryHistory', []).append({'marketplace': 'UK', 'before': old,
                                                       'updatedAt': time.time()})
    product.setdefault('erpDelivery', {})['UK'] = copy.deepcopy(details)


def _start_scope(user, workspace, data):
    if not isinstance(data, dict) or set(data) - {'productId', 'destinationId', 'confirmed', 'markets'} or data.get('confirmed') is not True:
        raise ValueError('请明确选择“发布美国并接续英国”，草稿采集不授权发布')
    markets = data.get('markets', ['US', 'UK'])
    if markets not in (['US'], ['US', 'UK']):
        raise ValueError('请选择美国，或美国接续英国')
    policy = publication_policy.default_followup('US') if markets == ['US', 'UK'] else None
    if markets == ['US', 'UK'] and not policy:
        raise ValueError('默认跨站发布规则未启用')
    product = collection.product_for(workspace, data.get('productId'))
    ds = [d for d in collection.destinations(user) if d['id'] == data.get('destinationId') and d.get('marketplace') == 'US']
    if len(ds) != 1:
        raise ValueError('请选择本账号美国ERP目标')
    return product, ds[0], markets, policy


def _start_preview(user, workspace, product, destination, markets):
    pair = next((p for p in pairs(user) if p['sourceDestinationId'] == destination['id']), {})
    source_job, source, receipt = source_receipt(user, product['id'], destination['id'])
    with tempfile.TemporaryDirectory(prefix='junfei-publication-check-') as tmp:
        fresh = collection.finished_snapshot(user, product, workspace.get('copy', {}).get(product['id'] + '-US', {}),
                                            destination, Path(tmp), source)
    if collection_inputs.content_digest(fresh) != collection_inputs.content_digest(source):
        raise ValueError('当前成品与美国原草稿不同，请先核对更新原采集记录')
    commerce = publication_inputs.uk_publication_inputs(source, product, pair) if 'UK' in markets else {}
    facts = publication_inputs.publication_facts(product)
    missing_facts = publication_inputs.us_publication_missing_facts(facts)
    if missing_facts:
        raise ValueError('，'.join(missing_facts))
    frozen = copy.deepcopy(source)
    frozen['destination'].update(receipt.get('resolvedDestination', {}), draftId=receipt['draftId'],
                                 destinationId=destination['id'], erpAccountId=source['destination']['erpUserId'])
    frozen['urls'] = {k: (v.get('url') if isinstance(v, dict) else v) for k, v in receipt.get('uploads', {}).items()}
    import store_rotation
    rotation = store_rotation.publication_scope(user, product['id'], destination['id'], markets)
    frozen.update(commerce, publicationMarkets=markets)
    if rotation:
        frozen['rotation'] = rotation
    frozen['publicationFacts'] = facts
    details = {'productId': product['id'], 'productName': product.get('name'),
               'sourceStore': source['store'], 'targetStore': pair['storeName'] if 'UK' in markets else None,
               'markets': markets,
               'destinationId': destination['id'], 'collectionId': source_job['id'],
               'draftId': receipt['draftId'], 'parentSku': source['goods']['parentSku'],
               'variantCount': len(source['goodsItems'])}
    return details, frozen


def _confirmation_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _resume_preview(owner, job, data):
    folder = ROOT / job['id']
    sources = {}
    for name in ('input.json', 'receipt.json'):
        path = folder / name
        if path.is_file():
            sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    details = {key: job.get(key) for key in ('id', 'productId', 'productName', 'sourceStore', 'targetStore',
                                             'destinationId', 'parentSku', 'variantCount', 'phase', 'markets')}
    return {**details, 'digest': _confirmation_digest([owner, 'continue', job, data, sources])}


def preview(owner, workspace, action, data):
    with LOCK:
        if action == 'start':
            product, destination, markets, policy = _start_scope(owner, workspace, data)
            details, frozen = _start_preview(owner, workspace, product, destination, markets)
            return {**details, 'digest': _confirmation_digest([owner, action, details, frozen, destination, policy])}
        if action == 'continue' and isinstance(data, dict) and data.get('id') and 'observeOnly' not in data:
            job = owned(owner, data['id'])
            return _resume_preview(owner, job, data)
    raise ValueError('请选择正式发布或修复操作')


def observe(user, job_id, *, handoff=None):
    return resume(user, {'id': job_id, 'confirmed': True, 'observeOnly': True}, handoff=handoff)


def start(user, workspace, data, *, confirmation=None):
    product, destination, markets, policy = _start_scope(user, workspace, data)
    with LOCK, connect() as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT body FROM jobs WHERE account_id=? AND product_id=? AND destination_id=?',
                           (user, product['id'], destination['id'])).fetchone()
        if prior:
            old_job = json.loads(prior[0])
            if old_job.get('markets', ['US', 'UK']) != markets:
                raise ValueError('原发布任务的站点范围已冻结，请核对原任务')
            return {**public(old_job), 'reused': True}
        details, frozen = _start_preview(user, workspace, product, destination, markets)
        if confirmation is not None and confirmation != _confirmation_digest([user, 'start', details, frozen, destination, policy]):
            raise ValueError('发布预检内容已变化，请重新核对')
        job = {'id': uuid.uuid4().hex, 'accountId': user, 'productId': product['id'],
               'productName': details['productName'], 'destinationId': destination['id'],
               'sourceCollectionId': details['collectionId'], 'sourceStore': details['sourceStore'],
               'targetStore': details['targetStore'], 'parentSku': details['parentSku'],
               'variantCount': details['variantCount'], 'status': 'queued',
               'phase': '等待发布美国' if markets == ['US'] else '等待美国发布与英国接续',
               'createdAt': time.time(), 'runOwner': RUNTIME, 'ownerPid': os.getpid(),
               'policy': policy, 'markets': markets}
        if confirmation is not None:
            job['operatorConfirmation'] = {'accountId': user, 'at': time.time(), 'digest': confirmation}
        frozen['runtime'] = {'pythonExecutable': sys.executable,'bundledValidator':bool(getattr(sys,'frozen',False))}
        frozen['jobId'] = job['id']
        # This exists before a worker is launched; it is never regenerated on resume.
        write_json(ROOT / job['id'] / 'input.json', {'input': frozen, 'connection': destination})
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                   (job['id'], user, product['id'], destination['id'], job['status'], json.dumps(job, ensure_ascii=False)))
        ACTIVE.add(job['id'])
    threading.Thread(target=execute, args=(job,), daemon=True).start()
    return public(job)


def _append_us_pants_rise(job, folder, packet, evidence):
    source = packet['input']
    facts = source.get('publicationFacts') or {}
    receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
    prepared = receipt.get('usPrepare') or {}
    schema = prepared.get('schemaObservation') or {}
    source_url = (facts.get('supplierModelNumberEvidence') or {}).get('sourceUrl')
    choices = {'高腰': 'High Rise', '中腰': 'Mid Rise', '低腰': 'Low Rise'}
    if (not isinstance(evidence, dict) or set(evidence) != {'sourceUrl', 'field', 'sourceValue', 'value', 'reference'}
            or not all(isinstance(value, str) and 0 < len(value) <= 2000 for value in evidence.values())
            or not source_url or evidence['sourceUrl'] != source_url or evidence['field'] != '腰型'
            or choices.get(evidence['sourceValue']) != evidence['value']):
        raise ValueError('美国裤装腰型须提供本品同一货源的原始字段和值')
    if (receipt.get('jobId') != job['id'] or receipt.get('productId') != job['productId']
            or receipt.get('accountId') != job['accountId'] or receipt.get('snapshotSha256') != source.get('sha256')
            or receipt.get('phase') != 'us_preparation' or receipt.get('usPublish')
            or schema.get('country_code') != 'US' or schema.get('product_type_name') != 'PANTS'
            or any(prepared.get(key) for key in ('saveIntendedAt', 'requestSent', 'submitted'))):
        raise ValueError('仅可在尚未保存或发布的原美国裤装任务追加腰型依据')
    amended = copy.deepcopy(packet)
    preparation = amended['input'].setdefault('usPreparation', {})
    if 'pantsRise' in preparation:
        if preparation['pantsRise'] != evidence:
            raise ValueError('已保留的原美国裤装腰型依据有冲突')
        return
    preparation['pantsRise'] = evidence
    amendment_id = uuid.uuid4().hex
    write_json(folder / 'amendments' / (amendment_id + '.json'), {
        'at': time.time(), 'kind': 'us_pants_rise', 'evidence': evidence, 'previousInput': packet})
    write_json(folder / 'input.json', amended)
    job.setdefault('amendments', []).append({'id': amendment_id, 'kind': 'us_pants_rise', 'evidence': evidence})


def _append_uk_sweatshirt_weave(user, job, folder, packet, evidence):
    receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
    original = packet['input']
    prepared = receipt.get('ukPrepare') or {}
    schema = prepared.get('schemaObservation') or {}
    instructions = {'Knit': '针织（Knit）', 'Woven': '梭织（Woven）'}
    if (not isinstance(evidence, dict) or set(evidence) != {'value', 'source', 'instruction', 'productId', 'sourceUrl'}
            or not isinstance(evidence['value'], str) or evidence['value'] not in instructions
            or evidence['source'] != 'user_confirmation' or evidence['instruction'] != instructions[evidence['value']]
            or evidence['productId'] != job['productId']
            or not isinstance(evidence['sourceUrl'], str) or not re.fullmatch(r'https://detail\.1688\.com/offer/\d+\.html', evidence['sourceUrl'])
            or evidence['sourceUrl'] != original.get('goods', {}).get('sourceUrl')
            or receipt.get('jobId') != job['id'] or receipt.get('productId') != job['productId']
            or receipt.get('accountId') != user or receipt.get('snapshotSha256') != original.get('sha256')
            or receipt.get('phase') != 'uk_preparation' or receipt.get('ukPublish')
            or not receipt.get('usPublish', {}).get('publishId') or receipt.get('ukCopy', {}).get('phase') != 'copy_verified'
            or schema.get('country_code') != 'GB' or schema.get('product_type_name') != 'SWEATSHIRT'
            or 'weave_type' not in (receipt.get('error') or '')
            or any(prepared.get(key) for key in ('saveIntendedAt', 'requestSent', 'submitted'))):
        raise ValueError('须在原英国卫衣保存前核对同货源的明确织法确认')
    amended = copy.deepcopy(packet)
    preparation = amended['input'].setdefault('ukPreparation', {})
    prior = preparation.get('sweatshirtWeave')
    if prior and prior != evidence:
        raise ValueError('原英国卫衣织法确认存在冲突')
    if prior:
        return
    preparation['sweatshirtWeave'] = evidence
    amendment_id = uuid.uuid4().hex
    write_json(folder / 'amendments' / (amendment_id + '.json'), {
        'at': time.time(), 'kind': 'uk_sweatshirt_weave', 'evidence': evidence, 'previousInput': packet})
    write_json(folder / 'input.json', amended)
    job.setdefault('amendments', []).append({'id': amendment_id, 'kind': 'uk_sweatshirt_weave', 'evidence': evidence})


def _append_us_sleeve_type(user, job, folder, packet, evidence):
    """Add a reviewed construction fact before the original US Save only."""
    source = packet['input']
    receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
    prepared = receipt.get('usPrepare') or {}
    schema = prepared.get('schemaObservation') or {}
    fields = {'value', 'sourceUrl', 'referenceImage', 'referenceSha256', 'observations'}
    if (not isinstance(evidence, dict) or set(evidence) != fields
            or any(not isinstance(v, str) or not 0 < len(v) <= 2000 for v in evidence.values())
            or evidence['value'] != 'Cuff Sleeve'
            or evidence['sourceUrl'] != source.get('goods', {}).get('sourceUrl')
            or evidence['sourceUrl'] != source.get('publicationFacts', {}).get('supplierModelNumberEvidence', {}).get('sourceUrl')
            or not re.fullmatch(r'/uploads/[a-zA-Z0-9_-]+\.(png|jpg|jpeg|webp)', evidence['referenceImage'])
            or not re.fullmatch(r'[a-f0-9]{64}', evidence['referenceSha256'])
            or job.get('accountId') != user or receipt.get('accountId') != user
            or receipt.get('jobId') != job['id'] or receipt.get('productId') != job['productId']
            or receipt.get('snapshotSha256') != source.get('sha256')
            or receipt.get('phase') != 'us_preparation' or receipt.get('usPublish')
            or schema.get('country_code') != 'US' or schema.get('product_type_name') != 'SHIRT'
            or 'sleeve' not in receipt.get('error', '')
            or any(prepared.get(k) for k in ('saveIntendedAt', 'requestSent', 'submitted'))
            or (app_paths.WORKER_USER and app_paths.WORKER_USER != user)):
        raise ValueError('袖型只能在原美国上衣尚未保存或发布时，凭同款供货照片补充')
    with closing(sqlite3.connect(app_paths.DATA / 'studio.sqlite3')) as db:
        row = db.execute('SELECT body FROM workspace WHERE id=1').fetchone()
    product = collection.product_for(json.loads(row[0]) if row else {}, job['productId'])
    supplier = product.get('sourceImport', {}).get('supplier', {})
    assets = [a for a in product.get('assets', []) if a.get('url') == evidence['referenceImage']
              and a.get('source') == '供应商图']
    if (product.get('sourceUrl') != evidence['sourceUrl'] or supplier.get('url') != evidence['sourceUrl']
            or len(assets) != 1 or assets[0].get('sha256') != evidence['referenceSha256']):
        raise ValueError('袖型参考图未绑定当前账号本款供货资料')
    import image_provider
    if hashlib.sha256(image_provider.local_image(evidence['referenceImage'])).hexdigest() != evidence['referenceSha256']:
        raise ValueError('袖型供货参考图内容变化')
    amended = copy.deepcopy(packet)
    fact = {**evidence, 'source': 'bound_supplier_photo_review', 'reviewerType': 'agent', 'humanReviewed': False}
    preparation = amended['input'].setdefault('usPreparation', {})
    if preparation.get('sleeveType'):
        if preparation['sleeveType'] != fact:
            raise ValueError('已保留袖型依据存在冲突')
        return
    preparation['sleeveType'] = fact
    amendment_id = uuid.uuid4().hex
    write_json(folder / 'amendments' / (amendment_id + '.json'), {
        'at': time.time(), 'kind': 'us_sleeve_type', 'evidence': fact, 'previousInput': packet})
    write_json(folder / 'input.json', amended)
    job.setdefault('amendments', []).append({'id': amendment_id, 'kind': 'us_sleeve_type', 'evidence': fact})


def resume(user, data, *, confirmation=None, handoff=None):
    if isinstance(data, dict) and 'ukCoatWeave' in data:
        if set(data) != {'id', 'confirmed', 'ukCoatWeave'} or data['confirmed'] is not True:
            raise ValueError('英国外套经营默认须单独沿原任务补充')
    if isinstance(data, dict) and 'usCoatFacts' in data:
        if set(data) != {'id', 'confirmed', 'usCoatFacts'} or data['usCoatFacts'] is not True or data['confirmed'] is not True:
            raise ValueError('外套同货源属性须单独沿原任务补充')
    if isinstance(data, dict) and 'usSkirtFacts' in data:
        if set(data) != {'id', 'confirmed', 'usSkirtFacts'} or data['usSkirtFacts'] is not True or data['confirmed'] is not True:
            raise ValueError('短裙供货属性修订须单独提交')
    if isinstance(data, dict) and 'usSleeveType' in data:
        if set(data) != {'id', 'confirmed', 'usSleeveType'} or data['confirmed'] is not True:
            raise ValueError('美国上衣袖型修订须单独提交同款供货照片依据')
    if isinstance(data, dict) and 'ukPantsWeave' in data:
        if set(data) != {'id', 'confirmed', 'ukPantsWeave'} or data['confirmed'] is not True:
            raise ValueError('英国裤装织法修订须单独确认')
    if isinstance(data, dict) and 'usPantsRise' in data:
        if set(data) != {'id', 'confirmed', 'usPantsRise'} or data['confirmed'] is not True:
            raise ValueError('美国裤装腰型修订须单独确认')
    if isinstance(data, dict) and 'ukSweatshirtWeave' in data:
        if set(data) != {'id', 'confirmed', 'ukSweatshirtWeave'} or data['confirmed'] is not True:
            raise ValueError('英国卫衣织法修订须单独确认')
    if not isinstance(data, dict) or set(data) - {'id', 'confirmed', 'ukCoatWeave', 'usCoatFacts', 'usSkirtFacts', 'usSleeveType', 'usPantsRise', 'ukPantsWeave', 'ukSweatshirtWeave', 'ukCategoryPath', 'categoryEvidence', 'ukShippingTemplate', 'shippingEvidence', 'ukWaterResistanceLevel', 'waterResistanceEvidence', 'retryMediaFailures', 'observeOnly', 'repairMainImage', 'retryMainImageSave', 'repairRequiredAttributes', 'requiredAttributeRepair', 'retryMediaEpisode', 'mediaEpisodeEvidence', 'resumeRequiredAttributesPreclick', 'resumeRequiredAttributesRetryPreflight', 'repairIdentifiers', 'identifierEvidence', 'retryUkMediaFailures', 'ukMediaEvidence'} or data.get('confirmed') is not True:
        raise ValueError('请确认从原发布回执继续核对')
    if ('retryUkMediaFailures' in data or 'ukMediaEvidence' in data) and (
            data.get('retryUkMediaFailures') is not True or not isinstance(data.get('ukMediaEvidence'), dict)
            or set(data) != {'id', 'confirmed', 'retryUkMediaFailures', 'ukMediaEvidence'}):
        raise ValueError('原英国媒体恢复须单独明确提交及两份完整研究证据')
    if ('retryMediaEpisode' in data or 'mediaEpisodeEvidence' in data) and (
            data.get('retryMediaEpisode') is not True or not isinstance(data.get('mediaEpisodeEvidence'),dict)
            or set(data)!={'id','confirmed','retryMediaEpisode','mediaEpisodeEvidence'}):
        raise ValueError('独立媒体episode须明确单独提交两份原任务证据，不能与其它恢复或修订合并')
    if ('repairRequiredAttributes' in data or 'requiredAttributeRepair' in data) and (
            data.get('repairRequiredAttributes') is not True or not isinstance(data.get('requiredAttributeRepair'), dict)
            or set(data) != {'id', 'confirmed', 'repairRequiredAttributes', 'requiredAttributeRepair'}):
        raise ValueError('英国必填属性修复必须单独提交完整原事实、证据和显式表示范围')
    if 'resumeRequiredAttributesPreclick' in data and (
            data['resumeRequiredAttributesPreclick'] is not True
            or set(data) != {'id', 'confirmed', 'resumeRequiredAttributesPreclick'}):
        raise ValueError('页面就绪前保存纠正必须明确单独提交，不能合并其他动作')
    if 'resumeRequiredAttributesRetryPreflight' in data and (
            data['resumeRequiredAttributesRetryPreflight'] is not True
            or set(data) != {'id', 'confirmed', 'resumeRequiredAttributesRetryPreflight'}):
        raise ValueError('原英国重试前置纠正必须单独提交，不能合并其他动作')
    if ('repairIdentifiers' in data or 'identifierEvidence' in data) and (
            data.get('repairIdentifiers') is not True or not isinstance(data.get('identifierEvidence'), dict)
            or set(data) != {'id', 'confirmed', 'repairIdentifiers', 'identifierEvidence'}):
        raise ValueError('原英国编码纠正须单独提交原任务完整研究证据')
    if 'retryMediaFailures' in data and data['retryMediaFailures'] is not True:
        raise ValueError('图片失败恢复必须明确选择重试原美国任务')
    if data.get('retryMediaFailures') and set(data) - {'id', 'confirmed', 'retryMediaFailures'}:
        raise ValueError('图片失败恢复不能与英国资料修订合并提交')
    if 'observeOnly' in data and (data['observeOnly'] is not True or set(data) - {'id', 'confirmed', 'observeOnly'}):
        raise ValueError('两站回执只读核对不能与发布恢复或资料修订合并提交')
    if 'repairMainImage' in data and (data['repairMainImage'] is not True or set(data) - {'id', 'confirmed', 'repairMainImage'}):
        raise ValueError('原主图字段修复必须明确单独提交，不能与其他恢复或英国修订合并')
    if 'retryMainImageSave' in data and (data['retryMainImageSave'] is not True or set(data) - {'id', 'confirmed', 'retryMainImageSave'}):
        raise ValueError('原主图重新保存必须明确单独提交，不能与其他恢复或英国修订合并')
    if ('ukWaterResistanceLevel' in data or 'waterResistanceEvidence' in data) and (
            data.get('ukWaterResistanceLevel') != 'not_water_resistant'
            or not isinstance(data.get('waterResistanceEvidence'), str)
            or not data['waterResistanceEvidence'].strip() or len(data['waterResistanceEvidence']) > 2000
            or set(data) - {'id', 'confirmed', 'ukWaterResistanceLevel', 'waterResistanceEvidence'}):
        raise ValueError('英国防水属性只能单独提交已明确选择的not_water_resistant及具体依据')
    with LOCK:
        job = owned(user, data.get('id'))
        if public(job)['status'] in ('queued', 'running'):
            return {**public(job), 'reused': True}
        if confirmation is not None:
            if confirmation != _resume_preview(user, job, data)['digest']:
                raise ValueError('原发布回执已变化，请重新核对')
            job.setdefault('operatorContinuations', []).append(
                {'accountId': user, 'at': time.time(), 'digest': confirmation})
        folder = ROOT / job['id']
        continuation_packet = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
        continuation_source = continuation_packet['input']
        if 'ukCoatWeave' in data:
            import publication_coat_facts
            publication_coat_facts.append_uk_weave(user, job, folder, continuation_packet, data['ukCoatWeave'], write_json)
        if data.get('usCoatFacts'):
            if app_paths.WORKER_USER and app_paths.WORKER_USER != user:
                raise ValueError('外套供货资料账号不符')
            with closing(sqlite3.connect(app_paths.DATA / 'studio.sqlite3')) as db:
                row = db.execute('SELECT body FROM workspace WHERE id=1').fetchone()
            product = collection.product_for(json.loads(row[0]) if row else {}, job['productId'])
            import publication_coat_facts
            publication_coat_facts.append(user, job, folder, continuation_packet, product, write_json)
        if data.get('usSkirtFacts'):
            if app_paths.WORKER_USER and app_paths.WORKER_USER != user:
                raise ValueError('短裙供货资料账号不符')
            with closing(sqlite3.connect(app_paths.DATA / 'studio.sqlite3')) as db:
                row = db.execute('SELECT body FROM workspace WHERE id=1').fetchone()
            product = collection.product_for(json.loads(row[0]) if row else {}, job['productId'])
            import publication_skirt_facts
            publication_skirt_facts.append(user, job, folder, continuation_packet, product, write_json)
        if 'usSleeveType' in data:
            _append_us_sleeve_type(user, job, folder, continuation_packet, data['usSleeveType'])
        if 'usPantsRise' in data:
            _append_us_pants_rise(job, folder, continuation_packet, data['usPantsRise'])
        if 'ukSweatshirtWeave' in data:
            _append_uk_sweatshirt_weave(user, job, folder, continuation_packet, data['ukSweatshirtWeave'])
        if 'ukPantsWeave' in data:
            evidence = data['ukPantsWeave']
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            prepared = receipt.get('ukPrepare') or {}
            schema = prepared.get('schemaObservation') or {}
            if (not isinstance(evidence, dict) or set(evidence) != {'value', 'source', 'instruction', 'productId'}
                    or evidence['value'] != 'Satin' or evidence['source'] != 'user_confirmation'
                    or evidence['instruction'] != '缎纹（Satin）' or evidence['productId'] != job['productId']
                    or receipt.get('jobId') != job['id'] or receipt.get('productId') != job['productId']
                    or receipt.get('accountId') != user or receipt.get('snapshotSha256') != continuation_source.get('sha256')
                    or receipt.get('phase') != 'uk_preparation' or receipt.get('ukPublish')
                    or receipt.get('ukCopy', {}).get('phase') != 'copy_verified'
                    or schema.get('country_code') != 'GB' or schema.get('product_type_name') != 'PANTS'
                    or any(prepared.get(key) for key in ('saveIntendedAt', 'requestSent', 'submitted'))):
                raise ValueError('须在原英国裤装保存前核对本品明确的缎纹确认')
            amended = copy.deepcopy(continuation_packet)
            preparation = amended['input'].setdefault('ukPreparation', {})
            if preparation.get('pantsWeave') not in (None, evidence):
                raise ValueError('原英国织法确认存在冲突')
            if preparation.get('pantsWeave') is None:
                preparation['pantsWeave'] = evidence
                amendment_id = uuid.uuid4().hex
                write_json(folder / 'amendments' / (amendment_id + '.json'), {
                    'at': time.time(), 'kind': 'uk_pants_weave', 'evidence': evidence, 'previousInput': continuation_packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': amendment_id, 'kind': 'uk_pants_weave', 'evidence': evidence})
        prior_uk_media = continuation_source.get('publicationUkMediaRecovery')
        if data.get('retryUkMediaFailures') or (prior_uk_media and set(data) <= {'id', 'confirmed'}):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            if prior_uk_media:
                if data.get('retryUkMediaFailures'):
                    raise ValueError('原英国已有唯一媒体恢复授权，普通继续只读核对原检查点')
                uk_media.validate(job, receipt, continuation_source)
                observe_media = True
            else:
                amendment = uk_media.freeze(job, receipt, continuation_source, PROJECT.parent, data['ukMediaEvidence'])
                amended = copy.deepcopy(continuation_packet)
                amended['input']['publicationUkMediaRecovery'] = amendment
                write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                    'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                    'authorization': amendment, 'previousInput': continuation_packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                    'publishId': amendment['publishId'], 'scope': 'original_parent_family', 'variantCount': 75})
                observe_media = False
            for flag in ('observationOnly', 'identifierRepairOnly', 'requiredAttributeRepairOnly', 'mediaEpisodeOnly', 'mediaEpisodeObserveOnly'):
                job.pop(flag, None)
            job.update(ukMediaOnly=True, ukMediaObserveOnly=observe_media)
            job.setdefault('history', []).append({'error': job.pop('error', None), 'resumedAt': time.time()})
            job.update(status='queued', phase='核对原英国媒体并仅重试原父任务（75个SKU）', runOwner=RUNTIME, ownerPid=os.getpid())
            persist(job); ACTIVE.add(job['id'])
            threading.Thread(target=execute, args=(job,), daemon=True).start()
            return public(job)
        if prior_uk_media and not data.get('observeOnly'):
            raise ValueError('原英国媒体恢复已冻结，只沿原检查点或两站原回执核对')
        prior_identifiers = continuation_source.get('publicationIdentifierRepair')
        if data.get('repairIdentifiers') or (prior_identifiers and set(data) <= {'id', 'confirmed'}):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            if prior_identifiers:
                if data.get('repairIdentifiers'):
                    raise ValueError('已有唯一编码纠正授权；普通继续只沿原检查点核对')
                identifiers.validate_frozen(job, receipt, continuation_source)
            else:
                evidence, reference = identifiers.read_evidence(PROJECT.parent, data['identifierEvidence'])
                scope = identifiers.freeze_scope(job, receipt, continuation_source, evidence, reference)
                amendment = {**scope, 'id': uuid.uuid4().hex, 'marketplace': 'UK',
                             'scope': identifiers.SCOPE, 'authorizedAt': time.time()}
                amended = copy.deepcopy(continuation_packet)
                amended['input']['publicationIdentifierRepair'] = amendment
                write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                    'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                    'authorization': amendment, 'previousInput': continuation_packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                    'publishId': amendment['publishId'], 'childCount': len(amendment['children'])})
            for flag in ('observationOnly', 'requiredAttributeRepairOnly', 'mediaEpisodeOnly', 'mediaEpisodeObserveOnly'):
                job.pop(flag, None)
            job['identifierRepairOnly'] = True
            job.setdefault('history', []).append({'error': job.pop('error', None), 'resumedAt': time.time()})
            job.update(status='queued', phase='核对英国登录并纠正原任务商品编码', runOwner=RUNTIME, ownerPid=os.getpid())
            persist(job);ACTIVE.add(job['id'])
            threading.Thread(target=execute, args=(job,), daemon=True).start()
            return public(job)
        if prior_identifiers and not data.get('observeOnly'):
            raise ValueError('已有独立编码纠正，普通继续只核对该检查点或两站原回执')
        prior_episodes = continuation_source.get('publicationMediaEpisodes')
        if (public(job)['status']=='submitted' and not data.get('observeOnly')
                and not data.get('retryMediaEpisode') and not data.get('resumeRequiredAttributesPreclick')
                and not data.get('resumeRequiredAttributesRetryPreflight') and not prior_episodes):
            return {**public(job),'reused':True}
        if data.get('retryMediaEpisode') or (prior_episodes and set(data)<={'id','confirmed'}):
            receipt=json.loads((folder/'receipt.json').read_text(encoding='utf-8'))
            if prior_episodes:
                if data.get('retryMediaEpisode'):
                    raise ValueError('原任务已有唯一媒体episode，不能再次授权；普通继续只读核对原journal')
                if not isinstance(prior_episodes,list) or len(prior_episodes)!=1:
                    raise ValueError('原媒体episode记录需核对，不能替代或新建')
                media_episode.validate_media_episode(job,receipt,continuation_source,prior_episodes[0])
                observe_episode=True
            else:
                anchor,readcheck,evidence=media_episode.read_episode_evidence(PROJECT.parent,folder/'receipt.json',
                    job,receipt,continuation_source,data['mediaEpisodeEvidence'])
                amendment=media_episode.build_media_episode(job,receipt,continuation_source,
                    failure_anchor=anchor,media_evidence=readcheck)
                amendment['evidence']=evidence
                amended=copy.deepcopy(continuation_packet)
                amended['input']['publicationMediaEpisodes']=[amendment]
                write_json(folder/'amendments'/(amendment['id']+'.json'),{'kind':amendment['scope'],
                    'at':amendment['authorizedAt'],'authorization':amendment,'previousInput':continuation_packet})
                write_json(folder/'input.json',amended)
                job.setdefault('amendments',[]).append({'id':amendment['id'],'kind':amendment['scope'],
                    'publishId':amendment['publishId'],'scope':'original_parent_family','variantCount':30})
                observe_episode=False
            job.pop('observationOnly',None);job.pop('requiredAttributeRepairOnly',None)
            job.update(mediaEpisodeOnly=True,mediaEpisodeObserveOnly=observe_episode)
            job.setdefault('history',[]).append({'error':job.pop('error',None),'resumedAt':time.time()})
            job.update(status='queued',phase='核对原美国媒体episode（原父任务30个SKU）',runOwner=RUNTIME,ownerPid=os.getpid())
            persist(job);ACTIVE.add(job['id'])
            threading.Thread(target=execute,args=(job,),daemon=True).start()
            return public(job)
        if prior_episodes:
            if not data.get('observeOnly') and not data.get('repairRequiredAttributes'):
                raise ValueError('已有独立媒体episode，普通继续仅核对该journal，不能再调用旧媒体或MAIN恢复')
            if data.get('repairRequiredAttributes'):
                job.pop('mediaEpisodeOnly',None);job.pop('mediaEpisodeObserveOnly',None)
        if continuation_source.get('publicationRequiredAttributeRepair'):
            continuation_receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            required_attributes.validate_frozen(job, continuation_receipt, continuation_source)
            if set(data) - {'id', 'confirmed', 'observeOnly', 'repairRequiredAttributes', 'requiredAttributeRepair', 'resumeRequiredAttributesPreclick', 'resumeRequiredAttributesRetryPreflight'}:
                raise ValueError('原英国必填修复已冻结，不能合并其它写入恢复')
            job['requiredAttributeRepairOnly'] = not data.get('observeOnly', False)
        if data.get('resumeRequiredAttributesRetryPreflight'):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            scope = required_attributes.freeze_retry_preflight_recovery(job, receipt, continuation_source)
            amendment = {**scope, 'id': uuid.uuid4().hex, 'marketplace': 'UK',
                         'scope': 'resume_original_required_attributes_retry_preflight', 'authorizedAt': time.time()}
            amended = copy.deepcopy(continuation_packet)
            amended['input']['publicationRequiredAttributeRepair']['retryPreflightRecovery'] = amendment
            write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                'authorization': amendment, 'previousInput': continuation_packet})
            write_json(folder / 'input.json', amended)
            continuation_packet, continuation_source = amended, amended['input']
            job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                'publishId': amendment['publishId'], 'childCount': len(amendment['children'])})
            job['requiredAttributeRepairOnly'] = True
            job.pop('observationOnly', None)
        if data.get('resumeRequiredAttributesPreclick'):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            scope = required_attributes.freeze_preclick_recovery(job, receipt, continuation_source)
            amendment = {**scope, 'id': uuid.uuid4().hex, 'marketplace': 'UK',
                         'scope': 'resume_original_required_attributes_preclick_save', 'authorizedAt': time.time()}
            amended = copy.deepcopy(continuation_packet)
            amended['input']['publicationRequiredAttributeRepair']['preclickSaveRecovery'] = amendment
            write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                'authorization': amendment, 'previousInput': continuation_packet})
            write_json(folder / 'input.json', amended)
            continuation_packet, continuation_source = amended, amended['input']
            job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                'publishId': amendment['publishId'], 'childCount': len(amendment['children'])})
            job['requiredAttributeRepairOnly'] = True
            job.pop('observationOnly', None)
        if data.get('repairRequiredAttributes'):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            prior = continuation_source.get('publicationRequiredAttributeRepair')
            if prior:
                if data['requiredAttributeRepair'] != {key: prior[key] for key in ('sourceFingerprint', 'schemaHash', 'facts', 'representation', 'evidence')}:
                    raise ValueError('既有英国必填修复范围不能替换或重新授权')
            else:
                _observation_scope(job, receipt, continuation_source)
                scope = required_attributes.freeze_scope(job, receipt, continuation_source, data['requiredAttributeRepair'])
                amendment = {**scope, 'id': uuid.uuid4().hex, 'marketplace': 'UK',
                             'scope': 'repair_original_failed_required_attributes', 'authorizedAt': time.time()}
                amended = copy.deepcopy(continuation_packet)
                amended['input']['publicationRequiredAttributeRepair'] = amendment
                write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                    'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                    'authorization': amendment, 'previousInput': continuation_packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                                                         'publishId': amendment['publishId'], 'childCount': len(amendment['children'])})
            job['requiredAttributeRepairOnly'] = True
            job.pop('observationOnly', None)
        if continuation_source.get('publicationMainImageRepair'):
            continuation_receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            _validate_main_image_amendment(job, continuation_receipt, continuation_source)
            if 'retryMediaFailures' in data:
                raise ValueError('原主图修复已冻结，不再次启用旧图片重试')
            if any(key in data for key in ('ukCategoryPath', 'ukShippingTemplate', 'ukWaterResistanceLevel')):
                us = continuation_receipt.get('usPublish') or {}
                latest = us.get('latestObservation') or {}
                resolution = us.get('mainImageRepair') or {}
                if ((resolution.get('saveVerified') is not True and resolution.get('resolvedNoWriteVerified') is not True)
                        or us.get('phase') != 'publishing_verified' or latest.get('verification') != 'accepted'
                        or latest.get('failed') is not False):
                    raise ValueError('原主图处理结果和美国最新受理尚未核实，暂不能修订后续英国资料')
        if data.get('retryMainImageSave'):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            main = continuation_source.get('publicationMainImageRepair')
            if not main:
                raise ValueError('尚无原主图修复授权，不能建立重新保存检查点')
            if not main.get('correctiveSave'):
                scope = _main_image_corrective_save_scope(job, receipt, continuation_source)
                corrective = {**scope, 'id': uuid.uuid4().hex, 'scope': 'explicit_idempotent_original_main_resave',
                              'authorizedAt': time.time()}
                amended = copy.deepcopy(continuation_packet)
                amended['input']['publicationMainImageRepair']['correctiveSave'] = corrective
                write_json(folder / 'amendments' / (corrective['id'] + '.json'), {
                    'kind': corrective['scope'], 'at': corrective['authorizedAt'],
                    'authorization': corrective, 'previousInput': continuation_packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': corrective['id'], 'kind': corrective['scope'],
                    'publishId': corrective['publishId'], 'originalMainAmendmentId': main['id']})
            job.pop('observationOnly', None)
        if data.get('repairMainImage'):
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            if not continuation_source.get('publicationMainImageRepair'):
                if (receipt.get('usPublish') or {}).get('mainImageRepair'):
                    raise ValueError('已有原主图修复检查点，不能建立另一份授权')
                scope = _main_image_repair_scope(job, receipt, continuation_source)
                amendment = {**scope, 'id': uuid.uuid4().hex, 'jobId': job['id'], 'marketplace': 'US',
                             'scope': 'repair_original_failed_main_image', 'authorizedAt': time.time()}
                amended = copy.deepcopy(continuation_packet)
                amended['input']['publicationMainImageRepair'] = amendment
                write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                    'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                    'authorization': amendment, 'previousInput': continuation_packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                                                         'publishId': amendment['publishId'], 'failedSku': amendment['failedSku']})
            job.pop('observationOnly', None)
        if data.get('observeOnly'):
            folder = ROOT / job['id']
            packet = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            _observation_scope(job, receipt, packet['input'])
            job['observationOnly'] = True
        if data.get('retryMediaFailures'):
            job.pop('observationOnly', None)
            folder = ROOT / job['id']
            packet = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            source = packet['input']
            prior = source.get('publicationRecovery')
            if prior:
                if (prior.get('scope') != 'retry_original_failed_media' or prior.get('marketplace') != 'US'
                        or prior.get('jobId') != job['id'] or prior.get('accountId') != user
                        or prior.get('productId') != job['productId'] or prior.get('snapshotSha256') != source.get('sha256')
                        or prior.get('publishId') != str((receipt.get('usPublish') or {}).get('publishId') or '')):
                    raise ValueError('既有图片恢复授权与原任务不符，只能核对原记录')
            else:
                scope = _media_retry_scope(job, receipt, source)
                amendment = {**scope, 'id': uuid.uuid4().hex, 'jobId': job['id'],
                             'scope': 'retry_original_failed_media', 'marketplace': 'US', 'authorizedAt': time.time()}
                amended = copy.deepcopy(packet)
                amended['input']['publicationRecovery'] = amendment
                write_json(folder / 'amendments' / (amendment['id'] + '.json'), {
                    'kind': amendment['scope'], 'at': amendment['authorizedAt'],
                    'authorization': amendment, 'previousInput': packet})
                write_json(folder / 'input.json', amended)
                job.setdefault('amendments', []).append({'id': amendment['id'], 'kind': amendment['scope'],
                                                         'publishId': amendment['publishId']})
        if 'ukShippingTemplate' in data:
            template, evidence = data['ukShippingTemplate'], data.get('shippingEvidence')
            if not isinstance(template, str) or not template.strip() or len(template)>200 or not isinstance(evidence, str) or not evidence.strip() or len(evidence)>2000:
                raise ValueError('英国实际运输模板名称和页面证据不完整')
            folder=ROOT/job['id'];receipt=json.loads((folder/'receipt.json').read_text(encoding='utf-8'));preparation=receipt.get('ukPrepare') or {}
            if receipt.get('ukCopy',{}).get('phase')!='copy_verified' or preparation.get('saveIntendedAt') or preparation.get('preparedInput') or receipt.get('ukPublish',{}).get('intendedAt'):
                raise ValueError('英国草稿已经保存或发布，不能改变本次模板')
            packet=json.loads((folder/'input.json').read_text(encoding='utf-8'));amended=copy.deepcopy(packet)
            preparation_input=amended['input'].setdefault('ukPreparation',{})
            if not isinstance(preparation_input,dict):raise ValueError('英国冻结准备资料格式需核对')
            preparation_input['shippingTemplate']={'name':template.strip()}
            amendment_id=uuid.uuid4().hex
            write_json(folder/'amendments'/(amendment_id+'.json'),{'at':time.time(),'kind':'uk_shipping_template','name':template.strip(),'evidence':evidence.strip(),'previousInput':packet})
            write_json(folder/'input.json',amended)
            job.setdefault('amendments',[]).append({'id':amendment_id,'kind':'uk_shipping_template','name':template.strip(),'evidence':evidence.strip()})
        if 'ukWaterResistanceLevel' in data:
            folder=ROOT/job['id'];receipt=json.loads((folder/'receipt.json').read_text(encoding='utf-8'))
            preparation=receipt.get('ukPrepare') or {};publication=receipt.get('ukPublish') or {}
            us=receipt.get('usPublish') or {};latest=us.get('latestObservation') or {}
            packet=json.loads((folder/'input.json').read_text(encoding='utf-8'));source=packet['input']
            if (receipt.get('accountId')!=user or receipt.get('productId')!=job['productId'] or receipt.get('jobId')!=job['id']
                    or receipt.get('snapshotSha256')!=source.get('sha256')
                    or source.get('accountId')!=user or source.get('productId')!=job['productId'] or source.get('jobId')!=job['id']
                    or receipt.get('ukCopy',{}).get('phase')!='copy_verified'
                    or not re.fullmatch(r'[1-9]\d*',str(receipt.get('ukCopy',{}).get('draftId') or ''))
                    or any(preparation.get(key) for key in ('preparedInput','saveIntendedAt','requestSent','submitted','response'))
                    or any(key in publication for key in ('intent','publishId','intendedAt','nativeRequests'))
                    or us.get('phase')!='publishing_verified' or latest.get('verification')!='accepted' or latest.get('failed') is not False
                    or not re.fullmatch(r'[1-9]\d*',str(us.get('publishId') or ''))
                    or str(latest.get('publishId'))!=str(us.get('publishId'))):
                raise ValueError('仅可在美国最新受理、原英国复制已核实且尚未冻结保存或发布时补充英国防水属性')
            amended=copy.deepcopy(packet);preparation_input=amended['input'].setdefault('ukPreparation',{})
            if not isinstance(preparation_input,dict):raise ValueError('英国冻结准备资料格式需核对')
            preparation_input['waterResistanceLevel']=data['ukWaterResistanceLevel']
            amendment_id=uuid.uuid4().hex;evidence=data['waterResistanceEvidence'].strip()
            write_json(folder/'amendments'/(amendment_id+'.json'),{'at':time.time(),'kind':'uk_water_resistance_level',
                'level':data['ukWaterResistanceLevel'],'evidence':evidence,'previousInput':packet})
            write_json(folder/'input.json',amended)
            job.setdefault('amendments',[]).append({'id':amendment_id,'kind':'uk_water_resistance_level',
                'level':data['ukWaterResistanceLevel'],'evidence':evidence})
        if 'ukCategoryPath' in data:
            path, evidence = data['ukCategoryPath'], data.get('categoryEvidence')
            if not isinstance(path, str) or not path.strip() or len(path) > 1000 or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
                raise ValueError('英国准确类目路径和实际候选证据不完整')
            folder = ROOT / job['id']
            receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            preparation = receipt.get('ukPrepare') or {}
            if ('ERP类目推荐有歧义' not in str(job.get('error', '')) or receipt.get('ukCopy', {}).get('phase') != 'copy_verified'
                    or preparation.get('saveIntendedAt') or preparation.get('preparedInput') or receipt.get('ukPublish', {}).get('intendedAt')):
                raise ValueError('仅可在英国分类歧义且尚未保存或发布时补充实际路径')
            packet = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
            amended = copy.deepcopy(packet)
            amended['input']['ukTarget']['categoryHints'] = {'path': path.strip()}
            amendment = {'at': time.time(), 'kind': 'uk_category_path', 'path': path.strip(), 'evidence': evidence.strip(), 'previousInput': packet}
            amendment_id = uuid.uuid4().hex
            write_json(folder / 'amendments' / (amendment_id + '.json'), amendment)
            write_json(folder / 'input.json', amended)
            job.setdefault('amendments', []).append({'id': amendment_id, 'kind': 'uk_category_path', 'path': path.strip(), 'evidence': evidence.strip()})
        job.setdefault('history', []).append({'error': job.pop('error', None), 'resumedAt': time.time()})
        job.update(status='queued', phase=('核对两站最新回执（只读）' if job.get('observationOnly') else '从原发布检查点核对'), runOwner=RUNTIME, ownerPid=os.getpid())
        if handoff:
            persist(job, handoff=handoff)
        else:
            persist(job)
        ACTIVE.add(job['id'])
    threading.Thread(target=execute, args=(job,), daemon=True).start()
    return public(job)


def run_browser(job, connection, phase):
    folder = ROOT / job['id']
    script = folder / ('run-' + phase + '-' + uuid.uuid4().hex + '.mjs')
    module = PROJECT / 'junfei-publisher/ego-publication-workflow.mjs'
    atomic_module = PROJECT / 'junfei-publisher/atomic_write_json.mjs'
    # All filesystem paths are encoded as JS string literals, never shell fragments.
    script.write_text('const fs=await import("node:fs/promises");\n'
        + 'const packet=JSON.parse(await fs.readFile(' + json.dumps(str(folder / 'input.json')) + ',"utf8"));\n'
        + 'const path=' + json.dumps(str(folder / 'receipt.json')) + ';\n'
        + 'let record={};try{record=JSON.parse(await fs.readFile(path,"utf8"))}catch(e){if(e.code!=="ENOENT")throw e;}\n'
        + 'const {writeJsonAtomic}=await import(' + json.dumps(atomic_module.as_uri()) + ');\n'
        + 'const persist=async()=>writeJsonAtomic(path,record);\n'
        + 'const {runPublicationWorkflow}=await import(' + json.dumps(module.as_uri()) + ');\n'
        + 'await runPublicationWorkflow(globalThis.amazonTask.page(' + json.dumps(connection['page'])
        + '),packet.input,record,persist,{phase:' + json.dumps(phase)
        + ',mediaEpisodeObserveOnly:' + json.dumps(bool(job.get('mediaEpisodeObserveOnly')))
        + ',ukMediaObserveOnly:' + json.dumps(bool(job.get('ukMediaObserveOnly'))) + '});\n', encoding='utf-8')
    browser_timeout = 300 if phase == 'uk_media_recovery' else 1200
    command = [sys.executable, '-X', 'utf8', str(PROJECT / 'automation/ego_browser.py'), 'run', str(script),
               '--timeout', str(browser_timeout), '--binding', connection['browserBinding'], '--application-job']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    job['browserPid'] = process.pid
    persist(job)
    try:
        stdout, stderr = process.communicate(timeout=browser_timeout + 20)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
        else:
            process.kill()
        process.communicate()
        raise ValueError('执行超时，保留原任务号；继续时只核对原回执') from None
    (folder / (script.stem + '.log')).write_bytes(stdout + stderr)
    job.pop('browserPid', None)
    path = folder / 'receipt.json'
    if not path.is_file():
        raise ValueError('未取得发布回执，保留原任务等待核对')
    record = json.loads(path.read_text(encoding='utf-8'))
    if process.returncode:
        current_error = current_browser_error(
            stdout, stderr, connection['browserBinding'], hashlib.sha256(script.read_bytes()).hexdigest())
        raise ValueError(str(current_error or record.get('error') or '发布链路未完成，原检查点已保留；请核对ERP后继续')[:1500])
    return record


def market_dom_click_authorized(job, connection, market):
    """An account/store/market-specific permission for the input method only."""
    owner = job.get('accountId')
    if (type(owner) is not int or owner < 1 or connection.get('accountId') != owner
            or not isinstance(connection.get('storeId'), str)
            or not re.fullmatch(r'[1-9]\d*', connection['storeId'])
            or not isinstance(connection.get('seller'), str) or not connection['seller']
            or market != 'UK'):
        return False
    try:
        policy = json.loads(MARKET_DOM_AUTHORIZATIONS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if (not isinstance(policy, dict) or type(policy.get('version')) is not int or policy['version'] != 1
            or not isinstance(policy.get('authorizations'), list)):
        return False
    for entry in policy['authorizations']:
        if (not isinstance(entry, dict) or type(entry.get('accountId')) is not int
                or entry.get('accountId') != owner or entry.get('storeId') != connection['storeId']
                or entry.get('seller') != connection['seller'] or entry.get('market') != market
                or entry.get('allowMarketDomClick') is not True
                or not isinstance(entry.get('source'), str) or not entry['source'].strip()
                or not isinstance(entry.get('authorizedAt'), str)):
            continue
        try:
            authorized_at = datetime.fromisoformat(entry['authorizedAt'])
        except ValueError:
            continue
        if authorized_at.utcoffset() is not None:
            return True
    return False


def login(job, connection, market):
    command = [sys.executable, '-X', 'utf8', str(PROJECT / 'automation/ziniao_plugin.py'),
               '--store-id', str(connection['storeId']), '--store-name', connection['storeName'],
               '--market', market, '--seller', connection['seller'], '--account', connection['erpAccount'],
               '--ensure-site', '--receipt', str(ROOT / job['id'] / ('login-' + market + '.json'))]
    if market_dom_click_authorized(job, connection, market):
        command.append('--allow-market-dom-click')
    result = subprocess.run(command, capture_output=True, timeout=150,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(market + '店铺、站点或插件登录未通过，原发布记录保留')


def validate_submitted(record, input):
    if (record.get('accountId') != input['accountId'] or record.get('productId') != input['productId']
            or record.get('snapshotSha256') != input['sha256']):
        raise ValueError('发布回执与当前账号产品快照不同')
    markets = input.get('publicationMarkets', ['US', 'UK'])
    for key in (market.lower() + 'Publish' for market in markets):
        r = record.get(key, {})
        if (r.get('phase') != 'publishing_verified' or r.get('status') not in ('accepted', 'publishing')
                or not re.fullmatch(r'[1-9]\d*', str(r.get('publishId', '')))):
            raise ValueError('美国或英国尚未取得真实发布中任务号')
        identity = r.get('identity', {})
        expected_country = 'US' if key == 'usPublish' else 'GB'
        expected_store = input['store'] if key == 'usPublish' else input['ukTarget']['storeName']
        expected_destination = input['destination']['destinationId'] if key == 'usPublish' else input['ukTarget']['id']
        if (identity.get('countryCode') != expected_country or identity.get('store') != expected_store
                or identity.get('accountId') != input['accountId'] or identity.get('productId') != input['productId']
                or identity.get('destinationId') != expected_destination):
            raise ValueError('发布结果账号、产品、店铺或站点身份不一致')
        publication_policy.validate_copy_skus(input['goods']['parentSku'], [v['sku'] for v in input['goodsItems']],
            identity.get('parentSku'), [v.get('sku') for v in identity.get('children', [])])
        expected_children = {v['sku']: (v['color'], v['size']) for v in input['goodsItems']}
        if any(expected_children.get(v['sku']) != (v.get('color'), v.get('size')) for v in identity['children']):
            raise ValueError('发布结果SKU与原颜色尺码对应不一致')
    if 'UK' in markets and (not record.get('ukCopy', {}).get('draftId') or record.get('ukPrepare', {}).get('phase') != 'uk_draft_verified'):
        raise ValueError('英国复制与草稿核对回执未齐全')


def rotation_receipt(owner, job_id, product, markets, scope):
    """Return exact accepted task IDs bound to this assignment and store."""
    job = owned(owner, job_id)
    target = scope['target']
    if (job['productId'] != product or job.get('markets', ['US', 'UK']) != markets
            or job['destinationId'] != target['destinationId'] or public(job)['status'] != 'submitted'):
        raise ValueError('原发布任务未受理或与当前轮转商品及目标不符')
    folder = ROOT / job_id
    try:
        source = json.loads((folder / 'input.json').read_text(encoding='utf-8'))['input']
        record = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
    except (OSError, KeyError, ValueError) as error:
        raise ValueError('尚未取得原任务冻结输入及发布回执') from error
    if (source.get('jobId') != job_id or record.get('jobId') != job_id or source.get('rotation') != scope
            or source.get('publicationMarkets') != markets or source.get('store') != target['store']
            or ('UK' in markets and source.get('ukTarget', {}).get('id') != target['ukDestinationId'])):
        raise ValueError('发布回执与当前轮转位置的快照不同')
    validate_submitted(record, source)
    ids = {}
    for market in markets:
        receipt = record[market.lower() + 'Publish']
        latest = receipt.get('latestObservation') or {}
        if latest and (latest.get('failed') is True or latest.get('verification') == 'failed'):
            raise ValueError('原发布任务的最新回执显示失败，暂不计数')
        ids[market] = str(receipt['publishId'])
    return {'publicationId': job_id, 'publishIds': ids, 'countedAt': time.time()}


def execute(job):
    try:
        packet = json.loads((ROOT / job['id'] / 'input.json').read_text(encoding='utf-8'))
        source, connection = packet['input'], packet['connection']
        single_us = source.get('publicationMarkets') == ['US']
        job.update(status='running', phase='等待专属ERP浏览器')
        persist(job)
        with collection.browser_lease(connection['browserBinding']):
            path = ROOT / job['id'] / 'receipt.json'
            record = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            if job.get('ukMediaOnly') and not job.get('observationOnly'):
                uk_media.validate(job, record, source)
                job['phase'] = '核对原英国媒体；保留原75个SKU及所有旧检查点'
                persist(job)
                if not job.get('ukMediaObserveOnly'):
                    login(job, connection, 'UK')
                record = run_browser(job, connection, 'uk_media_recovery')
                job.update(status='needs_reconciliation', phase='原英国媒体已核对；待核对两站最新回执',
                           ukPublishId=record.get('ukPublish', {}).get('publishId'))
                return
            if job.get('identifierRepairOnly') and not job.get('observationOnly'):
                identifiers.validate_frozen(job, record, source)
                job['phase'] = '核对英国登录并纠正原任务商品编码'
                persist(job)
                login(job, connection, 'UK')
                record = run_browser(job, connection, 'uk_identifiers')
                job.update(status='needs_reconciliation', phase='原英国编码已核对；待核对两站最新回执',
                           ukPublishId=record.get('ukPublish', {}).get('publishId'))
                return
            if job.get('mediaEpisodeOnly') and not job.get('observationOnly'):
                episodes=source.get('publicationMediaEpisodes')
                if not isinstance(episodes,list) or len(episodes)!=1:
                    raise ValueError('缺少唯一原美国媒体episode授权')
                media_episode.validate_media_episode(job,record,source,episodes[0])
                job['phase']='核对原美国媒体episode；保留英国原任务'
                persist(job)
                record=run_browser(job,connection,'us_media_episode')
                job.update(status='needs_reconciliation',phase='原美国媒体episode已核对；待核对两站最新回执',
                           usPublishId=record.get('usPublish',{}).get('publishId'))
                return
            if job.get('requiredAttributeRepairOnly') and not job.get('observationOnly'):
                required_attributes.validate_frozen(job, record, source)
                job['phase'] = '核对英国登录并修复原任务必填属性'
                persist(job)
                login(job, connection, 'UK')
                record = run_browser(job, connection, 'uk_required_attributes')
                job.update(status='needs_reconciliation', phase='原英国必填属性已核对；待核对两站最新回执',
                           ukPublishId=record.get('ukPublish', {}).get('publishId'))
                return
            if job.get('observationOnly'):
                _observation_scope(job, record, source)
                job['phase'] = '只读核对目标站点原发布任务最新回执'
                persist(job)
                record = run_browser(job, connection, 'observe')
                publications = [record.get('usPublish') or {}]
                if not single_us:
                    publications.append(record.get('ukPublish') or {})
                if any((publication.get('latestObservation') or {}).get('verification') != 'accepted'
                       or (publication.get('latestObservation') or {}).get('failed') is not False
                       for publication in publications):
                    raise ValueError('两站尚未取得最新成功核对回执，保留只读观察模式')
                validate_submitted(record, source)
                job.update(status='submitted', phase='目标站点最新发布回执已核对',
                           usPublishId=record['usPublish']['publishId'])
                if not single_us:
                    job.update(ukDraftId=record['ukCopy']['draftId'], ukPublishId=record['ukPublish']['publishId'])
                return
            us = record.get('usPublish') or {}
            reconcile_us = any(us.get(key) for key in ('publishId', 'intent', 'observedCandidate'))
            if not reconcile_us:
                # A damaged saved publication is not permission to submit it
                # again. Only a never-submitted draft needs a fresh US login.
                if (any(us.get(key) for key in ('nativeRequests', 'identity', 'sourceEvidence',
                                               'sourceLink', 'failureObservation', 'latestObservation'))
                        or us.get('phase') in ('publish_unknown', 'publishing_verified', 'publication_failed')
                        or us.get('status') in ('accepted', 'publishing', 'failed')):
                    raise ValueError('原美国发布检查点缺少任务关联，不重新登录或提交')
                job['phase'] = '核对美国登录并提交、回读发布任务'
                persist(job)
                login(job, connection, 'US')
            else:
                job['phase'] = ('核对图片并恢复原美国任务' if source.get('publicationRecovery')
                                else '只读复核原美国发布任务，接续前检查实际状态')
                persist(job)
            # Reconcile the original ID before UK work. Writes require the
            # separately frozen publicationRecovery authorization and native
            # one-shot intent; ordinary continuation remains a readback.
            record = run_browser(job, connection, 'us')
            us = record.get('usPublish', {})
            latest = us.get('latestObservation') or us.get('failureObservation')
            verified_online = bool(latest and latest.get('verification') == 'accepted'
                                   and latest.get('failed') is False
                                   and latest.get('allChildrenOnline') is True
                                   and latest.get('status') in ('online', 'buyable', 'discoverable', 'active'))
            if (us.get('phase') == 'publication_failed'
                    or (latest and (latest.get('failed') is True or latest.get('verification') == 'failed'
                                    or (latest.get('status') not in publication_policy.COPY_STATUSES
                                        and not verified_online)))
                    or not publication_policy.ready_for_copy({'sourceMarketplace': 'US', **us})):
                raise ValueError('美国尚未取得可接续的真实发布中回执')
            if single_us:
                validate_submitted(record, source)
                job.update(status='submitted', phase='美国已受理，待核实亚马逊在售',
                           usPublishId=us['publishId'], submittedAt=time.time())
                return
            job.update(usPublishId=us.get('publishId'), phase='核对英国登录、复制、原SKU及经营资料后发布')
            persist(job)
            login(job, connection, 'UK')
            record = run_browser(job, connection, 'uk')
            validate_submitted(record, source)
        job.update(status='submitted', phase='美国和英国均已受理并进入发布中，尚未核实在售',
                   usPublishId=record['usPublish']['publishId'], ukDraftId=record['ukCopy']['draftId'],
                   ukPublishId=record['ukPublish']['publishId'], submittedAt=time.time())
    except Exception as error:
        job.update(status='needs_reconciliation', phase='已保留原检查点，核对后可继续', error=str(error))
    finally:
        job.pop('browserPid', None)
        persist(job)
        with LOCK:
            ACTIVE.discard(job['id'])
