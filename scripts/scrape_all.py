import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import hashlib
from supabase import create_client
import unicodedata
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
import os
from pathlib import Path
import socket
import urllib3.util.connection as urllib3_cn


# CONFIG
# define urls to scrape, separating hub sites and tab sites. Home (leukerbad.ch) is automatically included
hub_sites = ['thermen', 'sommer', 'winter', 'aufenthalt', 'destination', 'nachhaltigkeit']
tab_sites = ['therme', 'alpentherme', 'therme51', 'wellness', 'gesundheit', 'wandern', 'biken', 'klettern', 'trailrunning', 'sommeraktivitaten',
            'ski', 'snowpark', 'langlaufen', 'schlitteln', 'winterwandern', 'schneeschuhlaufen', 'winteraktivitaten',
            'wintercard', 'summercard', 'magic', 'guestcard', 'indypass',
            'summeracts', 'winteracts', 'weyo',
            'kontakt', 'anreise', 'camping', 'gutschein', 'feedback', 'jobs', 'medien', 'dynamic-pricing', 'versicherung',
            'gemeinde', 'albinen', 'inden', 'varen', 'weininsel', 'naturpark',
            'restaurant-rinderhutte', 'restaurant-leukerbad-therme', 'restaurant-sportarena',
            'annullationsversicherung', 'nachhaltigkeit', 'swisstainable', 'myclimate', 'barrierefreies-reisen'
            'tourismus', 'tourismusorganisationen', 'leistungstragerverbande', 'webcam', 'medien'
          ]

# METHODS
# ---------------- HELPERS -------------
# helper function to collapse internal whitespace and strip
def norm_text(s):
    if s:
        return " ".join(s.split()).strip()
    else:
        return ""

def text_hash(s: str) -> str:
    return hashlib.sha1(s.encode('utf-8')).hexdigest()

def to_e164(ch: str | None) -> str | None:
    if not ch: return None
    # Keep + and digits only; normalize spaces/dashes
    digits = re.sub(r'[^+\d]', '', ch)
    return digits if digits.startswith('+') else None

def contains_tel(node) -> bool:
    return bool(node and node.select_one('a[href^="tel:"]'))

