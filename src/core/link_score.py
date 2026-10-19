"""Link Score: Screaming Frog's internal PageRank, from the crawl's own links.

Follows SF's documented method: the n scored pages start at 1/n, then 10
rounds of ((1 - D) / n) + D * (score passed by inlinks), with D = 0.85. Only
unique links count and self-links are ignored. A nofollow link takes its share
of the page's score but passes none of it. Links to a URL that redirects or
canonicalises elsewhere pass straight to the target, and that URL gets no
score of its own. Results map onto a 1-100 log scale, top page = 100.

SF leaves some cases undocumented. Here only internal HTML pages that loaded
are scored; links to other internal URLs (errors, files) take a share that
evaporates, as nofollow links do; a page-level nofollow makes all of a page's
links nofollow; external and image links are left out.
"""
import math

from src.core.issue_detector import canonical_elsewhere, is_internal_html_page, page_robots_directives
from src.core.link_manager import normalize_url

DAMPING = 0.85
ITERATIONS = 10


def link_scores(results, links):
    """Map result URLs to their Link Score (1-100)"""
    pages = [result for result in results if is_internal_html_page(result)]

    # Where a link to each URL ends up: its redirect target, then its canonical
    alias = {page['url']: canonical_elsewhere(page) or normalize_url(page.get('redirected_to') or page['url'])
             for page in pages}

    def resolve(url):
        seen = {url}
        while alias.get(url, url) not in seen:
            url = alias[url]
            seen.add(url)
        return url

    # Rows that hold a score: the page itself, or for a redirect, the page it lands on
    node_of = {page['url']: resolve(page['url']) for page in pages if canonical_elsewhere(page) is None}
    nodes = set(node_of.values())
    if not nodes:
        return {}
    page_nofollow = {page['url'] for page in pages if 'nofollow' in page_robots_directives(page)}

    outlinks = {node: {} for node in nodes}  # page -> {target: passes score}
    for link in links:
        source = node_of.get(link['source_url'])
        if source is None or not link.get('is_internal') or link.get('placement') == 'image':
            continue
        target = resolve(link['target_url'])
        if target != source:
            follows = not link.get('nofollow') and link['source_url'] not in page_nofollow
            outlinks[source][target] = outlinks[source].get(target, False) or follows

    n = len(nodes)
    score = dict.fromkeys(nodes, 1 / n)
    for _ in range(ITERATIONS):
        passed = dict.fromkeys(nodes, 0.0)
        for source, targets in outlinks.items():
            for target, follows in targets.items():
                if follows and target in passed:
                    passed[target] += score[source] / len(targets)
        score = {node: (1 - DAMPING) / n + DAMPING * passed[node] for node in nodes}

    low, high = math.log(min(score.values())), math.log(max(score.values()))
    scaled = {node: 100 if math.isclose(low, high) else round(1 + 99 * (math.log(value) - low) / (high - low))
              for node, value in score.items()}

    # A redirect's score shows on the page it lands on, unless that page has no row of its own
    page_urls = {page['url'] for page in pages}
    return {url: scaled[node] for url, node in node_of.items() if url == node or node not in page_urls}
