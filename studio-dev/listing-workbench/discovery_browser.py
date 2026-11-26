"""Fail-closed, read-only search, seller and seller-listing observations. Never ingests or qualifies products."""
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

from app_paths import DATA
import cyclic_discovery as ledger
import runtime_environment

DB = DATA / 'discovery_browser.sqlite3'
COLLECTOR = Path(__file__).with_name('discovery-seller-collector.js')
STORE_COLLECTOR = Path(__file__).with_name('discovery-store-collector.js')
SEARCH_COLLECTOR = Path(__file__).with_name('discovery-search-collector.js')
FAMILY_COLLECTOR = Path(__file__).with_name('discovery-family-collector.js')
SEARCH_SELECTOR = '.s-main-slot'
SEARCH_CARD_SELECTOR = '[data-component-type="s-search-result"][data-asin]'
SEARCH_TITLE_SELECTOR = 'a.a-link-normal.s-link-style > h2.a-text-normal'
# Exact keyword route only; unknown filters are not assumed harmless.
SEARCH_QUERY_KEYS = frozenset(('k', 'ref', 'ref_', 'qid', 'sprefix', 'crid', 'marketplaceID'))
STORE_SELECTOR = '.s-main-slot'
STORE_CARD_SELECTOR = '[data-component-type="s-search-result"][data-asin]'
STORE_TITLE_SELECTOR = 'h2'
MARKETPLACE_IDS = {'US': 'ATVPDKIKX0DER', 'UK': 'A1F83G8C2ARO7P',
    'DE': 'A1PA6795UKMFR9', 'FR': 'A13V1IB3VIYZZH', 'IT': 'APJ6JRA9NG5V4',
    'ES': 'A1RKKUPIHCS9HS', 'JP': 'A1VC38T7YXB528', 'CA': 'A2EUQ1WTGCTBG2',
    'AU': 'A39IBJ37TRP1C6', 'IN': 'A21TJRUUN4KGV', 'MX': 'A1AM78C64UM0Y8',
    'BR': 'A2Q3Y263D00KWC'}
# Only me identifies a seller listing. Other identity/filter routes are not
# equivalent, even when their values appear to agree with me.
CONFLICTING_QUERY_KEYS = frozenset(('k', 'q', 'seller', 'sellerid', 'merchant',
    'merchantid', 'm', 'smid', 'field-keywords', 'field-merchant', 'rh', 'p_6'))
TIMEOUT = 45
ACTIVE = ('capture_started', 'execution_unknown')
UNRESOLVED_HISTORY = frozenset((*ACTIVE, 'unknown', 'running'))
TERMINAL_HISTORY = frozenset(('captured', 'blocked', 'configuration_blocked', 'stopped', 'inaccessible', 'needs_review', 'cancelled'))
BLOCK_REASONS = frozenset(('task_space_not_agent_owned', 'task_space_mismatch',
    'existing_product_tab_required', 'page_or_ownership_changed', 'invalid_url',
    'unsupported_amazon_host', 'login_required', 'captcha_or_challenge',
    'access_interstitial', 'not_product_page', 'missing_or_ambiguous_product_title',
    'invalid_or_ambiguous_seller_link', 'visible_sold_by_link_not_found',
    'conflicting_seller_evidence', 'existing_store_tab_required',
    'not_seller_listing', 'store_target_mismatch', 'missing_or_ambiguous_results',
    'invalid_or_ambiguous_product_card', 'no_visible_store_products',
    'unsupported_store_layout', 'existing_search_tab_required',
    'not_keyword_search', 'search_target_mismatch', 'no_visible_search_products',
    'unsupported_search_layout', 'navigation_target_mismatch', 'navigation_source_not_public',
    'navigation_stopped', 'navigation_deadline', 'navigation_lease_invalid'))



_DIAGNOSTIC_TYPES = frozenset(('undefined', 'object', 'boolean', 'number', 'bigint',
    'string', 'symbol', 'function', 'unavailable'))
_DIAGNOSTIC_CHECKS = {
    'page_handle': frozenset(('pageLabelMatches', 'pageSpaceMatches', 'pageTargetMatches')),
    'before_navigation_recheck': frozenset(('agentSpaceMatches', 'agentOwnership',
        'oneTab', 'tabTargetMatches', 'pageTargetMatches', 'urlCompared', 'urlMatches')),
    'after_navigation_recheck': frozenset(('agentSpaceMatches', 'agentOwnership',
        'oneTab', 'tabTargetMatches', 'pageTargetMatches', 'urlCompared', 'urlMatches')),
    'after_capture_recheck': frozenset(('agentSpaceMatches', 'agentOwnership',
        'oneTab', 'tabTargetMatches', 'pageTargetMatches', 'urlCompared', 'urlMatches')),
}
_DIAGNOSTIC_FIELDS = {
    'page_handle': frozenset(('pageLabel', 'pageSpace', 'pageTarget', 'tabTarget')),
    **{name: frozenset(('pageTarget', 'tabTarget', 'tabURL')) for name in
        ('before_navigation_recheck', 'after_navigation_recheck', 'after_capture_recheck')},
}


def _navigation_diagnostic(receipt):
    """Diagnostics explain only a refusal; they never grant execution authority."""
    if 'navigationDiagnostics' not in receipt:
        return None
    value = receipt['navigationDiagnostics']
    if (type(receipt.get('schemaVersion')) is not int or receipt.get('schemaVersion') != 1
            or receipt.get('status') != 'blocked' or receipt.get('reason') != 'page_or_ownership_changed'
            or not isinstance(value, dict) or set(value) != {'checkpoint', 'checks', 'types'}):
        raise ValueError('invalid_navigation_diagnostics')
    checkpoint = value.get('checkpoint')
    if not isinstance(checkpoint, str) or checkpoint not in _DIAGNOSTIC_CHECKS:
        raise ValueError('invalid_navigation_diagnostics')
    phase, navigated, collected = {
        'page_handle': ('before_navigation', False, False),
        'before_navigation_recheck': ('before_navigation', False, False),
        'after_navigation_recheck': ('after_navigation', True, False),
        'after_capture_recheck': ('collector_dispatched', True, True),
    }[checkpoint]
    if (receipt.get('navigationPhase') != phase
            or receipt.get('navigationDispatched') is not navigated
            or receipt.get('collectorDispatched') is not collected):
        raise ValueError('invalid_navigation_diagnostics')
    checks, types = value['checks'], value['types']
    if (not isinstance(checks, dict) or set(checks) != _DIAGNOSTIC_CHECKS[checkpoint]
            or any(type(v) is not bool for v in checks.values())
            or not isinstance(types, dict) or set(types) != _DIAGNOSTIC_FIELDS[checkpoint]
            or any(not isinstance(v, str) or v not in _DIAGNOSTIC_TYPES for v in types.values())):
        raise ValueError('invalid_navigation_diagnostics')
    return {'checkpoint': checkpoint, 'checks': dict(checks), 'types': dict(types)}


