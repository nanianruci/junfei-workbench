"""Scoped command-line client for the workbench's Codex contract."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def call(action, proposal=None, *, workbench_url=None):
    token = os.environ.get('JUNFEI_CODEX_TOKEN')
    if not token:
        raise ValueError('请先从商品页取得配对码并设置 JUNFEI_CODEX_TOKEN')
    default = 'http://127.0.0.1:' + os.environ.get('JUNFEI_PORT', '18769')
    base = urlsplit(workbench_url if workbench_url is not None else
                    os.environ.get('JUNFEI_CODEX_URL', default))
    local = base.hostname in ('localhost', '127.0.0.1', '::1')
    if (not base.hostname or base.username or base.password or base.path not in ('', '/')
            or base.query or base.fragment or (base.scheme != 'https' and not (local and base.scheme == 'http'))):
        raise ValueError('工作台地址须使用 HTTPS 或本机 HTTP，且仅包含协议、主机和端口')
    body = None if proposal is None else json.dumps(proposal, ensure_ascii=False).encode('utf-8')
    url = f'{base.scheme}://{base.netloc}/api/codex/{action}'
    request = Request(url, data=body, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error).get('error')
        except (ValueError, AttributeError):
            detail = None
        raise ValueError(f'工作台返回 {error.code}。{detail or "请核对工作台地址和配对码"}') from None
    except (TimeoutError, URLError):
        raise ValueError('工作台未返回响应。先读取状态并核对原请求编号。') from None
    if action == 'status' and result.get('version') != 1:
        raise ValueError('工作台接口版本不兼容')
    return result


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    subcommands = cli.add_subparsers(dest='command', required=True)
    subcommands.add_parser('status')
    receipt = subcommands.add_parser('receipt', help='读取指定请求的审批和任务交接记录')
    receipt.add_argument('request_id')
    heartbeat = subcommands.add_parser('heartbeat', help='为当前会话所领取的原派单续租')
    heartbeat.add_argument('request_id')
    inbox = subcommands.add_parser('next', help='接收后端为本款商品派发的任务')
    inbox.add_argument('--wait', type=int, default=0, metavar='SECONDS')
    review = subcommands.add_parser('review', help='交回指定派单的核对结果')
    review.add_argument('result', type=Path)
    args = cli.parse_args(argv)
    try:
        if args.command == 'receipt':
            result = call('receipt?' + urlencode({'requestId': args.request_id}))
        elif args.command == 'heartbeat':
            result = call('heartbeat', {'requestId': args.request_id})
        elif args.command == 'review':
            result = call('review', json.loads(args.result.read_text(encoding='utf-8-sig')))
        elif args.command == 'next':
            if not 0 <= args.wait <= 60:
                raise ValueError('等待时间须为 0 至 60 秒')
            deadline = time.monotonic() + args.wait
            result = call('next')
            while result.get('assignment') is None and time.monotonic() < deadline:
                time.sleep(min(2, max(0, deadline - time.monotonic())))
                result = call('next')
        else:
            result = call('status')
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
