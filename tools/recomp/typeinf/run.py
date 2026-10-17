import argparse
import json
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(KIT / "tools"), str(KIT / "tools" / "recomp")]

import game_config  # noqa: E402
from program import Program  # noqa: E402
from settings import Settings  # noqa: E402
from typeinf.core import Functions, analyze, call_closure, this_node, vtable_slots  # noqa: E402
from typeinf.observed import ObservedEdges, load_observed  # noqa: E402
from typeinf.values import describe  # noqa: E402


def _key(value):
    if isinstance(value, tuple):
        return ":".join(_key(part) for part in value)
    return "%08x" % value if isinstance(value, int) else str(value)


def _plain(value):
    if isinstance(value, dict):
        return {_key(key) if not isinstance(key, str) else key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(item) for item in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    return value


def _node_shape(shapes, node):
    return {offset: {"kinds": sorted(field["kinds"]), "labels": sorted(field["labels"]), "sizes": field["sizes"],
                     "tables": field["tables"]}
            for offset, field in sorted(shapes.get(node, {}).items())}


def _explain(fv, shapes):
    values = {value.id: value for value in fv.ssa.values}
    print("== %08x" % fv.addr)
    for node in sorted(node for node in shapes if node[0] == fv.addr):
        fields = shapes[node]
        print("  %s" % describe(fv, values[node[1]]))
        for offset, field in sorted(fields.items()):
            print("    %+#06x %s%s%s" % (offset, "/".join(sorted(field["kinds"])) or "-",
                                      " " + ",".join(sorted(field["labels"])) if field["labels"] else "",
                                      " tables=" + ",".join("%08x" % t for t in sorted(field["tables"]))
                                      if field["tables"] else ""))


def main():
    parser = argparse.ArgumentParser(description="Infer object shapes from original bytes")
    game_config.add_game_args(parser)
    parser.add_argument("--root", type=lambda v: int(v, 16), action="append", default=[])
    parser.add_argument("--vtable", type=lambda v: int(v, 16), action="append", default=[])
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--observe", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--explain", type=lambda v: int(v, 16), action="append", default=[])
    parser.add_argument("--no-abi", action="store_true", help="do not assume indirect callees preserve EBX/ESI/EDI/EBP")
    args = parser.parse_args()
    game_config.resolve_game_args(args)
    program = Program(Settings(game_config.load(args.game_dir, args.build_root), None, 1, None))
    observed = load_observed(args.observe, program) if args.observe else ObservedEdges()
    deadline = time.monotonic() + args.timeout
    functions = Functions(program)
    started = time.monotonic()
    roots = args.root + [program.pe.rd32(table + slot) for table in args.vtable
                         for slot in vtable_slots(program, table) or ()
                         if program.pe.rd32(table + slot) in program.functions]
    seeds = call_closure(functions, roots or sorted(program.functions), observed, args.depth, deadline)
    report = analyze(functions, seeds, observed, deadline, abi=not args.no_abi)
    lifted, shapes = report.pop("lifted"), report.pop("shapes")
    report["this"] = {addr: _node_shape(shapes, node) for addr, fv in lifted.items()
                      if (node := this_node(addr, fv)) is not None and node in shapes}
    for addr in args.explain:
        _explain(lifted[addr], shapes)
    report["stats"]["seconds"] = round(time.monotonic() - started, 1)
    args.out.write_text(json.dumps(_plain(report), indent=1, default=str))
    print(json.dumps(_plain({"functions": {k: (v if isinstance(v, int) else len(v))
                                           for k, v in report["functions"].items()},
                             "classes": len(report["classes"]), **report["stats"]}), indent=1))


if __name__ == "__main__":
    main()
