"""Immutable frontend snapshots; every preparation receives a fresh graph."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import pickle
from tempfile import NamedTemporaryFile

from frontend import function_ir, external_exits, table_jumps
from .lift import LiftError
from .normalize import codegen_ir


@dataclass(frozen=True)
class FunctionArtifact:
    identity: str
    payload: bytes

    def materialize(self):
        return pickle.loads(self.payload)


class ArtifactStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.local = {}
        self.reusable = set()
        self.counts = {"lifted_builds": 0, "lifted_reuses": 0,
                       "normalized_builds": 0, "normalized_reuses": 0}

    def _get(self, phase, key, create):
        identity = hashlib.sha256(pickle.dumps((phase, key), protocol=5)).hexdigest()
        path = self.root / (identity + ".artifact") if self.root else None
        artifact = self.local.get(identity)
        if artifact is None and path is not None and path.exists():
            artifact = pickle.loads(path.read_bytes())
        reused = artifact is not None
        if reused:
            if artifact.identity != identity:
                raise ValueError("frontend artifact identity mismatch")
            self.counts[phase + "_reuses"] += 1
        else:
            graph = create()
            artifact = FunctionArtifact(identity, pickle.dumps(graph, protocol=5))
            self.counts[phase + "_builds"] += 1
            if path is not None:
                with NamedTemporaryFile(dir=self.root, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(pickle.dumps(artifact, protocol=5))
                temporary.replace(path)
        if self.root is None:
            self.local[identity] = artifact
        if reused:
            graph = artifact.materialize()
        action = "reused_instructions" if reused else "built_instructions"
        self.counts[phase + "_" + action] = self.counts.get(phase + "_" + action, 0) + len(graph.insns)
        return graph

    def flush_counts(self):
        if self.root is not None:
            (self.root / ("counts-%d.json" % os.getpid())).write_text(json.dumps(self.counts))

    def lifted(self, tr, lifter, fn):
        for ins in fn.insns:
            if ins.decode is None:
                raise LiftError("%08x: instruction decode evidence missing" % ins.addr)
        key = ("frontend-v3", lifter.language, fn.addr,
               tuple((ins.addr, ins.mnem, ins.decode.source_bytes) for ins in fn.insns),
               tuple(tuple(tr.successors(fn, i)) for i in range(len(fn.insns))),
               tuple((i, tr.never_returns(ins)) for i, ins in enumerate(fn.insns)),
               tr.seh_effects(fn), tuple(sorted(dict(fn.seh_sites).items())), tuple((site, targets) for (owner, site), targets in sorted(tr.jumptables.items())
                                        if owner == fn.addr), fn.return_jumps, fn.dead_addrs,
               fn.pushed_continuations, tuple(fn.fallthrough), tuple(fn.contiguous), fn.end,
               external_exits(tr, fn), table_jumps(tr, fn))
        return self._get("lifted", key, lambda: function_ir(tr, lifter, fn))

    def reuse_normalized(self, addr):
        self.reusable.add(addr)
        if self.root is not None:
            (self.root / ("reuse-%08x" % addr)).touch()

    def normalized(self, fir, lifter, tails=()):
        reusable = self.root is None or fir.addr in self.reusable or (self.root / ("reuse-%08x" % fir.addr)).exists()
        if not reusable:
            graph = codegen_ir(fir, lifter, tails)
            self.counts["normalized_builds"] += 1
            self.counts["normalized_built_instructions"] = self.counts.get("normalized_built_instructions", 0) + len(graph.insns)
            return graph
        key = (lifter.language, pickle.dumps(fir, protocol=5), tuple(sorted(tails)))
        return self._get("normalized", key, lambda: codegen_ir(fir, lifter, tails))
