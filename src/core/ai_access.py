"""AI Access: which AI crawlers a site's robots.txt lets in.

Bots are grouped by what blocking them costs. A search bot builds the index
an assistant answers from, so blocking it drops the site from those answers.
A live fetcher reads a page while answering a user. A training bot only
gathers model training data: blocking it is a business choice and leaves the
site in AI answers. Sources: each vendor's crawler docs, as of Oct 2026.
"""

from src.core.ai_search import AI_SEARCH_CHECKS

# Grouped by kind; check is the AI_SEARCH_CHECKS entry raised for a page blocked to a bot of that kind
BOT_KINDS = {
    'search': {'label': 'Search', 'check': 'blocked_search'},
    'live': {'label': 'Live fetch', 'check': 'blocked_live'},
    'training': {'label': 'Training', 'check': None},
}

# robots.txt token -> who uses it, its kind, and anything to know about it
AI_BOTS = {
    'OAI-SearchBot': {'used_by': 'ChatGPT search', 'kind': 'search'},
    'ChatGPT-User': {'used_by': 'ChatGPT, reading a page for a user', 'kind': 'live',
                     'note': 'OpenAI says robots.txt may not apply to it'},
    'GPTBot': {'used_by': 'OpenAI model training', 'kind': 'training'},
    'Claude-SearchBot': {'used_by': 'Claude search', 'kind': 'search'},
    'Claude-User': {'used_by': 'Claude, reading a page for a user', 'kind': 'live'},
    'ClaudeBot': {'used_by': 'Anthropic model training', 'kind': 'training'},
    'PerplexityBot': {'used_by': 'Perplexity search', 'kind': 'search'},
    'Perplexity-User': {'used_by': 'Perplexity, reading a page for a user', 'kind': 'live',
                        'note': 'Perplexity says it generally ignores robots.txt'},
    'Googlebot': {'used_by': 'Google AI Overviews and AI Mode', 'kind': 'search'},
    'Google-Extended': {'used_by': 'Gemini training and Gemini app answers', 'kind': 'training',
                        'note': 'Does not affect Google Search or AI Overviews'},
    'Bingbot': {'used_by': 'Microsoft Copilot', 'kind': 'search'},
    'Applebot': {'used_by': 'Siri and Spotlight', 'kind': 'search'},
    'Applebot-Extended': {'used_by': 'Apple model training', 'kind': 'training'},
    'CCBot': {'used_by': 'Common Crawl, a source of many training sets', 'kind': 'training'},
    'meta-externalagent': {'used_by': 'Meta model training', 'kind': 'training'},
    'Bytespider': {'used_by': 'ByteDance model training', 'kind': 'training'},
    'Amazonbot': {'used_by': 'Amazon model training', 'kind': 'training'},
}

CLOUDFLARE_MANAGED_MARKER = '# begin cloudflare managed content'


def robots_unavailable(status):
    """A robots.txt Google treats as a full block: rate limited, a server error or no response"""
    return status is None or status == 429 or status >= 500


def ai_robots_access(pages, robots_for, site_url):
    """Which AI bots robots.txt keeps off each of `pages` (internal HTML pages).

    robots_for(url) gives the robots.txt record for url's host. Returns
    (summary for the AI Search tab, {bot token: set of blocked page URLs}).
    """
    blocked = {token: set() for token in AI_BOTS}
    for page in pages:
        rules = robots_for(page['url'])['rules']
        for token in AI_BOTS:
            if not rules.can_fetch(page['url'], token):
                blocked[token].add(page['url'])

    robots = robots_for(site_url)
    lines = [line.strip() for line in robots['text'].splitlines()]
    summary = {
        'robots_url': robots['url'],
        'robots_status': robots['status'],
        'robots_unavailable': robots_unavailable(robots['status']),
        'cloudflare_managed': any(line.lower().startswith(CLOUDFLARE_MANAGED_MARKER) for line in lines),
        'content_signals': [line.split(':', 1)[1].strip() for line in lines
                            if line.lower().startswith('content-signal:')],
        'pages': len(pages),
        'bots': [{'token': token, 'used_by': bot['used_by'], 'kind': BOT_KINDS[bot['kind']]['label'],
                  'note': bot.get('note', ''), 'blocked_pages': len(blocked[token]),
                  'flagged': bool(blocked[token]) and _flags(BOT_KINDS[bot['kind']]['check'])}
                 for token, bot in AI_BOTS.items()],
    }
    return summary, blocked


def _flags(check):
    """Whether pages blocked to a bot of this kind get a warning, not just a note"""
    return check is not None and AI_SEARCH_CHECKS[check]['type'] == 'warning'
