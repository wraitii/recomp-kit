"""Validated whole-image preparation reused for selected-function emission."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import pickle
import shutil

from program import TranslateError
from ir import x87_facts

FORMAT = 'recomp-emission-cache-v1'
EMITTERS = frozenset(('ir/emit_c.py', 'ir/emit_llvm.py', 'ir/llvm_cfg.py', 'output.py'))


def identity(settings):
    root = Path(__file__).resolve().parent
    sources = [(str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
               for path in sorted(root.rglob('*.py')) if str(path.relative_to(root)) not in EMITTERS
               and 'tests' not in path.relative_to(root).parts]
    return x87_facts.digest({
        'sources': sources, 'policy': asdict(settings.emit),
        'translate': {key: value for key, value in settings.cfg['translate'].items() if key != 'backend'},
        'game': settings.cfg['game'], 'curated': settings.curated, 'module': settings.module,
        'allow_unmodelled': settings.allow_unmodelled,
        'executable': hashlib.sha256(settings.exe.read_bytes()).hexdigest(),
        'code_map': x87_facts.tree_digest(settings.code_map, ('*',)),
        'facts': hashlib.sha256(settings.x87_facts.read_bytes()).hexdigest() if settings.x87_facts else None,
        'boundaries': hashlib.sha256(settings.x87_mode_metadata.read_bytes()).hexdigest() if settings.x87_mode_metadata else None,
        'native': {name: x87_facts.tree_digest(root.parents[1] / name, ('*.c', '*.cpp', '*.h'))
                   for name in ('runtime', 'dx', 'mods')},
        'game_native': x87_facts.tree_digest(settings.game_dir, ('*.c', '*.cpp', '*.h')),
    })


def tree(out):
    root = Path(out)
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()
            and path.name != 'x86.h' and not any(part.startswith('aux-') for part in path.relative_to(root).parts)}


def load(root, settings, out):
    index = Path(root) / 'index.pickle'
    if not index.is_file():
        raise TranslateError('selected regeneration needs a cached full generation; run --regenerate first')
    with index.open('rb') as stream:
        state = pickle.load(stream)
    if state.get('format') != FORMAT or state['identity'] != identity(settings):
        raise TranslateError('selected regeneration rejected: preparation, policy or translation inputs changed; run a full --regenerate')
    if state['tree'] != tree(out):
        raise TranslateError('selected regeneration rejected: generated tree differs from its cached generation; run a full --regenerate')
    return state


def save(root, settings, out, plans, private_plans, analysis, functions, entries_by_fn, results, report):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for plan in plans.values():
        if plan.path is not None:
            shutil.copy2(plan.path, root / plan.path.name)
    for plan in private_plans:
        if plan.body is not None:
            shutil.copy2(plan.body, root / plan.body.name)
    state = {'format': FORMAT, 'identity': identity(settings), 'tree': tree(out),
             'plans': {addr: replace(plan, path=Path(plan.path.name) if plan.path is not None else None)
                       for addr, plan in plans.items()},
             'private_plans': [replace(plan, body=Path(plan.body.name) if plan.body is not None else None)
                               for plan in private_plans],
             'analysis': analysis, 'functions': functions, 'entries_by_fn': entries_by_fn,
             'results': results, 'report': report, 'history': []}
    write_index(root, state)


def write_index(root, state):
    root = Path(root)
    temporary = root / 'index.pickle.new'
    with temporary.open('wb') as stream:
        pickle.dump(state, stream, protocol=5)
    temporary.replace(root / 'index.pickle')


def update(root, state, out, selected):
    revision = x87_facts.tree_digest(Path(__file__).resolve().parent, ('*.py',))
    state['history'].append({'functions': ['%08x' % addr for addr in sorted(selected)], 'emitter_revision': revision})
    Path(out, 'partial-generations.json').write_text(json.dumps({'format': FORMAT, 'history': state['history']}, indent=1) + '\n')
    state['tree'] = tree(out)
    write_index(root, state)