def slugify_de(s: str) -> str:
    s = s.strip().lower()
    s = (s.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss"))
    s = s.replace("\u00ad", "")  # soft hyphen
    s = unicodedata.normalize('NFKD', s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "section"

# helper for deciding which url (cta or actual page) to refer to. Only called for components that should have CTA link on them if it's internal and not a shop link
def referral_url(component_type: str, cta_url: str, page_url: str):
    # if there is no cta url, we have to refer to the page url
    if cta_url is None or cta_url == "":
        referral_url = page_url

    # if the cta is an internal link and not a shop or image (assets) link, use that
    elif re.search('https://leukerbad.ch.*', cta_url) and not re.search('.*shop.*', cta_url) and not re.search('.*assets.*', cta_url):
        referral_url = cta_url

    # catchall/backup is page_url
    else:
        referral_url = page_url

    return referral_url

# helper for making tab-aware urls
def find_section_fragment(node) -> str | None:
    if node is None:
        return None

    # prefer tab container -> tab heading
    tab = node.find_parent(attrs={"data-selector": "tab-content-container"})
    if tab:
        h = tab.select_one('[data-selector="tab-content-heading"] h3')
        if h:
            return f"#{slugify_de(h.get_text(' '))}"

    # fallback: nearest visible section heading (heading-3/h2) above this node
    container = node.find_parent(['section', 'div'], class_='inlay') or node.find_parent('section') or node
    candidates = container.find_all(['h2', 'h3'])
    nearest = None
    for h in candidates:
        classes = h.get("class", [])
        if ('heading-3' in classes) or (h.name == 'h2'):
            nearest = h  # use "last seen" because bs4 often lacks sourceline
    if nearest:
        return f"#{slugify_de(nearest.get_text(' '))}"
    else:
        return None

# -- helpers to find/compute a group key--
# find enclosing slider ID if card sits inside a slider wrapper
def get_slider_id_for_card(card):
    wrapper = card.find_parent(class_='activity-slider-narrow-wrapper')
    return wrapper.get('data-id') if wrapper and wrapper.has_attr('data-id') else None

# walk backward in DOM to find the nearest slider wrapper and take its data-id
def find_previous_slider_id(node, max_steps=500):
    cur = node
    steps = 0
    while cur and steps < max_steps:
        # previous elements in document order
        cur = cur.find_previous()
        steps += 1
        if not cur: break
        if getattr(cur, 'name', None) and 'activity-slider-narrow-wrapper' in (cur.get('class') or []):
            return cur.get('data-id')
    return None

# if no slider id exists, generate a stable group key from main content
def fallback_group_from_main(title, summary):
    basis = (title or "") + "|" + (summary or "")
    return "main:" + text_hash(basis)

# if a card has no slider id, link it to the nearest following main segment
def fallback_group_for_card(card):
    main = card.find_next('div', class_='w-full lg:w-5/12 px-gap')
    if main:
        t = main.select_one('h3.heading-3')
        d = main.select_one('div.text-lg')
        title   = norm_text(t.get_text(" ")) if t else None
        summary = norm_text(d.get_text(" ")) if d else None
        return fallback_group_from_main(title, summary)
    return None

# --- make content hash for DB ---
def content_hash(record: dict) -> str:
    # choose the fields that define “content equality” for you
    payload = "||".join([
        norm_text(record.get("page_url")),
        norm_text((record.get("ref_url") or "")),
        norm_text(record.get("component")),
        norm_text(record.get("page_title")),
        str(record.get("tile_index") or record.get("card_index") or record.get("segment_index") or 0),
        norm_text(record.get("title")),
        norm_text(record.get("summary")),
        norm_text(record.get("cta_url")),
        norm_text(record.get("price_text")),
        norm_text(record.get("phone_display")),
        norm_text(record.get("phone_e164")),
        norm_text(record.get("group_id"))
    ])

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

# --- deduplication helpers ----
def coalesce(a, b):
    # pick the better value: prefer non-empty strings / non-null values
    if a is None or (isinstance(a, str) and not a.strip()):
        return b
    return a

def pick_longer(a, b):
    if (a or "") and (b or ""):
        return a if len(a) >= len(b) else b
    return a or b

def merge_rows(r1, r2):
    # same key; merge fields sensibly
    return {
        "page_url":     r1["page_url"],
        "component":    r1["component"],
        "group_id":     r1["group_id"],
        "item_index":   r1["item_index"],
        "page_title":   coalesce(r1.get("page_title"),   r2.get("page_title")),
        "title":        pick_longer(r1.get("title"),      r2.get("title")),
        "summary":      pick_longer(r1.get("summary"),    r2.get("summary")),
        "cta_url":      coalesce(r1.get("cta_url"),       r2.get("cta_url")),
        "ref_url":      coalesce(r1.get("ref_url"), r2.get("ref_url")),
        "price_text":   pick_longer(r1.get("price_text"), r2.get("price_text")),
        "phone_display":coalesce(r1.get("phone_display"), r2.get("phone_display")),
        "phone_e164":   coalesce(r1.get("phone_e164"),    r2.get("phone_e164")),
        "content_hash": r1.get("content_hash") or r2.get("content_hash"),
        "updated_at": r1.get("updated_at"),
        "language": "de"
    }

def dedupe_by_conflict_key(rows):
    seen = {}
    collisions = []
    for r in rows:
        # normalize key
        page_url = r["page_url"]
        group_id = r.get("group_id") or ""          # IMPORTANT: never None
        key = (page_url, r["component"], group_id, r["item_index"])
        r["group_id"] = group_id                    # keep payload consistent

        if key in seen:
            collisions.append(key)
            seen[key] = merge_rows(seen[key], r)    # keep best
        else:
            seen[key] = r

    if collisions:
        # optional: print a few to debug
        unique_collisions = sorted(set(collisions))
        print(f"[dedupe] collapsed {len(collisions)} duplicate rows into {len(unique_collisions)} keys")
        # If you want to inspect which extractor produced duplicates, log here.

    return list(seen.values())

# ---------- EXTRACTOR FUNCTIONS ------------------
# only sort of an extractor. pulls title to pass to all components on the page for organisation of information later
def extract_title(soup: BeautifulSoup):
    hero_wrap = soup.select_one('.parallax-container .header-image-content')
    if hero_wrap:
        hero_title_el    = hero_wrap.select_one('h1')
        hero_title   = norm_text(hero_title_el.get_text(" ")) if hero_title_el else None

    else:
        hero_title = None

    return hero_title

# ---- special extractors for home (leukerbad.ch) ----
# pulls offer_cards linked with the section text
# no url or topic passed because this is only called for home
def extract_home_sections(soup):
    internal_record = []
    base_url = 'https://leukerbad.ch'

    blocks = soup.select('.text-image-block')
    for block_idx, block in enumerate(blocks):
        # left column (main text)
        left = block.select_one('.w-full.lg\\:w-5\\/12.px-gap')
        if not left:
            # Sometimes spacing differs; mild fallback
            left = block.select_one('.lg\\:w-5\\/12')

        title_el = left.select_one('h2') if left else None
        body_el  = left.select_one('.lg\\:text-xl, .text-lg, .text-base') if left else None

        title   = norm_text(title_el.get_text(" ")) if title_el else None
        summary = norm_text(body_el.get_text(" ")) if body_el else None

        # Compute a stable group id from the left column content
        group_id = "home:" + text_hash(f"{title or ''}|{summary or ''}|{block_idx}")

        if any([title, summary]):
            internal_record.append({
                "component": "home_main",
                "page_url": 'https://leukerbad.ch',
                "ref_url": 'https://leukerbad.ch',
                "page_title": 'Home',
                "segment_index": block_idx,
                "group_id": group_id, # to link the cards
                "title": title,
                "summary": summary
            })

        # right column (cards inside the same block)
        right = block.select_one('.color-world-slider-wrapper') or block
        cards = right.select('.offer-card')

        for card_idx, card in enumerate(cards):
            # CTA
            a = card.find('a', href=True, recursive=False) or card.select_one('a[href]')
            card_cta = urljoin(base_url, a['href']) if a and a.get('href') else None

            # Title & summary
            title_el = card.select_one('.card-limit-heading-5') or card.select_one('.heading-5')
            desc_el  = card.select_one('.card-limit-text')
            card_title   = norm_text(title_el.get_text(" ")) if title_el else None
            card_summary = norm_text(desc_el.get_text(" ")) if desc_el else None

            # Price (some offer-cards have a price-tag)
            price_el = card.select_one('.price-tag')
            price_text = norm_text(price_el.get_text(" ")) if price_el else None

            if any([card_title, card_summary, card_cta, price_text]):
                internal_record.append({
                    "component": "offer_card",
                    "page_url": 'https://leukerbad.ch',
                    "ref_url": 'https://leukerbad.ch',
                    "page_title": 'Home',
                    "card_index": card_idx,
                    "group_id": group_id,    # links to the main text
                    "title": card_title,
                    "summary": card_summary,
                    "cta_url": card_cta,
                    "price_text": price_text
                })

    return internal_record

# newsletter signup also only called for home (leukerbad.ch) currently
def extract_newsletter_signup(soup: BeautifulSoup):
    internal_record = []
    page_url = 'https://leukerbad.ch'

    blocks = soup.select('.newsletter-signup')
    for idx, blk in enumerate(blocks):
        inlay = blk.select_one('.inlay') or blk

        title_el = inlay.select_one('h2.heading-3') or inlay.select_one('h2')
        title = norm_text(title_el.get_text(" ")) if title_el else None

        widget = inlay.select_one('#app-NewsletterSignupWidget')
        widget_lang = widget.get('data-lang') if widget and widget.has_attr('data-lang') else None

        if title:
            internal_record.append({
                "component": "newsletter_signup",
                "page_url": page_url,
                "ref_url": 'https://leukerbad.ch',
                "page_title": 'Home',
                "segment_index": idx,
                "title": title,
                "summary": 'Newsletter Anmeldung'
            })

    return internal_record

# ---- General extractors ----
# extractor for the whole top section (hero + breadcrumb + intro). Called for all (main, hub pages, tab pages)
def extract_top_section(soup: BeautifulSoup, page_url: str, topic: str):
    internal_record = []

    # Hero (parallax header)
    # structure: .parallax-container > .header-image-overlay > .header-image-content
    hero_wrap = soup.select_one('.parallax-container .header-image-content')
    if hero_wrap:
        hero_title_el    = hero_wrap.select_one('h1')
        hero_sub_el      = hero_wrap.select_one('p:not(.uppercase)')
        hero_title   = norm_text(hero_title_el.get_text(" ")) if hero_title_el else None
        hero_summary = norm_text(hero_sub_el.get_text(" ")) if hero_sub_el else None

        internal_record.append({
            "component": "hero",
            "page_url": page_url,
            "ref_url": page_url,
            "page_title": topic,
            "tile_index": 0,
            "title": hero_title,
            "summary": hero_summary
        })

    # breadcrumb. Might not be useful but is in for now
    bc = soup.select_one('.breadcrumb')
    if bc:
        # anchors + trailing text node
        crumb_parts = [norm_text(a.get_text(" ")) for a in bc.select('a')]
        # trailing text node (often the current page name) lives in bc.contents after anchors
        tail = norm_text(bc.get_text(" "))
        # If anchors exist, try to reconstruct
        breadcrumb_text = " > ".join([p for p in crumb_parts if p]) or tail
        current_title = None
        if crumb_parts:
            # attempt to get last non-empty piece from the whole string after the last anchor text
            # simple heuristic: the full bc text minus joined anchors
            current_title = tail.split(crumb_parts[-1], 1)[-1]
            current_title = norm_text(current_title)
        if not current_title:
            current_title = crumb_parts[-1] if crumb_parts else tail

        internal_record.append({
            "component": "breadcrumb",
            "page_url": page_url, # always return straight page_url for header/hero
            "ref_url": page_url,
            "page_title": topic,
            "tile_index": 0,
            "title": current_title,
            "summary": breadcrumb_text
        })

    # Intro block (title + paragraph. Omit right-hand link list because I have no idea what I'd do with it)
    intro = soup.select_one('.intro')
    if intro:
        intro_title_el = intro.select_one('h2')
        intro_text_el  = intro.select_one('.text-lg')
        intro_title = norm_text(intro_title_el.get_text(" ")) if intro_title_el else None
        intro_summary = norm_text(intro_text_el.get_text(" ")) if intro_text_el else None

        internal_record.append({
            "component": "intro_block",
            "page_url": page_url, # always return straight page_url for header/hero
            "ref_url": page_url,
            "page_title": topic,
            "tile_index": 0,
            "title": intro_title,
            "summary": intro_summary
        })

    return internal_record

# ---- extractors for hub pages ----
# extract counter from top section
def extract_counter(soup: BeautifulSoup, page_url: str, topic: str):
    internal_record = []

    # for each counter, extract icon + number + label
    # section: .activity-counter ... each block has span[data-count] and h4 label
    counters = soup.select('.activity-counter .flex.flex-col.items-center')
    for i, block in enumerate(counters):
        label_el = block.select_one('h4')
        value_el = block.select_one('span[data-count]')
        label = norm_text(label_el.get_text(" ")) if label_el else None
        value = value_el.get('data-count') if value_el else None
        if value is None and value_el:
            summary = norm_text(value_el.get_text(" "))

        else:
            summary = f"{value} {label} in der Destination Leukerbad."

        if label or value:
            internal_record.append({
                "component": "stat",
                "ref_url": page_url,
                "page_url": page_url,
                "page_title": topic,
                "tile_index": i,
                "title": label,
                "summary": summary
            })

    return internal_record

# extracts big tiles (main content on hub pages)
def extract_big_tiles(soup: BeautifulSoup, page_url: str, topic: str):
    internal_record = []
    tiles = soup.select('.tile.outdooractive-tile[data-selector="tile-container"]')
    for idx, tile in enumerate(tiles):
        title_el   = tile.select_one('.tile-frontcard h3.card-limit-heading')
        summary_ps = tile.select('.tile-frontcard h3.card-limit-heading + div p')
        cta_el     = tile.select_one('.tile-frontcard a.button[href]')
        price_el   = tile.select_one('.price-tag')

        title = norm_text(title_el.get_text()) if title_el else None

        # combine unique paragraphs in reading order
        seen, summaries = set(), []
        for p in summary_ps:
            t = norm_text(p.get_text(" "))
            if t and t not in seen:
                seen.add(t)
                summaries.append(t)
        summary = norm_text(" ".join(summaries)) if summaries else None

        cta_url = urljoin(page_url, cta_el.get("href")) if cta_el and cta_el.get("href") else None
        price_text = norm_text(price_el.get_text(" ")) if price_el else None

        # decide referral url
        ref_url = referral_url("big_tile", cta_url, page_url)

        if any([title, summary, cta_url, price_text]):
            internal_record.append({
                "component": "big_tile",
                "page_url": page_url,
                "ref_url": ref_url,
                "page_title": topic,
                "tile_index": idx,
                "title": title,
                "summary": summary,
                "cta_url": cta_url,
                "price_text": price_text
            })
    return internal_record

# extracts text components, linked to related offer_card sections
def extract_main(soup, page_url: str, topic: str):
    internal_record = []
    base_url = page_url.split('#', 1)[0]
    segments = soup.select('div.w-full.lg\\:w-5\\/12.px-gap')

    for idx, seg in enumerate(segments):
        title_el = seg.select_one('h3.heading-3')
        desc_el  = seg.select_one('div.text-lg')
        cta_el   = seg.select_one('a.link-arrow[href]')

        title   = norm_text(title_el.get_text(" ")) if title_el else None
        summary = norm_text(desc_el.get_text(" ")) if desc_el else None
        cta_url = urljoin(base_url, cta_el['href']) if cta_el else None

        # group key: nearest PREVIOUS slider wrapper id; else content hash
        slider_id = find_previous_slider_id(seg)
        group_id = slider_id or fallback_group_from_main(title, summary)

        # tab-aware URL
        frag = (find_section_fragment(seg) or "")
        best_page_url = base_url + frag

        if any([title, summary, cta_url]):
            internal_record.append({
                "component": "main_segment",
                "page_url": best_page_url,
                "ref_url": best_page_url,
                "page_title": topic,
                "segment_index": idx,
                "group_id": group_id,      # shared key for main segment and matching offer_cards
                "title": title,
                "summary": summary,
                "cta_url": cta_url
            })
    return internal_record

# extracts small offer cards on hub pages, linked to related text section
def extract_offer_cards(soup, page_url: str, topic: str):
    internal_record = []
    base_url = page_url.split('#', 1)[0]
    cards = soup.select('.offer-card')

    for idx, card in enumerate(cards):
        # Prefer a direct child <a> if present; fall back to any descendant
        a = card.find('a', href=True, recursive=False) or card.select_one('a[href]')
        cta_url = urljoin(base_url, a['href']) if a and a.get('href') else None

        # Title & description
        title_el = card.select_one('.card-limit-heading-5') or card.select_one('.heading-5')
        desc_el  = card.select_one('.card-limit-text')
        title   = norm_text(title_el.get_text(" ")) if title_el else None
        summary = norm_text(desc_el.get_text(" ")) if desc_el else None

        # price tag
        price_el = card.select_one('.price-tag')
        price_text = None
        if price_el:
            # Get all visible text and strip whitespace/newlines
            price_text = norm_text(price_el.get_text(" "))

        # group key: enclosing slider id if any; else nearest following main segment
        group_id = get_slider_id_for_card(card) or fallback_group_for_card(card)

        # tab-aware URL
        frag = (find_section_fragment(card) or "")
        best_page_url = base_url + frag

        # decide referral url
        ref_url = referral_url("offer_card", cta_url, page_url)

        if any([title, summary, cta_url]):
            internal_record.append({
                "component": "offer_card",
                "page_url": best_page_url,
                "ref_url": ref_url,
                "page_title": topic,
                "card_index": idx,
                "group_id": group_id,     # shared key for main segment and matching offer_cards
                "title": title,
                "summary": summary,
                "cta_url": cta_url,
                "price_text": price_text
            })

    return internal_record

# ---- tab site extractors ----
def extract_image_text_components(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []
    components = soup.select('.image-text-component')

    for comp_idx, comp in enumerate(components):
        # Each row is a flex container holding image + text halves
        rows = comp.select('.flex')

        for block_idx, row in enumerate(rows):
            # Look at the row's immediate child columns only
            cols = [c for c in row.find_all('div', recursive=False)]
            # Pick the column that has a title (h3.card-limit-heading)
            text_col = next((c for c in cols if c.select_one('h3.card-limit-heading')), None)
            if not text_col:
                continue  # skip rows without a text column

            # title
            title_el = text_col.select_one('h3.card-limit-heading')
            title = norm_text(title_el.get_text(" ")) if title_el else None

            # summary (the first direct div under the text column)
            text_div = text_col.find('div', recursive=False)
            summary = norm_text(text_div.get_text(" ")) if text_div else None

            # CTA (button in the text column)
            cta_el = text_col.select_one('a.button[href]')
            cta_url = urljoin(base_url, cta_el['href']) if cta_el else None

            # price block (row-scoped, under the image half)
            price_el = row.select_one('.price-tag')
            price_text = norm_text(price_el.get_text(" ")) if price_el else None

            # tab awareness
            frag = (find_section_fragment(row)
                    or find_section_fragment(comp)
                    or "")
            best_page_url = base_url + frag

            # decide referral url
            ref_url = referral_url("image_text_component", cta_url, best_page_url)

            if any([title, summary, cta_url, price_text]):
                internal_records.append({
                    "component": "image_text_component",
                    "page_url": best_page_url,
                    "ref_url": ref_url,
                    "page_title": topic,
                    "tile_index": block_idx,   # index within this component
                    "title": title,
                    "summary": summary,
                    "cta_url": cta_url,
                    "price_text": price_text
                })

    return internal_records

def extract_two_col_layout(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []
    components = soup.select('.two-column-layout')

    for sec_idx, comp in enumerate(components):
        # rows inside this section
        rows = comp.select('.flex.flex-wrap')
        for row_idx, row in enumerate(rows):
            # each card column is an immediate child .w-full md:w-1/2 ...
            cols = [c for c in row.find_all('div', recursive=False)]
            # grab descendant card containers inside each col
            cards = []
            for col in cols:
                card = col.select_one('.shadow-md.h-full.flex.flex-col') or col
                # only keep columns that actually contain a heading for this component
                if card.select_one('h2.card-limit-heading-4'):
                    cards.append(card)

            for card_idx, card in enumerate(cards):
                # title
                title_el = card.select_one('h2.card-limit-heading-4') \
                           or card.select_one('.heading-4')
                title = norm_text(title_el.get_text(" ")) if title_el else None

                # summary (rich text under title, lives inside .card-limit-text)
                desc_el = card.select_one('.card-limit-text')
                summary = norm_text(desc_el.get_text(" ")) if desc_el else None

                # CTA (button at bottom)
                cta_el = card.select_one('a.button[href]')
                cta_url = urljoin(base_url, cta_el['href']) if cta_el else None

                # price
                price_el = card.select_one('.price-tag')
                price_text = norm_text(price_el.get_text(" ")) if price_el else None

                # tab awareness
                frag = (find_section_fragment(card)
                        or find_section_fragment(row)
                        or find_section_fragment(comp)
                        or "")
                best_page_url = base_url + frag

                # decide referral url
                ref_url = referral_url("two_col_tile", cta_url, best_page_url)

                if any([title, summary, cta_url, price_text]):
                    internal_records.append({
                        "component": "two_col_tile",
                        "page_url": best_page_url,
                        "ref_url": ref_url,
                        "page_title": topic,
                        "tile_index": card_idx + row_idx * 2,
                        "title": title,
                        "summary": summary,
                        "cta_url": cta_url,
                        "price_text": price_text
                    })

    return internal_records

def extract_three_col_layout(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []

    # Each section has a wrapper .two-column-layout (even when it's actually 3 cols)
    sections = soup.select('.two-column-layout')

    for sec_idx, section in enumerate(sections):
        # Rows within the section
        rows = section.select('.flex.flex-wrap')
        for row_idx, row in enumerate(rows):
            # Each card column is an immediate child with a card container inside
            cols = [c for c in row.find_all('div', recursive=False)]
            card_containers = []
            for col in cols:
                card = col.select_one('.shadow-md.h-full.flex.flex-col')
                if card:
                    # Filter to only those that look like this component (has h3 / heading-5)
                    if card.select_one('h3.card-limit-heading-5') or card.select_one('.heading-5'):
                        card_containers.append(card)

            for col_idx, card in enumerate(card_containers):
                # title
                title_el = card.select_one('h3.card-limit-heading-5') or card.select_one('.heading-5')
                title = norm_text(title_el.get_text(" ")) if title_el else None

                # summary
                desc_el = card.select_one('.card-limit-text')
                summary = norm_text(desc_el.get_text(" ")) if desc_el else None

                # cta button in the rare case it exists
                cta_el = card.select_one('a.button[href]')
                cta_url = urljoin(base_url, cta_el['href']) if cta_el else None

                # tab awareness
                frag = (find_section_fragment(card)
                        or find_section_fragment(row)
                        or find_section_fragment(section)
                        or "")
                best_page_url = base_url + frag

                # decide referral url
                ref_url = referral_url("big_tile", cta_url, best_page_url)

                if any([title, summary]):
                    internal_records.append({
                        "component": "three_col_tile",
                        "page_url": best_page_url,
                        "ref_url": ref_url,
                        "page_title": topic,
                        "tile_index": col_idx + row_idx * max(1, len(card_containers)),
                        "title": title,
                        "summary": summary,
                        "cta_url": cta_url, # sometimes does have CTA button
                        "price_text": None # no price tag for 3cl. sometimes there is but we're not going near that chaos
                    })

    return internal_records

# general text elements. Might want some tidying this is a mess but it works
def extract_text(soup: BeautifulSoup, base_url: str, topic: str):
    internal_record = []

    # Scope: prefer main content only (drops hero/header which are otherwise pulled out as text)
    root = soup.select_one('main') or soup
    segments = root.select('div.inlay')

    # helper to decide if an inlay is just a container for other components we already extract
    def looks_like_nontext_component(root) -> bool:
        return bool(root.select_one(
            # cards & grids & tiles
            '.offer-card, .two-column-layout, .image-text-component, .tile, .outdooractive-tile, '
            # FAQs
            '.question, .question-content, '
            # sliders/carousels
            '.activity-slider-narrow-wrapper, .owl-carousel, '
            # price/metrics blocks
            '.price-tag, .activity-counter, '
            # hero/breadcrumb/intros (some pages put these inside inlays)
            '.header-image-content, .parallax-container, .breadcrumb, '
            # footer
            '.footer-main-links, .footer-highlighted-links, footer, '
            # info banners (big orange blocks) 
            '.info-banner'
        ))

    # helper to pull a "substantial" text node from an inlay (used for both titled/untitled)
    def find_text_block(inlay):
        # prefer common wrapper shapes and reject anything with a phone link
        el = inlay.select_one(':scope > .flex .w-full') or inlay.select_one(':scope > .w-full')
        if el and contains_tel(el):
            el = None

        # fallback: any sizeable direct child under this inlay
        if not el:
            for cand in inlay.select(':scope > *'):
                if getattr(cand, 'name', None) and cand.name not in ('script', 'style'):
                    el = cand
                    break
        # last resort: direct <p> children
        if not el:
            p = inlay.select_one(':scope > p')
            if p:
                el = p
        return el

    for idx, seg in enumerate(segments):
        # Skip obvious non-text component containers and contact containers (phone number)
        if contains_tel(seg):
            continue
        if looks_like_nontext_component(seg):
            continue
        if seg.find_parent(class_='parallax-container'):
            continue
        if seg.select_one('h1'):  # hero-like big heading
            continue

        title_el = seg.select_one('h2.heading-3')

        if title_el is not None:
            # titled section - case for most text
            title = norm_text(title_el.get_text(" "))

            # body likely in the next inlay
            body_inlay = seg.find_next_sibling('div', class_='inlay') or seg.find_next('div', class_='inlay')

            # if the body inlay itself is a contact/footer/etc., skip it
            if body_inlay and (
                    looks_like_nontext_component(body_inlay) or contains_tel(body_inlay) or body_inlay.find_parent(
                    'footer')):
                continue

            desc_el = find_text_block(body_inlay) if body_inlay else None
            summary = norm_text(desc_el.get_text(" ")) if desc_el else None

            frag = find_section_fragment(body_inlay or seg) or ""
            best_page_url = base_url + frag

            internal_record.append({
                "component": "text",
                "page_url": best_page_url,
                "ref_url": best_page_url,
                "page_title": topic,
                "segment_index": idx,
                "title": title,
                "summary": summary
            })

        else:
            # for untitled text
            # Only keep if there is a substantial text block in THIS inlay
            desc_el = find_text_block(seg)
            if not desc_el:
                continue

            summary = norm_text(desc_el.get_text(" "))
            if not summary or len(summary) < 40:
                continue  # avoid tiny stray labels

            # Optional "title": try first strong/heading-ish child, else leave None
            title = None
            first_strong = seg.select_one(':scope strong')
            if first_strong:
                # Use a trimmed first strong as a lightweight title (optional)
                t = norm_text(first_strong.get_text(" "))
                if t and len(t) <= 60:
                    title = t

            frag = (find_section_fragment(seg)
                    or "")
            best_page_url = base_url + frag

            internal_record.append({
                "component": "text",
                "page_url": best_page_url,
                "ref_url": best_page_url,
                "page_title": topic,
                "segment_index": idx,
                "title": title,        # may be None for pure body sections
                "summary": summary    # text from paragraph(s)
            })

    return internal_record

def extract_inspirations(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []

    sliders = soup.select('.activity-slider-narrow-wrapper')

    for slider_idx, slider in enumerate(sliders):
        slider_id = slider.get('data-id')

        # --- Header block right before the slider (big title + body + optional button) ---
        # Try the nearest previous .inlay, then look inside it for .header-image-overlay
        header_block = None
        prev_inlay = slider.find_previous('div', class_='inlay')
        if prev_inlay:
            header_block = prev_inlay.select_one('.header-image-overlay')
        if not header_block:
            # Fallback: any earlier header-image-overlay
            header_block = slider.find_previous(class_='header-image-overlay')

        if header_block:
            header_title_el = header_block.select_one('h3')
            header_copy_el = header_block.select_one('.text-base, .md\\:text-lg')
            header_cta_el  = header_block.select_one('a.button[href]')

            header_title = norm_text(header_title_el.get_text(" ")) if header_title_el else None
            header_copy  = norm_text(header_copy_el.get_text(" ")) if header_copy_el else None
            header_cta   = urljoin(base_url, header_cta_el['href']) if header_cta_el else None

            frag = (find_section_fragment(header_block)
                    or find_section_fragment(slider)
                    or "")
            best_page_url = base_url + frag

            # decide referral url
            ref_url = referral_url("inspirations_header", header_cta, best_page_url)

            if any([header_title, header_copy, header_cta]):
                records.append({
                    "component": "inspirations_header",
                    "page_url": best_page_url,
                    "ref_url": ref_url,
                    "page_title": topic,
                    "group_id": slider_id,
                    "tile_index": -1,                 # header row for this slider
                    "title": header_title,            # header title
                    "summary": header_copy,           # the body copy paragraph from the header
                    "cta_url": header_cta              # CTA button at the bottom of the header copy
                })

        # --- Cards in the slider ---
        cards = slider.select('.offer-card')
        for card_idx, card in enumerate(cards):
            title_el = card.select_one('.card-limit-heading-5') or card.select_one('.heading-5')
            desc_el  = card.select_one('.card-limit-text')

            title   = norm_text(title_el.get_text(" ")) if title_el else None
            summary = norm_text(desc_el.get_text(" ")) if desc_el else None

            # CTA: wrapper <a> (preferred) or any descendant <a>
            a = card.find('a', href=True, recursive=False) or card.select_one('a[href]')
            cta_url = urljoin(base_url, a['href']) if a else None

            frag = (find_section_fragment(card)
                    or find_section_fragment(slider)
                    or "")
            best_page_url = base_url + frag

            # decide referral url
            ref_url = referral_url("inspirations_slider", cta_url, best_page_url)

            if any([title, summary, cta_url]):
                internal_records.append({
                    "component": "inspirations_slider",
                    "page_url": best_page_url,
                    "ref_url": ref_url,
                    "page_title": topic,
                    "group_id": slider_id,          # links cards to the header above
                    "tile_index": card_idx,
                    "title": title,
                    "summary": summary,
                    "cta_url": cta_url
                })

    return internal_records

def extract_contacts(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []

    # Look only inside page content; adjust scope if needed
    tel_links = soup.select('.inlay a[href^="tel:"]') or soup.select('a[href^="tel:"]')

    seen = set()  # (title, phone_e164) to dedupe

    for tel in tel_links:
        # Find the nearest "column" ancestor that has a DIRECT child <h4> (provider name)
        col = None
        name_el = None

        cur = tel
        for _ in range(8):  # climb a few levels max
            cur = cur.find_parent('div')
            if not cur: break
            # find a direct child h4 (avoid overlay h4s)
            name_el = next((c for c in getattr(cur, 'children', [])
                            if getattr(c, 'name', None) == 'h4'), None)
            if name_el:
                col = cur
                break
        if not (col and name_el):
            continue  # not a recognizable contact block

        title = norm_text(name_el.get_text(" "))
        if not title:
            continue

        # Identify phone block and address block within this column
        # Phone block is the closest ancestor with class 'mt-6', else use tel's parent
        phone_block = tel.find_parent('div', class_='mt-6') or tel.parent
        # Address is the nearest PREVIOUS sibling .mt-6 before the phone block
        address_block = None
        if phone_block and phone_block.parent is col:
            # scan siblings from name_el forward to keep order
            children = [c for c in col.children if getattr(c, 'name', None)]
            try:
                i_name = children.index(name_el)
            except ValueError:
                i_name = -1
            # find address as first .mt-6 after name_el and before phone_block
            for c in children[i_name+1:]:
                if c is phone_block:
                    break
                if 'mt-6' in (c.get('class') or []):
                    address_block = c
                    break
        # Fallback: any .mt-6 in col without a tel: link
        if not address_block:
            for c in col.select('.mt-6'):
                if not c.select_one('a[href^="tel:"]'):
                    address_block = c
                    break

        address_text = norm_text(address_block.get_text(" ")) if address_block else None

        # normalise phone numbers
        phone_display = norm_text(tel.get_text(" "))
        phone_e164 = to_e164(phone_display) or to_e164(tel.get('href'))

        # tab awareness
        frag = (find_section_fragment(col)
                or find_section_fragment(name_el)
                or "")
        best_page_url = base_url + frag

        # dedupe in case phone block is repeated
        dedup_key = (title, phone_e164)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # save records
        if any([title, phone_display, address_text]):
            internal_records.append({
                "component": "contact",
                "page_url": best_page_url,
                "ref_url": best_page_url,
                "page_title": topic,
                "tile_index": 0,            # no more than 1 per section anyway
                "title": title,             # provider name saved as title
                "summary": address_text,    # postal address saved as text
                "phone_display": phone_display,
                "phone_e164": phone_e164
            })

    return internal_records

# extract Q&A blocks
def extract_faqs(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []

    faqs = soup.select('.question')
    for idx, q in enumerate(faqs):
        # Question
        q_el = q.select_one('h5')
        question = norm_text(q_el.get_text(" ")) if q_el else None

        # Answer (may have <p> inside, join all)
        a_el = q.select_one('.question-content')
        answer = norm_text(a_el.get_text(" ")) if a_el else None

        if not question and not answer:
            continue

        # tab awareness
        frag = (find_section_fragment(q)
                or "")
        best_page_url = base_url + frag

        internal_records.append({
            "component": "faq",
            "page_url": best_page_url,
            "ref_url": best_page_url,
            "page_title": topic,
            "tile_index": idx,   # order of FAQ
            "title": question,   # question as title
            "summary": answer   # answer as summary
        })

    return internal_records

# would prob extract any table but we only have price tables afaik
def extract_price_tables(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []
    tables = soup.select('.leukerbad-table table') # "leukerbad table" okay team

    for t_index, table in enumerate(tables):
        frag = (find_section_fragment(table)
                or find_section_fragment(table.find_previous('h3'))
                or "")
        best_page_url = base_url + frag

        # find headers to build summary context (if needed)
        headers = [norm_text(th.get_text(" ")) for th in table.select('thead th')]
        rows = table.select('tbody tr')

        for r_index, tr in enumerate(rows):
            cells = [norm_text(td.get_text(" ")) for td in tr.select('td')]
            if not any(cells):
                continue

            product = cells[0]
            # everything else after product is price info
            price_cols = [c for c in cells[1:] if c]
            price_text = " | ".join(price_cols) if price_cols else None

            # build a readable summary string with headers if present
            if headers and len(headers) == len(cells):
                # e.g. "Erwachsene (ab 16 J.): CHF 699.– | Kinder (ab 6 J.): CHF 399.–"
                summary_parts = []
                for h, v in zip(headers[1:], cells[1:]):
                    if h and v:
                        summary_parts.append(f"{h}: {v}")
                summary = " | ".join(summary_parts) if summary_parts else price_text
            else:
                summary = price_text

            internal_records.append({
                "component": "table",
                "page_url": best_page_url,
                "ref_url": best_page_url,
                "page_title": topic,
                "tile_index": r_index,
                "title": product,
                "summary": summary,
                "price_text": price_text
            })

    return internal_records

# still finding components...
def extract_info_banners(soup: BeautifulSoup, base_url: str, topic: str):
    internal_records = []

    banners = soup.select('.info-banner')
    for idx, b in enumerate(banners):
        # small "kicker" line
        kicker_el = b.select_one('p.text-lg')
        kicker = norm_text(kicker_el.get_text(" ")) if kicker_el else None

        # big line (heading style)
        big_el = b.select_one('.heading-3, .heading-4')
        big = norm_text(big_el.get_text(" ")) if big_el else None

        # title strat: combine if both exist, else whichever we have
        if kicker and big:
            title = f"{kicker} — {big}"
        else:
            title = kicker or big

        # body
        body_el = b.select_one('.info-banner-text')
        summary = norm_text(body_el.get_text(" ")) if body_el else None

        # CTA
        a = b.select_one('a.button[href]')
        cta_url = urljoin(base_url, a['href']) if a else None

        # tab awareness
        frag = (find_section_fragment(b) or
                find_section_fragment(b.find_previous('h3')) or
                "")
        best_page_url = base_url + frag

        # decide referral url
        ref_url = referral_url("info_banner", cta_url, best_page_url)

        if any([title, summary, cta_url]):
            internal_records.append({
                "component": "info_banner",
                "page_url": best_page_url,
                "ref_url": ref_url,
                "page_title": topic,
                "tile_index": idx,
                "title": title,
                "summary": summary,
                "cta_url": cta_url
            })

    return internal_records

# --------- MAIN CODE -------------
# empty list of records
records = []

def force_ipv4() -> socket.AddressFamily:
    return socket.AF_INET

urllib3_cn.allowed_gai_family = force_ipv4

# call home (leukerbad.ch) specifically
url = 'https://leukerbad.ch/'
print('Scraping home...')
topic = 'Home' # set topic manually for main/homepage
resp = requests.get(url, timeout=20)
resp.raise_for_status()
soup = BeautifulSoup(resp.content, 'html.parser')
# call regular hub site extractors first
records.extend(extract_top_section(soup, url, topic))
records.extend(extract_big_tiles(soup, url, topic))
# plus call special method only for home offer card segments
records.extend(extract_home_sections(soup))
records.extend(extract_newsletter_signup(soup))

# iterate urls to scrape hub sites
for site in hub_sites:
    print(f"Scraping {site}...")
    url = 'https://www.leukerbad.ch/' + str(site)
    base_url = url.split('#', 1)[0]

    resp = requests.get(url, timeout=20)
    resp.raise_for_status()

    print("requested url:", url)
    print("final url:", resp.url)
    print("status:", resp.status_code)

    soup = BeautifulSoup(resp.text, "html.parser")

    # pull page title first to pass to all other extractor functions
    topic = extract_title(soup)

    # call extractors
    records.extend(extract_top_section(soup, url, topic)) # add information header etc
    records.extend(extract_counter(soup, url, topic)) # extract counter separately from rest of header
    records.extend(extract_main(soup, url, topic)) # add information from main segments
    records.extend(extract_big_tiles(soup, url, topic)) # collect big cards
    records.extend(extract_offer_cards(soup, url, topic)) #collect small offer cards

# iterate urls to scrape tab sites
for site in tab_sites:
    print(f"Scraping {site}...")
    url = 'https://leukerbad.ch/' + str(site)
    base_url = url.split('#', 1)[0]

    resp = requests.get(url, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # pull page title first to pass to all other extractor functions
    topic = extract_title(soup)

    # extract all components and save to records
    records.extend(extract_top_section(soup, url, topic)) # top section
    records.extend(extract_image_text_components(soup, base_url, topic)) # from Bild/Text Komponente
    records.extend(extract_two_col_layout(soup, base_url, topic)) # from Zweispalten Layout
    records.extend(extract_three_col_layout(soup, base_url, topic)) # three columns/offer layout
    records.extend(extract_inspirations(soup, base_url, topic)) # inspiration carousel layout
    records.extend(extract_text(soup, base_url, topic)) # general (but not long-form text); no vectorising
    records.extend(extract_contacts(soup, base_url, topic)) # contact blocks/phone numbers
    records.extend(extract_faqs(soup, base_url, topic)) # faq blocks
    records.extend(extract_info_banners(soup,base_url, topic)) # info banners
    records.extend(extract_price_tables(soup, base_url, topic)) # (price) tables

component_types = []

for r in records:
    component = r.get("component")

    if component not in component_types:
        component_types.append(component)


print(component_types)

# get timestamp info for updated_at (saved in UTC, match local time at output)
updated_at = datetime.now(timezone.utc).isoformat()

# Resolve the project root (two levels up if needed)
project_root = Path(__file__).resolve().parent.parent
env_path = project_root / ".env"

# get env to connect to supabase
if env_path.exists():
    load_dotenv()

# read config
url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]

# connect to supabase LeukerbaDB project
supabase = create_client(url, key)

# Insert or update rows based on unique constraint (page_url, component, group_id, item_index)
# make sure group_id isn’t null so it matches the unique index
for r in records:
    r["group_id"] = r.get("group_id") or ""

# make rows for DB insertion from records
data = []
for r in records:
    row = {
        "component": r.get("component"),
        "page_url": r.get("page_url") or "https://leukerbad.ch",
        "page_title": r.get("page_title"),
        "item_index": r.get("tile_index") or r.get("card_index") or r.get("segment_index") or 0,
        "group_id": r.get("group_id"), 
        "title": r.get("title"),
        "summary": r.get("summary"),
        "cta_url": r.get("cta_url"),
        "price_text": r.get("price_text"),
        "phone_display": r.get("phone_display"),
        "phone_e164": r.get("phone_e164"),
        "content_hash": content_hash(r),
        "ref_url": r.get("ref_url"),
        "updated_at": updated_at,
        "language": "de"
    }
    data.append(row)

# collapse duplicates on (page_url, component, group_id, item_index)
data = dedupe_by_conflict_key(data)

# fetch existing hashes to skip unchanged rows
pages = sorted({d["page_url"].split('#', 1)[0] for d in data})
existing = {}
for page in pages:
    resp = supabase.table("web_content") \
        .select("page_url,component,group_id,item_index,content_hash") \
        .ilike("page_url", page + "%") \
        .execute()
    for row in resp.data or []:
        key = (row["page_url"], row["component"], row["group_id"], row["item_index"])
        existing[key] = row["content_hash"]

to_upsert = []
for d in data:
    key = (d["page_url"], d["component"], d["group_id"], d["item_index"])
    if existing.get(key) != d["content_hash"]:
        to_upsert.append(d)

if to_upsert:
    # upsert new data
    payload = to_upsert if to_upsert else []  # empty is fine; no-op
    supabase.table("web_content").upsert(
        payload,
        on_conflict="page_url,component,group_id,item_index"
    ).execute()

    print("Table web_content updated successfully")

else:
    print("No changes detected.")