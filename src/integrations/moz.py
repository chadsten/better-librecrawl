"""Moz URL metrics for crawled pages.

Uses one method of the Moz API (JSON-RPC 2.0, token in an x-moz-token header),
data.site.metrics.fetch.multiple. It returns every metric Moz has for a page
and costs one row of quota per page returned, so all of METRICS come for the
price of one row.
https://moz.com/api/docs/methods/DataSiteMetricsFetchMultipleAction
https://moz.com/api/docs/guides/error-handling
"""
import time
import uuid

import requests

API_URL = 'https://api.moz.com/jsonrpc'
METHOD = 'data.site.metrics.fetch.multiple'
BATCH_SIZE = 50  # site_queries takes at most 50 entries
TIMEOUT = 30
MAX_ATTEMPTS = 3  # per batch, for rate limits, server errors and network failures
MAX_RETRY_WAIT = 60  # seconds, so a long Retry-After can't stall the end of the crawl

# Moz site_metrics field -> (crawl result and database column, column type)
METRICS = {
    'page_authority': ('moz_page_authority', 'INTEGER'),
    'domain_authority': ('moz_domain_authority', 'INTEGER'),
    'spam_score': ('moz_spam_score', 'INTEGER'),  # -1 when Moz has no score for the page
    'root_domains_to_page': ('moz_linking_domains', 'INTEGER'),
    'external_pages_to_page': ('moz_inbound_links', 'INTEGER'),
}
FIELDS = tuple(field for field, _ in METRICS.values())

# The per-method rate limit is the one error that says how long to wait; the
# edge rate limit and server errors clear by themselves
RATE_LIMITED = -32658
RETRYABLE = {RATE_LIMITED, -32650, -32602, -32603}


class MozError(Exception):
    """A request that failed for good, with a message short enough to show the user"""


def fetch_url_metrics(token, urls):
    """Yield {url: {field: value}} per batch of urls, for the pages Moz returned.

    Requests go one at a time. One that still fails after its retries raises
    MozError and nothing more is sent; batches already yielded stand.
    """
    session = requests.Session()
    session.headers['x-moz-token'] = token
    for start in range(0, len(urls), BATCH_SIZE):
        batch = urls[start:start + BATCH_SIZE]
        result = _call(session, {'site_queries': [{'query': url, 'scope': 'url'} for url in batch]})

        by_key = {}
        for url in batch:
            by_key.setdefault(_match_key(url), []).append(url)
        found = {}
        for entry in result.get('results_by_site', []):
            metrics = entry['site_metrics']
            values = {field: metrics.get(name) for name, (field, _) in METRICS.items()}
            for url in _requested_urls(entry['site_query'], by_key):
                found[url] = values

        if len(found) < len(batch):
            print(f"Moz returned no metrics for {len(batch) - len(found)} of {len(batch)} URLs")
        yield found


def _call(session, data):
    """POST one request, retrying rate limits, server errors and network failures"""
    body = {'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': METHOD, 'params': {'data': data}}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = 2 ** (attempt - 1)
        try:
            response = session.post(API_URL, json=body, timeout=TIMEOUT)
            payload = response.json()
        except requests.JSONDecodeError:  # not JSON, e.g. a proxy's error page
            message = f'Moz API returned HTTP {response.status_code}'
        except requests.RequestException as e:
            print(f"Moz request error: {e}")
            message = f'Moz API unreachable ({type(e).__name__})'
        else:
            if 'result' in payload:
                return payload['result']
            error = payload.get('error', {})
            message = error.get('message') or f'Moz API returned HTTP {response.status_code}'
            if error.get('code') not in RETRYABLE:
                raise MozError(message)
            if error.get('code') == RATE_LIMITED:
                wait = error.get('data', {}).get('value', wait)
        if attempt < MAX_ATTEMPTS:
            wait = min(wait, MAX_RETRY_WAIT)
            print(f"Moz request failed ({message}), retrying in {wait}s")
            time.sleep(wait)
    raise MozError(message)


def _requested_urls(site_query, by_key):
    """The requested URLs a result is for. Results don't come back in request
    order and the query is echoed canonicalised (the docs show the scheme and a
    root URL's slash dropped), so it is matched loosely, an exact match first."""
    for echoed in (site_query.get('query'), site_query.get('original_site_query', {}).get('query')):
        urls = by_key.get(_match_key(echoed or ''))
        if urls:
            return [url for url in urls if url == echoed] or urls
    return []


def _match_key(url):
    """url without its scheme, www. or trailing slash, host lower-cased"""
    host, _, path = url.split('://', 1)[-1].partition('/')
    return f"{host.lower().removeprefix('www.')}/{path}".rstrip('/')