def _read_receipt(raw):
    receipt = json.loads(raw)
    if isinstance(receipt, dict):
        _navigation_diagnostic(receipt)
    return receipt


def _now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _request(user, data):
    ledger._owner(user)
    if not isinstance(data, dict) or set(data) != {'captureId', 'cycleId', 'expectedRevision', 'parentId', 'asin'}:
        raise ValueError('Expected captureId, cycleId, expectedRevision, parentId and asin only')
    result = {key: ledger._id(data.get(key), key) for key in ('captureId', 'cycleId', 'parentId')}
    result['asin'] = ledger._asin(data.get('asin'))
    revision = data.get('expectedRevision')
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid expectedRevision')
    result['expectedRevision'] = revision
    return result


def _store_request(user, data):
    ledger._owner(user)
    if not isinstance(data, dict) or set(data) != {'captureId', 'cycleId', 'expectedRevision', 'parentId', 'sellerId'}:
        raise ValueError('Expected captureId, cycleId, expectedRevision, parentId and sellerId only')
    result = {key: ledger._id(data.get(key), key) for key in ('captureId', 'cycleId', 'parentId')}
    seller = ledger._text(data.get('sellerId'), 'sellerId', 30)
    if not re.fullmatch('[A-Z0-9]{5,30}', seller):
        raise ValueError('Invalid sellerId')
    revision = data.get('expectedRevision')
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid expectedRevision')
    result.update(sellerId=seller, expectedRevision=revision)
    return result


def _store_parent(user, request):
    report = ledger.report(user, request['cycleId'])
    if report['revision'] != request['expectedRevision']:
        raise ValueError('stale_revision')
    parents = [e for e in report['events'] if e['id'] == request['parentId']]
    if len(parents) != 1:
        raise ValueError('parent_seller_mismatch')
    parent = parents[0]
    if parent.get('kind') != 'seller' or parent.get('sellerId') != request['sellerId']:
        raise ValueError('parent_seller_mismatch')
    # Recheck observation structure, not a seller name/brand or a synthetic node.
    ledger._clean(parent)
    return parent


def _search_request(user, data):
    ledger._owner(user)
    if not isinstance(data, dict) or set(data) != {'captureId', 'cycleId', 'expectedRevision', 'parentId'}:
        raise ValueError('Expected captureId, cycleId, expectedRevision and parentId only')
    result = {key: ledger._id(data.get(key), key) for key in ('captureId', 'cycleId', 'parentId')}
    revision = data.get('expectedRevision')
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid expectedRevision')
    result['expectedRevision'] = revision
    return result


def _search_parent(user, request):
    report = ledger.report(user, request['cycleId'])
    if report['revision'] != request['expectedRevision']:
        raise ValueError('stale_revision')
    parents = [e for e in report['events'] if e['id'] == request['parentId']]
    if len(parents) != 1 or parents[0].get('kind') != 'keyword':
        raise ValueError('parent_keyword_mismatch')
    parent = parents[0]
    ledger._clean(parent)
    return parent


def _public_url(value, market):
    value = ledger._text(value, 'url', 4000)
    if re.search(r'[\\\x00-\x20]', value) or re.search(r'/(?:\.|%2e){1,2}(?:/|$)', value.split('?', 1)[0].split('#', 1)[0], re.I):
        raise ValueError('Ambiguous URL encoding')
    p = urlsplit(value)
    host = (p.hostname or '').lower().removeprefix('www.')
    if p.scheme != 'https' or p.username is not None or p.password is not None or p.port not in (None, 443) or ledger.MARKETS.get(host) != market:
        raise ValueError('URL must be official same-market Amazon HTTPS')
    return p, host, parse_qsl(p.query, keep_blank_values=True)


def _store_url(value, market, seller):
    p, host, pairs = _public_url(value, market)
    me = [(k, v) for k, v in pairs if k.lower() == 'me']
    markets = [(k, v) for k, v in pairs if k.lower() == 'marketplaceid']
    if p.path != '/s' or me != [('me', seller)] or any(k.lower() in CONFLICTING_QUERY_KEYS for k, _ in pairs):
        raise ValueError('Seller listing URL mismatch')
    if markets and markets != [('marketplaceID', MARKETPLACE_IDS[market])]:
        raise ValueError('Marketplace identifier mismatch')
    return urlunsplit(('https', host, '/s', urlencode({'me': seller}), ''))


def _store_product_url(value, market, asin, seller):
    p, host, pairs = _public_url(value, market)
    match = re.fullmatch(r'(?:/(?:[^/]+)/dp|/dp|/gp/product)/([A-Za-z0-9]{10})(?:/[^?#]*)?', p.path)
    if not match or match[1].upper() != asin or any(k.lower() in (CONFLICTING_QUERY_KEYS - {'m'}) | {'me'} for k, _ in pairs):
        raise ValueError('Product card URL mismatch')
    merchants = [(k, v) for k, v in pairs if k.lower() == 'm']
    markets = [(k, v) for k, v in pairs if k.lower() == 'marketplaceid']
    if merchants and merchants != [('m', seller)]:
        raise ValueError('Product card seller mismatch')
    if markets and markets != [('marketplaceID', MARKETPLACE_IDS[market])]:
        raise ValueError('Product card marketplace mismatch')
    return urlunsplit(('https', host, '/dp/' + asin, '', ''))


def _search_url(value, market, query):
    p, host, pairs = _public_url(value, market)
    keywords = [(k, v) for k, v in pairs if k.lower() == 'k']
    markets = [(k, v) for k, v in pairs if k.lower() == 'marketplaceid']
    if p.path != '/s' or keywords != [('k', query)] or any(k not in SEARCH_QUERY_KEYS for k, _ in pairs):
        raise ValueError('Keyword search URL mismatch')
    if markets and markets != [('marketplaceID', MARKETPLACE_IDS[market])]:
        raise ValueError('Marketplace identifier mismatch')
    return urlunsplit(('https', host, '/s', urlencode({'k': query}), ''))


def _search_product_url(value, market, asin):
    p, host, pairs = _public_url(value, market)
    match = re.fullmatch(r'(?:/(?:[^/]+)/dp|/dp|/gp/product)/([A-Za-z0-9]{10})(?:/[^?#]*)?', p.path)
    if not match or match[1].upper() != asin or any(k.lower() in CONFLICTING_QUERY_KEYS | {'me'} for k, _ in pairs):
        raise ValueError('Product card URL mismatch')
    markets = [(k, v) for k, v in pairs if k.lower() == 'marketplaceid']
    if markets and markets != [('marketplaceID', MARKETPLACE_IDS[market])]:
        raise ValueError('Product card marketplace mismatch')
    return urlunsplit(('https', host, '/dp/' + asin, '', ''))


