"""Local wrapper benchmark. No authentication, network, or model calls.

Run from the repository root: python benchmarks/transport.py --runs 200
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    if args.runs < 1 or args.concurrency < 1:
        parser.error("runs and concurrency must be positive")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from theseus import CodexAppServer

    fake = Path(__file__).resolve().parents[1] / "tests/fake_app_server.py"
    with tempfile.TemporaryDirectory() as directory:
        with CodexAppServer(
            directory, server_command=(sys.executable, str(fake))
        ) as client:
            threads = [client.create_agent() for _ in range(args.concurrency)]
            client.run("Benchmark.", threads[0])
            samples = []
            for _ in range(args.runs):
                start = time.perf_counter()
                client.run("Benchmark.", threads[0])
                samples.append((time.perf_counter() - start) * 1000)

            async def parallel():
                for _ in range(args.runs):
                    await asyncio.gather(
                        *(client.run_async("Benchmark.", thread) for thread in threads)
                    )

            start = time.perf_counter()
            asyncio.run(parallel())
            elapsed = time.perf_counter() - start
            tracemalloc.start()
            for _ in range(args.runs):
                client.run("Benchmark.", threads[0])
            peak = tracemalloc.get_traced_memory()[1]
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0]
            tracemalloc.stop()
            print(
                json.dumps(
                    {
                        "runs": args.runs,
                        "concurrency": args.concurrency,
                        "sync_median_ms": round(statistics.median(samples), 3),
                        "sync_p95_ms": round(
                            sorted(samples)[int((len(samples) - 1) * 0.95)], 3
                        ),
                        "async_runs_per_second": round(
                            args.runs * args.concurrency / elapsed, 1
                        ),
                        "peak_traced_bytes": peak,
                        "retained_traced_bytes_after_gc": retained,
                        "scope": "local fake-server overhead only; raw-message API",
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
