"""Persistent, evidence-gated orchestration ledger. No external actions at import."""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import uuid
import copy
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from codex_workbench import call as workbench_call

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('selection', 'sourcing', 'research', 'listing', 'images', 'export', 'publish', 'verify')
CHECKS = {
    'selection': ('selection_rules_passed', 'not_previously_selected', 'ip_checks_passed'),
    'sourcing': ('same_product_verified', 'variants_verified', 'cost_weight_verified', 'image_rights_verified'),
    'research': ('competitors_matched', 'keywords_sourced'),
    'listing': ('facts_verified', 'keyword_coverage_checked', 'format_checked'),
    'images': ('product_identity_checked', 'colors_checked', 'text_checked', 'all_slots_present'),
    'export': ('files_complete', 'sku_image_mapping_checked'),
    'publish': ('all_variants_submitted',),
    'verify': ('all_variants_online',),
}
AMAZON_DOMAINS = {'US':'com','UK':'co.uk','DE':'de','FR':'fr','IT':'it','ES':'es','JP':'co.jp','CA':'ca','AU':'com.au','IN':'in','MX':'com.mx','BR':'com.br'}
IP_CHECKS = ('brand', 'trademark', 'designPatent', 'utilityPatent', 'imageRights')


def _source_ok(value):
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k].strip() for k in ('url', 'checked_at', 'raw')):
        return False
    try:
        parsed = urlsplit(value['url'])
        date.fromisoformat(value['checked_at'][:10])
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (ValueError, TypeError, AttributeError):
        return False


def _browser_rank_url_matches(front_url, rank, supplemental, source_id, market):
    if rank.get('url') == front_url:
        return True
    # Keep the natural title query separate from an explicitly identified own-ASIN lookup.
    # This lookup establishes the candidate's BSR, not independent keyword demand.
    if not _source_ok(supplemental):
        return False
    try:
        url = urlsplit(supplemental['url'])
        queries = parse_qs(url.query).get('k', [])
        purpose = supplemental.get('purpose')
        exact_lookup = purpose == 'candidate_asin_rank_lookup' and queries == [source_id]
        title_prefix_lookup = False
        if purpose == 'candidate_title_prefix_asin_rank_lookup' and len(queries) == 1:
            natural = parse_qs(urlsplit(front_url).query).get('k', [''])[0].casefold().split()
            words = queries[0].split()
            prefix = [word.casefold() for word in words[:-1]]
            title_prefix_lookup = (bool(prefix) and words[-1] == source_id and words.count(source_id) == 1
                                   and prefix == natural[:len(prefix)])
        return ((exact_lookup or title_prefix_lookup)
                and supplemental.get('asin') == rank.get('asin') == source_id
                and supplemental['url'] == rank.get('url')
                and supplemental['raw'] == rank.get('raw')
                and re.search(r'\b' + re.escape(source_id) + r'\b', supplemental['raw']) is not None
                and url.scheme == 'https'
                and url.hostname == 'www.amazon.' + AMAZON_DOMAINS[market]
                and url.path == '/s' and url.port in (None, 443)
                and not url.username and not url.password)
    except (ValueError, TypeError, KeyError):
        return False


