"""AI Search: checks for how well AI answer engines can find, read and quote a
site's pages, each labelled with how strong the evidence behind it is.

Sources are the vendors' own docs (Google, Bing, OpenAI, Anthropic,
Perplexity) and citation studies, as of Oct 2026. The best study so far
(Chen & Koudas, arXiv 2610.15033, a preprint) found that where a page ranks
in an engine's own searches explains about 32% of which pages get cited, and
everything on the page together about 3%. So these are hygiene checks, and
nothing here adds up to a score.
"""
import html
import re
from collections import Counter, defaultdict, deque
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4.element import Comment, NavigableString, PreformattedString

from src.core.link_manager import is_same_site

# How strong the case behind a check is, strongest first; strength drives the meter in the tab
EVIDENCE = {
    'documented': {'strength': 4, 'label': 'Documented',
                   'note': 'The vendor documents that this controls whether or how its AI uses the page.'},
    'guidance': {'strength': 3, 'label': 'Vendor guidance',
                 'note': "Google or Bing recommend it, but no study has shown it changes how often pages are cited."},
    'convention': {'strength': 2, 'label': 'Tool convention',
                   'note': 'Common in SEO audit tools, with no vendor guidance or study behind it.'},
    'unproven': {'strength': 1, 'label': 'No measured effect',
                 'note': 'Recommended in some guides, but studies found no effect on how often pages are cited.'},
}

# Thresholds. Where no source is named, the number is LibreCrawl's own.
HIDDEN_MIN_WORDS = 50
LONG_INTRO_WORDS = 150
LONG_PAGE_CHARS = 100_000  # Sitebulb's AI-readiness limit, about 25,000 tokens
GOOGLEBOT_HTML_BYTES = 2 * 1024 * 1024  # Googlebot reads only the first 2 MB of HTML
SAME_LASTMOD_SHARE = 0.9  # this share of sitemap URLs on one lastmod looks like the generation time
SAME_LASTMOD_MIN_URLS = 10
EXAMPLES = 3  # examples quoted in an issue's details

GENERIC_HEADINGS = {'info', 'information', 'more', 'more info', 'more information', 'learn more', 'read more',
                    'click here'}
GENERIC_LINK_TEXT = {'click here', 'here', 'read more', 'learn more', 'more', 'more info', 'see more',
                     'view more', 'find out more', 'continue', 'continue reading', 'details', 'link', 'this'}

# Head tags compared between the raw and the rendered HTML: result key -> name in issue details
JS_HEAD_TAGS = {'title': 'title', 'meta_description': 'meta description',
                'canonical_url': 'canonical', 'robots': 'meta robots'}

WORDS = re.compile(r'\b\w+\b')
_NOT_TEXT = ('script', 'style', 'template')
_HEADING = re.compile(r'^h[1-6]$')
_INVISIBLE_STYLE = re.compile(r'display\s*:\s*none|visibility\s*:\s*hidden'
                              r'|(opacity\s*:\s*0*\.?0+|font-size\s*:\s*0+(px|em|rem|%)?)\s*(!important)?\s*(;|$)'
                              r'|(?<![\w-])(left|top|text-indent)\s*:\s*-\d{3,}', re.I)
_SCREEN_READER_ONLY = {'sr-only', 'visually-hidden', 'screen-reader-text'}
_AI = r'(ai|llms?|large\s+language\s+models?|language\s+models?|ai\s+assistants?|ai\s+agents?|chatbots?)'
_AI_INSTRUCTION = re.compile(
    r'\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|preceding)\s+'
    r'(instructions|prompts?|directions|context)'
    rf'|\b(if|when)\s+you\s+are\s+an?\s+{_AI}\b'
    rf'|\b(note|message|instructions?)\s+(to|for)\s+{_AI}\b'
    r'|\b(ai|llm)s?\s+(assistants?|agents?|models?|crawlers?|systems?)\s*[,:]?\s+(must|should|always|never|please)\b'
    r'|\bwhen\s+summari[sz]ing\b', re.I)
