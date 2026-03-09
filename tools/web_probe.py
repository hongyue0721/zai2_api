from __future__ import annotations

"""Lightweight web endpoint probe for common HTTP(S) ports."""

import argparse
import json
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlparse

import httpx


COMMON_PORTS = [
    80,
    81,
    88,
    443,
    3000,
    3001,
    5000,
    5173,
    5174,
    5500,
    7001,
    7860,
    8000,
    8080,
    8081,
    8888,
    9000,
]


class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._in_title:
            self.title_chunks.append(data)

    @property
    def title(self) -> str:
        return " ".join(
            part.strip() for part in self.title_chunks if part.strip()
        ).strip()


@dataclass
class ProbeResult:
    host: str
    port: int
    scheme: str
    tcp_open: bool
    http_ok: bool
    status_code: int | None = None
    server: str = ""
    location: str = ""
    title: str = ""
    matched_features: list[str] | None = None
    error: str = ""


def _default_user_agent() -> str:
    return (
        "Mozilla/5.0 "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe a host for common web ports and rank likely HTTP endpoints."
    )
    parser.add_argument(
        "target",
        help="Host, URL, or IP to probe, for example 127.0.0.1 or https://example.com",
    )
    parser.add_argument(
        "--ports", help="Comma-separated ports or ranges, e.g. 80,443,3000-3010"
    )
    parser.add_argument("--path", default="/", help="HTTP path to request, default: /")
    parser.add_argument("--host-header", help="Optional Host header override")
    parser.add_argument(
        "--user-agent",
        default=_default_user_agent(),
        help="Request User-Agent header",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="Keyword expected in response title, headers, or body; repeatable",
    )
    parser.add_argument(
        "--timeout", type=float, default=2.0, help="Per-port timeout in seconds"
    )
    parser.add_argument("--workers", type=int, default=24, help="Concurrent workers")
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="Hide closed ports in text output",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Show only the top N ranked results in text output",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser.parse_args()


def normalize_target(raw: str) -> tuple[str, str | None]:
    if "://" in raw:
        parsed = urlparse(raw)
        return (parsed.hostname or raw, parsed.scheme or None)
    return raw, None


def parse_ports(raw: str | None) -> list[int]:
    if not raw:
        return COMMON_PORTS[:]
    ports: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            for port in range(min(start, end), max(start, end) + 1):
                if 1 <= port <= 65535:
                    ports.add(port)
        else:
            port = int(token)
            if 1 <= port <= 65535:
                ports.add(port)
    return sorted(ports)


def guess_schemes(port: int, explicit: str | None) -> list[str]:
    if explicit in {"http", "https"}:
        return [explicit]
    if port in {443, 8443, 9443}:
        return ["https", "http"]
    return ["http", "https"]


def check_tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def extract_title(text: str) -> str:
    parser = TitleParser()
    parser.feed(text[:20000])
    return parser.title


def feature_matches(
    text: str, title: str, headers: httpx.Headers, keywords: list[str]
) -> list[str]:
    haystack = "\n".join(
        [title, text[:5000], headers.get("server", ""), headers.get("location", "")]
    ).lower()
    matches: list[str] = []
    for keyword in keywords:
        if keyword.lower() in haystack:
            matches.append(keyword)
    return matches


def probe_http(
    host: str,
    port: int,
    scheme: str,
    path: str,
    host_header: str | None,
    user_agent: str,
    keywords: list[str],
    timeout: float,
) -> ProbeResult:
    url = f"{scheme}://{host}:{port}{path}"
    headers = {"User-Agent": user_agent, "Accept": "text/html,*/*"}
    if host_header:
        headers["Host"] = host_header
    try:
        verify = False if scheme == "https" else True
        with httpx.Client(
            timeout=timeout, verify=verify, follow_redirects=False
        ) as client:
            resp = client.get(url, headers=headers)
        text = (
            resp.text
            if "text" in resp.headers.get("content-type", "")
            or "html" in resp.headers.get("content-type", "")
            else ""
        )
        title = extract_title(text) if text else ""
        matches = feature_matches(text, title, resp.headers, keywords)
        return ProbeResult(
            host=host,
            port=port,
            scheme=scheme,
            tcp_open=True,
            http_ok=True,
            status_code=resp.status_code,
            server=resp.headers.get("server", ""),
            location=resp.headers.get("location", ""),
            title=title,
            matched_features=matches,
        )
    except Exception as exc:
        return ProbeResult(
            host=host,
            port=port,
            scheme=scheme,
            tcp_open=True,
            http_ok=False,
            error=str(exc),
            matched_features=[],
        )


def probe_port(
    host: str,
    port: int,
    explicit_scheme: str | None,
    path: str,
    host_header: str | None,
    user_agent: str,
    keywords: list[str],
    timeout: float,
) -> list[ProbeResult]:
    if not check_tcp_open(host, port, timeout):
        return [
            ProbeResult(
                host=host,
                port=port,
                scheme=explicit_scheme or "http",
                tcp_open=False,
                http_ok=False,
                matched_features=[],
                error="tcp closed",
            )
        ]
    results: list[ProbeResult] = []
    for scheme in guess_schemes(port, explicit_scheme):
        result = probe_http(
            host,
            port,
            scheme,
            path,
            host_header,
            user_agent,
            keywords,
            timeout,
        )
        results.append(result)
        if result.http_ok:
            break
    return results


def rank_results(results: Iterable[ProbeResult]) -> list[ProbeResult]:
    return sorted(
        results,
        key=lambda item: (
            0 if item.http_ok else 1,
            0 if item.matched_features else 1,
            abs(item.status_code - 200) if item.status_code is not None else 9999,
            item.port,
        ),
    )


def main() -> int:
    args = parse_args()
    host, explicit_scheme = normalize_target(args.target)
    ports = parse_ports(args.ports)
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    keywords = [item for item in args.keyword if item]

    all_results: list[ProbeResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                probe_port,
                host,
                port,
                explicit_scheme,
                path,
                args.host_header,
                args.user_agent,
                keywords,
                args.timeout,
            ): port
            for port in ports
        }
        for future in as_completed(futures):
            all_results.extend(future.result())

    ranked = rank_results(all_results)

    if args.json:
        print(
            json.dumps([asdict(item) for item in ranked], ensure_ascii=False, indent=2)
        )
        return 0

    visible = ranked
    if args.open_only:
        visible = [item for item in visible if item.tcp_open]
    if args.top and args.top > 0:
        visible = visible[: args.top]

    print(f"Target: {host}")
    print(f"Path:   {path}")
    if args.host_header:
        print(f"Host:   {args.host_header}")
    print()
    print("Candidates:")
    for item in visible:
        status = f"{item.status_code}" if item.status_code is not None else "-"
        features = ", ".join(item.matched_features or []) or "-"
        title = item.title or "-"
        server = item.server or "-"
        error = item.error or "-"
        print(
            f"  {item.scheme:5s} {item.port:5d} tcp={'open' if item.tcp_open else 'closed':6s} "
            f"http={'ok' if item.http_ok else 'fail':4s} status={status:3s} "
            f"server={server[:24]:24s} title={title[:40]:40s} match={features[:30]:30s} err={error[:40]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
