"""Open the installed Junfei popup through Ziniao CLI; never copy login tokens."""
import argparse
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import ziniao_market
import ziniao_mfa

PLUGIN_ID = 'acijbilfofphlklghbilbknpdmnholoi'
POPUP = f'chrome-extension://{PLUGIN_ID}/popup.html'
COUNTRIES = {'US': '美国', 'UK': '英国'}
SELLER_URLS = {'US':'https://sellercentral.amazon.com/', 'UK':'https://sellercentral.amazon.co.uk/'}
SELLER_ENTRY_PATHS = {
    'US': 'home',
    'UK': 'amazonsell/business?ref=homepage_direct_serve_ngs',
}


class SiteIdentityError(ValueError):
    """A fixed, safe diagnostic without CLI output or authentication data."""
    MESSAGES = {
        'selected_market_mismatch': '卖家后台实际选中市场与目标站点不符；域名或菜单选项不能代替当前市场身份',
        'selected_seller_mismatch': '卖家后台实际选中店铺与目标店铺不符，未打开插件代替核对',
        'seller_header_ambiguous': '卖家后台存在多个可见店铺市场控件，不能确定当前身份',
        'seller_header_unavailable': '未读取到已适配的实际店铺市场控件；不以正文或菜单中的店铺站点文字代替',
        'seller_header_read_failed': '卖家后台当前店铺市场控件读取失败，未继续插件核对',
        'seller_origin_changed': '卖家后台读取期间域名已变化，未确认目标站点身份',
        'seller_site_unverified': '卖家后台未核对到目标店铺和站点，不打开插件代替登录',
        'seller_mfa_required': '卖家后台要求双重验证，请在当前紫鸟页面完成验证后从原任务继续',
        'market_selection_unconfirmed': '市场切换已尝试，实际选中站点尚未确认；保留当前页面，不重复点击',
        'seller_return_url_unverified': '缺少同店同站已核实的卖家后台实际网址，不猜测返回页面',
        'seller_return_target_changed': '插件核验后页面目标已变化，未在另一标签恢复卖家后台',
        'seller_return_url_changed': '插件核验后未回到先前已核实的卖家后台实际页面',
    }

    def __init__(self, error_type):
        self.error_type = error_type
        super().__init__(self.MESSAGES[error_type])


def native_cli():
    found = shutil.which('ziniao-cli')
    if not found:
        raise ValueError('未安装紫鸟 CLI')
    path = Path(found)
    if path.suffix.lower() == '.exe':
        return path
    binary = path.parent / 'node_modules/@ziniao-open/cli/bin/ziniao-cli.exe'
    if not binary.is_file():
        raise ValueError('未找到紫鸟 CLI 原生入口；不使用拼接 shell 命令传参')
    return binary


def run(cli, *args):
    response = subprocess.run([str(cli), *args], capture_output=True, encoding='utf-8', timeout=35)
    try:
        value = json.loads(response.stdout)
    except (ValueError, TypeError):
        if args[:2] == ('page','visit'):
            # Successful navigation is a human-readable CLI receipt in 1.2.
            # It is not authentication evidence: always re-read the document.
            return {'ok':response.returncode == 0 and '页面已导航' in response.stdout,
                    'format':'cli_navigation_text'}
        raise ValueError('紫鸟 CLI 未返回有效 JSON；操作结果需核对，不自动重放') from None
    return value


def content_payload(value):
    if value.get('ok') is not True:
        raise ValueError('紫鸟页面读取失败')
    while isinstance(value.get('data'), dict) and 'content' not in value:
        value = value['data']
    if not isinstance(value.get('content'), str):
        raise ValueError('紫鸟页面正文格式变化')
    return value


def check_identity(page, market, seller, account):
    if page.get('url') != POPUP:
        return False
    text = ' '.join(page['content'].split())
    identities = re.findall(r'站点\s+(\S+)\s+店铺\s+(.+?)\s+版本号', text)
    if not identities:
        return False
    if len(identities) != 1:
        raise ValueError('插件显示多个店铺身份；未继续 ERP 操作')
    country, actual_seller = identities[0]
    if actual_seller != ' '.join(seller.split()):
        raise ValueError('插件实际店铺与当前任务不符；未继续 ERP 操作')
    if country != COUNTRIES[market]:
        return False
    logged_in = '退出' in text and '请登陆' not in text and '请登录' not in text
    if logged_in and not re.search(r'用户\s+' + re.escape(account) + r'(?=\s|$)', text):
        raise ValueError('插件登录账号与当前 ERP 账号不符')
    return logged_in


