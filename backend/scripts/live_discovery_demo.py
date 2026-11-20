"""Run the front half of lead discovery on REAL data and print what it finds (nothing is stored, nothing is sent).

    python scripts/live_discovery_demo.py --category dentist --lat 30.2672 --lon -97.7431 --limit 8

Pipeline: Geoapify finds businesses → identity rules dedupe them (ADR-028) → each company's own website is read through the
SSRF-guarded fetcher (robots.txt honoured, one request per second per host, at most 3 pages each) → published e-mail
addresses are extracted with their provenance → free checks (syntax, role, free-mail, disposable, MX lookup). The only vendor
credit spent is one Geoapify call. Keys come from ``.local/SECRETS.local.md`` into this process only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import live_stack  # noqa: E402  (reads .local/SECRETS.local.md; never prints it)
from pydantic import SecretStr  # noqa: E402

from app.core.clock import SYSTEM_CLOCK  # noqa: E402
from app.core.http import build_http_client  # noqa: E402
from app.domain.identity import (  # noqa: E402
    company_key,
    email_domain,
    is_disposable,
    is_free_mail,
    is_role_address,
)
from app.integrations.dns_checks import mail_status  # noqa: E402
from app.integrations.web.extract import FoundEmail, candidate_pages, extract_emails  # noqa: E402
from app.integrations.web.fetcher import FetchError, SafeFetcher, build_crawler_client  # noqa: E402
from app.providers.adapters.geoapify import GeoapifyProvider  # noqa: E402
from app.providers.discovery import CompanyHit, PlaceQuery  # noqa: E402

MAX_PAGES_PER_SITE = 3


async def read_site(
    fetcher: SafeFetcher, website: str
) -> tuple[list[FoundEmail], list[str], str | None]:
    """Home page, then the most promising contact/about pages. Returns (emails, pages read, why it stopped early)."""
    emails: dict[str, FoundEmail] = {}
    read: list[str] = []
    queue = [website]
    seen: set[str] = set()
    problem: str | None = None
    while queue and len(read) < MAX_PAGES_PER_SITE:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = await fetcher.fetch(url)
        except FetchError as exc:
            problem = problem or exc.reason + (" (unsafe)" if exc.unsafe else "")
            continue
        read.append(page.url)
        for found in extract_emails(page.text, page.url):
            emails.setdefault(found.address, found)
        if len(read) == 1:
            queue.extend(candidate_pages(page.text, page.url, limit=MAX_PAGES_PER_SITE - 1))
    return sorted(emails.values(), key=lambda e: (not e.same_site, e.address)), read, problem


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--category", default="dentist")
    parser.add_argument("--lat", type=float, default=30.2672)
    parser.add_argument("--lon", type=float, default=-97.7431)
    parser.add_argument("--radius", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    secrets = live_stack.load_secrets()
    live_stack.need(secrets, "GEOAPIFY_API_KEY")
    api = build_http_client()
    geoapify = GeoapifyProvider(
        api_key=SecretStr(secrets["GEOAPIFY_API_KEY"]), client=api, clock=SYSTEM_CLOCK
    )
    crawler = build_crawler_client()
    fetcher = SafeFetcher(client=crawler)

    print(f"Searching for '{args.category}' within {args.radius} m of ({args.lat}, {args.lon}) ...")
    hits = (
        await geoapify.search_places(
            PlaceQuery((args.category,), args.lat, args.lon, radius_m=args.radius, limit=args.limit)
        )
    ).value
    print(f"  {len(hits)} businesses returned\n")

    companies: dict[str, CompanyHit] = {}
    duplicates = 0
    for hit in hits:
        key = company_key(
            website=hit.website, name=hit.name, city=hit.city, country=hit.country_code
        )
        if key is None:
            continue
        if key in companies:
            duplicates += 1
            print(f"  duplicate collapsed: {hit.name!r} -> {key}")
            continue
        companies[key] = hit

    outcomes: Counter[str] = Counter()
    for key, hit in companies.items():
        print(f"== {hit.name}  [{key}]")
        print(
            f"   {hit.address or hit.city or ''}  |  phone: {hit.phone or '-'}  |  site: {hit.website or '(none listed)'}"
        )
        candidates: dict[str, tuple[str, str]] = {}  # address -> (where from, detail)
        if hit.email:
            candidates[hit.email] = ("published in OpenStreetMap via Geoapify", "")
        if hit.website:
            emails, pages, problem = await read_site(fetcher, hit.website)
            print(
                f"   read {len(pages)} page(s) of their site"
                + (f"; stopped: {problem}" if problem else "")
            )
            for found in emails:
                candidates.setdefault(
                    found.address,
                    (
                        f"{found.method} on {found.page_url}",
                        "own-domain" if found.same_site else "other-domain",
                    ),
                )
        if not candidates:
            print("   no published e-mail found")
            outcomes["no email"] += 1
            continue
        for address, (origin, note) in candidates.items():
            status = await mail_status(email_domain(address) or "")
            traits = [
                t
                for t, on in (
                    ("role", is_role_address(address)),
                    ("free-mail", is_free_mail(address)),
                    ("disposable", is_disposable(address)),
                )
                if on
            ]
            print(
                f"   - {address:40s} mail:{status:14s} {','.join(traits) or 'named/other':12s} {note:12s} <- {origin}"
            )
            outcomes[
                "usable domain" if status in ("mx", "implicit_mx") else f"rejected free: {status}"
            ] += 1
    print(
        f"\nSummary: {len(companies)} companies ({duplicates} duplicates collapsed); address checks: {dict(outcomes)}"
    )
    print("Nothing was stored and nothing was sent.")
    await crawler.aclose()
    await api.aclose()


if __name__ == "__main__":
    asyncio.run(main())