# JavaScript that sends the browser to another page
_NAVIGATES = re.compile(r'location(\.href)?\s*=[^=]|location\.(assign|replace)\s*\(|window\.open\s*\(|\.href\s*=[^=]')
# Common street-address words, reduced to one spelling so "Ave" matches "Avenue"
_ADDRESS_WORDS = {'street': 'st', 'avenue': 'ave', 'av': 'ave', 'boulevard': 'blvd', 'road': 'rd',
                  'drive': 'dr', 'lane': 'ln', 'court': 'ct', 'place': 'pl', 'parkway': 'pkwy', 'highway': 'hwy',
                  'suite': 'ste', 'apartment': 'apt', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
                  'northeast': 'ne', 'northwest': 'nw', 'southeast': 'se', 'southwest': 'sw'}
_FAQ_HEADING = re.compile(r'\b(faqs?|frequently asked|common questions|questions (and|&) answers|q\s?&\s?a)\b', re.I)
_ISO_DAY = re.compile(r'\s*(\d{4})-(\d{2})-(\d{2})')
_MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
           'september', 'october', 'november', 'december')
_LLMS_TITLE = re.compile(r'^\s*#\s+\S', re.M)
_MARKDOWN_LINK = re.compile(r'\[[^\]]+\]\([^)]+\)')


def sitemap_lastmod_status(site):
    """The sitemap row of the tab: (what it shows, whether that's a problem).
    sitemap_lastmods is None when the crawl didn't look for sitemaps."""
    lastmods = site.get('sitemap_lastmods')
    if lastmods is None:
        return 'Not checked: sitemap discovery was off, or this was a list crawl', False
    if not lastmods:
        return 'No sitemap found', False
    dated = [value for value in lastmods.values() if value]
    if len(dated) < len(lastmods):
        return f'{len(lastmods) - len(dated)} of {len(lastmods)} sitemap URLs have no lastmod', True
    value, count = Counter(dated).most_common(1)[0]
    if len(dated) >= SAME_LASTMOD_MIN_URLS and count >= SAME_LASTMOD_SHARE * len(dated):
        return (f'{count} of {len(dated)} URLs share lastmod {value}, '
                'which looks like when the sitemap was generated'), True
    return f'All {len(dated)} sitemap URLs have a lastmod', False


def llms_txt_status(site):
    """The llms.txt row of the tab: (what it shows, never a problem). Format rules are Lighthouse's."""
    fetched = site.get('llms_txt') or {}
    status = fetched.get('status')
    if status is None:
        return "Couldn't be fetched", False
    if not 200 <= status < 300:
        return f'Not found ({status})', False
    text = fetched.get('text') or ''
    if text.lstrip().startswith('<'):
        return 'Returns an HTML page, not an llms.txt file', False
    problems = [problem for problem, ok in (('is under 50 characters', len(text.strip()) >= 50),
                                            ('has no "# " title', _LLMS_TITLE.search(text)),
                                            ('has no Markdown links', _MARKDOWN_LINK.search(text))) if not ok]
    return 'Found' + (f', but it {" and ".join(problems)}' if problems else ''), False


