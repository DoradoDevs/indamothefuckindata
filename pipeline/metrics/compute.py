"""Deterministic metrics. Every number Claude is allowed to use comes from here.

Each metric has a stable ID, a value, a unit, the sample size it rests on (n), and a
plain-English description. Claude must cite IDs; verify/checks.py holds it to them.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime

from pipeline.metrics import stats

ALPHA = 0.05  # threshold on Benjamini-Hochberg adjusted p-values


@dataclass
class Metric:
    id: str
    value: float | None
    unit: str  # count | usd | ratio | days | correlation
    n: int
    description: str
    # 95% interval for the value (ratios, means, correlations), when meaningful.
    ci_low: float | None = None
    ci_high: float | None = None
    # For test.* metrics and correlations: multiple-comparison-adjusted p-value (q),
    # and whether it clears ALPHA ("unlikely to be chance").
    p_value: float | None = None
    significant: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _round(v, unit):
    if isinstance(v, float):
        return round(v, 4 if unit in ("ratio", "correlation") else 2)
    return v


class _Collector:
    def __init__(self):
        self.items: list[Metric] = []

    def add(self, id_: str, value, unit: str, n: int, desc: str, ci=None, p_value=None, significant=None):
        lo, hi = ci if ci else (None, None)
        self.items.append(Metric(id_, _round(value, unit), unit, int(n), desc,
                                 _round(lo, unit), _round(hi, unit),
                                 None if p_value is None else round(p_value, 4), significant))


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _lagged(series_x: dict[str, float], series_y: dict[str, float], lag: int) -> tuple[list, list]:
    months = sorted(set(series_x) | set(series_y))
    xs, ys = [], []
    for i, m in enumerate(months):
        if i + lag < len(months) and m in series_x and months[i + lag] in series_y:
            xs.append(series_x[m])
            ys.append(series_y[months[i + lag]])
    return xs, ys


def _group_stats(con: sqlite3.Connection, key_col: str) -> dict[str, dict]:
    """Contact/deal funnel per value of a contacts column (campaign_id, source)."""
    q = f"""
    SELECT COALESCE(c.{key_col}, 'none') AS k,
           COUNT(DISTINCT c.contact_id) AS contacts,
           COUNT(DISTINCT CASE WHEN d.deal_id IS NOT NULL THEN c.contact_id END) AS contacts_with_deal,
           COUNT(DISTINCT CASE WHEN d.is_won = 1 THEN c.contact_id END) AS contacts_with_won,
           COUNT(DISTINCT d.deal_id) AS deals,
           COUNT(DISTINCT CASE WHEN d.is_closed = 1 THEN d.deal_id END) AS closed,
           COUNT(DISTINCT CASE WHEN d.is_won = 1 THEN d.deal_id END) AS won,
           COALESCE(SUM(CASE WHEN d.is_won = 1 THEN d.amount END), 0) AS revenue_won,
           COUNT(DISTINCT CASE WHEN d.is_won = 1 AND d.amount IS NOT NULL THEN d.deal_id END) AS won_with_amount
    FROM contacts c LEFT JOIN deals d ON d.contact_id = c.contact_id
    GROUP BY k
    """
    cols = ["k", "contacts", "contacts_with_deal", "contacts_with_won", "deals", "closed", "won",
            "revenue_won", "won_with_amount"]
    return {r[0]: dict(zip(cols, r)) for r in con.execute(q)}


def _revenue_per_contact(con: sqlite3.Connection, key_col: str) -> dict[str, list[float]]:
    """Each contact's won revenue (0 for most), grouped - for intervals and tests on the mean."""
    out: dict[str, list[float]] = {}
    q = f"""SELECT COALESCE(c.{key_col}, 'none'),
                   COALESCE(SUM(CASE WHEN d.is_won = 1 THEN d.amount END), 0)
            FROM contacts c LEFT JOIN deals d ON d.contact_id = c.contact_id
            GROUP BY c.contact_id"""
    for k, v in con.execute(q):
        out.setdefault(k, []).append(float(v))
    return out


def _proportion_tests(m: _Collector, dim: str, measure: str, label: str, groups: dict[str, tuple[int, int]],
                      names: dict[str, str]) -> None:
    """Each group vs. all other groups in the same dimension; BH-adjusted within the family."""
    tot_k = sum(k for k, _ in groups.values())
    tot_n = sum(n for _, n in groups.values())
    keys, diffs, pvals, ns = [], [], [], []
    for key, (k, n) in sorted(groups.items()):
        rk, rn = tot_k - k, tot_n - n
        if not n or not rn:
            continue
        keys.append(key)
        diffs.append(k / n - rk / rn)
        pvals.append(stats.fisher_exact(k, n, rk, rn))
        ns.append(n)
    for key, diff, q, n in zip(keys, diffs, stats.bh_adjust(pvals), ns):
        m.add(f"test.{dim}.{key}.{measure}", diff, "ratio", n,
              f"{label} for {names.get(key, key)} minus all other {dim} groups combined "
              f"(positive = higher). Significant = unlikely to be chance after adjusting for "
              f"{len(keys)} comparisons.",
              p_value=q, significant=(q is not None and q < ALPHA))


