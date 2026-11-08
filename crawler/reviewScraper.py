# Steam Review Scraper
# Reads app IDs from crawl.db (shared with crawler.py) and
# fetches reviews from Steam's API. Saves bucketed data
# (positive/negative counts by playtime bracket) into the
# reviews table in crawl.db, plus JSON files for compatibility.
#
# No external dependencies, stdlib only
#
# Usage:
#   python scraper.py              (scrape all app IDs from crawler data)
#   python scraper.py 730 570      (scrape specific app IDs)

from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
import json
import os
import time
import sys
import re
import sqlite3


STEAM_API_KEY = '6DAB5A989112A05893E33A4A6EE588E6'
# Folder this script lives in, so paths work no matter where we run it from
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRAWL_DIR = os.path.join(SCRIPT_DIR, 'crawl_data')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'review_data')
DELAY = 1.5  # seconds between API requests
GAME_DELAY = 5  # seconds to wait between games
REVIEWS_PER_GAME = 10000
REVIEWS_PER_PAGE = 100


# Playtime brackets in hours
BUCKETS = [
    (0, 1),
    (1, 5),
    (5, 10),
    (10, 50),
    (50, 100),
    (100, 500),
    (500, None)  # None means no upper limit
]


# ---- Helper functions ----

# Turn a bucket tuple into a readable label
def bucket_label(low, high):
    if high is None:
        return '{}h+'.format(low)
    return '{}-{}h'.format(low, high)


# User-Agent header identifying the script
UA = 'SteamReviewScraper/1.0'


# Pull app IDs from crawl.db, falling back to urls.json
def extract_app_ids():
    # Try the database first
    db_path = os.path.join(CRAWL_DIR, 'crawl.db')
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        rows = conn.execute('SELECT url FROM urls WHERE visited = 1').fetchall()
        conn.close()
        app_ids = set()
        for row in rows:
            match = re.search(r'/app/(\d+)', row[0])
            if match:
                app_ids.add(int(match.group(1)))
        if app_ids:
            return sorted(app_ids)

    # Fall back to urls.json
    path = os.path.join(CRAWL_DIR, 'urls.json')
    if not os.path.exists(path):
        print('No crawl.db or urls.json found in {}/'.format(CRAWL_DIR))
        return []

    with open(path, 'r') as f:
        urls = json.load(f)

    app_ids = set()
    for url in urls.values():
        match = re.search(r'/app/(\d+)', url)
        if match:
            app_ids.add(int(match.group(1)))

    return sorted(app_ids)


# Fetch up to REVIEWS_PER_GAME reviews for one app ID
# using the new IUserReviewsService/GetAppReviews endpoint.
# (The old store.steampowered.com/appreviews/ was deprecated Oct 2026)
# Returns (reviews, complete) where complete is True if
# the API ran out of reviews naturally (not from an error).
def fetch_reviews(app_id):
    reviews = []
    cursor = '*'  # Steam uses * as the starting cursor
    complete = False  # did we get all available reviews?

    while len(reviews) < REVIEWS_PER_GAME:
        # Build the input_json parameter for the service API
        input_data = json.dumps({
            'appid': app_id,
            'filter': 1,  # 1 = recent
            'languages': ['all'],
            'cursor': cursor,
            'num_per_page': REVIEWS_PER_PAGE,
            'review_type': 0,  # 0 = all
            'purchase_type': 1,  # 1 = all purchases
            'day_range': 0  # 0 = no time limit
        })
        params = urlencode({
            'key': STEAM_API_KEY,
            'input_json': input_data
        })
        url = 'https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/?{}'.format(params)

        retries = 3
        data = None
        for attempt in range(retries):
            try:
                req = Request(url, headers={'User-Agent': UA})
                resp = urlopen(req, timeout=15)
                raw = resp.read().decode('utf-8', errors='replace')
                if not raw.strip():
                    print('  [ERROR] empty response (attempt {}/{})'.format(
                        attempt + 1, retries))
                    time.sleep(10)
                    continue
                data = json.loads(raw)
                break  # got valid JSON, move on
            except (URLError, HTTPError, TimeoutError, OSError, ValueError) as e:
                print('  [ERROR] {} (attempt {}/{})'.format(e, attempt + 1, retries))
                time.sleep(10)  # wait before retrying

        if data is None:
            print('  giving up on this page after {} attempts'.format(retries))
            break  # still counts as incomplete, will be retried next run

        # New API wraps everything under "response"
        resp_data = data.get('response', data)

        if resp_data.get('success') == 0:
            print('  [ERROR] API returned failure')
            break

        batch = resp_data.get('reviews', [])
        if not batch:
            # Check query_summary to see if reviews actually exist
            total = resp_data.get('query_summary', {}).get('total_reviews', 0)
            if total == 0:
                print('  API confirms 0 reviews for this app')
                complete = True
            elif len(reviews) == 0:
                print('  API says {} reviews exist but returned none'.format(total))
                complete = False
            else:
                complete = True  # we already got some, just ran out
            break

        reviews.extend(batch)

        # Get the next cursor, stop if it didn't change
        new_cursor = resp_data.get('cursor', '')
        if not new_cursor or new_cursor == cursor:
            complete = True  # ran out of pages
            break
        cursor = new_cursor

        print('  {} reviews so far'.format(len(reviews)))
        time.sleep(DELAY)

    # Hit the target = complete even if there are more
    if len(reviews) >= REVIEWS_PER_GAME:
        complete = True

    return reviews, complete