def _connect(write=False):
    if not write and not DB.exists():
        return None
    if write:
        DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB) if write else DB.resolve().as_uri() + '?mode=ro', uri=not write, timeout=20)
    if write:
        conn.execute('CREATE TABLE IF NOT EXISTS attempts (owner INTEGER NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(owner,id))')
        conn.commit()
    return conn


def _history_blocks(c, user):
    """Unrecognized or corrupt owner history cannot establish safe dispatch."""
    for capture_id, status, raw in c.execute('SELECT id,status,receipt FROM attempts WHERE owner=?', (user,)):
        try:
            receipt = _read_receipt(raw)
            if (not isinstance(receipt, dict) or receipt.get('captureId') != capture_id
                    or receipt.get('status') != status
                    or not isinstance(receipt.get('cycleId'), str)
                    or not isinstance(receipt.get('parentId'), str)
                    or receipt.get('captureKind', 'seller') not in ('seller', 'store', 'search', 'family')
                    or status in UNRESOLVED_HISTORY or status not in TERMINAL_HISTORY):
                return True
        except (TypeError, ValueError):
            return True
    return False


def history(user):
    ledger._owner(user)
    c = _connect()
    if c is None:
        return {'items': []}
    try:
        return {'items': [_read_receipt(r[0]) for r in c.execute('SELECT receipt FROM attempts WHERE owner=? ORDER BY rowid DESC LIMIT 100', (user,))]}
    finally:
        c.close()


def detail(user, capture_id):
    ledger._owner(user)
    capture_id = ledger._id(capture_id, 'captureId')
    c = _connect()
    try:
        row = c.execute('SELECT receipt FROM attempts WHERE owner=? AND id=?', (user, capture_id)).fetchone() if c else None
        if row is None:
            raise ValueError('Capture not found')
        return _read_receipt(row[0])
    finally:
        if c:
            c.close()


def _binding(user):
    config = runtime_environment.configuration()
    browser = config.get('discovery_browser', {})
    if not isinstance(browser, dict):
        raise ValueError('discovery_binding_required')
    bindings = browser.get('account_bindings', {})
    binding = bindings.get(str(user)) if isinstance(bindings, dict) else None
    if not isinstance(binding, dict) or binding.get('enabled') is not True or browser.get('engine') != 'ego-lite':
        raise ValueError('discovery_binding_required')
    sid, name, label = (binding.get(k) for k in ('task_space_id', 'task_space_name', 'page_label'))
    if type(sid) is not int or sid < 0 or not isinstance(name, str) or not name.strip() or not isinstance(label, str) or not label.strip() or len(label) > 100 or len(name) > 200:
        raise ValueError('invalid_discovery_binding')
    # ERP has its own route. Never observe through its task space.
    erp = config.get('browser', {})
    erp_sid = erp.get('task_space_id') if isinstance(erp, dict) else None
    if type(erp_sid) is int and erp_sid >= 0 and sid == erp_sid:
        raise ValueError('discovery_space_collides_with_erp')
    cli = browser.get('cli')
    if not isinstance(cli, str) or not Path(cli).is_absolute() or not Path(cli).is_file():
        raise ValueError('absolute_installed_cli_required')
    route = {'cli': str(Path(cli).resolve()), 'spaceId': sid, 'name': name, 'label': label}
    # Only the effective route for this account defines observation scope.
    # The collision guard above is rechecked on every pre/post-dispatch read.
    return route, _digest({'engine': browser['engine'], 'account': str(user), 'enabled': True, **route})


def _parent(user, request):
    report = ledger.report(user, request['cycleId'])
    if report['revision'] != request['expectedRevision']:
        raise ValueError('stale_revision')
    parent = next((e for e in report['events'] if e['id'] == request['parentId']), None)
    if not parent or parent['kind'] != 'search' or not any(p['asin'] == request['asin'] for p in parent['products']):
        raise ValueError('parent_search_mismatch')
    return parent


def _family_parent(user, request):
    report = ledger.report(user, request['cycleId'])
    if report['revision'] != request['expectedRevision']:
        raise ValueError('stale_revision')
    parent = next((e for e in report['events'] if e['id'] == request['parentId']), None)
    if not parent:
        raise ValueError('family_source_missing')
    kind = parent.get('kind')
    matches = (kind in ('keyword', 'seller') and parent.get('asin') == request['asin']) or (
        (kind == 'search' or (kind == 'store' and parent.get('status') == 'captured'))
        and any(p.get('asin') == request['asin'] for p in parent.get('products', [])))
    if not matches:
        raise ValueError('family_source_mismatch')
    ledger._clean(parent)
    return parent


def _runtime_script(binding, target, market, result_path, matcher, collector, missing, navigation=None):
    host = next(h for h, m in ledger.MARKETS.items() if m == market)
    params = {**binding, **target, 'host': host, 'output': str(result_path.resolve())}
    params.pop('cli')
    if navigation is not None:
        params.update(navigation)
        return _navigation_runtime(params, matcher, collector)
    return '''const p = PARAMS;
const fs = await import('node:fs/promises');
const fail = reason => ({schemaVersion:1,status:'blocked',reason});
MATCHER
let result;
try {
  const spaces=await listTaskSpaces();
  const owned=spaces.filter(s=>s.id===p.spaceId && s.name===p.name && s.ownership==='agent');
  if (owned.length!==1) result=fail('task_space_not_agent_owned');
  else {
    const task=await taskSpace(p.spaceId);
    if(task.spaceId!==p.spaceId || task.name!==p.name || task.ownership!=='agent') result=fail('task_space_mismatch');
    else {
      const before=(await task.tabs()).filter(t=>t.label===p.label);
      if(before.length!==1 || !before[0].targetId || !matches(before[0].url)) result=fail(MISSING);
      else {
        const page=task.page(p.label);
        result=await page.evaluate(COLLECTOR);
        const after=(await task.tabs()).filter(t=>t.label===p.label);
        const current=(await listTaskSpaces()).filter(s=>s.id===p.spaceId && s.name===p.name && s.ownership==='agent');
        if(current.length!==1 || task.ownership!=='agent' || after.length!==1 || after[0].targetId!==before[0].targetId || !matches(after[0].url) || after[0].url!==before[0].url) result=fail('page_or_ownership_changed');
      }
    }
  }
} catch (_) { result={schemaVersion:1,status:'execution_unknown',reason:'runtime_outcome_unknown'}; }
await fs.writeFile(p.output,JSON.stringify(result),{encoding:'utf8',flag:'wx'});
'''.replace('MATCHER', matcher).replace('MISSING', json.dumps(missing)).replace('COLLECTOR', collector).replace('PARAMS', json.dumps(params, ensure_ascii=True), 1)



