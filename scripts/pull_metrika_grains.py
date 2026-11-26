#!/usr/bin/env python3
"""Pull Metrika HE traffic at week/day grain and rebuild data.json rows."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

CID = 51852059
HE = "ym:s:startURL=@'high-education'"
BASE_M = [
    "ym:s:visits",
    "ym:s:users",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    "ym:s:avgVisitDurationSeconds",
]
GOAL_M = [
    "ym:s:goal167818039reaches",
    "ym:s:goal673892053reaches",
    "ym:s:goal516130414reaches",
    "ym:s:goal673893032reaches",
    "ym:s:goal516130640reaches",
]
BNAMES = ["visits", "users", "bounce", "depth", "avg_sec"]
GNAMES = ["form", "phone", "plan", "bot", "subscribe"]

CHANNEL_LABELS = {
    "(все)": "Все источники (high-education)",
    "Яндекс.Директ": "Яндекс.Директ (utm yandex|ya / cpc|cpm)",
    "Партнёрки": "Партнёрки (CPA / partners)",
    "Органика": "Органика (Search engine)",
    "Рефералы": "Рефералы (Link traffic)",
    "Прямые заходы": "Прямые заходы (Direct traffic)",
    "CRM": "CRM (email / push)",
    "Контент": "Контент / media",
    "TG SMM": "TG SMM",
    "Медийка Urban": "Медийка Urban",
    "Прочее": "Прочее (mailing / internal / прочий utm)",
}

TYPE_MAP = {
    "Яндекс.Директ": "Платные",
    "Партнёрки": "Платные",
    "Медийка Urban": "Платные",
    "CRM": "Платные",
    "Органика": "Органика",
    "Рефералы": "Органика",
    "Контент": "Органика",
    "TG SMM": "Органика",
    "Прочее": "Органика",
    "Прямые заходы": "Прямые переходы",
    "(все)": "ALL",
}

MONTH_RU = {9: "сент. 2026", 10: "окт. 2026", 11: "нояб. 2026"}

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "data.json"
CACHE = Path.home() / "amo-ym-join" / "metrika_he_grains_cache.json"


def get(params: dict) -> dict:
    tok = os.environ["METRIKA_TOKEN"]
    url = "https://api-metrika.yandex.net/stat/v1/data?" + urllib.parse.urlencode(
        params, doseq=True
    )
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {tok}"})
    last_err = None
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            last_err = e
            if e.code in (429, 503) or "complicated" in body or "timeout" in body.lower():
                time.sleep(3 + attempt * 4)
                continue
            print("HTTP", e.code, body[:300])
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Metrika failed after retries: {last_err}")


def classify(utm_source, utm_medium, last_src=None):
    src = (utm_source or "").lower()
    med = (utm_medium or "").lower()
    key = f"{src}/{med}"
    paid_med = {"cpc", "cpa", "cpm", "ppc"}
    if (
        med in paid_med
        or src in {"yandex", "ya", "advcake", "admitad", "edpartners", "urban"}
        or med == "partners"
    ):
        camp_type = "Платные"
    elif last_src == "Direct traffic" and not src:
        camp_type = "Прямые переходы"
    else:
        camp_type = "Органика"

    if src in {"yandex", "ya"} or key.startswith("yandex/") or key.startswith("ya/"):
        macro = "Яндекс.Директ"
    elif (
        src in {"advcake", "admitad", "edpartners", "sravni", "test"}
        or med == "partners"
        or "partners" in med
    ):
        macro = "Партнёрки"
    elif src == "crm" or med in {"email", "push"}:
        macro = "CRM"
    elif src == "content" or "habr" in src or med in {
        "media",
        "brandmedia",
        "internal",
        "yandex_wiki",
    }:
        macro = "Контент"
    elif src == "tg" or med == "smm":
        macro = "TG SMM"
    elif src == "urban":
        macro = "Медийка Urban"
    elif last_src == "Search engine traffic":
        macro = "Органика"
    elif last_src == "Link traffic":
        macro = "Рефералы"
    elif last_src == "Direct traffic":
        macro = "Прямые заходы"
    elif last_src == "Ad traffic":
        macro = "Яндекс.Директ" if src in {"yandex", "ya", ""} else "Прочее"
    elif last_src == "Mailing traffic":
        macro = "CRM"
    else:
        macro = "Прочее"
    return macro, camp_type if camp_type else TYPE_MAP.get(macro, "Органика")


def weeks(d1: date, d2: date):
    cur = d1
    out = []
    while cur <= d2:
        end = min(cur + timedelta(days=6), d2)
        out.append((cur, end))
        cur = end + timedelta(days=1)
    return out


def month_bounds(d1: date, d2: date):
    cur = date(d1.year, d1.month, 1)
    out = []
    while cur <= d2:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        start = max(cur, d1)
        end = min(nxt - timedelta(days=1), d2)
        out.append((start, end))
        cur = nxt
    return out


def merge_metrics(dst, src):
    for k in ("visits", "users", "form", "phone", "plan", "bot", "subscribe"):
        dst[k] = dst.get(k, 0) + (src.get(k) or 0)
    # weighted later
    w = src.get("visits") or 0
    if w and src.get("bounce") is not None:
        dst.setdefault("_bounce_acc", 0)
        dst.setdefault("_w", 0)
        dst["_bounce_acc"] += src["bounce"] * w
        dst["_w"] += w
    if w and src.get("depth") is not None:
        dst.setdefault("_depth_acc", 0)
        dst.setdefault("_dw", 0)
        dst["_depth_acc"] += src["depth"] * w
        dst["_dw"] += w
    if w and src.get("avg_sec") is not None:
        dst.setdefault("_time_acc", 0)
        dst.setdefault("_tw", 0)
        dst["_time_acc"] += src["avg_sec"] * w
        dst["_tw"] += w


def finalize(bucket: dict) -> dict:
    out = {
        "visits": bucket.get("visits") or None,
        "users": bucket.get("users") or None,
        "form": bucket.get("form") or None,
        "phone": bucket.get("phone") or None,
        "plan": bucket.get("plan") or None,
        "bot": bucket.get("bot") or None,
        "sub": bucket.get("subscribe") or None,
        "spend": None,
        "impressions": None,
        "clicks": None,
        "ctr": None,
        "cpc": None,
        "cpu": None,
        "cpa": None,
        "cr_form": None,
        "bounce": None,
        "depth": None,
        "time": None,
    }
    if out["form"] is not None and out["visits"]:
        out["cr_form"] = out["form"] / out["visits"]
    w = bucket.get("_w") or 0
    if w:
        out["bounce"] = bucket["_bounce_acc"] / w
    dw = bucket.get("_dw") or 0
    if dw:
        out["depth"] = bucket["_depth_acc"] / dw
    tw = bucket.get("_tw") or 0
    if tw:
        out["time"] = bucket["_time_acc"] / tw
    return out


def pull_period(d1: str, d2: str) -> list[dict]:
    """Return list of metric dicts with channel_key for [d1,d2]."""
    rows = []
    b = get(
        {
            "ids": CID,
            "date1": d1,
            "date2": d2,
            "metrics": ",".join(BASE_M),
            "filters": HE,
            "accuracy": "full",
            "dimensions": "ym:s:lastUTMSource,ym:s:lastUTMMedium",
            "limit": 200,
            "sort": "-ym:s:visits",
        }
    )
    g = get(
        {
            "ids": CID,
            "date1": d1,
            "date2": d2,
            "metrics": ",".join(GOAL_M),
            "filters": HE,
            "accuracy": "full",
            "dimensions": "ym:s:lastUTMSource,ym:s:lastUTMMedium",
            "limit": 200,
            "sort": "-ym:s:goal167818039reaches",
        }
    )
    gmap = {
        tuple(x["dimensions"][i]["name"] for i in range(2)): x["metrics"] for x in g["data"]
    }
    for x in b["data"]:
        src = x["dimensions"][0]["name"]
        med = x["dimensions"][1]["name"]
        if src in (None, "", "(none)", "(not set)") and med in (None, "", "(none)", "(not set)"):
            continue
        mets = dict(zip(BNAMES, x["metrics"]))
        gm = gmap.get((src, med))
        if gm:
            mets.update(dict(zip(GNAMES, gm)))
        else:
            for k in GNAMES:
                mets[k] = 0
        macro, ctype = classify(src, med)
        rows.append({"channel_key": macro, "campaign_type": ctype, **mets})

    # empty UTM via lastSign
    bs = get(
        {
            "ids": CID,
            "date1": d1,
            "date2": d2,
            "metrics": ",".join(BASE_M),
            "filters": HE,
            "accuracy": "full",
            "dimensions": "ym:s:lastSignTrafficSource",
            "limit": 30,
            "sort": "-ym:s:visits",
        }
    )
    gs = get(
        {
            "ids": CID,
            "date1": d1,
            "date2": d2,
            "metrics": ",".join(GOAL_M),
            "filters": HE,
            "accuracy": "full",
            "dimensions": "ym:s:lastSignTrafficSource",
            "limit": 30,
            "sort": "-ym:s:goal167818039reaches",
        }
    )
    gsmap = {x["dimensions"][0]["name"]: x["metrics"] for x in gs["data"]}
    for x in bs["data"]:
        sname = x["dimensions"][0]["name"]
        mets = dict(zip(BNAMES, x["metrics"]))
        gm = gsmap.get(sname)
        if gm:
            mets.update(dict(zip(GNAMES, gm)))
        else:
            for k in GNAMES:
                mets[k] = 0
        # Approximate empty-UTM slice: use source totals * share without UTM is hard;
        # instead classify by source only for mailing/search/direct/link leftovers.
        macro, ctype = classify("", "", last_src=sname)
        if sname == "Search engine traffic":
            macro, ctype = "Органика", "Органика"
        elif sname == "Direct traffic":
            macro, ctype = "Прямые заходы", "Прямые переходы"
        elif sname == "Link traffic":
            macro, ctype = "Рефералы", "Органика"
        elif sname == "Mailing traffic":
            macro, ctype = "CRM", "Платные"
        elif sname == "Ad traffic":
            # already mostly in UTM yandex/partners — skip to avoid double count
            continue
        rows.append({"channel_key": macro, "campaign_type": ctype, **mets})

    # also totals for (все)
    bt = get(
        {
            "ids": CID,
            "date1": d1,
            "date2": d2,
            "metrics": ",".join(BASE_M),
            "filters": HE,
            "accuracy": "full",
        }
    )
    gt = get(
        {
            "ids": CID,
            "date1": d1,
            "date2": d2,
            "metrics": ",".join(GOAL_M),
            "filters": HE,
            "accuracy": "full",
        }
    )
    all_m = dict(zip(BNAMES, bt["totals"]))
    all_m.update(dict(zip(GNAMES, gt["totals"])))
    rows.append({"channel_key": "(все)", "campaign_type": "ALL", **all_m})
    return rows


def aggregate_by_channel(period_rows: list[dict]) -> dict[str, dict]:
    buckets: dict[str, dict] = defaultdict(dict)
    types: dict[str, str] = {}
    for r in period_rows:
        key = r["channel_key"]
        types[key] = r["campaign_type"]
        merge_metrics(buckets[key], r)
    out = {}
    for key, b in buckets.items():
        out[key] = {**finalize(b), "campaign_type": types[key]}
    return out


def label_for(grain: str, d1: date, d2: date) -> str:
    if grain == "month":
        return MONTH_RU.get(d1.month, d1.strftime("%Y-%m"))
    if grain == "week":
        return f"{d1.isoformat()} — {d2.isoformat()}"
    return d1.isoformat()


def build_rows(grains: dict) -> list[dict]:
    """grains: {grain: {(d1,d2): {channel: metrics}}}"""
    order = [
        "(все)",
        "Яндекс.Директ",
        "Партнёрки",
        "Органика",
        "Рефералы",
        "Прямые заходы",
        "CRM",
        "Контент",
        "TG SMM",
        "Медийка Urban",
        "Прочее",
    ]
    rows = []
    sort = 0

    month_map = grains.get("month", {})
    channel_totals: dict[str, dict] = defaultdict(dict)
    channel_types: dict[str, str] = {}
    for (_a, _b), chmap in month_map.items():
        for ch, mets in chmap.items():
            channel_types[ch] = mets.get("campaign_type", TYPE_MAP.get(ch, "Органика"))
            merge_metrics(
                channel_totals[ch],
                {
                    "visits": mets.get("visits") or 0,
                    "users": mets.get("users") or 0,
                    "form": mets.get("form") or 0,
                    "phone": mets.get("phone") or 0,
                    "plan": mets.get("plan") or 0,
                    "bot": mets.get("bot") or 0,
                    "subscribe": mets.get("sub") or 0,
                    "bounce": mets.get("bounce"),
                    "depth": mets.get("depth"),
                    "avg_sec": mets.get("time"),
                },
            )

    for ch in order:
        if ch not in channel_totals:
            continue
        sort += 10
        kind = "grand" if ch == "(все)" else "total"
        mets = finalize(channel_totals[ch])
        rows.append(
            {
                "label": CHANNEL_LABELS.get(ch, ch),
                "channel_key": ch,
                "date_from": "2026-09-01",
                "date_to": "2026-11-25",
                "campaign_type": channel_types.get(ch, TYPE_MAP.get(ch, "Органика")),
                "row_kind": kind,
                "grain": "all",
                "sort": sort,
                **mets,
            }
        )
        for grain in ("month", "week", "day"):
            periods = sorted(grains.get(grain, {}).items(), key=lambda x: x[0][0])
            for (d1, d2), chmap in periods:
                if ch not in chmap:
                    continue
                sort += 10
                m = chmap[ch]
                rows.append(
                    {
                        "label": label_for(grain, d1, d2),
                        "channel_key": ch,
                        "date_from": d1.isoformat(),
                        "date_to": d2.isoformat(),
                        "campaign_type": m.get("campaign_type", TYPE_MAP.get(ch, "Органика")),
                        "row_kind": grain,
                        "grain": grain,
                        "sort": sort,
                        **{
                            k: m.get(k)
                            for k in (
                                "visits",
                                "users",
                                "form",
                                "phone",
                                "plan",
                                "bot",
                                "sub",
                                "spend",
                                "impressions",
                                "clicks",
                                "ctr",
                                "cpc",
                                "cpu",
                                "cpa",
                                "cr_form",
                                "bounce",
                                "depth",
                                "time",
                            )
                        },
                    }
                )
    return rows


def main():
    period_start = date(2026, 9, 1)
    period_end = date(2026, 11, 25)
    cache = {"month": {}, "week": {}, "day": {}}
    if CACHE.exists():
        raw = json.loads(CACHE.read_text())
        # revive keys
        for grain, items in raw.items():
            for key, chmap in items.items():
                a, b = key.split("|")
                cache[grain][(date.fromisoformat(a), date.fromisoformat(b))] = chmap
        print("loaded cache periods", {g: len(v) for g, v in cache.items()})

    def store(grain, d1: date, d2: date):
        key = (d1, d2)
        if key in cache[grain]:
            print("skip cached", grain, d1, d2)
            return
        print("pull", grain, d1, d2, flush=True)
        rows = pull_period(d1.isoformat(), d2.isoformat())
        cache[grain][key] = aggregate_by_channel(rows)
        # persist after each period
        serial = {
            g: {f"{a.isoformat()}|{b.isoformat()}": v for (a, b), v in mp.items()}
            for g, mp in cache.items()
        }
        CACHE.write_text(json.dumps(serial, ensure_ascii=False))
        time.sleep(0.8)

    for a, b in month_bounds(period_start, period_end):
        store("month", a, b)
    for a, b in weeks(period_start, period_end):
        store("week", a, b)
    if os.environ.get("SKIP_DAYS") == "1":
        print("SKIP_DAYS=1 — day grain omitted this run")
    else:
        cur = period_start
        while cur <= period_end:
            store("day", cur, cur)
            cur += timedelta(days=1)

    rows = build_rows(cache)
    existing = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
    out = {
        "meta": {
            **(existing.get("meta") or {}),
            "title": "Сквозная отчетность | Магистратура (high-education)",
            "counter": CID,
            "period": {"from": "2026-09-01", "to": "2026-11-25"},
            "filter_note": "startURL contains high-education; lastSign/lastUTM; accuracy=full",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "sources": ["Yandex Metrika"],
            "pending": [
                "Yandex Direct spend/impressions/clicks",
                "amo entry point join",
                "UTM campaign/content grain in UI",
            ],
            "grains": ["month", "week", "day"],
        },
        "dictionaries": {
            **(existing.get("dictionaries") or {}),
            "channels": ["(все)"] + [c for c in CHANNEL_LABELS if c != "(все)"],
            "campaign_types": ["(все)", "Платные", "Органика", "Прямые переходы"],
            "scales": ["Месяц", "Неделя", "День"],
        },
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print("wrote", OUT_JSON, "rows", len(rows))
    from collections import Counter

    print(Counter(r["row_kind"] for r in rows))


if __name__ == "__main__":
    main()