def submit_saved_email_form(cli, *, store_id, market):
    """Use the observed form when native coordinate input is unavailable.

    No credential is read into Python, and no password/OTP/passkey is handled.
    Amazon's email page contains both a span and input named `continue`.
    Only the unique real submit input in the saved-email form is submitted.
    A navigation error remains unknown until ensure_site reads the document.
    """
    host=urlsplit(SELLER_URLS[market]).hostname
    script=r'''(() => {try {
      const host=HOST;
      if(location.hostname!==host||location.pathname!=='/ap/signin')return JSON.stringify({submitted:false,reason:'not_email_step'});
      const emails=document.querySelectorAll('input#ap_email[type=email]');
      const submits=document.querySelectorAll('input#continue[type=submit]');
      if(emails.length!==1||submits.length!==1||document.querySelector('input[type=password]'))return JSON.stringify({submitted:false,reason:'form_changed'});
      const email=emails[0],submit=submits[0],form=email.form;
      if(!form||submit.form!==form||!email.getBoundingClientRect().width||!submit.getBoundingClientRect().width||!email.value||!email.checkValidity()||!form.checkValidity())return JSON.stringify({submitted:false,reason:'saved_email_unavailable'});
      const destination=new URL(form.action,location.href);
      if(destination.protocol!=='https:'||destination.hostname!==host||destination.pathname!=='/ap/signin')return JSON.stringify({submitted:false,reason:'destination_changed'});
      HTMLFormElement.prototype.requestSubmit.call(form,submit);
      return JSON.stringify({submitted:true,method:'observed_form_requestSubmit',credentialsRead:false});
    }catch(e){return JSON.stringify({submitted:false,reason:'form_submission_failed'});}})()'''.replace('HOST',json.dumps(host))
    try:
        value=run(cli,'page','exec','--store-id',store_id,'--script',script)
    except (ValueError,subprocess.TimeoutExpired):
        return {'submitted':None,'reason':'navigation_result_unknown','credentialsRead':False}
    while isinstance(value.get('data'),dict):value=value['data']
    try:
        result=json.loads(value.get('result',''))
    except (ValueError,TypeError):
        return {'submitted':None,'reason':'navigation_result_unknown','credentialsRead':False}
    return {key:result[key] for key in ('submitted','method','reason','credentialsRead') if key in result}


def selected_seller_header(cli, *, store_id):
    """Read only the observed selected header, never menu/body alternatives.

    The new Seller Central selector was observed on the real seller page.
    No older selector has been independently observed here: an absent header
    therefore remains unsupported, rather than falling back to body text.
    """
    script=r'''(() => {
      const visible=e=>{const rect=e.getBoundingClientRect(),style=getComputedStyle(e);return rect.width>0&&rect.height>0&&style.display!=='none'&&style.visibility!=='hidden'&&!e.closest('[aria-hidden="true"]');};
      const headers=[...document.querySelectorAll('.dropdown-account-switcher-header')].filter(visible);
      return JSON.stringify({origin:location.origin,url:location.href,visibleHeaderCount:headers.length,
        labels:headers.map(e=>(e.innerText||e.textContent||'').trim().replace(/\s+/g,' ').slice(0,200))});
    })()'''
    value=run(cli,'page','exec','--store-id',store_id,'--script',script)
    if value.get('ok') is not True:
        raise SiteIdentityError('seller_header_read_failed')
    while isinstance(value.get('data'),dict):value=value['data']
    try:
        if value.get('exceptionDetails'):
            raise ValueError('execution failed')
        result=json.loads(value.get('result',''))
        count,labels=result['visibleHeaderCount'],result['labels']
        if (not isinstance(result.get('origin'),str) or ('url' in result and not isinstance(result['url'],str)) or isinstance(count,bool) or not isinstance(count,int)
                or count<0 or not isinstance(labels,list) or len(labels)!=count
                or any(not isinstance(label,str) for label in labels)):
            raise ValueError('invalid selected header result')
    except (ValueError,TypeError,KeyError):
        raise SiteIdentityError('seller_header_read_failed') from None
    return {'origin':result['origin'],**({'url':result['url']} if 'url' in result else {}),'visibleHeaderCount':count,
            'labels':[' '.join(label.split()) for label in labels]}


def page_origin(page):
    parsed=urlsplit(page.get('url',''))
    return f'{parsed.scheme}://{parsed.netloc}'


