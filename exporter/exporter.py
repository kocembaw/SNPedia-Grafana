#!/usr/bin/env python3
"""SNPedia Knowledge Exporter.

A Prometheus exporter that collects statistics about the SNPedia wiki
through the MediaWiki Action API.

Data is fetched in a background thread at a fixed interval and cached in
memory. The /metrics endpoint serves only the cached values, so Prometheus
scrapes never generate requests to SNPedia.

Usage:
    python exporter.py --config config.yml          # run as a service
    python exporter.py --config config.yml --once   # fetch once, print metrics
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import yaml
from prometheus_client import generate_latest, start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

__version__ = "0.1.0"

LOG = logging.getLogger("snpedia_exporter")

MIN_INTERVAL_MINUTES = 5
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MAX_TITLES_PER_REQUEST = 50  # MediaWiki limit for anonymous clients
RECENT_CHANGES_WINDOW = timedelta(days=7)
RECENT_CHANGES_PAGE_LIMIT = 500  # MediaWiki limit for anonymous clients
RECENT_CHANGES_MAX_PAGES = 20
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
MEDIAWIKI_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    api_url: str
    user_agent: str
    categories: tuple[str, ...]
    interval_minutes: int = 30
    port: int = 9877
    listen_address: str = "0.0.0.0"
    timeout_seconds: float = 30.0
    min_request_interval_seconds: float = 2.0
    max_retries: int = 3
    maxlag: int = 5
    log_level: str = "INFO"


def _coerce(raw: dict[str, Any], key: str, cast: Callable[[Any], Any], default: Any) -> Any:
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"invalid value for {key!r}: {value!r}")
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value for {key!r}: {value!r}") from exc


def load_config(path: str) -> Config:
    """Load and validate the YAML configuration file."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path!r}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("the configuration file must contain a mapping")

    known_keys = set(Config.__dataclass_fields__)
    unknown_keys = sorted(set(raw) - known_keys)
    if unknown_keys:
        raise ConfigError(f"unknown configuration keys: {', '.join(unknown_keys)}")

    for key in ("api_url", "user_agent", "categories"):
        if not raw.get(key):
            raise ConfigError(f"missing required configuration key: {key!r}")

    categories = raw["categories"]
    if not isinstance(categories, list) or not all(
        isinstance(name, str) and name.strip() for name in categories
    ):
        raise ConfigError("'categories' must be a non-empty list of category names")
    # Remove duplicates while keeping the original order.
    unique_categories = tuple(dict.fromkeys(name.strip() for name in categories))

    config = Config(
        api_url=str(raw["api_url"]).strip(),
        user_agent=str(raw["user_agent"]).strip(),
        categories=unique_categories,
        interval_minutes=_coerce(raw, "interval_minutes", int, Config.interval_minutes),
        port=_coerce(raw, "port", int, Config.port),
        listen_address=str(raw.get("listen_address", Config.listen_address)),
        timeout_seconds=_coerce(raw, "timeout_seconds", float, Config.timeout_seconds),
        min_request_interval_seconds=_coerce(
            raw, "min_request_interval_seconds", float, Config.min_request_interval_seconds
        ),
        max_retries=_coerce(raw, "max_retries", int, Config.max_retries),
        maxlag=_coerce(raw, "maxlag", int, Config.maxlag),
        log_level=str(raw.get("log_level", Config.log_level)).upper(),
    )

    if not config.api_url.startswith(("http://", "https://")):
        raise ConfigError("'api_url' must start with http:// or https://")
    if config.interval_minutes < MIN_INTERVAL_MINUTES:
        raise ConfigError(
            f"'interval_minutes' must be at least {MIN_INTERVAL_MINUTES} "
            "to avoid putting unnecessary load on SNPedia"
        )
    if not 1 <= config.port <= 65535:
        raise ConfigError("'port' must be between 1 and 65535")
    if config.timeout_seconds <= 0:
        raise ConfigError("'timeout_seconds' must be greater than 0")
    if config.min_request_interval_seconds < MIN_REQUEST_INTERVAL_SECONDS:
        raise ConfigError(
            f"'min_request_interval_seconds' must be at least {MIN_REQUEST_INTERVAL_SECONDS:g}"
        )
    if config.max_retries < 0:
        raise ConfigError("'max_retries' must not be negative")
    if config.maxlag < 1:
        raise ConfigError("'maxlag' must be at least 1")
    if config.log_level not in logging.getLevelNamesMapping():
        raise ConfigError(f"unknown 'log_level': {config.log_level!r}")

    return config


