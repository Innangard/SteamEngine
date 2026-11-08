# Web Crawler for CS 454 - Information Retrieval
# Crawls store.steampowered.com using BFS and saves
# extracted text, visited URLs, and an adjacency matrix.
#
# State is stored in crawl.db (SQLite) so writes are
# incremental instead of dumping a giant JSON every checkpoint.
#
# No external dependencies, stdlib only
#
# Usage:
#   python crawler.py          (crawls until queue is empty or Ctrl+C)
#   python crawler.py resume   (resume a previous crawl)

from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, quote
from urllib.robotparser import RobotFileParser
import json
import csv
import gzip
import os
import time
import sys
import re
import sqlite3


SEED_URL = 'https://store.steampowered.com/'
# Folder this script lives in, so paths work no matter where we run it from
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'crawl_data')
DELAY = 1.0  # seconds between requests


# ---- HTML parser to grab links and visible text ----
# Subclass of Python's built-in HTMLParser. Feed it HTML with
# parser.feed(html) and it calls our overridden methods as it
# walks through each tag and text chunk.

class PageParser(HTMLParser):

    # Tags whose content is not visible text
    SKIP_TAGS = {'script', 'style', 'noscript'}

    def __init__(self):
        super().__init__()
        self.links = []
        self.text_parts = []
        self.lang = None  # language from <html lang="...">
        # Counter for nested skip tags (counter, not bool,
        # because skip tags could nest inside each other)
        self._skip_depth = 0

    # Called on every opening tag like <a href="..."> or <div>
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        # Grab the lang attribute from the <html> tag
        if tag == 'html':
            for name, value in attrs:
                if name == 'lang' and value:
                    self.lang = value.lower()
        # Grab the href attribute from anchor tags
        if tag == 'a':
            for name, value in attrs:
                if name == 'href' and value:
                    self.links.append(value)

    # Called on closing tags like </script>
    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    # Called on raw text between tags
    def handle_data(self, data):
        # Only collect text if we're not inside a skipped tag
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned)

    def get_text(self):
        return ' '.join(self.text_parts)


# ---- Helper functions ----

# Resolve a relative href against a base URL, strip the #fragment
# so /page#top and /page#bottom count as the same page,
# and percent-encode any special characters like spaces
def clean_url(base, href):
    full = urljoin(base, href)
    parsed = urlparse(full)
    encoded_path = quote(parsed.path, safe='/')
    return parsed._replace(fragment='', path=encoded_path).geturl()


# Check if a URL belongs to our crawl domain
def is_same_domain(url, domain):
    return urlparse(url).netloc == domain


# Check if a URL is a game/app page like /app/730/Counter-Strike_2/
def is_app_url(url):
    return bool(re.search(r'/app/\d+', url))


# Extract the app ID from a URL, or None if it's not an app page
def get_app_id(url):
    match = re.search(r'/app/(\d+)', url)
    if match:
        return int(match.group(1))
    return None


# Fetch and parse robots.txt so we can check which paths are allowed
def load_robots(domain):
    rp = RobotFileParser()
    url = 'https://{}/robots.txt'.format(domain)
    rp.set_url(url)
    try:
        rp.read()
        print('Loaded robots.txt')
    except Exception:
        print('Warning: could not load robots.txt, proceeding without it')
    return rp


# ---- SQLite state management ----
# Tables:
#   urls      - every URL we've seen, with its page ID and visited flag
#   queue     - URLs waiting to be crawled, with priority for ordering
#   edges     - adjacency list (source page -> target page)
#   page_text - extracted text for each visited page
#   reviews   - review bucket data per app (written by scraper.py)

