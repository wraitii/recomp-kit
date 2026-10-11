#!/usr/bin/env python3
"""Offline x87 analysis: propagation, writers and real call contexts."""
import argparse
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("propagation", "writers", "contexts"))
    args = parser.parse_args(sys.argv[1:2])
    rest = sys.argv[2:]
    from x87_analysis import propagation, writers, contexts
    {"propagation": propagation, "writers": writers, "contexts": contexts}[args.mode].main(rest)


if __name__ == "__main__":
    main()