def validate_selection_evidence(evidence, market, source_id, expected_ledger, job_id, freshness_basis="seller_listing", image_policy=None, design_policy=None, selection_policy=None):
    """Require auditable seller-level evidence before selection can advance."""
    item = evidence.get('selection_evidence')
    if not isinstance(item, dict):
        raise ValueError('缺少结构化 selection_evidence；选品勾选项不能替代事实证据')
    if not re.fullmatch(r'[A-Z0-9]{10}', source_id) or item.get('asin') != source_id or item.get('marketplace') != market:
        raise ValueError('选品证据 ASIN/站点与台账任务不一致')
    seller_id = item.get('seller_id')
    if not isinstance(seller_id, str) or not seller_id.strip():
        raise ValueError('缺少同行 Seller ID')

    window_days = (selection_policy or {}).get('window_days', 7)
    if type(window_days) is not int or not 1 <= window_days <= 365:
        raise ValueError('无效选品日期窗口')
    try:
        collected = date.fromisoformat(item.get('collection_date', ''))
        if freshness_basis not in ('seller_listing', 'sellersprite_product_listing'): raise ValueError()
        field = 'product_listing_date' if freshness_basis == 'sellersprite_product_listing' else 'seller_listing_date'
        listed = date.fromisoformat(item.get(field, ''))
        if not 0 <= (collected-listed).days <= window_days or collected > date.today(): raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(f'所选口径的上架日期不在采集日最近{window_days}天内') from None
    if item.get('freshness_scope') != freshness_basis:
        raise ValueError('Date First Available 或历史月份不能替代卖家上架日期')
    date_source = item.get('product_listing_source' if freshness_basis == 'sellersprite_product_listing' else 'seller_listing_source')
    if freshness_basis == 'sellersprite_product_listing':
        try:
            url = urlsplit(date_source.get('url', '')); host = (url.hostname or '').lower()
            dates = re.findall(r'(?:上架日期|上架时间|刊登日期|listing\s*date|date\s*listed|listed\s*on)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})(?!\d)', str(date_source.get('raw', '')), re.I)
            website = host == 'sellersprite.com' or host.endswith('.sellersprite.com')
            extension = (date_source.get('capture_type') == 'sellersprite_browser_extension'
                and date_source.get('tool') == 'SellerSprite' and date_source.get('asin') == source_id
                and host == 'www.amazon.' + AMAZON_DOMAINS[market] and url.path == '/dp/' + source_id
                and date_source.get('element_id') in ('seller-sprite-extension-quick-view-listing', 'seller-sprite-extension-quick-view-listing-page')
                and re.search(r'ASIN\s*[:：]\s*' + re.escape(source_id) + r'\b', str(date_source.get('raw', '')), re.I))
            from sellersprite_date import validate_export_source
            exported = validate_export_source(date_source, market, source_id, listed, 'www.amazon.' + AMAZON_DOMAINS[market])
            if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or not (website or extension or exported) or set(dates) != {listed.isoformat()}): raise ValueError()
        except (ValueError, TypeError, AttributeError, OSError, ImportError):
            raise ValueError('缺少SellerSprite商品上架日期明确原文；不代表卖家独立上新') from None
    if not _source_ok(date_source):
        raise ValueError('缺少卖家上架日期的来源原文')
    if item.get('seller_country') != 'CN' or not _source_ok(item.get('seller_country_source')):
        raise ValueError('缺少有原文依据的中国卖家国家证据')
    if item.get('fulfillment_type') != 'FBM' or not _source_ok(item.get('fulfillment_source')):
        raise ValueError('缺少有原文依据的FBM履约证据')

    search = item.get('front_search')
    if not isinstance(search, dict) or search.get('status') != 'opened' or not _source_ok(search):
        raise ValueError('缺少已打开的Amazon前台标题首句搜索记录')
    try:
        parsed = urlsplit(search['url'])
        query = parse_qs(parsed.query).get('k', [''])[0]
        expected_host = 'www.amazon.' + AMAZON_DOMAINS[market]
        if parsed.scheme != 'https' or parsed.hostname != expected_host or parsed.path != '/s' or not query.strip(): raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError('Amazon前台搜索网址与任务站点不匹配') from None
    if not isinstance(item.get('precise_keyword'), str) or ' '.join(item['precise_keyword'].split()).casefold() != ' '.join(query.split()).casefold():
        raise ValueError('精准关键词必须与实际打开的Amazon前台搜索一致')

    any_seller_rank = (selection_policy or {}).get('same_product_rank_policy', {}).get('seller_scope') == 'any_seller'
    match = item.get('match')
    if not isinstance(match, dict) or match.get('level') not in ('exact', 'strong') or not isinstance(match.get('asin'), str) or not re.fullmatch(r'[A-Z0-9]{10}', match['asin']) or (match['asin'] == source_id and not any_seller_rank) or not _source_ok(match.get('source')):
        raise ValueError('缺少exact/strong同款与核图证据' if any_seller_rank else '缺少其他卖家的exact/strong同款与核图证据')
    rank = item.get('matched_rank')
    browser_rank = any_seller_rank and isinstance(rank, dict) and rank.get('capture_type') == 'browser_search_card' and rank.get('source') == 'Amazon search page extension'
    if not isinstance(rank, dict) or (rank.get('source') != 'SellerSprite same-product search' and not browser_rank) or rank.get('asin') != match.get('asin') or type(rank.get('value')) is not int or rank['value'] <= 0 or not _source_ok(rank):
        raise ValueError('缺少匹配同款ASIN的数值销售排名')
    if browser_rank:
        # A search position, price or estimated sales cannot stand in for BSR.
        ranks = re.findall(r'#\s*([\d,]+)\s*\n?\s*in\s*([^\n]+)', rank['raw'], re.I)
        if (not _browser_rank_url_matches(search['url'], rank, item.get('supplemental_rank_search'), source_id, market) or not str(rank.get('category', '')).strip()
                or (rank['value'], rank['category'].strip()) not in [(int(v.replace(',', '')), c.strip()) for v, c in ranks]):
            raise ValueError('搜索商品卡缺少对应类目数值销售排名原文')
    elif rank.get('capture_type') == 'sellersprite_mcp_asin_detail':
        try:
            url = urlsplit(rank['url']); payload = json.loads(rank['raw'])
            if (url.scheme != 'https' or url.hostname != 'mcp.sellersprite.com' or url.path != '/mcp' or url.query or url.username or url.password or url.port not in (None, 443) or not isinstance(payload, dict) or payload.get('asin') != match['asin'] or type(payload.get('bsrRank')) is not int or payload['bsrRank'] != rank['value']): raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('SellerSprite MCP同款ASIN/数值/来源不一致') from None
    else:
        raw_rank = ''.join(c for c in rank['raw'] if c.isdigit())
        if not raw_rank or int(raw_rank) != rank['value']:
            raise ValueError('SellerSprite原始排名文本与解析数值不一致')

    checks = item.get('ip_checks')
    if not isinstance(checks, dict): raise ValueError('缺少五项IP核验记录')
    selection_policy = selection_policy or {}
    if selection_policy.get('brand_policy', {}).get('mode') == 'generic_only':
        brand = checks.get('brand') or {}
        u = urlsplit(brand.get('url', ''))
        if (brand.get('observedBrand') != 'Generic' or not _source_ok(brand)
                or u.scheme != 'https' or u.hostname != 'www.amazon.' + AMAZON_DOMAINS[market]
                or u.path != '/dp/' + source_id
                or not re.search(r'\bBrand\s*[:：]\s*Generic\b', str(brand.get('raw', '')), re.I)):
            raise ValueError('只选Amazon商品页品牌明确为Generic的产品，缺少对应ASIN品牌原文')
    for name in IP_CHECKS:
        check = checks.get(name)
        patent_policy = selection_policy.get('patent_search_policy') or {}
        if name in ('designPatent', 'utilityPatent') and patent_policy.get('mode') == 'skip_by_user_instruction' and patent_policy.get('instruction'):
            if (not isinstance(check, dict) or check.get('status') != 'skipped_by_user'
                    or check.get('searchPerformed') is not False or not str(check.get('judgment', '')).strip()
                    or check.get('instruction') != patent_policy['instruction']):
                raise ValueError('专利须按用户要求明确记录未检索，不得标为检索通过：' + name)
            continue
        accepted = isinstance(check, dict) and check.get('status') == 'clear'
        if name == 'designPatent' and isinstance(check, dict) and design_policy:
            accepted = accepted or (design_policy.get('mode') == 'obvious_overall_match_only'
                and bool(design_policy.get('instruction'))
                and check.get('status') == 'no_obvious_match'
                and check.get('searchPerformed') is True
                and check.get('obviousOverallMatchFound') is False)
        if name == 'imageRights' and isinstance(check, dict) and image_policy:
            try:
                u = urlsplit(check.get('url', ''))
                accepted = accepted or (image_policy.get('supplier_image_policy') == 'user_default_supplier_images'
                    and bool(image_policy.get('supplier_image_authorization', {}).get('instruction'))
                    and check.get('status') == 'user_default' and check.get('basis') == 'user_default_supplier_use_not_supplier_license'
                    and check.get('visuallyReviewed') is True and check.get('noUnauthorizedMarks') is True
                    and u.scheme == 'https' and u.hostname == 'detail.1688.com' and u.port in (None, 443)
                    and not u.username and not u.password and bool(re.fullmatch(r'/offer/\d+\.html', u.path)))
            except (ValueError, TypeError):
                accepted = False
        if not accepted or not isinstance(check.get('query'), str) or not check['query'].strip() or not isinstance(check.get('judgment'), str) or not check['judgment'].strip() or not _source_ok(check):
            raise ValueError('侵权/权利核验未完成或缺少原文：' + name)

    dedupe = item.get('dedupe')
    if not isinstance(dedupe, dict) or not isinstance(dedupe.get('ledger_path'), str) or not Path(dedupe['ledger_path']).is_absolute() or Path(dedupe['ledger_path']).resolve() != Path(expected_ledger).resolve() or not isinstance(dedupe.get('ledger_sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', dedupe['ledger_sha256']):
        raise ValueError('缺少跨批次已选ASIN排重台账核查证据')
    ledger = Path(dedupe['ledger_path'])
    if not ledger.is_file() or hashlib.sha256(ledger.read_bytes()).hexdigest() != dedupe['ledger_sha256']:
        raise ValueError('排重台账不存在或核查后已变化，请重新核对')
    try:
        with ledger.open('r', encoding='utf-8-sig', newline='') as stream:
            selected = csv.DictReader(stream)
            if not {'marketplace', 'asin'}.issubset(set(selected.fieldnames or [])): raise ValueError()
            duplicates = [row for row in selected if (row.get('marketplace') or '').upper() == market and (row.get('asin') or '').upper() == source_id]
            if duplicates and not all(row.get('source') == 'pipeline:'+job_id and row.get('status') == 'validation_candidate' for row in duplicates):
                raise ValueError('候选ASIN已存在于全局排重台账')
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        if str(exc) == '候选ASIN已存在于全局排重台账': raise
        raise ValueError('全局已选ASIN排重台账无法解析') from None
    quota = item.get('daily_quota')
    if not isinstance(quota, dict) or quota.get('seller_id') != seller_id or quota.get('marketplace') != market or quota.get('collection_date') != item.get('collection_date') or type(quota.get('count_after_selection')) is not int or not 1 <= quota['count_after_selection'] <= 2 or not _source_ok(quota.get('source')):
        raise ValueError('同行店铺每日 shortlist 数量超过2或缺少配额键')


def reserve_selected_candidate(ledger_path, market, asin, job_id, expected_hash):
    """Atomically reserve a fully qualified candidate in the cross-batch ledger."""
    ledger = Path(ledger_path)
    raw = ledger.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError('排重台账在本次选品核验后发生变化，未预留候选')
    try:
        text = raw.decode('utf-8-sig')
        from io import StringIO
        stream = StringIO(text, newline='')
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
        if not {'marketplace', 'asin'}.issubset(fields): raise ValueError()
    except (UnicodeError, csv.Error, ValueError):
        raise ValueError('全局已选ASIN排重台账无法安全更新') from None
    existing = [row for row in rows if (row.get('marketplace') or '').upper() == market and (row.get('asin') or '').upper() == asin]
    if existing:
        if all(row.get('source') == 'pipeline:'+job_id and row.get('status') == 'validation_candidate' for row in existing):
            return {'path':str(ledger),'status':'already_reserved'}
        raise ValueError('候选ASIN已存在于全局排重台账')
    required = ('marketplace','asin','selected_at','source','status')
    fields.extend(key for key in required if key not in fields)
    rows.append({'marketplace':market,'asin':asin,'selected_at':now(),'source':'pipeline:'+job_id,'status':'validation_candidate'})
    temp = ledger.with_name(ledger.name+'.'+job_id+'.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
            writer.writeheader(); writer.writerows(rows)
            output.flush()
        temp.replace(ledger)
    finally:
        if temp.exists(): temp.unlink()
    return {'path':str(ledger),'status':'reserved_validation_candidate'}


def now():
    return datetime.now(timezone.utc).isoformat()


def rmb_fen(value, *, allow_zero=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
            raise InvalidOperation()
        cents = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if cents != amount:
            raise InvalidOperation()
        return int(cents * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('人民币金额必须为有效的两位小数金额') from None


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def published_children(record, row):
    pub = record.get('publication')
    store = json.loads(row['config'])['publishing']['store']
    if (not isinstance(pub, dict) or pub.get('marketplace') != row['market'] or pub.get('store') != store
            or not isinstance(pub.get('publish_id'), str) or not re.fullmatch(r'[1-9][0-9]*', pub['publish_id'])
            or not isinstance(record.get('external_ids'), list) or pub['publish_id'] not in record['external_ids']):
        raise ValueError('发布回执须匹配本任务的店铺、站点及外部发布编号')
    skus = pub.get('child_skus')
    if (not isinstance(skus,list) or not skus or not all(isinstance(s,str) and s and s==s.strip() for s in skus)
            or len(set(skus))!=len(skus)):
        raise ValueError('发布回执须记录完整且唯一的子 SKU')
    return pub


def workbench_receipt(db, row, evidence):
    from urllib.parse import urlencode
    events = db.execute("SELECT data,created_at FROM events WHERE job_id=? AND stage='publish' AND action='completed' LIMIT 2",
                        (row['id'],)).fetchall()
    if len(events) != 1:
        raise ValueError('缺少唯一原发布回执')
    record = json.loads(events[0]['data'])
    pub = published_children(record, row)
    reference = evidence.get('workbench_publication_id') or record.get('workbench_publication_id')
    if (not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', reference)
            or record.get('workbench_publication_id', reference) != reference
            or not isinstance(evidence.get('external_ids'), list) or pub['publish_id'] not in evidence['external_ids']):
        raise ValueError('在售核验须引用原工作台发布任务和 ERP 发布编号')
    query = urlencode({'jobId': reference, 'publishId': pub['publish_id'],
                       'sourceId': row['source_id'], 'pipelineJobId': row['id']})
    url = json.loads(row['config'])['execution'].get('workbench_url')
    if url is not None and (not isinstance(url, str) or not url.strip()):
        raise ValueError('任务配置缺少有效的工作台地址')
    receipt = workbench_call('publication-receipt?' + query, workbench_url=url)
    expected = {k: pub[k] for k in ('publish_id', 'marketplace', 'store')}
    expected['workbench_publication_id'] = reference
    if record.get('delivery_scope'):
        expected['parent_sku'] = record['delivery_scope']['parent_sku']
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        raise ValueError('工作台 ERP 回执与原发布号、店铺或站点不同')
    children = receipt.get('children')
    if (not isinstance(children, list) or len(children) != len(pub['child_skus'])
            or any(not isinstance(child, dict) or not isinstance(child.get('sku'), str)
                   or child.get('status') not in ('online', 'active', 'buyable', 'discoverable')
                   or not re.fullmatch(r'[A-Z0-9]{10}', str(child.get('asin') or '')) for child in children)
            or {child['sku'] for child in children} != set(pub['child_skus'])):
        raise ValueError('工作台 ERP 回执须证明全部原子 SKU 在售且有 ASIN')
    try:
        observed = datetime.fromisoformat(receipt['observed_at'].replace('Z', '+00:00'))
        if not datetime.fromisoformat(events[0]['created_at']) <= observed <= datetime.now(timezone.utc):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError('请到工作台只读刷新原发布任务，再完成在售核验') from None
    return receipt


def doctor(config, root=ROOT):
    problems = []
    for field in ('platform', 'store', 'quantity', 'shipping_template'):
        if config['publishing'].get(field) is None or config['publishing'].get(field) == '':
            problems.append('publishing.' + field + ' 未设置')
    if config['images'].get('enabled', True):
        for field in ('per_product_budget_rmb', 'daily_budget_rmb'):
            if config['images'].get(field) is None:
                problems.append('images.' + field + ' 未设置，不能开始付费生图')
    if config['publishing'].get('platform') != 'junfei':
        problems.append('当前仅配置钧飞ERP，其他平台尚未接入')
    quantity = config['publishing'].get('quantity')
    if type(quantity) is not int or quantity < 0:
        problems.append('publishing.quantity 必须为非负整数')
    recipe = root / config['publishing']['recipe']
    if not recipe.exists() or not read_json(recipe).get('calibrated') or 'REPLACE_' in recipe.read_text(encoding='utf-8-sig'):
        problems.append('刊登页面适配未完成；不可运行保存/发布')
    return problems


def readiness(config, root=ROOT):
    issues = doctor(config, root)
    enabled = config['images'].get('enabled', True)
    return {
        'configured': not issues,
        'unattended_execution': False,
        'defaults': config.get('defaults', {}),
        'profile': config.get('defaults', {}).get('profile'),
        'image_generation': 'enabled' if enabled else 'disabled_use_existing_assets',
        'issues': issues,
        'gaps': [
            '选品、货源、关键词研究、文案、图片生成、导出和在售核对尚未全部连成无人值守执行器',
            '钧飞 recipe 未在通用脚本中校准；人工试刊保存成功记录在 automation/data/junfei-draft-calibration-20261031.json，不能替代通用刊登适配',
            'CLI台账与网页智能体任务中心尚未同步',
            '发布须按具体任务获得明确授权'
        ]
    }


class Pipeline:
    def __init__(self, db, config):
        self.config = config
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, market TEXT NOT NULL, source_id TEXT NOT NULL,
          stage INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ready',
          attempts INTEGER NOT NULL DEFAULT 0, token TEXT, reason TEXT,
          config TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(market,source_id));
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL,
          action TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS image_costs (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL, day TEXT NOT NULL,
          reserved_fen INTEGER NOT NULL, actual_fen INTEGER,
          state TEXT NOT NULL, external_id TEXT, note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS image_costs_job ON image_costs(job_id,state);
        CREATE INDEX IF NOT EXISTS image_costs_day ON image_costs(day,state);
        ''')

    def event(self, job, stage, action, data):
        self.db.execute('INSERT INTO events(job_id,stage,action,data,created_at) VALUES(?,?,?,?,?)',
                        (job, stage, action, json.dumps(data, ensure_ascii=False), now()))

    def add(self, market, source_id, profile=None):
        if not market.strip() or not source_id.strip():
            raise ValueError('站点及源商品ID不能为空')
        cfg = copy.deepcopy(self.config)
        profile = profile or cfg.get('defaults', {}).get('profile')
        if not profile or profile not in cfg.get('profiles', {}):
            raise ValueError('必须选择已设置的店铺配置档案：先运行profile-set，或检查defaults.profile')
        cfg['publishing'].update(cfg['profiles'][profile])
        cfg['publishing']['platform'] = 'junfei'
        cfg['profile_id'] = profile
        if market.upper() != cfg['publishing']['marketplace'].upper():
            raise ValueError('任务站点与店铺配置档案不一致')
        job = uuid.uuid4().hex[:16]
        with self.transaction():
            self.db.execute('INSERT INTO jobs(id,market,source_id,config,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                            (job, market.upper(), source_id, json.dumps(cfg, ensure_ascii=False), now(), now()))
            self.event(job, STAGES[0], 'created', {'source_id': source_id})
        return job

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def tx():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return tx()

    def get(self, job):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        return dict(row)

    def claim(self, job):
        with self.transaction():
            row = self.get(job)
            if row['status'] != 'ready':
                raise ValueError('只可领取ready任务；运行中/结果未知必须先对账')
            cfg = json.loads(row['config'])
            stage = STAGES[row['stage']]
            if stage in ('publish', 'verify') and cfg['execution'].get('local_handoff_only') is True:
                raise ValueError('本任务仅允许本地交接，不能领取ERP阶段')
            if row['attempts'] >= cfg['execution']['max_attempts']:
                raise ValueError('已达本阶段尝试上限，需诊断')
            if stage == 'images' and cfg['images'].get('enabled', True) and any(cfg['images'][f] is None for f in ('per_product_budget_rmb', 'daily_budget_rmb')):
                raise ValueError('未确定生图预算，不能领取图片执行任务')
            if stage == 'images' and cfg['images'].get('enabled', True):
                self._reject_image_over_budget(job)
                reserved = self.db.execute("SELECT 1 FROM image_costs WHERE job_id=? AND state='reserved' LIMIT 1", (job,)).fetchone()
                if not reserved:
                    raise ValueError('生图前必须先登记费用上限并成功预留预算')
            if stage == 'publish':
                issues = doctor(cfg)
                if issues:
                    raise ValueError('; '.join(issues))
            token = uuid.uuid4().hex
            self.db.execute("UPDATE jobs SET status='running',token=?,attempts=attempts+1,updated_at=? WHERE id=?", (token, now(), job))
            self.event(job, stage, 'claimed', {'token': token})
            # These are gates the operator must satisfy, not results already observed.
            return {'job_id': job, 'stage': stage, 'token': token,
                    'required_checks': CHECKS[stage], 'config': cfg}

    def finish(self, job, token, evidence):
        if not isinstance(evidence, dict):
            raise ValueError('阶段证据须为对象')
        receipt = None
        initial = self.get(job)
        if (initial['stage'] == STAGES.index('verify') and token and initial['token'] == token
                and initial['status'] in ('running', 'uncertain')
                and evidence.get('job_id') == job and evidence.get('stage') == 'verify'
                and evidence.get('simulated') is False):
            receipt = workbench_receipt(self.db, initial, evidence)
        with self.transaction():
            row = self.get(job)
            if row['status'] not in ('running', 'uncertain') or row['token'] != token:
                raise ValueError('任务状态或领取token不匹配，拒绝重复完成')
            stage = STAGES[row['stage']]
            if evidence.get('stage') != stage or evidence.get('job_id') != job:
                raise ValueError('证据不属于当前任务/阶段')
            if evidence.get('simulated') is not False:
                raise ValueError('正式台账只接受明确标记simulated=false的真实证据')
            ledger_path = None
            if stage == 'selection':
                cfg = json.loads(row['config'])
                ledger_path = Path(cfg['selection']['selected_ledger'])
                if not ledger_path.is_absolute(): ledger_path = ROOT / ledger_path
                validate_selection_evidence(evidence, row['market'], row['source_id'], ledger_path, job, cfg['selection'].get('freshness_basis', 'seller_listing'), cfg.get('images'), cfg['selection'].get('design_patent_policy'), cfg['selection'])
            if not isinstance(evidence.get('checks'), dict) or any(evidence['checks'].get(k) is not True for k in CHECKS[stage]):
                raise ValueError('阶段校验未全部通过：' + ', '.join(CHECKS[stage]))
            if not evidence.get('sources') or not evidence.get('summary'):
                raise ValueError('缺少来源和执行摘要')
            files = evidence.get('artifacts', [])
            if stage in ('listing', 'images', 'export') and not files:
                raise ValueError('该阶段必须保存实际产物')
            if stage == 'images' and json.loads(row['config'])['images'].get('enabled', True):
                costs = self.db.execute("SELECT state FROM image_costs WHERE job_id=?", (job,)).fetchall()
                if any(cost['state'] == 'over_budget' for cost in costs):
                    raise ValueError('本产品生图费用已超预算，图片阶段阻断，需先调整预算或替换素材')
                if not costs or any(cost['state'] == 'reserved' for cost in costs) or not any(cost['state'] == 'settled' for cost in costs):
                    raise ValueError('图片阶段必须先结算已确认的生图费用，或对确认无费用的调用结算为¥0')
            hashes = {}
            for item in files:
                path = Path(item)
                if not path.is_absolute() or not path.is_file():
                    raise ValueError('产物必须是存在的绝对文件路径')
                hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            if stage == 'publish':
                published_children(evidence, row)
            if stage == 'verify' and receipt is None:
                raise ValueError('须使用工作台的原始在售回执完成核验')
            reservation = None
            if stage == 'selection':
                reservation = reserve_selected_candidate(ledger_path, row['market'], row['source_id'], job,
                    evidence['selection_evidence']['dedupe']['ledger_sha256'])
            next_stage = row['stage'] + 1
            event_data = dict(evidence, artifact_sha256=hashes)
            if receipt is not None:
                event_data['online_observation'] = receipt
            if reservation: event_data['selection_reservation'] = reservation
            self.event(job, stage, 'completed', event_data)
            status = 'done' if next_stage == len(STAGES) else 'ready'
            reason = None
            if stage == 'export' and json.loads(row['config'])['execution'].get('local_handoff_only') is True:
                status, reason = 'blocked', '本地交接包已完成，ERP阶段停用'
            self.db.execute('UPDATE jobs SET stage=?,status=?,attempts=0,token=NULL,reason=?,updated_at=? WHERE id=?',
                            (next_stage, status, reason, now(), job))

    def _reject_image_over_budget(self, job):
        if self.db.execute("SELECT 1 FROM image_costs WHERE job_id=? AND state='over_budget' LIMIT 1", (job,)).fetchone():
            raise ValueError('Image costs exceeded budget; further CLI image submissions are blocked')

    def reserve_image_cost(self, job, maximum_rmb):
        estimate_fen = rmb_fen(maximum_rmb)
        with self.transaction():
            today = date.today().isoformat()
            row = self.get(job)
            cfg = json.loads(row['config'])
            if row['stage'] != STAGES.index('images') or cfg['images'].get('enabled', True) is not True:
                raise ValueError('任务未处于启用生图的图片阶段')
            if row['status'] != 'ready':
                raise ValueError('仅可为ready任务预留；运行中/结果未知任务必须先对账')
            if self.db.execute("SELECT 1 FROM image_costs WHERE job_id=? AND state='reserved' LIMIT 1", (job,)).fetchone():
                raise ValueError('该产品已有未结算生图预留，先核对原任务')
            self._reject_image_over_budget(job)
            product_limit = rmb_fen(cfg['images'].get('per_product_budget_rmb'))
            daily_limit = rmb_fen(cfg['images'].get('daily_budget_rmb'))
            product_used = self.db.execute("SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN reserved_fen ELSE COALESCE(actual_fen,0) END),0) FROM image_costs WHERE job_id=?", (job,)).fetchone()[0]
            daily_used = self.db.execute("SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN reserved_fen ELSE COALESCE(actual_fen,0) END),0) FROM image_costs WHERE day=? OR state='reserved'", (today,)).fetchone()[0]
            if product_used + estimate_fen > product_limit:
                raise ValueError('本产品生图预算不足：已用及预留金额加本次上限将超过每款预算')
            if daily_used + estimate_fen > daily_limit:
                raise ValueError('每日生图预算不足：已用及预留金额加本次上限将超过每日预算')
            reservation = uuid.uuid4().hex[:16]
            stamp = now()
            self.db.execute('INSERT INTO image_costs(id,job_id,day,reserved_fen,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                            (reservation, job, today, estimate_fen, 'reserved', stamp, stamp))
            self.event(job, 'images', 'cost_reserved', {'reservation_id': reservation, 'day': today, 'reserved_rmb': f'{estimate_fen/100:.2f}'})
            return {'reservation_id': reservation, 'job_id': job, 'day': today, 'reserved_rmb': f'{estimate_fen/100:.2f}', 'state': 'reserved'}

    def settle_image_cost(self, reservation, actual_rmb, note, external_id=''):
        actual_fen = rmb_fen(actual_rmb, allow_zero=True)
        if not isinstance(note, str) or len(note.strip()) < 4:
            raise ValueError('结算必须记录账单核实依据；¥0需说明已确认未收费')
        with self.transaction():
            item = self.db.execute('SELECT * FROM image_costs WHERE id=?', (reservation,)).fetchone()
            if item is None:
                raise ValueError('生图费用预留不存在')
            if item['state'] != 'reserved':
                raise ValueError('生图费用预留已結算，不可重复记账')
            row = self.get(item['job_id'])
            cfg = json.loads(row['config'])
            product_limit = rmb_fen(cfg['images'].get('per_product_budget_rmb'))
            daily_limit = rmb_fen(cfg['images'].get('daily_budget_rmb'))
            product_previous = self.db.execute("SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN reserved_fen ELSE COALESCE(actual_fen,0) END),0) FROM image_costs WHERE job_id=? AND id<>?", (item['job_id'], reservation)).fetchone()[0]
            daily_previous = self.db.execute("SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN reserved_fen ELSE COALESCE(actual_fen,0) END),0) FROM image_costs WHERE day=? AND id<>?", (item['day'], reservation)).fetchone()[0]
            over_budget = actual_fen > item['reserved_fen'] or product_previous + actual_fen > product_limit or daily_previous + actual_fen > daily_limit
            state = 'over_budget' if over_budget else 'settled'
            stamp = now()
            self.db.execute('UPDATE image_costs SET actual_fen=?,state=?,external_id=?,note=?,updated_at=? WHERE id=?',
                            (actual_fen, state, external_id or None, note.strip(), stamp, reservation))
            self.event(item['job_id'], 'images', 'cost_settled', {'reservation_id': reservation, 'actual_rmb': f'{actual_fen/100:.2f}', 'state': state, 'external_id': external_id or None, 'note': note.strip()})
            return {'reservation_id': reservation, 'job_id': item['job_id'], 'actual_rmb': f'{actual_fen/100:.2f}', 'state': state}

    def image_budget_status(self, day=None):
        day = day or date.today().isoformat()
        rows = self.db.execute('''SELECT c.*,j.config FROM image_costs c JOIN jobs j ON j.id=c.job_id WHERE c.day=? OR c.state='reserved' ORDER BY c.created_at''', (day,)).fetchall()
        daily_limit = rmb_fen(self.config['images'].get('daily_budget_rmb'))
        daily_used = sum((r['reserved_fen'] if r['state']=='reserved' else (r['actual_fen'] or 0)) for r in rows)
        carryover = sum(r['reserved_fen'] for r in rows if r['day'] != day and r['state'] == 'reserved')
        products = {}
        for row in rows:
            entry = products.setdefault(row['job_id'], {'reserved_fen':0,'actual_fen':0,'limit_fen':rmb_fen(json.loads(row['config'])['images'].get('per_product_budget_rmb'))})
            if row['state'] == 'reserved': entry['reserved_fen'] += row['reserved_fen']
            else: entry['actual_fen'] += row['actual_fen'] or 0
        return {'scope':'CLI local reservations only; not a provider spending cap; not synchronized with the web ledger',
                'carryover_reserved_rmb':f'{carryover/100:.2f}',
                'day':day,'daily_limit_rmb':f'{daily_limit/100:.2f}','daily_used_and_reserved_rmb':f'{daily_used/100:.2f}',
                'reservations':[{'id':r['id'],'job_id':r['job_id'],'day':r['day'],'reserved_rmb':f"{r['reserved_fen']/100:.2f}",'actual_rmb':None if r['actual_fen'] is None else f"{r['actual_fen']/100:.2f}",'state':r['state'],'external_id':r['external_id']} for r in rows],
                'products':{job:{'limit_rmb':f"{v['limit_fen']/100:.2f}",'used_and_reserved_rmb':f"{(v['reserved_fen']+v['actual_fen'])/100:.2f}"} for job,v in products.items()}}

    def block_ready(self, job, reason):
        """Record a newly discovered evidence conflict before claiming any work."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('需明确阻断原因')
        with self.transaction():
            row = self.get(job)
            if row['status'] != 'ready' or row['token']:
                raise ValueError('仅可阻断未领取的ready任务；运行中或未知任务须按原token对账')
            self.event(job, STAGES[row['stage']], 'blocked', {'reason': reason, 'before_claim': True})
            self.db.execute('UPDATE jobs SET status=?,reason=?,updated_at=? WHERE id=?',
                            ('blocked', reason, now(), job))

    def hold(self, job, token, status, reason):
        if status not in ('blocked', 'uncertain') or not reason.strip():
            raise ValueError('需明确阻断或未知原因')
        with self.transaction():
            row = self.get(job)
            if row['status'] != 'running' or row['token'] != token:
                raise ValueError('任务状态或token不匹配')
            self.event(job, STAGES[row['stage']], status, {'reason': reason})
            self.db.execute('UPDATE jobs SET status=?,reason=?,updated_at=? WHERE id=?', (status, reason, now(), job))

    def resume(self, job, reconciliation):
        if not reconciliation.strip():
            raise ValueError('必须记录核查结论')
        with self.transaction():
            row = self.get(job)
            if row['status'] not in ('blocked', 'uncertain', 'running'):
                raise ValueError('当前状态不需要恢复')
            execution = json.loads(row['config'])['execution']
            if row['stage'] >= STAGES.index('publish') and execution.get('local_handoff_only') is True:
                raise ValueError('本任务仅允许本地交接，不能恢复到ERP阶段')
            if row['attempts'] >= execution['max_attempts']:
                raise ValueError('重试上限已达到，需修复执行器后另行处理，禁止自动重发')
            self.event(job, STAGES[row['stage']], 'reconciled_no_external_submission', {'conclusion': reconciliation})
            self.db.execute("UPDATE jobs SET status='ready',token=NULL,reason=NULL,updated_at=? WHERE id=?", (now(), job))

    def status(self):
        rows = [dict(r) for r in self.db.execute('SELECT id,market,source_id,stage,status,attempts,reason FROM jobs ORDER BY created_at')]
        for r in rows:
            r['stage'] = STAGES[r['stage']] if r['stage'] < len(STAGES) else 'complete'
        return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'automation/data/pipeline.sqlite3'))
    parser.add_argument('--config', default=str(ROOT / 'automation/config.json'))
    sub = parser.add_subparsers(dest='action', required=True)
    diagnostic = sub.add_parser('doctor'); diagnostic.add_argument('--profile')
    sub.add_parser('status')
    sub.add_parser('readiness')
    add = sub.add_parser('add'); add.add_argument('--market', default='US'); add.add_argument('--source-id', required=True); add.add_argument('--profile')
    sub.add_parser('profiles')
    profile = sub.add_parser('profile-set')
    profile.add_argument('name'); profile.add_argument('--store', required=True); profile.add_argument('--market', required=True)
    profile.add_argument('--shipping-template', required=True); profile.add_argument('--quantity', type=int, default=88)
    profile.add_argument('--mode', choices=['draft', 'publish'], default='draft')
    claim = sub.add_parser('claim'); claim.add_argument('job')
    template = sub.add_parser('template'); template.add_argument('job')
    finish = sub.add_parser('finish'); finish.add_argument('job'); finish.add_argument('--token', required=True); finish.add_argument('--evidence', required=True)
    hold = sub.add_parser('hold'); hold.add_argument('job'); hold.add_argument('--token', required=True); hold.add_argument('--status', choices=['blocked', 'uncertain'], required=True); hold.add_argument('--reason', required=True)
    resume = sub.add_parser('resume'); resume.add_argument('job'); resume.add_argument('--reconciled-no-submission', required=True)
    reserve = sub.add_parser('reserve-image-cost'); reserve.add_argument('job'); reserve.add_argument('--max-rmb', required=True)
    settle = sub.add_parser('settle-image-cost'); settle.add_argument('reservation'); settle.add_argument('--actual-rmb', required=True); settle.add_argument('--note', required=True); settle.add_argument('--external-id', default='')
    budget = sub.add_parser('image-budget-status'); budget.add_argument('--day')
    args = parser.parse_args()
    cfg = read_json(args.config)
    if args.action == 'profiles':
        print(json.dumps(cfg.get('profiles', {}), ensure_ascii=False, indent=2)); return 0
    if args.action == 'profile-set':
        if args.quantity < 0 or not all(s.strip() for s in (args.name, args.store, args.market, args.shipping_template)):
            raise ValueError('库存需非负，配置字段不得为空')
        cfg.setdefault('profiles', {})[args.name] = {'store': args.store, 'marketplace': args.market.upper(), 'shipping_template': args.shipping_template, 'quantity': args.quantity, 'mode': args.mode}
        target = Path(args.config)
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(target)
        print('已保存配置；已有任务保持创建时的快照，新任务使用新配置。'); return 0
    if args.action == 'doctor':
        if args.profile:
            if args.profile not in cfg.get('profiles', {}):
                raise ValueError('配置档案不存在')
            cfg['publishing'].update(cfg['profiles'][args.profile])
        issues = doctor(cfg)
        print(json.dumps({'ready_for_unattended_run': False, 'issues': issues, 'note': '当前为Codex驱动总控；各阶段尚未全部接成无人值守执行器'}, ensure_ascii=False, indent=2))
        return 1 if issues else 0
    p = Pipeline(args.db, cfg)
    try:
        if args.action == 'status': result = p.status()
        elif args.action == 'readiness': result = readiness(cfg)
        elif args.action == 'add': result = {'job_id': p.add(args.market, args.source_id, args.profile or cfg.get('defaults', {}).get('profile'))}
        elif args.action == 'claim': result = p.claim(args.job)
        elif args.action == 'template':
            row = p.get(args.job)
            if row['stage'] >= len(STAGES): raise ValueError('任务已完成')
            stage = STAGES[row['stage']]
            result = {'job_id': args.job, 'stage': stage, 'simulated': False, 'checks': dict.fromkeys(CHECKS[stage], False), 'sources': [], 'artifacts': [], 'summary': '', 'external_ids': []}
            if stage == 'selection':
                cfg = json.loads(row['config']); ledger_path = Path(cfg['selection']['selected_ledger'])
                if not ledger_path.is_absolute(): ledger_path = ROOT / ledger_path
                ledger_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest() if ledger_path.is_file() else ''
                result['selection_evidence'] = {'asin':row['source_id'],'marketplace':row['market'],'seller_id':'','collection_date':'','seller_listing_date':'','product_listing_date':'','freshness_scope':cfg['selection'].get('freshness_basis','seller_listing'),'seller_listing_source':{},'product_listing_source':{},'seller_country':'','seller_country_source':{},'fulfillment_type':'','fulfillment_source':{},'precise_keyword':'','front_search':{'url':'','status':'','checked_at':'','raw':''},'match':{'asin':'','level':'','source':{}},'matched_rank':{'source':'SellerSprite same-product search','asin':'','value':None,'url':'','checked_at':'','raw':''},'ip_checks':{name:{'status':'','query':'','url':'','checked_at':'','raw':'','judgment':''} for name in IP_CHECKS},'dedupe':{'ledger_path':str(ledger_path.resolve()),'ledger_sha256':ledger_hash},'daily_quota':{'marketplace':row['market'],'seller_id':'','collection_date':'','count_after_selection':None,'source':{}}}
                result['selection_evidence'].update(candidate_parent_asin='', ranked_competitors=[])
        elif args.action == 'finish': p.finish(args.job, args.token, read_json(args.evidence)); result = p.get(args.job)
        elif args.action == 'hold': p.hold(args.job, args.token, args.status, args.reason); result = p.get(args.job)
        elif args.action == 'reserve-image-cost': result = p.reserve_image_cost(args.job, args.max_rmb)
        elif args.action == 'settle-image-cost': result = p.settle_image_cost(args.reservation, args.actual_rmb, args.note, args.external_id)
        elif args.action == 'image-budget-status': result = p.image_budget_status(args.day)
        else: p.resume(args.job, args.reconciled_no_submission); result = p.get(args.job)
        if isinstance(result, dict): result.pop('config', None) if args.action != 'claim' else None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        p.db.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, sqlite3.IntegrityError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
