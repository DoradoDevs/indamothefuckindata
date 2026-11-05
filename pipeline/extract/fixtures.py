"""Synthetic HubSpot-shaped data for building and testing the pipeline before the
real connector is approved.

All names are fictional placeholders. Records mimic the HubSpot CRM v3 object shape
({"id", "properties", "associations"}) so the same normalizer handles real and fake data.

Planted patterns (the analysis should find these):
  * cmp-001 (webinars): modest volume, high win rate, high revenue per contact.
  * cmp-002 (paid search): highest contact volume, poor deal conversion.
  * cmp-005 (social): produces contacts but essentially no won revenue.
  * Website sessions lead new contacts by about one month.
  * ~8% of deals are missing an amount (data-quality issue).
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

CAMPAIGNS = [
    # id, name, channel, source, weight, p_deal, p_won, mean_amount
    ("cmp-001", "Rural Fiber Webinar Series", "webinar", "OTHER_CAMPAIGNS", 0.10, 0.45, 0.55, 42000),
    ("cmp-002", "Paid Search - Wavelength", "paid_search", "PAID_SEARCH", 0.30, 0.10, 0.25, 18000),
    ("cmp-003", "Carrier Newsletter", "email", "EMAIL_MARKETING", 0.15, 0.25, 0.35, 24000),
    ("cmp-004", "Regional Trade Show Booth", "event", "OFFLINE", 0.05, 0.50, 0.40, 65000),
    ("cmp-005", "Social Awareness Push", "social", "SOCIAL_MEDIA", 0.15, 0.04, 0.03, 12000),
]
UNATTRIBUTED = (None, None, None, None, 0.25, 0.15, 0.30, 20000)
OTHER_SOURCES = ["ORGANIC_SEARCH", "DIRECT_TRAFFIC", "REFERRALS"]
TRAFFIC_SOURCES = ["ORGANIC_SEARCH", "PAID_SEARCH", "SOCIAL_MEDIA", "EMAIL_MARKETING", "DIRECT_TRAFFIC", "REFERRALS"]
INDUSTRIES = ["Carrier", "Enterprise", "Aggregator", "Government", "Education"]
STATES = ["IA", "KS", "MN", "NE", "SD", "ND", "MO", "WI"]


def _iso(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _months(start: date, n: int) -> list[date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    if lam > 50:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def generate(seed: int = 7, months: int = 24, end: date = date(2026, 10, 1)) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    start_y, start_m = end.year, end.month - months + 1
    while start_m <= 0:
        start_y, start_m = start_y - 1, start_m + 12
    month_starts = _months(date(start_y, start_m, 1), months)

    # Website sessions: trend + seasonality + two campaign-driven spikes.
    sessions_by_month = []
    for i, _ in enumerate(month_starts):
        base = 4000 + 60 * i + 600 * math.sin(i / 12 * 2 * math.pi)
        if i in (6, 15):
            base *= 1.6
        sessions_by_month.append(max(500, rng.gauss(base, 250)))

    web_traffic = []
    for ms, total in zip(month_starts, sessions_by_month):
        shares = [rng.uniform(0.5, 1.5) for _ in TRAFFIC_SOURCES]
        s = sum(shares)
        for src, sh in zip(TRAFFIC_SOURCES, shares):
            web_traffic.append({"month": ms.isoformat(), "source": src, "sessions": round(total * sh / s)})

    companies = []
    for i in range(1, 181):
        companies.append({
            "id": str(100000 + i),
            "properties": {
                "name": f"Example Company {i:03d}",
                "industry": rng.choice(INDUSTRIES),
                "state": rng.choice(STATES),
                "createdate": _iso(month_starts[0] - timedelta(days=rng.randint(0, 700))),
            },
        })

    campaigns = []
    for cid, name, channel, _src, *_ in CAMPAIGNS:
        start_i = rng.randint(0, 4)
        campaigns.append({
            "id": cid,
            "properties": {
                "hs_name": name,
                "channel": channel,
                "hs_start_date": month_starts[start_i].isoformat(),
                "hs_end_date": None,
            },
        })

    contacts, deals = [], []
    profiles = list(CAMPAIGNS) + [UNATTRIBUTED]
    weights = [p[4] for p in profiles]
    cid_n, did_n = 0, 0
    for i, ms in enumerate(month_starts):
        # New contacts track the PREVIOUS month's sessions (planted 1-month lag).
        lag_sessions = sessions_by_month[i - 1] if i > 0 else sessions_by_month[0]
        n_new = _poisson(rng, lag_sessions * 0.025)
        for _ in range(n_new):
            cid_n += 1
            prof = rng.choices(profiles, weights)[0]
            camp_id, _name, _chan, src, _w, p_deal, p_won, mean_amt = prof
            source = src or rng.choice(OTHER_SOURCES)
            created = datetime(ms.year, ms.month, 1, tzinfo=timezone.utc) + timedelta(
                days=rng.randint(0, 27), hours=rng.randint(8, 18))
            company = rng.choice(companies)["id"]
            stage = "lead"
            if rng.random() < 0.5:
                stage = "marketingqualifiedlead"
            contact_id = str(500000 + cid_n)

            if rng.random() < p_deal:
                did_n += 1
                stage = "opportunity"
                deal_created = created + timedelta(days=rng.randint(3, 30))
                won = rng.random() < p_won
                close = deal_created + timedelta(days=max(7, round(rng.gammavariate(3, 20))))
                closed = close.date() <= end + timedelta(days=30)
                if closed and won:
                    dealstage, stage = "closedwon", "customer"
                elif closed:
                    dealstage = "closedlost"
                else:
                    dealstage = rng.choice(["qualifiedtobuy", "presentationscheduled", "contractsent"])
                amount = None if rng.random() < 0.08 else round(rng.lognormvariate(math.log(mean_amt), 0.4), -2)
                deals.append({
                    "id": str(900000 + did_n),
                    "properties": {
                        "dealname": f"Example Deal {did_n:04d}",
                        "amount": None if amount is None else str(amount),
                        "dealstage": dealstage,
                        "pipeline": "default",
                        "createdate": _iso(deal_created),
                        "closedate": _iso(close) if closed else None,
                    },
                    "associations": {"contacts": [contact_id], "companies": [company]},
                })

            contacts.append({
                "id": contact_id,
                "properties": {
                    # PII-like fields included on purpose so the PII stripper is exercised.
                    "firstname": "Test", "lastname": f"Contact{cid_n}",
                    "email": f"contact{cid_n}@example.com", "phone": "555-0100",
                    "createdate": _iso(created),
                    "lifecyclestage": stage,
                    "hs_analytics_source": source,
                    "hs_analytics_first_touch_converting_campaign": camp_id,
                },
                "associations": {"companies": [company]},
            })

    email_stats = []
    for cid, _name, channel, *_ in CAMPAIGNS:
        if channel not in ("email", "webinar"):
            continue
        open_rate = 0.32 if cid == "cmp-001" else 0.21
        ctr = 0.06 if cid == "cmp-001" else 0.025
        for j, ms in enumerate(month_starts):
            if j % 2:
                continue
            sent = rng.randint(1500, 2500)
            delivered = round(sent * rng.uniform(0.95, 0.99))
            email_stats.append({
                "email_id": f"{cid}-em-{j:02d}", "campaign_id": cid, "send_date": ms.isoformat(),
                "sent": sent, "delivered": delivered,
                "opened": round(delivered * rng.gauss(open_rate, 0.02)),
                "clicked": round(delivered * rng.gauss(ctr, 0.005)),
            })

    return {
        "contacts": contacts, "companies": companies, "deals": deals,
        "campaigns": campaigns, "email_stats": email_stats, "web_traffic": web_traffic,
    }
