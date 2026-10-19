"""SEO issue detection and reporting"""
import math
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from fnmatch import fnmatch
from urllib.parse import urljoin, urlparse

from src.core.ai_access import AI_BOTS, BOT_KINDS
from src.core.ai_search import EXAMPLES, GOOGLEBOT_HTML_BYTES, HIDDEN_MIN_WORDS, JS_HEAD_TAGS, ai_issue
from src.core.link_manager import normalize_url

# Near-duplicate detection is bounded so a large site can never hold a crawl in
# "finishing up" indefinitely: when the budget is exhausted the pass stops with
# a warning and partial results instead of running to completion.
DUPLICATION_TIME_BUDGET_SECONDS = 120
DUPLICATION_EXAMPLES_PER_PAGE = 5

_TOKEN_SPLIT = re.compile(r'[^\w]+')

# A page "needs JavaScript" when rendering adds this much: more than half
# again the words or internal links of the raw HTML, and at least this many
JS_ONLY_RATIO = 1.5
JS_ONLY_MIN_WORDS = 50
JS_ONLY_MIN_LINKS = 5

# max-snippet values below this leave AI features little to quote
SHORT_MAX_SNIPPET = 50

# Robots directives whose own syntax contains a colon ("max-snippet:50"), so
# the colon does not mean a crawler name ("bingbot: noindex")
_ROBOTS_DIRECTIVES_WITH_VALUES = {'max-snippet', 'max-image-preview', 'max-video-preview',
                                  'unavailable_after'}

# Page elements checked for exact duplicates across indexable pages:
# (result key, issue name, how the details name it)
_DUPLICATE_ELEMENTS = (
    ('title', 'Duplicate Title', 'title'),
    ('meta_description', 'Duplicate Meta Description', 'meta description'),
    ('h1', 'Duplicate H1', 'H1'),
)


def has_alt_attribute(image):
    """True when the img carried an alt attribute, empty or not.

    alt="" is the correct way to mark an image decorative, so it counts as
    present. Crawls saved before has_alt existed carry no flag, and are treated
    as fine rather than guessed at (issue #95).
    """
    if 'has_alt' in image:
        return bool(image['has_alt'])
    return True


def robots_directives(value, crawler='googlebot'):
    """The directives in a meta robots or X-Robots-Tag value that apply to one
    crawler, as {name: setting}: "noindex, max-snippet:50" gives
    {'noindex': '', 'max-snippet': '50'}.

    X-Robots-Tag can aim directives at one crawler ("bingbot: noindex,
    nofollow"); those count only when unscoped or aimed at `crawler`.
    "none" is shorthand for noindex, nofollow.
    """
    directives = {}
    agent = None
    for token in (value or '').lower().split(','):
        name, colon, rest = token.partition(':')
        if colon and name.strip() not in _ROBOTS_DIRECTIVES_WITH_VALUES:
            agent, token = name.strip(), rest
        name, _, setting = token.partition(':')
        if agent in (None, crawler) and name.strip():
            directives[name.strip()] = setting.strip()
    if 'none' in directives:
        directives.update(noindex='', nofollow='')
    return directives


def page_robots_directives(result, crawler='googlebot'):
    """Robots directives a page sets for one crawler: its meta robots tag, a
    meta tag named after the crawler (<meta name="bingbot">) and its
    X-Robots-Tag header"""
    meta_tags = result.get('meta_tags') or {}
    return (robots_directives(result.get('robots'), crawler)
            | robots_directives(meta_tags.get(crawler), crawler)
            | robots_directives(result.get('x_robots_tag'), crawler))


def is_internal_html_page(result):
    """An internal page that loaded (2xx) and is HTML"""
    status = result.get('status_code') or 0
    content_type = (result.get('content_type') or '').lower()
    return (bool(result.get('url')) and bool(result.get('is_internal')) and 200 <= status < 300
            and (not content_type or 'html' in content_type))


def canonical_elsewhere(result):
    """The URL a page's canonical points to, or None when it's missing or self-referencing"""
    canonical = result.get('canonical_url')
    if not canonical:
        return None
    url = result['url']
    landed = result.get('redirected_to') or url
    target = normalize_url(urljoin(landed, canonical))
    return None if target in (normalize_url(url), normalize_url(landed)) else target


