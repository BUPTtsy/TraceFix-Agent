"""编码前由宿主按当前增量预查 CodeGraph 符号与调用关系，在字符预算内压缩为 coder prompt 的 code_map。"""
from __future__ import annotations

import json
import re
from time import monotonic

from tracefix.rules.codegraph import CodeGraphError, RepositoryCodeGraph, is_graph_file
from tracefix.runtime.contracts import digest
from tracefix.runtime.harness import CODE_MAP_TOTAL, scrub
from tracefix.storage.artifacts import sanitize


SEED_LIMIT = 5
KIND_SCORE = {'component': 3, 'function': 3, 'method': 3, 'class': 2, 'variable': 1, 'constant': 1}
SKIPPED_KINDS = {'import', 'export', 'file', 'module'}
LOCATION = re.compile(r'([\w@$./-]+\.[A-Za-z]\w*):(\d+)')
TIERS = ('T1', 'T2', 'T3', 'T4')
ACCESS = {'T1': 'writable', 'T2': 'writable', 'T3': 'allowed', 'T4': 'outside'}


def unavailable(reason, **values):
    return {'source_version': None, 'seeds': [], 'files': {}, 'edges': [], 'omitted': {},
            'unavailable': scrub(sanitize(str(reason)))[:200], **values}


def read_sources(workspace, paths):
    """同 code.analyze：只读可索引授权源码，跳过受保护、缺失或非 UTF-8 文件。"""
    sources = {}
    for path in paths:
        if not is_graph_file(path):
            continue
        try:
            sources[path] = workspace.path(path).read_bytes().decode('utf-8')
        except (PermissionError, FileNotFoundError, UnicodeDecodeError):
            continue
    return sources


def sources_changed(workspace, versions):
    for path, version in versions.items():
        try:
            if digest(workspace.path(path).read_bytes()) != version:
                return True
        except OSError:
            return True
    return False


def seed_score(symbol, text, locations):
    name, path, start = symbol['name'], symbol['filePath'], symbol.get('startLine') or 0
    score = KIND_SCORE.get(symbol.get('kind'), 0)
    if len(name) >= 3 and re.search(r'(?<![\w$])' + re.escape(name) + r'(?![\w$])', text):
        score += 100
    end = symbol.get('endLine')
    score += max((80 if end and start <= line <= end else 30 for ref, line in locations
                  if ref == path or ref.endswith('/' + path)), default=0)
    return score


def relations(graph, key, seed):
    """解析 callers/callees；名称歧义时只保留与种子同文件（优先同起始行）的定义。"""
    definitions = graph.get('definitions')
    if not isinstance(definitions, list):
        raise CodeGraphError('CodeGraph 关系结果结构无效')
    matched = [entry for entry in definitions if isinstance(entry, dict)
               and isinstance(entry.get('definition'), dict)
               and entry['definition'].get('filePath') == seed['filePath']]
    exact = [entry for entry in matched if entry['definition'].get('startLine') == seed.get('startLine')]
    for entry in exact or matched:
        sites = {}
        for edge in entry.get('edges') or []:
            if isinstance(edge, dict):
                sites.setdefault(edge.get('source' if key == 'callers' else 'target'), edge.get('line'))
        for node in entry.get(key) or []:
            if (isinstance(node, dict) and isinstance(node.get('name'), str) and isinstance(node.get('filePath'), str)
                    and node.get('id') != entry['definition'].get('id') and node.get('kind') not in SKIPPED_KINDS):
                # 调用方定位到调用点，被调方定位到定义处：都是另一端需要 Read 的位置。
                line = sites.get(node.get('id')) if key == 'callers' else None
                yield node, line or node.get('startLine')