def _navigation_runtime(params, matcher, collector):
    # This template is internal only. URLs and JavaScript never come from HTTP.
    # Only official SDK calls and the fixed checked-in DOM collector are used.
    template = r'''const p = PARAMS;
const fs = await import('node:fs/promises');
const fail = (reason, diagnostics) => ({schemaVersion:1,status:'blocked',reason,...(diagnostics?{navigationDiagnostics:diagnostics}:{})});
const unavailable=Symbol('unavailable');
const metadata=read=>{try{return read();}catch(_){return unavailable;}};
const metadataType=value=>value===unavailable?'unavailable':typeof value;
const handleDiagnostic=(page,tab)=>{
  const label=metadata(()=>page.label), space=metadata(()=>page.spaceId), target=metadata(()=>page.targetId);
  return {checkpoint:'page_handle',
    checks:{pageLabelMatches:label===p.label,pageSpaceMatches:space===p.spaceId,pageTargetMatches:target===tab.targetId},
    types:{pageLabel:metadataType(label),pageSpace:metadataType(space),pageTarget:metadataType(target),tabTarget:metadataType(tab.targetId)}};
};
let latestDiagnostic;
MATCHER
const accessReasons=new Set(['login_required','captcha_or_challenge','access_interstitial']);
const targetMatches=value=>{
  if(typeof value!=='string' || /[\\\u0000-\u0020]/.test(value) || /\/(?:\.|%2e){1,2}(?:\/|$)/i.test(value.split(/[?#]/,1)[0]))return false;
  try {
    const u=new URL(value), target=new URL(p.destination);
    if(u.hash || !matches(value))return false;
    if(target.pathname==='/s')return true;
    const conflicts=['k','q','seller','sellerid','merchant','merchantid','m','smid','field-keywords','field-merchant','rh','p_6','me','asin'];
    return ![...u.searchParams.keys()].some(k=>conflicts.includes(k.toLowerCase()));
  }catch(_){return false;}
};

let stage='before_navigation', navigated=false, collected=false;
const cancelled=async()=>{
  if(!Number.isFinite(p.deadline) || Date.now()/1000>=p.deadline)return 'navigation_deadline';
  try {await fs.stat(p.cancelPath);return 'navigation_stopped';}
  catch(e){if(e.code!=='ENOENT')return 'navigation_lease_invalid';}
  return null;
};
const sourceAllowed=value=>{
  if(value==='about:blank')return true;
  try {
    if(typeof value!=='string' || /[\\\u0000-\u0020]/.test(value) || /\/(?:\.|%2e){1,2}(?:\/|$)/i.test(value.split(/[?#]/,1)[0]))return false;
    const u=new URL(value);
    if(u.protocol!=='https:' || u.username || u.password || u.port || u.hash || u.hostname.replace(/^www\./,'')!==p.host)return false;
    if(/^\/(?:ap\/|gp\/sign-in|signin)/i.test(u.pathname) || /captcha|challenge/i.test(u.pathname))return false;
    if(/^\/(?:[^/]+\/dp|dp|gp\/product)\/[A-Z0-9]{10}(?:\/(?:ref=[A-Za-z0-9_.=-]+)?)?$/i.test(u.pathname))return true;
    if(u.pathname!=='/s')return false;
    const pairs=[...u.searchParams.entries()], keys=pairs.map(([k])=>k);
    return pairs.length>0 && new Set(keys).size===keys.length && keys.every(k=>['k','me','ref','ref_','qid','sprefix','crid','marketplaceID'].includes(k)) && (keys.includes('k')!==keys.includes('me'));
  }catch(_){return false;}
};
const perform=async()=>{
  let stop=await cancelled();if(stop)return fail(stop);
  const spaces=await listTaskSpaces();
  if(spaces.filter(s=>s.id===p.spaceId && s.name===p.name && s.ownership==='agent').length!==1)return fail('task_space_not_agent_owned');
  const task=await taskSpace(p.spaceId);
  if(task.spaceId!==p.spaceId || task.name!==p.name || task.ownership!=='agent')return fail('task_space_mismatch');
  const before=(await task.tabs()).filter(t=>t.label===p.label);
  if(before.length!==1 || typeof before[0].targetId!=='string' || !before[0].targetId || !sourceAllowed(before[0].url))return fail('navigation_source_not_public');
  // task.page(label) is lazy in EGO 0.6.2.7: targetId is undefined until
  // resolve. tabs() supplies a Page hydrated from this exact inventory entry.
  // Do not construct a fresh lazy handle or fall back if binding is absent.
  // SDK actions still re-resolve labels: pre/post checks detect drift but
  // cannot atomically pin an action against concurrent browser changes.
  const page=before[0].page;
  if(!page || page.label!==p.label || page.spaceId!==p.spaceId || page.targetId!==before[0].targetId)return fail('page_or_ownership_changed',handleDiagnostic(page,before[0]));
  if(before[0].openedBy!=='agent' || page.openedBy!=='agent')return fail('page_or_ownership_changed');
  const unchanged=async(url,checkpoint)=>{
    const current=(await listTaskSpaces()).filter(s=>s.id===p.spaceId && s.name===p.name && s.ownership==='agent');
    const tabs=(await task.tabs()).filter(t=>t.label===p.label);
    const matched=current.length===1 && task.ownership==='agent' && tabs.length===1 && tabs[0].openedBy==='agent' && tabs[0].targetId===before[0].targetId && tabs[0].page && tabs[0].page.label===p.label && tabs[0].page.spaceId===p.spaceId && tabs[0].page.targetId===before[0].targetId && tabs[0].page.openedBy==='agent' && page.label===p.label && page.spaceId===p.spaceId && page.openedBy==='agent' && page.targetId===before[0].targetId && (url===null || tabs[0].url===url) ? tabs[0] : null;
    if(!matched){
      const tab=tabs.length===1?tabs[0]:null, target=metadata(()=>page.targetId);
      latestDiagnostic={checkpoint,checks:{agentSpaceMatches:current.length===1,agentOwnership:task.ownership==='agent',
        oneTab:tabs.length===1,tabTargetMatches:!!tab && tab.targetId===before[0].targetId,
        pageTargetMatches:target===before[0].targetId,urlCompared:url!==null,urlMatches:url===null || (!!tab && tab.url===url)},
        types:{pageTarget:metadataType(target),tabTarget:metadataType(tab?tab.targetId:undefined),tabURL:metadataType(tab?tab.url:undefined)}};
    }
    return matched;
  };
  // Do not navigate away from a visible login/challenge on an existing page.
  // The same fixed collector checks access gates before its content selectors.
  if(before[0].url!=='about:blank'){
    stop=await cancelled();if(stop)return fail(stop);
    const initial=await page.evaluate(COLLECTOR);
    if(!initial || !['blocked','captured'].includes(initial.status))throw Error('initial outcome unknown');
    if(initial.status==='blocked' && accessReasons.has(initial.reason))return fail(initial.reason);
  }
  stop=await cancelled();if(stop)return fail(stop);
  if(!await unchanged(before[0].url,'before_navigation_recheck'))return fail('page_or_ownership_changed',latestDiagnostic);
  stop=await cancelled();if(stop)return fail(stop);
  stage='navigation_dispatched';navigated=true;
  const navigationReceipt=await page.goto(p.destination,{timeout:Math.max(1,Math.min(20000,Math.floor((p.deadline-Date.now()/1000)*1000)))});
  stage='after_navigation';
  // A fulfilled goto can contain a dialog or popup rather than navigation
  // success. Only the SDK's empty normal receipt is supported; do not retry.
  if(!navigationReceipt || typeof navigationReceipt!=='object' || Object.getPrototypeOf(navigationReceipt)!==Object.prototype || Reflect.ownKeys(navigationReceipt).length!==0)throw Error('navigation outcome unknown');
  // A stop cannot undo an in-flight read-only goto; it forbids the next read.
  stop=await cancelled();if(stop)return fail(stop);
  const selected=await unchanged(null,'after_navigation_recheck');
  if(!selected)return fail('page_or_ownership_changed',latestDiagnostic);
  if(!targetMatches(selected.url))return fail('navigation_target_mismatch');
  stop=await cancelled();if(stop)return fail(stop);
  stage='collector_dispatched';collected=true;
  const result=await page.evaluate(COLLECTOR);
  stop=await cancelled();if(stop)return fail(stop);
  const after=await unchanged(selected.url,'after_capture_recheck');
  if(!after || !targetMatches(after.url))return fail('page_or_ownership_changed',latestDiagnostic);
  if(result && result.status==='captured')result.navigationEvidence={spaceId:p.spaceId,name:p.name,label:p.label,destination:p.destination,selectedURL:after.url};
  stage='finished';
  return result;
};
let result;
try{result=await perform();}catch(_){result={schemaVersion:1,status:'execution_unknown',reason:'runtime_outcome_unknown'};}
if(!result || typeof result!=='object')result={schemaVersion:1,status:'execution_unknown',reason:'runtime_outcome_unknown'};
result.navigationPhase=stage;result.navigationDispatched=navigated;result.collectorDispatched=collected;
await fs.writeFile(p.output,JSON.stringify(result),{encoding:'utf8',flag:'wx'});
'''
    return template.replace('MATCHER', matcher).replace('COLLECTOR', collector).replace('PARAMS', json.dumps(params, ensure_ascii=True), 1)


