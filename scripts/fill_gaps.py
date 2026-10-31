#!/usr/bin/env python3
"""
Fill the gaps in the myepg guide with listings from our own guide (CTVU-EPG-OWN).

Rules (owner, 2026-10-30):
  1. Decided fresh on every run, from the newest myepg file.
  2. myepg always wins. Our listings are used only for a stream name that has no real
     current listing in myepg (name missing, only on a dummy channel, listings expired, or
     only filler such as "SIGN OFF" / "This is TNT Sports"). A myepg channel that is mostly
     filler (as many filler hours as real ones in the next 24h, e.g. FOX: games only, SIGN OFF
     in between) gives way when ours has over half an hour more real listings in that window.
     A name myepg also puts on a real channel is removed from its dummy/expired/filler copies.
  3. Never two guides on one name: a name we take over is removed from the myepg dummy or
     expired channel instead of being added twice. A dummy left with no names is dropped
     along with its filler programmes.
  4. Anything going wrong here means fetch_epg.py publishes plain myepg, as before.

myepg's real channels and their programmes are copied byte for byte.
"""
import datetime
import gzip
import hashlib
import html
import json
import re
import time

import requests

OWN_BASE = "https://raw.githubusercontent.com/Cabletvunleashed/CTVU-EPG-OWN/main/"
PREFIX = b"ctvu-own."   # our channel ids (myepg itself uses ids like "own.us", so not just "own.")

CHANNEL_RE = re.compile(rb"<channel\b[^>]*>.*?</channel>", re.S)
ID_RE = re.compile(rb'\bid="([^"]*)"')
NAME_RE = re.compile(rb"<display-name\b[^>]*>(.*?)</display-name>", re.S)
PROG_RE = re.compile(rb"<programme\b[^>]*>")
ATTR_RE = re.compile(rb'\b(stop|channel)="([^"]*)"')
TITLE_RE = re.compile(rb"<title\b[^>]*>(.*?)</title>", re.S)
# Titles that are not a listing. A myepg channel whose current listings are all filler is
# treated like a dummy. "This is <channel name>" counts only when it names the channel itself.
FILLER_TITLE = re.compile(r"\s*(test channel\s*\d*|channel no longer available|tv guide is not available|"
                          r"no information|no data|sign ?off|(channel )?off air|sendepause|to be announced|tba)\s*$", re.I)
# Also not a listing when counting real hours (but not enough on its own to call a channel filler-only).
PLACEHOLDER_TITLE = re.compile(r"\s*(local programming|paid programming|.*programmes start at.*|programming will resume.*)\s*$", re.I)
WINDOW = datetime.timedelta(hours=24)   # real hours are compared over the next 24h (myepg's own reach)
MARGIN_HOURS = 0.5


def fetch_own(timeout):
    """Our guide + the ids of its channels with real listings, both from the same build."""
    headers = {"User-Agent": "CTVU-EPG fill-gaps/1.0"}
    for attempt in (1, 2):
        r = requests.get(OWN_BASE + "real_ids.json", headers=headers, timeout=(30, timeout))
        r.raise_for_status()
        meta = json.loads(r.content)
        r = requests.get(OWN_BASE + "epg.xml.gz", headers=headers, timeout=(30, timeout))
        r.raise_for_status()
        if hashlib.sha256(r.content).hexdigest() == meta["epg_sha256"]:
            break
        if attempt == 2:      # caught mid-publish twice: placeholders could pass as real listings
            raise ValueError("our guide and real_ids.json are from different builds")
        time.sleep(60)
    raw = r.content
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw, {x.encode("utf-8") for x in meta["ids"]}, set(meta["english_names"])


def stop_utc(v):
    t = datetime.datetime.strptime(v[:14].decode(), "%Y%m%d%H%M%S")
    tz = v[14:].strip()
    if tz:
        sign = -1 if tz[:1] == b"-" else 1
        t -= sign * datetime.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
    return t


def programmes(raw, start, end):
    """Yield (channel id, stop or None, element start, element end) for every programme in raw[start:end]."""
    starts = [m.start() for m in PROG_RE.finditer(raw, start, end)]
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end
        tag_end = raw.index(b">", s)
        attrs = dict(ATTR_RE.findall(raw, s, tag_end))
        yield attrs.get(b"channel", b""), attrs.get(b"stop"), s, e


def live_ids(raw, start, end, now):
    live = set()
    for cid, stop, _, _ in programmes(raw, start, end):
        if cid not in live and stop and stop_utc(stop) > now:
            live.add(cid)
    return live


def key(name):
    return html.unescape(name.decode("utf-8", "replace")).strip()


def squash(text):
    return re.sub(r"[^a-z0-9]", "", text.lower()).replace("sports", "sport")


