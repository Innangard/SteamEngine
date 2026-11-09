"""
Assignment 4 - Whoosh Search Engine (Index Builder)
CS 454
Reads crawled Steam store pages from the page_text table in
  crawl_data/crawl.db and builds a Whoosh full-text index in index_dir/.

Usage:
    python build_index.py
"""

import os
import re
import sqlite3

from whoosh import index
from whoosh.fields import Schema, TEXT, ID

# folder this script lives in, so the DB path works no matter
# where the script is run from
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# the crawler's database (page text is in the page_text table)
DB_PATH = os.path.join(SCRIPT_DIR, 'crawl_data', 'crawl.db')
# where to put the whoosh index
INDEX_DIR = 'index_dir'


# try to get a readable title from the URL
# steam app pages have a slug like /app/12345/Game_Name/
# so we grab that slug and swap underscores for spaces
# for non-app pages just use the first chunk of text
def get_title(url, body):
    match = re.search(r'/app/\d+/([^/?]+)', url)
    if match:
        slug = match.group(1)
        return slug.replace('_', ' ')

    # fallback: first line of body text that has some content
    for line in body.split('\n'):
        line = line.strip()
        if len(line) > 5:
            return line[:120]
    return 'Untitled'


# turn one row from the page_text table into a dict for whoosh
# returns None if the page has no text
def parse_row(page_id, url, text):
    if not text or not text.strip():
        return None

    # collapse whitespace so it stores cleanly
    body = re.sub(r'\s+', ' ', text).strip()
    title = get_title(url, body)

    return {
        # zero-padded like the old file names ("00001") so
        # search results look the same as before
        'page_id': '{:05d}'.format(page_id),
        'url': url,
        'title': title,
        'content': body,
    }


# create a fresh whoosh index with our schema
# page_id: unique file number like "00001"
# url: the page URL, stored but not full-text searched
# title: short name for the page, searchable
# content: full page text, searchable
def create_index(index_dir):
    if not os.path.exists(index_dir):
        os.makedirs(index_dir)

    schema = Schema(
        page_id=ID(stored=True, unique=True),
        url=ID(stored=True),
        title=TEXT(stored=True),
        content=TEXT(stored=True),
    )
    return index.create_in(index_dir, schema)


# read every page from the database and add it to the index
def build_index(db_path, index_dir):
    if not os.path.exists(db_path):
        print(f'No database found at {db_path}')
        return 0

    ix = create_index(index_dir)
    writer = ix.writer()

    conn = sqlite3.connect(db_path)
    # loop over the cursor directly so we only hold one page
    # in memory at a time instead of all 100k+
    rows = conn.execute('SELECT page_id, url, text FROM page_text ORDER BY page_id')
    added = 0

    for page_id, url, text in rows:
        doc = parse_row(page_id, url, text)
        if doc is None:
            continue
        writer.add_document(**doc)
        added += 1

        if added % 500 == 0:
            print(f'  indexed {added} pages ...')

    conn.close()
    writer.commit()
    return added


if __name__ == '__main__':
    print(f'Building index from {DB_PATH} ...')
    count = build_index(DB_PATH, INDEX_DIR)
    print(f'Done. {count} documents indexed in {INDEX_DIR}/')

    # verify
    ix = index.open_dir(INDEX_DIR)
    print(f'Verification: {ix.doc_count()} documents in index.')