def _destination(kind, request, parent):
    market = parent['marketplace']
    host = next(h for h, m in ledger.MARKETS.items() if m == market)
    if kind == 'search':
        return _search_url('https://' + host + '/s?' + urlencode({'k': parent['phrase']}), market, parent['phrase'])
    if kind == 'store':
        return _store_url('https://' + host + '/s?' + urlencode({'me': request['sellerId']}), market, request['sellerId'])
    return _search_product_url('https://' + host + '/dp/' + request['asin'], market, request['asin'])


def _validate_navigation(raw, binding, kind, request, parent):
    proof = raw.get('navigationEvidence')
    destination = _destination(kind, request, parent)
    if (not isinstance(proof, dict) or proof.get('spaceId') != binding['spaceId']
            or proof.get('name') != binding['name'] or proof.get('label') != binding['label']
            or proof.get('destination') != destination
            or raw.get('navigationDispatched') is not True or raw.get('collectorDispatched') is not True
            or raw.get('navigationPhase') != 'finished'):
        raise ValueError('navigation_evidence_invalid')
    selected = proof.get('selectedURL')
    if kind == 'search':
        actual = _search_url(selected, parent['marketplace'], parent['phrase'])
    elif kind == 'store':
        actual = _store_url(selected, parent['marketplace'], request['sellerId'])
    else:
        actual = _search_product_url(selected, parent['marketplace'], request['asin'])
    if actual != destination:
        raise ValueError('navigation_evidence_invalid')
    return {'destination': destination, 'selectedURL': actual, 'readOnly': True}


def _space_history_blocks(c, binding):
    # Local attempts share one namespace across accounts and capture kinds.
    # This is not a cross-runtime/machine mutex; isolated live mode enforces
    # its stricter deployment gate in discovery_live.
    for status, raw in c.execute('SELECT status,receipt FROM attempts'):
        try:
            receipt = _read_receipt(raw)
            if not isinstance(receipt, dict) or receipt.get('status') != status:
                return True
            target = receipt.get('target')
            unresolved = status in UNRESOLVED_HISTORY or status not in TERMINAL_HISTORY
            if unresolved and (not isinstance(target, dict)
                    or type(target.get('taskSpaceId')) is not int
                    or target['taskSpaceId'] == binding['spaceId']):
                return True
        except (ValueError, TypeError):
            return True
    return False


def _script(binding, request, market, result_path, navigation=None):
    matcher = '''const matches = value => {
  try { const u=new URL(value); const q=[...u.searchParams.keys()];
    const m=u.pathname.match(/^(?:\\/(?:[^/]+)\\/dp|\\/dp|\\/gp\\/product)\\/([A-Za-z0-9]{10})(?:\\/.*)?$/i);
    return u.protocol==='https:' && !u.username && !u.password && (!u.port || u.port==='443') && u.hostname.replace(/^www\\./,'')===p.host && m && m[1].toUpperCase()===p.asin && !q.some(k=>['k','me','seller'].includes(k));
  } catch (_) {return false;}
};'''
    return _runtime_script(binding, {'asin': request['asin']}, market, result_path, matcher,
        COLLECTOR.read_text(encoding='utf-8').strip(), 'existing_product_tab_required', navigation)


def _family_script(binding, request, market, result_path, navigation=None):
    matcher = r'''const matches = value => {
  try {
    if (typeof value!=='string' || /[\\\u0000-\u0020]/.test(value)) return false;
    const u=new URL(value), q=[...u.searchParams.keys()];
    const m=u.pathname.match(/^(?:\/(?:[^/]+)\/dp|\/dp|\/gp\/product)\/([A-Za-z0-9]{10})(?:\/.*)?$/i);
    return u.protocol==='https:' && !u.username && !u.password && !u.port && u.hostname.replace(/^www\./,'')===p.host && m && m[1].toUpperCase()===p.asin && !q.some(k=>p.conflicts.includes(k.toLowerCase()));
  } catch (_) {return false;}
};'''
    expected = json.dumps({'asin': request['asin'], 'marketplace': market}, ensure_ascii=True)
    collector = '() => (' + FAMILY_COLLECTOR.read_text(encoding='utf-8').strip() + ')(' + expected + ')'
    return _runtime_script(binding, {'asin': request['asin'], 'conflicts': sorted(CONFLICTING_QUERY_KEYS | {'me'})}, market, result_path, matcher, collector, 'existing_product_tab_required', navigation)


def _validate_family(raw, request, parent, started):
    import discovery_family
    evidence = discovery_family.normalize_capture(raw, marketplace=parent['marketplace'], asin=request['asin'], started_at=started)
    if datetime.fromisoformat(evidence['observedAt'].replace('Z', '+00:00')) < datetime.fromisoformat(parent['observedAt'].replace('Z', '+00:00')):
        raise ValueError('capture_time_mismatch')
    return None, evidence


