import os
import sys
import json
import gzip
import logging

from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import ParselCrawler

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://www.transfermarkt.co.uk'


def _throttle_settings():
    """Politeness throttle for the wide-tail crawl (soccer-analysis step_2c §5.6).

    Transfermarkt is scraped with *no proxy*, so the run leans on a conservative,
    self-throttled request rate to avoid 429s/blocks. This fork is Crawlee-based
    (the plan was written against the older Scrapy version), so the Scrapy
    AUTOTHROTTLE / DOWNLOAD_DELAY knobs it references map to Crawlee
    ``ConcurrencySettings`` here: concurrency pinned to 1 and the request rate
    capped. Both are env-tunable so the rate can be calibrated against the observed
    block rate without code edits:

        TFMKT_MAX_CONCURRENCY        (default 1)
        TFMKT_MAX_TASKS_PER_MINUTE   (default 40  -> ~1 request / 2 s)
    """
    max_concurrency = int(os.environ.get('TFMKT_MAX_CONCURRENCY', '1'))
    max_tasks_per_minute = float(os.environ.get('TFMKT_MAX_TASKS_PER_MINUTE', '40'))
    return ConcurrencySettings(
        min_concurrency=1,
        max_concurrency=max_concurrency,
        desired_concurrency=max_concurrency,
        max_tasks_per_minute=max_tasks_per_minute,
    )


def create_crawler():
    """Create a ParselCrawler that tracks failed requests.

    Returns a (crawler, failures) tuple. After crawler.run(), call
    check_failures(failures) to exit with non-zero status if any requests failed.
    The crawler is throttled per :func:`_throttle_settings` (step_2c §5.6).
    """
    failures = []
    crawler = ParselCrawler(concurrency_settings=_throttle_settings())

    @crawler.failed_request_handler
    async def on_failed_request(context, error):
        failures.append((context.request.url, error))

    return crawler, failures


def check_failures(failures):
    """Exit with status 1 if there were any failed requests."""
    if failures:
        for url, error in failures:
            logger.error("Failed to scrape %s: %s", url, error)
        sys.exit(1)


def safe_strip(word):
    if word:
        return word.strip()
    return word


def read_lines(file_name, reading_fn):
    with reading_fn(file_name) as f:
        lines = f.readlines()
        return [json.loads(line) for line in lines]


def load_parents(parents_arg=None):
    if parents_arg is not None:
        extension = parents_arg.split(".")[-1]
        if extension == "gz":
            parents = read_lines(parents_arg, gzip.open)
        else:
            parents = read_lines(parents_arg, open)
    elif not sys.stdin.isatty():
        parents = [json.loads(line) for line in sys.stdin]
    else:
        return []

    # 2nd level parents are redundant
    for parent in parents:
        if parent.get('parent') is not None:
            del parent['parent']

    return parents


def seasonize_href(item, season, base_url):
    if item['type'] in ('club', 'national_team'):
        return f"{base_url}{item['href']}/saison_id/{season}"
    elif item['type'] == 'country':
        return f"{base_url}{item['href']}"
    elif item['type'] == 'competition':
        if item['competition_type'] == 'first_tier':
            return f"{base_url}{item['href']}/plus/0?saison_id={season}"
        elif item['competition_type'] in ['domestic_cup', 'domestic_super_cup']:
            return f"{base_url}{item['href']}?saison_id={season}".replace("wettbewerb", "pokalwettbewerb")
        else:
            return f"{base_url}{item['href']}?saison_id={season}"
    else:
        return f"{base_url}{item['href']}"


def build_initial_requests(parents, season, base_url, label, spider_name):
    requests = []
    for item in parents:
        # Clubs are scraped for league competitions of every tier: the clubs table
        # is present on the season startseite for 2nd/3rd/4th-tier and youth leagues
        # too, not just first_tier (verified empirically). Competitions that
        # genuinely have no clubs table (placement stages, etc.) yield nothing and
        # are handled gracefully by the clubs parser.
        seasoned_href = seasonize_href(item, season, base_url)
        item['seasoned_href'] = seasoned_href
        requests.append(
            Request.from_url(
                url=seasoned_href,
                label=label,
                user_data={'parent': item},
            )
        )
    return requests