def filler_only(titles, names):
    """True when every current title is filler, or "This is X" where X is one of the channel's names."""
    flat = [squash(n) for n in names]
    for t in titles:
        if FILLER_TITLE.match(t):
            continue
        m = re.match(r"(?i)\s*this is (.+)$", t)
        x = squash(m.group(1)) if m else ""
        if x and any(x == n or (len(x) >= 4 and x in n) for n in flat):
            continue
        return False
    return True


def current_titles(raw, start, end, now, ids):
    """Titles of programmes still running or ahead, for the given channel ids."""
    out = {}
    for cid, stop, s, e in programmes(raw, start, end):
        if cid in ids and stop and stop_utc(stop) > now:
            t = TITLE_RE.search(raw, s, e)
            out.setdefault(cid, set()).add(key(t.group(1)) if t else "")
    return out


def slots(raw, start, end, now, ids, names_of):
    """cid -> [(start, stop, is_real)] of programmes in the next WINDOW, for the given channel ids."""
    until, out = now + WINDOW, {}
    for cid, stop, s, e in programmes(raw, start, end):
        if cid not in ids or not stop:
            continue
        sp = min(stop_utc(stop), until)
        if sp <= now:
            continue
        tag_end = raw.index(b">", s)
        m = re.search(rb'\bstart="([^"]*)"', raw[s:tag_end])
        st0 = stop_utc(m.group(1)) if m else now
        st = max(st0, now)
        if sp <= st:
            continue
        t = TITLE_RE.search(raw, s, e)
        t = key(t.group(1)) if t else ""
        filler = (PLACEHOLDER_TITLE.match(t) or filler_only([t], names_of.get(cid, ()))
                  or (re.match(r"(?i)\s*this is ", t) and stop_utc(stop) - st0 >= datetime.timedelta(hours=2)))
        out.setdefault(cid, []).append((st, sp, not filler))
    return out


def hours(slots_of, until=None, real=True):
    """Real (or filler) hours in slots_of, counted up to until."""
    return sum(((min(sp, until) if until else sp) - st).total_seconds() / 3600
               for st, sp, r in slots_of if r == real and (until is None or st < until))


