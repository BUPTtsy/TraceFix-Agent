"""Evaluator-only source mutations. Never copy this module into Agent workspace."""
CASES = [
 {'id':'B01','family':'mutation-method','split':'dev','file':'src/api.ts',
  'before':"{method: 'PATCH', body: JSON.stringify(fields)}",'after':"{method: 'POST', body: JSON.stringify(fields)}",'goal':'Mark Write project brief complete, reload, and verify it stays complete.'},
 {'id':'B02','family':'delete-route','split':'test','file':'src/api.ts',
  'before':"`/api/tasks/${id}`, {method: 'DELETE'}",'after':"`/api/task/${id}`, {method: 'DELETE'}",'goal':'Delete Write project brief and verify it disappears after reload.'},
 {'id':'B03','family':'empty-title','split':'dev','file':'src/App.tsx',
  'before':"if (!title.trim()) {setError('Title is required'); return;}", 'after':"if (!title.trim()) return;",'goal':'Submit a blank task title and verify an explanatory error is shown.'},
 {'id':'B04','family':'modal-draft','split':'validation','file':'src/App.tsx',
  'before':"setDraft(editing.title);",'after':"setDraft('');",'goal':'Open edit for Write project brief and verify its existing title is prefilled.'},
 {'id':'B05','family':'filter-search-state','split':'dev','file':'src/App.tsx',
  'before':"setFilter(f);setPage(0);",'after':"setFilter(f);setPage(0);setQuery(f);",'goal':'Choose Todo filter and verify the first Todo task is shown on page one.'},
 {'id':'B06','family':'search-normalization','split':'test','file':'src/App.tsx',
  'before':"t.title.toLowerCase().includes(query.toLowerCase())",'after':"t.title.includes(query)",'goal':'Search for WRITE and verify Write project brief is visible.'},
 {'id':'B07','family':'save-refresh','split':'validation','file':'src/App.tsx',
  'before':"try {await work(); await refresh();}",'after':"try {await work();}",'goal':'Create a task named Fresh item and verify it appears immediately.'},
 {'id':'B08','family':'pagination-boundary','split':'test','file':'src/App.tsx',
  'before':"filtered.slice(page*PAGE_SIZE, (page+1)*PAGE_SIZE)",'after':"filtered.slice(page*PAGE_SIZE+1, (page+1)*PAGE_SIZE)",'goal':'Verify the first seeded task Write project brief is visible.'},
 {'id':'B09','family':'error-recovery','split':'dev','file':'src/App.tsx',
  'before':"finally {setBusy(false);}",'after':"finally {setBusy(true);}",'goal':'Create a task and verify Add task is enabled afterward.'},
 {'id':'B10','family':'cancel-modal','split':'validation','file':'src/App.tsx',
  'before':'type="button" onClick={()=>setEditing(null)}>Cancel',
  'after':'type="button" onClick={()=>setError(\'\')}>Cancel','goal':'Open edit, click Cancel, and verify the modal closes.'},
 {'id':'B11','family':'server-title-write','split':'test','file':'server/tasks.mjs',
  'before':"if ('title' in fields) next.title = validateTitle(fields.title);",'after':"if ('title' in fields) validateTitle(fields.title);",'goal':'Rename Write project brief to Revised brief and verify the change persists after reload.'},
 {'id':'B12','family':'status-filter','split':'dev','file':'src/App.tsx',
  'before':"filter === 'All' || t.status === filter",'after':"filter === 'All' || t.status !== filter",'goal':'Choose Done and verify only completed tasks are shown.'},
]
CONTROLS = [{'id':'CLEAN','kind':'bug-free'}, {'id':'FLAKY','kind':'nondeterministic oracle control'}, {'id':'INFRA','kind':'unreachable sandbox control'}]

REVISIONS = {
    'e7': 'e7a9c04002a2d51a14d37da1f4808c2ee4695c57',
    'natural': '5a280cc3dfb739585ab1523881d72f1bc8e54e6e',
    'clean_v2': '0e562dfb4d99da36a6905d1e5651918e23678258',
}


def case_mutations(case):
    if 'mutations' in case:
        return tuple(dict(mutation) for mutation in case['mutations'])
    if case.get('file'):
        return ({name: case[name] for name in ('file', 'before', 'after')},)
    return ()


def frozen_cases(tier='smoke'):
    if tier not in {'smoke', 'standard'}:
        raise ValueError('tier 必须为 smoke 或 standard')
    cases = [dict(case, category='B', track='compare', revision=REVISIONS['e7'],
                  good_revision=REVISIONS['e7'], oracle_cases=[case['id']], known_bug=True)
             for case in CASES]
    by_id = {case['id']: case for case in cases}
    for identifier, components in [('M01', ('B01', 'B11')), ('M02', ('B02', 'B07')),
                                   ('M03', ('B06', 'B12'))]:
        cases.append({'id': identifier, 'family': 'multi-file', 'split': 'dev', 'category': 'M',
                      'track': 'compare', 'revision': REVISIONS['e7'],
                      'good_revision': REVISIONS['e7'], 'known_bug': True,
                      'mutations': [mutation for component in components
                                    for mutation in case_mutations(by_id[component])],
                      'oracle_cases': list(components),
                      'goal': ' '.join(by_id[component]['goal'] for component in components)})
    natural_goals = [
        'Change a task priority, save, reload, and verify the priority persists.',
        'Search for review and verify Review navigation flow and Review security checklist are shown.',
        'Sort tasks by priority and verify High precedes Medium and Low.',
        'Navigate through all task pages and verify all thirteen seeded tasks are reachable.',
        'Export the task board as CSV and verify the download succeeds.',
    ]
    for index, goal in enumerate(natural_goals, 1):
        cases.append({'id': f'N{index:02}', 'family': 'natural', 'split': 'test', 'category': 'N',
                      'track': 'compare', 'revision': REVISIONS['natural'],
                      'good_revision': REVISIONS['clean_v2'], 'known_bug': True,
                      'mutations': [], 'oracle_cases': [f'N{index:02}'], 'goal': goal,
                      'oracle_status': 'pending'})
    for identifier, revision in [('CLEAN-e7', REVISIONS['e7']), ('CLEAN-v2', REVISIONS['clean_v2'])]:
        cases.append({'id': identifier, 'family': 'bug-free', 'split': 'test', 'category': 'CLEAN',
                      'track': 'compare', 'revision': revision, 'good_revision': revision,
                      'known_bug': False, 'mutations': [], 'oracle_cases': [],
                      'goal': 'Verify the task board loads and Add task is enabled without changing source code.',
                      'oracle_status': 'pending' if identifier == 'CLEAN-v2' else 'existing'})
    if tier == 'smoke':
        selected = {'B01', 'B06', 'B11', 'M01', 'CLEAN-e7'}
        return [case for case in cases if case['id'] in selected]
    return cases
