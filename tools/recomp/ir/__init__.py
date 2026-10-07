"""Instruction IR for the translator: SLEIGH p-code lifting and analyses.

The existing emitter in translate.py turns decoded instructions directly into
C text. This package lifts the same instruction boundaries into raw p-code
(Ghidra's SLEIGH semantics, through pypcode; Ghidra itself is not needed) so
whole-function analyses work on one small, explicit operation set instead of
on emitted C. See docs/ir.md.
"""
