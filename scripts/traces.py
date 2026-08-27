"""Print a bounded Atlas turn-trace rollup."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker.traces import TraceRecorder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    args = parser.parse_args()
    if args.days < 1 or args.days > 3_650:
        parser.error("--days must be between 1 and 3650")

    recorder = TraceRecorder()
    try:
        summary = recorder.summary(days=args.days)
        enabled = recorder.enabled
    finally:
        recorder.close()

    rows = (
        ("enabled", str(enabled).lower()),
        ("turns", str(summary["turns"])),
        ("avg ms", f'{summary["avg_ms"]:.1f}'),
        ("tool calls", str(summary["tool_calls"])),
        ("input tokens", str(summary["input_tokens"])),
        ("output tokens", str(summary["output_tokens"])),
        ("cache read tokens", str(summary["cache_read_tokens"])),
        ("cache write tokens", str(summary["cache_write_tokens"])),
        ("cache hit ratio", f'{summary["cache_hit_ratio"]:.4f}'),
        ("cost usd", f'{summary["cost_usd"]:.6f}'),
    )
    width = max(len(label) for label, _value in rows)
    print(f'{"metric":<{width}}  value')
    print(f'{"-" * width}  -----')
    for label, value in rows:
        print(f"{label:<{width}}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
