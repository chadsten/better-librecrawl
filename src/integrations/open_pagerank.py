"""Open PageRank lookup for the crawled site's domain.

API reference: https://keywordseverywhere.com/openpagerank/docs
"""
import requests

# Tests point this at a local fake server
BASE_URL = 'https://openpagerank.keywordseverywhere.com'

# Seconds. The lookup runs at the end of a crawl and must not hold it up.
TIMEOUT = 10


def lookup_domain(host, api_key):
    """The Open PageRank of host's registered domain, for the crawl stats.

    The API reduces a hostname (www.example.com, blog.example.com) to its
    registered domain itself. Returns {domain, open_page_rank, rank, as_of},
    where open_page_rank and rank are None for a domain the API does not know,
    or {error} with a short message for the UI. Never raises.
    """
    try:
        response = requests.post(
            f'{BASE_URL}/v1/domains/bulk',
            headers={'Authorization': f'Bearer {api_key}'},
            json={'domains': [host], 'include_history': False},
            timeout=TIMEOUT)
        if response.status_code != 200:
            print(f"Open PageRank lookup failed: HTTP {response.status_code} {response.text[:200]}")
            return {'error': f'API error {response.status_code}'}

        data = response.json()
        # A host the API cannot reduce to a registered domain (an IP address,
        # localhost) is listed under 'invalid' instead of in the results
        if not data['results']:
            return {'error': 'Not a public domain'}

        row = data['results'][0]
        return {
            'domain': row['domain'],
            'open_page_rank': row['open_page_rank'],
            'rank': row['rank'],
            'as_of': data['as_of'],
        }
    except Exception as e:
        print(f"Open PageRank lookup failed: {e}")
        return {'error': 'Timed out' if isinstance(e, requests.exceptions.Timeout) else 'Lookup failed'}