def _mean_tests(m: _Collector, dim: str, measure: str, label: str, groups: dict[str, list[float]],
                names: dict[str, str]) -> None:
    keys, diffs, pvals, ns = [], [], [], []
    for key, vals in sorted(groups.items()):
        rest = [v for k2, vs in groups.items() if k2 != key for v in vs]
        if len(vals) < 2 or len(rest) < 2:
            continue
        keys.append(key)
        diffs.append(statistics.fmean(vals) - statistics.fmean(rest))
        pvals.append(stats.welch_z(vals, rest))
        ns.append(len(vals))
    for key, diff, q, n in zip(keys, diffs, stats.bh_adjust(pvals), ns):
        m.add(f"test.{dim}.{key}.{measure}", diff, "usd", n,
              f"{label} for {names.get(key, key)} minus all other {dim} groups combined "
              f"(positive = higher). Significant = unlikely to be chance after adjusting for "
              f"{len(keys)} comparisons.",
              p_value=q, significant=(q is not None and q < ALPHA))


def _days_to_close(con: sqlite3.Connection, key_col: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    q = f"""SELECT COALESCE(c.{key_col}, 'none'), c.created_at, d.closed_at
            FROM deals d JOIN contacts c ON c.contact_id = d.contact_id
            WHERE d.is_won = 1 AND d.closed_at IS NOT NULL AND c.created_at IS NOT NULL"""
    for k, a, b in con.execute(q):
        days = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400
        out.setdefault(k, []).append(days)
    return out


def compute(con: sqlite3.Connection) -> list[Metric]:
    m = _Collector()

    # --- Overview -----------------------------------------------------------
    n_contacts = con.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    n_deals, n_closed, n_won, rev = con.execute(
        "SELECT COUNT(*), SUM(is_closed), SUM(is_won), COALESCE(SUM(CASE WHEN is_won=1 THEN amount END),0) FROM deals"
    ).fetchone()
    n_closed, n_won = n_closed or 0, n_won or 0
    m.add("overview.contacts", n_contacts, "count", n_contacts, "Total contacts in scope")
    m.add("overview.deals", n_deals, "count", n_deals, "Total deals in scope")
    m.add("overview.deals_won", n_won, "count", n_deals, "Closed-won deals")
    m.add("overview.revenue_won", float(rev), "usd", n_won, "Sum of amount on closed-won deals")
    m.add("overview.contact_to_deal_rate", _ratio(n_deals, n_contacts), "ratio", n_contacts,
          "Deals per contact")
    m.add("overview.win_rate", _ratio(n_won, n_closed), "ratio", n_closed, "Won / closed deals")

    for stage, cnt in con.execute("SELECT COALESCE(lifecycle_stage,'unknown'), COUNT(*) FROM contacts GROUP BY 1"):
        m.add(f"funnel.stage.{stage}.contacts", cnt, "count", n_contacts, f"Contacts in lifecycle stage '{stage}'")

    # --- Campaigns ----------------------------------------------------------
    names = {cid: (name, ch) for cid, name, ch in con.execute("SELECT campaign_id, name, channel FROM campaigns")}
    camp = _group_stats(con, "campaign_id")
    email = {r[0]: r[1:] for r in con.execute(
        "SELECT campaign_id, SUM(delivered), SUM(opened), SUM(clicked) FROM email_stats GROUP BY 1")}
    camp_rpc = _revenue_per_contact(con, "campaign_id")
    camp_names = {cid: (names.get(cid, (cid,))[0] if cid != "none" else "No campaign attributed") for cid in camp}
    for cid, s in sorted(camp.items()):
        label = camp_names[cid]
        p = f"campaign.{cid}"
        m.add(f"{p}.contacts", s["contacts"], "count", s["contacts"], f"Contacts first-touch attributed to {label}")
        m.add(f"{p}.deals", s["deals"], "count", s["contacts"], f"Deals from {label} contacts")
        m.add(f"{p}.deals_won", s["won"], "count", s["deals"], f"Won deals from {label}")
        m.add(f"{p}.contact_to_deal_rate", _ratio(s["contacts_with_deal"], s["contacts"]), "ratio", s["contacts"],
              f"Share of {label} contacts with at least one deal",
              ci=stats.wilson(s["contacts_with_deal"], s["contacts"]))
        m.add(f"{p}.win_rate", _ratio(s["won"], s["closed"]), "ratio", s["closed"], f"Won / closed deals, {label}",
              ci=stats.wilson(s["won"], s["closed"]))
        m.add(f"{p}.revenue_won", float(s["revenue_won"]), "usd", s["closed"], f"Won revenue, {label}")
        m.add(f"{p}.revenue_per_contact", _ratio(s["revenue_won"], s["contacts"]), "usd", s["contacts"],
              f"Won revenue per contact, {label}", ci=stats.mean_ci(camp_rpc.get(cid, [])))
        if cid in email and email[cid][0]:
            delivered, opened, clicked = email[cid]
            m.add(f"{p}.email_open_rate", _ratio(opened, delivered), "ratio", delivered, f"Email open rate, {label}")
            m.add(f"{p}.email_click_rate", _ratio(clicked, delivered), "ratio", delivered, f"Email click rate, {label}")

    _proportion_tests(m, "campaign", "contact_to_deal_rate", "Share of contacts with a deal",
                      {k: (s["contacts_with_deal"], s["contacts"]) for k, s in camp.items()}, camp_names)
    _proportion_tests(m, "campaign", "win_rate", "Win rate",
                      {k: (s["won"], s["closed"]) for k, s in camp.items()}, camp_names)
    _mean_tests(m, "campaign", "revenue_per_contact", "Won revenue per contact", camp_rpc, camp_names)

    # --- Lead sources ------------------------------------------------------
    src = _group_stats(con, "source")
    src_rpc = _revenue_per_contact(con, "source")
    dtc = _days_to_close(con, "source")
    for k, s in sorted(src.items()):
        p = f"source.{k}"
        m.add(f"{p}.contacts", s["contacts"], "count", s["contacts"], f"Contacts with original source {k}")
        m.add(f"{p}.deals_won", s["won"], "count", s["deals"], f"Won deals, source {k}")
        m.add(f"{p}.contact_to_won_rate", _ratio(s["contacts_with_won"], s["contacts"]), "ratio", s["contacts"],
              f"Share of contacts from source {k} with at least one won deal",
              ci=stats.wilson(s["contacts_with_won"], s["contacts"]))
        m.add(f"{p}.revenue_won", float(s["revenue_won"]), "usd", s["closed"], f"Won revenue, source {k}")
        m.add(f"{p}.revenue_per_contact", _ratio(s["revenue_won"], s["contacts"]), "usd", s["contacts"],
              f"Won revenue per contact, source {k}", ci=stats.mean_ci(src_rpc.get(k, [])))
        if dtc.get(k):
            m.add(f"{p}.median_days_to_won", statistics.median(dtc[k]), "days", len(dtc[k]),
                  f"Median days from contact created to deal won, source {k}")

    src_names = {k: f"source {k}" for k in src}
    _proportion_tests(m, "source", "contact_to_won_rate", "Share of contacts with a won deal",
                      {k: (s["contacts_with_won"], s["contacts"]) for k, s in src.items()}, src_names)
    _mean_tests(m, "source", "revenue_per_contact", "Won revenue per contact", src_rpc, src_names)

    # --- Deal pipelines (e.g. sales vs. sponsorships; never mix their totals) -
    for pid, label, deals_n, closed, won, revenue in con.execute("""
        SELECT d.pipeline, COALESCE(p.label, d.pipeline), COUNT(*), SUM(d.is_closed), SUM(d.is_won),
               COALESCE(SUM(CASE WHEN d.is_won=1 THEN d.amount END),0)
        FROM deals d LEFT JOIN pipelines p ON p.pipeline_id = d.pipeline GROUP BY 1"""):
        pp = f"pipeline.{pid}"
        m.add(f"{pp}.deals", deals_n, "count", deals_n, f"Deals in pipeline '{label}'")
        m.add(f"{pp}.deals_won", won or 0, "count", deals_n, f"Won deals in pipeline '{label}'")
        m.add(f"{pp}.win_rate", _ratio(won or 0, closed or 0), "ratio", closed or 0, f"Win rate in pipeline '{label}'",
              ci=stats.wilson(won or 0, closed or 0))
        m.add(f"{pp}.revenue_won", float(revenue), "usd", closed or 0, f"Won value in pipeline '{label}'")

    # --- Industry ----------------------------------------------------------
    ind_groups = {}
    for ind, closed, won, revenue in con.execute("""
        SELECT COALESCE(co.industry,'unknown'), SUM(d.is_closed), SUM(d.is_won),
               COALESCE(SUM(CASE WHEN d.is_won=1 THEN d.amount END),0)
        FROM deals d LEFT JOIN companies co ON co.company_id = d.company_id GROUP BY 1"""):
        closed, won = closed or 0, won or 0
        ind_groups[ind] = (won, closed)
        m.add(f"industry.{ind}.win_rate", _ratio(won, closed), "ratio", closed, f"Win rate, industry {ind}",
              ci=stats.wilson(won, closed))
        m.add(f"industry.{ind}.revenue_won", float(revenue), "usd", won, f"Won revenue, industry {ind}")
    _proportion_tests(m, "industry", "win_rate", "Win rate", ind_groups, {k: f"industry {k}" for k in ind_groups})

    # --- Monthly series + lagged correlations -------------------------------
    new_contacts = dict(con.execute("SELECT created_month, COUNT(*) FROM contacts WHERE created_month IS NOT NULL GROUP BY 1"))
    rev_rows = con.execute(
        "SELECT closed_month, SUM(amount), COUNT(*) FROM deals WHERE is_won=1 AND closed_month IS NOT NULL GROUP BY 1"
    ).fetchall()
    revenue = {r[0]: r[1] or 0 for r in rev_rows}
    won_count = {r[0]: r[2] for r in rev_rows}
    sessions = dict(con.execute("SELECT month, SUM(sessions) FROM web_traffic GROUP BY 1"))
    for month in sorted(set(new_contacts) | set(revenue) | set(sessions)):
        if month in new_contacts:
            m.add(f"monthly.{month}.new_contacts", new_contacts[month], "count", new_contacts[month],
                  f"New contacts created in {month}")
        if month in revenue:
            m.add(f"monthly.{month}.revenue_won", float(revenue[month]), "usd", won_count[month], f"Won revenue closed in {month}")
        if month in sessions:
            m.add(f"monthly.{month}.sessions", sessions[month], "count", sessions[month], f"Website sessions in {month}")

    pairs = [("sessions", sessions, "new_contacts", new_contacts, range(0, 3)),
             ("new_contacts", new_contacts, "revenue_won", revenue, range(0, 4))]
    corr_rows = []
    for xname, xs_map, yname, ys_map, lags in pairs:
        if not xs_map or not ys_map:
            continue
        for lag in lags:
            xs, ys = _lagged(xs_map, ys_map, lag)
            r = pearson(xs, ys)
            ci, p = stats.correlation_ci_p(r, len(xs))
            corr_rows.append((f"corr.{xname}_vs_{yname}.lag{lag}", r, len(xs), ci, p,
                              f"Pearson correlation of monthly {xname} with {yname} {lag} month(s) later. "
                              f"Monthly series trend and repeat, so treat as a lead, not proof."))
    for (id_, r, n, ci, _p, desc), q in zip(corr_rows, stats.bh_adjust([row[4] for row in corr_rows])):
        m.add(id_, r, "correlation", n, desc, ci=ci, p_value=q, significant=(q is not None and q < ALPHA))

    # --- Data quality -------------------------------------------------------
    missing_amt = con.execute("SELECT COUNT(*) FROM deals WHERE amount IS NULL").fetchone()[0]
    won_missing = con.execute("SELECT COUNT(*) FROM deals WHERE is_won=1 AND amount IS NULL").fetchone()[0]
    no_src = con.execute("SELECT COUNT(*) FROM contacts WHERE source IS NULL").fetchone()[0]
    no_contact = con.execute("SELECT COUNT(*) FROM deals WHERE contact_id IS NULL").fetchone()[0]
    no_campaign = con.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id IS NULL").fetchone()[0]
    no_company = con.execute("SELECT COUNT(*) FROM contacts WHERE company_id IS NULL").fetchone()[0]
    m.add("quality.contacts_missing_campaign_rate", _ratio(no_campaign, n_contacts), "ratio", n_contacts,
          "Share of contacts with no first-touch campaign (campaign results can't cover them)")
    m.add("quality.contacts_missing_company_rate", _ratio(no_company, n_contacts), "ratio", n_contacts,
          "Share of contacts not linked to a company (industry results can't cover them)")
    m.add("quality.deals_missing_amount_rate", _ratio(missing_amt, n_deals), "ratio", n_deals,
          "Share of deals with no amount")
    m.add("quality.won_deals_missing_amount", won_missing, "count", n_won,
          "Won deals with no amount (understates revenue)")
    m.add("quality.contacts_missing_source_rate", _ratio(no_src, n_contacts), "ratio", n_contacts,
          "Share of contacts with no original source")
    m.add("quality.deals_without_contact", no_contact, "count", n_deals,
          "Deals with no associated contact (excluded from campaign/source attribution)")

    return m.items