# Every check in the AI Search tab, in display order. A check with an issue is
# raised per page in the Issues tab; one with count is a page inventory that is
# never flagged; one with site is a single row about the whole site.
AI_SEARCH_CHECKS = {
    'blocked_search': {
        'group': 'Access', 'issue': 'Blocked for AI Search Bots', 'type': 'warning', 'evidence': 'documented',
        'basis': "AI search engines cite pages their search bot may fetch, and each vendor documents its bot's "
                 'robots.txt token.'},
    'blocked_live': {
        'group': 'Access', 'issue': 'Blocked for AI Live Fetchers', 'type': 'info', 'evidence': 'documented',
        'basis': 'Stops an assistant reading the page while it answers a user. OpenAI and Perplexity say these '
                 'fetchers may not follow robots.txt.'},
    'no_google_ai': {
        'group': 'Access', 'issue': 'Excluded from Google AI Answers', 'type': 'warning', 'evidence': 'documented',
        'basis': "Google: nosnippet and max-snippet:0 keep a page's text out of AI Overviews and AI Mode."},
    'snippet_limited': {
        'group': 'Access', 'issue': 'AI Snippet Limited', 'type': 'info', 'evidence': 'documented',
        'basis': 'Google: max-snippet caps how much of the page its AI features may quote.'},
    'no_copilot': {
        'group': 'Access', 'issue': 'Excluded from Copilot Answers', 'type': 'warning', 'evidence': 'documented',
        'basis': "Bing: NOARCHIVE stops Copilot using the page's content in answers."},
    'copilot_limited': {
        'group': 'Access', 'issue': 'Copilot Limited to Title and Snippet', 'type': 'info',
        'evidence': 'documented', 'basis': "Bing: NOCACHE limits Copilot to the page's URL, title and snippet."},
    'nosnippet_text': {
        'group': 'Access', 'issue': 'Text Hidden from AI Snippets', 'type': 'info', 'evidence': 'documented',
        'basis': "Google: text inside data-nosnippet isn't quoted in snippets or AI features."},
    'needs_js': {
        'group': 'Markup & JavaScript', 'issue': 'Content Needs JavaScript', 'type': 'warning',
        'evidence': 'guidance', 'js_only': True,
        'basis': "Bing: don't hide important content behind client-side rendering. The ChatGPT, Claude and "
                 "Perplexity crawlers don't run JavaScript."},
    'js_head': {
        'group': 'Markup & JavaScript', 'issue': 'Head Tags Change After JavaScript', 'type': 'warning',
        'evidence': 'guidance', 'js_only': True,
        'basis': "Crawlers that don't run JavaScript see only the original tags, and Google's JavaScript SEO "
                 'guide warns against changing canonical or robots tags with JavaScript.'},
    'invalid_schema': {
        'group': 'Markup & JavaScript', 'issue': 'Invalid Structured Data', 'type': 'warning',
        'evidence': 'convention',
        'basis': "Six major audit tools flag JSON-LD that doesn't parse or lacks a schema.org @context and "
                 '@type; search engines skip such a block.'},
    'schema_only': {
        'group': 'Markup & JavaScript', 'issue': 'Facts Only in Structured Data', 'type': 'warning',
        'evidence': 'guidance',
        'basis': 'Google and Bing: structured data must match what the page shows. In a 2025 test, ChatGPT, '
                 'Claude, Perplexity and Gemini ignored JSON-LD when reading pages.'},
    'html_over_2mb': {
        'group': 'Markup & JavaScript', 'issue': 'HTML Over 2 MB', 'type': 'warning', 'evidence': 'documented',
        'basis': "Google: Googlebot reads only the first 2 MB of an HTML file, so content after that isn't "
                 'indexed.'},
    'heading_skips': {
        'group': 'Structure', 'issue': 'Skipped Heading Levels', 'type': 'info', 'evidence': 'guidance',
        'basis': 'Bing asks for a logical H1-H6 hierarchy, and Google for headings that give the page a clear '
                 'structure. Checked in <main> when the page has one.'},
    'weak_headings': {
        'group': 'Structure', 'issue': 'Empty or Generic Headings', 'type': 'info', 'evidence': 'guidance',
        'basis': "Bing: use headings that say what the section covers, not vague ones like 'Learn more'."},
    'hidden_content': {
        'group': 'Structure', 'issue': 'Content Hidden by Default', 'type': 'info', 'evidence': 'guidance',
        'basis': "Bing: don't hide important answers in tabs or expandable menus, since AI systems may not "
                 'render them. Catches closed <details>, the hidden attribute and aria-expanded panels, not '
                 'panels hidden with CSS alone.'},
    'ai_instructions': {
        'group': 'Structure', 'issue': 'Hidden Instructions for AI', 'type': 'warning', 'evidence': 'documented',
        'basis': "Bing: content written to manipulate language models can cost a page its visibility or get it "
                 "removed, and Google's spam policy covers manipulating AI answers. Matched by wording, so "
                 'check each one.'},
    'links_no_text': {
        'group': 'Structure', 'issue': 'Links Without Text', 'type': 'info', 'evidence': 'guidance',
        'basis': 'Google and Bing ask for descriptive link text; an internal link with no text, alt or '
                 'aria-label says nothing about the page it points to.'},
    'generic_links': {
        'group': 'Structure', 'issue': 'Generic Link Text', 'type': 'info', 'evidence': 'guidance',
        'basis': "Google's link best practices ask for descriptive anchor text rather than generic text like "
                 "'click here'. Checks internal links."},
    'unfollowable_links': {
        'group': 'Structure', 'issue': "Links Crawlers Can't Follow", 'type': 'warning', 'evidence': 'guidance',
        'basis': 'Google can only follow links that are <a> elements with an href, and Bing asks for standard '
                 '<a href> links. Flags href on other elements and navigation done in JavaScript.'},
    'bad_dates': {
        'group': 'Freshness', 'issue': 'Inconsistent Page Dates', 'type': 'warning', 'evidence': 'guidance',
        'basis': 'Google asks for accurate structured dates. Flags dates in the future, and modified dates '
                 'before published ones.'},
    'date_not_shown': {
        'group': 'Freshness', 'issue': 'Date Not Shown on Page', 'type': 'info', 'evidence': 'guidance',
        'basis': "Google: show a visible date that matches the one in the markup. Flags article pages whose "
                 "published and modified days don't appear in the text."},
    'sitemap_lastmod': {
        'group': 'Freshness', 'label': 'Sitemap lastmod dates', 'evidence': 'guidance',
        'site': sitemap_lastmod_status,
        'basis': "Google uses lastmod only when it's consistently accurate. Bing calls it a key signal for AI "
                 'search, and says not to set it to the time the sitemap was generated.'},
    'faq': {
        'group': 'Content inventory', 'label': 'Has an FAQ section', 'evidence': 'unproven',
        'count': lambda facts: facts.get('faq'),
        'basis': 'Bing recommends FAQ sections. A study of 158K AI answers found no effect when comparing pages '
                 'on the same site.'},
    'question_headings': {
        'group': 'Content inventory', 'label': 'Has headings phrased as questions', 'evidence': 'unproven',
        'count': lambda facts: facts.get('question_headings'),
        'basis': 'Bing gives question headings as an example. Studies disagree, and matching what people ask '
                 'mattered more than the question form.'},
    'lists_tables': {
        'group': 'Content inventory', 'label': 'Has lists or tables', 'evidence': 'unproven',
        'count': lambda facts: facts.get('lists_tables'),
        'basis': 'Bing recommends lists and tables. Comparing pages on the same site, studies found no effect '
                 'on citations.'},
    'long_intro': {
        'group': 'Content inventory', 'label': f'Over {LONG_INTRO_WORDS} words before the first subheading',
        'evidence': 'guidance', 'count': lambda facts: (facts.get('intro_words') or 0) > LONG_INTRO_WORDS,
        'basis': 'Bing: avoid long introductions before the main topic. One study found 44% of ChatGPT '
                 f"citations come from the first 30% of a page. The {LONG_INTRO_WORDS}-word cut-off is "
                 "LibreCrawl's."},
    'very_long': {
        'group': 'Content inventory', 'label': f'Over {LONG_PAGE_CHARS:,} characters of text',
        'evidence': 'convention', 'count': lambda facts: (facts.get('text_chars') or 0) > LONG_PAGE_CHARS,
        'basis': "Sitebulb's AI-readiness check warns above 100,000 characters (about 25,000 tokens), since AI "
                 'tools may read only part of a long page.'},
    'llms_txt': {
        'group': 'Content inventory', 'label': 'llms.txt', 'evidence': 'unproven', 'site': llms_txt_status,
        'basis': 'Google ignores llms.txt, and Ahrefs found 97% of llms.txt files got no requests at all. '
                 'Shown for completeness.'},
}


