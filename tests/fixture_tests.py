"""Deterministic crawler tests against local fixture sites.

Runs WebCrawler in-process against tiny HTTP servers started by this script,
so every behaviour is checked without touching a third-party site. No test
framework and no network access required:

    python tests/fixture_tests.py

Each test pins down a bug that reached production once, so a failure here
means a real regression rather than a flaky expectation:

  1. relative links on a page reached through a cross-domain redirect resolve
     against the site that served the content, not the URL that was requested
  2. images hosted off-domain (CDNs) and external links are HEAD-checked and
     listed even while "crawl external links" is off, without crawling
     off-domain pages
  3. sitemap discovery does not block start_crawl, and a crawl does not
     finish while discovery is still feeding the queue
  4. max_urls budgets pages actually fetched, not the rows synthesized from
     image HEAD checks
  5. the event journal never announces an update before the row it refers to,
     and never repeats a row, even while a client polls during the crawl
"""
import http.server
import os
import sys
import threading
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.crawler import WebCrawler  # noqa: E402

# Fixture servers bind here upward. Override if these clash locally.
BASE_PORT = int(os.environ.get('FIXTURE_BASE_PORT', '8911'))

PASSED, FAILED = [], []


def result(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")


def serve(handler, port):
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def html(body):
    return f'<html><head><title>t</title></head><body>{body}</body></html>'


def png():
    return b'\x89PNG\r\n\x1a\n' + b'x' * 40


def make_handler(routes, hits=None, slow_paths=(), slow_seconds=0.0, received=None):
    """Build a handler serving `routes`: path -> html str, bytes, (code, location),
    or {'status', 'body', 'headers'} for a page with its own status or headers.
    POSTs are served the same routes; `received` collects every request's
    path, headers and body."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _send(self, code, body=b'', ctype='text/html', headers=None):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body and self.command != 'HEAD':
                self.wfile.write(body)

        def do_GET(self):
            if hits is not None:
                hits.append(f'{self.command} {self.path}')
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if received is not None:
                received.append({'path': self.path, 'headers': self.headers, 'body': body})
            if any(self.path.startswith(p) for p in slow_paths):
                time.sleep(slow_seconds)

            route = routes.get(self.path)
            if route is None:
                self._send(404, b'not found')
            elif isinstance(route, tuple):
                self.send_response(route[0])
                self.send_header('Location', route[1])
                self.send_header('Content-Length', '0')
                self.end_headers()
            elif isinstance(route, dict):
                self._send(route.get('status', 200), route.get('body', '').encode(),
                           headers=route.get('headers'))
            elif isinstance(route, bytes):
                ctype = 'image/png' if self.path.endswith('.png') else 'application/octet-stream'
                self._send(200, route, ctype)
            else:
                self._send(200, route.encode())

        do_HEAD = do_POST = do_GET

    return Handler


def new_crawler(**config):
    """A crawler with fast, fixture-friendly settings."""
    crawler = WebCrawler()
    crawler.config.update({
        'delay': 0.02, 'concurrency': 5, 'max_urls': 200, 'max_depth': 4,
        'crawl_external': False, 'crawl_images': False,
        'discover_sitemaps': False, 'respect_robots': False,
    })
    crawler.config.update(config)
    return crawler


def finish(crawler, started, timeout=120):
    """Wait for a started crawl to complete."""
    ok, message = started
    assert ok, message
    deadline = time.time() + timeout
    while crawler.is_running and time.time() < deadline:
        time.sleep(0.1)
    assert not crawler.is_running, 'crawl did not finish within timeout'
    return crawler


def crawl(url, timeout=120, **config):
    """Run a crawl to completion and return the crawler for inspection."""
    crawler = new_crawler(**config)
    return finish(crawler, crawler.start_crawl(url), timeout)


def test_cross_domain_redirect():
    """A page that redirects off-domain must not mint internal URLs."""
    site_a, site_b = BASE_PORT, BASE_PORT + 1
    a = serve(make_handler({
        '/': html(f'<a href="/go">offsite</a><a href="/local.html">local</a>'),
        '/local.html': html('<p>local page</p>'),
        '/go': (302, f'http://127.0.0.1:{site_b}/landing'),
    }), site_a)
    b_hits = []
    b = serve(make_handler({
        '/landing': html('<a href="/issues">issues</a><a href="/pulls">pulls</a>'),
    }, hits=b_hits), site_b)
    try:
        crawler = crawl(f'http://127.0.0.1:{site_a}/')
        urls = {r['url'] for r in crawler.crawl_results}
        targets = {l['target_url'] for l in crawler.link_manager.all_links}

        phantoms = [u for u in urls
                    if f':{site_a}' in u and ('/issues' in u or '/pulls' in u)]
        result('redirect: no phantom internal URLs', not phantoms, str(phantoms[:2]))

        go = next(r for r in crawler.crawl_results if r['url'].endswith('/go'))
        result('redirect: chain and destination recorded',
               bool(go.get('redirects'))
               and go.get('redirected_to', '').endswith('/landing'))

        result('redirect: offsite links attributed to the other host',
               f'http://127.0.0.1:{site_b}/issues' in targets)
        # (with crawl_external off, offsite links are HEAD-checked for the
        # External tab, but never fetched)
        result('redirect: offsite content does not enter the queue',
               'GET /issues' not in b_hits, str(b_hits))
    finally:
        a.shutdown()
        b.shutdown()


def test_external_images():
    """With crawl_external off, off-domain images (CDNs) and external links are
    HEAD-checked and listed, but the external site is never crawled."""
    site, other = BASE_PORT + 2, BASE_PORT + 3
    other_hits = []
    off = f'http://127.0.0.1:{other}'
    a = serve(make_handler({
        '/': html(f'<img src="/in.png"><img src="{off}/out.png">'
                  f'<a href="{off}/page">offsite page</a>'
                  f'<a href="{off}/gone">dead link</a>'
                  f'<a href="{off}/no-head">HEAD refused</a>'
                  f'<a href="javascript:void(0)">js</a>'),
        '/in.png': png(),
    }), site)

    class NoHead(make_handler({'/out.png': png(), '/page': html('<a href="/deeper">x</a>'),
                               '/no-head': html('')}, hits=other_hits)):
        def do_HEAD(self):
            if self.path == '/no-head':
                other_hits.append(f'HEAD {self.path}')
                self._send(405)
            else:
                self.do_GET()

    b = serve(NoHead, other)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')
        rows = {r['url']: r for r in crawler.crawl_results}
        result('images: offsite image listed as a row', f'{off}/out.png' in rows)
        result('images: offsite image HEAD-checked', 'HEAD /out.png' in other_hits, str(other_hits))
        result('images: same-domain image still listed',
               f'http://127.0.0.1:{site}/in.png' in rows)

        ext = rows.get(f'{off}/page')
        result('external: link listed as an external row',
               ext is not None and ext['is_internal'] is False and ext['status_code'] == 200,
               str(ext and (ext['is_internal'], ext['status_code'])))
        result('external: link HEAD-checked, never fetched',
               'HEAD /page' in other_hits and 'GET /page' not in other_hits
               and not any('/deeper' in h for h in other_hits), str(other_hits))
        result('external: dead link reports 404',
               rows.get(f'{off}/gone', {}).get('status_code') == 404)
        result('external: HEAD 405 falls back to GET',
               rows.get(f'{off}/no-head', {}).get('status_code') == 200, str(other_hits))
        link_statuses = {l['target_url']: l['target_status'] for l in crawler.link_manager.all_links}
        result('external: link status shown in Links tab',
               link_statuses.get(f'{off}/gone') == 404, str(link_statuses))
        result('external: link rows flagged link_check_only (left out of Overview)',
               ext is not None and ext.get('link_check_only') is True)
        result('external: offsite image rows not flagged (stay in Overview)',
               rows.get(f'{off}/out.png', {}).get('link_check_only') is False)
        result('external: crawled pages not flagged',
               not rows[f'http://127.0.0.1:{site}/'].get('link_check_only'))
        result('external: non-http links ignored',
               not any(u.startswith('javascript:') for u in rows))

        other_hits.clear()
        crawler = crawl(f'http://127.0.0.1:{site}/', crawl_external=True)
        result('external: crawled for real when crawl_external is on',
               'GET /page' in other_hits and 'GET /deeper' in other_hits, str(other_hits))
    finally:
        a.shutdown()
        b.shutdown()


def test_sitemap_discovery_is_async():
    """Discovery runs in the background but still gates completion."""
    site = BASE_PORT + 4
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?><urlset>'
               f'<url><loc>http://127.0.0.1:{site}/only-in-sitemap.html</loc></url>'
               '</urlset>')
    a = serve(make_handler({
        '/': html('<p>root</p>'),
        '/only-in-sitemap.html': html('<p>reachable only via sitemap</p>'),
        '/sitemap.xml': sitemap,
    }, slow_paths=('/sitemap',), slow_seconds=2.0), site)
    try:
        crawler = WebCrawler()
        crawler.config.update({'delay': 0.02, 'max_urls': 50, 'max_depth': 3,
                               'respect_robots': False, 'discover_sitemaps': True})
        started = time.time()
        ok, message = crawler.start_crawl(f'http://127.0.0.1:{site}/')
        latency = time.time() - started
        assert ok, message

        deadline = time.time() + 60
        while crawler.is_running and time.time() < deadline:
            time.sleep(0.1)
        urls = {r['url'] for r in crawler.crawl_results}

        result('sitemap: start_crawl returns immediately',
               latency < 0.5, f'{latency * 1000:.0f}ms')
        result('sitemap: crawl waits for slow discovery',
               f'http://127.0.0.1:{site}/only-in-sitemap.html' in urls)
    finally:
        a.shutdown()


def test_max_urls_counts_pages_not_images():
    """Synthesized image rows must not consume the crawl budget."""
    site = BASE_PORT + 5
    routes = {'/': html(''.join(f'<img src="/img{i}.png">' for i in range(30))
                        + ''.join(f'<a href="/p{i}.html">page</a>' for i in range(10)))}
    for i in range(30):
        routes[f'/img{i}.png'] = png()
    for i in range(10):
        routes[f'/p{i}.html'] = html(f'<p>page {i}</p><a href="/">home</a>')

    a = serve(make_handler(routes), site)
    try:
        limit, concurrency = 5, 5
        crawler = crawl(f'http://127.0.0.1:{site}/', max_urls=limit, concurrency=concurrency)
        pages = [r for r in crawler.crawl_results
                 if (r.get('content_type') or '').startswith('text/html')]
        # The submit loop fills every worker slot before rechecking the budget,
        # so a crawl may overshoot by up to `concurrency` pages. What must not
        # happen is image rows eating the budget, which used to leave a single
        # real page crawled no matter how high the limit was.
        result('max_urls: budgets fetched pages, not image rows',
               limit <= len(pages) <= limit + concurrency,
               f'{len(pages)} pages, {len(crawler.crawl_results)} rows total')
        result('max_urls: image rows still captured',
               len(crawler.crawl_results) > len(pages))
    finally:
        a.shutdown()


def test_event_ordering_under_polling():
    """The journal stays consistent while a client polls mid-crawl."""
    site = BASE_PORT + 6
    routes = {'/': html(''.join(f'<a href="/p{i}.html">p</a>' for i in range(40)))}
    for i in range(40):
        routes[f'/p{i}.html'] = html(
            f'<img src="/i{i}.png">'
            f'<a href="/p{(i + 1) % 40}.html">next</a><a href="/">home</a>')
        routes[f'/i{i}.png'] = png()

    a = serve(make_handler(routes), site)
    try:
        crawler = WebCrawler()
        crawler.config.update({'delay': 0.0, 'concurrency': 8, 'max_urls': 40,
                               'max_depth': 3, 'respect_robots': False,
                               'discover_sitemaps': False})
        problems = []
        seen_urls, seen_links = set(), set()
        cursor = {'seq': 0}

        def poll():
            """Mirror what the browser client does on every poll."""
            while crawler.is_running or cursor['seq'] < len(crawler.event_log._events):
                _, events, latest, _ = crawler.event_log.events_since(
                    cursor['seq'], crawler.event_log.epoch)
                cursor['seq'] = latest
                for event in events:
                    kind, data = event['kind'], event['data']
                    if kind == 'url':
                        if data['url'] in seen_urls:
                            problems.append(('duplicate url', data['url']))
                        seen_urls.add(data['url'])
                    elif kind == 'url_update':
                        if data['url'] not in seen_urls:
                            problems.append(('url_update before url', data['url']))
                    elif kind == 'link':
                        key = data['source_url'] + '|' + data['target_url']
                        if key in seen_links:
                            problems.append(('duplicate link', key))
                        seen_links.add(key)
                    elif kind == 'link_update':
                        key = data['source_url'] + '|' + data['target_url']
                        if key not in seen_links:
                            problems.append(('link_update before link', key))
                # this is the call the status route makes, and it emits events
                crawler.get_status_light()
                time.sleep(0.05)

        ok, message = crawler.start_crawl(f'http://127.0.0.1:{site}/')
        assert ok, message
        poller = threading.Thread(target=poll, daemon=True)
        poller.start()

        deadline = time.time() + 90
        while crawler.is_running and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(1.5)  # let the poller drain the tail

        result('events: no ordering violations under concurrent polling',
               not problems, f'{len(problems)} found: {problems[:3]}')
        result('events: replayed client state matches the crawler',
               seen_urls == {r['url'] for r in crawler.crawl_results},
               f'{len(seen_urls)} client rows vs {len(crawler.crawl_results)} crawler rows')
    finally:
        a.shutdown()


def test_discovered_counts_image_rows():
    """Issue #94: the discovered counter ignored synthesized image rows."""
    site = BASE_PORT + 7
    routes = {'/': html(''.join(f'<img src="/i{i}.png">' for i in range(6))
                        + '<a href="/p1.html">p</a>'),
              '/p1.html': html('<p>page</p>')}
    for i in range(6):
        routes[f'/i{i}.png'] = png()

    a = serve(make_handler(routes), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')
        status = crawler.get_status_light()
        discovered = status['stats']['discovered']
        crawled = status['stats']['crawled']
        rows = len(crawler.crawl_results)

        result('discovered counts every row, images included',
               discovered >= rows,
               f'discovered={discovered} crawled={crawled} rows={rows}')
        result('discovered is never behind crawled',
               discovered >= crawled, f'{discovered} vs {crawled}')
        result('image rows were actually synthesized',
               rows > 2, f'{rows} rows')
    finally:
        a.shutdown()


def test_js_response_time_excludes_render_wait():
    """Issue #93: response_time included js_wait_time, flagging every page."""
    if not _playwright_available():
        result('js response time excludes the render wait', True,
               'skipped, playwright not installed')
        return

    site = BASE_PORT + 8
    a = serve(make_handler({'/': html('<h1>fast</h1>')}), site)
    try:
        wait = 3
        crawler = crawl(f'http://127.0.0.1:{site}/', max_urls=1, enable_javascript=True,
                        js_wait_time=wait, js_max_concurrent_pages=1, timeout=180)
        pages = [r for r in crawler.crawl_results
                 if (r.get('content_type') or '').startswith('text/html')]
        if not pages:
            result('js response time excludes the render wait', False, 'no page crawled')
            return

        page = pages[0]
        response_ms = page.get('response_time') or 0
        render_ms = page.get('render_time') or 0

        result('response_time excludes the render wait',
               response_ms < wait * 1000, f'{response_ms}ms with a {wait}s wait')
        result('response_time stays under the slow threshold for a fast page',
               response_ms < 3000, f'{response_ms}ms')
        result('render_time is reported separately and includes the wait',
               render_ms >= wait * 1000, f'{render_ms}ms')

        from src.core.issue_detector import IssueDetector
        detector = IssueDetector([])
        detector.detect_issues(page)
        slow = [i for i in detector.detected_issues
                if i.get('issue') == 'Slow Response Time']
        result('a fast page is not flagged as slow',
               not slow, str(slow[:1]))
    finally:
        a.shutdown()


def test_duplicate_detection_is_linear():
    """A site of N near-identical pages (suburb "doorway" pages) used to hold
    the crawl in "finishing up" for minutes and produce N² duplicate issues
    (700k rows for 1,000 pages), locking SQLite while they were saved. It must
    finish promptly with exactly one issue per page."""
    from collections import Counter
    from src.core.issue_detector import IssueDetector

    site = BASE_PORT + 9
    count = 60
    routes = {'/': html(''.join(f'<a href="/area-{i}/">{i}</a>' for i in range(count)))}
    for i in range(count):
        routes[f'/area-{i}/'] = (
            f'<html><head><title>Asbestos Removal Area{i} | Get a Free Quote Today!</title>'
            f'<meta name="description" content="Proudly known as the best Asbestos Removal '
            f'experts in Area{i}! Enjoy peace of mind."></head>'
            f'<body><h1>Asbestos Removal Area{i}</h1><p>{"word " * 300}</p></body></html>')
    a = serve(make_handler(routes), site)
    try:
        started = time.time()
        crawler = crawl(f'http://127.0.0.1:{site}/', max_urls=count + 1,
                        enable_duplication_check=True, duplication_threshold=0.85)
        elapsed = time.time() - started
        dupes = [i for i in crawler.issue_detector.get_issues()
                 if i.get('issue') == 'Duplicate Content Detected']
        per_url = Counter(i['url'] for i in dupes)
        result('one duplicate issue per templated page',
               len(dupes) == count and per_url and max(per_url.values()) == 1,
               f'{len(dupes)} issues for {count} pages')
        result('each issue counts the other templated pages',
               bool(dupes) and all(f'{count - 1} similar pages' in i['details'] for i in dupes),
               dupes[0]['details'][:100] if dupes else 'no issues')
        result('the unrelated index page is not flagged',
               f'http://127.0.0.1:{site}/' not in per_url)
        result('crawl finished promptly', elapsed < 60, f'{elapsed:.1f}s')
    finally:
        a.shutdown()

    # Scale check on the detector alone: 1,500 templated pages used to take
    # ~10 minutes of pairwise SequenceMatcher calls.
    pages = [{'url': f'https://x.au/p{i}/', 'status_code': 200, 'content_type': 'text/html',
              'title': f'Asbestos Removal Area{i} | Get a Free Quote Today!',
              'meta_description': f'Proudly known as the best Asbestos Removal experts in Area{i}!',
              'h1': f'Asbestos Removal Area{i}', 'word_count': 1000 + i % 9} for i in range(1500)]
    detector = IssueDetector()
    started = time.time()
    detector.detect_duplication_issues(pages, 0.85)
    elapsed = time.time() - started
    result('1,500 templated pages produce 1,500 issues quickly',
           len(detector.get_issues()) == 1500 and elapsed < 30 and not detector.duplication_truncated,
           f'{len(detector.get_issues())} issues in {elapsed:.1f}s')


def test_export_formats_apply_to_every_data_type():
    """The format picked in Settings must apply to URLs, links AND issues.
    Links/issues used to fall back to CSV for XML, and "Excel (XLSX)" was
    offered in the UI but never implemented, so it silently produced CSV."""
    import base64
    import json
    import types
    import xml.etree.ElementTree as ET
    from io import BytesIO

    os.environ.setdefault('LOCAL_MODE', 'true')
    import main as app_module
    from openpyxl import load_workbook
    from src.core.issue_detector import IssueDetector

    urls = [{'url': 'https://e.au/', 'status_code': 200, 'title': 'Home', 'h2': ['a', 'b']},
            {'url': 'https://e.au/p', 'status_code': 404, 'title': 'Gone', 'h2': []}]
    links = [{'source_url': 'https://e.au/', 'target_url': 'https://e.au/p', 'anchor_text': 'p',
              'is_internal': True, 'target_domain': 'e.au', 'target_status': 404, 'placement': 'body'}]
    issues = [{'url': 'https://e.au/p', 'type': 'error', 'category': 'HTTP', 'issue': 'Not Found',
               'details': '404'}]
    formats = (('csv', 'text/csv'), ('json', 'application/json'),
               ('xml', 'application/xml'), ('xlsx', app_module.XLSX_MIMETYPE))

    def parses(fmt, data, expect_rows):
        if fmt == 'xlsx':
            rows = list(load_workbook(BytesIO(data)).active.iter_rows(values_only=True))
            return len(rows) == expect_rows + 1
        if fmt == 'xml':
            root = ET.fromstring(data)
            return len(root[0]) == expect_rows
        if fmt == 'json':
            return len(json.loads(data)['data']) == expect_rows
        return data.decode().count('\n') == expect_rows + 1

    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'fixture'
        s['tier'] = 'admin'

    # Streaming endpoint (active or loaded crawl): seed this session's crawler.
    client.get('/api/crawl_status?stats_only=1')
    with client.session_transaction() as s:
        crawler = app_module.crawler_instances[s['session_id']]['crawler']
    crawler.crawl_results = urls
    crawler.issue_detector = IssueDetector()
    crawler.issue_detector.detected_issues = list(issues)
    crawler.link_manager = types.SimpleNamespace(all_links=[dict(l) for l in links])
    for data_type, expect_rows in (('urls', 2), ('links', 1), ('issues', 1)):
        for fmt, mimetype in formats:
            resp = client.get(f'/api/export_stream?format={fmt}&type={data_type}&fields=url,title,h2')
            disposition = resp.headers.get('Content-Disposition', '')
            ok = (resp.status_code == 200 and resp.mimetype == mimetype
                  and disposition.endswith('.' + fmt) and parses(fmt, resp.data, expect_rows))
            result(f'streaming {data_type} export honours {fmt}', ok,
                   f'{resp.status_code} {resp.mimetype} {disposition}')

    # Legacy endpoint (crawl loaded from a file, data lives in the browser).
    for fmt, mimetype in formats:
        resp = client.post('/api/export_data', json={
            'format': fmt, 'fields': ['url', 'title', 'links_detailed', 'issues_detected'],
            'localData': {'urls': urls, 'links': links, 'issues': issues}})
        body = resp.get_json() or {}
        files = body.get('files') or []
        ok = body.get('success') and len(files) == 3 and all(
            f['mimetype'] == mimetype and f['filename'].endswith('.' + fmt) for f in files)
        if ok and fmt == 'xlsx':
            ok = all(f.get('encoding') == 'base64'
                     and parses('xlsx', base64.b64decode(f['content']), 1 if 'export' not in f['filename'] else 2)
                     for f in files)
        result(f'legacy export honours {fmt} for every file', bool(ok),
               ', '.join(f['filename'] for f in files) if files else str(body)[:120])


def test_empty_alt_is_not_missing_alt():
    """Issue #95: alt="" is correct for decorative images, not a defect."""
    site = BASE_PORT + 11
    page = html('<img src="/a.png" alt="A hand holding a crawler report">'
                '<img src="/b.png" alt="">'
                '<img src="/c.png">')
    routes = {'/': page}
    for name in ('a', 'b', 'c'):
        routes[f'/{name}.png'] = png()

    a = serve(make_handler(routes), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')
        root = next((r for r in crawler.crawl_results
                     if r['url'].rstrip('/').endswith(str(site))), None)
        if root is None:
            root = next(r for r in crawler.crawl_results
                        if (r.get('content_type') or '').startswith('text/html'))

        images = {img['src'].rsplit('/', 1)[-1]: img for img in root.get('images', [])}
        result('alt extraction keeps all three images',
               len(images) == 3, str(sorted(images)))

        result('described image records its alt',
               images['a.png'].get('alt') == 'A hand holding a crawler report'
               and images['a.png'].get('has_alt') is True)
        result('decorative image is marked as having an alt',
               images['b.png'].get('alt') == '' and images['b.png'].get('has_alt') is True)
        result('image with no attribute is marked as missing',
               images['c.png'].get('has_alt') is False)

        from src.core.issue_detector import IssueDetector
        detector = IssueDetector([])
        detector.detect_issues(root)
        alt_issues = [i for i in detector.detected_issues
                      if i.get('issue') == 'Images Without Alt Text']

        result('exactly one image is reported, not two',
               len(alt_issues) == 1 and '1 of 3' in alt_issues[0]['details'],
               alt_issues[0]['details'] if alt_issues else 'no issue raised')
        result('the decorative image is called out as fine',
               bool(alt_issues) and 'decorative' in alt_issues[0]['details'],
               alt_issues[0]['details'] if alt_issues else '')

        # a page where every image is correctly marked must raise nothing
        clean = dict(root)
        clean['images'] = [images['a.png'], images['b.png']]
        detector.detected_issues = []
        detector.detect_issues(clean)
        result('correct markup raises no alt warning at all',
               not [i for i in detector.detected_issues
                    if i.get('issue') == 'Images Without Alt Text'])
    finally:
        a.shutdown()


def issues_for(crawler, path):
    return [i for i in crawler.issue_detector.detected_issues if i['url'].endswith(path)]


def test_crawl_delay_applies_without_js():
    """Issue #110: the crawl delay was only honoured with JS rendering on, so
    HTTP crawls fired every request at once regardless of the setting."""
    site = BASE_PORT + 12
    pages = [f'/p{n}' for n in range(5)]
    routes = {'/': html(''.join(f'<a href="{p}">{p}</a>' for p in pages))}
    routes.update({p: html('<p>page</p>') for p in pages})

    a = serve(make_handler(routes), site)
    try:
        started = time.time()
        crawler = crawl(f'http://127.0.0.1:{site}/', delay=0.4, concurrency=5)
        elapsed = time.time() - started
        # 6 pages spaced 0.4s apart cannot finish in under 5 gaps' worth
        result('delay spaces out HTTP requests', elapsed >= 1.9 and len(crawler.crawl_results) == 6,
               f'{elapsed:.2f}s for {len(crawler.crawl_results)} pages')
    finally:
        a.shutdown()


def test_failed_pages_report_only_the_failure():
    """A page that failed to load used to get ~11 extra issues (missing title,
    H1, viewport, ...) piled on top of the real error."""
    site = BASE_PORT + 13
    a = serve(make_handler({
        '/': html('<a href="/gone">gone</a><a href="/boom">boom</a>'),
        '/boom': {'status': 500, 'body': 'server error'},
    }), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')
        gone = [i['issue'] for i in issues_for(crawler, '/gone')]
        boom = [i['issue'] for i in issues_for(crawler, '/boom')]
        result('404 page reports only the 404', gone == ['404 Client Error'], str(gone))
        result('500 page reports only the 500', boom == ['500 Server Error'], str(boom))

        from src.core.issue_detector import IssueDetector
        from src.core.seo_extractor import SEOExtractor
        detector = IssueDetector([])
        detector.detect_issues(SEOExtractor.create_empty_result(
            'http://example.test/', 0, 0, 'timed out', error_type='timeout'))
        timeout = [i['issue'] for i in detector.detected_issues]
        result('unreachable URL reports only the fetch error', timeout == ['Request Timeout'], str(timeout))
    finally:
        a.shutdown()


def test_x_robots_tag_header():
    """noindex/nofollow sent as an HTTP header was invisible: only the meta tag
    was read."""
    site = BASE_PORT + 14
    page = html('<p>page</p>')
    a = serve(make_handler({
        '/': html('<a href="/hdr">h</a><a href="/bing">b</a><a href="/gbot">g</a>'),
        '/hdr': {'body': page, 'headers': {'X-Robots-Tag': 'noindex'}},
        '/bing': {'body': page, 'headers': {'X-Robots-Tag': 'bingbot: noindex, nofollow'}},
        '/gbot': {'body': page, 'headers': {'X-Robots-Tag': 'googlebot: nofollow'}},
    }), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')
        hdr_row = next(r for r in crawler.crawl_results if r['url'].endswith('/hdr'))
        result('header value recorded on the row', hdr_row.get('x_robots_tag') == 'noindex',
               repr(hdr_row.get('x_robots_tag')))

        hdr = {i['issue']: i['details'] for i in issues_for(crawler, '/hdr')}
        result('header noindex is reported, naming the header',
               'X-Robots-Tag header' in hdr.get('Noindex Tag Present', ''), str(hdr.get('Noindex Tag Present')))

        bing = {i['issue'] for i in issues_for(crawler, '/bing')}
        result('directives aimed at another crawler are ignored',
               not bing & {'Noindex Tag Present', 'Nofollow Tag Present'}, str(bing))

        gbot = {i['issue'] for i in issues_for(crawler, '/gbot')}
        result('directives aimed at googlebot count',
               'Nofollow Tag Present' in gbot and 'Noindex Tag Present' not in gbot, str(gbot))

        from src.core.issue_detector import robots_directives
        result('"none" means noindex and nofollow',
               {'noindex', 'nofollow'} <= robots_directives('none'))
        result('max-snippet:-1 is a directive, not a crawler name',
               robots_directives('max-snippet:-1, noindex') >= {'max-snippet', 'noindex'})
    finally:
        a.shutdown()


def test_redirect_chains_and_loops():
    """Redirect chains were recorded but never reported, and a loop was retried
    3 times at 30 redirects each."""
    site = BASE_PORT + 15
    hits = []
    a = serve(make_handler({
        '/': html('<a href="/old">o</a><a href="/single">s</a><a href="/loop-a">l</a>'),
        '/old': (301, '/older'),
        '/older': (301, '/new'),
        '/single': (301, '/new'),
        '/new': html('<p>new</p>'),
        '/loop-a': (302, '/loop-b'),
        '/loop-b': (302, '/loop-a'),
    }, hits=hits), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')

        chain = {i['issue']: i['details'] for i in issues_for(crawler, '/old')}
        result('two-hop chain is reported with every hop',
               'Redirect Chain' in chain and '/older (301)' in chain['Redirect Chain']
               and chain['Redirect Chain'].endswith('/new (200)'), str(chain.get('Redirect Chain')))

        single = {i['issue'] for i in issues_for(crawler, '/single')}
        result('a single redirect is not a chain', 'Redirect Chain' not in single, str(single))

        loop = [i['issue'] for i in issues_for(crawler, '/loop-a')]
        result('loop reported as Redirect Loop and nothing else', loop == ['Redirect Loop'], str(loop))

        loop_row = next(r for r in crawler.crawl_results if r['url'].endswith('/loop-a'))
        result('loop row keeps the hops it followed', len(loop_row.get('redirects') or []) >= 2,
               str(len(loop_row.get('redirects') or [])))

        loop_gets = [h for h in hits if h.startswith('GET /loop-')]
        result('loop is not retried', len(loop_gets) <= 11, f'{len(loop_gets)} requests')
    finally:
        a.shutdown()


def test_js_mode_records_redirects_and_header():
    """The JavaScript path left redirects empty and never read X-Robots-Tag."""
    if not _playwright_available():
        result('js mode redirects and header', True, 'skipped, playwright not installed')
        return

    site = BASE_PORT + 16
    a = serve(make_handler({
        '/old': (301, '/older'),
        '/older': (301, '/new'),
        '/new': {'body': html('<p>new</p>'), 'headers': {'X-Robots-Tag': 'noindex'}},
    }), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/old', max_urls=1, enable_javascript=True,
                        js_wait_time=0, js_max_concurrent_pages=1, timeout=180)
        row = crawler.crawl_results[0]
        hops = [(h['url'].rsplit('/', 1)[-1], h['status_code']) for h in row.get('redirects') or []]
        result('js mode records every redirect hop', hops == [('old', 301), ('older', 301)], str(hops))
        result('js mode records where the chain ends',
               (row.get('redirected_to') or '').endswith('/new'), str(row.get('redirected_to')))
        result('js mode reads the X-Robots-Tag header', row.get('x_robots_tag') == 'noindex',
               repr(row.get('x_robots_tag')))
    finally:
        a.shutdown()


def test_link_checks_respect_crawl_delay():
    """Image and link HEAD checks against the crawled site ignored the crawl
    delay; checks against other hosts (CDNs) are left unthrottled."""
    site, cdn = BASE_PORT + 17, BASE_PORT + 18
    names = [f'i{n}.png' for n in range(4)]
    routes = {'/local': html(''.join(f'<img src="/{n}" alt="x">' for n in names)),
              '/cdn': html(''.join(f'<img src="http://127.0.0.1:{cdn}/{n}" alt="x">' for n in names))}
    routes.update({f'/{n}': png() for n in names})
    a = serve(make_handler(routes), site)
    b = serve(make_handler({f'/{n}': png() for n in names}), cdn)
    try:
        started = time.time()
        crawl(f'http://127.0.0.1:{site}/local', delay=0.4)
        local = time.time() - started
        # the page plus 4 same-site image checks, 0.4s apart
        result('same-site image checks wait for the crawl delay', local >= 1.5, f'{local:.2f}s')

        started = time.time()
        crawl(f'http://127.0.0.1:{site}/cdn', delay=0.4)
        offsite = time.time() - started
        result('off-site image checks are not held back', offsite < 1.2, f'{offsite:.2f}s')
    finally:
        a.shutdown()
        b.shutdown()


def test_page_fetch_is_one_request():
    """Every page cost a HEAD (for the file-size limit) before its GET."""
    site = BASE_PORT + 19
    hits = []
    a = serve(make_handler({
        '/': html('<a href="/page">p</a><a href="/big">b</a>'),
        '/page': html('<p>page</p>'),
        '/big': html('x' * 5000),
    }, hits=hits), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/', max_file_size=2000)
        result('pages are fetched with a single GET', sorted(hits) == ['GET /', 'GET /big', 'GET /page'],
               str(sorted(hits)))
        big = next(r for r in crawler.crawl_results if r['url'].endswith('/big'))
        result('a file over the size limit is still turned away',
               big.get('error_type') == 'file_too_large', str(big.get('error')))
        page = next(r for r in crawler.crawl_results if r['url'].endswith('/page'))
        result('a normal page still records its size', page.get('size', 0) > 0, str(page.get('size')))
    finally:
        a.shutdown()


def page(title, description='', h1='', head=''):
    meta = f'<meta name="description" content="{description}">' if description else ''
    return (f'<html><head><title>{title}</title>{meta}{head}</head>'
            f'<body><h1>{h1}</h1><p>body</p></body></html>')


def test_duplicate_titles_descriptions_h1s():
    """Indexable pages sharing a title, meta description or H1 are flagged;
    noindexed and canonicalised pages are left out, as Screaming Frog does."""
    site = BASE_PORT + 20
    paths = ['/a', '/b', '/c', '/noindex', '/canon', '/self', '/self2']
    a = serve(make_handler({
        '/': html(''.join(f'<a href="{p}">{p}</a>' for p in paths)),
        '/a': page('Retaining Walls Houston', 'Shared description', 'Walls'),
        '/b': page('  retaining walls   HOUSTON ', 'Description B', 'walls'),
        '/c': page('Unique title', 'Shared description', 'Other'),
        '/noindex': page('Retaining Walls Houston', 'Description N', 'N',
                         '<meta name="robots" content="noindex">'),
        '/canon': page('Retaining Walls Houston', 'Description C', 'C',
                       '<link rel="canonical" href="/a">'),
        '/self': page('Self canonical', 'Description S', 'S', '<link rel="canonical" href="/self">'),
        '/self2': page('Self canonical', 'Description S2', 'S2'),
    }), site)
    try:
        crawler = crawl(f'http://127.0.0.1:{site}/')

        def flagged(issue):
            return sorted(i['url'].rsplit('/', 1)[-1] for i in crawler.issue_detector.detected_issues
                          if i['issue'] == issue)

        result('duplicate titles match across case and spacing', flagged('Duplicate Title')
               == ['a', 'b', 'self', 'self2'], str(flagged('Duplicate Title')))
        result('duplicate meta descriptions are flagged', flagged('Duplicate Meta Description')
               == ['a', 'c'], str(flagged('Duplicate Meta Description')))
        result('duplicate H1s are flagged', flagged('Duplicate H1') == ['a', 'b'],
               str(flagged('Duplicate H1')))

        details = next(i['details'] for i in issues_for(crawler, '/a') if i['issue'] == 'Duplicate Title')
        result('details name the shared text and the other page',
               '"Retaining Walls Houston"' in details and details.endswith('/b')
               and '1 other page ' in details, details)
    finally:
        a.shutdown()


def test_list_mode():
    """List mode crawls exactly the listed URLs and follows none of their links."""
    from src.crawler import clean_url_list

    site = BASE_PORT + 21
    base = f'http://127.0.0.1:{site}'
    hits = []
    a = serve(make_handler({
        '/': html('<a href="/p1">p1</a>'),
        '/p1': html('<a href="/p2">p2</a>'),
        '/p2': html('<p>p2</p>'),
        '/p3': html('<p>p3</p>'),
    }, hits=hits), site)
    try:
        crawler = new_crawler()
        finish(crawler, crawler.start_list_crawl([f'{base}/p1', '  ', f'{base}/p3', f'{base}/p1']))
        crawled = sorted(r['url'].rsplit('/', 1)[-1] for r in crawler.crawl_results)
        result('only the listed URLs are crawled, once each', crawled == ['p1', 'p3'], str(crawled))
        result('links on listed pages are not followed', 'GET /p2' not in hits, str(hits))
        result('discovered counts the list', crawler.stats['discovered'] == 2,
               str(crawler.stats['discovered']))

        # the same crawler goes back to spidering for a normal crawl
        hits.clear()
        finish(crawler, crawler.start_crawl(f'{base}/'))
        result('a later spider crawl follows links again', 'GET /p2' in hits, str(hits))

        result('url list cleanup', clean_url_list(
            ['example.com/a', ' ', 'https://example.com/a', 'ftp://x.test/', 'http://b.test/'])
            == ['https://example.com/a', 'http://b.test/'])
        result('an empty list is refused', new_crawler().start_list_crawl(['', ' '])[0] is False)
    finally:
        a.shutdown()


@contextmanager
def temp_crawl_db():
    """Point crawl_db at a throwaway database so tests never touch data/users.db"""
    import tempfile
    from src import crawl_db

    original = crawl_db.DB_FILE
    with tempfile.TemporaryDirectory() as tmp:
        crawl_db.DB_FILE = os.path.join(tmp, 'test.db')
        try:
            crawl_db.init_crawl_tables()
            yield crawl_db
        finally:
            crawl_db.DB_FILE = original


def test_new_fields_survive_the_database():
    """x_robots_tag and redirected_to must round-trip through a saved crawl."""
    with temp_crawl_db() as crawl_db:
        crawl_db.save_url_batch(1, [{
            'url': 'http://example.test/old', 'status_code': 200,
            'x_robots_tag': 'noindex', 'redirected_to': 'http://example.test/new',
            'redirects': [{'url': 'http://example.test/old', 'status_code': 301}],
        }])
        row = crawl_db.load_crawled_urls(1)[0]
        result('x_robots_tag and redirected_to are saved and loaded',
               row.get('x_robots_tag') == 'noindex'
               and row.get('redirected_to') == 'http://example.test/new'
               and row.get('redirects') == [{'url': 'http://example.test/old', 'status_code': 301}],
               str({k: row.get(k) for k in ('x_robots_tag', 'redirected_to', 'redirects')}))


def test_post_crawl_updates_reach_the_database():
    """Fields set after rows were saved (linked_from etc.) must update the saved crawl."""
    with temp_crawl_db() as crawl_db:
        crawl_db.save_url_batch(1, [{'url': 'http://example.test/a', 'status_code': 200,
                                     'linked_from': ['http://example.test/']}])
        crawl_db.update_url_fields(1, [{'url': 'http://example.test/a', 'linked_from': [
            'http://example.test/', 'http://example.test/b']}], ('linked_from',))
        row = crawl_db.load_crawled_urls(1)[0]
        result('a post-crawl update overwrites the saved value',
               row.get('linked_from') == ['http://example.test/', 'http://example.test/b'],
               str(row.get('linked_from')))


@contextmanager
def open_pagerank_api(url):
    """Point the Open PageRank client at a fake API, with a short timeout"""
    from src.integrations import open_pagerank

    original = open_pagerank.BASE_URL, open_pagerank.TIMEOUT
    open_pagerank.BASE_URL, open_pagerank.TIMEOUT = url, 1
    try:
        yield
    finally:
        open_pagerank.BASE_URL, open_pagerank.TIMEOUT = original


def test_open_pagerank_lookup():
    """With a key set, the crawled site gets one Open PageRank lookup when the
    crawl ends, and the result reaches the stats the UI polls."""
    import json

    site, api = BASE_PORT + 22, BASE_PORT + 23
    # The documented /v1/domains/bulk example, for one domain without history
    documented = {'as_of': '2026-06-01', 'count': 1, 'invalid': [], 'results': [
        {'domain': 'github.com', 'found': True, 'open_page_rank': 9.67, 'rank': 30,
         'referring_domains': 282833}]}
    received = []
    a = serve(make_handler({'/': html('<a href="/p">p</a>'), '/p': html('<p>p</p>')}), site)
    fake = serve(make_handler({'/v1/domains/bulk': {'body': json.dumps(documented)}},
                              received=received), api)
    try:
        with open_pagerank_api(f'http://127.0.0.1:{api}'):
            crawler = crawl(f'http://127.0.0.1:{site}/', open_pagerank_api_key='opr_live_test')
            result('one lookup per crawl', [r['path'] for r in received] == ['/v1/domains/bulk'],
                   str([r['path'] for r in received]))
            sent = received[0]
            result('the key is sent as a bearer token',
                   sent['headers']['Authorization'] == 'Bearer opr_live_test',
                   str(sent['headers']['Authorization']))
            result('the crawled host is sent without its port',
                   json.loads(sent['body']) == {'domains': ['127.0.0.1'], 'include_history': False},
                   sent['body'].decode())
            stats = crawler.get_status_light()['stats']
            result('the score and global rank reach the polled stats',
                   stats.get('open_pagerank') == {'domain': 'github.com', 'open_page_rank': 9.67,
                                                  'rank': 30, 'as_of': '2026-06-01'},
                   str(stats.get('open_pagerank')))

            crawler.load_data({'base_url': 'http://example.test', 'base_domain': 'example.test'},
                              [], [], [])
            result('a crawl loaded from the database does not show the last score',
                   'open_pagerank' not in crawler.stats)

            received.clear()
            crawler = crawl(f'http://127.0.0.1:{site}/')
            result('no lookup without a key', not received and 'open_pagerank' not in crawler.stats,
                   str(received))
    finally:
        a.shutdown()
        fake.shutdown()


def test_open_pagerank_failures():
    """A failed Open PageRank lookup is recorded for the UI and never fails or
    stalls the crawl. A stopped crawl skips the lookup."""
    import json
    import socket

    site, api, hung, closed = BASE_PORT + 24, BASE_PORT + 25, BASE_PORT + 26, BASE_PORT + 27
    base = f'http://127.0.0.1:{site}'
    a = serve(make_handler({'/': html('<p>home</p>'), '/slow': html('<p>slow</p>')},
                           slow_paths=('/slow',), slow_seconds=1), site)
    routes, received = {}, []
    fake = serve(make_handler(routes, received=received), api)
    # Accepts connections but never answers
    silent = socket.socket()
    silent.bind(('127.0.0.1', hung))
    silent.listen()

    def outcome(api_port):
        with open_pagerank_api(f'http://127.0.0.1:{api_port}'):
            crawler = crawl(f'{base}/', open_pagerank_api_key='opr_live_test')
        return crawler.get_status_light()['status'], crawler.stats.get('open_pagerank')

    try:
        routes['/v1/domains/bulk'] = {'status': 401, 'body': json.dumps({'error': {
            'type': 'authentication_error', 'message': 'Missing or invalid Open Page Rank API key.'}})}
        got = outcome(api)
        result('an API error is recorded and the crawl completes',
               got == ('completed', {'error': 'API error 401'}), str(got))

        got = outcome(hung)
        result('an API that never answers times out and the crawl completes',
               got == ('completed', {'error': 'Timed out'}), str(got))

        got = outcome(closed)
        result('an unreachable API is recorded and the crawl completes',
               got == ('completed', {'error': 'Lookup failed'}), str(got))

        routes['/v1/domains/bulk'] = {'body': json.dumps(
            {'as_of': '2026-06-01', 'count': 0, 'results': [], 'invalid': ['127.0.0.1']})}
        got = outcome(api)
        result('a host the API cannot score says so',
               got == ('completed', {'error': 'Not a public domain'}), str(got))

        received.clear()
        with open_pagerank_api(f'http://127.0.0.1:{api}'):
            crawler = new_crawler(open_pagerank_api_key='opr_live_test')
            ok, message = crawler.start_crawl(f'{base}/slow')
            assert ok, message
            crawler.stop_crawl()
        result('a stopped crawl skips the lookup',
               not received and 'open_pagerank' not in crawler.stats, str(received))
    finally:
        a.shutdown()
        fake.shutdown()
        silent.close()


def _playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


TESTS = (
    test_cross_domain_redirect,
    test_external_images,
    test_sitemap_discovery_is_async,
    test_max_urls_counts_pages_not_images,
    test_event_ordering_under_polling,
    test_discovered_counts_image_rows,
    test_js_response_time_excludes_render_wait,
    test_duplicate_detection_is_linear,
    test_export_formats_apply_to_every_data_type,
    test_empty_alt_is_not_missing_alt,
    test_crawl_delay_applies_without_js,
    test_failed_pages_report_only_the_failure,
    test_x_robots_tag_header,
    test_redirect_chains_and_loops,
    test_js_mode_records_redirects_and_header,
    test_link_checks_respect_crawl_delay,
    test_page_fetch_is_one_request,
    test_duplicate_titles_descriptions_h1s,
    test_list_mode,
    test_new_fields_survive_the_database,
    test_post_crawl_updates_reach_the_database,
    test_open_pagerank_lookup,
    test_open_pagerank_failures,
)


def main():
    for test in TESTS:
        print(f'\n{test.__name__}:')
        try:
            test()
        except Exception as exc:  # a raised test is a failed test
            result(test.__name__, False, f'raised {type(exc).__name__}: {exc}')

    print('\n' + '=' * 64)
    print(f'  {len(PASSED)} passed, {len(FAILED)} failed')
    if FAILED:
        print('  failed: ' + ', '.join(FAILED))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
