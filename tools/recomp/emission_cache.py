"""Validated whole-image preparation reused for selected-function emission."""
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from enum import Enum
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import pickle
import shutil
import sqlite3
import sys

from program import TranslateError
from ir import x87_facts

FORMAT = 'recomp-emission-cache-v3'
NON_PRODUCING = x87_facts.NON_PRODUCING
RE_RUN_MODULES = x87_facts.RE_RUN_MODULES


def source_revisions(root=None):
    """Preparation sources and the finalization sources re-run by selected regeneration."""
    root = Path(__file__).resolve().parent if root is None else Path(root)
    preparation, finalization = [], []
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root)
        if NON_PRODUCING.intersection(relative.parts):
            continue
        entry = (str(relative), hashlib.sha256(path.read_bytes()).hexdigest())
        (finalization if str(relative) in RE_RUN_MODULES else preparation).append(entry)
    return {'preparation': x87_facts.digest(preparation),
            'finalization': x87_facts.digest(finalization)}


def identity(settings, *, code_map=True):
    revisions = source_revisions()
    inputs = {
        'sources': revisions['preparation'],
        'policy': asdict(settings.emit),
        'translate': dict(settings.cfg['translate']),
        'game': settings.cfg['game'], 'curated': settings.curated, 'module': settings.module,
        'allow_unmodelled': settings.allow_unmodelled,
        'executable': hashlib.sha256(settings.exe.read_bytes()).hexdigest(),
        'dependencies': {name: version(name) for name in ('pypcode', 'capstone', 'pefile')},
        'python': sys.implementation.cache_tag,
        'facts': hashlib.sha256(settings.x87_facts.read_bytes()).hexdigest() if settings.x87_facts else None,
        'boundaries': hashlib.sha256(settings.x87_mode_metadata.read_bytes()).hexdigest() if settings.x87_mode_metadata else None,
        'native': {name: x87_facts.tree_digest(Path(__file__).resolve().parents[2] / name, ('*.c', '*.cpp', '*.h'))
                   for name in ('runtime', 'dx', 'mods')},
        'game_native': x87_facts.tree_digest(settings.game_dir, ('*.c', '*.cpp', '*.h')),
    }
    if code_map:
        inputs['code_map'] = x87_facts.tree_digest(settings.code_map, ('*',))
    if settings.indirect_targets:
        inputs['indirect_targets'] = hashlib.sha256(settings.indirect_targets.read_bytes()).hexdigest()
    return x87_facts.digest(inputs)


def canonical(value):
    if isinstance(value, Mapping):
        return ('mapping', sorted(((canonical(k), canonical(v)) for k, v in value.items()), key=repr))
    if is_dataclass(value):
        return (type(value).__module__, type(value).__qualname__,
                [(field.name, canonical(getattr(value, field.name))) for field in fields(value) if field.init])
    if isinstance(value, Enum):
        return (type(value).__module__, type(value).__qualname__, canonical(value.value))
    if isinstance(value, (set, frozenset)):
        return ('set', sorted((canonical(item) for item in value), key=repr))
    if isinstance(value, (tuple, list)):
        return (type(value).__name__, [canonical(item) for item in value])
    if isinstance(value, bytes):
        return ('bytes', value.hex())
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError('unsupported preparation cache input: ' + type(value).__qualname__)


def preparation_key(namespace, fir, request):
    if fir is None or isinstance(fir, tuple):
        return x87_facts.digest(canonical((namespace, fir, request)))
    instructions = ([[(name, getattr(ins, name)) for name in ins.__slots__ if name != 'ops']
                     for ins in fir.insns] if fir is not None else ())
    body = {name: value for name, value in vars(fir).items() if name != 'insns'} if fir is not None else {}
    return x87_facts.digest(canonical((namespace, body, instructions, request)))


class AnalysisCache:
    def __init__(self, root, namespace, readonly=False):
        self.namespace = namespace
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'analysis.sqlite'
        self.connection = (sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) if readonly else sqlite3.connect(path))
        if not readonly:
            self.connection.execute('CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID')
        self.pending, self.used, self.counts = {}, set(), {}

    def key(self, fir, request):
        return preparation_key(self.namespace, fir, request)

    def get(self, key, kind='contract'):
        self.used.add(key)
        value = self.pending.get(key)
        if value is None:
            row = self.connection.execute('SELECT value FROM results WHERE key = ?', (key,)).fetchone()
            value = pickle.loads(row[0]) if row is not None else None
        counter = kind + ('_hits' if value is not None else '_misses')
        self.counts[counter] = self.counts.get(counter, 0) + 1
        return value

    def put(self, key, value):
        self.used.add(key)
        self.pending[key] = value

    def retain(self, key):
        self.used.add(key)

    def finish(self):
        try:
            with self.connection:
                self.connection.executemany('INSERT OR REPLACE INTO results VALUES (?, ?)',
                    ((key, pickle.dumps(value, protocol=5)) for key, value in self.pending.items()))
                self.connection.execute('CREATE TEMP TABLE used (key TEXT PRIMARY KEY) WITHOUT ROWID')
                self.connection.executemany('INSERT INTO used VALUES (?)', ((key,) for key in self.used))
                self.connection.execute('DELETE FROM results WHERE key NOT IN (SELECT key FROM used)')
        finally:
            self.connection.close()