def ai_issue(url, check, details):
    """An Issues tab entry for one of AI_SEARCH_CHECKS"""
    spec = AI_SEARCH_CHECKS[check]
    return {'url': url, 'type': spec['type'], 'category': 'AI Search', 'issue': spec['issue'], 'details': details}


def ai_search_report(access, pages, issues, site):
    """The AI Search tab: the robots.txt access summary, then a row per check
    with the share of `pages` (internal HTML pages) it flags or counts"""
    urls = {page['url'] for page in pages}
    flagged = defaultdict(set)
    for issue in issues:
        if issue['category'] == 'AI Search' and issue['url'] in urls:
            flagged[issue['issue']].add(issue['url'])
    rendered = any(page.get('raw_word_count') is not None for page in pages)

    rows = []
    for check in AI_SEARCH_CHECKS.values():
        evidence = EVIDENCE[check['evidence']]
        row = {'group': check['group'], 'check': check.get('issue') or check['label'], 'basis': check['basis'],
               'evidence': evidence['label'], 'strength': evidence['strength'], 'flag': False}
        if 'site' in check:
            row['value'], row['flag'] = check['site'](site)
        elif check.get('js_only') and not rendered:
            row['value'] = 'Needs JavaScript rendering'
        elif 'issue' in check:
            row['pages'] = len(flagged[check['issue']])
            row['flag'] = bool(row['pages']) and check['type'] != 'info'
        else:
            row['pages'] = sum(1 for page in pages if check['count'](page.get('ai_search') or {}))
        rows.append(row)
    return {**access, 'checks': rows, 'evidence_levels': list(EVIDENCE.values())}


