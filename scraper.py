#!/usr/bin/env python3
"""
scraper.py — Google Maps Review Scraper for ServiceTasker
Called by Laravel's ScraperExecutorService via subprocess.

Usage:
    python scraper.py --url="https://maps.app.goo.gl/xxx" \
                      --sort=newest \
                      --profile-dir=/var/www/storage/app/browser-profile \
                      --output=json \
                      --headless

Prints a single JSON object to stdout on success.
Prints nothing to stdout and exits non-zero on failure.
All logging goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ── logging to stderr only (stdout is reserved for JSON output) ─────────────

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

# ──────────────────────────────────────────────────────────────────────────────
# Selector lists  (ordered: most reliable → fallback)
# ──────────────────────────────────────────────────────────────────────────────

SEL_SEARCH_BOX   = ["input#searchboxinput", "input[name='q']", "input[aria-label*='Search']"]
SEL_BIZ_NAME     = ["h1.DUwDvf", "h1[class*='fontHeadlineLarge']", "//h1"]
SEL_CATEGORY     = ["button[jsaction*='category']", "span[jsaction*='category']", "div.skqShb button"]
SEL_ADDRESS      = ["button[data-item-id='address']", "div[data-item-id='address']"]
SEL_WEBSITE      = ["a[data-item-id='authority']"]
SEL_PHONE        = ["button[data-item-id*='phone']", "div[data-item-id*='phone'] .Io6YTe"]
SEL_RATING       = ["div.F7nice span[aria-hidden='true']", "span.ceNzKf"]
SEL_REVIEW_COUNT = ["div.F7nice span[aria-label*='review']", "span[aria-label*='review']"]
SEL_REVIEWS_TAB  = [
    "button[aria-label*='Reviews']",
    "div[role='tab'][aria-label*='Reviews']",
    "//button[@aria-label[contains(.,'Reviews')]]",
]
SEL_SORT_BTN     = ["button[aria-label*='Sort reviews']", "//button[@aria-label[contains(.,'Sort')]]"]
SEL_CARDS        = ["div[data-review-id]", "div.jftiEf"]
SEL_MORE_BTNS    = ["button[aria-label*='See more']", "button.w8nwRe"]
SEL_SCROLL_CONT  = ["div.m6QErb[tabindex]", "div[role='feed']", "div.DxyBCb"]
SEL_CONSENT = [
    "button:has-text('Accept all')",
    "button:has-text('Tout accepter')",
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Priimti viską')",   # Lithuanian
    "button[aria-label*='Accept']",
    "form[action*='consent'] button[type='submit']",
]
SEL_CAPTCHA      = ["#recaptcha", "iframe[src*='recaptcha']", "div.g-recaptcha"]

SORT_LABELS = {
    "newest":        ["Newest"],
    "most_relevant": ["Most relevant"],
    "highest":       ["Highest rating"],
    "lowest":        ["Lowest rating"],
}

CAPTCHA_MAX_WAIT = 300   # seconds


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _rhash(name: str, text: str, date: str) -> str:
    return hashlib.md5(f"{name}|{text[:80]}|{date}".encode()).hexdigest()[:14]


def _parse_rating(text: str) -> Optional[float]:
    m = re.search(r"(\d[\d.,]*)\s*(?:out of\s*\d+)?\s*star", text, re.I)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    m = re.search(r"^(\d[\d.,]*)$", text.strip())
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def _parse_count(text: str) -> int:
    nums = re.findall(r"[\d,]+", text)
    if nums:
        try:
            return int(nums[0].replace(",", ""))
        except ValueError:
            pass
    return 0


def _relative_to_iso(text: str) -> Optional[str]:
    """Convert '2 months ago' → ISO-8601 string. Returns None if not parseable."""
    if not text:
        return None
    text = text.lower().strip()
    now  = datetime.utcnow()
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta_map = {
            "second": timedelta(seconds=n), "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),   "day":    timedelta(days=n),
            "week":   timedelta(weeks=n),   "month":  timedelta(days=n * 30),
            "year":   timedelta(days=n * 365),
        }
        return (now - delta_map[unit]).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "just now" in text or "a moment ago" in text:
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


async def _delay(lo: float = 0.5, hi: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


# ──────────────────────────────────────────────────────────────────────────────
# Scraper class
# ──────────────────────────────────────────────────────────────────────────────

class MapsScraper:
    def __init__(self, url: str, sort_by: str, profile_dir: str, headless: bool):
        self.url         = url
        self.sort_by     = sort_by
        self.profile_dir = profile_dir
        self.headless    = headless

        self._pw   = None
        self._ctx  = None
        self._page = None

        self.review_cache = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw  = await async_playwright().start()

        os.makedirs(self.profile_dir, exist_ok=True)

        log.info("Launching browser (headless=%s, profile=%s)", self.headless, self.profile_dir)
        # self._ctx = await self._pw.chromium.launch_persistent_context(
        #     user_data_dir=self.profile_dir,
        #     headless=self.headless,
        #     slow_mo=60,
        #     viewport={"width": 1380, "height": 900},
        #     locale="en-US",
        #     timezone_id="UTC",
        #     args=[
        #         "--disable-blink-features=AutomationControlled",
        #         "--no-sandbox",
        #         "--disable-infobars",
        #     ],
        #     ignore_default_args=["--enable-automation"],
        # )
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            slow_mo=60,
            viewport={"width": 1380, "height": 900},

            locale="en-US",

            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9"
            },

            timezone_id="UTC",

            args=[
                "--lang=en-US",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],

            ignore_default_args=["--enable-automation"],
        )
        await self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._page = (
            self._ctx.pages[0] if self._ctx.pages
            else await self._ctx.new_page()
        )
        self._page.set_default_timeout(15_000)
        return self

    async def __aexit__(self, *_):
        try:
            if self._ctx:  await self._ctx.close()
            if self._pw:   await self._pw.stop()
        except Exception:
            pass

    # ── selector helpers ──────────────────────────────────────────────────────

    async def _find(self, selectors: List[str], parent=None, timeout: int = 8000):
        root = parent or self._page
        for sel in selectors:
            try:
                loc = (root.locator(f"xpath={sel}") if sel.startswith("//")
                       else root.locator(sel)).first
                await loc.wait_for(state="attached", timeout=timeout)
                return loc
            except Exception:
                continue
        return None

    async def _text(self, selectors: List[str], parent=None) -> str:
        el = await self._find(selectors, parent)
        if not el:
            return ""
        try:
            return (await el.inner_text(timeout=3000)).strip()
        except Exception:
            return ""

    async def _attr(self, selectors: List[str], attr: str) -> str:
        el = await self._find(selectors)
        if not el:
            return ""
        try:
            return (await el.get_attribute(attr) or "").strip()
        except Exception:
            return ""

    async def _click(self, selectors: List[str], retries: int = 3) -> bool:
        for _ in range(retries):
            el = await self._find(selectors)
            if not el:
                await asyncio.sleep(1.5)
                continue
            try:
                await el.scroll_into_view_if_needed()
                await _delay(0.2, 0.5)
                await el.click()
                return True
            except Exception as e:
                log.debug("click error: %s", e)
                await asyncio.sleep(1.5)
        return False

    # ── page state ────────────────────────────────────────────────────────────

    async def _page_state(self) -> str:
        url = self._page.url.lower()
        if "sorry" in url:
            return "sorry"
        if (
            "consent" in url
            or "before you continue" in (await self._page.title()).lower()
            or "prieš pereinant" in (await self._page.title()).lower()
        ):
            return "consent"
        if "captcha" in url or "recaptcha" in url:
            return "captcha"
        for sel in SEL_CAPTCHA:
            try:
                if await self._page.query_selector(sel):
                    return "captcha"
            except Exception:
                pass
        if "google.com/maps" in url:
            return "maps"
        return "unknown"

    async def _handle_consent(self) -> None:
        try:
            buttons = await self._page.locator("button").all()

            for btn in buttons:
                try:
                    text = (await btn.inner_text()).strip()

                    if any(x in text.lower() for x in [
                        "accept",
                        "agree",
                        "priimti",
                        "accepter",
                        "akzeptieren"
                    ]):
                        await btn.click()
                        log.info("Consent accepted: %s", text)
                        await _delay(2, 3)
                        return
                except:
                    pass

        except Exception as e:
            log.warning("Consent handling failed: %s", e)

    async def _wait_captcha(self) -> bool:
        log.warning("CAPTCHA detected — waiting for manual solve (max %ds)", CAPTCHA_MAX_WAIT)
        deadline = time.time() + CAPTCHA_MAX_WAIT
        while time.time() < deadline:
            await asyncio.sleep(3)
            state = await self._page_state()
            if state != "captcha":
                log.info("CAPTCHA resolved")
                return True
        log.error("CAPTCHA timeout")
        return False

    # ── 1. Navigate ───────────────────────────────────────────────────────────

    async def navigate(self) -> bool:
        log.info("Navigating to Google Maps")
        # await self._page.goto("https://www.google.com/maps", wait_until="domcontentloaded", timeout=30_000)
        await self._page.goto(
            "https://www.google.com/maps?hl=en",
            wait_until="domcontentloaded",
            timeout=30000
        )
        await _delay(1.5, 2.5)

        state = await self._page_state()
        if state == "consent":
            await self._handle_consent()
            state = await self._page_state()
        if state == "captcha":
            if not await self._wait_captcha():
                return False
        if state == "sorry":
            log.error("Google blocked this IP")
            return False
        return True

    # ── 2. Search ─────────────────────────────────────────────────────────────

    async def search(self) -> bool:
        log.info("Navigating directly to URL: %s", self.url)
        log.info(
            "LANG=%s",
            await self._page.evaluate("navigator.language")
        )

        log.info(
            "URL=%s",
            self._page.url
        )
        try:
            # await self._page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
            url = self.url

            if "?" in url:
                url += "&hl=en"
            else:
                url += "?hl=en"

            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )
        except Exception as e:
            log.error("Navigation error: %s", e)
            return False

        await _delay(2.0, 3.5)
        state = await self._page_state()

        if state == "consent":
            await self._handle_consent()
            await _delay(1.5, 2.5)
            state = await self._page_state()

        if state == "captcha":
            if not await self._wait_captcha():
                return False

        # Check listing is open
        if await self._is_listing_open():
            log.info("Listing loaded directly")
            return True

        # Fallback: open Maps home and use search box
        log.info("Listing not found via direct URL — falling back to Maps search")
        return await self._search_via_box()

    async def _search_via_box(self) -> bool:
        await self._page.goto("https://www.google.com/maps", wait_until="domcontentloaded", timeout=30_000)
        await _delay(1.0, 2.0)
        state = await self._page_state()
        if state == "consent":
            await self._handle_consent()

        box = await self._find(SEL_SEARCH_BOX, timeout=10_000)
        if not box:
            log.error("Maps search box not found")
            return False

        await box.click()
        await box.fill("")
        for char in self.url:
            await box.type(char, delay=random.randint(30, 80))
        await _delay(0.5, 1.0)
        await box.press("Enter")
        await _delay(3.0, 5.0)

        return await self._is_listing_open()

    async def _is_listing_open(self) -> bool:
        for sel in SEL_BIZ_NAME:
            try:
                loc = (self._page.locator(f"xpath={sel}") if sel.startswith("//")
                       else self._page.locator(sel)).first
                await loc.wait_for(state="visible", timeout=4000)
                return True
            except Exception:
                pass
        return False

    # ── 3. Business metadata ──────────────────────────────────────────────────

    async def extract_meta(self) -> Dict[str, Any]:
        log.info("Extracting business metadata")
        meta: Dict[str, Any] = {
            "business_name":  "",
            "place_id":       "",
            "address":        "",
            "phone":          "",
            "website":        "",
            "overall_rating": None,
            "total_reviews":  0,
        }

        meta["business_name"] = await self._text(SEL_BIZ_NAME)
        meta["address"]       = await self._attr(SEL_ADDRESS, "aria-label") or await self._text(SEL_ADDRESS)
        meta["website"]       = await self._attr(SEL_WEBSITE, "href")
        phone_raw             = await self._attr(SEL_PHONE, "aria-label") or await self._text(SEL_PHONE)
        meta["phone"]         = re.sub(r"[^\d\s\+\-\(\)]", "", phone_raw).strip()

        # Clean address prefix
        if meta["address"]:
            meta["address"] = re.sub(r"^address:\s*", "", meta["address"], flags=re.I).strip()

        # Rating
        rating_text = await self._text(SEL_RATING)
        if not rating_text:
            el = await self._find(["span[aria-label*='star']"])
            if el:
                rating_text = await el.get_attribute("aria-label") or ""
        if rating_text:
            meta["overall_rating"] = _parse_rating(rating_text)

        # Review count
        # rev_text = await self._attr(SEL_REVIEW_COUNT, "aria-label") or await self._text(SEL_REVIEW_COUNT)
        # if rev_text:
        #     meta["total_reviews"] = _parse_count(rev_text)
        # Review count
        try:
            f7_text = await self._page.locator("div.F7nice").first.inner_text()
            log.info("F7nice text = %r", f7_text)

            nums = re.findall(r"\((\d+)\)", f7_text)
            if nums:
                meta["total_reviews"] = int(nums[0])

        except Exception as e:
            log.warning("Review count parse failed: %s", e)

        # Place ID from URL
        m = re.search(r"place/([^/]+)", self._page.url)
        if m:
            meta["place_id"] = m.group(1)

        log.info("Meta: name=%r  rating=%s  reviews=%d",
                 meta["business_name"], meta["overall_rating"], meta["total_reviews"])
        return meta

    # ── 4. Open reviews tab ───────────────────────────────────────────────────

    async def open_reviews_tab(self) -> bool:
        log.info("Opening Reviews tab")

        tabs = await self._page.locator("[role='tab']").all()

        for tab in tabs:
            try:
                text = (await tab.inner_text()).strip().lower()

                if any(x in text for x in [
                    "review",
                    "atsiliep",
                    "avis",
                    "bewertung",
                    "opinion",
                    "comment"
                ]):
                    await tab.click()
                    await _delay(2, 3)

                    log.info("Reviews tab clicked: %s", text)
                    return True

            except Exception:
                pass

        log.error("Reviews tab not found")
        return False

    # ── 5. Sort ───────────────────────────────────────────────────────────────

    async def sort_reviews(self) -> None:
        labels = SORT_LABELS.get(self.sort_by, ["Newest"])
        log.info("Setting sort: %s", self.sort_by)

        try:
            await self._click(SEL_SORT_BTN)
            await _delay(0.7, 1.2)
            for label in labels:
                for tmpl in [
                    f"//li[contains(.,'{label}')]",
                    f"//div[@role='menuitem'][contains(.,'{label}')]",
                    f"[role='menuitemradio']:has-text('{label}')",
                ]:
                    try:
                        loc = (self._page.locator(f"xpath={tmpl}") if tmpl.startswith("//")
                               else self._page.locator(tmpl)).first
                        await loc.wait_for(state="visible", timeout=3000)
                        await loc.click()
                        log.info("Sort set to '%s'", label)
                        await _delay(1.5, 2.5)
                        return
                    except Exception:
                        pass
        except Exception as e:
            log.warning("Sort failed: %s", e)

    # ── 6. Scroll ─────────────────────────────────────────────────────────────

    # async def scroll_all(self) -> int:
    async def scroll_all(self, expected_reviews: int) -> int:
        log.info("Scrolling to load all reviews")
        container    = await self._find(SEL_SCROLL_CONT, timeout=5000)
        last_count   = 0
        stall_streak = 0

        start_time = time.time()

        while True:
            if time.time() - start_time > 120:
                log.info("Maximum scroll time reached")
                break

            await self._expand_more()

            # count = await self._count_cards()
            # cards = await self._page.locator("div[data-review-id]").all()

            # for card in cards:
            #     review = await self._parse_card(card)
            #     if review:
            #         self.review_cache[review["review_id"]] = review

            locator = self._page.locator("div[data-review-id]")
            count_cards = await locator.count()

            for i in range(count_cards):
                review = await self._parse_card(locator.nth(i))
                if review:
                    self.review_cache[review["review_id"]] = review

            count = len(self.review_cache)
            log.info(
                "Loaded %d / %d reviews",
                count,
                expected_reviews
            )
            if count > last_count:
                stall_streak = 0
                # log.info("  %d reviews loaded", count)
            else:
                stall_streak += 1
                # log.debug("  stall %d/6", stall_streak)

            # if stall_streak >= 6:
            #     log.info("All reviews loaded: %d cards", count)
            #     break
            if expected_reviews > 0 and count >= expected_reviews:
                log.info("Loaded all %d reviews", count)
                break

            if stall_streak >= 5:
                log.info("No more reviews after multiple scrolls")
                break

            last_count = count
            # delta = random.randint(700, 1100)
            # try:
            #     if container:
            #         await container.evaluate(f"el => el.scrollBy(0, {delta})")
            #     else:
            #         await self._page.evaluate(f"window.scrollBy(0, {delta})")
            # except Exception:
            #     await self._page.evaluate(f"window.scrollBy(0, {delta})")

            # await asyncio.sleep(random.uniform(1.2, 2.4))
            cards = self._page.locator("div[data-review-id]")

            try:
                if await cards.count() > 0:
                    # Last loaded review tak scroll karo
                    # await cards.last.scroll_into_view_if_needed()
                    await cards.nth(await cards.count() - 1).scroll_into_view_if_needed()
                elif container:
                    # Fallback
                    await container.evaluate("el => el.scrollBy(0, 1000)")
                else:
                    await self._page.evaluate("window.scrollBy(0, 1000)")
            except Exception:
                await self._page.evaluate("window.scrollBy(0, 1000)")

            # Google ko next reviews load karne ka time do
            # await asyncio.sleep(random.uniform(2.5, 4.0))
            await asyncio.sleep(1.5)

        return last_count

    # async def _count_cards(self) -> int:
    #     for sel in SEL_CARDS:
    #         try:
    #             els = await self._page.locator(sel).all()
    #             if els:
    #                 return len(els)
    #         except Exception:
    #             pass
    #     return 0
    async def _count_cards(self):
        return len(self.review_cache)

    async def _expand_more(self) -> None:
        for sel in SEL_MORE_BTNS:
            try:
                buttons = await self._page.locator(sel).all()
                for btn in buttons:
                    try:
                        await btn.click(timeout=1500)
                        await asyncio.sleep(0.15)
                    except Exception:
                        pass
            except Exception:
                pass

    # ── 7. Extract reviews ────────────────────────────────────────────────────

    # async def extract_reviews(self) -> List[Dict[str, Any]]:
    #     log.info("Extracting review data")
    #     card_sel = None
    #     for sel in SEL_CARDS:
    #         try:
    #             els = await self._page.locator(sel).all()
    #             if els:
    #                 card_sel = sel
    #                 break
    #         except Exception:
    #             pass

    #     if not card_sel:
    #         log.warning("No review cards found")
    #         return []

    #     cards   = await self._page.locator(card_sel).all()
    #     reviews = []
    #     seen    = set()

    #     for card in cards:
    #         r = await self._parse_card(card)
    #         if not r:
    #             continue
    #         rid = r.get("review_id", "")
    #         if rid and rid in seen:
    #             continue
    #         seen.add(rid)
    #         reviews.append(r)

    #     log.info("Extracted %d reviews", len(reviews))
    #     return reviews
    async def extract_reviews(self) -> List[Dict[str, Any]]:
        log.info("Extracted %d reviews", len(self.review_cache))
        return list(self.review_cache.values())

    async def _parse_card(self, card) -> Optional[Dict[str, Any]]:
        try:
            review_id = ""
            try:
                review_id = await card.get_attribute("data-review-id") or ""
            except Exception:
                pass

            # Reviewer name
            name = ""
            for sel in ["div.d4r55", "button.WEBjve", "span.TSUbDb"]:
                try:
                    name = (await card.locator(sel).first.inner_text(timeout=1500)).strip()
                    if name:
                        break
                except Exception:
                    pass

            # Profile URL
            profile_url = ""
            for sel in ["a.WEBjve", "button.WEBjve"]:
                try:
                    href = await card.locator(sel).first.get_attribute("href", timeout=1500)
                    if href:
                        profile_url = href
                        break
                except Exception:
                    pass

            # Rating
            rating = None
            for sel in ["span[aria-label*='star']", "span[role='img'][aria-label*='star']"]:
                try:
                    aria = await card.locator(sel).first.get_attribute("aria-label", timeout=1500) or ""
                    r    = _parse_rating(aria)
                    if r is not None:
                        rating = int(round(r))
                        break
                except Exception:
                    pass

            # Review text
            text = ""
            for sel in ["span.wiI7pd", "[class*='review-full-text']", "div.MyEned span"]:
                try:
                    text = (await card.locator(sel).first.inner_text(timeout=1500)).strip()
                    if text:
                        break
                except Exception:
                    pass

            # Date
            date_raw = ""
            for sel in ["span.rsqaWe", "span[class*='dehysf']"]:
                try:
                    date_raw = (await card.locator(sel).first.inner_text(timeout=1500)).strip()
                    if date_raw:
                        break
                except Exception:
                    pass

            date_iso = _relative_to_iso(date_raw)

            # Photos
            photos_count = 0
            try:
                photos = await card.locator("button.Tya61d, img.YQ4gaf").all()
                photos_count = len(photos)
            except Exception:
                pass

            # Owner reply
            owner_reply      = ""
            owner_reply_date = ""
            for sel in ["div.CDe7pd", "[class*='owner-reply']"]:
                try:
                    loc = card.locator(sel).first
                    await loc.wait_for(state="attached", timeout=1000)
                    owner_reply = (await loc.inner_text(timeout=1500)).strip()
                    # Try to separate reply date
                    date_loc = loc.locator("span.rsqaWe").first
                    try:
                        owner_reply_date = (await date_loc.inner_text(timeout=800)).strip()
                        date_iso_reply   = _relative_to_iso(owner_reply_date)
                        owner_reply_date = date_iso_reply or owner_reply_date
                        owner_reply      = owner_reply.replace(owner_reply_date, "").strip()
                    except Exception:
                        pass
                    break
                except Exception:
                    pass

            if not review_id:
                review_id = _rhash(name, text, date_raw)

            return {
                "review_id":            review_id,
                "reviewer_name":        name,
                "reviewer_profile_url": profile_url,
                "rating":               rating,
                "review_text":          text,
                "review_date":          date_iso or date_raw,
                "photos_count":         photos_count if photos_count else None,
                "owner_reply":          owner_reply or None,
                "owner_reply_date":     owner_reply_date or None,
            }

        except Exception as exc:
            log.debug("Card parse error: %s", exc)
            return None

    # ── Main run ──────────────────────────────────────────────────────────────

    async def run(self) -> Dict[str, Any]:
        if not await self.navigate():
            raise RuntimeError("Failed to load Google Maps")
        if not await self.search():
            raise RuntimeError("Failed to load business listing")

        meta = await self.extract_meta()

        if not await self.open_reviews_tab():
            await self._page.screenshot(path="headless-debug.png", full_page=True)
            # print(self._page.url)
            # Return partial result (metadata only)
            return {**meta, "reviews": []}

        await self.sort_reviews()
        # await self.scroll_all()
        await self.scroll_all(meta["total_reviews"])
        reviews = await self.extract_reviews()

        return {
            "business_name":  meta["business_name"],
            "place_id":       meta["place_id"],
            "address":        meta["address"],
            "phone":          meta["phone"],
            "website":        meta["website"],
            "overall_rating": meta["overall_rating"],
            "total_reviews":  meta["total_reviews"],
            "reviews":        reviews,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Google Maps review scraper")
    parser.add_argument("--url",         required=True,  help="Google Maps URL")
    parser.add_argument("--sort",        default="newest",
                        choices=["newest", "most_relevant", "highest", "lowest"])
    parser.add_argument("--profile-dir", default=os.path.join(os.path.dirname(__file__),
                                                               "browser-profile"))
    parser.add_argument("--output",      default="json", choices=["json"])
    parser.add_argument("--headless",    action="store_true")
    args = parser.parse_args()

    async def _run():
        async with MapsScraper(
            url=args.url,
            sort_by=args.sort,
            profile_dir=args.profile_dir,
            headless=args.headless,
        ) as s:
            return await s.run()

    try:
        result = asyncio.run(_run())
        # Only JSON goes to stdout — Laravel reads this
        sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str))
        sys.stdout.flush()
    except Exception as exc:
        log.error("Scraper failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