def fill(my, own, own_real, now=None, scope="all", english=frozenset()):
    """Return (merged bytes, stats). scope: "english" = fill only English-folder stream names
    (AUTO/TRIAL feeds), "other" = only the rest (EU feeds), "all" = both."""
    now = now or datetime.datetime.utcnow()
    # ── myepg ──
    p0 = my.find(b"<programme")
    tv_end = my.rindex(b"</tv>")
    chan_end = p0 if p0 >= 0 else tv_end
    chans = []
    for m in CHANNEL_RE.finditer(my, 0, chan_end):
        cid = ID_RE.search(my, m.start(), my.index(b">", m.start())).group(1)
        chans.append((m.start(), m.end(), cid))
    if not chans:
        raise ValueError("no channels in myepg")
    live = live_ids(my, chan_end, tv_end, now)
    real = {cid for _, _, cid in chans if cid in live and not cid.startswith(b"dummy")}
    names_of = {}
    for s, e, cid in chans:
        names_of.setdefault(cid, []).extend(key(n) for n in NAME_RE.findall(my, s, e))
    titles = current_titles(my, chan_end, tv_end, now, real)
    filler = {cid for cid in real if filler_only(titles.get(cid, ()), names_of[cid])}
    real -= filler          # "SIGN OFF" / "This is TNT Sports" only: not a real listing
    my_slots = slots(my, chan_end, tv_end, now, real, names_of)
    weak = {cid for cid in real if hours(my_slots.get(cid, ()), real=False) >= hours(my_slots.get(cid, ()))}
    covered, present, strong, weak_by = set(), set(), set(), {}
    for s, e, cid in chans:
        for n in NAME_RE.findall(my, s, e):
            present.add(key(n))
            if cid in real:
                covered.add(key(n))
                if cid in weak:      # compared only up to where this myepg channel's listings end
                    sl = my_slots.get(cid, ())
                    weak_by.setdefault(key(n), []).append((hours(sl), max((sp for _, sp, _ in sl), default=now)))
                else:
                    strong.add(key(n))

    # ── ours: only channels with real listings ending in the future ──
    q0 = own.find(b"<programme")
    own_tv_end = own.rindex(b"</tv>")
    own_chan_end = q0 if q0 >= 0 else own_tv_end
    own_live = live_ids(own, own_chan_end, own_tv_end, now) & own_real
    own_titles = current_titles(own, own_chan_end, own_tv_end, now, own_live)
    own_names = {}
    for m in CHANNEL_RE.finditer(own, 0, own_chan_end):
        own_names.setdefault(ID_RE.search(m.group(0)).group(1), []).extend(key(n) for n in NAME_RE.findall(m.group(0)))
    own_slots = slots(own, own_chan_end, own_tv_end, now, own_live, own_names)
    take, taken_keys = [], set()
    for m in CHANNEL_RE.finditer(own, 0, own_chan_end):
        el = m.group(0)
        cid = ID_RE.search(el).group(1)
        if cid not in own_live or b"dummy" in cid.lower():   # provider "Dummy" ids carry filler only
            continue
        if filler_only(own_titles.get(cid, ()), [key(n) for n in NAME_RE.findall(el)]):
            continue                                       # no better than what myepg shows
        names = [n for n in NAME_RE.findall(el) if key(n) not in taken_keys
                 and (key(n) not in covered or (key(n) not in strong and all(
                     hours(own_slots.get(cid, ()), until) > theirs + MARGIN_HOURS for theirs, until in weak_by[key(n)])))
                 and not key(n).startswith("#")                   # "#### CBS ####" group headings
                 and (scope == "all" or (key(n) in english) == (scope == "english"))]
        if not names:
            continue
        taken_keys.update(key(n) for n in names)
        icon = re.search(rb"<icon\b[^>]*/>", el)
        take.append((cid, names, icon.group(0) if icon else b""))
    repoint = taken_keys & present
    # A name myepg puts on a real channel AND on a dummy/expired/filler one: TiviMate binds a name to
    # the LAST channel carrying it, so drop it from the empty one (FOX SPORTS SOUTH HD, 2026-10-31).
    stale = {key(n) for s, e, cid in chans if cid not in real for n in NAME_RE.findall(my, s, e)}
    stale &= {n for n in covered if scope == "all" or (n in english) == (scope == "english")}
    my_ids = {cid for _, _, cid in chans}
    if any(PREFIX + cid in my_ids for cid, _, _ in take):
        raise ValueError("one of our channel ids already exists in myepg")

    # ── write ──
    out = [my[:chans[0][0]]]
    kept_ids, dropped = set(), set()
    pos = chans[0][0]
    for s, e, cid in chans:
        out.append(my[pos:s])                 # whatever sat between channel elements
        pos = e
        el = my[s:e]
        gone = repoint if cid in real else repoint | stale
        if (cid in real and cid not in weak) or not gone:
            out.append(el)
            kept_ids.add(cid)
            continue
        names = NAME_RE.findall(el)
        if not any(key(n) in gone for n in names):
            out.append(el)
            kept_ids.add(cid)
            continue
        new = re.sub(rb"<display-name\b[^>]*>(.*?)</display-name>",
                     lambda mm: b"" if key(mm.group(1)) in gone else mm.group(0), el, flags=re.S)
        if NAME_RE.search(new):
            out.append(new)
            kept_ids.add(cid)
        else:
            dropped.add(cid)
    dropped -= kept_ids                       # a duplicate id still in use keeps its programmes
    out.append(my[pos:chan_end])
    for cid, names, icon in take:
        out.append(b'<channel id="' + PREFIX + cid + b'">' +
                   b"".join(b"<display-name>" + n + b"</display-name>" for n in names) + icon + b"</channel>")

    n_drop_prog = 0
    if dropped:
        pos = chan_end
        for cid, _, s, e in programmes(my, chan_end, tv_end):
            if cid in dropped:
                out.append(my[pos:s])
                pos = e
                n_drop_prog += 1
        out.append(my[pos:tv_end])
    else:
        out.append(my[chan_end:tv_end])

    want = {cid for cid, _, _ in take}
    n_add_prog = 0
    for cid, _, s, e in programmes(own, own_chan_end, own_tv_end):
        if cid in want:
            el = own[s:e].strip()
            el = el.replace(b'channel="' + cid + b'"', b'channel="' + PREFIX + cid + b'"', 1)
            out.append(el)
            n_add_prog += 1
    out.append(my[tv_end:])
    stats = {"own_channels_added": len(take), "names_added": len(taken_keys),
             "names_repointed": len(repoint), "names_unduplicated": len(stale - repoint), "names_new": len(taken_keys - present),
             "myepg_real_channels": len(real), "myepg_filler_only": len(filler), "myepg_mostly_filler": len(weak),
             "names_from_mostly_filler": len(taken_keys & covered), "dummy_dropped": len(dropped),
             "dummy_programmes_dropped": n_drop_prog, "own_programmes_added": n_add_prog}
    return b"".join(out), stats


def try_fill(my, scope, budget_seconds=300):
    """Merged guide, or None (with the reason printed) - the caller then publishes plain myepg."""
    t0 = time.monotonic()
    try:
        if scope not in ("english", "other", "all"):
            raise ValueError(f"FILL_SCOPE must be english, other or all, not {scope!r}")
        own, own_real, english = fetch_own(min(120, budget_seconds))
        merged, stats = fill(my, own, own_real, scope=scope, english=english)
    except Exception as e:  # noqa: BLE001  any problem -> plain myepg
        print(f"Gap fill skipped ({type(e).__name__}: {str(e)[:200]}) - publishing plain myepg")
        return None
    took = time.monotonic() - t0
    if took > budget_seconds:
        print(f"Gap fill took {took:.0f}s (budget {budget_seconds}s) - publishing plain myepg")
        return None
    print(f"Gap fill: {json.dumps(stats)} in {took:.0f}s")
    return merged