def _is_text(string):
    """A piece of page text, not a comment or the inside of a script, style or template"""
    return not isinstance(string, PreformattedString) and string.parent.name not in _NOT_TEXT


def visible_strings(tag):
    """The text pieces in a tag, leaving out scripts, styles and comments"""
    return [text for text in tag.find_all(string=True) if _is_text(text)]


def visible_word_count(tag):
    """Words in a tag's text, leaving out scripts, styles and comments"""
    return sum(len(WORDS.findall(text)) for text in visible_strings(tag))


def json_ld_scripts(soup):
    """A page's JSON-LD script tags"""
    return soup.find_all('script', attrs={'type': 'application/ld+json'})


def json_ld_nodes(blocks):
    """Every object in parsed JSON-LD blocks, nested ones and @graph members included"""
    queue = deque(blocks)
    while queue:
        item = queue.popleft()
        if isinstance(item, dict):
            yield item
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)


def js_head_changes(raw, rendered):
    """Head tags that differ once JavaScript has run, as {result key: raw value}.
    'json_ld' holds the raw block count when structured data appears or vanishes."""
    changes = {key: raw.get(key) or '' for key in JS_HEAD_TAGS
               if (raw.get(key) or '') != (rendered.get(key) or '')}
    if bool(raw.get('json_ld')) != bool(rendered.get('json_ld')):
        changes['json_ld'] = len(raw.get('json_ld') or [])
    return changes


def page_facts(soup, result, base_domain):
    """What the AI Search checks need from a parsed page. Runs after the
    JSON-LD has been extracted into result['json_ld']."""
    main = soup.find('main') or soup.find(attrs={'role': 'main'}) or soup.body or soup
    collapsed = _collapsed_regions(soup, main)
    text = ' '.join(' '.join(piece.split()) for piece in visible_strings(soup) if piece.strip())
    blocks = result.get('json_ld') or []
    published, modified = _page_dates(soup, blocks)
    return {
        **_heading_facts(main, {id(region) for region in collapsed}),
        **_link_facts(soup, result['url'], base_domain),
        'main_words': visible_word_count(main),
        'hidden_words': _hidden_words(collapsed),
        'ai_instructions': _ai_instructions(soup)[:EXAMPLES],
        'json_ld_errors': _json_ld_errors(soup, blocks),
        'schema_only': _schema_only_facts(blocks, text),
        'date_published': published and published.isoformat(),
        'date_modified': modified and modified.isoformat(),
        'date_shown': _date_shown((published, modified), text, soup),
        'faq': _has_faq(main, blocks),
        'lists_tables': _lists_and_tables(main),
        'intro_words': _intro_words(main),
        'text_chars': len(text),
    }


def _outermost(tags):
    """The tags that aren't inside another of `tags`"""
    ids = {id(tag) for tag in tags}
    return [tag for tag in tags if not any(id(parent) in ids for parent in tag.parents)]


def _within(tag, region_ids):
    return id(tag) in region_ids or any(id(parent) in region_ids for parent in tag.parents)


def _content_section(tag):
    return tag.name in ('main', 'article', 'section') or tag.get('role') == 'main'


def _in_boilerplate(tag):
    """In site navigation, a footer, an aside or the page-wide header (not a
    section's own <header>)"""
    for element in (tag, *tag.parents):
        if element.name in ('nav', 'footer', 'aside'):
            return True
        if element.name == 'header' and not element.find_parent(_content_section):
            return True
    return False


def _mostly_links(tag):
    """Navigation by another name (a menu, a list of categories): most of its words are link text"""
    words = visible_word_count(tag)
    return words > 0 and 2 * sum(visible_word_count(link) for link in tag.find_all('a')) > words


def _collapsed_regions(soup, main):
    """Content in `main` a reader has to click to see: closed <details>,
    elements with the hidden attribute, and panels an aria-expanded="false"
    control opens (accordions and tabs). Menus and site chrome are left out."""
    opened_by_click = {panel for control in soup.find_all(attrs={'aria-expanded': 'false'})
                       for panel in (control.get('aria-controls') or '').split()}

    def collapsed(tag):
        return (tag.has_attr('hidden') or (tag.name == 'details' and not tag.has_attr('open'))
                or tag.get('id') in opened_by_click)

    return [region for region in _outermost(main.find_all(collapsed))
            if not _in_boilerplate(region) and not _mostly_links(region)]


