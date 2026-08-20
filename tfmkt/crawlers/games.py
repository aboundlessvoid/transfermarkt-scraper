import json
import re

from crawlee import Request

from tfmkt.common import DEFAULT_BASE_URL, load_parents, build_initial_requests, safe_strip, create_crawler, check_failures
from tfmkt.utils import background_position_in_px_to_minute

#: A half-time score as Transfermarkt prints it under the full-time one: "(1:0)".
#: Whitespace is permitted everywhere inside the brackets, because the score is assembled
#: from several text nodes and how they are indented is the site's business, not ours.
HALF_TIME_RE = re.compile(r'\(\s*(\d+)\s*:\s*(\d+)\s*\)')


def extract_result_annotation(result_box):
    """``(half_time_score, result_type)`` from a match report's ``div.ergebnis-wrap``.

    The annotation under the full-time score lives in ``div.sb-halbzeit`` and is ONE
    element carrying TWO different facts:

    * a half-time score, marked up as ``(<span>0:</span>2)`` -- **three** text nodes;
    * or one of the literals ``AET`` / ``on pens`` / ``(uncontested)``, which say how the
      match was decided and mean there is no half-time score printed at all.

    This used to be ``css('div.sb-halbzeit::text').get()``, which returns the *first*
    text node -- so every normal game yielded the bare opening bracket ``"("`` and the
    half-time score was never scraped. Joining all descendant text is what fixes it, and
    it must be a join rather than a nicer-looking index: the score is split across the
    element and a nested ``<span>`` precisely because Transfermarkt styles the two halves
    differently.

    The sibling ``div.sb-endstand`` has the same shape and is nonetheless read correctly
    by ``::text`` -- there the first text node genuinely is the full-time score. That
    coincidence is why this went unnoticed; do not copy the pattern.
    """
    half_time_box = result_box.css('div.sb-halbzeit')
    if not half_time_box:
        return None, None

    raw = re.sub(r'\s+', ' ', ''.join(half_time_box[0].xpath('.//text()').getall())).strip()
    if not raw:
        return None, None

    match = HALF_TIME_RE.fullmatch(raw)
    if match:
        return f"{match.group(1)}:{match.group(2)}", None
    # Not a score: an annotation about how the result came about. Kept verbatim apart
    # from the brackets Transfermarkt wraps some of them in, so consumers see
    # "uncontested" rather than "(uncontested)" beside a bare "AET".
    return None, raw.strip('()').strip() or None


def extract_game_events(selector, event_type):
    event_elements = selector.xpath(
        f"//div[./h2/@class = 'content-box-headline' and normalize-space(./h2/text()) = "
        f"'{'Penalty shoot-out' if event_type == 'Shootout' else event_type}']"
        f"//div[@class='sb-aktion']"
    )

    events = []
    for e in event_elements:
        event = {}
        event["type"] = event_type
        if event_type == "Shootout":
            event["minute"] = -1
            extra_minute_text = ''
        else:
            background_position_match = re.match(
                "background-position: ([-+]?[0-9]+)px ([-+]?[0-9]+)px;",
                e.xpath("./div[1]/span[@class='sb-sprite-uhr-klein']/@style").get()
            )
            event["minute"] = background_position_in_px_to_minute(
                int(background_position_match.group(1)),
                int(background_position_match.group(2)),
            )
            extra_minute_text = safe_strip(
                e.xpath("./div[1]/span[@class='sb-sprite-uhr-klein']/text()").get()
            )
        if len(extra_minute_text) <= 1:
            extra_minute = None
        else:
            extra_minute = int(extra_minute_text)

        event["extra"] = extra_minute
        event["player"] = {
            "href": e.xpath("./div[@class = 'sb-aktion-spielerbild']/a/@href").get()
        }
        event["club"] = {
            "name": e.xpath("./div[@class = 'sb-aktion-wappen']/a/@title").get(),
            "href": e.xpath("./div[@class = 'sb-aktion-wappen']/a/@href").get()
        }

        action_element = e.xpath("./div[@class = 'sb-aktion-aktion']")
        event["action"] = {
            "result": safe_strip(
                e.xpath("./div[@class = 'sb-aktion-spielstand']/b/text()").get()
            ),
            "description": safe_strip(
                (" ".join([s.strip() for s in action_element.xpath("./text()").getall()])).strip()
                or (" ".join(action_element.xpath(
                    ".//span[@class = 'sb-aktion-wechsel-aus']/span/text()"
                ).getall())).strip()
            ),
            "player_in": {
                "href": action_element.xpath(".//div/a/@href").get()
            },
            "player_assist": {
                "href": action_element.xpath("./a/@href").getall()[1]
                if len(action_element.xpath("./a/@href").getall()) > 1 else None
            }
        }
        events.append(event)

    return events