# --------------------------------------------------------------------------
# MediaWiki API client
# --------------------------------------------------------------------------


class ApiError(Exception):
    """Raised when data cannot be fetched from the MediaWiki API."""


class ShutdownRequested(ApiError):
    """Raised when the exporter is stopping during a request cycle."""


class MediaWikiClient:
    """A polite MediaWiki API client: throttled, retrying, identifying itself."""

    def __init__(self, config: Config, stop_event: threading.Event) -> None:
        self._config = config
        self._stop = stop_event
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": config.user_agent, "Accept": "application/json"}
        )

    def query(self, **params: Any) -> dict[str, Any]:
        """Run an action=query request and return the decoded JSON response."""
        params = {"action": "query", "format": "json", "maxlag": self._config.maxlag, **params}
        attempts = self._config.max_retries + 1
        reason = "unknown error"

        for attempt in range(1, attempts + 1):
            self._throttle()
            retry_after: float | None = None

            try:
                response = self._session.get(
                    self._config.api_url, params=params, timeout=self._config.timeout_seconds
                )
            except requests.RequestException as exc:
                reason = f"request failed: {exc}"
            else:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                data, reason = self._check_response(response)
                if data is not None:
                    return data

            if attempt == attempts:
                break
            delay = (
                retry_after
                if retry_after is not None
                else BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            )
            delay = min(delay, BACKOFF_MAX_SECONDS)
            LOG.warning("%s, retrying in %.0f s (attempt %d/%d)", reason, delay, attempt, attempts)
            if self._stop.wait(delay):
                raise ShutdownRequested("shutdown requested")

        raise ApiError(f"giving up after {attempts} attempt(s): {reason}")

    def _check_response(self, response: requests.Response) -> tuple[dict[str, Any] | None, str]:
        """Return (data, "") on success or (None, reason) for errors worth retrying.

        Raises ApiError for errors that retrying will not fix.
        """
        if response.status_code == 429 or response.status_code >= 500:
            return None, f"HTTP {response.status_code}"
        if response.status_code != 200:
            raise ApiError(f"HTTP {response.status_code} from {self._config.api_url}")

        try:
            data = response.json()
        except ValueError:
            return None, "response is not valid JSON"
        if not isinstance(data, dict):
            return None, "response is not a JSON object"

        error = data.get("error")
        if error is None:
            if "warnings" in data:
                LOG.debug("API warnings: %s", data["warnings"])
            return data, ""
        if error.get("code") == "maxlag":
            return None, "SNPedia server is lagged (maxlag)"
        raise ApiError(f"API error {error.get('code')!r}: {error.get('info')}")

    def _throttle(self) -> None:
        wait = self._last_request + self._config.min_request_interval_seconds - time.monotonic()
        if wait > 0 and self._stop.wait(wait):
            raise ShutdownRequested("shutdown requested")
        self._last_request = time.monotonic()


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_timestamp(value: str) -> float:
    parsed = datetime.strptime(value, MEDIAWIKI_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _chunks(items: tuple[str, ...], size: int) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeStats:
    articles: int
    edits: int
    category_pages: dict[str, int]


@dataclass(frozen=True)
class RecentChanges:
    count_7d: int
    last_change_timestamp: float | None


def fetch_knowledge_stats(client: MediaWikiClient, categories: tuple[str, ...]) -> KnowledgeStats:
    """Fetch wiki statistics and category sizes.

    Site statistics (meta=siteinfo) and category sizes (prop=categoryinfo)
    are combined into a single request. More than 50 categories are split
    into several requests.
    """
    articles: int | None = None
    edits: int | None = None
    category_pages: dict[str, int] = {}

    for chunk in _chunks(categories, MAX_TITLES_PER_REQUEST):
        titles = {f"Category:{name}": name for name in chunk}
        params: dict[str, Any] = {"prop": "categoryinfo", "titles": "|".join(titles)}
        if articles is None:
            params.update(meta="siteinfo", siprop="statistics")

        query = client.query(**params).get("query") or {}

        statistics = query.get("statistics")
        if statistics:
            articles = int(statistics["articles"])
            edits = int(statistics["edits"])

        # MediaWiki normalizes titles, e.g. "Category:Is_a_snp" -> "Category:Is a snp".
        for item in query.get("normalized") or []:
            if item.get("from") in titles:
                titles[item["to"]] = titles.pop(item["from"])

        pages = query.get("pages") or {}
        if isinstance(pages, dict):  # formatversion=1 returns pages keyed by page ID
            pages = pages.values()

        for page in pages:
            name = titles.get(page.get("title"))
            if name is None:
                continue
            info = page.get("categoryinfo")
            if info is None:
                LOG.warning("Category %r does not exist on SNPedia or is empty, skipping it", name)
                continue
            category_pages[name] = int(info.get("pages", 0))

    if articles is None or edits is None:
        raise ApiError("site statistics are missing from the API response")

    return KnowledgeStats(articles=articles, edits=edits, category_pages=category_pages)


def fetch_recent_changes(client: MediaWikiClient, now: datetime | None = None) -> RecentChanges:
    """Count edits and new pages from the last 7 days and find the latest change."""
    now = now or datetime.now(timezone.utc)
    window_start = now - RECENT_CHANGES_WINDOW
    base_params: dict[str, Any] = {
        "list": "recentchanges",
        "rctype": "edit|new",
        "rcprop": "timestamp",
    }
    params = {
        **base_params,
        "rclimit": RECENT_CHANGES_PAGE_LIMIT,
        "rcend": window_start.strftime(MEDIAWIKI_TIMESTAMP_FORMAT),
    }

    count = 0
    last_change: float | None = None

    for _ in range(RECENT_CHANGES_MAX_PAGES):
        data = client.query(**params)
        changes = (data.get("query") or {}).get("recentchanges") or []
        if last_change is None and changes:
            # Results are sorted from newest to oldest.
            last_change = _parse_timestamp(changes[0]["timestamp"])
        count += len(changes)

        if "continue" in data:
            params.update(data["continue"])
        elif "query-continue" in data:  # older MediaWiki versions
            params.update(data["query-continue"].get("recentchanges", {}))
        else:
            break
    else:
        LOG.warning(
            "Recent changes span more than %d pages, the 7-day count is truncated",
            RECENT_CHANGES_MAX_PAGES,
        )

    if last_change is None:
        # No changes in the last 7 days: look up the most recent change at all.
        data = client.query(**base_params, rclimit=1)
        changes = (data.get("query") or {}).get("recentchanges") or []
        if changes:
            last_change = _parse_timestamp(changes[0]["timestamp"])

    return RecentChanges(count_7d=count, last_change_timestamp=last_change)


# --------------------------------------------------------------------------
# Prometheus collector
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    up: bool = False
    last_success_timestamp: float | None = None
    knowledge: KnowledgeStats | None = None
    recent: RecentChanges | None = None


class SNPediaCollector(Collector):
    """Exposes the most recent snapshot. Never calls the SNPedia API itself."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()

    @property
    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def publish(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def collect(self) -> Iterator[GaugeMetricFamily | CounterMetricFamily]:
        snapshot = self.snapshot

        yield GaugeMetricFamily(
            "snpedia_up",
            "Whether the last data fetch from SNPedia succeeded (1) or failed (0).",
            value=1 if snapshot.up else 0,
        )

        if snapshot.last_success_timestamp is not None:
            yield GaugeMetricFamily(
                "snpedia_last_success_timestamp_seconds",
                "Unix time of the last successful data fetch.",
                value=snapshot.last_success_timestamp,
            )

        if snapshot.knowledge is not None:
            yield GaugeMetricFamily(
                "snpedia_site_articles",
                "Number of articles in the wiki.",
                value=snapshot.knowledge.articles,
            )
            yield CounterMetricFamily(
                "snpedia_site_edits",
                "Total number of edits in the wiki's history.",
                value=snapshot.knowledge.edits,
            )
            category_pages = GaugeMetricFamily(
                "snpedia_category_pages",
                "Number of pages in a SNPedia category.",
                labels=["category"],
            )
            for name, pages in snapshot.knowledge.category_pages.items():
                category_pages.add_metric([name], pages)
            yield category_pages

        if snapshot.recent is not None:
            yield GaugeMetricFamily(
                "snpedia_recent_changes_7d",
                "Number of edits and new pages in the last 7 days.",
                value=snapshot.recent.count_7d,
            )
            if snapshot.recent.last_change_timestamp is not None:
                yield GaugeMetricFamily(
                    "snpedia_last_change_timestamp_seconds",
                    "Unix time of the most recent change in the wiki.",
                    value=snapshot.recent.last_change_timestamp,
                )


# --------------------------------------------------------------------------
# Collection loop
# --------------------------------------------------------------------------


def run_cycle(client: MediaWikiClient, config: Config, collector: SNPediaCollector) -> None:
    """Fetch all data once and publish a new snapshot.

    If a fetch fails, the previous values are kept so dashboards do not go
    blank, and snpedia_up is set to 0 to signal that the data is stale.
    """
    previous = collector.snapshot
    knowledge, recent = previous.knowledge, previous.recent
    succeeded = True
    started = time.monotonic()

    try:
        knowledge = fetch_knowledge_stats(client, config.categories)
    except ShutdownRequested:
        return
    except ApiError as exc:
        succeeded = False
        LOG.error("Failed to fetch site statistics and categories: %s", exc)
    except (KeyError, TypeError, ValueError):
        succeeded = False
        LOG.exception("Unexpected response format for site statistics and categories")

    try:
        recent = fetch_recent_changes(client)
    except ShutdownRequested:
        return
    except ApiError as exc:
        succeeded = False
        LOG.error("Failed to fetch recent changes: %s", exc)
    except (KeyError, TypeError, ValueError):
        succeeded = False
        LOG.exception("Unexpected response format for recent changes")

    collector.publish(
        Snapshot(
            up=succeeded,
            last_success_timestamp=time.time() if succeeded else previous.last_success_timestamp,
            knowledge=knowledge,
            recent=recent,
        )
    )

    if succeeded:
        LOG.info(
            "Data fetched in %.1f s: %d categories, %d changes in the last 7 days",
            time.monotonic() - started,
            len(knowledge.category_pages) if knowledge else 0,
            recent.count_7d if recent else 0,
        )


def collection_loop(
    client: MediaWikiClient,
    config: Config,
    collector: SNPediaCollector,
    stop_event: threading.Event,
) -> None:
    interval = config.interval_minutes * 60
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            run_cycle(client, config, collector)
        except Exception:  # keep the background thread alive no matter what
            LOG.exception("Unexpected error during data collection")
        stop_event.wait(max(0.0, started + interval - time.monotonic()))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for statistics about the SNPedia wiki."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("SNPEDIA_EXPORTER_CONFIG", "config.yml"),
        help="path to the YAML configuration file "
        "(default: $SNPEDIA_EXPORTER_CONFIG or config.yml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="fetch data once, print the metrics to stdout and exit",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2

    logging.getLogger().setLevel(config.log_level)
    if "example.com" in config.user_agent:
        LOG.warning("Set your own contact address in 'user_agent' before running against SNPedia")

    stop_event = threading.Event()
    client = MediaWikiClient(config, stop_event)
    collector = SNPediaCollector()
    REGISTRY.register(collector)

    if args.once:
        run_cycle(client, config, collector)
        sys.stdout.write(generate_latest(REGISTRY).decode("utf-8"))
        return 0 if collector.snapshot.up else 1

    def handle_signal(signum: int, _frame: Any) -> None:
        LOG.info("Received %s, shutting down", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    start_http_server(config.port, addr=config.listen_address)
    LOG.info(
        "SNPedia Knowledge Exporter %s serving metrics on http://%s:%d/metrics",
        __version__,
        config.listen_address,
        config.port,
    )

    worker = threading.Thread(
        target=collection_loop,
        args=(client, config, collector, stop_event),
        name="collector",
        daemon=True,
    )
    worker.start()

    while not stop_event.wait(1):
        pass
    worker.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
