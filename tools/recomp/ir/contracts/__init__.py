"""Conservative observable-contract analysis; never codegen authorization.

model defines immutable facts, dataflow composes them, and lifted adapts original
instruction CFGs. Runtime observer semantics must be supplied explicitly.
"""