def relocate(plan, root):
    return replace(plan, **{name: root / value.name for name in ('path', 'body')
                           if isinstance(value := getattr(plan, name, None), Path)})


class PreparationCache:
    def __init__(self, root, namespace):
        self.root, self.namespace = Path(root), namespace
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / 'preparation.pickle'
        self.previous, self.entries, self.counts = {}, {}, {}
        if manifest.is_file():
            with manifest.open('rb') as stream:
                state = pickle.load(stream)
            if state.get('format') == FORMAT and state.get('namespace') == namespace:
                self.previous = state['entries']

    def key(self, fir, request):
        return preparation_key(self.namespace, fir, request)

    def path(self, key):
        return self.root / ('prepared-' + key + '.pickle')

    def get(self, key, kind):
        plan = self.entries.get(key, self.previous.get(key))
        if plan is not None:
            plan = relocate(plan, self.root)
            if all(not isinstance(path, Path) or path.is_file()
                   for path in (getattr(plan, 'path', None), getattr(plan, 'body', None))):
                self.entries[key] = relocate(plan, Path())
                self.counts[kind + '_hits'] = self.counts.get(kind + '_hits', 0) + 1
                return plan
        self.counts[kind + '_misses'] = self.counts.get(kind + '_misses', 0) + 1
        return None

    def put(self, key, plan):
        self.entries[key] = relocate(plan, Path())

    def state(self):
        return self.entries, self.counts


def tree(out):
    root = Path(out)
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()
            and path.name != 'x86.h' and not any(part.startswith('aux-') for part in path.relative_to(root).parts)}


def load(root, settings, out):
    index = Path(root) / 'index.pickle'
    if not index.is_file():
        raise TranslateError('selected regeneration needs a cached full generation; run --regenerate --cache-emission first')
    with index.open('rb') as stream:
        state = pickle.load(stream)
    if state.get('format') != FORMAT or state['identity'] != identity(settings):
        raise TranslateError('selected regeneration rejected: preparation, policy or translation inputs changed; run a full --regenerate')
    if state['tree'] != tree(out):
        raise TranslateError('selected regeneration rejected: generated tree differs from its cached generation; run a full --regenerate')
    return state


def save(root, settings, out, plans, private_plans, analysis, functions, entries_by_fn, results, report,
         known_modes, preparation=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for plan in plans.values():
        if plan.path is not None:
            if plan.path != root / plan.path.name:
                shutil.copy2(plan.path, root / plan.path.name)
    for plan in private_plans:
        if plan.body is not None:
            if plan.body != root / plan.body.name:
                shutil.copy2(plan.body, root / plan.body.name)
    state = {'format': FORMAT, 'identity': identity(settings), 'tree': tree(out),
             'revisions': source_revisions(),
             'plans': {addr: replace(plan, path=Path(plan.path.name) if plan.path is not None else None)
                       for addr, plan in plans.items()},
             'private_plans': [replace(plan, body=Path(plan.body.name) if plan.body is not None else None)
                               for plan in private_plans],
             'analysis': analysis, 'functions': functions, 'entries_by_fn': entries_by_fn,
             'results': results, 'report': report, 'known_modes': frozenset(known_modes), 'history': []}
    write_index(root, state)
    if preparation is not None:
        namespace, entries = preparation
        temporary = root / 'preparation.pickle.new'
        with temporary.open('wb') as stream:
            pickle.dump({'format': FORMAT, 'namespace': namespace, 'entries': entries}, stream, protocol=5)
        temporary.replace(root / 'preparation.pickle')
        retained = {path.name for plan in [*plans.values(), *private_plans, *entries.values()]
                    for path in (getattr(plan, 'path', None), getattr(plan, 'body', None))
                    if isinstance(path, Path)}
        retained.update(('index.pickle', 'preparation.pickle'))
        for path in root.glob('*.pickle*'):
            if path.name not in retained:
                path.unlink()


def write_index(root, state):
    root = Path(root)
    temporary = root / 'index.pickle.new'
    with temporary.open('wb') as stream:
        pickle.dump(state, stream, protocol=5)
    temporary.replace(root / 'index.pickle')


def update(root, state, out, selected):
    state['history'].append({'functions': ['%08x' % addr for addr in sorted(selected)],
                             'revisions': source_revisions()})
    Path(out, 'partial-generations.json').write_text(
        json.dumps({'format': FORMAT, 'history': state['history']}, indent=1) + '\n')
    state['tree'] = tree(out)
    write_index(root, state)