def init_db():
    # timeout=30 waits for the scraper's writes instead of erroring
    conn = sqlite3.connect(os.path.join(OUTPUT_DIR, 'crawl.db'), timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')  # faster writes
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            visited INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS queue (
            pos INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            priority INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS edges (
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS page_text (
            page_id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            app_id INTEGER PRIMARY KEY,
            total_reviews INTEGER DEFAULT 0,
            reviews_with_playtime INTEGER DEFAULT 0,
            reviews_without_playtime INTEGER DEFAULT 0,
            complete INTEGER DEFAULT 0,
            buckets_json TEXT DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_urls_url ON urls(url);
        CREATE INDEX IF NOT EXISTS idx_queue_pop ON queue(priority, pos);
    ''')
    conn.commit()
    return conn


# Pop the next URL from the queue.
# Priority 0 (app pages) come first, then priority 1 (normal).
# Within the same priority, FIFO order by insertion position.
def db_queue_pop(conn):
    row = conn.execute(
        'SELECT pos, url FROM queue ORDER BY priority, pos LIMIT 1'
    ).fetchone()
    if row is None:
        return None
    conn.execute('DELETE FROM queue WHERE pos = ?', (row[0],))
    return row[1]


# Add a URL to the crawl queue
# front=True gives it priority 0 (processed before normal URLs)
def db_queue_add(conn, url, front=False):
    priority = 0 if front else 1
    conn.execute('INSERT INTO queue (url, priority) VALUES (?, ?)',
                 (url, priority))


# Check if the queue has anything in it
def db_queue_has_items(conn):
    return conn.execute('SELECT 1 FROM queue LIMIT 1').fetchone() is not None


# Store a URL with its assigned page ID
def db_store_url(conn, url_id, url):
    conn.execute('INSERT OR IGNORE INTO urls (id, url) VALUES (?, ?)',
                 (url_id, url))


# Mark a URL as visited by its page ID
def db_mark_visited(conn, url_id):
    conn.execute('UPDATE urls SET visited = 1 WHERE id = ?', (url_id,))


# Store edges from a source page to all pages it links to
def db_add_edges(conn, source_id, target_ids):
    conn.executemany('INSERT INTO edges (source_id, target_id) VALUES (?, ?)',
                     [(source_id, t) for t in target_ids])


# Load visited URLs and url-to-id mapping from the database.
# Returns a dict with visited/url_to_id/next_id, or None if DB is empty.
def db_load_state(conn):
    rows = conn.execute('SELECT id, url, visited FROM urls').fetchall()
    if not rows:
        return None

    visited = set()
    url_to_id = {}
    max_id = 0

    for row_id, url, is_visited in rows:
        url_to_id[url] = row_id
        if row_id > max_id:
            max_id = row_id
        if is_visited:
            visited.add(url)

    return {
        'visited': visited,
        'url_to_id': url_to_id,
        'next_id': max_id + 1
    }


# Import existing page files into a fresh database.
# Used to migrate from the old state.json system, or when
# the DB was lost but pages/ folder survived.
def db_import_pages(conn):
    pages_dir = os.path.join(OUTPUT_DIR, 'pages')
    if not os.path.isdir(pages_dir):
        return None

    visited = set()
    url_to_id = {}
    max_id = 0
    imported = 0

    for filename in os.listdir(pages_dir):
        if not filename.endswith('.txt'):
            continue
        try:
            page_id = int(filename.replace('.txt', ''))
        except ValueError:
            continue

        filepath = os.path.join(pages_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            if not first_line.startswith('URL: '):
                continue
            f.readline()  # skip blank line
            text = f.read()

        url = first_line[5:]
        visited.add(url)
        url_to_id[url] = page_id
        if page_id > max_id:
            max_id = page_id

        conn.execute(
            'INSERT OR IGNORE INTO urls (id, url, visited) VALUES (?, ?, 1)',
            (page_id, url))
        conn.execute(
            'INSERT OR REPLACE INTO page_text (page_id, url, text) '
            'VALUES (?, ?, ?)', (page_id, url, text))
        imported += 1

    conn.commit()

    if not visited:
        return None

    print('Imported {} pages into database'.format(imported))
    return {
        'visited': visited,
        'url_to_id': url_to_id,
        'next_id': max_id + 1
    }


# Copy text from page files into page_text for any pages that
# aren't in the table yet. Only does real work the first time
# after upgrading, after that everything is already in the DB.
def backfill_page_text(conn):
    pages_dir = os.path.join(OUTPUT_DIR, 'pages')
    if not os.path.isdir(pages_dir):
        return

    have = set()
    for row in conn.execute('SELECT page_id FROM page_text'):
        have.add(row[0])

    added = 0
    for filename in os.listdir(pages_dir):
        if not filename.endswith('.txt'):
            continue
        try:
            page_id = int(filename.replace('.txt', ''))
        except ValueError:
            continue
        if page_id in have:
            continue

        filepath = os.path.join(pages_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            if not first_line.startswith('URL: '):
                continue
            f.readline()  # skip blank line
            text = f.read()

        conn.execute(
            'INSERT OR REPLACE INTO page_text (page_id, url, text) '
            'VALUES (?, ?, ?)', (page_id, first_line[5:], text))
        added += 1
        # Commit in chunks so a crash doesn't lose everything
        if added % 5000 == 0:
            conn.commit()
            print('  backfilled {} pages...'.format(added))

    conn.commit()
    if added > 0:
        print('Backfilled {} pages into page_text'.format(added))


# Save the adjacency data as a sparse edge list, gzipped.
# Each row is "source_id,target_id" meaning source links to target.
# Sparse format only stores the 1s, so a page with 50 links
# produces 50 rows instead of one row with N columns.
# Directed graph: source -> target does NOT imply target -> source.
def save_matrix(conn):
    path = os.path.join(OUTPUT_DIR, 'adjacency.csv.gz')
    edges = 0
    with gzip.open(path, 'wt', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['source', 'target'])
        # Only include edges where the source was actually visited
        cursor = conn.execute('''
            SELECT e.source_id, e.target_id FROM edges e
            JOIN urls u ON e.source_id = u.id
            WHERE u.visited = 1
        ''')
        for source, target in cursor:
            writer.writerow([source, target])
            edges += 1

    print('Adjacency saved: {} edges, gzipped'.format(edges))


# ---- Main crawl function ----

def crawl(resume=False):
    # Extract domain from seed URL for filtering links later
    domain = urlparse(SEED_URL).netloc
    robots = load_robots(domain)

    os.makedirs(os.path.join(OUTPUT_DIR, 'pages'), exist_ok=True)

    # Open or create the database
    conn = init_db()
    backfill_page_text(conn)

    # Either resume from database or start fresh
    if resume:
        # Try loading from database first
        state = db_load_state(conn)
        if state is None:
            # DB is empty, try importing from page files
            print('Database empty, looking for page files to import...')
            state = db_import_pages(conn)

        if state is None:
            print('Nothing to resume from, starting fresh.')
            visited = set()
            url_to_id = {}
            next_id = 1
            db_queue_add(conn, SEED_URL)
            conn.commit()
        else:
            visited = state['visited']
            url_to_id = state['url_to_id']
            next_id = state['next_id']
            print('Resumed: {} pages done'.format(len(visited)))
            # If queue is empty, queue all visited URLs for rescanning.
            # The crawl loop will re-download them to discover their
            # outgoing links, but won't re-save the page text.
            if not db_queue_has_items(conn):
                print('Queue empty, queueing visited pages for link rescan...')
                for v_url in visited:
                    db_queue_add(conn, v_url)
                conn.commit()
                print('Queued {} pages for rescan'.format(len(visited)))
    else:
        visited = set()           # URLs we've already crawled
        url_to_id = {}            # maps each URL to a numeric page ID
        next_id = 1               # next ID to assign
        db_queue_add(conn, SEED_URL)
        conn.commit()

    # Track unique app IDs we've seen so far
    seen_app_ids = set()
    for url in visited:
        aid = get_app_id(url)
        if aid is not None:
            seen_app_ids.add(aid)

    print('Crawling {} (no page limit)'.format(domain))
    if seen_app_ids:
        print('Already found {} unique app IDs'.format(len(seen_app_ids)))
    print('-' * 50)

    count = len(visited)
    skipped_lang = 0  # pages skipped for non-English

    try:
        # BFS: keep going until the queue is empty or Ctrl+C
        while True:
            # Pop the highest-priority URL from the database queue
            url = db_queue_pop(conn)
            if url is None:
                break

            # Skip off-domain or robots.txt blocked URLs
            # Visited URLs are NOT skipped — we still re-download them
            # to discover their outgoing links (but don't re-save the text)
            rescan = url in visited
            if not is_same_domain(url, domain):
                continue
            if not robots.can_fetch('*', url):
                print('  [SKIP] blocked by robots.txt: {}'.format(url))
                continue

            # Try to download the page
            try:
                req = Request(url, headers={'User-Agent': 'CS454Crawler/1.0'})
                resp = urlopen(req, timeout=10)
                # Skip non-HTML responses (images, PDFs, etc.)
                ctype = resp.getheader('Content-Type', '')
                if 'text/html' not in ctype:
                    continue
                html = resp.read().decode('utf-8', errors='replace')
            except (URLError, HTTPError, TimeoutError, OSError) as e:
                print('  [ERROR] {}'.format(e))
                continue

            # Feed the HTML into our parser to extract text and links
            parser = PageParser()
            try:
                parser.feed(html)
            except Exception:
                # Malformed HTML can crash the parser, just use what we got
                pass

            # Skip non-English pages
            if parser.lang and not parser.lang.startswith('en'):
                skipped_lang += 1
                # Still extract links so we can find app pages
                for href in parser.links:
                    link = clean_url(url, href)
                    if is_same_domain(link, domain) and link not in visited:
                        db_queue_add(conn, link, front=is_app_url(link))
                conn.commit()
                continue

            if not rescan:
                visited.add(url)
                count += 1

                # Track unique app IDs
                aid = get_app_id(url)
                if aid is not None:
                    if aid not in seen_app_ids:
                        seen_app_ids.add(aid)
                        print('  [NEW APP] {} unique app IDs found'.format(
                            len(seen_app_ids)))

            # Assign a numeric ID to this URL if it doesn't have one
            if url not in url_to_id:
                url_to_id[url] = next_id
                next_id += 1
            page_id = url_to_id[url]

            # Write this page's URL and visited status to the database
            if not rescan:
                db_store_url(conn, page_id, url)
                db_mark_visited(conn, page_id)

            # Process each link found on this page
            outgoing_ids = set()  # set so duplicate links are ignored
            for href in parser.links:
                link = clean_url(url, href)
                if not is_same_domain(link, domain):
                    continue
                # Every URL gets an ID, even if we haven't visited it yet
                if link not in url_to_id:
                    url_to_id[link] = next_id
                    next_id += 1
                link_id = url_to_id[link]
                db_store_url(conn, link_id, link)
                outgoing_ids.add(link_id)
                # Add unvisited links to the queue
                # Prioritize app pages we haven't seen yet
                if link not in visited:
                    link_aid = get_app_id(link)
                    front = link_aid is not None and link_aid not in seen_app_ids
                    db_queue_add(conn, link, front=front)

            # Store this page's outgoing edges
            db_add_edges(conn, page_id, outgoing_ids)

            # Commit all writes for this page at once
            conn.commit()

            # Save extracted text (skip on rescan)
            if not rescan:
                text = parser.get_text()
                # Store in database
                conn.execute(
                    'INSERT OR REPLACE INTO page_text (page_id, url, text) '
                    'VALUES (?, ?, ?)', (page_id, url, text))
                conn.commit()
                # Also write to file for backwards compatibility
                page_file = os.path.join(OUTPUT_DIR, 'pages',
                                         '{:05d}.txt'.format(page_id))
                with open(page_file, 'w', encoding='utf-8') as f:
                    f.write('URL: {}\n\n'.format(url))
                    f.write(text)

            if rescan:
                print('[rescan] {} ({} links)'.format(url, len(outgoing_ids)))
            else:
                print('[{}] {} ({} links)'.format(
                    count, url, len(outgoing_ids)))

            # Wait between requests to be polite to the server
            time.sleep(DELAY)

    except KeyboardInterrupt:
        # Ctrl+C pressed, commit any pending writes
        print('\nInterrupted, saving...')
        conn.commit()

    # Save URL list as {id: url} so it's easy to read
    id_to_url = {v: k for k, v in url_to_id.items()}
    with open(os.path.join(OUTPUT_DIR, 'urls.json'), 'w') as f:
        json.dump(id_to_url, f, indent=2)

    # Convert edge table to sparse edge list, gzipped
    save_matrix(conn)

    # Clean up old files from the JSON-based system
    old_csv = os.path.join(OUTPUT_DIR, 'adjacency.csv')
    if os.path.exists(old_csv):
        os.remove(old_csv)
        print('Removed old adjacency.csv')
    old_state = os.path.join(OUTPUT_DIR, 'state.json')
    if os.path.exists(old_state):
        os.remove(old_state)
        print('Removed old state.json (migrated to crawl.db)')

    conn.close()

    print('-' * 50)
    print('Done. {} pages crawled.'.format(count))
    print('{} unique app IDs found'.format(len(seen_app_ids)))
    print('{} non-English pages skipped'.format(skipped_lang))
    print('Output in {}/'.format(OUTPUT_DIR))


# ---- Entry point ----

if __name__ == '__main__':
    resume = False

    # Check for "resume" argument
    if len(sys.argv) > 1 and sys.argv[1] == 'resume':
        resume = True

    crawl(resume)