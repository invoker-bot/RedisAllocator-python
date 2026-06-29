"""Command-line entry point for allocator diagnostics."""

from __future__ import annotations

import argparse

from redis import Redis

from redis_allocator.diagnostics.collector import RedisAllocatorDiagnostics


def build_parser() -> argparse.ArgumentParser:
    """Build the diagnostics CLI parser."""
    parser = argparse.ArgumentParser(description="Inspect a live RedisAllocator instance.")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--suffix", default="allocator")
    parser.add_argument("--shared", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--scan-limit", type=int, default=1000)
    parser.add_argument("--json", action="store_true", help="Print one JSON snapshot and exit.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the diagnostics CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    redis_client = Redis.from_url(args.redis_url, decode_responses=True)
    diagnostics = RedisAllocatorDiagnostics(
        redis_client,
        prefix=args.prefix,
        suffix=args.suffix,
        shared=args.shared,
        sample_limit=args.sample_limit,
        scan_limit=args.scan_limit,
    )
    if args.json:
        print(diagnostics.snapshot_json(indent=2))
        return 0

    from redis_allocator.diagnostics.dashboard import serve_dashboard

    serve_dashboard(diagnostics, host=args.host, port=args.port, interval=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
