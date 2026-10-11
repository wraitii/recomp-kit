"""Compatibility facade for `x87_cw`, `x87_propagation` and `x87_report`."""
from .x87_cw import (
    CW_D3D8_PRESERVED, CW_D3D8_SETUP, CW_D3D8_SETUP_MASK,
    CW_D3D8_SETUP_VALUE, CW_EXCEPTION_MASK, CW_FAST, CW_MASK, CW_NEUTRAL,
    CW_NEUTRAL_USER_OPS, CW_PC, CW_PC_RC, CW_RC, CW_RESET, Dynamic, ENTRY, Fact,
    Guarded, InstructionProof, ModeCondition, Proven, ReturnSummary, UNKNOWN,
    UNKNOWN_SUMMARY, condition_of, fact_dict, instruction_effect,
    instruction_mnemonic, invalidate, join, overwrite, summary_dict,
    writer_effect,
)
from .x87_propagation import (
    Analysis, Prepared, analyze, direct_call_target, mode_preserving_leaf,
    ordinary_entries, prepare, propagate, summarize,
)
from .x87_report import exposure_report, mode_report
