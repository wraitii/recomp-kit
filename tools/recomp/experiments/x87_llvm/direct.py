"""Explicit successful-access contract for the code-generation comparison.

This selects a helper ABI, not instruction semantics or stack values. Memory is
mapped and separate from X86/runtime storage; no memory watch hooks, asynchronous
observers or interior faults. Explicit rk_observe and full exit state remain.
"""
import re

HELPERS = ('load', 'load64', 'store', 'fnstsw', 'ret')


def direct_ir(text, effects=False):
    for name in HELPERS:
        text = text.replace(f'@rk_{name}(', f'@rk_direct_{name}(')
    attrs = ' "recomp.x87.direct"'
    if effects:
        attrs += ' "recomp.x87.effects"'
    return re.sub(r'^(define [^\n]+) \{$', lambda m: m[1] + attrs + ' {', text, flags=re.M)
