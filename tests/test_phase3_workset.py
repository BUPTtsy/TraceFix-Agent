import json

import pytest

from tracefix.knowledge.assembler import ContextAssembler, TokenCounter, compact_steps
from tracefix.knowledge.workset import expand_reference, prepare_workset, select_snapshot
from tracefix.runtime.contracts import digest
from tracefix.runtime.engine import public_context_manifest
from tracefix.storage.artifacts import Artifacts


def counter():
    return TokenCounter(lambda text: len(text), name='test_characters')


def observation():
    lines = [f'- generic "navigation item {index}"' for index in range(1500)]
    lines[749:753] = ['- main "Task board"', '  - group "Order 527"',
                      '    - alert "Order 527 refresh contradicted stored done=false"',
                      '    - checkbox "Cancel completion" [checked]']
    return {'snapshot': '\n'.join(lines), 'scope_id': 'scope', 'source_manifest': 'source',
            'patch_hash': 'patch', 'page_generation': 3, 'artifact_ref': 'observation.json'}


def test_middle_counterevidence_and_ancestors_are_semantically_selected():
    current = observation()
    text, omitted, manifest = select_snapshot(current['snapshot'], counter(), 500,
        query='Order 527', ref=current['artifact_ref'], binding={'page_generation': 3})
    assert 'refresh contradicted' in text
    assert 'main "Task board"' in text
    assert 'group "Order 527"' in text
    assert 'Cancel completion' in text
    assert len(omitted) > 1400
    assert manifest['selected'] and manifest['dropped']
    assert manifest['content_hash'] == digest(current['snapshot'])


def test_public_context_manifest_exposes_workset_indexes_without_private_content():
    manifest = {
        'version': 'tracefix/context/1',
        'workset': {
            'binding': {'scope_id': 'scope', 'run_id': 'run', 'revision': 4,
                        'patch_hash': 'patch', 'source_manifest': 'source',
                        'test_spec_hash': 'spec', 'private': 'oracle'},
            'selected': [{'field': 'working_memory', 'id': 'note-1', 'refs': ['ev-1'],
                          'binding': {'revision': 4, 'environment_digest': 'env'}}],
            'dropped': [{'field': 'observation', 'reason': 'version_mismatch:patch_hash',
                         'details': {'source': 'oracle', 'message': 'hidden'}}],
            'limitations': ['upstream_truncated'],
            'snapshot': {'ref': 'obs.json', 'version': 'workset/1', 'content_hash': 'hash',
                         'selected': [{'start': 1, 'end': 2}], 'dropped': [{'start': 3, 'end': 4}],
                         'coverage': 'semantic_view', 'span': [1, 4], 'off': False,
                         'content': 'must not be copied'},
        },
    }
    projected = public_context_manifest(manifest)
    assert projected['version'] == 'tracefix/context/1'
    assert projected['binding']['scope_id'] == 'scope'
    assert projected['selected'][0]['refs'] == ['ev-1']
    assert projected['dropped'][0] == {'field': 'observation', 'reason': 'version_mismatch:patch_hash'}
    assert projected['snapshot']['selected'] == [{'start': 1, 'end': 2}]
    assert projected['snapshot']['coverage'] == 'semantic_view'
    assert projected['snapshot']['off'] is False
    assert 'content' not in projected['snapshot']
    assert 'private' not in json.dumps(projected)
    assert 'hidden' not in json.dumps(projected)


