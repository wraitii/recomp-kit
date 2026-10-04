"""Route baseline-emitted accesses through the same opaque ABI as LLVM lowering."""
import re


def instrument_memory(text):
    # Preserve expression evaluation: e.g. fto_float runs before the store
    # adapter, while fdrop remains afterwards. Only this experiment uses it.
    adapters = {'rdf32': 'f32', 'rdf64': 'f64', 'rd32': 'u32', 'wrf32': 'store32'}
    for original, adapter in adapters.items():
        text = re.sub(r'\b' + original + r'\(', f'rk_access_{adapter}(c, ', text)
    if re.search(r'\b(?:rd|wr)[a-z0-9_]*\(', text):
        raise ValueError('unsupported baseline memory accessor at observation boundary')
    return text