def _invisible(tag):
    """Kept from readers on purpose: the hidden attribute, an inline style that
    hides it (display:none, transparent, zero-size, off screen) or a
    screen-reader-only class. Closed accordions don't count: readers open them."""
    return (tag.has_attr('hidden') or bool(_INVISIBLE_STYLE.search(tag.get('style') or ''))
            or not _SCREEN_READER_ONLY.isdisjoint(tag.get('class') or ()))


def _hidden_words(regions):
    """Words in collapsed regions, less a <details> summary, which shows"""
    words = 0
    for region in regions:
        words += visible_word_count(region)
        summary = region.find('summary') if region.name == 'details' else None
        if summary:
            words -= visible_word_count(summary)
    return words


def _heading_facts(main, hidden_ids):
    """Skipped levels, empty and generic headings, and question headings in the main content"""
    facts = {'heading_skips': [], 'empty_headings': 0, 'generic_headings': [], 'question_headings': 0}
    previous = None
    for heading in main.find_all(_HEADING):
        if _within(heading, hidden_ids) or _in_boilerplate(heading):
            continue
        level = int(heading.name[1])
        if previous and level > previous + 1:
            facts['heading_skips'].append(f'H{previous} → H{level}')
        previous = level
        text = ' '.join(heading.get_text(' ').split())
        if not text and not any(image.get('alt') for image in heading.find_all('img')):
            facts['empty_headings'] += 1
        elif text.lower().rstrip(' :.') in GENERIC_HEADINGS:
            facts['generic_headings'].append(text)
        if level in (2, 3) and text.endswith('?'):
            facts['question_headings'] += 1
    return facts


def _ai_instructions(soup):
    """Hidden text and HTML comments worded as instructions to an AI model, quoted around the match"""
    texts = [tag.get_text(' ') for tag in _outermost(soup.find_all(_invisible))]
    texts += soup.find_all(string=lambda text: isinstance(text, Comment))
    quotes = []
    for text in texts:
        text = ' '.join(text.split())
        match = _AI_INSTRUCTION.search(text)
        if match:
            quotes.append(text[max(0, match.start() - 40):match.end() + 60])
    return quotes


def _link_facts(soup, page_url, base_domain):
    """Internal links with no text or generic text, and links crawlers can't follow"""
    no_text, generic, unfollowable = 0, Counter(), []
    for link in soup.find_all('a'):
        href = (link.get('href') or '').strip()
        onclick = link.get('onclick') or ''
        if not href or href == '#' or href.lower().startswith('javascript:'):
            if _NAVIGATES.search(f'{href} {onclick}'):
                unfollowable.append(f'<a href="{href[:60]}">' if href.lower().startswith('javascript:')
                                    else f'<a onclick="{onclick[:60]}">')
            continue
        if href.lower().startswith(('#', 'mailto:', 'tel:', 'sms:')):
            continue
        try:
            if not is_same_site(urljoin(page_url, href), base_domain):
                continue
        except ValueError:  # an href urljoin can't parse, like an unclosed IPv6 host
            continue
        # The accessible name: aria-label, else the text, else image alt text, else the title
        candidates = (link.get('aria-label'), link.get_text(' '),
                      ' '.join(image.get('alt') or '' for image in link.find_all('img')), link.get('title'))
        name = next((name for name in (' '.join((c or '').split()) for c in candidates) if name), '')
        text = name.lower().strip(' .:…›»→>-')
        if not name:
            if not link.get('aria-labelledby'):
                no_text += 1
        elif text in GENERIC_LINK_TEXT:
            generic[text] += 1
    unfollowable += [f'<{tag.name} href="{tag["href"][:60]}">' for tag in soup.find_all(_href_on_non_link)]
    return {'links_no_text': no_text, 'generic_links': dict(generic),
            'unfollowable_count': len(unfollowable), 'unfollowable_links': unfollowable[:EXAMPLES]}


def _href_on_non_link(tag):
    """An href on an element crawlers don't follow, like <span href> or <div href> (SVG's own hrefs aside)"""
    return (tag.has_attr('href') and tag.name not in ('a', 'link', 'area', 'base', 'svg')
            and not tag.find_parent('svg'))