def test_semantic_range_expands_middle_original_and_is_scoped(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    current = observation()
    ref = artifacts.put('scope', 'run', current)
    expanded = expand_reference(artifacts, 'scope', 'run', ref, start=750, end=753,
        expected_hash=digest(current['snapshot']), binding={'patch_hash': 'patch'})
    assert 'refresh contradicted' in expanded['text']
    assert expanded['span'] == {'start': 750, 'end': 753}
    with pytest.raises(FileNotFoundError):
        expand_reference(artifacts, 'other', 'run', ref)
    path = artifacts.root / 'scope' / 'run' / ref
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        expand_reference(artifacts, 'scope', 'run', ref)


def test_repeated_failures_cluster_counts_first_last_and_business_variants():
    steps = [{'step': index, 'action': {'kind': 'click', 'locator': {'name': 'Order 527'}},
              'result': {'error': 'stored done=false'}, 'evidence_refs': [f'ev-{index}']}
             for index in range(200)]
    steps.append({'step': 201, 'action': steps[0]['action'],
                  'result': {'error': 'stored done=true'}, 'evidence_refs': ['different-value']})
    summary = compact_steps(steps, keep_recent=0)
    assert len(summary['failures']) == 2
    repeated = next(item for item in summary['failures'] if item['count'] == 200)
    assert repeated['first_refs'] == ['ev-0'] and repeated['last_refs'] == ['ev-199']
    assert len(repeated['variant_refs']) <= 3
    assert len(json.dumps(summary)) < 8000


def test_old_excluded_drops_after_patch_change_but_current_counterevidence_remains():
    context, manifest, _, _ = prepare_workset({'phase': 'DIAGNOSE', 'patch_hash': 'new',
        'working_memory': {'excluded': [{'id': 'old', 'text': 'not storage', 'metadata': {'patch_hash': 'old'}}],
                           'finding': [{'id': 'current', 'text': 'refresh contradicts DOM',
                                        'metadata': {'patch_hash': 'new'}}]}})
    assert context['working_memory']['excluded'] == []
    assert context['working_memory']['finding'][0]['id'] == 'current'
    assert manifest['dropped'][0]['reason'] == 'version_mismatch:patch_hash'


def test_unknown_pending_and_reverse_invariant_stay_uncertain_and_thin():
    context, manifest, _, _ = prepare_workset({'goal': 'Save and cancel completion',
        'invariants': [{'ref': 'spec', 'text': 'unchecking survives reload'}],
        'recent_steps': [{'status': 'UNKNOWN', 'error': 'request timeout',
                          'evidence_refs': ['inflight-operation']}]})
    assert context['pruning_facts']['pending'] == [
        {'status': 'unknown', 'refs': ['inflight-operation']}]
    assert context['pruning_facts']['invariants'][0]['text'] == 'unchecking survives reload'
    assert context['recent_steps'][0]['status'] == 'UNKNOWN'
    assert not manifest['recent_steps']['failures']


def test_assembler_bounds_repeated_log_fulltext_and_declares_provider_truncation():
    current = observation()
    current.update(provider_truncated=True, console=['error old ' * 400],
                   network=['POST /api done=false ' * 400])
    assembler = ContextAssembler(context_window=4500, output_tokens=100, overhead_tokens=100,
                                 counter=counter())
    result = assembler.assemble({'goal': 'Order 527', 'phase': 'DIAGNOSE',
        'patch_hash': 'patch', 'observation': current,
        'recent_action_results': [{'error': 'failed stored done=false', 'artifact_ref': f'error-{index}'}
                                  for index in range(100)]})
    assert result.manifest['tokens_after'] <= result.manifest['input_limit']
    assert result.context['pruning_facts'].keys() == {'goal', 'invariants', 'pending'}
    assert result.manifest['workset']['limitations'] == [
        'upstream_truncated: omitted source cannot be reconstructed']
    assert 'refresh contradicted' in result.context['observation']['snapshot']


def test_observation_version_and_note_scope_are_filtered_before_ranking():
    context, manifest, _, _ = prepare_workset({'scope_id': 'scope', 'page_generation': 4,
        'observation': observation(), 'working_memory': {'finding': [
            {'id': 'foreign', 'scope_id': 'other', 'text': 'Order 527 relevant but foreign'}]}})
    assert context['observation']['unavailable'] == 'version_mismatch:page_generation'
    assert context['working_memory']['finding'] == []
    assert any(item['reason'] == 'version_mismatch:scope_id' for item in manifest['dropped'])


def test_repeated_compaction_keeps_source_refs_count_support_and_counterevidence():
    steps = [{'step': index, 'passed': False, 'error': 'reload lost done=false',
              'support': 'DOM initially checked', 'counterevidence': 'API reads false',
              'unknown': 'write timing not established', 'evidence_refs': [f'ev-{index}']}
             for index in range(20)]
    summary = compact_steps(steps, keep_recent=0)
    for _ in range(5):
        summary = compact_steps(summary['clusters'], keep_recent=0)
    cluster = summary['failures'][0]
    assert cluster['count'] == 20
    assert cluster['first_refs'] == ['ev-0'] and cluster['last_refs'] == ['ev-19']
    assert cluster['fact']['support'] == 'DOM initially checked'
    assert cluster['fact']['counterevidence'] == 'API reads false'
    assert cluster['fact']['unknown'] == 'write timing not established'


def test_expansion_checks_bindings_hash_and_explicit_heldout_markers(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    ref = artifacts.put('scope', 'run', {'snapshot': 'public observation', 'patch_hash': 'old'})
    with pytest.raises(ValueError):
        expand_reference(artifacts, 'scope', 'run', ref, binding={'patch_hash': 'new'})
    with pytest.raises(ValueError):
        expand_reference(artifacts, 'scope', 'run', ref, expected_hash='wrong')
    hidden = artifacts.put('scope', 'run', {'snapshot': 'held out', 'source': 'oracle'})
    with pytest.raises(PermissionError):
        expand_reference(artifacts, 'scope', 'run', hidden)
    with pytest.raises(ValueError):
        expand_reference(artifacts, 'scope', 'run', ref, max_lines=201)


def test_expansion_long_line_is_bounded_and_marks_source_truncation(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    ref = artifacts.put('scope', 'run', {'snapshot': 'alert ' * 10000, 'provider_truncated': True})
    result = expand_reference(artifacts, 'scope', 'run', ref)
    assert len(result['text']) == 16000
    assert result['coverage'] == 'range_truncated'
    assert result['upstream_truncated'] is True


def test_expired_environment_or_spec_mismatch_and_hidden_nested_marker_drop():
    context, manifest, _, _ = prepare_workset({'phase': 'VERIFY',
        'environment_digest': 'env-current', 'test_spec_hash': 'spec-current',
        'working_memory': {'finding': [
            {'id': 'expired', 'text': 'old', 'expires_at': 1},
            {'id': 'env-old', 'text': 'old env', 'environment_digest': 'env-old'},
            {'id': 'spec-old', 'text': 'old spec', 'test_spec_hash': 'spec-old'},
            {'id': 'nested-hidden', 'text': {'result': {'provenance': 'oracle'}}},
            {'id': 'valid', 'text': 'current', 'environment_digest': 'env-current',
             'test_spec_hash': 'spec-current'}]}})
    assert [item['id'] for item in context['working_memory']['finding']] == ['valid']
    reasons = {item['id']: item['reason'] for item in manifest['dropped']}
    assert reasons['expired'] == 'expired'
    assert reasons['env-old'] == 'version_mismatch:environment_digest'
    assert reasons['spec-old'] == 'version_mismatch:test_spec_hash'
    assert reasons['nested-hidden'] == 'non_public_evidence'


def test_missing_environment_and_spec_fields_remain_compatible():
    context, manifest, _, binding = prepare_workset({'scope_id': 'scope',
        'working_memory': {'finding': [{'id': 'legacy', 'text': 'old compatible note'}]}})
    assert context['working_memory']['finding'][0]['id'] == 'legacy'
    assert binding['environment_digest'] is None and binding['test_spec_hash'] is None
    assert not manifest['dropped']
