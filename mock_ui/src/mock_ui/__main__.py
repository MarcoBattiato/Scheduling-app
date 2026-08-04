"""Start the mock UI: `python -m mock_ui`.

A single entry point so nobody has to remember a uvicorn incantation, and so
the VS Code launch configuration has something file-independent to point at.
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the scheduling mock UI.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--reload", action="store_true", help="restart when the source changes"
    )
    args = parser.parse_args()

    # flush: stdout is block-buffered when this is not a terminal, and the
    # banner is the whole point of printing it.
    lines = [f"\n  Mock UI on http://{args.host}:{args.port}", "  Open a tab per person:"]
    lines += [f"    http://{args.host}:{args.port}/?as={who}"
              for who in ("provider", "alice", "bob", "carol")]
    print("\n".join(lines) + "\n  Ctrl-C to stop.\n", flush=True)

    uvicorn.run("mock_ui.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
