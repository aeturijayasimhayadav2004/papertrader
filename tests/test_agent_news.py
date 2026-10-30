from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from papertrader.agent.http import FetchError
from papertrader.agent.market import IST
from papertrader.agent.news import (
    NewsItem, dedupe, fetch_news, google_news_url, news_check, news_finding, parse_google_rss,
    parse_nse_announcements, tag_headline,
)

FIX = Path(__file__).parent / "fixtures" / "agent" / "news"
NOW = dt.datetime(2026, 10, 29, 18, 0, tzinfo=IST)


def router(routes: dict[str, str]):
    """Fake fetch: the first URL substring that matches serves that fixture, else a 403."""
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        for part, name in routes.items():
            if part in url:
                return (FIX / name).read_bytes()
        raise FetchError(f"{url}: HTTP Error 403: Forbidden")
    fetch.calls = calls
    return fetch


def test_tags_for_indian_headlines():
    cases = {
        "SEBI imposes Rs 25 lakh penalty on XYZ": "regulatory",
        "Income tax raids at offices of ABC Ltd": "regulatory",
        "Statutory auditor of XYZ resigns citing governance concerns": "auditor_cfo_exit",
        "L&T bags orders worth Rs 2,500 crore": "order_win",
        "Jefferies cuts target price on HDFC Bank": "target_cut",
        "Motilal upgrades ITC to buy": "rating_upgrade",
        "Infosys draws four downgrades in a day": "rating_downgrade",
        "Company defaults on NCD interest payment": "fraud_default",
        "Board approves bonus issue in 1:1 ratio and stock split": "bonus_split",
        "Promoters pledge 5% stake with lenders": "pledge",
        "TCS board approves Rs 17,000 crore buyback": "buyback",
        "Infosys Q2 results: net profit rises 13%": "results",
        "Bharti Airtel raises Rs 15,000 crore via QIP": "fundraise",
        "Reliance to acquire 51% stake in XYZ": "deal",
        "Infosys lowers FY27 revenue guidance": "guidance_cut",
        "HUL declares interim dividend of Rs 19": "dividend",
        "ICICI Bank CEO steps down": "management_exit",
    }
    for title, tag in cases.items():
        assert tag in tag_headline(title), (title, tag_headline(title))
    assert tag_headline("RBI keeps repo rate unchanged") == []      # a regulator alone is not an action
    assert "auditor_cfo_exit" not in tag_headline("ICICI Bank CEO steps down")


def test_parse_real_google_rss_strips_publisher_and_converts_to_ist():
    items = parse_google_rss((FIX / "gnews_infy.xml").read_bytes())
    assert len(items) == 12
    first = items[0]
    assert first.source == "Business Standard"
    assert not first.title.endswith("Business Standard")
    assert first.published == dt.datetime(2026, 10, 28, 12, 23, tzinfo=IST)   # 06:53 GMT
    assert first.published.utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_google_url_is_india_edition():
    url = google_news_url("Infosys", 48)
    assert "hl=en-IN" in url and "gl=IN" in url and "ceid=IN%3Aen" in url and "when%3A2d" in url


def test_window_merge_and_newest_first():
    fetch = router({"news.google.com": "gnews_infy.xml", "corporate-announcements": "nse_announcements_infy.json"})
    items = fetch_news("INFY.NS", hours=48, fetch=fetch, now=NOW)
    times = [i.published for i in items]
    assert times == sorted(times, reverse=True)
    assert all(NOW - dt.timedelta(hours=48) <= t <= NOW + dt.timedelta(minutes=10) for t in times)
    assert sum(i.source == "NSE filing" for i in items) == 2      # 28 and 29 Oct; the 16 Oct one is too old
    assert "Infosys" in fetch.calls[0]


def test_dedupe_same_story_from_two_papers():
    a = NewsItem("SEBI fines X", "ET", NOW, "u1", [])
    b = NewsItem("SEBI fines X", "BS", NOW - dt.timedelta(minutes=5), "u2", [])
    c = NewsItem("Other", "ET", NOW, "u1", [])
    assert [i.source for i in dedupe([b, a, c])] == ["ET"]


def test_warn_on_downgrade_in_48h_but_not_in_24h():
    fetch = router({"news.google.com": "gnews_infy.xml"})          # NSE refuses: Google alone is enough
    f48 = news_check("INFY.NS", hours=48, fetch=fetch, now=NOW)
    assert f48.status == "warn"
    assert "downgrades" in f48.summary
    f24 = news_check("INFY.NS", hours=24, fetch=fetch, now=NOW)
    assert f24.status == "info"


def test_block_on_severe_news_and_summary_names_it():
    fetch = router({"news.google.com": "gnews_severe.xml"})
    f = news_check("ACME.NS", fetch=fetch, now=NOW, company="Acme Infra")
    assert f.status == "block" and f.blocks
    assert "auditor resigns" in f.summary
    assert f.data["count"] == 2      # duplicate SEBI story collapsed; the 26 Oct order win is outside 48h
    # the auditor exit blocks; the SEBI penalty is a regulatory warning only (fines are routine)
    assert {t for i in f.data["flagged"] for t in i["tags"]} >= {"auditor_cfo_exit"}


def test_regulatory_fines_warn_but_raids_and_bans_block():
    from papertrader.agent.news import NewsItem, severity

    def sev(title):
        return severity(NewsItem(title, "x", NOW, "", tag_headline(title)))
    assert sev("SEBI imposes Rs 2 crore penalty on Acme promoters") == "warn"
    assert sev("Income tax department raids Acme offices") == "block"
    assert sev("SEBI bars Acme promoters from securities market") == "block"


def test_nse_announcements_parse():
    items = parse_nse_announcements(json.loads((FIX / "nse_announcements_infy.json").read_text()))
    assert items[0].published == dt.datetime(2026, 10, 16, 16, 35, 12, tzinfo=IST)
    assert "results" in items[0].tags and "dividend" in items[0].tags


def test_no_headlines_is_ok_and_failure_is_unknown():
    assert news_finding("INFY", []).status == "ok"
    f = news_check("INFY.NS", fetch=router({}), now=NOW)
    assert f.status == "unknown" and f.desk == "news"


def test_bad_xml_is_unknown_not_raised():
    f = news_check("INFY.NS", fetch=lambda url: b"<html>not rss", now=NOW)
    assert f.status == "unknown"


def test_sister_company_headlines_are_not_about_this_company():
    from papertrader.agent.news import about_sibling
    assert about_sibling("SBI Life gets IRDAI penalty notice", "SBI")
    assert not about_sibling("SBI raises Rs 10,000 crore; SBI Life also up", "SBI")
    assert not about_sibling("SBI hikes lending rates", "SBI")