def _store_script(binding, request, market, result_path, navigation=None):
    matcher = r'''const matches = value => {
  try {
    if (typeof value!=='string' || /[\\\u0000-\u0020]/.test(value) || /\/(?:\.|%2e){1,2}(?:\/|$)/i.test(value.split(/[?#]/,1)[0])) return false;
    const u=new URL(value), pairs=[...u.searchParams.entries()];
    const me=pairs.filter(([k])=>k.toLowerCase()==='me');
    const markets=pairs.filter(([k])=>k.toLowerCase()==='marketplaceid');
    return u.protocol==='https:' && !u.username && !u.password && !u.port && u.hostname.replace(/^www\./,'')===p.host && u.pathname==='/s' && me.length===1 && me[0][0]==='me' && me[0][1]===p.sellerId && !pairs.some(([k])=>p.conflicts.includes(k.toLowerCase())) && (!markets.length || (markets.length===1 && markets[0][0]==='marketplaceID' && markets[0][1]===p.marketplaceId));
  } catch (_) {return false;}
};'''
    expected = json.dumps({'sellerId': request['sellerId'], 'marketplace': market}, ensure_ascii=True)
    collector = '() => (' + STORE_COLLECTOR.read_text(encoding='utf-8').strip() + ')(' + expected + ')'
    return _runtime_script(binding, {'sellerId': request['sellerId'], 'marketplaceId': MARKETPLACE_IDS[market], 'conflicts': sorted(CONFLICTING_QUERY_KEYS)}, market, result_path, matcher, collector, 'existing_store_tab_required', navigation)


def _search_script(binding, request, market, result_path, navigation=None):
    matcher = r'''const matches = value => {
  try {
    if (typeof value!=='string' || /[\\\u0000-\u0020]/.test(value) || /\/(?:\.|%2e){1,2}(?:\/|$)/i.test(value.split(/[?#]/,1)[0])) return false;
    const u=new URL(value), pairs=[...u.searchParams.entries()];
    const keywords=pairs.filter(([k])=>k.toLowerCase()==='k');
    const markets=pairs.filter(([k])=>k.toLowerCase()==='marketplaceid');
    return u.protocol==='https:' && !u.username && !u.password && !u.port && u.hostname.replace(/^www\./,'')===p.host && u.pathname==='/s' && keywords.length===1 && keywords[0][0]==='k' && keywords[0][1]===p.query && pairs.every(([k])=>p.queryKeys.includes(k)) && (!markets.length || (markets.length===1 && markets[0][0]==='marketplaceID' && markets[0][1]===p.marketplaceId));
  } catch (_) {return false;}
};'''
    expected = json.dumps({'query': request['query'], 'marketplace': market}, ensure_ascii=True)
    collector = '() => (' + SEARCH_COLLECTOR.read_text(encoding='utf-8').strip() + ')(' + expected + ')'
    return _runtime_script(binding, {'query': request['query'], 'marketplaceId': MARKETPLACE_IDS[market], 'queryKeys': sorted(SEARCH_QUERY_KEYS)}, market, result_path, matcher, collector, 'existing_search_tab_required', navigation)


def _validate(raw, request, parent, started):
    if not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw.get('schemaVersion') != 1 or raw.get('status') != 'captured':
        raise ValueError('invalid_capture_result')
    if raw.get('asin') != request['asin'] or raw.get('marketplace') != parent['marketplace']:
        raise ValueError('capture_target_mismatch')
    event = ledger._clean({**raw, 'id': 'capture-' + _digest(request['captureId'])[:40], 'kind': 'seller', 'parentId': request['parentId'], 'sourceType': 'browser_visible', 'observedAt': raw.get('capturedAt')})
    dt = datetime.fromisoformat(event['observedAt'])
    parent_time = datetime.fromisoformat(parent['observedAt'].replace('Z', '+00:00'))
    if dt < started - timedelta(seconds=5) or dt > _now() + timedelta(seconds=5) or dt < parent_time:
        raise ValueError('capture_time_mismatch')
    ledger._text(raw.get('title'), 'title')
    evidence = raw.get('evidence')
    if not isinstance(evidence, dict):
        raise ValueError('seller_evidence_required')
    safe = {k: ledger._text(evidence.get(k), k, 4000 if k == 'anchorHref' else 2000) for k in ('selector', 'labelText', 'anchorText', 'anchorHref')}
    allowed = ('#merchant-info', '#merchantInfoFeature_feature_div, [offer-display-feature-name="merchantInfo"], .offer-display-feature-merchant-info')
    if safe['selector'] not in allowed or safe['labelText'] != event['sellerLabel'] or safe['anchorText'] != event['sellerName'] or ledger._url(safe['anchorHref'], parent['marketplace'], 'seller', event['sellerId']) != event['sellerLink']:
        raise ValueError('seller_evidence_mismatch')
    labels = ('sold by', 'verkauf durch', 'vendu par', 'venduto da', 'vendido por', '販売元')
    combined = parent['marketplace'] == 'US' and re.match(r'^shipper\s*/\s*seller(?:\s|:|$)', safe['labelText'].casefold())
    if not combined and not any(label in safe['labelText'].casefold() for label in labels):
        raise ValueError('sold_by_label_required')
    safe['anchorHref'] = event['sellerLink']
    return event, safe


def _validate_store(raw, request, parent, started):
    if not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw.get('schemaVersion') != 1 or raw.get('status') != 'captured':
        raise ValueError('invalid_capture_result')
    market, seller = parent['marketplace'], request['sellerId']
    if raw.get('sellerId') != seller or parent['sellerId'] != seller or raw.get('marketplace') != market:
        raise ValueError('capture_target_mismatch')
    url = _store_url(raw.get('url'), market, seller)
    event = ledger._clean({**raw, 'url': url, 'id': 'capture-store-' + _digest(request['captureId'])[:40], 'kind': 'store', 'parentId': request['parentId'], 'sourceType': 'browser_visible', 'observedAt': raw.get('capturedAt')})
    dt = datetime.fromisoformat(event['observedAt'])
    parent_time = datetime.fromisoformat(parent['observedAt'].replace('Z', '+00:00'))
    if dt < started - timedelta(seconds=5) or dt > _now() + timedelta(seconds=5) or dt < parent_time:
        raise ValueError('capture_time_mismatch')
    evidence = raw.get('evidence')
    if not isinstance(evidence, dict) or evidence.get('selector') != STORE_SELECTOR or evidence.get('sellerId') != seller or evidence.get('marketplace') != market or evidence.get('capturedAt') != raw.get('capturedAt') or _store_url(evidence.get('sourceURL'), market, seller) != url:
        raise ValueError('store_evidence_mismatch')
    products, cards = raw.get('products'), evidence.get('cards')
    if not isinstance(cards, list) or len(cards) != len(products) or len(products) != len(event['products']):
        raise ValueError('store_card_evidence_required')
    safe_cards = []
    for product, card, clean in zip(products, cards, event['products']):
        if not isinstance(card, dict) or product.get('asin') != clean['asin'] or card.get('asin') != clean['asin'] or card.get('cardAsin') != clean['asin'] or product.get('title') != clean['title'] or card.get('title') != clean['title']:
            raise ValueError('store_card_evidence_mismatch')
        if card.get('cardSelector') != STORE_CARD_SELECTOR or card.get('titleSelector') != STORE_TITLE_SELECTOR or card.get('visible') is not True or card.get('sponsored') is not False:
            raise ValueError('store_card_structure_mismatch')
        product_url = _store_product_url(product.get('productURL'), market, clean['asin'], seller)
        if _store_product_url(card.get('anchorHref'), market, clean['asin'], seller) != product_url:
            raise ValueError('store_card_link_mismatch')
        safe_cards.append({'asin': clean['asin'], 'cardAsin': clean['asin'], 'title': clean['title'], 'productURL': product_url, 'anchorHref': product_url, 'cardSelector': STORE_CARD_SELECTOR, 'titleSelector': STORE_TITLE_SELECTOR, 'visible': True, 'sponsored': False})
    return event, {'selector': STORE_SELECTOR, 'sellerId': seller, 'marketplace': market,
        'sourceURL': url, 'capturedAt': event['observedAt'], 'cards': safe_cards}


