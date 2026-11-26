"""Explicit account-owned handoff from a verified selection task to copy only."""
import json
import math
import re
import time
from contextlib import closing

import agent_center as agent
import agent_selection
from agent_research import snapshot

SCOPE = 'selection_to_copy'



def assert_candidate_identity(task, fresh):
    product = fresh.get('product')
    if not isinstance(product, dict):
        raise ValueError('当前账号找不到原候选产品')
    market = task.get('profile', {}).get('marketplace')
    reference = (product.get('selectionResearch') or {}).get('reference') or {}
    intake = task.get('intake') or {}
    if (intake.get('source') != 'selection-report' or not intake.get('asin')
            or product.get('id') != task.get('productId')
            or reference.get('asin') != intake['asin']
            or reference.get('market') != market
            or (product.get('market') or market) != market
            or task.get('profile', {}).get('mode') != 'draft'):
        raise ValueError('原候选产品身份、选品来源或站点已变化，不能接续')
    handoff = task.get('copyHandoff')
    if isinstance(handoff, dict):
        expected = {'productId': task['productId'], 'marketplace': market,
                    'profileId': task['profile']['id'], 'store': task['profile']['store']}
        if any(handoff.get(key) != value for key, value in expected.items()):
            raise ValueError('文案接续授权的产品或店铺身份不一致，须核对原记录')


def inspect_before_copy(task):
    """Recheck saved identity and live local facts before any scoped paid step."""
    try:
        if (task.get('executionScope') != SCOPE
                or (task.get('copyHandoff') or {}).get('scope') != SCOPE):
            raise ValueError('缺少本任务文案接续的明确范围记录')
        assert_candidate_identity(task, task['snapshot'])
        if snapshot(agent.load_workspace(), task) != task['snapshot']:
            raise ValueError('产品资料或文案在检查后已变化，请更新资料重检；未调用研究或模型')
        facts = agent.inspect_facts(task)
        selection = facts.get('selection') or {}
        if facts.get('ok') is not True or selection.get('ok') is not True or selection.get('skipped'):
            return {'ok': False, 'issues': ['当前选品与货源检查未明确通过，未调用研究或模型'] + facts.get('issues', []),
                    'selection': selection}
        return {'ok': True}
    except ValueError as error:
        return {'ok': False, 'issues': [str(error)]}


def copy_parameters(data):
    budget = data.get('budgetCny')
    if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
        raise ValueError('请填写本任务的正数人民币预算')
    raw = data.get('researchAsins', '')
    if not isinstance(raw, str):
        raise ValueError('竞品ASIN格式无效')
    asins = list(dict.fromkeys(value.upper() for value in re.split(r'[\s,，;；]+', raw.strip()) if value))
    if len(asins) > 5 or any(not re.fullmatch('[A-Z0-9]{10}', value) for value in asins):
        raise ValueError('竞品ASIN须为最多5个有效编号，或留空使用已有研究或前台观察候选')
    return budget, asins


