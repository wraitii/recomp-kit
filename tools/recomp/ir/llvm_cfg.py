"""SSA names for finalized LLVM control-flow paths and clone side exits."""
from collections import defaultdict, deque
from dataclasses import dataclass, field
import re

from .ssa import SSAError


@dataclass
class Block:
    name: str
    lines: list = field(default_factory=list)
    writes: dict = field(default_factory=dict)
    predecessors: list = field(default_factory=list)


class Graph:
    def __init__(self):
        self.blocks = {}
        self.current = None
        self.entries = {}
        self.pending = deque()
        self.widths = {}
        self.serial = 0

    def block(self, name):
        if name in self.blocks:
            raise SSAError('duplicate LLVM block ' + name)
        self.current = self.blocks[name] = Block(name)

    def line(self, text):
        self.current.lines.append(text)

    def read(self, value):
        self.widths[value.id] = value.size * 8
        if value.id in self.current.writes:
            return self.current.writes[value.id]
        return self.entry(self.current.name, value.id)

    def write(self, value, operand):
        self.widths[value.id] = value.size * 8
        self.current.writes[value.id] = operand

    def entry(self, block, value):
        key = block, value
        if key not in self.entries:
            self.entries[key] = '%%q%d' % self.serial
            self.serial += 1
            self.pending.append(key)
        return self.entries[key]

    def render(self):
        successors = {}
        for name, block in self.blocks.items():
            if not block.lines:
                raise SSAError('empty LLVM block ' + name)
            terminator = block.lines[-1]
            if not terminator.startswith(('br ', 'switch ', 'ret ', 'unreachable')):
                raise SSAError('unterminated LLVM block ' + name)
            targets = list(dict.fromkeys(re.findall(r'label %([A-Za-z0-9_]+)', terminator)))
            successors[name] = targets
            for target in targets:
                if target not in self.blocks:
                    raise SSAError('missing LLVM block ' + target)
                self.blocks[target].predecessors.append(name)
        reachable, pending = set(), ['start']
        while pending:
            name = pending.pop()
            if name not in reachable:
                reachable.add(name)
                pending.extend(successors[name])
        phis = {}
        while self.pending:
            block_name, value = self.pending.popleft()
            if block_name not in reachable:
                continue
            block = self.blocks[block_name]
            predecessors = [name for name in block.predecessors if name in reachable]
            if not predecessors:
                raise SSAError('LLVM value %d is undefined at %s' % (value, block_name))
            incoming = []
            for name in predecessors:
                source = self.blocks[name]
                operand = source.writes[value] if value in source.writes else self.entry(name, value)
                incoming.append((operand, name))
            phis[self.entries[block_name, value]] = (block_name, self.widths[value], incoming)
        aliases, users = {}, defaultdict(set)
        for name, (_, _, incoming) in phis.items():
            for operand, _ in incoming:
                users[operand].add(name)

        def resolve(operand):
            path = []
            while operand in aliases:
                path.append(operand)
                operand = aliases[operand]
            for name in path:
                aliases[name] = operand
            return operand

        work = deque(phis)
        while work:
            name = work.popleft()
            if name in aliases:
                continue
            incoming = {resolve(operand) for operand, _ in phis[name][2]}
            incoming.discard(name)
            if len(incoming) == 1:
                replacement = incoming.pop()
                aliases[name] = replacement
                affected = users[name]
                users[replacement].update(affected)
                work.extend(affected)
        heads = defaultdict(list)
        for name, (block, width, incoming) in phis.items():
            if name not in aliases:
                args = ', '.join('[ %s, %%%s ]' % (resolve(operand), predecessor)
                                 for operand, predecessor in incoming)
                heads[block].append('%s = phi i%d %s' % (name, width, args))
        lines = []
        for name, block in self.blocks.items():
            if name not in reachable:
                continue
            lines.append(name + ':')
            for line in (*heads[name], *block.lines):
                lines.append('  ' + re.sub(r'%q\d+\b', lambda match: resolve(match.group()), line))
        return lines