def validated_seller_url(value, market):
    try:
        if not isinstance(value,str) or not value or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError()
        parsed=urlsplit(value)
        if (parsed.scheme!='https' or parsed.hostname!=urlsplit(SELLER_URLS[market]).hostname
                or parsed.username is not None or parsed.password is not None or parsed.port not in (None,443)
                or parsed.path.startswith('/ap/')):raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise SiteIdentityError('seller_return_url_unverified') from None
    return value


def verified_site_target(site, *, store_id, market, seller):
    """Only an actual successful pre-plugin header observation is a return target."""
    if not isinstance(site,dict):raise SiteIdentityError('seller_return_url_unverified')
    selection=site.get('sellercentralSelection') or {}
    url=validated_seller_url(site.get('sellercentralUrl'),market)
    target=site.get('sellercentralTargetId')
    if (site.get('sellercentralConfirmed') is not True or site.get('storeId')!=store_id
            or selection.get('method')!='actual_unique_visible_header'
            or selection.get('label')!=' '.join((seller+' '+COUNTRIES[market]).split())
            or selection.get('origin')!=SELLER_URLS[market].rstrip('/') or selection.get('url')!=url
            or not isinstance(target,str) or not target.strip()):
        raise SiteIdentityError('seller_return_url_unverified')
    return url,target


def ensure_site(cli, *, store_id, store_name, market, seller, timeout=60, allow_market_dom_click=False, verified_site=None):
    if not re.fullmatch(r'\d+',store_id) or market not in COUNTRIES:
        raise ValueError('必须指定真实紫鸟店铺 ID 和 US/UK 站点')
    restore=verified_site_target(verified_site,store_id=store_id,market=market,seller=seller) if verified_site is not None else None
    resolved=run(cli,'store','resolve','--id',store_id,'--expected-name',store_name)
    match=resolved.get('data',{})
    if resolved.get('ok') is not True or match.get('matched') is not True or str(match.get('storeId'))!=store_id or match.get('name')!=store_name:
        raise ValueError('切站前店铺身份核对失败')
    page=content_payload(run(cli,'page','content','--store-id',store_id,'--content-format','text'))
    if restore and page.get('targetId')!=restore[1]:raise SiteIdentityError('seller_return_target_changed')
    expected_origin=SELLER_URLS[market].rstrip('/')
    expected_label=' '.join((seller+' '+COUNTRIES[market]).split())
    if page_origin(page)!=expected_origin or (restore and page.get('url')!=restore[0]):
        entry_url=restore[0] if restore else SELLER_URLS[market]+SELLER_ENTRY_PATHS[market]
        run(cli,'page','visit','--store-id',store_id,'--url',entry_url,
            '--wait-until','domcontentloaded','--timeout','30000')
        # Navigation acknowledgment is not a page identity observation.
        page=content_payload(run(cli,'page','content','--store-id',store_id,'--content-format','text'))
        if restore and page.get('targetId')!=restore[1]:raise SiteIdentityError('seller_return_target_changed')
    deadline=time.monotonic()+timeout
    recovery=None
    market_selection=None
    mfa_recovery=None
    missing_header=False
    while True:
        if restore and page.get('targetId')!=restore[1]:raise SiteIdentityError('seller_return_target_changed')
        parsed=urlsplit(page.get('url',''))
        if recovery is None and parsed.scheme=='https' and parsed.hostname==urlsplit(SELLER_URLS[market]).hostname and parsed.path=='/ap/signin':
            recovery=submit_saved_email_form(cli,store_id=store_id,market=market)
            page=content_payload(run(cli,'page','content','--store-id',store_id,'--content-format','text'))
            parsed=urlsplit(page.get('url',''))
        if page_origin(page)==expected_origin and parsed.path=='/ap/mfa':
            if mfa_recovery is not None:
                raise SiteIdentityError('seller_mfa_required')
            mfa_recovery=ziniao_mfa.submit_autofilled(run,cli,store_id=store_id,
                origin=expected_origin,deadline=min(deadline,time.monotonic()+12))
            if mfa_recovery['status']!='left_mfa':
                raise SiteIdentityError('seller_mfa_required')
            page=content_payload(run(cli,'page','content','--store-id',store_id,'--content-format','text'))
            continue
        if page_origin(page)==expected_origin and parsed.path!='/ap/signin':
            header=selected_seller_header(cli,store_id=store_id)
            if header['origin']!=expected_origin:
                raise SiteIdentityError('seller_origin_changed')
            count=header['visibleHeaderCount']
            if count>1:
                raise SiteIdentityError('seller_header_ambiguous')
            missing_header=count==0
            if count==1:
                actual_label=header['labels'][0]
                if actual_label!=expected_label:
                    kind='selected_market_mismatch' if actual_label.startswith(' '.join(seller.split())+' ') else 'selected_seller_mismatch'
                    if kind=='selected_seller_mismatch':
                        raise SiteIdentityError(kind)
                    if market_selection is None:
                        if time.monotonic()>=deadline:
                            raise SiteIdentityError(kind)
                        market_selection=ziniao_market.select_observed_market(run,cli,store_id=store_id,
                            market=market,seller=' '.join(seller.split()),deadline=deadline,
                            allow_dom_click=allow_market_dom_click)
                        page=content_payload(run(cli,'page','content','--store-id',store_id,'--content-format','text'))
                        continue
                else:
                    actual_url=validated_seller_url(header.get('url',page.get('url')),market)
                    if restore and actual_url!=restore[0]:raise SiteIdentityError('seller_return_url_changed')
                    result={'sellercentralConfirmed':True,'sellercentralOrigin':SELLER_URLS[market],
                            'sellercentralSelection':{'method':'actual_unique_visible_header','label':actual_label,'origin':header['origin'],'url':actual_url},
                            'sellercentralUrl':actual_url,'sellercentralTargetId':page.get('targetId'),'storeId':store_id}
                    if recovery is not None:result['loginRecovery']=recovery
                    if mfa_recovery is not None:result['mfaRecovery']=mfa_recovery
                    if market_selection is not None:result['marketSelection']={**market_selection,'confirmed':True}
                    return result
        if time.monotonic()>=deadline:
            raise SiteIdentityError('market_selection_unconfirmed' if market_selection is not None else
                                    'seller_header_unavailable' if missing_header else 'seller_site_unverified')
        time.sleep(2)
        page=content_payload(run(cli,'page','content','--store-id',store_id,'--content-format','text'))


