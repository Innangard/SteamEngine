Steam Crawler and Review Scraper (CS 454)

I wrote two scripts that collect data from the Steam store for our search project. Both use only the Python standard library and share one SQLite database, crawl_data/crawl.db.
They can run at the same time without stepping on each other.

FILES
crawler.py          BFS crawler for store.steampowered.com. Skips non-English pages and puts unseen /app/ pages at the front of the queue.
                    Run "python crawler.py" to start fresh or "python crawler.py resume" to pick up where it left off.
scraper.py          Pulls app IDs from crawl.db and fetches reviews from Steam's IUserReviewsService/GetAppReviews API (needs an API key in STEAM_API_KEY).
                    Run "python scraper.py" for every app found by the crawler, or "python scraper.py 730 570" for specific apps.
build_index.py      Builds the Whoosh search index in index_dir/ from the page_text table. Run "python build_index.py".
search_engine.py    Interactive command line search over index_dir/. Run "python search_engine.py".
crawl_data/crawl.db         The database. This is the only data file you need, both scripts write all their data here.
crawl_data/urls.json        {page_id: url} export, rewritten when the crawler exits.
crawl_data/adjacency.csv.gz Edge list export (source,target), rewritten when the crawler exits.
index_dir/                  Whoosh index, not in git. Rebuild it with build_index.py after pulling a new crawl.db.

DATABASE SCHEMA
urls        every URL the crawler has seen, visited or not
  id            INTEGER PRIMARY KEY   page ID, used everywhere else
  url           TEXT UNIQUE
  visited       INTEGER               1 if the page was crawled, 0 if only discovered
page_text   extracted visible text for each crawled page
  page_id       INTEGER PRIMARY KEY   matches urls.id
  url           TEXT
  text          TEXT
edges       links between pages (directed graph, for PageRank and similar)
  source_id     INTEGER               page the link is on
  target_id     INTEGER               page the link points to
queue       crawler work queue, only matters for resuming
  pos           INTEGER PRIMARY KEY   insertion order
  url           TEXT
  priority      INTEGER               0 = new app page (crawled first), 1 = normal
reviews     review counts per game, bucketed by playtime at time of review
  app_id                    INTEGER PRIMARY KEY   Steam app ID
  total_reviews             INTEGER               reviews fetched (capped at 10000)
  reviews_with_playtime     INTEGER               reviews that had playtime_at_review
  reviews_without_playtime  INTEGER               reviews skipped because Steam had no playtime for them
  complete                  INTEGER               1 if we got everything available, 0 if cut short (gets retried)
  buckets_json              TEXT                  JSON list of buckets, see below

Each bucket in buckets_json looks like this:
  {"label": "10-50h", "min_hours": 10, "max_hours": 50, "positive": 812, "negative": 95}
The buckets are 0-1h, 1-5h, 5-10h, 10-50h, 50-100h, 100-500h and 500h+ (max_hours is null for the last one).

EXAMPLE QUERIES
  SELECT text FROM page_text WHERE url LIKE '%/app/730/%';
  SELECT buckets_json FROM reviews WHERE app_id = 730;
  SELECT target_id FROM edges WHERE source_id = 1;
  SELECT COUNT(*) FROM urls WHERE visited = 1;

In Python:
  import sqlite3, json
  conn = sqlite3.connect('crawl_data/crawl.db')
  row = conn.execute('SELECT buckets_json FROM reviews WHERE app_id = ?', (730,)).fetchone()
  buckets = json.loads(row[0])

NOTES
Stop both scripts before committing crawl.db. Recent writes sit in crawl.db-wal until they are merged, and that file is not in git.
Older versions wrote crawl_data/pages/*.txt and review_data/*.json. If those folders are still around, the scripts copy anything missing into crawl.db on startup, and after that the folders can be deleted.
The database is tracked with Git LFS because it is too big for a normal GitHub file.