# Sort a list of reviews into playtime brackets,
# counting positive and negative separately.
# Reviews with 0 playtime_at_review are from before Steam
# tracked that field, so we skip them since the only
# alternative (playtime_forever) is their current total,
# not what it was when they reviewed.
def make_buckets(reviews):
    result = []
    for low, high in BUCKETS:
        result.append({
            'label': bucket_label(low, high),
            'min_hours': low,
            'max_hours': high,
            'positive': 0,
            'negative': 0
        })

    skipped = 0
    for review in reviews:
        minutes = review.get('author', {}).get('playtime_at_review', 0)
        if minutes == 0:
            skipped += 1
            continue

        hours = minutes / 60.0
        positive = review.get('voted_up', False)

        # Find the right bucket for this playtime
        for bucket in result:
            low = bucket['min_hours']
            high = bucket['max_hours']
            fits = False
            if high is None:
                fits = hours >= low
            else:
                fits = low <= hours < high
            if fits:
                if positive:
                    bucket['positive'] += 1
                else:
                    bucket['negative'] += 1
                break

    if skipped > 0:
        print('  skipped {} reviews with no playtime data'.format(skipped))

    return result, skipped


# ---- Database helpers ----

# Open crawl.db and make sure the reviews table exists.
# The crawler creates it too, but this way the scraper works
# even if it runs first.
def open_db():
    os.makedirs(CRAWL_DIR, exist_ok=True)
    conn = sqlite3.connect(os.path.join(CRAWL_DIR, 'crawl.db'), timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')  # lets crawler and scraper share it
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            app_id INTEGER PRIMARY KEY,
            total_reviews INTEGER DEFAULT 0,
            reviews_with_playtime INTEGER DEFAULT 0,
            reviews_without_playtime INTEGER DEFAULT 0,
            complete INTEGER DEFAULT 0,
            buckets_json TEXT DEFAULT '[]'
        )
    ''')
    conn.commit()
    return conn


# Look up a previous result for this app, from the DB or an old JSON file.
# Returns a dict like the JSON output, or None if nothing saved yet.
def load_previous(conn, app_id):
    row = conn.execute(
        'SELECT total_reviews, complete FROM reviews WHERE app_id = ?',
        (app_id,)).fetchone()
    if row is not None:
        return {'total_reviews': row[0], 'complete': bool(row[1])}

    # Fall back to JSON files from before the DB existed
    out_path = os.path.join(OUTPUT_DIR, '{}.json'.format(app_id))
    if os.path.exists(out_path):
        with open(out_path, 'r') as f:
            return json.load(f)
    return None


# Write one game's results to the DB and to its JSON file
def save_result(conn, output):
    conn.execute('''
        INSERT OR REPLACE INTO reviews
        (app_id, total_reviews, reviews_with_playtime,
         reviews_without_playtime, complete, buckets_json)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (output['app_id'], output['total_reviews'],
          output['reviews_with_playtime'], output['reviews_without_playtime'],
          1 if output['complete'] else 0, json.dumps(output['buckets'])))
    conn.commit()

    out_path = os.path.join(OUTPUT_DIR, '{}.json'.format(output['app_id']))
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)


