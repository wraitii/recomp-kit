"""Activation keeps numeric semantics, caller routing and fallback explicit."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.x87_llvm.direct import boundary_ir, direct_ir
from experiments.x87_llvm.function import emit_function
from llvm_runtime import wrappers
import translate as T


def test_boundary_abi_retains_operations_without_effect_contract():
    insns = T.parse_listing_text('00100000  FLD float ptr [ESI]\n00100002  FSTP float ptr [EDI]\n00100004  RET')
    source = direct_ir(emit_function('test', insns, True, True), effects=True)
    result = boundary_ir(source)
    assert '"recomp.x87.boundaries"' in result and '"recomp.x87.sync"' in result
    assert '"recomp.x87.region"' in result
    assert '"recomp.x87.effects"' not in result and '@rk_direct_' not in result
    assert '@rk_boundary_load(' in result and '@rk_boundary_store(' in result
    assert '@rk_boundary_ret(' in result
    assert boundary_ir(result) == result
    assert result.replace('@rk_boundary_', '@rk_direct_').replace(
        '"recomp.x87.boundaries"', '"recomp.x87.direct"') == source.replace(' "recomp.x87.effects"', '')


def test_dispatch_fallback_does_not_capture_its_own_redirect():
    header, source = wrappers({'00400100': {}}, {'00400200': {}}, Path('/game/native.h'))
    assert '#include "/game/native.h"' in source
    assert 'activation.h' not in source
    assert '#define FN_00400100 recomp_llvm_dispatch_00400100' in header
    assert '#define FN_00400200 recomp_llvm_dispatch_00400200' in header
    assert 'FN_00400100(c); return;' in source
    assert 'FN_00400200(c); return;' in source
    assert 'recomp_llvm_00400100(c);' in source
    assert 'fn_00400200(c);' in source  # enabled callers run retained C
    assert 'RECOMP_LLVM_ORIGINAL' in source and 'RECOMP_LLVM_STATS' in source


def test_caller_and_llvm_sets_are_disjoint():
    with pytest.raises(ValueError, match='disjoint'):
        wrappers({'00400100': {}}, {'00400100': {}}, None)
