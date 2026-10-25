import importlib.util
from pathlib import Path
import pytest
import os

from tracefix.knowledge.context import build_context
from tracefix.knowledge.retrieval import rrf
from tracefix.runtime.contracts import RunState


def test_rrf_deduplicates_rankings_and_breaks_ties_by_id():
    assert rrf(['a','b','b'],['b','c'])[0]=='b'
    assert rrf(['b'],['a'])==['a','b']


def test_context_deduplicates_pairs_without_losing_protected_spec():
    s=RunState(scope_id='b',goal='test goal',url='http://app:3000')
    s.observation_ref='current-observation.json'
    pair={'action':'click','result':'done'}
    ctx=build_context(s,{'assertions':['frozen']},observation={'id':'obs_current'},pairs=[pair,pair])
    assert ctx['test_spec']['assertions']==['frozen']
    assert ctx['recent_action_results']==[pair]
    assert ctx['available_evidence_refs']==['current-observation.json']


def test_reward_group_requires_reset_fingerprint_and_variance():
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('reward',root/'training/reward.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    r={'state_fingerprint':'same','oracle_success':False,'invalid_action':False,'potential_before':0,'potential_after':0,'action_cost':1}
    assert module.group_rewards([r]*4)[1]=='zero_variance'
    rows=[r,r,r,{**r,'oracle_success':True,'potential_after':1}]
    assert module.group_rewards(rows)[0][-1]>0.9
    with pytest.raises(ValueError):module.group_rewards([r,r,r,{**r,'state_fingerprint':'different'}])


def test_all_twelve_mutations_match_exact_baseline_once():
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('cases',root/'evals/cases.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert len(module.CASES)==12
    helper_spec=importlib.util.spec_from_file_location('target_preparation',root/'bugboard/scripts/prepare_target.py')
    helper=importlib.util.module_from_spec(helper_spec);helper_spec.loader.exec_module(helper)
    source=Path(os.getenv('TRACEFIX_TEST_REPOSITORY_CACHE') or helper.target_cache(root))
    assert source.is_dir(), '请先执行 python bugboard/scripts/prepare_target.py --verify-cases 准备真实固定版本基线'
    helper.validate_target(source,*helper.repository_settings())
    helper.verify_cases(source,root)