def _types(node):
    value = node.get('@type')
    return {item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)}


def _json_ld_errors(soup, blocks):
    """JSON-LD blocks that didn't parse (extract_json_ld drops those), or that
    have no schema.org @context or no @type"""
    scripts = json_ld_scripts(soup)
    malformed = 0
    for block in blocks:
        nodes = list(json_ld_nodes([block]))
        if not (any('schema.org' in str(node.get('@context', '')) for node in nodes)
                and any(_types(node) for node in nodes)):
            malformed += 1
    return len(scripts) - len(blocks) + malformed


def _plain(text):
    """Lower-case words and numbers only, HTML entities decoded, for loose text matching"""
    return ' '.join(re.sub(r'[^\w]+', ' ', html.unescape(str(text)).lower()).split())


def _shown_name(name, tokens):
    """A name counts as shown when its last word and at least half its words
    are on the page, so "Jane Smith" matches "By J. Smith" or "Smith, Jane" """
    words = _plain(name).split()
    return bool(words) and words[-1] in tokens and 2 * sum(word in tokens for word in words) >= len(words)


def _shown_address(address, tokens):
    """An address counts as shown when its numbers and at least half its words
    are on the page, with spellings like Ave/Avenue and NW/Northwest treated alike"""
    words = [_ADDRESS_WORDS.get(word, word) for word in _plain(address).split()]
    numbers = [word for word in words if word.isdigit()]
    others = [word for word in words if not word.isdigit()]
    return all(number in tokens for number in numbers) and 2 * sum(word in tokens for word in others) >= len(others)


def _names(value):
    """The names in a JSON-LD author: a string, an object with a name, or a list of them"""
    for item in value if isinstance(value, list) else [value]:
        name = item.get('name') if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip() and not name.startswith('http'):
            yield name.strip()


def _price_texts(value):
    """Ways a page might print a JSON-LD price, or none for a missing, zero or unreadable one"""
    raw = str(value).strip()
    if re.fullmatch(r'\d+,\d{1,2}', raw):  # a decimal comma, "12,50"
        raw = raw.replace(',', '.')
    try:
        price = float(raw.replace(',', ''))
    except ValueError:
        return set()
    if not 0 < price < float('inf'):
        return set()
    grouped = f'{price:,.2f}'
    texts = {f'{price:.2f}', grouped, f'{price:.2f}'.replace('.', ','),
             grouped.translate(str.maketrans(',.', '.,'))}
    if price.is_integer():
        texts |= {f'{price:.0f}', f'{price:,.0f}', f'{price:,.0f}'.replace(',', '.')}
    return texts


def _schema_only_facts(blocks, text):
    """Phone numbers, prices, addresses, authors and FAQ questions in the
    JSON-LD that the page text doesn't show. AI tools reading the page skip
    JSON-LD, so a fact that's only there is lost to them."""
    if not blocks:
        return []
    plain = f' {_plain(text)} '
    tokens = set(plain.split())
    address_tokens = {_ADDRESS_WORDS.get(token, token) for token in tokens}
    numbers = set(re.findall(r'\d[\d.,]*\d|\d', text))
    digits = re.sub(r'\D', '', text)
    missing, questions, unseen = [], 0, 0
    for node in json_ld_nodes(blocks):
        phone = node.get('telephone')
        phone_digits = re.sub(r'\D', '', phone) if isinstance(phone, str) else ''
        if len(phone_digits) >= 7 and phone_digits[-9:] not in digits:
            missing.append(f'telephone {phone.strip()}')
        if 'price' in node:
            texts = _price_texts(node['price'])
            if texts and texts.isdisjoint(numbers):
                missing.append(f"price {node['price']}")
        address = node.get('streetAddress')
        if isinstance(address, str) and address.strip() and not _shown_address(address, address_tokens):
            missing.append(f'address {address.strip()}')
        for name in _names(node.get('author')):
            if not _shown_name(name, tokens):
                missing.append(f'author {name}')
        if 'FAQPage' in _types(node):
            entities = node.get('mainEntity')
            for question in entities if isinstance(entities, list) else [entities]:
                if isinstance(question, dict) and isinstance(question.get('name'), str):
                    questions += 1
                    unseen += f' {_plain(question["name"])} ' not in plain
    if unseen:
        missing.append(f'{unseen} of {questions} FAQ questions')
    return list(dict.fromkeys(missing))


