#!/usr/bin/env python3
"""
Fill locations.json with the location tags of your posts.

The official API does not expose location tags, but the public post page does.
This script reads each post page once, slowly, and stores the tag name under the
post's shortcode. Posts without a tag are stored as null so they are not looked
up again. Already known posts are skipped, so the script can be rerun any time
and resumes where it stopped.

Usage
  python import_locations.py                 shortcodes from the API (needs IG_ACCESS_TOKEN)
  python import_locations.py codes.txt       shortcodes or post URLs, one per line
  python import_locations.py --retry-unknown also retry posts stored as null
"""

import sys
import time

from story_quiz import LOCATIONS_FILE, fetch_all_media, load_json, lookup_post_location, save_json, shortcode_of

DELAY_SECONDS = 2.0


def codes_from_file(path):
    with open(path, encoding="utf-8") as fh:
        return [shortcode_of(line.strip()) for line in fh if line.strip()]


def codes_from_api():
    return [shortcode_of(p["permalink"]) for p in fetch_all_media() if p.get("permalink")]


def main(argv):
    retry_unknown = "--retry-unknown" in argv
    files = [a for a in argv if not a.startswith("--")]
    codes = codes_from_file(files[0]) if files else codes_from_api()
    locations = load_json(LOCATIONS_FILE, {})
    locations.pop("_comment", None)

    todo = [c for c in codes if c not in locations or (retry_unknown and locations[c] is None)]
    print(f"{len(codes)} posts, {len(todo)} to look up")

    for i, code in enumerate(todo, 1):
        try:
            place = lookup_post_location(code)
        except Exception as exc:
            print(f"[{i}/{len(todo)}] {code}: stopped ({exc}). Rerun later to continue.")
            break
        locations[code] = place
        save_json(LOCATIONS_FILE, locations)
        print(f"[{i}/{len(todo)}] {code}: {place or '-'}")
        time.sleep(DELAY_SECONDS)

    tagged = sum(1 for v in locations.values() if v)
    print(f"Done. {tagged} of {len(locations)} posts have a place.")


if __name__ == "__main__":
    main(sys.argv[1:])
