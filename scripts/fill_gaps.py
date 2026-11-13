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
  5. (2026-11-01) A stream name myepg has no channel at all for may get one of our name-only
     placeholder channels ("blank_ids" in real_ids.json): it never replaces any myepg channel. A name in "bad_names" (myepg binds it to an unrelated channel, e.g. NBC
     SPORTS NETWORK on an eSports channel) loses that myepg alias and is filled by us instead.

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
    return (raw, {x.encode("utf-8") for x in meta["ids"]}, set(meta["english_names"]),
            {x.encode("utf-8") for x in meta.get("blank_ids", ())}, set(meta.get("bad_names", ())))


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



# ── alias re-point (2026-11-13, owner order "fix every channel that has no guide or just the channel name") ──
# myepg often binds a provider stream name to a NAME-ONLY channel ("Duck Dynasty" whose only titles are "Duck Dynasty",
# "Telemundo" for WNJU) while the very same channel exists in myepg WITH real listings under its own name
# ("Duck Dynasty (DUCKDYN)", wnju.us). This step moves such a stream name onto that real channel. Strict rules:
# the stream name is in this feed's scope, is on no real (non name-only) myepg channel now, is not in bad_names,
# and EXACTLY ONE real myepg channel has the same normalised primary name (provider prefix, quality tags and
# brackets removed) - or, for a name carrying a call sign "(WNJU)", EXACTLY ONE real channel with id "<callsign>.us".
# Names with a [CITY] bracket are never name-matched. Any problem here -> no moves (the rest of fill() is unchanged).
RP_PREFIX = re.compile(r"^\s*[^:|]{1,12}[:|]\s*")
RP_QUAL = re.compile(r"(?:\s*(?:ᴴᴰ|ᶠᴴᴰ|ᵁᴴᴰ|ᴿᴬᵂ|⁴ᴷ|⁸ᴷ|ⱽᴵᴾ|ᴳᴼᴸᴰ|⁶⁰ᶠᵖˢ|ʰᵉᵛᶜ|◉|FHD|UHD|HD|SD|4K|8K|HEVC|H265|RAW|VIP|GOLD|BACKUP|ALT|LQ|HQ|\d+\s?FPS))+\s*$", re.I)
RP_CALL = re.compile(r"\(([KW][A-Z]{2,3})(?:-[A-Z0-9]+)?\)")
# provider prefix -> required id ending of the myepg channel (the US app lines and the Caribbean AMP list carry US
# channels; UK: UK ones). Everything else (CA:, NZ:, 24/7:, HT: ...) is never moved.
RP_COUNTRY = {"US": b".us", "AT&T": b".us", "PRIME": b".us", "RK": b".us", "ROKU": b".us", "TUBI": b".us", "TV": b".us",
              "AMP": b".us", "PEACOCK": b".us", "UK": b".uk"}
RP_STOP = {"programming", "program", "programs", "programme", "programmes", "channel", "tv", "network", "live", "hd", "the",
           "schedule", "24", "7", "and", "sd", "fhd", "uhd", "4k", "raw", "east", "west", "tonight", "on", "air", "stream"}


def rp_key(name):
    x, prev = RP_PREFIX.sub("", name or ""), None
    while prev != x:
        prev, x = x, RP_QUAL.sub("", x).strip()
    x = re.sub(r"\([^)]*\)", " ", x)
    return re.sub(r"[^a-z0-9]", "", x.lower())


