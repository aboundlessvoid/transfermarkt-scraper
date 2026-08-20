import json
import re
from urllib.parse import unquote, urlparse

from crawlee import Request

from tfmkt.common import DEFAULT_BASE_URL, load_parents, build_initial_requests, safe_strip, create_crawler, check_failures


async def run(parents_arg=None, season=2024, base_url=None):
    base_url = base_url or DEFAULT_BASE_URL
    parents = load_parents(parents_arg)
    requests = build_initial_requests(parents, season, base_url, label='parse', spider_name='players')

    crawler, failures = create_crawler()

    @crawler.router.handler('parse')
    async def parse(context) -> None:
        parent = context.request.user_data['parent']
        sel = context.selector

        players_table = sel.xpath("//div[@class='responsive-table']")
        if not players_table:
            players_table = sel.xpath("//table[contains(@class, 'items')]")
        assert len(players_table) >= 1
        players_table = players_table[0]

        player_hrefs = players_table.xpath(
            '//table[@class="inline-table"]//td[@class="hauptlink"]/a/@href'
        ).getall()

        new_requests = []
        for href in player_hrefs:
            cb_data = {
                'type': 'player',
                'href': href,
                'parent': parent,
            }
            new_requests.append(
                Request.from_url(
                    url=base_url + href,
                    label='parse_details',
                    user_data={'base': cb_data},
                )
            )

        if new_requests:
            await context.add_requests(new_requests)

    @crawler.router.handler('parse_details')
    async def parse_details(context) -> None:
        base = context.request.user_data['base']
        sel = context.selector

        attributes = {}

        name_element = sel.xpath("//h1[@class='data-header__headline-wrapper']")
        attributes["name"] = safe_strip("".join(name_element.xpath("text()").getall()).strip())
        attributes["last_name"] = safe_strip(name_element.xpath("strong/text()").get())
        attributes["number"] = safe_strip(name_element.xpath("span/text()").get())

        attributes['name_in_home_country'] = sel.xpath(
            "//span[text()='Name in home country:']/following::span[1]/text()"
        ).get()
        # birthDate text is like "Jan 1, 1990 (35)", but some player pages omit it
        # entirely (incomplete TM data, common on lower-tier players), so guard
        # against a missing/empty value instead of calling .strip() on None.
        birth_raw = sel.xpath("//span[@itemprop='birthDate']/text()").get()
        birth_raw = birth_raw.strip() if birth_raw else None
        attributes['date_of_birth'] = birth_raw.split(" (")[0].strip() if birth_raw else None
        attributes['place_of_birth'] = {
            'country': sel.xpath(
                "//span[text()='Place of birth:']/following::span[1]/span/img/@title"
            ).get(),
            'city': sel.xpath(
                "//span[text()='Place of birth:']/following::span[1]/span/text()"
            ).get(),
        }
        attributes['age'] = (
            birth_raw.split('(')[-1].split(')')[0].strip()
            if birth_raw and '(' in birth_raw else None
        )
        attributes['height'] = sel.xpath(
            "//span[text()='Height:']/following::span[1]/text()"
        ).get()
        # Full name is the "Name in home country" which is the official full name
        attributes['full_name'] = sel.xpath(
            "//span[text()='Name in home country:']/following::span[1]/text()"
        ).get()

        all_citizenships = sel.xpath(
            "//span[text()='Citizenship:']/following::span[1]/img/@title"
        ).getall()
        attributes['citizenship'] = all_citizenships[0] if all_citizenships else None
        if len(all_citizenships) > 1:
            attributes['additional_citizenships'] = all_citizenships[1:]
        attributes['position'] = safe_strip(sel.xpath(
            "//span[text()='Position:']/following::span[1]/text()"
        ).get())
        attributes['player_agent'] = {
            'href': sel.xpath(
                "//span[text()='Player agent:']/following::span[1]/a/@href"
            ).get(),
            'name': sel.xpath(
                "//span[text()='Player agent:']/following::span[1]/a/text()"
            ).get(),
        }
        attributes['image_url'] = sel.xpath(
            "//img[@class='data-header__profile-image']/@src"
        ).get()
        attributes['current_club'] = {
            'href': sel.xpath(
                "//span[contains(text(),'Current club:')]/following::span[1]/a/@href"
            ).get(),
        }
        attributes['foot'] = sel.xpath(
            "//span[text()='Foot:']/following::span[1]/text()"
        ).get()
        attributes['joined'] = sel.xpath(
            "//span[text()='Joined:']/following::span[1]/text()"
        ).get()
        attributes['contract_expires'] = safe_strip(sel.xpath(
            "//span[text()='Contract expires:']/following::span[1]/text()"
        ).get())
        attributes['day_of_last_contract_extension'] = sel.xpath(
            "//span[text()='Date of last contract extension:']/following::span[1]/text()"
        ).get()
        attributes['outfitter'] = sel.xpath(
            "//span[text()='Outfitter:']/following::span[1]/text()"
        ).get()

        # National team info (in the data-header section)
        national_player_li = sel.xpath("//li[contains(text(), 'National player:')]")
        if national_player_li:
            national_team_country = safe_strip(
                national_player_li.xpath(".//span/img/@title").get()
            )
            national_team_href = national_player_li.xpath(".//span/a/@href").get()
            if national_team_href:
                attributes['national_team'] = {
                    'country': national_team_country,
                    'href': national_team_href,
                }

        # International caps and goals (in the data-header section)
        caps_goals_li = sel.xpath("//li[contains(text(), 'Caps/Goals:')]")
        if caps_goals_li:
            caps_goals_values = caps_goals_li.xpath("a/text()").getall()
            if len(caps_goals_values) >= 2:
                attributes['international_caps'] = safe_strip(caps_goals_values[0])
                attributes['international_goals'] = safe_strip(caps_goals_values[1])

        current_market_value, market_value_last_update = extract_current_market_value(sel)
        attributes['current_market_value'] = current_market_value
        attributes['market_value_last_update'] = market_value_last_update
        # `highest_market_value` and `market_value_history` are NOT scrapeable from this
        # page any more -- see extract_current_market_value. They are emitted as None to
        # keep the record schema stable for consumers that already read the keys, and the
        # values come from the separate market-value harvest instead. An explicit None is
        # deliberate: the selectors that used to be here matched markup Transfermarkt has
        # deleted, and a dead selector reads as "we tried" where this reads as a decision.
        attributes['highest_market_value'] = None

        social_media_value_node = sel.xpath(
            "//span[text()='Social-Media:']/following::span[1]"
        )
        if len(social_media_value_node) > 0:
            attributes['social_media'] = []
            for element in social_media_value_node.xpath('div[@class="socialmedia-icons"]/a'):
                href = element.xpath('@href').get()
                attributes['social_media'].append(href)

        attributes['market_value_history'] = None
        attributes['code'] = unquote(urlparse(base["href"]).path.split("/")[1])

        item = {**base, **attributes}
        print(json.dumps(item), flush=True)

    await crawler.run(requests)
    check_failures(failures)


