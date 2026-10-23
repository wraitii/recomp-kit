#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import tomllib


def analyze(addresses, shift):
    base = addresses[0] >> shift << shift
    used = {}
    collisions = set()
    displaced = 0
    total = 0
    maximum = 0
    for address in addresses:
        home = (address - base) >> shift
        slot = home
        path = []
        while slot in used:
            path.append(slot)
            slot = used[slot]
        for previous in path:
            used[previous] = slot
        if slot != home:
            collisions.add(home)
            displaced += 1
        used[slot] = slot + 1
        total += slot - home
        maximum = max(maximum, slot - home)
    span = ((addresses[-1] - base) >> shift) + 1
    return {
        'shift': shift, 'entries': len(addresses), 'collision_buckets': len(collisions),
        'displaced_entries': displaced, 'max_probe': maximum,
        'mean_hit_probes': 1 + total / len(addresses),
        'table_bytes': (span + maximum) * 16,
    }


def main():
    parser = argparse.ArgumentParser(description='Analyze shifted dispatch-table placement from generated symbols.')
    parser.add_argument('symbols', type=Path)
    parser.add_argument('--shifts', nargs='+', type=int, default=[0, 1, 2, 3, 4])
    parser.add_argument('--game', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if any(not 0 <= shift <= 8 for shift in args.shifts):
        parser.error('shifts must be between zero and eight')
    addresses = sorted({int(row['addr'], 16) for row in json.loads(args.symbols.read_text())['functions']})
    if not addresses:
        parser.error('symbols contain no entries')
    results = [analyze(addresses, shift) for shift in args.shifts]
    config = tomllib.loads(args.game.read_text()).get('translate', {}).get('dispatch') if args.game else None
    if args.json:
        print(json.dumps({'configured': config, 'layouts': results}, indent=2))
        return
    if config:
        print(f"Configured: shift={config['shift']}, max_probe={config['max_probe']}")
    print('shift  collision buckets  displaced  max probe  mean probes  table MiB')
    for row in results:
        print(f"{row['shift']:5}  {row['collision_buckets']:17,}  {row['displaced_entries']:9,}"
              f"  {row['max_probe']:9,}  {row['mean_hit_probes']:11.6f}  {row['table_bytes'] / 2**20:9.3f}")


if __name__ == '__main__':
    main()
