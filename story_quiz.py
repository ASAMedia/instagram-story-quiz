#!/usr/bin/env python3
"""
Instagram story quiz.

Every day: pick a random old post, cut a random detail out of it, and publish it
as a story asking "when / where was this taken?".  Twelve hours later publish a
reveal story showing the full photo, the date and (if known) the place.

Sub-commands
  make question             pick a post, render docs/<date>-question.jpg, write state
  make reveal               render docs/<date>-reveal.jpg from the stored state
  publish question          publish the prepared question image as a story
  publish reveal            publish the prepared reveal image as a story
  refresh                   refresh the long-lived access token
  demo <image> [handle] [avatar.jpg] [place]
                            render a question + reveal from a local file, no API needed

Environment
  IG_ACCESS_TOKEN     long-lived token (Instagram API with Instagram Login)
  PUBLIC_BASE_URL     public URL under which docs/ is reachable, e.g.
                      https://raw.githubusercontent.com/<user>/<repo>/main/docs
  QUIZ_PAUSED         "true" pauses new questions (an open round is still revealed)
  QUIZ_PAUSED_UNTIL   YYYY-MM-DD: pause new questions up to and including that day
  QUIZ_FORCE          "true" ignores the pause (set for manual workflow runs)
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
FONTS = ROOT / "fonts"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "current.json"
HISTORY_FILE = STATE_DIR / "history.json"
LOCATIONS_FILE = ROOT / "locations.json"
CONFIG_FILE = ROOT / "config.json"

GRAPH = "https://graph.instagram.com/v21.0"

# ---- layout constants (Instagram story canvas) ------------------------------
W, H = 1080, 1920
MARGIN = 80                 # left/right margin
SAFE_TOP = 250              # Instagram overlays the profile bar here
SAFE_BOTTOM = 1670          # reply bar / "send message" field starts here
RADIUS = 36                 # card corner radius
QUESTION_CARD = (W - 2 * MARGIN, 720)   # crop card size; crops use the same aspect

WHITE = (255, 255, 255, 255)
MUTED = (255, 255, 255, 175)
INK = (18, 18, 22, 255)

DEFAULT_CONFIG = {
    "timezone": "Europe/Berlin",
    "language": "de",
    "accent": "#FFCB47",
    "crop_fraction_min": 0.28,
    "crop_fraction_max": 0.45,
    "avoid_repeat_last_n": 60,
    "handle": "",
    "texts": {
        "de": {
            "eyebrow_question": "FOTO-RÄTSEL",
            "eyebrow_reveal": "AUFLÖSUNG",
            "headline_question": "Wann und wo\nwar das?",
            "note_question": "Ein Ausschnitt aus einem meiner alten Posts.",
            "cta_question": "Antworte auf diese Story",
            "hint_question": "Auflösung in 12 Stunden",
            "headline_reveal_place": "Das war\n{place}",
            "headline_reveal_date": "Das war im\n{month_year}",
            "chip_date": "GEPOSTET",
            "chip_place": "ORT",
            "note_reveal": "Der Ausschnitt von heute Morgen ist markiert.",
            "cta_reveal": "Wie nah warst du dran?",
            "hint_reveal": "Morgen geht es weiter",
            "answers_title": "So lagt ihr",
            "answers_correct": "{pct} % richtig",
            "answers_wrong": "{pct} % daneben",
            "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
                       "August", "September", "Oktober", "November", "Dezember"],
            "date_format": "{day}. {month} {year}",
        },
        "en": {
            "eyebrow_question": "PHOTO QUIZ",
            "eyebrow_reveal": "THE ANSWER",
            "headline_question": "When and where\nwas this?",
            "note_question": "A detail from one of my old posts.",
            "cta_question": "Reply to this story",
            "hint_question": "Answer in 12 hours",
            "headline_reveal_place": "It was\n{place}",
            "headline_reveal_date": "It was in\n{month_year}",
            "chip_date": "POSTED",
            "chip_place": "PLACE",
            "note_reveal": "This morning's crop is marked.",
            "cta_reveal": "How close were you?",
            "hint_reveal": "Next one tomorrow",
            "answers_title": "How you did",
            "answers_correct": "{pct} % right",
            "answers_wrong": "{pct} % off",
            "months": ["January", "February", "March", "April", "May", "June", "July",
                       "August", "September", "October", "November", "December"],
            "date_format": "{day} {month} {year}",
        },
    },
}


# --------------------------------------------------------------------------- #
# config / small helpers
# --------------------------------------------------------------------------- #

def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in load_json(CONFIG_FILE, {}).items():
        if key == "texts":
            for lang, strings in value.items():
                cfg["texts"].setdefault(lang, {}).update(strings)
        else:
            cfg[key] = value
    return cfg


def texts():
    cfg = config()
    return cfg["texts"].get(cfg["language"], cfg["texts"]["de"])


def accent():
    hex_ = config()["accent"].lstrip("#")
    return tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def token():
    value = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not value:
        sys.exit("IG_ACCESS_TOKEN is not set")
    return value


def api(method, path, fatal=True, **params):
    params["access_token"] = token()
    url = path if path.startswith("http") else f"{GRAPH}/{path}"
    resp = requests.request(method, url, params=params, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    if resp.status_code >= 400 or "error" in data:
        message = f"Instagram API error on {path}: {json.dumps(data, indent=2)}"
        if fatal:
            sys.exit(message)
        raise RuntimeError(message)
    return data


_font_cache = {}


def font(weight, size):
    """weight: 'bold' or 'regular'. Inter is bundled in fonts/."""
    key = (weight, size)
    if key not in _font_cache:
        name = "Inter-ExtraBold.ttf" if weight == "bold" else "Inter-Medium.ttf"
        path = FONTS / name
        if path.exists():
            _font_cache[key] = ImageFont.truetype(str(path), size)
        else:
            fallback = "C:/Windows/Fonts/arialbd.ttf" if weight == "bold" else "C:/Windows/Fonts/arial.ttf"
            _font_cache[key] = ImageFont.truetype(fallback, size) if Path(fallback).exists() else ImageFont.load_default(size)
    return _font_cache[key]


def today():
    return datetime.now(ZoneInfo(config()["timezone"])).strftime("%Y-%m-%d")


def parse_ts(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").astimezone(ZoneInfo(config()["timezone"]))


def format_date(iso):
    t, dt = texts(), parse_ts(iso)
    return t["date_format"].format(day=dt.day, month=t["months"][dt.month - 1], year=dt.year)


def format_month_year(iso):
    t, dt = texts(), parse_ts(iso)
    return f"{t['months'][dt.month - 1]} {dt.year}"


# --------------------------------------------------------------------------- #
# drawing primitives
# --------------------------------------------------------------------------- #

def cover(img, size):
    scale = max(size[0] / img.width, size[1] / img.height)
    img = img.resize((round(img.width * scale) + 1, round(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - size[0]) // 2, (img.height - size[1]) // 2
    return img.crop((left, top, left + size[0], top + size[1]))


def fit(img, max_w, max_h):
    scale = min(max_w / img.width, max_h / img.height)
    return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)


def vertical_gradient(size, top_alpha, bottom_alpha, color=(0, 0, 0)):
    """RGBA layer whose alpha goes linearly from top_alpha to bottom_alpha."""
    ramp = Image.linear_gradient("L").resize((1, size[1])).resize(size)
    lo, hi = min(top_alpha, bottom_alpha), max(top_alpha, bottom_alpha)
    ramp = ramp.point(lambda v: top_alpha + (bottom_alpha - top_alpha) * v / 255)
    layer = Image.new("RGBA", size, color + (0,))
    layer.putalpha(ramp)
    return layer


def background(photo):
    """Blurred cover-fit photo with a dark scrim, darker toward the bottom."""
    bg = cover(photo.convert("RGB"), (W, H)).filter(ImageFilter.GaussianBlur(70)).convert("RGBA")
    bg.alpha_composite(Image.new("RGBA", (W, H), (10, 10, 16, 120)))
    bg.alpha_composite(vertical_gradient((W, H), 0, 170))
    bg.alpha_composite(vertical_gradient((W, H), 90, 0))
    return bg


def rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def drop_shadow(canvas, box, radius, blur=34, offset=(0, 22), alpha=150):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(layer).rounded_rectangle(
        [x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]], radius, fill=(0, 0, 0, alpha))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def paste_card(canvas, img, x, y, radius=RADIUS):
    drop_shadow(canvas, (x, y, x + img.width, y + img.height), radius)
    canvas.alpha_composite(rounded(img, radius), (x, y))
    return (x, y, img.width, img.height)


def wrap(draw, text, fnt, max_width):
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=fnt) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def text_block(draw, text, x, y, fnt, fill=WHITE, max_width=W - 2 * MARGIN, leading=1.1, align="left"):
    """Draw wrapped text, return the y below the block."""
    for line in wrap(draw, text, fnt, max_width):
        width = draw.textlength(line, font=fnt)
        lx = x if align == "left" else (W - width) / 2
        draw.text((lx, y), line, font=fnt, fill=fill)
        y += round(fnt.size * leading)
    return y


def tracked_text(draw, text, x, y, fnt, fill, tracking=3):
    """Letter-spaced text (for small uppercase labels)."""
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + tracking
    return x


def tracked_width(draw, text, fnt, tracking=3):
    return sum(draw.textlength(ch, font=fnt) + tracking for ch in text) - tracking


def pill(canvas, text, cx, y, fnt, fill, color, pad_x=44, height=None):
    """Centered rounded pill with text. Returns its box."""
    draw = ImageDraw.Draw(canvas)
    height = height or round(fnt.size * 2.1)
    width = round(draw.textlength(text, font=fnt)) + 2 * pad_x
    box = [cx - width // 2, y, cx + width // 2, y + height]
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, height // 2, fill=fill)
    canvas.alpha_composite(layer)
    bbox = fnt.getbbox(text)
    ty = y + (height - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw.text((box[0] + pad_x, ty), text, font=fnt, fill=color)
    return box


def chip(canvas, label, value, x, y):
    """Glass chip with small uppercase label and a value. Returns right edge."""
    draw = ImageDraw.Draw(canvas)
    f_label, f_value = font("bold", 22), font("bold", 40)
    inner = max(tracked_width(draw, label, f_label), draw.textlength(value, font=f_value))
    w, h, pad = round(inner) + 2 * 36, 128, 36
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([x, y, x + w, y + h], 28, fill=(255, 255, 255, 34),
                                            outline=(255, 255, 255, 60), width=2)
    canvas.alpha_composite(layer)
    tracked_text(draw, label, x + pad, y + 22, f_label, accent())
    draw.text((x + pad, y + 56), value, font=f_value, fill=WHITE)
    return x + w


def answers_chart(canvas, answers, y):
    """Single stacked bar: correct (accent) vs wrong (muted), direct-labelled."""
    draw = ImageDraw.Draw(canvas)
    total, correct = answers["total"], answers["correct"]
    pct = round(100 * correct / total)
    f_small, f_label = font("regular", 30), font("bold", 32)
    draw.text((MARGIN, y), texts()["answers_title"], font=f_small, fill=MUTED)
    y += 50
    bar_h, x0, x1 = 22, MARGIN, W - MARGIN
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([x0, y, x1, y + bar_h], bar_h // 2, fill=(255, 255, 255, 105))
    if correct:
        split = x0 + round((x1 - x0) * correct / total)
        d.rounded_rectangle([x0, y, split, y + bar_h], bar_h // 2, fill=accent())
        if correct < total:  # 2 px surface gap between the two segments
            d.rectangle([split - 1, y, split + 1, y + bar_h], fill=(18, 18, 22, 255))
    canvas.alpha_composite(layer)
    y += bar_h + 26
    x = MARGIN
    for color, label in ((accent(), texts()["answers_correct"].format(pct=pct)),
                         ((255, 255, 255, 140), texts()["answers_wrong"].format(pct=100 - pct))):
        draw.ellipse([x, y + 9, x + 18, y + 27], fill=color)
        draw.text((x + 32, y), label, font=f_label, fill=WHITE)
        x += 32 + draw.textlength(label, font=f_label) + 48
    return y + 40


def header(canvas, handle, avatar, eyebrow, round_no):
    """Avatar + handle on the left, eyebrow label on the right, below the safe zone."""
    draw = ImageDraw.Draw(canvas)
    y = SAFE_TOP + 50
    x = MARGIN
    if avatar is not None:
        size = 76
        av = cover(avatar.convert("RGB"), (size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        ring = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse([x - 4, y - 4, x + size + 3, y + size + 3], outline=WHITE, width=3)
        canvas.alpha_composite(ring)
        canvas.paste(av, (x, y), mask)
        x += size + 24
    if handle:
        f = font("bold", 36)
        draw.text((x, y + 38 - f.size / 2 - 4), f"@{handle}", font=f, fill=WHITE)
    label = f"{eyebrow}  ·  #{round_no}" if round_no else eyebrow
    f = font("bold", 24)
    tw = tracked_width(draw, label, f)
    tracked_text(draw, label, W - MARGIN - tw, y + 26, f, accent())


def footer(canvas, cta, hint):
    """Pill call to action plus a muted hint, sitting just above the reply bar."""
    draw = ImageDraw.Draw(canvas)
    f_cta, f_hint = font("bold", 38), font("regular", 30)
    y = SAFE_BOTTOM - 150
    pill(canvas, cta, W // 2, y, f_cta, accent(), INK)
    hw = draw.textlength(hint, font=f_hint)
    draw.text(((W - hw) / 2, y + 100), hint, font=f_hint, fill=MUTED)


# --------------------------------------------------------------------------- #
# story layouts
# --------------------------------------------------------------------------- #

def render_question(photo, crop_box, out_path, handle="", avatar=None, round_no=0):
    t = texts()
    canvas = background(photo)
    draw = ImageDraw.Draw(canvas)
    header(canvas, handle, avatar, t["eyebrow_question"], round_no)

    y = text_block(draw, t["headline_question"], MARGIN, SAFE_TOP + 180, font("bold", 104), leading=1.02)

    card_w, card_h = QUESTION_CARD
    card_y = y + 44
    crop = cover(photo.crop(tuple(crop_box)), (card_w, card_h))
    paste_card(canvas, crop, MARGIN, card_y)

    draw = ImageDraw.Draw(canvas)
    text_block(draw, t["note_question"], MARGIN, card_y + card_h + 34, font("regular", 32), fill=MUTED)
    footer(canvas, t["cta_question"], t["hint_question"])
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)


def render_reveal(photo, crop_box, timestamp, place, out_path, handle="", avatar=None, round_no=0, answers=None):
    t = texts()
    canvas = background(photo)
    draw = ImageDraw.Draw(canvas)
    header(canvas, handle, avatar, t["eyebrow_reveal"], round_no)

    if place:
        headline = t["headline_reveal_place"].format(place=place)
    else:
        headline = t["headline_reveal_date"].format(month_year=format_month_year(timestamp))
    f_head = font("bold", 96 if len(max(headline.split("\n"), key=len)) <= 14 else 78)
    y = text_block(draw, headline, MARGIN, SAFE_TOP + 180, f_head, leading=1.02)

    card_w = W - 2 * MARGIN
    card_top = y + 44
    show_chart = bool(answers and answers.get("total"))
    card_max_h = SAFE_BOTTOM - (500 if show_chart else 360) - card_top   # room for chips (+ chart) + footer
    img = fit(photo, card_w, card_max_h)

    # spotlight: dim everything outside this morning's crop, then outline it
    sx, sy = img.width / photo.width, img.height / photo.height
    cb = [round(crop_box[0] * sx), round(crop_box[1] * sy), round(crop_box[2] * sx), round(crop_box[3] * sy)]
    dim = Image.new("RGBA", img.size, (0, 0, 0, 120))
    ImageDraw.Draw(dim).rounded_rectangle(cb, 18, fill=(0, 0, 0, 0))
    img = img.convert("RGBA")
    img.alpha_composite(dim)
    ImageDraw.Draw(img).rounded_rectangle(cb, 18, outline=accent(), width=7)

    x = (W - img.width) // 2
    paste_card(canvas, img, x, card_top)

    chip_y = card_top + img.height + 32
    right = chip(canvas, t["chip_date"], format_date(timestamp), MARGIN, chip_y)
    if place:
        chip(canvas, t["chip_place"], place, right + 20, chip_y)
    if show_chart:
        answers_chart(canvas, answers, chip_y + 128 + 22)

    footer(canvas, t["cta_reveal"], t["hint_reveal"])
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)


# --------------------------------------------------------------------------- #
# Instagram data
# --------------------------------------------------------------------------- #

def fetch_all_media():
    fields = "id,caption,media_type,media_url,timestamp,permalink,children{id,media_type,media_url}"
    data = api("GET", "me/media", fields=fields, limit=100)
    items = list(data.get("data", []))
    while data.get("paging", {}).get("next"):
        data = api("GET", data["paging"]["next"])
        items.extend(data.get("data", []))
    return items


def candidate_images(post):
    """Return list of (media_id, url) for still images in a post."""
    if post["media_type"] == "IMAGE":
        return [(post["id"], post["media_url"])]
    if post["media_type"] == "CAROUSEL_ALBUM":
        return [
            (c["id"], c["media_url"])
            for c in post.get("children", {}).get("data", [])
            if c["media_type"] == "IMAGE"
        ]
    return []


def download(url):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGB")


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
LOCATION_RE = re.compile(
    r'"location":\{"__typename":"XDTLocationDict","pk":\d+,"lat":[-\d.]+,"lng":[-\d.]+,"name":"((?:[^"\\]|\\.)*)"')


def shortcode_of(permalink):
    """'https://www.instagram.com/p/DXSHMY_jD5S/' -> 'DXSHMY_jD5S' (bare codes pass through)."""
    return permalink.rstrip("/").split("/")[-1]


def lookup_post_location(shortcode):
    """Read the location tag from the public post page.

    Returns the tag name, or None when the post has no tag. Raises when the
    page could not be read (rate limit, login wall), so callers can back off.
    """
    resp = requests.get(f"https://www.instagram.com/p/{shortcode}/", headers=BROWSER_HEADERS, timeout=30)
    html = resp.text
    if resp.status_code != 200 or "RootContentQueryRelayPreloader" not in html:
        raise RuntimeError(f"post page not readable (HTTP {resp.status_code})")
    match = LOCATION_RE.search(html)
    if match:
        return json.loads('"' + match.group(1) + '"')
    if '"location":null' in html:
        return None
    raise RuntimeError("could not find location data in the page")


def lookup_place(post):
    """Place for a post: locations.json first, otherwise the public post page.

    Only import_locations.py writes locations.json; the daily run keeps the
    place in state/current.json instead, so the two never edit the same file.
    """
    locations = load_json(LOCATIONS_FILE, {})
    code = shortcode_of(post.get("permalink", ""))
    for key in (post["id"], code):
        if key in locations:
            return locations[key] or ""
    try:
        return lookup_post_location(code) or ""
    except Exception as exc:
        print(f"Location lookup for {code} failed: {exc}")
        return ""


def profile():
    """(user id, handle, avatar image or None)."""
    me = api("GET", "me", fields="id,user_id,username,profile_picture_url")
    handle = config()["handle"] or me.get("username", "")
    avatar = None
    if me.get("profile_picture_url"):
        try:
            avatar = download(me["profile_picture_url"])
        except Exception as exc:  # avatar is decoration only
            print(f"Could not load profile picture: {exc}")
    return me.get("user_id") or me["id"], handle, avatar


# --------------------------------------------------------------------------- #
# replies to the question story
# --------------------------------------------------------------------------- #

STOPWORDS = {"und", "the", "and", "der", "die", "das", "bei", "von", "auf", "dem", "den", "des", "im", "in", "am"}


def normalize(text):
    text = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("à", "a")):
        text = text.replace(a, b)
    return text


def accepted_terms(state):
    """Words that make a reply count as right: place words, caption hashtags, year, month."""
    terms = set()
    for word in re.split(r"[^a-z0-9]+", normalize(state.get("place") or "")):
        if len(word) >= 3 and word not in STOPWORDS:
            terms.add(word)
    for tag in re.findall(r"#(\w+)", normalize(state.get("caption") or "")):
        if len(tag) >= 4:
            terms.add(tag)
    dt = parse_ts(state["timestamp"])
    terms.add(str(dt.year))
    for lang in config()["texts"].values():
        terms.add(normalize(lang["months"][dt.month - 1]))
    return terms


def is_correct(reply, terms):
    text = normalize(reply)
    return any(term in text for term in terms)


def count_answers(state):
    """Count replies (one per person) that arrived after the question went out.

    Returns {"total": n, "correct": k} or None when messages cannot be read
    (missing permission, connected tools disabled). Reply texts are never
    stored, only the two numbers.
    """
    since = state.get("question_published_at")
    if not since:
        return None
    terms = accepted_terms(state)
    try:
        me = api("GET", "me", fields="username", fatal=False)["username"]
        total = correct = 0
        page = api("GET", "me/conversations", platform="instagram", fields="id,updated_time", limit=50, fatal=False)
        while True:
            for conv in page.get("data", []):
                if conv.get("updated_time", "") < since:
                    continue
                messages = api("GET", conv["id"], fields="messages{id,created_time,from,message}", fatal=False)
                for msg in messages.get("messages", {}).get("data", []):  # newest first
                    if msg.get("created_time", "") < since or msg.get("from", {}).get("username") == me:
                        continue
                    text = msg.get("message") or ""
                    if len(re.sub(r"[^a-z0-9]", "", normalize(text))) < 2:
                        continue  # emoji or reaction only
                    total += 1
                    correct += is_correct(text, terms)
                    break  # one answer per person: the latest reply
            next_url = page.get("paging", {}).get("next")
            if not next_url or all(c.get("updated_time", "") < since for c in page.get("data", [])):
                break
            page = api("GET", next_url, fatal=False)
    except Exception as exc:
        print(f"Could not read replies, reveal goes out without the chart: {exc}")
        return None
    print(f"Replies: {total}, right: {correct}")
    return {"total": total, "correct": correct}


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def random_crop_box(image, candidates=8):
    """Pick a random crop in the card's aspect ratio.

    Several random boxes are sampled and the one with the most visual detail
    (highest luminance spread) wins, so the quiz does not show plain sky or
    a blank wall.
    """
    cfg = config()
    aspect = QUESTION_CARD[0] / QUESTION_CARD[1]
    short = min(image.width, image.height)
    gray = image.convert("L")

    def sample():
        h = int(short * random.uniform(cfg["crop_fraction_min"], cfg["crop_fraction_max"]))
        w = min(int(h * aspect), image.width)
        h = int(w / aspect)
        left = random.randint(0, image.width - w)
        top = random.randint(0, image.height - h)
        return [left, top, left + w, top + h]

    boxes = [sample() for _ in range(candidates)]
    return max(boxes, key=lambda b: ImageStat.Stat(gray.crop(tuple(b))).stddev[0])


def paused():
    """Pause switch, read from the environment (GitHub repository variables)."""
    if os.environ.get("QUIZ_FORCE", "").strip().lower() in ("1", "true", "yes"):
        return False
    until = os.environ.get("QUIZ_PAUSED_UNTIL", "").strip()
    if until and today() <= until:
        return f"paused until {until}"
    if os.environ.get("QUIZ_PAUSED", "").strip().lower() in ("1", "true", "yes", "on"):
        return "paused (QUIZ_PAUSED)"
    return False


def make_question():
    if reason := paused():
        print(f"Skipping today's question: {reason}")
        return
    state = load_json(STATE_FILE, {})
    if state.get("stage") == "question_prepared":
        print("A question is already prepared and not published yet; reusing it.")
        return
    cfg = config()
    history = load_json(HISTORY_FILE, [])
    recent = set(history[-cfg["avoid_repeat_last_n"]:])

    posts = [p for p in fetch_all_media() if candidate_images(p)]
    if not posts:
        sys.exit("No image posts found on this account")
    fresh = [p for p in posts if p["id"] not in recent] or posts
    post = random.choice(fresh)
    media_id, url = random.choice(candidate_images(post))
    _, handle, avatar = profile()

    photo = download(url)
    crop_box = random_crop_box(photo)
    date = today()
    round_no = len(history) + 1
    out = DOCS / f"{date}-question.jpg"
    DOCS.mkdir(exist_ok=True)
    render_question(photo, crop_box, out, handle, avatar, round_no)

    save_json(STATE_FILE, {
        "stage": "question_prepared",
        "date": date,
        "round": round_no,
        "post_id": post["id"],
        "media_id": media_id,
        "media_url": url,
        "permalink": post.get("permalink"),
        "timestamp": post["timestamp"],
        "caption": (post.get("caption") or "")[:300],
        "place": lookup_place(post),
        "crop_box": crop_box,
        "question_image": out.name,
    })
    history.append(post["id"])
    save_json(HISTORY_FILE, history)
    print(f"Prepared {out.name} from post {post.get('permalink')}")


def make_reveal():
    state = load_json(STATE_FILE, {})
    if state.get("stage") == "reveal_prepared":
        print("A reveal is already prepared; reusing it.")
        return
    if state.get("stage") != "question_published":
        sys.exit(f"Nothing to reveal (stage is {state.get('stage')!r})")
    _, handle, avatar = profile()
    answers = count_answers(state)
    photo = download(state["media_url"])
    out = DOCS / f"{state['date']}-reveal.jpg"
    render_reveal(photo, state["crop_box"], state["timestamp"], state.get("place"), out,
                  handle, avatar, state.get("round", 0), answers)
    state["answers"] = answers
    state["stage"] = "reveal_prepared"
    state["reveal_image"] = out.name
    save_json(STATE_FILE, state)
    print(f"Prepared {out.name}")


def wait_until_public(url, attempts=30, delay=10):
    for _ in range(attempts):
        try:
            r = requests.head(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(delay)
    sys.exit(f"Image never became reachable at {url}")


def publish_story(image_url):
    user, _, _ = profile()
    container = api("POST", f"{user}/media", media_type="STORIES", image_url=image_url)["id"]
    for _ in range(30):
        status = api("GET", container, fields="status_code")["status_code"]
        if status == "FINISHED":
            break
        if status == "ERROR":
            sys.exit(f"Container {container} failed: {api('GET', container, fields='status')}")
        time.sleep(5)
    return api("POST", f"{user}/media_publish", creation_id=container)["id"]


def publish(kind):
    state = load_json(STATE_FILE, {})
    expected = "question_prepared" if kind == "question" else "reveal_prepared"
    if state.get("stage") != expected:
        print(f"Nothing to publish for {kind} (stage is {state.get('stage')!r})")
        return
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        sys.exit("PUBLIC_BASE_URL is not set")
    image_url = f"{base}/{state[kind + '_image']}"
    wait_until_public(image_url)
    story_id = publish_story(image_url)
    if kind == "question":
        state["stage"] = "question_published"
        state["question_story_id"] = story_id
        state["question_published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
        save_json(STATE_FILE, state)
    else:
        save_json(STATE_FILE, {"stage": "idle", "last": {**state, "reveal_story_id": story_id}})
    print(f"Published {kind} story {story_id}")


def refresh():
    data = api("GET", "refresh_access_token", grant_type="ig_refresh_token")
    days = data.get("expires_in", 0) // 86400
    print(f"::add-mask::{data['access_token']}")
    print(f"New token valid for {days} days")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"token={data['access_token']}\n")
    else:
        print(data["access_token"])


def demo(path, handle="", avatar_path="", place="Lissabon"):
    photo = Image.open(path).convert("RGB")
    avatar = Image.open(avatar_path).convert("RGB") if avatar_path else None
    crop_box = random_crop_box(photo)
    DOCS.mkdir(exist_ok=True)
    render_question(photo, crop_box, DOCS / "demo-question.jpg", handle, avatar, 12)
    render_reveal(photo, crop_box, "2019-05-12T14:03:00+0000", place, DOCS / "demo-reveal.jpg", handle, avatar, 12,
                  answers={"total": 12, "correct": 7})
    print("Wrote docs/demo-question.jpg and docs/demo-reveal.jpg")


def main(argv):
    if len(argv) >= 2 and argv[0] == "make" and argv[1] in ("question", "reveal"):
        return make_question() if argv[1] == "question" else make_reveal()
    if len(argv) >= 2 and argv[0] == "publish" and argv[1] in ("question", "reveal"):
        return publish(argv[1])
    if argv[:1] == ["refresh"]:
        return refresh()
    if 2 <= len(argv) <= 5 and argv[0] == "demo":
        return demo(*argv[1:])
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