def extract_current_market_value(selector):
    """``(current_market_value, last_update)`` from the profile page's data header.

    **Transfermarkt no longer renders market values into the player page.** The block
    this crawler used to read -- ``div.tm-player-market-value-development__current-value``,
    its ``__max-value`` sibling, and the inline Highcharts ``series`` script behind the
    graph -- has been replaced by a ``<tm-market-value-development-graph-integrated>``
    custom element that fetches its own data client-side. Measured on live pages:
    ``current-value``, ``max-value``, ``series`` and ``Highcharts`` each occur **zero**
    times. No selector against this page or the ``/marktwertverlauf/`` one can recover
    the history or the highest value; both come from the market-value harvest instead.

    What *is* still server-rendered is the current value in the page header, and that is
    what this reads. Note the markup::

        <a class="data-header__market-value-wrapper">
          <span class="waehrung">EUR</span>220.00<span class="waehrung">m</span>
          <p class="data-header__last-update">Last update: 22/07/2026</p>
        </a>

    -- the value is split across THREE text nodes because the currency symbol and the
    magnitude suffix are styled separately, so ``::text`` and ``.get()`` would return
    only the middle chunk. This is the same trap that made ``games.py`` scrape ``"("``
    as every half-time score, which is why the join here is explicit and the ``<p>`` is
    excluded from it by name rather than by hoping it sorts last.

    Players with no valuation at all (youth, lower tiers) have **no wrapper element** --
    verified against a live page -- so an absent value is normal and returns
    ``(None, None)``.
    """
    wrapper = selector.css('a.data-header__market-value-wrapper')
    if not wrapper:
        return None, None
    wrapper = wrapper[0]

    # Direct text nodes plus the currency <span>s, deliberately excluding the nested
    # <p class="data-header__last-update">, which is returned separately.
    value = ''.join(wrapper.xpath('./text() | ./span/text()').getall())
    value = re.sub(r'\s+', '', value) or None

    last_update = safe_strip(wrapper.css('p.data-header__last-update::text').get())
    if last_update:
        # Printed as "Last update: 22/07/2026"; keep the date, drop the label.
        last_update = last_update.split(':', 1)[-1].strip() or None

    return value, last_update
