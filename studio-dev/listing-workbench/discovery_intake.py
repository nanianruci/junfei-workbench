"""Promote reviewed CLI evidence into the account product library without dispatching work."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

import cyclic_discovery
import product_archive


STAGES = ('selection', 'sourcing', 'research', 'listing', 'images', 'export', 'publish', 'verify')
STATUSES = {'ready', 'running', 'blocked', 'uncertain', 'done'}
CHECKS = {
    'selection': ('selection_rules_passed', 'not_previously_selected', 'ip_checks_passed'),
    'sourcing': ('same_product_verified', 'variants_verified', 'cost_weight_verified', 'image_rights_verified'),
}
ASIN = re.compile(r'[A-Z0-9]{10}')


def ranked_competitors(selected, market, candidate_asin):
    """Read ranked research seeds from the account's completed selection receipt."""
    if 'ranked_competitors' not in selected:
        return None
    rows = selected['ranked_competitors']
    if not isinstance(rows, list):
        return []
    accepted = []
    parents = {candidate_asin}
    candidate_parent = selected.get('candidate_parent_asin')
    if isinstance(candidate_parent, str) and ASIN.fullmatch(candidate_parent):
        parents.add(candidate_parent)
    seen = {candidate_asin}
    host = urlsplit(selected.get('front_search', {}).get('url', '')).hostname
    for row in rows[:30]:
        if not isinstance(row, dict) or row.get('marketplace') != market:
            continue
        asin, parent = row.get('asin'), row.get('parent_asin')
        if (not isinstance(asin, str) or not ASIN.fullmatch(asin) or asin in seen
                or not isinstance(parent, str) or not ASIN.fullmatch(parent) or parent in parents):
            continue
        if row.get('match_level') not in ('exact', 'strong') or not all(
            isinstance(row.get(key), str) and row[key].strip() for key in ('title', 'match_evidence')
        ):
            continue
        search, rank = row.get('search'), row.get('rank')
        if not isinstance(search, dict) or not isinstance(rank, dict):
            continue
        if (type(rank.get('value')) is not int or rank['value'] <= 0
                or not all(isinstance(rank.get(key), str) and rank[key].strip()
                           for key in ('category', 'source', 'raw', 'checked_at'))):
            continue
        try:
            search_url, rank_url = urlsplit(search['url']), urlsplit(rank['url'])
            if (not host or search_url.hostname != host or search_url.path != '/s'
                    or not isinstance(search.get('keyword'), str) or not search['keyword'].strip()
                    or parse_qs(search_url.query).get('k') != [search['keyword']]
                    or any(url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
                           for url in (search_url, rank_url))):
                continue
            date.fromisoformat(rank['checked_at'][:10])
            raw = rank['raw'].strip()
            visible = re.findall(r'#\s*([\d,]+)\s*in\s*([^\r\n]+)', raw, re.I)
            ranked = any(int(value.replace(',', '')) == rank['value']
                         and category.strip().casefold() == rank['category'].strip().casefold()
                         for value, category in visible if value.replace(',', '').isdigit())
            if rank['source'] == 'Amazon search page extension':
                ranked = ranked and rank_url == search_url and re.search(r'\b' + re.escape(asin) + r'\b', raw) is not None
            elif rank['source'] == 'Amazon product page':
                ranked = ranked and rank_url.hostname == host and rank_url.path.rstrip('/') == '/dp/' + asin
            elif rank['source'] == 'SellerSprite MCP':
                response = json.loads(raw)
                ranked = (rank_url.hostname == 'mcp.sellersprite.com' and rank_url.path == '/mcp'
                          and not rank_url.query and isinstance(response, dict) and response.get('asin') == asin
                          and type(response.get('bsrRank')) is int and response['bsrRank'] == rank['value'])
            else:
                ranked = False
            if not ranked:
                continue
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        accepted.append({
            'asin': asin, 'parent_asin': parent, 'marketplace': market,
            'product_url': f'https://{host}/dp/{asin}',
            'title': row['title'].strip()[:600], 'match_level': row['match_level'],
            'match_evidence': row['match_evidence'].strip()[:1000],
            'seller_id': str(row.get('seller_id') or '')[:100],
            'seller_name': str(row.get('seller_name') or '')[:200],
            'search': {key: search[key] for key in ('keyword', 'url')},
            'rank': {key: rank[key] for key in ('value', 'category', 'source', 'raw', 'url', 'checked_at')},
        })
        parents.add(parent)
        seen.add(asin)
        if len(accepted) == 5:
            break
    return accepted


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS discovery_product_links (
        owner INTEGER NOT NULL, market TEXT NOT NULL, asin TEXT NOT NULL,
        product_id TEXT NOT NULL, job_id TEXT NOT NULL, job_created TEXT NOT NULL,
        cycle_id TEXT NOT NULL, cycle_revision INTEGER NOT NULL,
        observation_id TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner, market, asin), UNIQUE(owner, job_id), UNIQUE(owner, product_id))''')


@contextmanager
def read_db(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only = ON')
        db.execute('BEGIN')
        yield db


def cli_path(root):
    path = root / 'pipeline.sqlite3'
    if path.resolve() != path or not path.is_file():
        raise ValueError('当前账号没有可读的本地 CLI 台账，请在本账号数据目录配置 pipeline.sqlite3')
    return path


def links(path, owner, product_id=None):
    if not path.is_file():
        return []
    with read_db(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_product_links' AND type='table'").fetchone():
            return []
        query = 'SELECT * FROM discovery_product_links WHERE owner=?'
        if product_id is not None:
            return db.execute(query + ' AND product_id=?', (owner, product_id)).fetchall()
        return db.execute(query + ' ORDER BY cycle_id, product_id', (owner,)).fetchall()


def assert_web_available(root, owner, product_id):
    if links(root / 'studio.sqlite3', owner, product_id):
        raise ValueError('该产品由已关联的 CLI 台账执行。请在 CLI 对账或续跑，网页不会重复执行。')


def completed(db, job, stage):
    rows = db.execute('SELECT data FROM events WHERE job_id=? AND stage=? AND action=? LIMIT 2',
                      (job, stage, 'completed')).fetchall()
    if len(rows) != 1:
        raise ValueError('缺少唯一的 CLI ' + stage + ' 完成回执')
    raw = rows[0]['data']
    evidence = json.loads(raw)
    if (evidence.get('job_id') != job or evidence.get('stage') != stage
            or evidence.get('simulated') is not False or not evidence.get('sources')
            or not all(evidence.get('checks', {}).get(key) is True for key in CHECKS[stage])):
        raise ValueError('CLI 完成回执缺少真实核验记录')
    return evidence, raw


def qualify(cli, owner, market, asin):
    job = cli.execute('SELECT id, stage, status, created_at FROM jobs WHERE market=? AND source_id=?',
                      (market, asin)).fetchone()
    if job is None or type(job['stage']) is not int or not 2 <= job['stage'] <= len(STAGES):
        raise ValueError('CLI 台账尚未完成选品与货源核验')
    if job['status'] not in STATUSES:
        raise ValueError('CLI 台账任务状态不明，须先对账')
    selection, selection_raw = completed(cli, job['id'], 'selection')
    sourcing, sourcing_raw = completed(cli, job['id'], 'sourcing')
    selected = selection.get('selection_evidence') or {}
    supply = sourcing.get('sourcing_evidence') or {}
    quote = supply.get('selected_quote') or {}
    try:
        profit = Decimal(str(quote.get('profit_at_market_price_rmb')))
    except InvalidOperation:
        profit = Decimal('NaN')
    supplier = str(supply.get('supplier_id') or '')
    supplier_url = 'https://detail.1688.com/offer/' + supplier + '.html'
    if (selected.get('asin') != asin or selected.get('marketplace') != market or not selected.get('seller_id')
            or selection.get('selection_reservation', {}).get('status') not in ('reserved_validation_candidate', 'already_reserved')
            or type(supply.get('accountId')) is not int or supply['accountId'] != owner
            or supply.get('job_id') != job['id'] or supply.get('asin') != asin
            or supply.get('seller_id') != selected['seller_id'] or supply.get('qualified') is not True
            or supply.get('qualification_scope') != 'positive_estimated_profit_under_configured_base_scenario'
            or quote.get('scope') != 'quote_based_draft_base_scenario' or not profit.is_finite() or profit <= 0
            or not re.fullmatch(r'[0-9]+', supplier)
            or not any(isinstance(source, dict) and source.get('url') == supplier_url
                       and source.get('raw') and source.get('checked_at') for source in sourcing['sources'])):
        raise ValueError('CLI 证据未证明本账号同一候选的排重、同款货源及报价情景正利润')
    digest = hashlib.sha256((selection_raw + '\n' + sourcing_raw).encode('utf-8')).hexdigest()
    proof = {'sellerId': selected['seller_id'], 'supplierUrl': supplier_url,
             'estimatedMarketProfitRmb': str(profit), 'profitScope': quote['scope'],
             'cliJobId': job['id'], 'evidenceSha256': digest}
    pricing=draft_pricing(supply,sourcing['sources'],supplier_url,market,digest)
    if pricing:proof['draftPricing']=pricing
    return job, proof, ranked_competitors(selected, market, asin)


def draft_pricing(supply,sources,supplier_url,market,digest):
    """Keep verified selection commerce as product facts, without CLI authority."""
    currency,key={'US':('USD','formula_price_usd'),'UK':('GBP','formula_price_gbp')}.get(market,(None,None))
    if not key:return None
    quote=supply.get('selected_quote') or {};cost=quote.get('purchase_price_rmb')
    supplier_sources=[s for s in sources if s.get('url')==supplier_url and s.get('checked_at')]
    if cost is None:
        found=[]
        for source in supplier_sources:
            raw=source.get('raw','')
            # Only an explicitly labelled procurement price in the qualified
            # supplier observation is parsed; freight/rank/market prices never are.
            for m in re.finditer(r'(?:采购|进货)(?:价)?\s*[:：]?\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)(?![\d.])',raw):
                if re.match(r'\s*[-–~至]',raw[m.end():]):return None
                found.append(m[1])
        if len(set(found))!=1:return None
        cost=found[0]
    try:
        cost,price=Decimal(str(cost)),Decimal(str(quote.get(key)))
        if not all(v.is_finite() and 0<v<10000000 for v in (cost,price)):return None
    except (InvalidOperation,ValueError):return None
    return {'marketplace':market,'currency':currency,'price':str(price),'purchaseCny':str(cost),
            'sourceUrl':supplier_url,'checkedAt':supplier_sources[0]['checked_at'] if supplier_sources else '',
            'scope':'quote_based_draft_base_scenario','basisSha256':digest}


def promote(db, root, user, data, workspace, web_products):
    owner = user['id']
    graph = cyclic_discovery.report(owner, data['cycleId'])
    if type(data['expectedRevision']) is not int or data['expectedRevision'] != graph['revision']:
        raise ValueError('发现批次已变化，请刷新后重新选择')
    keys = data['productKeys']
    if not isinstance(keys, list) or not 1 <= len(keys) <= 30 or any(not isinstance(key, str) for key in keys):
        raise ValueError('请选择1至30个发现候选')
    by_key = {node['id']: node for node in graph['products']}
    if any(key not in by_key for key in keys):
        raise ValueError('候选不属于当前账号的发现批次')
    events = {event['id']: event for event in graph['events']}
    outcomes = []
    with read_db(cli_path(root)) as cli:
        for key in dict.fromkeys(keys):
            node = by_key[key]
            market, asin = node['marketplace'], node['asin']
            outcome = {'key': key, 'asin': asin, 'status': 'blocked'}
            try:
                observed = next((s for s in node['sources'] if s.get('title') and s.get('similarity') != 'REJECT'
                                 and events[s['eventId']]['kind'] in ('search', 'store')), None)
                if observed is None:
                    raise ValueError('缺少搜索或店铺商品观察；标题种子不能入库')
                job, proof, competitors = qualify(cli, owner, market, asin)
                product = product_archive.find(workspace, market, asin)
                link = db.execute('''SELECT product_id, job_id, job_created, evidence_sha256
                                     FROM discovery_product_links WHERE owner=? AND market=? AND asin=?''',
                                  (owner, market, asin)).fetchone()
                if link:
                    if not product or tuple(link) != (product['id'], job['id'], job['created_at'], proof['evidenceSha256']):
                        raise ValueError('原档案或 CLI 回执已变化，保留关联并等待核对')
                    outcomes.append(dict(outcome, status='reused', productId=product['id'], cliJobId=job['id']))
                    continue
                if product and (product['id'] in web_products or db.execute(
                        'SELECT 1 FROM discovery_product_links WHERE owner=? AND product_id=?',
                        (owner, product['id'])).fetchone()):
                    raise ValueError('现有档案已关联任务，请先核对执行归属')
                candidate = {'market': market, 'asin': asin, 'title': observed['title']}
                research = {'sourceMonth': 'cyclic-discovery', 'cycleId': graph['cycleId'],
                            'cycleRevision': graph['revision'], 'observation': observed,
                            'qualification': proof}
                if competitors is not None:
                    research['rankedCompetitors'] = competitors
                product, created = product_archive.ensure(workspace, user, candidate, research)
                if product is None:
                    raise ValueError('原档案已删除或归档，请在原档案处处理')
                if proof.get('draftPricing'):
                    product.setdefault('erpDraftPricing',{})[market]=proof['draftPricing']
                    if created:
                        product['sourceUrl']=proof['supplierUrl']
                        product['purchasePrice']=proof['draftPricing']['purchaseCny']+' CNY'
                db.execute('INSERT INTO discovery_product_links VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (owner, market, asin, product['id'], job['id'], job['created_at'], graph['cycleId'],
                            graph['revision'], observed['eventId'], proof['evidenceSha256']))
                outcome.update(status='created' if created else 'linked', productId=product['id'], cliJobId=job['id'])
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                outcome['reason'] = str(error) if type(error) is ValueError else 'CLI 或发现证据结构异常，需核对'
            outcomes.append(outcome)
    return outcomes


def overview(root, owner, product_id=None):
    records = links(root / 'studio.sqlite3', owner, product_id)
    items = [{'id': r['job_id'], 'productId': r['product_id'], 'marketplace': r['market'],
              'asin': r['asin'], 'cycleId': r['cycle_id'], 'status': 'unavailable', 'stage': None,
              'executor': 'cli'} for r in records]
    if not items:
        return items
    try:
        with read_db(cli_path(root)) as cli:
            for record, item in zip(records, items):
                job = cli.execute('SELECT stage,status,updated_at FROM jobs WHERE id=? AND market=? AND source_id=? AND created_at=?',
                                  (record['job_id'], record['market'], record['asin'], record['job_created'])).fetchone()
                if not job or job['status'] not in STATUSES or type(job['stage']) is not int:
                    continue
                stage = job['stage']
                if 0 <= stage < len(STAGES) or (stage == len(STAGES) and job['status'] == 'done'):
                    item.update(status=job['status'], stage=STAGES[stage] if stage < len(STAGES) else 'complete',
                                updatedAt=job['updated_at'])
    except (ValueError, sqlite3.Error):
        pass  # Keep the durable product link visible, with unavailable status.
    return items
