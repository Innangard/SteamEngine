Steam Crawler, Review Scraper and Search Engine (CS 454)

I wrote two scripts that collect data from the Steam store for our search project. Both use only the Python standard library and share one SQLite database, crawl_data/crawl.db.
They can run at the same time without stepping on each other.
The search side (build_index.py and search_engine.py) reads from that database and needs Whoosh: "pip install whoosh".

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
index_dir/                  Whoosh index, not in git. See SEARCH INDEX below.

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

SEARCH INDEX
build_index.py reads every row of page_text and builds a fresh index in index_dir/ (it wipes any old index first). Each page becomes one Whoosh document:
  page_id       ID, stored, unique    zero padded page ID like "00001", matches urls.id and page_text.page_id
  url           ID, stored            the page URL, stored but not searched
  title         TEXT, stored          game name from the /app/ID/Name/ slug, or the first 120 characters of the page for other pages
  content       TEXT, stored          the page text with whitespace collapsed, stored so search results can show snippets
Building the full index (about 119k pages) takes a while. Adding the pages goes fairly fast, then it prints "finishing index" and sits there while writer.commit() merges everything into the final index. That step shows no progress and can take a long time, so let it finish.
Do not Ctrl+C during the commit. It leaves index_dir/ half written, and you have to delete it and run build_index.py again.
The writer uses limitmb=512 so there is less to merge at the end. If your machine is short on memory, lower it to 256 in build_index.py.
It is much faster with Windows Python on Windows files than with WSL Python on a /mnt/c folder.
search_engine.py opens index_dir/ and asks for a mode before the first search:
  point         every word in the query has to match (AND)
  range         any word in the query can match (OR)
It shows the top 10 results with title, URL, score and a snippet. Change TOP_K in search_engine.py for more or fewer. Type "quit" to exit.
After pulling a new crawl.db, run build_index.py again so the index matches.

NOTES
Stop both scripts before committing crawl.db. Recent writes sit in crawl.db-wal until they are merged, and that file is not in git.
Older versions wrote crawl_data/pages/*.txt and review_data/*.json. If those folders are still around, the scripts copy anything missing into crawl.db on startup, and after that the folders can be deleted.
The database is tracked with Git LFS because it is too big for a normal GitHub file.