class IssueDetector:
    """Detects SEO and technical issues in crawled pages"""

    # Weights of the signals that make up the duplicate-content score.
    _DUP_WEIGHTS = {'title': 0.35, 'desc': 0.35, 'h1': 0.20, 'word_count': 0.10}

    def __init__(self, exclusion_patterns=None):
        self.exclusion_patterns = exclusion_patterns or []
        self.detected_issues = []
        self.issues_lock = threading.Lock()
        self.duplication_truncated = False

    def detect_issues(self, result):
        """Detect SEO issues for a crawled URL"""
        url = result.get('url', '')
        issues = []

        # Skip if URL matches exclusion patterns
        if self._should_exclude(url):
            return

        # SEO checks only apply to HTML pages, not images/CSS/JS/etc.
        content_type = result.get('content_type', '')
        if content_type and 'html' not in content_type.lower():
            return

        # Fetch errors, status codes and redirects apply to every response
        self._check_technical_issues(result, issues)
        self._check_redirect_issues(result, issues)

        # Everything else judges the page itself. A failed fetch or an error
        # page has no real page to judge: checking it anyway buried the actual
        # error under a missing title, H1, viewport and so on.
        status_code = result.get('status_code') or 0
        if 200 <= status_code < 300:
            self._check_title_issues(result, issues)
            self._check_meta_description_issues(result, issues)
            self._check_heading_issues(result, issues)
            self._check_content_issues(result, issues)
            self._check_canonical_issues(result, issues)
            self._check_mobile_issues(result, issues)
            self._check_accessibility_issues(result, issues)
            self._check_social_media_issues(result, issues)
            self._check_structured_data_issues(result, issues)
            self._check_performance_issues(result, issues)
            self._check_indexability_issues(result, issues)
            self._check_ai_snippet_issues(result, issues)
            self._check_js_only_content_issues(result, issues)
            self._check_ai_page_issues(result, issues)
            self._check_broken_image_issues(result, issues)

        # Add all detected issues
        with self.issues_lock:
            self.detected_issues.extend(issues)

    def _check_title_issues(self, result, issues):
        """Check for title-related issues"""
        url = result.get('url', '')
        title = result.get('title', '')

        if not title:
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'SEO',
                'issue': 'Missing Title Tag',
                'details': 'Page has no title tag'
            })
        elif len(title) > 60:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'SEO',
                'issue': 'Title Too Long',
                'details': f"Title is {len(title)} characters (recommended: ≤60)"
            })
        elif len(title) < 30:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'SEO',
                'issue': 'Title Too Short',
                'details': f"Title is {len(title)} characters (recommended: 30-60)"
            })

    def _check_meta_description_issues(self, result, issues):
        """Check for meta description issues"""
        url = result.get('url', '')
        meta_desc = result.get('meta_description', '')

        if not meta_desc:
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'SEO',
                'issue': 'Missing Meta Description',
                'details': 'Page has no meta description'
            })
        elif len(meta_desc) > 160:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'SEO',
                'issue': 'Meta Description Too Long',
                'details': f"Description is {len(meta_desc)} characters (recommended: ≤160)"
            })
        elif len(meta_desc) < 120:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'SEO',
                'issue': 'Meta Description Too Short',
                'details': f"Description is {len(meta_desc)} characters (recommended: 120-160)"
            })

    def _check_heading_issues(self, result, issues):
        """Check for heading-related issues"""
        url = result.get('url', '')

        if not result.get('h1'):
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'SEO',
                'issue': 'Missing H1 Tag',
                'details': 'Page has no H1 heading'
            })

    def _check_content_issues(self, result, issues):
        """Check for content-related issues"""
        url = result.get('url', '')
        word_count = result.get('word_count', 0)

        if word_count < 300:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Content',
                'issue': 'Thin Content',
                'details': f'Page has only {word_count} words (recommended: ≥300)'
            })

    def _check_technical_issues(self, result, issues):
        """Check for technical SEO issues"""
        url = result.get('url', '')
        status_code = result.get('status_code', 0)

        # No HTTP response at all (DNS failure, connection refused, timeout, etc.)
        if status_code == 0:
            error_type = result.get('error_type')
            error_label_map = {
                'dns_not_found': ('DNS Not Found',
                                  'Domain does not resolve. The site may be expired or misconfigured.'),
                'connection_refused': ('Connection Refused',
                                       'Server actively refused the connection.'),
                'timeout': ('Request Timeout',
                            'Server did not respond before the request timed out.'),
                'ssl_error': ('SSL/TLS Error',
                              'Could not establish a secure connection (certificate or TLS issue).'),
                'redirect_loop': ('Redirect Loop',
                                  'URL redirects in a loop (or a very long chain) and never reaches a page.'),
                'connection_error': ('Connection Error',
                                     'Could not connect to the server.'),
            }
            if error_type and error_type != 'file_too_large':
                title, default_details = error_label_map.get(
                    error_type, ('No Response', 'No HTTP response received.')
                )
                issues.append({
                    'url': url,
                    'type': 'error',
                    'category': 'Technical',
                    'issue': title,
                    'details': result.get('error') or default_details
                })

        if status_code >= 400 and status_code < 500:
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'Technical',
                'issue': f'{status_code} Client Error',
                'details': self._get_status_code_message(status_code)
            })
        elif status_code >= 500:
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'Technical',
                'issue': f'{status_code} Server Error',
                'details': self._get_status_code_message(status_code)
            })
        elif status_code >= 300 and status_code < 400:
            issues.append({
                'url': url,
                'type': 'info',
                'category': 'Technical',
                'issue': f'{status_code} Redirect',
                'details': 'URL redirects to another location'
            })

    def _check_redirect_issues(self, result, issues):
        """Flag redirect chains: more than one hop to reach the final page.

        Loops are reported as a fetch error instead (error_type redirect_loop).
        """
        hops = result.get('redirects') or []
        if len(hops) < 2 or result.get('error_type'):
            return

        steps = [f"{hop.get('url')} ({hop.get('status_code')})" for hop in hops]
        steps.append(f"{result.get('redirected_to', '')} ({result.get('status_code')})")
        issues.append({
            'url': result.get('url', ''),
            'type': 'warning',
            'category': 'Technical',
            'issue': 'Redirect Chain',
            'details': f"{len(hops)} redirects to reach the final page: " + ' → '.join(steps)
        })

    def _check_canonical_issues(self, result, issues):
        """Check the canonical URL"""
        url = result.get('url', '')
        canonical_url = result.get('canonical_url', '')
        if not canonical_url:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Technical',
                'issue': 'Missing Canonical URL',
                'details': 'Page has no canonical URL specified'
            })
        elif canonical_url != url:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Technical',
                'issue': 'Canonical URL Different',
                'details': f"Canonical points to: {canonical_url}"
            })

    def _check_mobile_issues(self, result, issues):
        """Check for mobile optimization issues"""
        url = result.get('url', '')

        if not result.get('viewport'):
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'Mobile',
                'issue': 'Missing Viewport Meta Tag',
                'details': 'Page is not mobile-optimized'
            })

    def _check_accessibility_issues(self, result, issues):
        """Check for accessibility issues"""
        url = result.get('url', '')

        if not result.get('lang'):
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Accessibility',
                'issue': 'Missing Language Attribute',
                'details': 'HTML tag has no lang attribute'
            })

        # Image alt text. Only a missing attribute is a defect;
        # alt="" marks a decorative image and is correct.
        images = result.get('images', [])
        missing = [img for img in images if not has_alt_attribute(img)]
        decorative = [img for img in images
                      if has_alt_attribute(img) and not (img.get('alt') or '').strip()]

        if missing:
            detail = f'{len(missing)} of {len(images)} images have no alt attribute'
            if decorative:
                count = len(decorative)
                noun = 'image' if count == 1 else 'images'
                verb = 'is' if count == 1 else 'are'
                detail += f' ({count} decorative {noun} with alt="" {verb} fine)'
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Accessibility',
                'issue': 'Images Without Alt Text',
                'details': detail
            })

    def _check_social_media_issues(self, result, issues):
        """Check for social media optimization issues"""
        url = result.get('url', '')

        if not result.get('og_tags'):
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Social',
                'issue': 'Missing OpenGraph Tags',
                'details': 'Page has no OpenGraph tags for social sharing'
            })

        if not result.get('twitter_tags'):
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Social',
                'issue': 'Missing Twitter Card Tags',
                'details': 'Page has no Twitter Card tags'
            })

    def _check_structured_data_issues(self, result, issues):
        """Check for structured data issues"""
        url = result.get('url', '')

        if not result.get('json_ld') and not result.get('schema_org'):
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'Structured Data',
                'issue': 'No Structured Data',
                'details': 'Page has no JSON-LD or Schema.org markup'
            })

    def _check_performance_issues(self, result, issues):
        """Check for performance issues"""
        url = result.get('url', '')
        response_time = result.get('response_time', 0)
        page_size = result.get('size', 0)

        if response_time > 3000:
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'Performance',
                'issue': 'Slow Response Time',
                'details': f'Page took {response_time}ms to respond (recommended: <3000ms)'
            })
        elif response_time > 1000:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Performance',
                'issue': 'Moderate Response Time',
                'details': f'Page took {response_time}ms to respond (recommended: <1000ms)'
            })

        if page_size > 3 * 1024 * 1024:
            issues.append({
                'url': url,
                'type': 'error',
                'category': 'Performance',
                'issue': 'Large Page Size',
                'details': f'Page size is {page_size / 1024 / 1024:.1f}MB (recommended: <3MB)'
            })
        elif page_size > 1 * 1024 * 1024:
            issues.append({
                'url': url,
                'type': 'warning',
                'category': 'Performance',
                'issue': 'Moderate Page Size',
                'details': f'Page size is {page_size / 1024 / 1024:.1f}MB (recommended: <1MB)'
            })

    def _check_indexability_issues(self, result, issues):
        """Check for noindex/nofollow in the meta robots or googlebot tags or the X-Robots-Tag header"""
        url = result.get('url', '')
        sources = {
            'meta robots tag': robots_directives(result.get('robots')),
            'meta googlebot tag': robots_directives((result.get('meta_tags') or {}).get('googlebot')),
            'X-Robots-Tag header': robots_directives(result.get('x_robots_tag')),
        }

        for directive, issue, details in (
            ('noindex', 'Noindex Tag Present',
             'Page is BLOCKED from search engines - has noindex directive'),
            ('nofollow', 'Nofollow Tag Present',
             'Links on this page are NOT followed by search engines - has nofollow directive'),
        ):
            found_in = [source for source, directives in sources.items() if directive in directives]
            if found_in:
                issues.append({
                    'url': url,
                    'type': 'error',
                    'category': 'Indexability',
                    'issue': issue,
                    'details': f"{details} (in the {' and '.join(found_in)})"
                })

    def _check_ai_snippet_issues(self, result, issues):
        """Robots directives that keep a page's text out of AI answers or cut what they quote"""
        url = result.get('url', '')
        google = page_robots_directives(result)
        if 'noindex' in google:
            return  # already reported, and it keeps the page out of everything

        def add(check, details):
            issues.append(ai_issue(url, check, details))

        max_snippet = google.get('max-snippet', '')
        if 'nosnippet' in google or max_snippet == '0':
            add('no_google_ai', 'nosnippet (or max-snippet:0) keeps the page\'s text out of AI Overviews and AI Mode')
        elif max_snippet.isdigit() and int(max_snippet) < SHORT_MAX_SNIPPET:
            add('snippet_limited',
                f'max-snippet:{max_snippet} limits how much of the page Google\'s AI features can quote')

        bing = page_robots_directives(result, 'bingbot')
        if 'noarchive' in bing:
            add('no_copilot', 'noarchive stops Microsoft Copilot from using the page\'s content in answers')
        elif 'nocache' in bing:
            add('copilot_limited', 'nocache limits Microsoft Copilot to the page\'s URL, title and snippet')

        hidden_words = result.get('nosnippet_words') or 0
        if hidden_words:
            add('nosnippet_text',
                f'{hidden_words} words are inside data-nosnippet elements, which Google\'s AI features won\'t quote')

    def _check_js_only_content_issues(self, result, issues):
        """Content and head tags that only appear, or change, once JavaScript
        runs. Set for pages crawled with JavaScript rendering, which also
        record what the raw HTML holds."""
        raw_words = result.get('raw_word_count')
        if raw_words is None:
            return
        url = result.get('url', '')

        missing = []
        words = result.get('rendered_word_count') or 0
        if words - raw_words >= JS_ONLY_MIN_WORDS and words > JS_ONLY_RATIO * raw_words:
            missing.append(f'{words - raw_words} of {words} words')
        if result.get('h1') and not result.get('raw_h1'):
            missing.append('the H1')
        raw_links, links = result.get('raw_internal_links') or 0, result.get('internal_links') or 0
        if links - raw_links >= JS_ONLY_MIN_LINKS and links > JS_ONLY_RATIO * raw_links:
            missing.append(f'{links - raw_links} of {links} internal links')

        if missing:
            issues.append(ai_issue(url, 'needs_js',
                                   f"Only there after JavaScript runs: {', '.join(missing)}. "
                                   "The ChatGPT, Claude and Perplexity crawlers don't run JavaScript, so they miss it."))

        changes = (result.get('ai_search') or {}).get('js_changes')
        if changes:
            issues.append(ai_issue(url, 'js_head', '; '.join(
                self._js_head_change(key, raw, result) for key, raw in changes.items())))

    @staticmethod
    def _js_head_change(key, raw, result):
        """One head tag that JavaScript changed, as issue details put it"""
        if key == 'json_ld':
            return 'structured data only after JavaScript' if not raw else 'structured data removed by JavaScript'
        name = JS_HEAD_TAGS[key]
        if not raw:
            return f'{name} only after JavaScript'
        verb = 'changed' if result.get(key) else 'removed'
        return f'{name} {verb} by JavaScript (raw HTML: "{raw[:80]}")'

    def _check_ai_page_issues(self, result, issues):
        """What an AI tool reading the page would trip over, from the facts
        ai_search.page_facts collected. A noindexed page is out of AI search anyway."""
        facts = result.get('ai_search')
        if not facts or 'noindex' in page_robots_directives(result):
            return
        url = result['url']

        def add(check, details):
            issues.append(ai_issue(url, check, details))

        def plural(count, noun):
            return f"{count} {noun}{'s' if count != 1 else ''}"

        if facts['json_ld_errors']:
            add('invalid_schema', plural(facts['json_ld_errors'], 'JSON-LD block')
                + ' with a parse error, or no schema.org @context or @type')
        if facts['schema_only']:
            add('schema_only', 'In the structured data but not the page text: ' + '; '.join(facts['schema_only']))
        html_bytes = facts.get('raw_bytes') or result.get('size') or 0
        if html_bytes > GOOGLEBOT_HTML_BYTES:
            add('html_over_2mb', f'The HTML is {html_bytes / 1024 / 1024:.1f} MB. Googlebot reads only the '
                                 "first 2 MB, so the rest isn't indexed")

        if facts['heading_skips']:
            add('heading_skips', 'Heading levels skipped: ' + ', '.join(
                skip + (f' ({count}×)' if count > 1 else '')
                for skip, count in Counter(facts['heading_skips']).items()))
        weak = []
        if facts['empty_headings']:
            weak.append(plural(facts['empty_headings'], 'empty heading'))
        if facts['generic_headings']:
            weak.append('generic: ' + ', '.join(f'"{heading}"' for heading in facts['generic_headings'][:EXAMPLES]))
        if weak:
            add('weak_headings', '; '.join(weak))
        if facts['hidden_words'] >= HIDDEN_MIN_WORDS:
            add('hidden_content', f"{facts['hidden_words']} of {facts['main_words']} words stay hidden until a "
                                  'reader clicks or opens them (tabs, accordions, <details>)')
        if facts['ai_instructions']:
            add('ai_instructions', 'Hidden text or comments addressed to AI: '
                + ' | '.join(f'"…{quote}…"' for quote in facts['ai_instructions']))

        if facts['links_no_text']:
            add('links_no_text', plural(facts['links_no_text'], 'link') + ' with no text, image alt or aria-label')
        if facts['generic_links']:
            add('generic_links', 'Links that only say ' + ', '.join(
                f'"{text}"' + (f' ({count}×)' if count > 1 else '')
                for text, count in sorted(facts['generic_links'].items(), key=lambda item: -item[1])))
        if facts['unfollowable_count']:
            add('unfollowable_links', plural(facts['unfollowable_count'], 'link')
                + ' without a crawlable <a href>, e.g. ' + ', '.join(facts['unfollowable_links']))

        published, modified = (date.fromisoformat(day) if day else None
                               for day in (facts['date_published'], facts['date_modified']))
        problems = [f'{name} {day} is in the future'
                    for name, day in (('datePublished', published), ('dateModified', modified))
                    if day and day > date.today() + timedelta(days=1)]
        if published and modified and modified < published:
            problems.append(f'dateModified {modified} is before datePublished {published}')
        if problems:
            add('bad_dates', '; '.join(problems))
        if facts['date_shown'] is False:
            dates = ', '.join(f'{name} {day}' for name, day in (('published', published), ('modified', modified)) if day)
            add('date_not_shown', f"The article's dates in its markup ({dates}) don't appear in the page text")

    def _check_broken_image_issues(self, result, issues):
        """Check for broken image URLs on the page"""
        url = result.get('url', '')
        broken_images = result.get('broken_images', [])
        for img in broken_images:
            status = img.get('status', 0)
            img_url = img.get('url', '')
            if status == 0:
                issues.append({
                    'url': url,
                    'type': 'error',
                    'category': 'Content',
                    'issue': 'Broken Image (No Response)',
                    'details': f'Image does not respond: {img_url}'
                })
            elif status >= 400:
                issues.append({
                    'url': url,
                    'type': 'error',
                    'category': 'Content',
                    'issue': f'Broken Image ({status})',
                    'details': f'Image returned {status}: {img_url}'
                })

    def detect_duplicate_element_issues(self, all_results):
        """Flag indexable pages that share a title, meta description or H1.

        Matching ignores case and extra whitespace, and blanks are skipped
        (the Missing ... checks cover those). As in Screaming Frog, only
        indexable pages count: a noindexed page, or one that canonicalises
        to another URL, sharing a title is expected rather than a problem.
        One issue per page and element, naming a few of the pages it matches.
        """
        pages = [result for result in all_results if self._is_indexable_page(result)]
        issues = []

        for key, issue, label in _DUPLICATE_ELEMENTS:
            groups = defaultdict(list)
            for page in pages:
                text = self._normalise_text(page.get(key))
                if text:
                    groups[text].append(page)

            for group in groups.values():
                if len(group) < 2:
                    continue
                others = len(group) - 1
                for page in group:
                    text = ' '.join(self._first_text(page.get(key)).split())
                    examples = [p['url'] for p in group[:DUPLICATION_EXAMPLES_PER_PAGE + 1]
                                if p is not page][:DUPLICATION_EXAMPLES_PER_PAGE]
                    more = others - len(examples)
                    issues.append({
                        'url': page['url'],
                        'type': 'warning',
                        'category': 'SEO',
                        'issue': issue,
                        'details': f"Same {label} as {others} other page{'s' if others != 1 else ''} "
                                   f"(\"{text}\"): " + ', '.join(examples)
                                   + (f" (+{more} more)" if more > 0 else '')
                    })

        with self.issues_lock:
            self.detected_issues.extend(issues)

    def detect_ai_bot_issues(self, all_results, blocked):
        """Flag pages robots.txt keeps AI search bots or live fetchers off.

        blocked maps each AI bot token to the URLs it may not fetch. Training
        bots raise no issue: blocking them doesn't take a page out of AI answers.
        """
        issues = []
        for result in all_results:
            url = result['url']
            if self._should_exclude(url):
                continue
            by_kind = defaultdict(list)
            for token, urls in blocked.items():
                if url in urls:
                    by_kind[AI_BOTS[token]['kind']].append(token)
            for kind, tokens in by_kind.items():
                check = BOT_KINDS[kind]['check']
                if check:
                    issues.append(ai_issue(url, check, 'robots.txt blocks ' + ', '.join(
                        f"{token} ({AI_BOTS[token]['used_by']})" for token in tokens)))

        with self.issues_lock:
            self.detected_issues.extend(issues)

    def _is_indexable_page(self, result):
        """An internal HTML page that loaded without redirecting, isn't noindex
        and doesn't canonicalise to a different URL"""
        return (is_internal_html_page(result) and not result.get('redirected_to')
                and not self._should_exclude(result['url'])
                and 'noindex' not in page_robots_directives(result)
                and canonical_elsewhere(result) is None)

    def detect_duplication_issues(self, all_results, similarity_threshold=0.85):
        """
        Flag pages whose title, meta description, H1 and length are near-identical
        to other crawled pages.

        Emits at most ONE issue per page — how many near-duplicates it has and
        the closest few — so a site of N templated pages yields N issues, not N².
        (The previous pairwise version produced two issues per matching pair:
        700k rows for a 1,000-page site, which took minutes to compute and
        locked SQLite for over a minute while saving.)

        Only HTML pages that returned 200 and carry a title or description are
        considered. Pages with identical normalised title/description/H1 are
        grouped first; one representative per group is scored and the result
        applies to every member. Candidate pairs between groups come from a
        prefix filter on title tokens: with the default 0.85 threshold a pair
        needs title similarity of at least 0.57, so any qualifying pair must
        share one of each title's rarest few tokens. Sites with distinct titles
        therefore score very few pairs; templated sites score many, but each
        comparison is a handful of set operations, and the whole pass stops
        after DUPLICATION_TIME_BUDGET_SECONDS with partial results rather than
        holding the crawl in "finishing up".

        Args:
            all_results: List of all crawled result dictionaries
            similarity_threshold: Minimum similarity ratio to flag as duplicate (0.0-1.0)
        """
        started = time.monotonic()
        self.duplication_truncated = False

        pages = self._duplication_candidates(all_results)
        if len(pages) < 2:
            return

        # 1. Group pages with identical title / description / H1.
        group_index = {}
        groups = []
        for page in pages:
            signature = (page['title'], page['desc'], page['h1'])
            idx = group_index.get(signature)
            if idx is None:
                idx = group_index[signature] = len(groups)
                groups.append([])
            groups[idx].append(page)
        representatives = [group[0] for group in groups]

        # 2. Score candidate pairs of groups, each pair once.
        similar = defaultdict(list)  # group idx -> [(similarity, other group idx)]
        compared = 0
        for i, j in self._duplication_candidate_pairs(representatives, similarity_threshold):
            compared += 1
            if compared % 2000 == 0 and time.monotonic() - started > DUPLICATION_TIME_BUDGET_SECONDS:
                self.duplication_truncated = True
                print(f"Duplication detection stopped after {DUPLICATION_TIME_BUDGET_SECONDS}s "
                      f"and {compared} comparisons; duplicate results are partial")
                break
            similarity = self._duplication_similarity(
                representatives[i], representatives[j], similarity_threshold)
            if similarity is not None:
                similar[i].append((similarity, j))
                similar[j].append((similarity, i))

        # 3. One issue per page.
        issues = []
        for idx, group in enumerate(groups):
            partners = [(1.0, member['url'], True) for member in group]
            for similarity, j in similar.get(idx, ()):
                partners.extend((similarity, other['url'], False) for other in groups[j])
            if len(partners) < 2:
                continue
            partners.sort(key=lambda p: (-p[0], p[1]))
            total = len(partners) - 1  # everything except the page itself
            for page in group:
                examples = [p for p in partners if p[1] != page['url']][:DUPLICATION_EXAMPLES_PER_PAGE]
                described = '; '.join(
                    f"{url} (identical title, description and H1)" if identical
                    else f"{url} ({similarity * 100:.1f}% similar)"
                    for similarity, url, identical in examples
                )
                more = total - len(examples)
                issues.append({
                    'url': page['url'],
                    'type': 'warning',
                    'category': 'Duplication',
                    'issue': 'Duplicate Content Detected',
                    'details': f"{total} similar page{'s' if total != 1 else ''}: {described}"
                               + (f" (+{more} more)" if more > 0 else '')
                })

        # Add all detected duplication issues
        with self.issues_lock:
            self.detected_issues.extend(issues)

    def _duplication_candidates(self, all_results):
        """Pages eligible for duplicate detection, with normalised fields and token sets."""
        pages = []
        for result in all_results:
            url = result.get('url', '')
            if not url or self._should_exclude(url):
                continue
            status = result.get('status_code')
            if status is not None and status != 200:
                continue
            content_type = (result.get('content_type') or '').lower()
            if content_type and 'html' not in content_type:
                continue
            page = self._duplication_page(result)
            if not page['title'] and not page['desc']:
                continue
            pages.append(page)
        return pages

    def _duplication_page(self, result):
        title = self._normalise_text(result.get('title'))
        desc = self._normalise_text(result.get('meta_description'))
        h1 = self._normalise_text(result.get('h1'))
        return {
            'url': result.get('url', ''),
            'title': title,
            'desc': desc,
            'h1': h1,
            'title_tokens': self._tokens(title),
            'desc_tokens': self._tokens(desc),
            'h1_tokens': self._tokens(h1),
            'word_count': result.get('word_count') or 0,
        }

    @staticmethod
    def _first_text(value):
        """A field's text; H1 may arrive as a list."""
        if isinstance(value, list):
            value = next((v for v in value if isinstance(v, str) and v.strip()), '')
        return value if isinstance(value, str) else ''

    @classmethod
    def _normalise_text(cls, value):
        """Lower-cased, whitespace-collapsed text."""
        return ' '.join(cls._first_text(value).lower().split())

    @staticmethod
    def _tokens(text):
        return frozenset(t for t in _TOKEN_SPLIT.split(text) if t)

    def _duplication_candidate_pairs(self, representatives, threshold):
        """Yield (i, j) with i < j for the pairs worth scoring.

        Uses a prefix filter on title tokens. The title carries 35% of the
        score, so a pair can only reach `threshold` if the titles' Dice
        similarity is at least (threshold - 0.65) / 0.35. Dice(A, B) >= t
        implies |A ∩ B| >= t·|A| / (2 - t); ordering tokens by global rarity,
        two such sets must share a token within the first
        |A| - need + 1 tokens of each, so indexing those prefixes finds every
        qualifying pair while skipping most pairs of distinct titles.
        """
        weights = self._DUP_WEIGHTS
        min_title = (threshold - (1.0 - weights['title'])) / weights['title']
        count = len(representatives)

        if min_title <= 0:
            # Threshold is too low for the title to prune anything.
            for i in range(count):
                for j in range(i + 1, count):
                    yield i, j
            return

        frequency = defaultdict(int)
        for rep in representatives:
            for token in rep['title_tokens']:
                frequency[token] += 1

        index = defaultdict(list)
        prefixes = []
        for i, rep in enumerate(representatives):
            ordered = sorted(rep['title_tokens'], key=lambda t: (frequency[t], t))
            need = math.ceil(min_title * len(ordered) / (2 - min_title))
            prefix = ordered[:max(1, len(ordered) - need + 1)] if ordered else []
            prefixes.append(prefix)
            for token in prefix:
                index[token].append(i)

        for i, prefix in enumerate(prefixes):
            seen = set()
            for token in prefix:
                for j in index[token]:
                    if j < i and j not in seen:
                        seen.add(j)
                        yield j, i

    def _duplication_similarity(self, a, b, threshold):
        """Weighted similarity of two candidate pages, or None once it is clear
        the score cannot reach `threshold` (so cheap signals short-circuit)."""
        weights = self._DUP_WEIGHTS
        score = self._dice(a['title_tokens'], b['title_tokens']) * weights['title']
        if score + weights['desc'] + weights['h1'] + weights['word_count'] < threshold:
            return None
        score += self._dice(a['desc_tokens'], b['desc_tokens']) * weights['desc']
        if score + weights['h1'] + weights['word_count'] < threshold:
            return None
        score += self._dice(a['h1_tokens'], b['h1_tokens']) * weights['h1']
        if score + weights['word_count'] < threshold:
            return None
        score += self._length_similarity(a['word_count'], b['word_count']) * weights['word_count']
        return score if score >= threshold else None

    @staticmethod
    def _dice(tokens_a, tokens_b):
        """Dice coefficient of two token sets (0.0 when either is empty)."""
        if not tokens_a or not tokens_b:
            return 0.0
        return 2.0 * len(tokens_a & tokens_b) / (len(tokens_a) + len(tokens_b))

    @staticmethod
    def _length_similarity(count_a, count_b):
        if not count_a or not count_b:
            return 0.0
        return min(count_a, count_b) / max(count_a, count_b)

    def _calculate_content_similarity(self, result1, result2):
        """
        Similarity of two page results in [0.0, 1.0]: word-level Dice
        similarity of title (35%), meta description (35%) and H1 (20%), plus
        the ratio of their word counts (10%).
        """
        return self._duplication_similarity(
            self._duplication_page(result1), self._duplication_page(result2), 0.0)

    def _should_exclude(self, url):
        """Check if URL should be excluded from issue detection"""
        parsed = urlparse(url)
        path = parsed.path

        for pattern in self.exclusion_patterns:
            if '*' in pattern:
                if fnmatch(path, pattern):
                    return True
            elif path == pattern or path.startswith(pattern.rstrip('*')):
                return True

        return False

    def _get_status_code_message(self, status_code):
        """Get descriptive message for HTTP status codes"""
        messages = {
            400: 'Bad Request',
            401: 'Unauthorized',
            403: 'Forbidden',
            404: 'Not Found',
            405: 'Method Not Allowed',
            406: 'Not Acceptable',
            408: 'Request Timeout',
            410: 'Gone',
            429: 'Too Many Requests',
            500: 'Internal Server Error',
            501: 'Not Implemented',
            502: 'Bad Gateway',
            503: 'Service Unavailable',
            504: 'Gateway Timeout',
            505: 'HTTP Version Not Supported'
        }
        return messages.get(status_code, f'HTTP {status_code} Error')

    def get_issues(self):
        """Get all detected issues"""
        with self.issues_lock:
            return self.detected_issues.copy()

    def reset(self):
        """Reset detected issues"""
        with self.issues_lock:
            self.detected_issues.clear()
