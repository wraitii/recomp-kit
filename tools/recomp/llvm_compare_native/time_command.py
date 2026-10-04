"""Record one CMake compile/pass invocation; never use a shell command string."""
import json
from pathlib import Path
import subprocess
import sys
import time

if __name__ == '__main__':
    record, *command = sys.argv[1:]
    start = time.perf_counter()
    result = subprocess.run(command)
    Path(record).write_text(json.dumps({'command': command, 'elapsed_seconds': time.perf_counter() - start,
                                       'returncode': result.returncode}, indent=2) + '\n')
    raise SystemExit(result.returncode)
