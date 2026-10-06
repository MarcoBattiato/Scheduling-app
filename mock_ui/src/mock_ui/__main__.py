"""Start the mock UI: `python -m mock_ui`.

A single entry point so nobody has to remember a uvicorn incantation, and so
the VS Code launch configuration has something file-independent to point at.
"""
from __future__ import annotations

import argparse
import socket

import uvicorn


def lan_address() -> str | None:
    """This machine's address on the local network, or None if it has none.

    Read from the interfaces rather than from the routing table: with a VPN
    up, the default route goes through the tunnel, and its address is one a
    phone on the same Wi-Fi cannot reach. Wired and wireless interfaces
    (en*, eth*, wlan*) are preferred; tunnels and loopback never qualify.
    """
    import re
    import subprocess

    try:
        listing = subprocess.run(["ifconfig"], capture_output=True, text=True,
                                 timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ""
    found, interface = [], None
    for line in listing.splitlines():
        header = re.match(r"^(\S+?):? ", line)
        if header:
            interface = header.group(1)
        address = re.search(r"\binet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", line)
        if address and interface and not interface.startswith(("lo", "utun", "tun", "tap", "wg", "ppp")):
            found.append((interface, address.group(1)))
    for interface, address in found:
        if interface.startswith(("en", "eth", "wlan", "wl")) and not address.startswith("169.254."):
            return address
    if found:
        return found[0][1]

    try:      # no ifconfig: fall back to asking which way traffic would go
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


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
    # 0.0.0.0 is where to listen, not an address anyone can open. Printing it
    # as a link sent people to a URL that works nowhere.
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    lines = [f"\n  Mock UI on http://{shown}:{args.port}", "  Open a tab per person:"]
    lines += [f"    http://{shown}:{args.port}/?as={who}"
              for who in ("provider", "alice", "bob", "carol")]
    if args.host in ("0.0.0.0", ""):
        lan = lan_address()
        if lan:
            lines += ["", "  On a phone on the same Wi-Fi:",
                      f"    http://{lan}:{args.port}/phone?as=provider",
                      f"    http://{lan}:{args.port}/phone?as=carol",
                      "  No login: anyone on this network can open it."]
    else:
        lines += ["", "  For a phone, restart with --host 0.0.0.0"]
    print("\n".join(lines) + "\n  Ctrl-C to stop.\n", flush=True)

    uvicorn.run("mock_ui.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