async def run(parents_arg=None, season=2024, base_url=None):
    base_url = base_url or DEFAULT_BASE_URL
    parents = load_parents(parents_arg)
    requests = build_initial_requests(parents, season, base_url, label='parse', spider_name='games')

    crawler, failures = create_crawler()

    @crawler.router.handler('parse')
    async def parse(context) -> None:
        parent = context.request.user_data['parent']
        sel = context.selector

        cb_data = {'parent': parent}

        # Try named footer links first (domestic competitions)
        next_url = None
        footer_links = sel.css('div.footer-links')
        for footer_link in footer_links:
            text = footer_link.xpath('a//text()').get()
            if text and text.strip() in ["All fixtures & results", "All games"]:
                next_url = footer_link.xpath('a/@href').get()
                break

        # Fallback: find any gesamtspielplan link on the page (tournament competitions
        # like UEFA Euro use a different footer link text or page layout)
        if not next_url:
            gesamtspielplan_links = sel.xpath('//a[contains(@href, "/gesamtspielplan/")]')
            if gesamtspielplan_links:
                next_url = gesamtspielplan_links[0].xpath('@href').get()

        # Final fallback for tournament editions (pokalwettbewerb): the edition
        # `startseite` page exposes neither an "All games" footer nor a
        # gesamtspielplan link, but the schedule lives at the same path with
        # `/startseite/` swapped for `/gesamtspielplan/` (e.g. Copa America,
        # World Cup). Derive it directly from the parent edition href.
        #
        # Derive it from `seasoned_href`, NOT from the bare `href`: the bare one carries
        # no season, so the derived URL 302s to whatever the current season is and the
        # crawler silently scrapes zero games for every past season it is asked for.
        # `build_initial_requests` has already computed the seasoned URL — it is an
        # absolute URL, hence the base strip, since callers re-prepend the base below.
        if not next_url:
            parent_href = parent.get('seasoned_href') or parent.get('href', '')
            if parent_href.startswith(base_url):
                parent_href = parent_href[len(base_url):]
            if '/startseite/' in parent_href:
                next_url = parent_href.replace('/startseite/', '/gesamtspielplan/')

        if next_url:
            await context.add_requests([
                Request.from_url(
                    url=base_url + next_url,
                    label='extract_game_urls',
                    user_data={'base': cb_data},
                )
            ])

    @crawler.router.handler('extract_game_urls')
    async def extract_game_urls_handler(context) -> None:
        base = context.request.user_data['base']
        sel = context.selector

        game_links = sel.css('a.ergebnis-link')
        new_requests = []
        for game_link in game_links:
            href = game_link.xpath('@href').get()
            cb_data = {
                'parent': base['parent'],
                'href': href,
            }
            new_requests.append(
                Request.from_url(
                    url=base_url + href,
                    label='parse_game',
                    user_data={'base': cb_data},
                )
            )

        if new_requests:
            await context.add_requests(new_requests)

    @crawler.router.handler('parse_game')
    async def parse_game(context) -> None:
        base = context.request.user_data['base']
        sel = context.selector

        game_id = int(base['href'].split('/')[-1])

        game_box = sel.css('div.box-content')

        home_club_box = game_box.css('div.sb-heim')
        away_club_box = game_box.css('div.sb-gast')

        home_club_href = home_club_box.css('a::attr(href)').get()
        home_club_name = safe_strip(
            home_club_box.xpath('.//a/@title').get()
        ) or safe_strip(
            home_club_box.xpath('.//a/img/@alt').get()
        )
        away_club_href = away_club_box.css('a::attr(href)').get()
        away_club_name = safe_strip(
            away_club_box.xpath('.//a/@title').get()
        ) or safe_strip(
            away_club_box.xpath('.//a/img/@alt').get()
        )

        home_club_position = home_club_box[0].xpath('p/text()').get()
        away_club_position = away_club_box[0].xpath('p/text()').get()

        datetime_box = game_box.css('div.sb-spieldaten')[0]

        text_elements = [
            element for element in datetime_box.xpath('p//text()')
            if len(safe_strip(element.get())) > 0
        ]

        matchday = safe_strip(text_elements[0].get()).split("  ")[0]
        date = safe_strip(datetime_box.xpath('p/a[contains(@href, "datum")]/text()').get())

        venue_box = game_box.css('p.sb-zusatzinfos')

        stadium = safe_strip(venue_box.xpath('node()')[1].xpath('a/text()').get())
        attendance = safe_strip(venue_box.xpath('node()')[1].xpath('strong/text()').get())
        referee = safe_strip(venue_box.xpath('a[contains(@href, "schiedsrichter")]/@title').get())
        referee_href = venue_box.xpath('a[contains(@href, "schiedsrichter")]/@href').get()

        result_box = game_box.css('div.ergebnis-wrap')
        result = safe_strip(result_box.css('div.sb-endstand::text').get())
        half_time_score, result_type = extract_result_annotation(result_box)

        # Kickoff time - search for time pattern in the date/time area
        kickoff_time = None
        for el in text_elements:
            text = safe_strip(el.get())
            if text and re.match(r'\d{1,2}:\d{2}', text):
                kickoff_time = text
                break

        manager_names = sel.xpath(
            "//tr[(contains(td/b/text(),'Manager')) or (contains(td/div/text(),'Manager'))]/td[2]/a/text()"
        ).getall()
        manager_hrefs = sel.xpath(
            "//tr[(contains(td/b/text(),'Manager')) or (contains(td/div/text(),'Manager'))]/td[2]/a/@href"
        ).getall()

        game_events = (
            extract_game_events(sel, event_type="Goals")
            + extract_game_events(sel, event_type="Substitutions")
            + extract_game_events(sel, event_type="Cards")
            + extract_game_events(sel, event_type="Shootout")
        )

        item = {
            **base,
            'type': 'game',
            'game_id': game_id,
            'home_club': {
                'type': 'club',
                'href': home_club_href,
            },
            'home_club_name': home_club_name,
            'home_club_position': home_club_position,
            'away_club': {
                'type': 'club',
                'href': away_club_href,
            },
            'away_club_name': away_club_name,
            'away_club_position': away_club_position,
            'result': result,
            'half_time_score': half_time_score,
            # How the match was decided, when the site says so: "AET", "on pens",
            # "uncontested". None for an ordinary game. See extract_result_annotation.
            'result_type': result_type,
            'matchday': matchday,
            'date': date,
            'kickoff_time': kickoff_time,
            'stadium': stadium,
            'attendance': attendance,
            'referee': referee,
            'referee_href': referee_href,
            'events': game_events,
        }

        if len(manager_names) == 2:
            home_manager_name, away_manager_name = manager_names
            home_manager_href = manager_hrefs[0] if len(manager_hrefs) > 0 else None
            away_manager_href = manager_hrefs[1] if len(manager_hrefs) > 1 else None
            item["home_manager"] = {'name': home_manager_name, 'href': home_manager_href}
            item["away_manager"] = {'name': away_manager_name, 'href': away_manager_href}

        print(json.dumps(item), flush=True)

    await crawler.run(requests)
    check_failures(failures)