def _day(value):
    match = _ISO_DAY.match(value) if isinstance(value, str) else None
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def _page_dates(soup, blocks):
    """(published, modified) days of an article: from Article or BlogPosting
    JSON-LD, else the article:*_time meta tags of a page with a published
    time. Other pages aren't dated: SEO plugins stamp dates on every page's
    WebPage markup and meta tags, home page included."""
    articles = [node for node in json_ld_nodes(blocks)
                if any(kind.endswith(('Article', 'Posting')) for kind in _types(node))]
    if articles:
        return tuple(next((day for day in (_day(node.get(key)) for node in articles) if day), None)
                     for key in ('datePublished', 'dateModified'))
    published = soup.find('meta', attrs={'property': 'article:published_time'})
    if not published:
        return None, None
    modified = soup.find('meta', attrs={'property': 'article:modified_time'})
    return _day(published.get('content')), (_day(modified.get('content')) if modified else None)


def _date_texts(day):
    """Ways a page might print a day, normalised the way _normalise_dates leaves page text"""
    month = _MONTHS[day.month - 1]
    names = {month, month[:3]} | ({'sept'} if day.month == 9 else set())
    y = day.year
    texts = {day.isoformat()}
    for d in {str(day.day), f'{day.day:02}'}:
        for m in {str(day.month), f'{day.month:02}'}:
            texts |= {f'{m}/{d}/{y}', f'{d}/{m}/{y}', f'{d}-{m}-{y}', f'{m}-{d}-{y}', f'{y}/{m}/{d}',
                      f'{d} {m} {y}', f'{y} {m} {d}'}
        for name in names:
            texts |= {f'{name} {d} {y}', f'{d} {name} {y}', f'{d} of {name} {y}'}
    return texts


def _normalise_dates(text):
    """Lower case, with ordinal suffixes, commas and full stops dropped: "Oct. 5th, 2026" -> "oct 5 2026" """
    text = re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', text.lower())
    return ' '.join(re.sub(r'[,.]', ' ', text).split())


def _date_shown(days, text, soup):
    """Whether the page shows its published or modified day, in its text or a
    <time> element. A day either side counts, since markup dates are often in
    UTC. None when there's no date to look for, or when a page that isn't in
    English doesn't show one: month names are only matched in English."""
    days = {day + timedelta(days=offset) for day in days if day for offset in (-1, 0, 1)}
    if not days:
        return None
    if any(time.get_text(strip=True) and _day(time.get('datetime')) in days for time in soup.find_all('time')):
        return True
    forms = set().union(*map(_date_texts, days))
    pattern = re.compile(r'(?<![\w/-])(' + '|'.join(map(re.escape, forms)) + r')(?![\w/-])')
    if pattern.search(_normalise_dates(text)):
        return True
    lang = (soup.html.get('lang') or '') if soup.html else ''
    return False if not lang or lang.lower().startswith('en') else None


def _has_faq(main, blocks):
    """An FAQ heading in the main content, FAQPage markup, or two or more <details> whose summary is a question"""
    return (any(_FAQ_HEADING.search(heading.get_text(' ')) for heading in main.find_all(_HEADING)
                if not _in_boilerplate(heading))
            or any('FAQPage' in _types(node) for node in json_ld_nodes(blocks))
            or sum(summary.get_text(strip=True).endswith('?') for summary in main.find_all('summary')) >= 2)


def _lists_and_tables(main):
    """Lists of two or more items and tables of two or more rows in the main
    content, leaving out lists of links (menus, category widgets)"""
    return sum(1 for tag in main.find_all(('ul', 'ol', 'table'))
               if len(tag.find_all('tr' if tag.name == 'table' else 'li')) >= 2
               and not _in_boilerplate(tag) and not _mostly_links(tag))


def _intro_words(main):
    """Words between the H1 (or the start of the main content) and the first H2; None without an H2"""
    first_h2 = main.find('h2')
    if not first_h2:
        return None
    h1 = main.find('h1')
    start = h1 if h1 and h1.find_next('h2') is first_h2 else main
    words = 0
    for element in start.next_elements:
        if element is first_h2:
            break
        if (isinstance(element, NavigableString) and _is_text(element) and not _in_boilerplate(element.parent)
                and not (start is h1 and any(parent is h1 for parent in element.parents))):
            words += len(WORDS.findall(element))
    return words