def rp_toks(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def name_only(titles, names):
    """True when every current title is only the channel's own words (or filler / a bare 'Programming')."""
    ns = set()
    for n in names:
        ns |= rp_toks(RP_PREFIX.sub("", n))
    ns -= RP_STOP
    for t in titles:
        if FILLER_TITLE.match(t):
            continue
        tt = rp_toks(t) - RP_STOP
        if tt and not tt <= ns:
            return False
    return True


def alias_moves(chans, my, names_of, real, titles, filler, english, bad, scope, own_real_keys=frozenset()):
    """{stream name: myepg channel id (bytes)} for names to re-point; {} when scope is not english."""
    if scope != "english":
        return {}
    weak_only = {cid for cid in real if name_only(titles.get(cid, ()), names_of[cid])}
    strong = real - weak_only
    by_name = {}
    for _, _, cid in chans:
        for n in names_of[cid]:
            by_name.setdefault(n, set()).add(cid)
    prim, calls = {}, {}
    for cid in strong:
        if cid.startswith(b"dummy"):
            continue
        first = next((n for n in names_of[cid] if not RP_PREFIX.match(n)), None)
        if first:
            prim.setdefault(rp_key(first), set()).add(cid)
        m = re.fullmatch(rb"([kw][a-z]{2,3})\.us", cid.lower())
        if m:
            calls.setdefault(m.group(1).decode(), set()).add(cid)
    moves = {}
    for n in sorted(english):
        cids = by_name.get(n, set())
        if n in bad or n.startswith("#") or any(c in strong for c in cids):
            continue
        # only (a) names myepg binds ONLY to name-only channels, or (b) names myepg lacks and we have no real listing for
        if cids and not all(c in weak_only for c in cids if c in real) or (not cids and n in own_real_keys):
            continue
        if cids and any(c not in real for c in cids):
            continue
        pm = RP_PREFIX.match(n)
        want = RP_COUNTRY.get(pm.group(0).strip().rstrip(":|").strip().upper()) if pm else None
        if not want:
            continue                  # prefix of another country / unknown: never cross countries
        cs = RP_CALL.search(n)
        hit = None
        if cs:
            ids = {i for i in calls.get(cs.group(1).lower(), set()) if i.lower().endswith(want)}
            if len(ids) == 1:
                hit = next(iter(ids))
        elif "[" not in n:
            k = rp_key(n)
            ids = {i for i in prim.get(k, set()) if i.lower().endswith(want)} if len(k) >= 4 else set()
            if len(ids) == 1:
                hit = next(iter(ids))
        if hit is not None:
            moves[n] = hit
    return moves


def fill(my, own, own_real, now=None, scope="all", english=frozenset(), blank=frozenset(), bad_names=frozenset()):
    """Return (merged bytes, stats). scope: "english" = fill only English-folder stream names
    (AUTO/TRIAL feeds), "other" = only the rest (EU feeds), "all" = both."""
    now = now or datetime.datetime.utcnow()
    bad = {n.strip() for n in bad_names}
    blank = set(blank) - set(own_real)       # a channel counted as real (Caribbean placeholders) stays real
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
            if key(n) in bad:             # a wrong binding does not count as myepg having the name
                continue
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
    own_live = live_ids(own, own_chan_end, own_tv_end, now) & (own_real | blank)
    own_titles = current_titles(own, own_chan_end, own_tv_end, now, own_live)
    own_names = {}
    for m in CHANNEL_RE.finditer(own, 0, own_chan_end):
        own_names.setdefault(ID_RE.search(m.group(0)).group(1), []).extend(key(n) for n in NAME_RE.findall(m.group(0)))
    own_slots = slots(own, own_chan_end, own_tv_end, now, own_live, own_names)
    # names our own guide has a real listing for are left to the normal fill (the move only helps names with none)
    # (a Caribbean name-block "ph.*" counts as real in real_ids.json so the feeds list the name, but it is only the channel's
    #  own name as the title: it must not stop the move onto a real myepg channel of the same name - CR/AMP, 2026-11-13)
    own_real_keys = {n for cid in own_live if cid in own_real and not filler_only(own_titles.get(cid, ()), own_names.get(cid, ()))
                     and not (cid.startswith(b"ph.") and name_only(own_titles.get(cid, ()), own_names.get(cid, ())))
                     for n in own_names.get(cid, ())}
    # alias re-point (see alias_moves): stream names moved onto a real myepg channel count as covered by it
    try:
        moves = alias_moves(chans, my, names_of, real, titles, filler, english, bad, scope, own_real_keys)
    except Exception as e:  # noqa: BLE001  any problem -> no moves, fill() behaves exactly as before
        print(f"alias re-point skipped ({type(e).__name__}: {str(e)[:120]})")
        moves = {}
    add_names = {}
    for n, rid in moves.items():
        covered.add(n)
        present.add(n)
        strong.add(n)
        add_names.setdefault(rid, []).append(n)
    bad = bad | set(moves)

    take, taken_keys = [], set()
    for m in CHANNEL_RE.finditer(own, 0, own_chan_end):
        el = m.group(0)
        cid = ID_RE.search(el).group(1)
        if cid not in own_live or b"dummy" in cid.lower():   # provider "Dummy" ids carry filler only
            continue
        if filler_only(own_titles.get(cid, ()), [key(n) for n in NAME_RE.findall(el)]):
            continue                                       # no better than what myepg shows
        names = [n for n in NAME_RE.findall(el) if key(n) not in taken_keys
                 and ((key(n) not in present) if cid in blank else (key(n) not in covered or (key(n) not in strong and all(
                     hours(own_slots.get(cid, ()), until) > theirs + MARGIN_HOURS for theirs, until in weak_by[key(n)]))))
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

    def with_moved(cid, el):
        """alias re-point: add the moved stream names to their new (real) myepg channel."""
        if cid not in add_names:
            return el
        return el.replace(b"</channel>", b"".join(b"<display-name>" + html.escape(n, quote=False).encode("utf-8") +
                                                  b"</display-name>" for n in add_names[cid]) + b"</channel>", 1)

    # ── write ──
    out = [my[:chans[0][0]]]
    kept_ids, dropped = set(), set()
    pos = chans[0][0]
    for s, e, cid in chans:
        out.append(my[pos:s])                 # whatever sat between channel elements
        pos = e
        el = my[s:e]
        gone = (repoint if cid in real else repoint | stale) | bad
        if not gone or (cid in real and cid not in weak and not any(key(n) in bad for n in NAME_RE.findall(el))):
            out.append(with_moved(cid, el))
            kept_ids.add(cid)
            continue
        names = NAME_RE.findall(el)
        if not any(key(n) in gone for n in names):
            out.append(with_moved(cid, el))
            kept_ids.add(cid)
            continue
        new = re.sub(rb"<display-name\b[^>]*>(.*?)</display-name>",
                     lambda mm: b"" if key(mm.group(1)) in gone else mm.group(0), el, flags=re.S)
        if NAME_RE.search(new):
            out.append(with_moved(cid, new))
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
             "dummy_programmes_dropped": n_drop_prog, "own_programmes_added": n_add_prog,
             "names_from_blank": len({key(n) for cid, names, _ in take if cid in blank for n in names}),
             "bad_aliases_removed": len(bad & {key(n) for s, e, c in chans for n in NAME_RE.findall(my, s, e)})}
    stats["names_moved"] = len(moves)       # (included in bad_aliases_removed: a moved name leaves its old channel)
    return b"".join(out), stats


def try_fill(my, scope, budget_seconds=300):
    """Merged guide, or None (with the reason printed) - the caller then publishes plain myepg."""
    t0 = time.monotonic()
    try:
        if scope not in ("english", "other", "all"):
            raise ValueError(f"FILL_SCOPE must be english, other or all, not {scope!r}")
        own, own_real, english, blank, bad_names = fetch_own(min(120, budget_seconds))
        merged, stats = fill(my, own, own_real, scope=scope, english=english, blank=blank, bad_names=bad_names)
    except Exception as e:  # noqa: BLE001  any problem -> plain myepg
        print(f"Gap fill skipped ({type(e).__name__}: {str(e)[:200]}) - publishing plain myepg")
        return None
    took = time.monotonic() - t0
    if took > budget_seconds:
        print(f"Gap fill took {took:.0f}s (budget {budget_seconds}s) - publishing plain myepg")
        return None
    print(f"Gap fill: {json.dumps(stats)} in {took:.0f}s")
    return merged