def build_code_map(engine, state, item, feedback, *, budget=CODE_MAP_TOTAL, deadline_seconds=60):
    """查询当前增量的种子符号与一跳调用关系，按 T1→T4 整条填入预算；任何失败只降级不抛出。"""
    started = monotonic()
    writable, allowed = set(item.writable_files), set(item.allowed_files)
    if not writable:
        return unavailable('当前增量没有可写文件')
    try:
        sources = read_sources(engine.workspace, engine.source['files'])
        if not sources:
            return unavailable('没有可索引的授权源码')
        selected = [path for path in item.writable_files if path in sources]
        if not selected:
            return unavailable('可写文件均不在 CodeGraph 支持范围')
        versions = {path: digest(content.encode('utf-8')) for path, content in sources.items()}
        project = engine.scopes.projects[state.scope_id]
        index = RepositoryCodeGraph(project, engine.artifacts.root.parent)
        binding = {'scope_id': state.scope_id, 'run_id': state.run_id, 'source_manifest': state.source_manifest,
                   'patch_hash': state.patch_hash, 'view': 'run'}
        text = '\n'.join([item.title, state.goal or '', json.dumps(feedback, ensure_ascii=False) if feedback else ''])
        locations = [(ref.lstrip('./'), int(line)) for ref, line
                     in LOCATION.findall(text.replace('\\\\', '/').replace('\\', '/'))]
        candidates, incomplete, pairs = {tier: {} for tier in TIERS}, [], set()
        with index.query_session(list(sources.items()), binding) as session:
            symbols = [symbol for symbol in session.symbols(selected)[0]
                       if symbol.get('kind') not in SKIPPED_KINDS and isinstance(symbol.get('name'), str)]
            ranked = sorted(symbols, key=lambda symbol: (-seed_score(symbol, text, locations),
                            symbol['filePath'], symbol.get('startLine') or 0))
            seeds = ranked[:SEED_LIMIT]
            for symbol in ranked:
                end, start = symbol.get('endLine'), symbol.get('startLine')
                line = f'L{start}' + (f'-{end}' if end and end != start else '')
                identity = (symbol['filePath'], symbol['name'], start)
                candidates['T1'].setdefault(identity, (identity, f"{scrub(symbol['name'])} {symbol.get('kind')} {line}", None))
            for seed in seeds:
                for key in ('callers', 'callees'):
                    if monotonic() - started >= deadline_seconds:
                        incomplete.append(scrub(seed['name']))
                        break
                    for node, line in relations(getattr(session, key)(seed['name']), key, seed):
                        path = node['filePath']
                        # 两个种子互为调用方/被调方时，同一条边只保留先查到的一次。
                        pair = (node['name'], seed['name']) if key == 'callers' else (seed['name'], node['name'])
                        if pair in pairs:
                            continue
                        pairs.add(pair)
                        tier = 'T2' if path in writable else 'T3' if path in allowed else 'T4'
                        other, own = scrub(node['name']), scrub(seed['name'])
                        location = f'{scrub(path)}:{line}, {ACCESS[tier]}'
                        edge = f'{other} -> {own} ({location})' if key == 'callers' else f'{own} -> {other} ({location})'
                        identity = (path, node['name'], node.get('startLine'))
                        candidates[tier].setdefault(edge, (identity,
                            f"{other} {node.get('kind')} L{node.get('startLine')}", edge))
            source_version = session.sync['source_version']
        if sources_changed(engine.workspace, versions):
            return unavailable('CodeGraph 查询期间授权源码已变化，已放弃图谱')
    except (OSError, ValueError, RuntimeError, LookupError) as error:
        return unavailable(f'{type(error).__name__}: {error}')
    omitted = {tier: 0 for tier in TIERS}
    seeds = [f"{scrub(seed['name'])} ({scrub(seed['filePath'])}:{seed.get('startLine')})" for seed in seeds]
    # 计量时 omitted 先按七位数占位，最终计数只会更短，保证总长不超预算。
    result = {'source_version': source_version, 'seeds': seeds, 'files': {}, 'edges': [],
              'omitted': {**{tier: 10 ** 6 for tier in TIERS}, **({'seeds': incomplete} if incomplete else {})},
              'unavailable': None}
    size = lambda: len(json.dumps(result, ensure_ascii=False))
    while seeds and size() > budget:
        seeds.pop()
    if size() > budget:
        return unavailable('code_map 预算不足以容纳图谱骨架')
    known = set()
    for tier in TIERS:
        for identity, symbol, edge in candidates[tier].values():
            path = identity[0]
            access = 'writable' if path in writable else 'allowed' if path in allowed else 'outside'
            entry = result['files'].get(scrub(path))
            created, fresh = entry is None, identity not in known
            if created:
                entry = result['files'][scrub(path)] = {'access': access, 'symbols': []}
            if fresh:
                entry['symbols'].append(symbol)
            if edge:
                result['edges'].append(edge)
            if size() <= budget:
                known.add(identity)
                continue
            # 整条丢弃：撤回本条新增的边、符号与空文件项。
            omitted[tier] += 1
            if edge:
                result['edges'].pop()
            if fresh:
                entry['symbols'].pop()
            if created:
                del result['files'][scrub(path)]
    result['omitted'] = {**omitted, **({'seeds': incomplete} if incomplete else {})}
    return result
