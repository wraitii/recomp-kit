"""Whole-image calling-convention census (translate.py --ir-census FILE).

Runs after the translator has resolved every function, jump table and entry,
reusing its control flow, and writes per-function summaries plus totals. The
report measures how much of the image the IR's call optimizations could cover;
it changes nothing in the generated code.
"""
from collections import Counter
import json
import time

from .imports import read_import_cleanup
from .lift import Lifter, LiftError
from .cfg import function_ir  # compatibility export
from .summary import summarize_all


def direct_targets(fn):
    out = []
    for ins in fn.insns:
        if ins.mnem in ("CALL", "JMP") and ins.ops and ins.ops[0].startswith("0x"):
            t = int(ins.ops[0], 16)
            if ins.mnem == "CALL" or t not in fn.addrs:
                out.append(t)
    return out


def ret_purge(fn):
    """Bytes popped by the function's RET instructions when they all agree."""
    vals = set()
    for ins in fn.insns:
        if ins.mnem == "RET":
            vals.add(int(ins.ops[0], 16) if ins.ops else 0)
    return vals.pop() if len(vals) == 1 else None


def run_census(tr, functions, path, entries_by_fn=None, quiet=False):
    started = time.time()
    lifter = Lifter()
    by_addr = {fn.addr: fn for fn in functions}
    targets = {a: direct_targets(fn) for a, fn in by_addr.items()}
    purges = {a: ret_purge(fn) for a, fn in by_addr.items()}
    import_cleanup = read_import_cleanup()
    iat_purges = {a: import_cleanup.get((tr.image.iat_dlls.get(a, ""), name))
                  for a, name in tr.image.iat_names.items()}

    def progress(done, total):
        if not quiet and (done % 5000 < 1 or done == total):
            print("  ir census: %d/%d functions (%.0fs)" % (done, total, time.time() - started))

    summaries = summarize_all(sorted(by_addr), lambda a: function_ir(tr, lifter, by_addr[a]),
                              targets.__getitem__, tr.image.iat_names.get, progress=progress,
                              ret_purge=purges.get, import_purge=iat_purges.get)
    categories = Counter()
    totals = Counter()
    reasons = Counter()
    nonstandard = Counter()
    notes = Counter()
    for a, s in summaries.items():
        totals["functions"] += 1
        totals["ok" if s.ok else "failed"] += 1
        category = ("failed" if not s.ok else "standard" if s.standard() else
                    "nonstandard_entry_ebp" if "EBP" in s.inputs else "other_nonstandard")
        categories[category] += 1
        if s.standard():
            totals["standard"] += 1
        if not s.returns:
            totals["no return"] += 1
        if entries_by_fn and entries_by_fn.get(a):
            totals["with alternate entries"] += 1
        for r in s.reasons:
            reasons[r.split(":")[0] if r.startswith("lift") else r] += 1
        for n in s.notes:
            notes[n] += 1
        if s.ok and not s.standard():
            for r in sorted(s.inputs - {"ECX", "EDX", "DF"}):
                nonstandard["input %s" % r] += 1
            for r in sorted({"EBX", "EBP", "ESI", "EDI"} - s.preserved):
                nonstandard["clobbers %s" % r] += 1
            if s.x87_inputs:
                nonstandard["x87 inputs"] += 1
            if s.x87_delta not in (0, 1):
                nonstandard["x87 delta %s" % s.x87_delta] += 1
    report = {
        "seconds": round(time.time() - started, 1),
        "totals": dict(totals),
        "categories": dict(categories),
        "category_notes": {
            "nonstandard_entry_ebp": "Reads entry EBP; includes parent-frame unwind funclets. "
                                     "This trait alone does not prove unwinder-only reachability.",
        },
        "import_cleanup": {"iat_slots": len(iat_purges),
                           "known": sum(p is not None for p in iat_purges.values())},
        "failure_reasons": dict(reasons.most_common()),
        "nonstandard_reasons": dict(nonstandard.most_common()),
        "notes": dict(notes.most_common()),
        "functions": {"%08x" % a: s.as_json() for a, s in sorted(summaries.items())},
    }
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1)
    if not quiet:
        print("  ir census: %s" % json.dumps(report["totals"]))
        for k, v in list(reasons.most_common())[:10]:
            print("    failed: %6d  %s" % (v, k))
        for k, v in list(nonstandard.most_common())[:10]:
            print("    nonstandard: %6d  %s" % (v, k))
    return summaries