def open_and_verify(cli, *, store_id, store_name, market, seller, account, timeout=60):
    if not re.fullmatch(r'\d+', store_id) or market not in COUNTRIES:
        raise ValueError('必须指定真实紫鸟店铺 ID 和 US/UK 站点')
    resolved = run(cli, 'store', 'resolve', '--id', store_id, '--expected-name', store_name)
    match = resolved.get('data', {})
    if resolved.get('ok') is not True or match.get('matched') is not True or str(match.get('storeId')) != store_id or match.get('name') != store_name:
        raise ValueError('紫鸟店铺身份核对失败')
    page = content_payload(run(cli, 'page', 'content', '--store-id', store_id, '--content-format', 'text'))
    navigation = None
    if page.get('url') != POPUP:
        # Open the action.default_popup from the installed extension manifest.
        # CLI 1.2 may report TARGET_NOT_FOUND after the document has navigated;
        # inspect the resulting document instead of repeating the navigation.
        navigation = run(cli, 'page', 'visit', '--store-id', store_id,
                         '--url', POPUP, '--wait-until', 'domcontentloaded', '--timeout', '20000')
    deadline = time.monotonic() + timeout
    while True:
        page = content_payload(run(cli, 'page', 'content', '--store-id', store_id, '--content-format', 'text'))
        if check_identity(page, market, seller, account):
            return {'storeId':store_id, 'storeName':store_name, 'marketplace':market,
                    'sellerName':seller, 'erpAccount':account, 'pluginUrl':POPUP,
                    'targetId':page.get('targetId'), 'loginVerified':True,
                    'method':'ziniao_cli_installed_action_popup',
                    'navigationAcknowledged':None if navigation is None else navigation.get('ok') is True,
                    'checkedAt':datetime.now(timezone.utc).isoformat(),
                    'credentialsRead':False, 'cookiesCopied':False, 'published':False}
        if time.monotonic() >= deadline:
            raise ValueError('插件已打开，但未通过站点、店铺及登录核对；未继续 ERP 操作')
        time.sleep(2)


