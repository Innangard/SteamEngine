# Steam Review Scraper
# Reads app IDs from the crawler's urls.json and fetches
# reviews from Steam's public review API. Saves bucketed
# data (positive/negative counts by playtime bracket) as
# JSON files, one per game.
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


STEAM_API_KEY = '6DAB5A989112A05893E33A4A6EE588E6'
CRAWL_DIR = 'crawl_data'
OUTPUT_DIR = 'review_data'
DELAY = 0.75  # seconds between API requests
GAME_DELAY = 2.5  # seconds to wait between games
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


# Pull app IDs out of the crawler's urls.json by matching
# URLs that look like /app/730/ or /app/730/Counter-Strike_2/
def extract_app_ids():
    path = os.path.join(CRAWL_DIR, 'urls.json')
    if not os.path.exists(path):
        print('No urls.json found in {}/'.format(CRAWL_DIR))
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


# ---- Main scrape function ----

def scrape(app_ids):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print('Scraping reviews for {} games'.format(len(app_ids)))
    print('-' * 50)

    for i, app_id in enumerate(app_ids):
        # Skip games we already scraped fully (resume support).
        # If the file exists but has fewer reviews than the target,
        # it probably got cut short by a 403, so we redo it.
        out_path = os.path.join(OUTPUT_DIR, '{}.json'.format(app_id))
        if os.path.exists(out_path):
            with open(out_path, 'r') as f:
                old = json.load(f)
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
            with open(out_path, 'w') as f:
                json.dump(output, f, indent=2)
            print('  no reviews found, saved as complete')
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

        with open(out_path, 'w') as f:
            json.dump(output, f, indent=2)

        print('  saved {} reviews in buckets'.format(len(reviews)))

        # Wait between games so Steam doesn't block us
        if i < len(app_ids) - 1:
            print('  waiting {} seconds before next game...'.format(GAME_DELAY))
            time.sleep(GAME_DELAY)

    print('-' * 50)
    print('Done. Output in {}/'.format(OUTPUT_DIR))


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