def _validate_search(raw, request, parent, started):
    if not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw.get('schemaVersion') != 1 or raw.get('status') != 'captured':
        raise ValueError('invalid_capture_result')
    market, query = parent['marketplace'], parent['phrase']
    if raw.get('query') != query or raw.get('marketplace') != market:
        raise ValueError('capture_target_mismatch')
    url = _search_url(raw.get('url'), market, query)
    products = raw.get('products')
    # No verified zero-results DOM is supported yet. Never mistake delayed,
    # sponsored-only or unsupported content for a confirmed empty result set.
    if not isinstance(products, list) or not 1 <= len(products) <= 50 or not all(isinstance(p, dict) for p in products):
        raise ValueError('search_products_required')
    event = ledger._clean({'id': 'capture-search-' + _digest(request['captureId'])[:40], 'kind': 'search', 'parentId': request['parentId'], 'sourceType': 'browser_visible', 'observedAt': raw.get('capturedAt'), 'query': query, 'marketplace': market, 'url': url, 'products': [{'asin': p.get('asin'), 'title': p.get('title')} for p in products]})
    dt = datetime.fromisoformat(event['observedAt'])
    parent_time = datetime.fromisoformat(parent['observedAt'].replace('Z', '+00:00'))
    if dt < started - timedelta(seconds=5) or dt > _now() + timedelta(seconds=5) or dt < parent_time:
        raise ValueError('capture_time_mismatch')
    evidence = raw.get('evidence')
    if not isinstance(evidence, dict) or evidence.get('selector') != SEARCH_SELECTOR or evidence.get('query') != query or evidence.get('marketplace') != market or evidence.get('capturedAt') != raw.get('capturedAt') or _search_url(evidence.get('sourceURL'), market, query) != url:
        raise ValueError('search_evidence_mismatch')
    cards = evidence.get('cards')
    if not isinstance(cards, list) or len(cards) != len(products) or len(products) != len(event['products']):
        raise ValueError('search_card_evidence_required')
    safe_cards = []
    for product, card, clean in zip(products, cards, event['products']):
        if not isinstance(card, dict) or product.get('asin') != clean['asin'] or card.get('asin') != clean['asin'] or card.get('cardAsin') != clean['asin'] or product.get('title') != clean['title'] or card.get('title') != clean['title']:
            raise ValueError('search_card_evidence_mismatch')
        if card.get('cardSelector') != SEARCH_CARD_SELECTOR or card.get('titleSelector') != SEARCH_TITLE_SELECTOR or card.get('directMainSlot') is not True or card.get('visible') is not True or card.get('sponsored') is not False:
            raise ValueError('search_card_structure_mismatch')
        product_url = _search_product_url(product.get('productURL'), market, clean['asin'])
        if _search_product_url(card.get('anchorHref'), market, clean['asin']) != product_url:
            raise ValueError('search_card_link_mismatch')
        safe_cards.append({'asin': clean['asin'], 'cardAsin': clean['asin'], 'title': clean['title'], 'productURL': product_url, 'anchorHref': product_url, 'cardSelector': SEARCH_CARD_SELECTOR, 'titleSelector': SEARCH_TITLE_SELECTOR, 'directMainSlot': True, 'visible': True, 'sponsored': False})
    return event, {'selector': SEARCH_SELECTOR, 'query': query, 'marketplace': market, 'sourceURL': url, 'capturedAt': event['observedAt'], 'cards': safe_cards}


def capture(user, data):
    return _capture(user, data, 'seller')


def capture_store(user, data):
    return _capture(user, data, 'store')


def capture_search(user, data):
    return _capture(user, data, 'search')


def capture_family(user, data):
    return _capture(user, data, 'family')


def capture_live(user, data, kind, lease):
    """Internal coordinator entry point. No HTTP route accepts a lease or URL."""
    if kind not in ('seller', 'store', 'search', 'family'):
        raise ValueError('invalid_capture_kind')
    import discovery_live
    # The coordinator verifies the opaque, non-JSON capability and exact step.
    discovery_live._check_lease(lease, user, data, kind, phase='before_dispatch')
    return _capture(user, data, kind, lease=lease, navigate=True)