def failure_diagnostic(error):
    if isinstance(error,SiteIdentityError):
        return error.error_type,str(error)
    if isinstance(error,ziniao_market.MarketSelectionError):
        return error.error_type,str(error)
    safe_values={
        '未安装紫鸟 CLI':'cli_unavailable',
        '未找到紫鸟 CLI 原生入口；不使用拼接 shell 命令传参':'cli_unavailable',
        '紫鸟 CLI 未返回有效 JSON；操作结果需核对，不自动重放':'cli_result_unconfirmed',
        '紫鸟页面读取失败':'page_read_failed',
        '紫鸟页面正文格式变化':'page_format_changed',
        '必须指定真实紫鸟店铺 ID 和 US/UK 站点':'invalid_store_or_market',
        '切站前店铺身份核对失败':'store_identity_mismatch',
        '紫鸟店铺身份核对失败':'store_identity_mismatch',
        '插件实际店铺与当前任务不符；未继续 ERP 操作':'plugin_seller_mismatch',
        '插件登录账号与当前 ERP 账号不符':'plugin_account_mismatch',
        '插件已打开，但未通过站点、店铺及登录核对；未继续 ERP 操作':'plugin_identity_unverified',
        'coordinate input geometry is unavailable':'native_input_unavailable',
    }
    if isinstance(error,subprocess.TimeoutExpired):
        return 'cli_timeout','紫鸟 CLI 执行超时，保留当前页面与原记录，需核对后继续'
    message=str(error)
    if message in safe_values:
        return safe_values[message],message
    # Unknown exception text can contain commands or raw provider details.
    # Never put that text, stdout, stderr, tokens or cookie data in a receipt.
    return 'unexpected_runtime_error','店铺或插件核对未完成；原操作结果需检查，未继续发布'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('store-id','store-name','market','seller','account','receipt'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--ensure-site',action='store_true',help='先核对/切换当前卖家后台，再打开插件')
    parser.add_argument('--allow-market-dom-click',action='store_true',help='已明确授权时，允许同店铺市场菜单在可信输入故障后使用受控DOM点击')
    args=parser.parse_args(argv)
    stage='native_cli'
    popup_observation=None
    identity_order=[]
    deadline=time.monotonic()+120
    try:
        cli=native_cli()
        site={}
        if args.ensure_site:
            stage='sellercentral_identity'
            site=ensure_site(cli,store_id=args.store_id,store_name=args.store_name,market=args.market,seller=args.seller,
                             **({'allow_market_dom_click':True} if args.allow_market_dom_click else {}))
            verified_site_target(site,store_id=args.store_id,market=args.market,seller=args.seller)
            identity_order.append('sellercentral_before_plugin')
        stage='plugin_identity'
        result=open_and_verify(cli,store_id=args.store_id,store_name=args.store_name,
                               market=args.market,seller=args.seller,account=args.account)
        popup_observation={key:result[key] for key in ('pluginUrl','targetId','marketplace','sellerName','erpAccount',
            'loginVerified','checkedAt','credentialsRead','cookiesCopied') if key in result}
        identity_order.append('plugin_popup')
        result.update(site)
        if args.ensure_site:
            stage='sellercentral_retained'
            retained=ensure_site(cli,store_id=args.store_id,store_name=args.store_name,market=args.market,seller=args.seller,
                timeout=min(30,max(0,deadline-time.monotonic())),verified_site=site,
                **({'allow_market_dom_click':True} if args.allow_market_dom_click else {}))
            identity_order.append('sellercentral_after_plugin')
            result.update(sellercentralRetained=True,afterPluginSelection=retained['sellercentralSelection'],
                          sellercentralRetainedUrl=retained['sellercentralUrl'],sellercentralRetainedTargetId=retained['sellercentralTargetId'],
                          sellercentralRetainedAt=datetime.now(timezone.utc).isoformat(),
                          pluginObservation=popup_observation,identityCheckOrder=identity_order)
        code=0
    except Exception as error:
        error_type,message=failure_diagnostic(error)
        result={'loginVerified':False,'stage':stage,'errorType':error_type,'message':message,
                'marketplace':args.market,'storeId':args.store_id,'storeName':args.store_name,
                'checkedAt':datetime.now(timezone.utc).isoformat(),
                'credentialsRead':False,'cookiesCopied':False,'published':False}
        if args.ensure_site:
            result.update(sellercentralRetained=False,identityCheckOrder=identity_order)
        if popup_observation is not None:
            result['pluginObservation']=popup_observation
            if 'pluginUrl' in popup_observation:result['pluginUrl']=popup_observation['pluginUrl']
        code=1
    target=Path(args.receipt).resolve();target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return code


if __name__=='__main__':
    raise SystemExit(main())