def prepare(user, tid, data, *, handoff=None):
    if not isinstance(data, dict):
        raise ValueError('接续设置格式无效')
    if data.get('confirmPaidText') is not True or data.get('acknowledgeBudgetNotHardCap') is not True:
        raise ValueError('请明确同意研究与文案可能消耗费用，并确认预算记录不是账单硬上限')
    budget, asins = copy_parameters(data)
    with agent.LOCK, closing(agent.connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        task = agent.owned(c, user, tid)
        prior = task.get('copyHandoff')
        if 'copyHandoff' in task:
            if not isinstance(prior, dict):
                raise ValueError('文案接续记录不完整，须先核对；未重新配置')
            # Retried authorization must never restart a task or clear attempts.
            if (prior.get('budgetCny') != budget or prior.get('researchAsins') != asins
                    or task.get('executionScope') != SCOPE):
                raise ValueError('本任务已经设置文案接续；不能通过重复请求更换预算、竞品或执行范围')
            if handoff and prior.get('codexHandoff')!=list(handoff):
                raise ValueError('该接续已有另一份授权，请核对原任务')
            return agent.public(task)
        if (task.get('taskMode') != 'production'
                or (task.get('intake') or {}).get('source') != 'selection-report'
                or task.get('status') != 'blocked' or tid in agent.ACTIVE):
            raise ValueError('仅已停止的选品入队任务可在原任务接续研究与文案')
        if task.get('profile', {}).get('mode') != 'draft':
            raise ValueError('文案接续仅用于草稿店铺档案，不接通发布')
        if (task.get('exportAvailable') or task.get('junfeiPlanAvailable')
                or any(key in task.get('results', {}) for key in ('export', 'junfeiPreflight', 'publish', 'verify'))
                or 'erpObservation' in task):
            raise ValueError('已有交付或ERP记录，须先核对原流程；不能改为文案接续')
        if 'researchAttempt' in task or 'listingAttempt' in task:
            raise ValueError('已有研究或文案调用记录，请先使用原报告或草稿；不重置付费尝试')
        for key in ('planningAttempt', 'sourcingAttempt'):
            attempt = task.get(key)
            if key in task and (not isinstance(attempt, dict) or attempt.get('state') != 'saved'):
                raise ValueError('已有未确认的工具调用，请先核对原记录，不允许接续')
        if task.get('aiPlanning') and not task.get('results', {}).get('plan', {}).get('ok'):
            raise ValueError('原AI规划没有确认完成，不能跳过未知调用')
        revision = agent.workspace_revision()
        expected = data.get('workspaceRevision')
        if revision is not None and (type(expected) is not int or expected != revision):
            raise ValueError('工作台资料版本已变化，请刷新后重新核验接续')
        workspace = agent.load_workspace()
        fresh = snapshot(workspace, task)
        assert_candidate_identity(task, fresh)
        probe = dict(task, snapshot=fresh, selectionPolicy=agent_selection.current_policy())
        facts = agent.inspect_facts(probe)
        selection = facts.get('selection') or {}
        if facts.get('ok') is not True or selection.get('ok') is not True or selection.get('skipped'):
            issues = facts.get('issues') or ['选品准入证据没有明确通过']
            raise ValueError('选品与货源资料尚未通过：' + '；'.join(str(item) for item in issues)[:1000])
        if agent.workspace_revision() != revision:
            raise ValueError('核验期间工作台资料已变化，请刷新后重试')
        now = time.time()
        task.setdefault('revisions', []).append({
            'at': now, 'reason': '明确授权原选品任务接续至文案，保留全部原证据',
            'snapshot': task['snapshot'], 'results': task['results'],
            'selectionPolicy': task.get('selectionPolicy'),
            'autoResearch': task.get('autoResearch'), 'autoListing': task.get('autoListing'),
            'autoSourcing': task.get('autoSourcing'), 'budgetCny': task.get('budgetCny'),
        })
        task.update(snapshot=fresh, selectionPolicy=probe['selectionPolicy'],
                    autoResearch=True, autoListing=True, autoSourcing=False,
                    researchAsins=asins, budgetCny=budget, executionScope=SCOPE,
                    cursor=1, status='queued', pauseRequested=False,
                    reason='研究与文案接续已排队；点击开始后执行，止于文案，不作图或操作ERP',
                    updated=now,
                    results={key: value for key, value in task['results'].items() if key == 'plan'},
                    copyHandoff={'at': now, 'scope': SCOPE, 'budgetCny': budget,
                                 'productId': task['productId'], 'marketplace': task['profile']['marketplace'],
                                 'profileId': task['profile']['id'], 'store': task['profile']['store'],
                                 'researchAsins': asins, 'confirmedPaidText': True,
                                 'budgetEnforcement': 'record_only_not_hard_cap',
                                 'selectionAndFactsChecked': True})
        if revision is not None:
            task['workspaceRevision'] = revision
        if handoff:
            import codex_handoff
            task['copyHandoff']['codexHandoff']=list(handoff)
            codex_handoff.record(c,handoff,tid,'prepared')
        agent.put(c, task)
        agent.event(c, task, 'copy_handoff_prepared',
                    '已按明确选择排队研究与文案；预算不是硬封顶；未调用模型、未启动任务、未授权图片或ERP',
                    scope=SCOPE, budgetCny=budget, researchAsins=asins,
                    budgetEnforcement='record_only_not_hard_cap')
    return agent.public(task)