def _capture(user, data, kind, *, lease=None, navigate=False):
    is_store, is_search = kind == 'store', kind == 'search'
    reader, parent_reader, script_builder, validator, collector = {
        'seller': (_request, _parent, _script, _validate, COLLECTOR),
        'store': (_store_request, _store_parent, _store_script, _validate_store, STORE_COLLECTOR),
        'search': (_search_request, _search_parent, _search_script, _validate_search, SEARCH_COLLECTOR),
        'family': (_request, _family_parent, _family_script, _validate_family, FAMILY_COLLECTOR),
    }[kind]
    request = reader(user, data)
    # Preserve existing seller and store digest formats exactly.
    digest = _digest(data) if kind == 'seller' else _digest({'captureKind': kind, 'request': data})
    if navigate:
        digest = _digest({'captureMode': 'navigate_read', 'captureKind': kind, 'request': data})
    receipt = {**request, 'captureKind': kind, 'schemaVersion': 1, 'status': 'capture_started', 'reason': 'read_only_capture_intent', 'trust': ledger.TRUST, 'qualificationEvaluated': False, 'qualifiedCount': 0, 'executionAllowed': False, 'publicationAllowed': False, 'paidExternalCallsPerformed': 0, 'startedAt': _now().isoformat()}
    if navigate:
        receipt.update(captureMode='navigate_read', reason='read_only_navigation_capture_intent')
    c = _connect(True)
    try:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT digest,receipt FROM attempts WHERE owner=? AND id=?', (user, request['captureId'])).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError('Conflicting captureId reuse')
            c.rollback()
            return _read_receipt(old[1])
        if _history_blocks(c, user):
            receipt.update(status='blocked', reason='prior_capture_unresolved')
        else:
            try:
                parent = parent_reader(user, request)
            except (ValueError, KeyError, TypeError):
                receipt.update(status='blocked', reason='parent_or_revision_invalid')
            if receipt['status'] == 'capture_started':
                try:
                    binding, fingerprint = _binding(user)
                    receipt.update(marketplace=parent['marketplace'], sourceURL=_store_url('https://' + next(h for h, m in ledger.MARKETS.items() if m == parent['marketplace']) + '/s?me=' + request['sellerId'], parent['marketplace'], request['sellerId']) if is_store else _search_url('https://' + next(h for h, m in ledger.MARKETS.items() if m == parent['marketplace']) + '/s?' + urlencode({'k': parent['phrase']}), parent['marketplace'], parent['phrase']) if is_search else parent['url'], bindingDigest=_digest(binding), collectorSha256=hashlib.sha256(collector.read_bytes()).hexdigest(), target={'taskSpaceId': binding['spaceId'], 'taskSpaceName': binding['name'], 'pageLabel': binding['label']})
                    if kind == 'family' or navigate:
                        receipt['sourceURL'] = _destination(kind, request, parent)
                except (ValueError, KeyError, TypeError, OSError):
                    receipt.update(status='configuration_blocked', reason='explicit_discovery_binding_required')
                if receipt['status'] == 'capture_started':
                    if _space_history_blocks(c, binding):
                        receipt.update(status='blocked', reason='prior_space_capture_unresolved')
                    else:
                        import discovery_live
                        try:
                            discovery_live._browser_guard(c, user, binding, lease=lease)
                        except ValueError as exc:
                            reason = str(exc)
                            receipt.update(status='blocked', reason=reason if re.fullmatch('[a-z_]{1,100}', reason) else 'live_run_guard_blocked')
        c.execute('INSERT INTO attempts VALUES (?,?,?,?,?)', (user, request['captureId'], digest, receipt['status'], json.dumps(receipt)))
        c.commit()
    finally:
        c.close()
    if receipt['status'] != 'capture_started':
        return receipt
    dispatched = False
    try:
        directory = DB.parent / 'discovery-captures' / str(user) / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        result_path = directory / 'result.json'
        script_path = directory / 'capture.mjs'
        script_request = {**request, 'query': parent['phrase']} if is_search else request
        navigation = None
        if navigate:
            import discovery_live
            check = discovery_live._check_lease(lease, user, request, kind, phase='before_dispatch')
            navigation = {'destination': _destination(kind, request, parent),
                'cancelPath': str(Path(check['cancelPath']).resolve()), 'deadline': check['deadline']}
        script = script_builder(binding, script_request, parent['marketplace'], result_path, navigation=navigation) if navigate else script_builder(binding, script_request, parent['marketplace'], result_path)
        script_path.write_text(script, encoding='utf-8')
        # Validate current scope again immediately before dispatch.
        if _binding(user) != (binding, fingerprint) or parent_reader(user, request) != parent or hashlib.sha256(collector.read_bytes()).hexdigest() != receipt['collectorSha256']:
            receipt.update(status='blocked', reason='scope_changed_before_capture')
        else:
            timeout = TIMEOUT
            with runtime_environment.ego_space(binding['spaceId']):
                if navigate:
                    check = discovery_live._check_lease(lease, user, request, kind, phase='before_dispatch')
                    timeout = min(TIMEOUT, max(0.1, check['deadline'] - time.time()))
                    discovery_live._dispatch_started(lease, user, request, kind)
                dispatched = True
                completed = subprocess.run([binding['cli'], 'nodejs', '-e', 'await import(' + json.dumps(script_path.resolve().as_uri()) + ')'], cwd=str(directory), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            receipt['runtimeExitCode'] = completed.returncode
            receipt.update(status='execution_unknown', reason='runtime_outcome_unknown')
            if completed.returncode == 0 and result_path.is_file() and result_path.stat().st_size <= (524288 if is_store or is_search or kind == 'family' else 32768):
                raw = json.loads(result_path.read_text(encoding='utf-8'))
                if navigate and isinstance(raw, dict):
                    diagnostic = _navigation_diagnostic(raw)
                    if diagnostic is not None:
                        receipt['navigationDiagnostics'] = diagnostic
                    for key in ('navigationDispatched', 'collectorDispatched'):
                        if type(raw.get(key)) is bool:
                            receipt[key] = raw[key]
                    if raw.get('navigationPhase') in ('before_navigation', 'navigation_dispatched', 'after_navigation', 'collector_dispatched', 'finished'):
                        receipt['navigationPhase'] = raw['navigationPhase']
                if isinstance(raw, dict) and type(raw.get('schemaVersion')) is int and raw.get('schemaVersion') == 1 and raw.get('status') == 'blocked':
                    reason = raw.get('reason')
                    receipt.update(status='blocked', reason=reason if isinstance(reason, str) and reason in BLOCK_REASONS else 'browser_capture_blocked')
                elif isinstance(raw, dict) and raw.get('status') == 'captured':
                    try:
                        navigation_evidence = None
                        if navigate:
                            discovery_live._check_lease(lease, user, request, kind, phase='after_capture')
                            navigation_evidence = _validate_navigation(raw, binding, kind, request, parent)
                        event, evidence = validator(raw, request, parent, datetime.fromisoformat(receipt['startedAt']))
                        if _binding(user) != (binding, fingerprint) or parent_reader(user, request) != parent or hashlib.sha256(collector.read_bytes()).hexdigest() != receipt['collectorSha256']:
                            raise ValueError('scope_changed')
                        receipt.update(status='captured', reason='unverified_' + kind + '_observation', evidence=evidence)
                        if navigation_evidence is not None:
                            receipt['navigationEvidence'] = navigation_evidence
                        if kind == 'family':
                            receipt['familyEvidence'] = evidence
                        else:
                            receipt['preparedBatch'] = {'cycleId': request['cycleId'], 'expectedRevision': request['expectedRevision'], 'events': [event]}
                    except (ValueError, TypeError, KeyError, OSError):
                        receipt.update(status='blocked', reason='capture_evidence_or_scope_invalid')
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        receipt.update(status='execution_unknown' if dispatched else 'blocked', reason='runtime_outcome_unknown' if dispatched else 'capture_preparation_failed')
    receipt['phase'] = 'runtime_dispatched' if dispatched else 'pre_dispatch'
    receipt['finishedAt'] = _now().isoformat()
    c = _connect(True)
    try:
        c.execute('UPDATE attempts SET status=?,receipt=? WHERE owner=? AND id=?', (receipt['status'], json.dumps(receipt), user, request['captureId']))
        c.commit()
    finally:
        c.close()
    return receipt