# Copy any existing JSON results into the DB so the group gets them too
def import_json_files(conn):
    if not os.path.isdir(OUTPUT_DIR):
        print('No review_data folder at {}, nothing to import'.format(OUTPUT_DIR))
        return
    json_count = 0
    for filename in os.listdir(OUTPUT_DIR):
        if filename.endswith('.json'):
            json_count += 1
    print('Found {} JSON files in {}'.format(json_count, OUTPUT_DIR))
    imported = 0
    for filename in os.listdir(OUTPUT_DIR):
        if not filename.endswith('.json'):
            continue
        app_id = int(filename.replace('.json', ''))
        row = conn.execute('SELECT 1 FROM reviews WHERE app_id = ?',
                           (app_id,)).fetchone()
        if row is not None:
            continue  # already in DB
        with open(os.path.join(OUTPUT_DIR, filename), 'r') as f:
            data = json.load(f)
        conn.execute('''
            INSERT INTO reviews
            (app_id, total_reviews, reviews_with_playtime,
             reviews_without_playtime, complete, buckets_json)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (app_id, data.get('total_reviews', 0),
              data.get('reviews_with_playtime', 0),
              data.get('reviews_without_playtime', 0),
              1 if data.get('complete', False) else 0,
              json.dumps(data.get('buckets', []))))
        imported += 1
    conn.commit()
    if imported > 0:
        print('Imported {} existing JSON results into crawl.db'.format(imported))


# ---- Main scrape function ----

def scrape(app_ids):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print('Database: {}'.format(os.path.join(CRAWL_DIR, 'crawl.db')))
    conn = open_db()
    import_json_files(conn)
    row = conn.execute('SELECT COUNT(*) FROM reviews').fetchone()
    print('{} games already in the reviews table'.format(row[0]))

    print('Scraping reviews for {} games'.format(len(app_ids)))
    print('-' * 50)

    for i, app_id in enumerate(app_ids):
        # Skip games we already scraped fully (resume support).
        # If it was saved but has fewer reviews than the target,
        # it probably got cut short, so we redo it.
        old = load_previous(conn, app_id)
        if old is not None:
            if old.get('total_reviews', 0) >= REVIEWS_PER_GAME:
                print('[{}/{}] app {} already done, skipping'.format(
                    i + 1, len(app_ids), app_id))
                continue
            if old.get('complete', False):
                print('[{}/{}] app {} has all available reviews, skipping'.format(
                    i + 1, len(app_ids), app_id))
                continue
            print('[{}/{}] app {} only had {} reviews, retrying'.format(
                i + 1, len(app_ids), app_id, old.get('total_reviews', 0)))

        print('[{}/{}] app {}'.format(i + 1, len(app_ids), app_id))

        reviews, complete = fetch_reviews(app_id)

        if not reviews:
            # Save it as complete with 0 so we don't keep retrying
            output = {
                'app_id': app_id,
                'total_reviews': 0,
                'reviews_with_playtime': 0,
                'reviews_without_playtime': 0,
                'complete': complete,
                'buckets': []
            }
            save_result(conn, output)
            print('  no reviews found, saved')
            continue

        buckets, skipped = make_buckets(reviews)

        output = {
            'app_id': app_id,
            'total_reviews': len(reviews),
            'reviews_with_playtime': len(reviews) - skipped,
            'reviews_without_playtime': skipped,
            'complete': complete,  # True = got everything, False = cut short
            'buckets': buckets
        }

        save_result(conn, output)

        print('  saved {} reviews in buckets'.format(len(reviews)))

        # Wait between games so Steam doesn't block us
        if i < len(app_ids) - 1:
            print('  waiting {} seconds before next game...'.format(GAME_DELAY))
            time.sleep(GAME_DELAY)

    conn.close()
    print('-' * 50)
    print('Done. Results in {}/crawl.db (reviews table) and {}/'.format(
        CRAWL_DIR, OUTPUT_DIR))


# ---- Entry point ----

if __name__ == '__main__':
    if len(sys.argv) > 1:
        # Specific app IDs passed as arguments
        app_ids = [int(x) for x in sys.argv[1:]]
    else:
        # Pull all app IDs from the crawler's output
        app_ids = extract_app_ids()
        if not app_ids:
            print('No app IDs found.')
            print('Run the crawler first or pass IDs as arguments.')
            sys.exit(1)
        print('Found {} app IDs in crawler data'.format(len(app_ids)))

    scrape(app_ids)