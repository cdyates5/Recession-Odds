#!/usr/bin/env python3
"""US Recession Odds Monitor - rebuild.py

Rebuilds the dashboard from public APIs. Works from an empty directory and unattended in CI.

    python3 rebuild.py [OUTDIR] [--cache DIR] [--skip-gate]

    OUTDIR       where the site is written (default: current directory)
                   index.html                   the dashboard (single self-contained file)
                   us_recession_odds_weekly.csv weekly Friday-close odds, every contract
    --cache DIR  raw-data cache (default: ./cache). Created if missing. Settled contracts are
                 served from it; live contracts are refetched and fall back to it on failure.
    --skip-gate  write output even if the sanity checks fail (local use only)

Sources (all public, no API keys):
    Kalshi      api.elections.kalshi.com/trade-api/v2   live + /historical candlesticks (daily)
    Polymarket  gamma-api.polymarket.com (events) + clob.polymarket.com/prices-history (daily)
    NY Fed      newyorkfed.org yield-curve recession probability (allmonth.xls)

New calendar-year contracts (e.g. Kalshi KXRECSSNBER-28, Polymarket "US recession by end of 2028?")
are discovered automatically; the current/next-year roles roll on 1 January.
"""
import sys, os, re, io, json, time, argparse, datetime as dt, urllib.request, urllib.error, urllib.parse
import pandas as pd
import numpy as np

UA = {'User-Agent': 'Mozilla/5.0 (us-recession-odds-dashboard)'}
K = 'https://api.elections.kalshi.com/trade-api/v2'
GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
NYFED = 'https://www.newyorkfed.org/medialibrary/media/research/capital_markets/allmonth.xls'
BASE_RATE = 0.15    # ~12 NBER recession starts 1946-2025 (80 yrs) -> ~15% per calendar year
MIN_YEAR = 2023     # first contract year tracked (Kalshi's 2022 contract lived 4 weeks before the Q2'22 print)
PAST_YEARS = 3      # past contract years drawn in the charts beside current + next year

# Known contracts with curated rule summaries. Anything newer is discovered at build time.
SEED = [
    dict(key='K23', venue='Kalshi', year=2023, ident='RECSSNBER-23',
         window='2Q negative real GDP within 2023 (BEA advance)'),
    dict(key='K24', venue='Kalshi', year=2024, ident='RECSSNBER-24',
         window='2Q negative real GDP in 2023 or 2024 (BEA)'),
    dict(key='K25', venue='Kalshi', year=2025, ident='RECSSNBER-25',
         window='2Q negative real GDP in 2024 or 2025 (BEA)'),
    dict(key='K26', venue='Kalshi', year=2026, ident='KXRECSSNBER-26', series='KXRECSSNBER',
         window='2Q negative real GDP in 2025 or 2026 (BEA)'),
    dict(key='K27', venue='Kalshi', year=2027, ident='KXRECSSNBER-27', series='KXRECSSNBER',
         window="2Q negative real GDP, Q4'26 through Q4'27 (BEA advance)"),
    dict(key='P24', venue='Polymarket', year=2024, ident='us-recession-in-2024-1', cumulative=False,
         window='2Q negative real GDP within 2024 (BEA, latest revision)'),
    dict(key='P25', venue='Polymarket', year=2025, ident='us-recession-in-2025', cumulative=False,
         window="2Q negative GDP Q4'24-Q4'25, or NBER announces a 2025 recession by 31 Dec 2025"),
    dict(key='P26', venue='Polymarket', year=2026, ident='us-recession-by-end-of-2026', cumulative=True,
         window="2Q negative GDP Q2'25-Q4'26, or NBER announces a 2025/26 recession"),
    dict(key='P27', venue='Polymarket', year=2027, ident='us-recession-by-end-of-2027-20260807185409760', cumulative=True,
         window="Cumulative: 2Q negative GDP Q2'25-Q4'27, or NBER announces a 2025-27 recession"),
]

# Event markers. Add a line here to annotate a new shock; nothing else needs changing.
EVENTS = [
    ('2023-03-10', 'Silicon Valley Bank fails'),
    ('2024-08-02', 'July payrolls trigger the Sahm rule'),
    ('2025-04-02', '"Liberation Day" tariff announcement'),
    ('2025-04-30', "Q1'25 GDP advance estimate prints -0.3%"),
    ('2026-02-28', 'US-Israel strikes on Iran; Hormuz disruption'),
    ('2026-04-08', 'US-Iran two-week ceasefire; oil falls ~17%'),
    ('2026-07-14', 'US-Iran strikes resume'),
]


# ============================================================================ utilities
def log(*a):
    print(*a, flush=True)


def get_json(url, retries=5):
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(2 + 3 * i); continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i < retries - 1:
                time.sleep(2 + 3 * i); continue
            raise


def get_bytes(url, retries=4):
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return r.read()
        except Exception:
            if i < retries - 1:
                time.sleep(3 + 3 * i); continue
            raise


def asof_date(ts):
    """Venue timestamp -> US as-of date (ts - 5h). Kalshi daily candles end 04:00 UTC and Polymarket
    daily points sit near 00:00 UTC; both describe the prior US trading day."""
    return (dt.datetime.fromtimestamp(ts, dt.timezone.utc) - dt.timedelta(hours=5)).date()


def iso_ts(s):
    return int(dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp())


def yy(y):
    return f'{y % 100:02d}'


# ============================================================================ discovery
def kalshi_window(rules):
    m = re.search(r'two consecutive quarters of negative GDP growth (.*?),? according', rules or '', re.I)
    return f'2Q negative real GDP {m.group(1)} (BEA)' if m else (rules or 'See Kalshi rules')[:140]


def pm_window(desc, cumulative):
    m = re.search(r'between (Q\d) (\d{4}) and (Q\d) (\d{4})', desc or '')
    rng = f"{m.group(1)}'{m.group(2)[2:]}-{m.group(3)}'{m.group(4)[2:]}" if m else 'the contract window'
    nber = ', or an NBER announcement' if 'NBER' in (desc or '') else ''
    return ('Cumulative: ' if cumulative else '') + f'2Q negative GDP {rng}{nber}'


def discover_kalshi():
    found = {}
    for s in ('KXRECSSNBER', 'RECSSNBER'):
        for path in (f'/markets?series_ticker={s}&limit=200', f'/historical/markets?series_ticker={s}&limit=200'):
            try:
                ms = get_json(K + path).get('markets', [])
            except Exception as e:
                log(f'    discovery warning (Kalshi {s}): {e}'); continue
            for m in ms:
                mt = re.fullmatch(r'(?:KX)?RECSSNBER-(\d{2})', m.get('ticker', ''))
                if not mt:
                    continue
                y = 2000 + int(mt.group(1))
                if y < MIN_YEAR:
                    continue
                found[f'K{yy(y)}'] = dict(key=f'K{yy(y)}', venue='Kalshi', year=y, ident=m['ticker'],
                                          series=m['ticker'].split('-')[0], window=kalshi_window(m.get('rules_primary')))
            time.sleep(0.6)
    return found


def discover_polymarket():
    best = {}
    for q in ('US recession', 'U.S. recession'):
        try:
            d = get_json(f"{GAMMA}/public-search?q={urllib.parse.quote(q)}&limit_per_type=50&events_status=all")
        except Exception as e:
            log(f'    discovery warning (Polymarket): {e}'); continue
        for e in d.get('events', []):
            mt = re.fullmatch(r'U\.?S\.? recession (in|by end of) (\d{4})\?', (e.get('title') or '').strip(), re.I)
            if not mt:
                continue
            y = int(mt.group(2)); vol = float(e.get('volume') or 0)
            if y < MIN_YEAR or (y in best and best[y]['_vol'] >= vol):
                continue
            cum = mt.group(1).lower() == 'by end of'
            best[y] = dict(key=f'P{yy(y)}', venue='Polymarket', year=y, ident=e['slug'], cumulative=cum,
                           window=pm_window(e.get('description'), cum), _vol=vol)
        time.sleep(0.4)
    return {v['key']: {k: x for k, x in v.items() if k != '_vol'} for v in best.values()}


def build_registry(cached_registry):
    reg = {c['key']: dict(c) for c in SEED}
    for c in cached_registry or []:            # contracts discovered on earlier runs
        reg.setdefault(c['key'], c)
    for fn in (discover_kalshi, discover_polymarket):
        for k, c in fn().items():
            if k not in reg:
                log(f'    discovered new contract {k}: {c["ident"]}')
                reg[k] = c
    order = {'Kalshi': 0, 'Polymarket': 1}
    return sorted(reg.values(), key=lambda c: (c['year'], order[c['venue']]))


# ============================================================================ fetch
def fetch_kalshi(c):
    t = c['ident']
    try:
        m = get_json(f'{K}/markets/{t}')['market']; hist = False
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        m = get_json(f'{K}/historical/markets/{t}')['market']; hist = True
    series = c.get('series') or m.get('series_ticker') or t.split('-')[0]
    open_ts, close_ts = iso_ts(m['open_time']), iso_ts(m['close_time'])
    end_ts = min(close_ts, int(time.time()))
    rows, start = [], open_ts - 86400
    base = f'{K}/historical/markets/{t}' if hist else f'{K}/series/{series}/markets/{t}'
    while start < end_ts:
        stop = min(start + 400 * 86400, end_ts)
        d = get_json(f'{base}/candlesticks?start_ts={start}&end_ts={stop}&period_interval=1440')
        for k in d.get('candlesticks', []):
            pr = k.get('price', {})
            close = pr.get('close_dollars', pr.get('close'))
            if close is None:
                continue
            rows.append([str(asof_date(k['end_period_ts'])), round(float(close), 4)])
        start = stop
        time.sleep(1.1)
    result = (m.get('result') or '').lower() or 'open'
    last = float(m.get('last_price_dollars') or 'nan')
    today = str(asof_date(time.time()))
    if result == 'open' and not np.isnan(last) and (not rows or today > rows[-1][0]):
        rows.append([today, round(last, 4)])       # today's last trade keeps the live series current
    meta = dict(open=m['open_time'][:10], close=m['close_time'][:10], result=result,
                volume=float(m.get('volume_fp', m.get('volume')) or 0),
                oi=float(m.get('open_interest_fp', m.get('open_interest')) or 0),
                rules=m.get('rules_primary', ''), last=last, url='https://kalshi.com/markets/kxrecssnber')
    return rows, meta


def fetch_polymarket(c):
    e = get_json(f"{GAMMA}/events?slug={c['ident']}")[0]
    mk = e['markets'][0]
    yes = json.loads(mk['clobTokenIds'])[0]
    d = get_json(f'{CLOB}/prices-history?market={yes}&interval=max&fidelity=1440')
    rows = [[str(asof_date(h['t'])), round(float(h['p']), 4)] for h in d.get('history', [])]
    prices = json.loads(mk.get('outcomePrices') or '[null,null]')
    closed = bool(mk.get('closed'))
    result = ('yes' if float(prices[0]) > 0.5 else 'no') if closed else 'open'
    meta = dict(open=(mk.get('startDate') or '')[:10], close=(mk.get('endDate') or '')[:10], result=result,
                volume=float(mk.get('volume') or 0), oi=None,
                rules=(e.get('description') or '').split('Otherwise')[0].strip(),
                last=float(prices[0]) if prices[0] is not None else None,
                url=f"https://polymarket.com/event/{c['ident']}")
    time.sleep(0.4)
    return rows, meta


def fetch_nyfed():
    x = pd.read_excel(io.BytesIO(get_bytes(NYFED)), sheet_name=0)
    x['Date'] = pd.to_datetime(x['Date'], format='%d-%b-%Y')
    spread = dict(zip(x['Date'], x['Spread']))
    rows = []
    for d, p in zip(x['Date'], x['Rec_prob']):
        if pd.isna(p) or d < pd.Timestamp('2021-01-01'):
            continue
        origin = d - pd.DateOffset(months=12)       # prob is dated at the target month t+12
        s = spread.get(origin)
        rows.append([str(origin.date()), round(float(p), 6), None if s is None or pd.isna(s) else round(float(s), 4)])
    return rows


# ============================================================================ cache
def _write_rows_json(path, head, rows):
    with open(path, 'w', encoding='utf-8') as f:     # one row per line keeps git diffs readable
        f.write('{"meta":' + json.dumps(head, sort_keys=True) + ',\n"rows":[\n')
        f.write(',\n'.join(json.dumps(r) for r in rows))
        f.write('\n]}\n')


def load_cache(d):
    out = {'registry': [], 'contracts': {}, 'nyfed': None}
    if not os.path.isdir(d):
        return out
    p = os.path.join(d, 'registry.json')
    if os.path.exists(p):
        out['registry'] = json.load(open(p, encoding='utf-8'))
    for fn in os.listdir(d):
        m = re.fullmatch(r'([KP]\d{2})\.json', fn)
        if m:
            out['contracts'][m.group(1)] = json.load(open(os.path.join(d, fn), encoding='utf-8'))
    p = os.path.join(d, 'nyfed.json')
    if os.path.exists(p):
        out['nyfed'] = json.load(open(p, encoding='utf-8'))
    return out


def save_cache(d, registry, contracts, nyfed_rows, nyfed_fetched):
    os.makedirs(d, exist_ok=True)
    json.dump(registry, open(os.path.join(d, 'registry.json'), 'w', encoding='utf-8'), indent=1, sort_keys=True)
    for k, v in contracts.items():
        _write_rows_json(os.path.join(d, f'{k}.json'), v['meta'], v['rows'])
    _write_rows_json(os.path.join(d, 'nyfed.json'), {'fetched': nyfed_fetched, 'source': NYFED}, nyfed_rows)


def gather(cache_dir):
    """Fetch everything, using the cache for settled contracts and as a fallback for failures."""
    cache = load_cache(cache_dir)
    stale = []
    log('Discovering contracts ...')
    registry = build_registry(cache['registry'])
    contracts = {}
    for c in registry:
        k = c['key']; cached = cache['contracts'].get(k)
        if cached and cached['meta'].get('result') in ('yes', 'no'):
            contracts[k] = cached; src = 'cache (settled)'
        else:
            try:
                rows, meta = fetch_kalshi(c) if c['venue'] == 'Kalshi' else fetch_polymarket(c)
                if not rows:
                    raise ValueError('empty price history')
                meta['fetched'] = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
                contracts[k] = dict(meta=meta, rows=rows); src = 'live'
            except Exception as e:
                if not cached:
                    raise RuntimeError(f'{k}: fetch failed and no cache to fall back on: {e}')
                contracts[k] = cached; src = 'CACHE FALLBACK'
                stale.append(dict(source=f"{c['venue']} {c['year']}", since=cached['rows'][-1][0], error=str(e)[:160]))
        r = contracts[k]['rows']
        log(f"  {c['venue']:10s} {k}  {len(r):4d} days  {r[0][0]} -> {r[-1][0]}  [{src}]")
    log('  NY Fed yield-curve model ...')
    try:
        ny_rows = fetch_nyfed(); ny_fetched = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    except Exception as e:
        if not cache['nyfed']:
            raise RuntimeError(f'NY Fed fetch failed and no cache: {e}')
        ny_rows = cache['nyfed']['rows']; ny_fetched = cache['nyfed']['meta'].get('fetched')
        stale.append(dict(source='NY Fed model', since=ny_fetched, error=str(e)[:160]))
    save_cache(cache_dir, registry, contracts, ny_rows, ny_fetched)
    return registry, contracts, ny_rows, stale


# ============================================================================ process
def r1(x, nd=2):
    if x is None:
        return None
    x = float(x)
    return None if np.isnan(x) else round(x, nd)


def fmtp(v):   # round half up, matching the page's JS toFixed
    if v is None:
        return '-'
    return f'{np.floor(v * 10 + 0.5) / 10:.1f}%' if abs(v) < 10 else f'{int(np.floor(v + 0.5))}%'


def build(registry, contracts, ny_rows, stale):
    REG = {c['key']: c for c in registry}
    keys = [c['key'] for c in registry]
    daily = {}
    live = [k for k in keys if contracts[k]['meta']['result'] == 'open']
    if not live:
        raise RuntimeError('no live contracts found')
    asof = max(pd.Timestamp(contracts[k]['rows'][-1][0]) for k in live)
    for k in keys:
        rows = contracts[k]['rows']
        s = pd.Series([r[1] * 100.0 for r in rows], index=pd.to_datetime([r[0] for r in rows]))
        s = s[~s.index.duplicated(keep='last')].sort_index()
        end = asof if k in live else s.index.max()
        daily[k] = s.reindex(pd.date_range(s.index.min(), end, freq='D')).ffill()
    Dd = pd.DataFrame(daily).reindex(pd.date_range(min(s.index.min() for s in daily.values()), asof, freq='D'))[keys]
    Dw = Dd.resample('W-FRI').last().dropna(how='all')
    if Dw.index[-1] > asof:                       # label the partial final week by its last data date
        Dw = Dw.rename(index={Dw.index[-1]: asof})

    cy, ny = asof.year, asof.year + 1
    def slot(y):
        s = y - cy + PAST_YEARS
        return s if 0 <= s <= PAST_YEARS + 1 else -1

    # Polymarket single-year odds from nested cumulative contracts: (P_Y - P_Y-1) / (1 - P_Y-1)
    imp_d, imp_w = {}, {}
    for c in registry:
        if c['venue'] == 'Polymarket' and c.get('cumulative') and f"P{yy(c['year'] - 1)}" in REG:
            a, b = c['key'], f"P{yy(c['year'] - 1)}"
            for D_, out in ((Dd, imp_d), (Dw, imp_w)):
                v = (D_[a] - D_[b]) / (100 - D_[b]) * 100
                v = v.where(D_[a].notna() & D_[b].notna())
                if v.notna().any():
                    out[str(c['year'])] = v

    # venue gap (Polymarket - Kalshi), same calendar year, both venues present
    gap_years = [y for y in sorted({c['year'] for c in registry}) if f'K{yy(y)}' in REG and f'P{yy(y)}' in REG and slot(y) >= 0]
    gap_d = {str(y): Dd[f'P{yy(y)}'] - Dd[f'K{yy(y)}'] for y in gap_years}
    gap_w = {str(y): Dw[f'P{yy(y)}'] - Dw[f'K{yy(y)}'] for y in gap_years}
    gap_d = {y: v for y, v in gap_d.items() if v.notna().any()}
    gap_w = {y: v for y, v in gap_w.items() if v.notna().any()}

    def arr(s):
        return [r1(v) for v in s.tolist()]

    # life-cycle grid: days relative to 1 Jan of the contract year
    first_off = min((daily[k].index[0] - pd.Timestamp(f"{REG[k]['year']}-01-01")).days for k in keys if slot(REG[k]['year']) >= 0)
    lo = max(-400, min(-260, first_off - 7))
    offs = list(range(lo, 401))
    life = {}
    for c in registry:
        if slot(c['year']) < 0:
            continue
        j1 = pd.Timestamp(f"{c['year']}-01-01"); s = daily[c['key']]
        life[c['key']] = [r1(s.get(j1 + pd.Timedelta(days=o))) for o in offs]

    def at(key, date):
        if key not in daily:
            return None
        d = pd.Timestamp(date); s = daily[key]
        return r1(s.get(d)) if d in s.index else None

    cur_off = (asof - pd.Timestamp(f'{cy}-01-01')).days
    nxt_off = (asof - pd.Timestamp(f'{ny}-01-01')).days
    same = dict(curYear=cy, nextYear=ny, curOff=cur_off, nextOff=nxt_off, cur={}, nxt={})
    for c in registry:
        y = c['year']
        same['cur'][c['key']] = at(c['key'], pd.Timestamp(f'{y}-01-01') + pd.Timedelta(days=cur_off))
        same['nxt'][c['key']] = at(c['key'], pd.Timestamp(f'{y}-01-01') + pd.Timedelta(days=nxt_off))

    out_contracts = []
    for c in registry:
        k = c['key']; m = contracts[k]['meta']; s = daily[k].dropna(); w = Dw[k].dropna()
        last, ld = s.iloc[-1], s.index[-1]
        def chg(days):
            prior = s.loc[:ld - pd.Timedelta(days=days)]
            return r1(last - prior.iloc[-1]) if len(prior) else None
        yr = s.loc[ld - pd.Timedelta(days=364):]
        ytd = s.loc[pd.Timestamp(f'{ld.year}-01-01'):]
        settled = m['result'] in ('yes', 'no')
        brier = r1(((w / 100 - (1 if m['result'] == 'yes' else 0)) ** 2).mean(), 4) if settled else None
        out_contracts.append(dict(
            key=k, venue=c['venue'], year=c['year'], slot=slot(c['year']), window=c['window'], ident=c['ident'],
            url=m['url'], open=m['open'], close=m['close'], result=m['result'], volume=r1(m['volume'], 0),
            oi=r1(m.get('oi'), 0), cumulative=bool(c.get('cumulative')),
            first=r1(s.iloc[0]), last=r1(last), lastDate=str(ld.date()), lastObs=contracts[k]['rows'][-1][0],
            d1w=chg(7), d4w=chg(28), d13w=chg(91), hi52=r1(yr.max()), lo52=r1(yr.min()),
            ytdHi=r1(ytd.max()) if len(ytd) else None, ytdHiDate=str(ytd.idxmax().date()) if len(ytd) else None,
            peak=r1(s.max()), peakDate=str(s.idxmax().date()), entering=at(k, f"{c['year'] - 1}-12-31"),
            midyear=at(k, f"{c['year']}-06-30"), brier=brier, weeks=int(len(w)), wks50=int((w >= 50).sum())))
    C = {c['key']: c for c in out_contracts}

    resolved = [c for c in out_contracts if c['result'] in ('yes', 'no')]
    if resolved:
        pooled = pd.concat([(Dw[c['key']].dropna() / 100 - (1 if c['result'] == 'yes' else 0)) for c in resolved])
        brier_pooled = r1((pooled ** 2).mean(), 4)
    else:
        brier_pooled = None
    brier_base = r1(BASE_RATE ** 2, 4)

    # NY Fed model (origin-dated) + Kalshi continuous current/next-year, month-end
    ny_df = pd.DataFrame(ny_rows, columns=['origin', 'prob', 'spread'])
    ny_df['origin'] = pd.to_datetime(ny_df['origin'])
    ny_df = ny_df[(ny_df['origin'] <= asof) & ny_df['prob'].notna()]
    months = pd.date_range(max(pd.Timestamp('2022-11-01'), Dd.index[0].replace(day=1)), asof, freq='MS')
    def month_end_val(key, mstart):
        if key not in daily:
            return None
        me = min(mstart + pd.offsets.MonthEnd(0), asof); sub = daily[key].loc[:me].dropna()
        return r1(sub.iloc[-1]) if len(sub) and sub.index[-1] >= mstart else None
    front = [month_end_val(f'K{yy(m_.year)}', m_) for m_ in months]
    nxt = [month_end_val(f'K{yy(m_.year + 1)}', m_) for m_ in months]
    nymap = {r.origin: r for r in ny_df.itertuples()}
    nyp = [r1(nymap[m_].prob * 100) if m_ in nymap else None for m_ in months]
    ny_last = ny_df.iloc[-1]

    # ---------------- findings, generated from the numbers
    def dshort(d):   # portable: %-d fails on Windows
        d = pd.Timestamp(d)
        return f"{d.day} {d.strftime('%b')}"

    def dlong(d):
        d = pd.Timestamp(d)
        return f"{d.day} {d.strftime('%b %Y')}"
    ck, cp = C.get(f'K{yy(cy)}'), C.get(f'P{yy(cy)}')
    nk, npm = C.get(f'K{yy(ny)}'), C.get(f'P{yy(ny)}')
    findings = []
    prior_cur = [(y, same['cur'][f'K{yy(y)}']) for y in range(cy - PAST_YEARS, cy) if same['cur'].get(f'K{yy(y)}') is not None]
    if ck:
        txt = f"<b>{cy}:</b> {fmtp(ck['last'])} on Kalshi" + (f" and {fmtp(cp['last'])} on Polymarket" if cp and cp['result'] == 'open' else '')
        if ck['ytdHi'] is not None and ck['ytdHiDate'] and ck['ytdHi'] > ck['last'] + 2:
            txt += f", down from year highs of {fmtp(ck['ytdHi'])} ({dshort(ck['ytdHiDate'])})"
            if cp and cp['ytdHi'] is not None:
                txt += f" and {fmtp(cp['ytdHi'])} ({dshort(cp['ytdHiDate'])})"
        txt += '.'
        if prior_cur:
            lo_c, hi_c = min(v for _, v in prior_cur), max(v for _, v in prior_cur)
            settled_all = all(C.get(f'K{yy(y)}', {}).get('result') == 'no' for y, _ in prior_cur)
            rel = 'in line with' if lo_c - 2 <= ck['last'] <= hi_c + 2 else ('below' if ck['last'] < lo_c else 'above')
            txt += (f" That is {rel} the {prior_cur[0][0]}&ndash;{prior_cur[-1][0]} Kalshi contracts on the same date "
                    f"({fmtp(lo_c)[:-1]}&ndash;{fmtp(hi_c)})" + (', all of which settled No.' if settled_all else '.'))
        findings.append(txt)
    prior_nxt = [(y, same['nxt'][f'K{yy(y)}']) for y in range(cy - PAST_YEARS + 1, ny) if same['nxt'].get(f'K{yy(y)}') is not None]
    if nk:
        vals = sorted([v for _, v in prior_nxt] + [nk['last']])
        rank = vals.index(nk['last']) + 1
        rank_txt = ('the lowest' if rank == 1 else 'the highest' if rank == len(vals) else f'#{rank} of {len(vals)}') if prior_nxt else 'the only'
        txt = f"<b>{ny}:</b> Kalshi prices {fmtp(nk['last'])}, {rank_txt} next-year reading at this point in the calendar"
        txt += (' (' + ', '.join(f'{y}: {fmtp(v)}' for y, v in prior_nxt) + f', each {abs(nxt_off)} days before its year began).') if prior_nxt else '.'
        findings.append(txt)
    else:
        lk = C.get(f'K{yy(cy)}')
        findings.append(f"<b>{ny}:</b> no {ny} contract has listed on Kalshi yet" +
                        (f" (the {cy} contract listed on {dlong(lk['open'])})." if lk else '.'))
    if npm and str(ny) in imp_d:
        imp_last = r1(imp_d[str(ny)].dropna().iloc[-1], 1)
        txt = (f"<b>Venue basis:</b> Polymarket's {ny} contract is cumulative and NBER-inclusive. Netting out the {cy} contract leaves "
               f"{fmtp(imp_last)} for {ny} alone")
        if nk:
            txt += f", {imp_last - nk['last']:+.0f}pp vs Kalshi"
        txt += f", on ${npm['volume'] / 1e3:,.0f}k lifetime volume" + (f" (Kalshi: {nk['volume'] / 1e3:,.0f}k contracts)." if nk else '.')
        findings.append(txt)
    else:
        imp_last = None
    if resolved:
        pk = [c['peak'] for c in resolved]; yes = sum(c['result'] == 'yes' for c in resolved)
        outcome = f"all {len(resolved)} resolved contracts settled No" if yes == 0 else f"{yes} of {len(resolved)} resolved contracts settled Yes"
        verdict = 'worse than' if brier_pooled > brier_base else 'better than'
        yrs = sorted({c['year'] for c in resolved})
        findings.append(f"<b>Track record:</b> {outcome} after peaking at {min(pk):.0f}&ndash;{max(pk):.0f}%. Their pooled weekly Brier score is "
                        f"{brier_pooled:.3f}, against {brier_base:.4f} for a flat 15% base rate, so the markets scored {verdict} the base rate. "
                        f"{len(yrs)} calendar years of outcomes ({yrs[0]}&ndash;{yrs[-1]}) makes this descriptive rather than a calibration test.")
    ref = nk or ck
    if ref is not None:
        sp = '' if pd.isna(ny_last.spread) else f"10y&ndash;3m spread {ny_last.spread:+.2f}pp, "
        findings.append(f"<b>Yield curve:</b> the NY Fed probit ({sp}{ny_last.origin.strftime('%b %Y')}) gives {ny_last.prob * 100:.0f}% "
                        f"for recession 12 months out, {'below' if ny_last.prob * 100 < ref['last'] else 'above'} the {fmtp(ref['last'])} Kalshi prices for {ref['year']}.")

    def ds(t):
        return str(pd.Timestamp(t).date())
    payload = dict(
        asof=ds(asof), generated=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        baseRate=BASE_RATE * 100, brierPooled=brier_pooled, brierBase=brier_base, pastYears=PAST_YEARS,
        contracts=out_contracts, findings=findings, same=same, implied=imp_last, stale=stale,
        weekly=dict(t=[ds(t) for t in Dw.index], s={k: arr(Dw[k]) for k in keys},
                    imp={y: arr(v) for y, v in imp_w.items()}, gap={y: arr(v) for y, v in gap_w.items()}),
        daily=dict(t=[ds(t) for t in Dd.index], s={k: arr(Dd[k]) for k in keys},
                   imp={y: arr(v) for y, v in imp_d.items()}, gap={y: arr(v) for y, v in gap_d.items()}),
        life=dict(x=offs, s=life),
        monthly=dict(t=[ds(m_) for m_ in months], front=front, next=nxt, nyfed=nyp,
                     nyLast=dict(origin=ds(ny_last.origin), prob=r1(ny_last.prob * 100, 1),
                                 spread=None if pd.isna(ny_last.spread) else r1(ny_last.spread))),
        events=[dict(d=d, label=l) for d, l in EVENTS],
    )
    csv = Dw.copy()
    csv.columns = [f"{REG[k]['year']}_{REG[k]['venue'].lower()}" for k in keys]
    for y, v in imp_w.items():
        csv[f'{y}_polymarket_{y}_only'] = v
    csv.index.name = 'week_ending'
    return payload, csv.round(2)


# ============================================================================ checks + output
def sanity_gate(payload, html):
    """Problems that should block a deploy. Returns a list of strings (empty = pass)."""
    p = []
    today = asof_date(time.time())
    asof = pd.Timestamp(payload['asof']).date()
    if (today - asof).days > 4:
        p.append(f'live data is stale: latest observation {asof}, today {today}')
    live = [c for c in payload['contracts'] if c['result'] == 'open']
    if not any(c['venue'] == 'Kalshi' for c in live):
        p.append('no live Kalshi contract')
    for c in live:   # actual last observation, not the forward-filled series end
        if (asof - pd.Timestamp(c['lastObs']).date()).days > 6:
            p.append(f"{c['key']} last real observation {c['lastObs']} lags as-of {asof} (source down or delisted?)")
    for k, vals in payload['daily']['s'].items():
        bad = [v for v in vals if v is not None and not (0 <= v <= 100)]
        if bad:
            p.append(f'{k} has out-of-range values, e.g. {bad[0]}')
    if payload['monthly']['nyLast']['prob'] is None:
        p.append('NY Fed probability missing')
    if len(html) < 100_000:
        p.append(f'HTML unexpectedly small ({len(html)} bytes)')
    return p


def render(payload):
    blob = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).replace('</', '<\\/')
    html, n = re.subn(r'__DATA_JSON__', lambda m: blob, TEMPLATE)
    assert n == 1, 'data placeholder not found'
    return html


def main():
    ap = argparse.ArgumentParser(description='Rebuild the US Recession Odds Monitor')
    ap.add_argument('outdir', nargs='?', default='.')
    ap.add_argument('--cache', default='cache')
    ap.add_argument('--skip-gate', action='store_true')
    a = ap.parse_args()
    registry, contracts, ny_rows, stale = gather(a.cache)
    payload, csv = build(registry, contracts, ny_rows, stale)
    html = render(payload)
    problems = sanity_gate(payload, html)
    for s in stale:
        log(f"WARNING stale source: {s['source']} (cached through {s['since']}): {s['error']}")
    if problems:
        log('SANITY GATE FAILED:\n  - ' + '\n  - '.join(problems))
        if not a.skip_gate:
            sys.exit(2)
    os.makedirs(a.outdir, exist_ok=True)
    out = os.path.join(a.outdir, 'index.html')
    open(out, 'w', encoding='utf-8').write(html)
    csv.to_csv(os.path.join(a.outdir, 'us_recession_odds_weekly.csv'))
    log(f"Wrote {out} ({len(html) / 1024:.0f} KB), as of {payload['asof']}, "
        f"{len(payload['contracts'])} contracts, {len(stale)} stale source(s)")


TEMPLATE = r'''<title>US Recession Odds Monitor</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600&display=swap">
<style>
/* Layout: research-note masthead -> KPI strip -> readout -> hero chart -> paired analytic charts -> tables -> method.
   Colour: Acheron house palette by contract role: r0-r2 the three prior years (slate, ochre, plum), r3 current year (orange),
   r4 next year (teal). Roles roll on 1 January, so the validated adjacent order never changes;
   venue is encoded by line style (Kalshi solid, Polymarket dashed). */
:root{
  --paper:#F7F1E6; --card:#FDFAF3; --ink:#221C14; --ink2:#5B5245; --muted:#857B6C;
  --rule:#E4DACA; --rule-strong:#B9AD99; --grid:#EEE6D8; --shade:rgba(34,28,20,.035); --sel:#221C14; --selfg:#FDFAF3;
  --r0:#4E6577; --r1:#A67A22; --r2:#7A4A66; --r3:#D2622A; --r4:#0E756C;
  --display:"Space Grotesk","IBM Plex Sans",system-ui,sans-serif;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --paper:#16120D; --card:#1F1A14; --ink:#F0E8DA; --ink2:#C3B8A6; --muted:#918675;
  --rule:#353026; --rule-strong:#5E5546; --grid:#2A251D; --shade:rgba(240,232,218,.035); --sel:#F0E8DA; --selfg:#1F1A14;
  --r0:#9AB0C4; --r1:#D9A93E; --r2:#B98AB8; --r3:#F47E4A; --r4:#2FBFAE; color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#16120D; --card:#1F1A14; --ink:#F0E8DA; --ink2:#C3B8A6; --muted:#918675;
  --rule:#353026; --rule-strong:#5E5546; --grid:#2A251D; --shade:rgba(240,232,218,.035); --sel:#F0E8DA; --selfg:#1F1A14;
  --r0:#9AB0C4; --r1:#D9A93E; --r2:#B98AB8; --r3:#F47E4A; --r4:#2FBFAE; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font:400 15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:1260px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:28px 64px;display:flex;flex-direction:column;gap:22px}
a{color:inherit;text-decoration-color:var(--rule-strong);text-underline-offset:2px}
a:hover{text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--c2026);outline-offset:2px}
.num,.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.eyebrow{font:500 11.5px/1.3 var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--ink2)}
h1{font:600 clamp(30px,4.2vw,46px)/1.02 var(--display);letter-spacing:-.02em;margin:6px 0 0;text-wrap:balance}
h2{font:600 19px/1.2 var(--display);letter-spacing:-.01em;margin:0;text-wrap:balance}
h3{font:600 15px/1.25 var(--display);margin:0}
p{margin:0}
.masthead{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px 28px;align-items:end;border-bottom:1px solid var(--rule-strong);padding-bottom:18px}
.dek{color:var(--ink2);max-width:70ch;margin-top:10px}
.mast-meta{display:flex;flex-direction:column;gap:8px;align-items:flex-end;text-align:right}
.asof{font:500 13px/1.3 var(--mono);color:var(--ink2)}
.asof b{color:var(--ink);font-weight:500}
@media (max-width:760px){.masthead{grid-template-columns:1fr}.mast-meta{align-items:flex-start;text-align:left}}

.seg{display:inline-flex;border:1px solid var(--rule-strong);border-radius:3px;overflow:hidden;flex-wrap:nowrap}
.seg button{appearance:none;border:0;background:transparent;color:var(--ink2);font:500 12px/1 var(--mono);padding:7px 10px;cursor:pointer;letter-spacing:.02em}
.seg button+button{border-left:1px solid var(--rule)}
.seg button:hover{color:var(--ink);background:var(--shade)}
.seg button[aria-pressed="true"]{background:var(--sel);color:var(--selfg)}
.ctl{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.ctl-label{font:500 11px/1 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.07em}

.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
@media (max-width:1000px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:520px){.kpis{grid-template-columns:1fr}}
.kpi{background:var(--card);border:1px solid var(--rule);border-radius:4px;padding:16px 18px 15px;display:flex;flex-direction:column;gap:10px;min-width:0}
.kpi-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.kpi-head .sw{width:22px;height:3px;border-radius:2px;flex:none;align-self:center}
.vals{display:flex;gap:22px;flex-wrap:wrap}
.val{display:flex;flex-direction:column;gap:2px;min-width:0}
.val .v{font:500 30px/1 var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.val .vl{font:500 11px/1.2 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.kpi-foot{font-size:12.5px;color:var(--ink2);line-height:1.45;border-top:1px solid var(--rule);padding-top:9px}
.kpi-foot .num{color:var(--ink)}
.minibars{display:flex;flex-direction:column;gap:5px}
.mb{display:grid;grid-template-columns:42px minmax(0,1fr) 40px;gap:8px;align-items:center;font:500 12px/1 var(--mono)}
.mb .track{height:8px;background:var(--grid);border-radius:2px;overflow:hidden}
.mb .fill{height:100%;border-radius:2px}
.mb .yr{color:var(--ink2)} .mb .pv{text-align:right;font-variant-numeric:tabular-nums}
.mb.now .yr,.mb.now .pv{color:var(--ink);font-weight:600}

.readout{display:grid;grid-template-columns:200px minmax(0,1fr);gap:18px 28px;padding:4px 0}
@media (max-width:760px){.readout{grid-template-columns:1fr}}
.readout ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;max-width:96ch}
.readout li{position:relative;padding-left:18px;color:var(--ink2)}
.readout li::before{content:"";position:absolute;left:0;top:.62em;width:7px;height:7px;background:var(--ink2);border-radius:1px}
.readout li b{color:var(--ink);font-weight:600}

.card{background:var(--card);border:1px solid var(--rule);border-radius:4px;padding:18px 20px 16px;min-width:0;display:flex;flex-direction:column;gap:12px}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px 18px;flex-wrap:wrap}
.card-head .sub{color:var(--ink2);font-size:13px;margin-top:4px;max-width:72ch}
.chart-box{position:relative;height:430px;width:100%}
.chart-box.sm{height:320px}
@media (max-width:640px){.chart-box{height:320px}.chart-box.sm{height:280px}}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
@media (max-width:980px){.grid2{grid-template-columns:1fr}}
.legend{display:flex;gap:6px 16px;flex-wrap:wrap;font:500 12px/1.2 var(--mono);color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.lg-line{display:inline-block;width:20px;height:0;border-top:2.25px solid currentColor}
.lg-line.dash{border-top-style:dashed}
.lg-line.dot{border-top-style:dotted}
.events{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 18px;font-size:12.5px;color:var(--ink2);border-top:1px solid var(--rule);padding-top:10px}
.events div{display:flex;gap:8px;align-items:baseline}
.events .n{flex:none;width:17px;height:17px;border-radius:50%;border:1px solid var(--rule-strong);font:500 10px/15px var(--mono);text-align:center;color:var(--ink)}
.events .d{font-family:var(--mono);font-size:11.5px;color:var(--muted);white-space:nowrap}
.stale{border:1px solid var(--r3);border-radius:4px;padding:10px 14px;font-size:13px;color:var(--ink);background:var(--card)}
.stale b{font-weight:600}
.note{font-size:12.5px;color:var(--muted);line-height:1.5}

.tbl-wrap{overflow-x:auto;border:1px solid var(--rule);border-radius:3px}
.tbl-wrap.tall{max-height:460px;overflow-y:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--rule);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{font:500 11px/1.25 var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:var(--card);position:sticky;top:0;z-index:1;vertical-align:bottom}
tbody td{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
tbody td.txt{font-family:var(--sans);white-space:normal;text-align:left;color:var(--ink2);min-width:260px;max-width:440px}
tbody tr:hover td{background:var(--shade)}
tfoot td{font-family:var(--mono);color:var(--ink2);border-bottom:0;background:var(--shade)}
td.na{color:var(--muted)}
.pill{display:inline-block;font:500 10.5px/1 var(--mono);text-transform:uppercase;letter-spacing:.06em;padding:4px 6px;border-radius:2px;border:1px solid var(--rule-strong);color:var(--ink2)}
.pill.open{border-color:currentColor;color:var(--ink)}
.th-sw{display:inline-block;width:12px;height:3px;border-radius:1px;margin-right:5px;vertical-align:middle}
.btn{appearance:none;border:1px solid var(--rule-strong);background:transparent;color:var(--ink);font:500 12px/1 var(--mono);padding:7px 11px;border-radius:3px;cursor:pointer}
.btn:hover{background:var(--shade)}
.method{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 32px;font-size:13.5px;color:var(--ink2)}
@media (max-width:860px){.method{grid-template-columns:1fr}}
.method p b{color:var(--ink);font-weight:600}
footer{font-size:12px;color:var(--muted);border-top:1px solid var(--rule);padding-top:14px;display:flex;flex-wrap:wrap;gap:6px 18px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="wrap">
  <header class="masthead">
    <div>
      <div class="eyebrow">Prediction-market monitor &middot; Kalshi + Polymarket &middot; weekly</div>
      <h1>US Recession Odds</h1>
      <p class="dek">Every US calendar-year recession contract on Kalshi and Polymarket since 2023, sampled at Friday close. Each line is a traded probability, and each contract defines &ldquo;recession&rdquo; its own way, so the rules sit beside the prices.</p>
    </div>
    <div class="mast-meta">
      <div class="asof">Data as of <b id="asof">&ndash;</b></div>
      <div class="ctl"><span class="ctl-label">Sampling</span>
        <div class="seg" id="seg-freq" role="group" aria-label="Sampling frequency">
          <button type="button" id="freq-weekly" data-v="weekly">Weekly</button><button type="button" id="freq-daily" data-v="daily">Daily</button>
        </div>
      </div>
    </div>
  </header>

  <div class="stale" id="stale" role="status" hidden></div>

  <section class="kpis" id="kpis" aria-label="Headline readings"></section>

  <section class="readout" aria-label="Readout">
    <div><div class="eyebrow">Readout</div><p class="note" style="margin-top:6px">Generated from the data at build time.</p></div>
    <ul id="findings"></ul>
  </section>

  <section class="card" aria-labelledby="h-all">
    <div class="card-head">
      <div>
        <h2 id="h-all">Every calendar-year contract</h2>
        <p class="sub">Probability that the US meets each contract's recession test in that year. Past-year contracts decay to zero as their window runs out. Colours follow each contract's role: orange is the current year, teal next year, and the three prior years in slate, ochre and plum.</p>
      </div>
      <div class="ctl">
        <div class="seg" id="seg-venue" role="group" aria-label="Venue">
          <button type="button" id="venue-both" data-v="both">Both</button><button type="button" id="venue-k" data-v="Kalshi">Kalshi</button><button type="button" id="venue-p" data-v="Polymarket">Polymarket</button>
        </div>
        <div class="seg" id="seg-range" role="group" aria-label="Date range">
          <button type="button" id="range-all" data-v="all">All</button><button type="button" id="range-2y" data-v="2y">2Y</button><button type="button" id="range-1y" data-v="1y">1Y</button>
        </div>
      </div>
    </div>
    <div class="legend" id="legend-main"></div>
    <div class="chart-box"><canvas id="ch-all" role="img" aria-label="Line chart of recession probability for each calendar-year contract on Kalshi and Polymarket since November 2022"></canvas></div>
    <div class="events" id="events"></div>
  </section>

  <div class="grid2">
    <section class="card" aria-labelledby="h-life">
      <div class="card-head">
        <div>
          <h2 id="h-life">Same point in each contract's life</h2>
          <p class="sub">Days relative to 1 January of the contract year. Left of zero the contract is &ldquo;next year&rdquo;; right of zero it is the current year. Vertical marks show where today falls.</p>
        </div>
        <div class="seg" id="seg-life" role="group" aria-label="Venue for life-cycle chart">
          <button type="button" id="life-k" data-v="Kalshi">Kalshi</button><button type="button" id="life-p" data-v="Polymarket">Polymarket</button>
        </div>
      </div>
      <div class="legend" id="legend-life"></div>
      <div class="chart-box sm"><canvas id="ch-life" role="img" aria-label="Contract life-cycle chart: each year's contract plotted against days relative to the start of its year"></canvas></div>
    </section>

    <section class="card" aria-labelledby="h-cn">
      <div class="card-head">
        <div>
          <h2 id="h-cn">Current year vs next year</h2>
          <p class="sub">Both venues since the current-year contract listed. The dotted line nets the cumulative Polymarket contract down to next year alone.</p>
        </div>
      </div>
      <div class="legend" id="legend-cn"></div>
      <div class="chart-box sm"><canvas id="ch-cn" role="img" aria-label="Current-year and next-year recession odds on both venues"></canvas></div>
    </section>
  </div>

  <div class="grid2">
    <section class="card" aria-labelledby="h-gap">
      <div class="card-head">
        <div>
          <h2 id="h-gap">Venue gap: Polymarket minus Kalshi</h2>
          <p class="sub">Same calendar year, percentage points. Polymarket also pays on an NBER declaration, so a positive gap is the expected sign.</p>
        </div>
      </div>
      <div class="legend" id="legend-gap"></div>
      <div class="chart-box sm"><canvas id="ch-gap" role="img" aria-label="Spread between Polymarket and Kalshi odds for the 2024, 2025 and 2026 contracts"></canvas></div>
    </section>

    <section class="card" aria-labelledby="h-ny">
      <div class="card-head">
        <div>
          <h2 id="h-ny">Markets vs the yield-curve model</h2>
          <p class="sub">Month-end Kalshi prices against the NY Fed probit (10y&ndash;3m spread), dated at the month the forecast was made. The model gives the odds of being in recession 12 months later.</p>
        </div>
      </div>
      <div class="legend" id="legend-ny"></div>
      <div class="chart-box sm"><canvas id="ch-ny" role="img" aria-label="Kalshi current and next-year contract prices against the NY Fed yield-curve recession probability"></canvas></div>
    </section>
  </div>

  <section class="card" aria-labelledby="h-track">
    <div class="card-head"><div><h2 id="h-track">Track record of resolved contracts</h2>
      <p class="sub">Brier score is the mean squared error of the weekly price against the outcome (0 = perfect). The base-rate forecaster says <span id="base-rate">15%</span> every week, roughly the post-1945 frequency of NBER recession starts per calendar year.</p></div></div>
    <div class="tbl-wrap"><table id="tbl-track"></table></div>
  </section>

  <section class="card" aria-labelledby="h-weekly">
    <div class="card-head">
      <div><h2 id="h-weekly">Weekly data</h2><p class="sub">Friday close, newest first, in percent. The latest row is the partial current week.</p></div>
      <button class="btn" type="button" id="copy-csv">Copy as CSV</button>
    </div>
    <div class="tbl-wrap tall"><table id="tbl-weekly"></table></div>
    <textarea id="csv-fallback" hidden readonly style="width:100%;height:120px;font:12px var(--mono);background:var(--card);color:var(--ink);border:1px solid var(--rule)"></textarea>
  </section>

  <section class="card" aria-labelledby="h-spec">
    <div class="card-head"><div><h2 id="h-spec">Contracts and resolution rules</h2>
      <p class="sub">What each line measures. Kalshi uses only the two-quarter GDP rule; Polymarket also resolves Yes on an NBER announcement.</p></div></div>
    <div class="tbl-wrap"><table id="tbl-spec"></table></div>
  </section>

  <section class="card" aria-labelledby="h-method">
    <h2 id="h-method">Method and caveats</h2>
    <div class="method">
      <p><b>Prices.</b> Kalshi: daily last-trade close from the trade API candlesticks (settled contracts from the historical endpoint), forward-filled across no-trade days. Polymarket: daily CLOB price history for the Yes token. Both are mapped to the US trading day they describe, then sampled at the last observation of each Friday-ending week.</p>
      <p><b>Not one series.</b> Each year is a separate contract with its own window. The 2024&ndash;2026 Kalshi contracts span two calendar years (e.g. &ldquo;2025 or 2026&rdquo;) but only the later year is live once the earlier one has printed clean. Kalshi 2027 starts at Q4'26.</p>
      <p><b>Time decay.</b> A current-year contract falls toward zero as quarters print without a contraction, whatever the outlook. Read it against the same date in prior years (life-cycle chart), not against its own spring level.</p>
      <p><b>Cumulative Polymarket contracts.</b> The &ldquo;by end of&rdquo; contracts pay if the rule is met at any point in a window starting Q2'25, so each sits at or above the previous year's. The single-year figure is (P<sub>Y</sub> &minus; P<sub>Y&minus;1</sub>) / (1 &minus; P<sub>Y&minus;1</sub>). The newest contract trades thinly; treat single-week moves with caution.</p>
      <p><b>Yield-curve model.</b> NY Fed probit on the 10y&ndash;3m spread, re-dated from target month to forecast month. It estimates P(in recession at t+12), a different quantity from &ldquo;a two-quarter contraction during year Y&rdquo;; the comparison is directional.</p>
      <p><b>Track record.</b> <span id="track-span">Few</span> calendar years have resolved, with heavily overlapping information across venues. The Brier comparison is descriptive, not a test. Early-life prices on thin books (Kalshi 2024 listed at 60%) inflate the scores.</p>
    </div>
  </section>

  <footer>
    <span>Sources: <a href="https://kalshi.com/markets/kxrecssnber" target="_blank" rel="noopener">Kalshi</a> trade API &middot; <a href="https://polymarket.com/event/us-recession-by-end-of-2026" target="_blank" rel="noopener">Polymarket</a> gamma + CLOB APIs &middot; <a href="https://www.newyorkfed.org/research/capital_markets/ycfaq" target="_blank" rel="noopener">NY Fed yield-curve model</a></span>
    <span id="built"></span>
    <span>Refreshed daily by GitHub Actions &middot; rebuild.py</span>
  </footer>
</div>

<script type="application/json" id="payload">__DATA_JSON__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
(function(){
'use strict';
const D = JSON.parse(document.getElementById('payload').textContent);
const MS = s => Date.parse(s + 'T00:00:00Z');
const DAY = 864e5;
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const tok = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const YT = y => { const s = y - D.same.curYear + D.pastYears; return s >= 0 && s <= D.pastYears + 1 ? '--r' + s : '--muted'; };
const CBY = Object.fromEntries(D.contracts.map(c => [c.key, c]));
const KEYS = D.contracts.map(c => c.key);
const WIN = D.contracts.filter(c => c.slot >= 0).map(c => c.key);
const CY = D.same.curYear, NY = D.same.nextYear, yy = y => String(y).slice(2);
const ASOF = MS(D.asof);
['weekly','daily'].forEach(g => { D[g].ms = D[g].t.map(MS); });
D.monthly.ms = D.monthly.t.map(MS);

const state = {freq:'weekly', venue:'both', range:'all', life:'Kalshi'};
try { Object.assign(state, JSON.parse(localStorage.getItem('rec-odds-v1') || '{}')); } catch (e) {}
const save = () => { try { localStorage.setItem('rec-odds-v1', JSON.stringify(state)); } catch (e) {} };

const fmt = v => v == null ? '–' : (Math.abs(v) < 10 ? v.toFixed(1) : v.toFixed(0)) + '%';
const fmtPP = v => v == null ? '–' : (v > 0 ? '+' : v < 0 ? '−' : '±') + Math.abs(v).toFixed(1) + 'pp';
const fdate = ms => { const d = new Date(ms); return d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear(); };
const fshort = s => { const d = new Date(MS(s)); return d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + " '" + String(d.getUTCFullYear()).slice(2); };
const ftick = ms => { const d = new Date(ms); return MON[d.getUTCMonth()] + " '" + String(d.getUTCFullYear()).slice(2); };
const venueShort = v => v === 'Kalshi' ? 'K' : 'PM';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------- static text ---------- */
document.getElementById('asof').textContent = fdate(ASOF);
document.getElementById('built').textContent = 'Built ' + D.generated;
document.getElementById('base-rate').textContent = D.baseRate.toFixed(0) + '%';
(() => { const r = D.contracts.filter(c => c.result !== 'open'); if (!r.length) return;
  const yrs = [...new Set(r.map(c => c.year))].sort(), by = v => r.filter(c => c.venue === v).map(c => c.year);
  const span = a => a.length ? (a.length > 1 ? Math.min(...a) + '&ndash;' + Math.max(...a) : String(a[0])) : null;
  const parts = ['Kalshi', 'Polymarket'].map(v => span(by(v)) ? span(by(v)) + ' on ' + v : null).filter(Boolean);
  document.getElementById('track-span').innerHTML = yrs.length + ' (' + parts.join(', ') + '; ' + r.length + ' contracts, ' +
    (r.every(c => c.result === 'no') ? 'all No' : r.filter(c => c.result === 'yes').length + ' Yes') + ')'; })();
document.getElementById('findings').innerHTML = D.findings.map(f => '<li>' + f + '</li>').join('');
document.getElementById('events').innerHTML = D.events.map((e, i) =>
  '<div><span class="n">' + (i + 1) + '</span><span><span class="d">' + fdate(MS(e.d)) + '</span> &nbsp;' + esc(e.label) + '</span></div>').join('');

/* ---------- stale-source notice ---------- */
if (D.stale && D.stale.length) {
  const el = document.getElementById('stale');
  el.innerHTML = '<b>Some sources could not be refreshed on the last run.</b> Showing cached data for: ' +
    D.stale.map(x => esc(x.source) + ' (through ' + esc(String(x.since).slice(0, 10)) + ')').join(', ') + '.';
  el.hidden = false;
}

/* ---------- KPI tiles ---------- */
function kpis(){
  const k = CBY['K' + yy(CY)], p = CBY['P' + yy(CY)], kn = CBY['K' + yy(NY)], pn = CBY['P' + yy(NY)];
  const live = c => c && c.result === 'open';
  const nyL = D.monthly.nyLast;
  const prior = [];
  for (let y = CY - D.pastYears; y <= NY; y++) {
    const key = 'K' + yy(y);
    const v = y === NY ? (kn ? kn.last : null) : (CBY[key] ? D.same.nxt[key] : null);
    if (v != null || y === NY) prior.push({y, v, now: y === NY});
  }
  let cmpTitle = 'Next-year odds, same date', cmpFoot = 'Kalshi next-year contract ' + Math.abs(D.same.nextOff) + ' days before its year began';
  if (prior.filter(r => !r.now && r.v != null).length === 0) {      // early in the year: nothing listed that far ahead
    prior.length = 0;
    for (let y = CY - D.pastYears; y <= CY; y++) {
      const v = CBY['K' + yy(y)] ? D.same.cur['K' + yy(y)] : null;
      if (v != null) prior.push({y, v, now: y === CY});
    }
    cmpTitle = 'Current-year odds, same date'; cmpFoot = 'Kalshi current-year contract ' + D.same.curOff + ' days into its year';
  }
  const mx = Math.max(...prior.map(r => r.v || 0), 1);
  const nyO = new Date(MS(nyL.origin)), nyT = new Date(Date.UTC(nyO.getUTCFullYear() + 1, nyO.getUTCMonth(), 1));
  const tile = (title, swYear, body, foot) =>
    '<article class="kpi"><div class="kpi-head"><span class="eyebrow">' + title + '</span>' +
    (swYear ? '<span class="sw" style="background:var(' + YT(swYear) + ')"></span>' : '') + '</div>' + body +
    '<div class="kpi-foot">' + foot + '</div></article>';
  const val = (v, l) => '<div class="val"><span class="v">' + fmt(v) + '</span><span class="vl">' + l + '</span></div>';
  const n = v => '<span class="num">' + v + '</span>';
  let t1 = '<div class="vals">' + (k ? val(k.last, 'Kalshi') : '') + (live(p) ? val(p.last, 'Polymarket') : '') + '</div>';
  let f1 = k ? '4-wk change ' + n(fmtPP(k.d4w)) + ' K' + (live(p) ? ' / ' + n(fmtPP(p.d4w)) + ' PM' : '') + '<br>' +
    'Year high ' + n(fmt(k.ytdHi)) + ' K (' + fshort(k.ytdHiDate) + ')' + (live(p) && p.ytdHi != null ? ' &middot; ' + n(fmt(p.ytdHi)) + ' PM' : '') : 'No current-year Kalshi contract';
  const impNY = D.implied;
  let t2, f2;
  if (kn || pn) {
    t2 = '<div class="vals">' + (kn ? val(kn.last, 'Kalshi') : '') +
      (impNY != null ? val(impNY, 'PM, ' + NY + ' only') : (pn ? val(pn.last, 'Polymarket' + (pn.cumulative ? ' cum.' : '')) : '')) + '</div>';
    f2 = (kn ? '4-wk change ' + n(fmtPP(kn.d4w)) + ' K' : 'Kalshi ' + NY + ' not listed yet') +
      (pn && impNY != null ? ' &middot; PM cumulative ' + n(fmt(pn.last)) : '') + '<br>' +
      (kn ? 'Kalshi high since listing ' + n(fmt(kn.peak)) + ' (' + fshort(kn.peakDate) + ')' : 'Polymarket listed ' + fshort(pn.open));
  } else {
    t2 = '<div class="vals"><div class="val"><span class="v" style="font-size:22px">Not listed</span><span class="vl">Kalshi or Polymarket</span></div></div>';
    f2 = 'The ' + CY + ' Kalshi contract listed ' + (k ? fshort(k.open) : '&ndash;') + '; the ' + NY + ' contract should follow.';
  }
  document.getElementById('kpis').innerHTML =
    tile(CY + ' &middot; current year', CY, t1, f1) +
    tile(NY + ' &middot; next year', NY, t2, f2) +
    tile(cmpTitle, null,
      '<div class="minibars">' + prior.map(r =>
        '<div class="mb' + (r.now ? ' now' : '') + '"><span class="yr">' + r.y + '</span><span class="track"><span class="fill" style="display:block;width:' +
        ((r.v || 0) / mx * 100).toFixed(1) + '%;background:var(' + YT(r.y) + ')"></span></span><span class="pv">' + (r.v == null ? 'n/l' : fmt(r.v)) + '</span></div>').join('') + '</div>',
      cmpFoot) +
    tile('NY Fed yield-curve model', null,
      '<div class="vals">' + val(nyL.prob, 'P(recession) ' + MON[nyT.getUTCMonth()] + " '" + String(nyT.getUTCFullYear()).slice(2)) + '</div>',
      (nyL.spread != null ? '10y&ndash;3m spread ' + n((nyL.spread >= 0 ? '+' : '') + nyL.spread.toFixed(2) + 'pp') + ' in ' : 'Forecast made ') +
      MON[nyO.getUTCMonth()] + ' ' + nyO.getUTCFullYear() +
      ((kn || k) ? '<br>vs Kalshi ' + (kn || k).year + ' at ' + n(fmt((kn || k).last)) : ''));
}

/* ---------- chart plumbing ---------- */
const charts = {};
function mount(id, cfg){ if (charts[id]) charts[id].destroy(); charts[id] = new Chart(document.getElementById(id), cfg); }

function timeTicks(min, max){
  const span = (max - min) / DAY, step = span > 1100 ? 6 : span > 520 ? 3 : span > 220 ? 2 : 1, out = [];
  const d = new Date(min); let y = d.getUTCFullYear(), m = d.getUTCMonth();
  for (let i = 0; i < 400; i++) { const t = Date.UTC(y, m, 1); if (t > max) break; if (t >= min && m % step === 0) out.push({value: t}); if (++m > 11) { m = 0; y++; } }
  return out;
}
function edgePad(){ return window.innerWidth < 560 ? 100 : 112; }
function niceMax(v){ return Math.min(100, Math.max(10, Math.ceil((v + 6) / 10) * 10)); }

function tooltipCfg(labelFn){
  return {
    backgroundColor: tok('--card'), borderColor: tok('--rule-strong'), borderWidth: 1, cornerRadius: 3, padding: 10,
    titleColor: tok('--ink'), bodyColor: tok('--ink'), titleFont: {family: 'IBM Plex Sans', weight: '600', size: 12},
    bodyFont: {family: 'IBM Plex Mono', size: 11.5}, boxWidth: 14, boxHeight: 3, boxPadding: 5,
    filter: it => it.raw && it.raw.y != null,
    itemSort: (a, b) => b.raw.y - a.raw.y,
    callbacks: {
      title: items => items.length ? fdate(items[0].raw.x) : '',
      label: labelFn || (it => ' ' + it.dataset.label + '   ' + fmt(it.raw.y)),
      labelColor: it => ({borderColor: it.dataset.borderColor, backgroundColor: it.dataset.borderColor, borderWidth: 0})
    }
  };
}

function baseOpts(o){
  const grid = tok('--grid'), rule = tok('--rule-strong'), muted = tok('--muted'), ink2 = tok('--ink2');
  return {
    responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
    interaction: {mode: 'index', intersect: false},
    layout: {padding: {top: o.padTop ?? 26, right: o.padRight ?? 10, left: 0, bottom: 0}},
    plugins: Object.assign({legend: {display: false}, tooltip: tooltipCfg(o.label)}, o.plugins || {}),
    scales: {
      x: {type: 'linear', min: o.xmin, max: o.xmax, offset: false,
          grid: {color: grid, drawTicks: false}, border: {color: rule},
          ticks: {color: muted, font: {family: 'IBM Plex Mono', size: 11}, maxRotation: 0, autoSkipPadding: 16, padding: 7,
                  callback: o.xtick || ftick},
          afterBuildTicks: o.xticks || (sc => { sc.ticks = timeTicks(sc.min, sc.max); })},
      y: {min: o.ymin ?? 0, max: o.ymax, grid: {color: o.ygrid || grid, drawTicks: false}, border: {display: false},
          ticks: {color: muted, font: {family: 'IBM Plex Mono', size: 11}, padding: 8, maxTicksLimit: 7,
                  callback: o.yfmt || (v => v + '%')}}
    }
  };
}

const crosshair = {id: 'crosshair', afterDatasetsDraw(ch){
  const act = ch.tooltip && ch.tooltip.getActiveElements(); if (!act || !act.length) return;
  const x = act[0].element.x, a = ch.chartArea, ctx = ch.ctx;
  ctx.save(); ctx.strokeStyle = tok('--rule-strong'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom); ctx.stroke(); ctx.restore();
}};

const eventsPlugin = {id: 'events',
  beforeDatasetsDraw(ch, args, o){
    if (!o || !o.on) return; const a = ch.chartArea, x = ch.scales.x, ctx = ch.ctx;
    ctx.save(); ctx.strokeStyle = tok('--rule-strong'); ctx.globalAlpha = .55; ctx.lineWidth = 1;
    D.events.forEach(e => { const px = x.getPixelForValue(MS(e.d)); if (px < a.left || px > a.right) return;
      ctx.beginPath(); ctx.moveTo(px, a.top - 4); ctx.lineTo(px, a.bottom); ctx.stroke(); });
    ctx.restore();
  },
  afterDatasetsDraw(ch, args, o){
    if (!o || !o.on) return; const a = ch.chartArea, x = ch.scales.x, ctx = ch.ctx;
    let lastPx = -99, row = 0;
    ctx.save(); ctx.font = '500 10px "IBM Plex Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    D.events.forEach((e, i) => {
      const px = x.getPixelForValue(MS(e.d)); if (px < a.left || px > a.right) return;
      row = (px - lastPx < 19) ? 1 - row : 0; lastPx = px;
      const cy = a.top - 13 + row * 20;
      ctx.beginPath(); ctx.arc(px, cy, 8, 0, Math.PI * 2); ctx.fillStyle = tok('--card'); ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = tok('--rule-strong'); ctx.stroke();
      ctx.fillStyle = tok('--ink'); ctx.fillText(String(i + 1), px, cy + .5);
    });
    ctx.restore();
  }
};

const labelsPlugin = {id: 'endLabels', afterDatasetsDraw(ch, args, o){
  if (!o || !o.on) return; const a = ch.chartArea, ctx = ch.ctx, right = [];
  ctx.save(); ctx.font = '500 10.5px "IBM Plex Mono"'; ctx.textBaseline = 'middle';
  ch.data.datasets.forEach((ds, i) => {
    if (!ds._lab || !ch.isDatasetVisible(i)) return;
    const meta = ch.getDatasetMeta(i); let last = -1, pk = -1, pkv = -1;
    for (let j = 0; j < ds.data.length; j++) {
      const r = ds.data[j]; if (r.y == null) continue; const el = meta.data[j];
      if (!el || el.x < a.left - .5 || el.x > a.right + .5) continue;
      last = j; if (r.y > pkv) { pkv = r.y; pk = j; }
    }
    if (last < 0) return;
    const el = meta.data[last];
    if (ds._lab.edge && el.x > a.right - 6) right.push({y: el.y, text: ds._lab.edge + ' ' + fmt(ds.data[last].y), color: ds.borderColor, dash: ds.borderDash});
    else if (ds._lab.peak && pk >= 0 && pkv > 6) {
      const p = meta.data[pk], txt = ds._lab.peak + ' ' + fmt(pkv), w = ctx.measureText(txt).width;
      const tx = Math.min(Math.max(p.x, a.left + w / 2 + 2), a.right - w / 2 - 2);
      ctx.fillStyle = tok('--ink2'); ctx.textAlign = 'center'; ctx.fillText(txt, tx, Math.max(p.y - 10, a.top + 6));
    }
  });
  right.sort((p, q) => p.y - q.y);
  for (let i = 1; i < right.length; i++) if (right[i].y - right[i - 1].y < 13) right[i].y = right[i - 1].y + 13;
  for (let i = right.length - 1; i >= 0; i--) { const lim = i === right.length - 1 ? a.bottom - 4 : right[i + 1].y - 13; if (right[i].y > lim) right[i].y = lim; }
  right.forEach(r => {
    ctx.strokeStyle = r.color; ctx.lineWidth = 2; ctx.setLineDash(r.dash && r.dash.length ? [3, 2] : []);
    ctx.beginPath(); ctx.moveTo(a.right + 6, r.y); ctx.lineTo(a.right + 16, r.y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = tok('--ink'); ctx.textAlign = 'left'; ctx.fillText(r.text, a.right + 20, r.y);
  });
  ctx.restore();
}};
Chart.register(crosshair, eventsPlugin, labelsPlugin);

function series(grid, arr, label, color, extra){
  return Object.assign({label, data: grid.ms.map((x, i) => ({x, y: arr[i]})), borderColor: color, backgroundColor: color,
    borderWidth: 2, pointRadius: 0, pointHoverRadius: 3.5, pointHoverBorderWidth: 2, pointHoverBorderColor: tok('--card'),
    pointHoverBackgroundColor: color, tension: 0, spanGaps: false}, extra || {});
}
function contractDS(grid, key, peakLabels){
  const c = CBY[key], col = tok(YT(c.year)), open = c.result === 'open', pm = c.venue === 'Polymarket';
  const label = c.year + (pm && c.cumulative && c.year === NY ? ' PM (cum.)' : ' ' + c.venue);
  return series(grid, grid.s[key], label, col, {
    borderWidth: open ? 2.25 : 1.6, borderDash: pm ? [6, 3] : [],
    _lab: {edge: open ? c.year + ' ' + venueShort(c.venue) : null, peak: !open && peakLabels ? String(c.year) : null}});
}
function legendHTML(items){
  return items.map(it => '<span style="color:' + it.color + '"><i class="lg-line' + (it.style ? ' ' + it.style : '') + '"></i><span style="color:var(--ink2)">' + it.label + '</span></span>').join('');
}
function visibleMax(dsets, xmin, xmax){
  let m = 0; dsets.forEach(ds => ds.data.forEach(p => { if (p.y != null && p.x >= xmin && p.x <= xmax && p.y > m) m = p.y; })); return m;
}

/* ---------- 1. all contracts ---------- */
function drawAll(){
  const g = D[state.freq];
  const keys = WIN.filter(k => state.venue === 'both' || CBY[k].venue === state.venue);
  const peakVenue = state.venue === 'Polymarket' ? 'Polymarket' : 'Kalshi';
  const ds = keys.map(k => contractDS(g, k, CBY[k].venue === peakVenue));
  const xmax = ASOF, xmin = state.range === '1y' ? ASOF - 365 * DAY : state.range === '2y' ? ASOF - 730 * DAY : g.ms[0];
  const years = [...new Set(keys.map(k => CBY[k].year))].sort();
  document.getElementById('legend-main').innerHTML = legendHTML(years.map(y => ({color: tok(YT(y)), label: String(y)}))) +
    (state.venue !== 'Polymarket' ? legendHTML([{color: tok('--ink2'), label: 'Kalshi'}]) : '') +
    (state.venue !== 'Kalshi' ? legendHTML([{color: tok('--ink2'), label: 'Polymarket', style: 'dash'}]) : '');
  const o = baseOpts({xmin, xmax, ymax: niceMax(visibleMax(ds, xmin, xmax)), padRight: edgePad(),
    plugins: {events: {on: true}, endLabels: {on: true}}});
  mount('ch-all', {type: 'line', data: {datasets: ds}, options: o});
}

/* ---------- 2. life cycle ---------- */
function drawLife(){
  const x = D.life.x, keys = WIN.filter(k => CBY[k].venue === state.life && D.life.s[k]);
  const ds = keys.map(k => {
    const c = CBY[k], col = tok(YT(c.year)), open = c.result === 'open';
    return {label: String(c.year), data: x.map((o, i) => ({x: o, y: D.life.s[k][i]})), borderColor: col, backgroundColor: col,
      borderWidth: open ? 2.4 : 1.6, borderDash: state.life === 'Polymarket' ? [6, 3] : [], pointRadius: 0, pointHoverRadius: 3.5,
      pointHoverBorderWidth: 2, pointHoverBorderColor: tok('--card'), pointHoverBackgroundColor: col, tension: 0};
  });
  document.getElementById('legend-life').innerHTML = legendHTML(keys.map(k => ({color: tok(YT(CBY[k].year)), label: String(CBY[k].year) + (CBY[k].result === 'open' ? ' (live)' : ''), style: state.life === 'Polymarket' ? 'dash' : ''})));
  const TK = [[-365, "Jan Y−1"], [-273, "Apr Y−1"], [-184, "Jul Y−1"], [-92, "Oct Y−1"], [0, "1 Jan Y"], [90, "Apr Y"], [181, "Jul Y"], [273, "Oct Y"], [365, "Jan Y+1"]];
  const xmin = x[0], xmax = 400;
  const offDate = off => { const d = new Date(Date.UTC(2001, 0, 1) + off * DAY); return d.getUTCDate() + ' ' + MON[d.getUTCMonth()]; };
  const lifeMarks = {id: 'lifeMarks',
    beforeDatasetsDraw(ch){
      const a = ch.chartArea, sx = ch.scales.x, ctx = ch.ctx, z = sx.getPixelForValue(0);
      ctx.save(); ctx.fillStyle = tok('--shade'); ctx.fillRect(a.left, a.top, z - a.left, a.bottom - a.top);
      ctx.font = '500 10px "IBM Plex Mono"'; ctx.fillStyle = tok('--muted'); ctx.textBaseline = 'top';
      ctx.textAlign = 'right'; ctx.fillText('NEXT-YEAR PHASE', z - 8, a.top + 6);
      ctx.textAlign = 'left'; ctx.fillText('CURRENT-YEAR PHASE', z + 8, a.top + 6);
      ctx.strokeStyle = tok('--rule-strong'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(z, a.top); ctx.lineTo(z, a.bottom); ctx.stroke();
      ctx.restore();
    },
    afterDatasetsDraw(ch){
      const a = ch.chartArea, sx = ch.scales.x, ctx = ch.ctx;
      [[D.same.curOff, CY], [D.same.nextOff, NY]].forEach(([off, y]) => {
        const px = sx.getPixelForValue(off); if (px < a.left || px > a.right) return;
        ctx.save(); ctx.strokeStyle = tok(YT(y)); ctx.lineWidth = 1.25; ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(px, a.top - 14); ctx.lineTo(px, a.bottom); ctx.stroke(); ctx.setLineDash([]);
        ctx.font = '500 10px "IBM Plex Mono"'; ctx.fillStyle = tok('--ink'); ctx.textBaseline = 'bottom'; ctx.textAlign = 'center';
        ctx.fillText(y + ' today', Math.min(Math.max(px, a.left + 30), a.right - 30), a.top - 15); ctx.restore();
      });
    }};
  const o = baseOpts({xmin, xmax, ymax: niceMax(visibleMax(ds, xmin, xmax)), padTop: 30,
    xticks: sc => { sc.ticks = TK.filter(t => t[0] >= xmin && t[0] <= xmax).map(t => ({value: t[0]})); },
    xtick: v => { const t = TK.find(q => q[0] === v); return t ? t[1] : ''; }});
  o.plugins.tooltip.callbacks.title = items => { if (!items.length) return ''; const off = items[0].raw.x;
    return offDate(off) + (off < 0 ? ' of the prior year' : off >= 365 ? ' after year end' : ' of the contract year') + '  (' + (off > 0 ? '+' : '') + off + 'd)'; };
  o.plugins.tooltip.callbacks.label = it => { const c = it.dataset.label; const yr = +c; const d = new Date(Date.UTC(yr, 0, 1) + it.raw.x * DAY);
    return ' ' + c + '   ' + fmt(it.raw.y) + '   (' + d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear() + ')'; };
  mount('ch-life', {type: 'line', data: {datasets: ds}, options: o, plugins: [lifeMarks]});
}

/* ---------- 3. current vs next ---------- */
function drawCN(){
  const g = D[state.freq];
  const kc = 'K' + yy(CY), pc = 'P' + yy(CY), kn = 'K' + yy(NY), pn = 'P' + yy(NY);
  const have = [kc, pc, kn, pn].filter(k => CBY[k]);
  const ds = have.map(k => contractDS(g, k, false));
  const imp = g.imp[String(NY)];
  if (imp) ds.push(series(g, imp, NY + ' PM, ' + NY + ' only', tok(YT(NY)), {borderWidth: 1.6, borderDash: [1.5, 3], _lab: {edge: NY + ' PM*'}}));
  let first = Infinity; ds.forEach(d => d.data.forEach(p => { if (p.y != null && p.x < first) first = p.x; }));
  first = Math.max(first, ASOF - 540 * DAY);
  const xmin = first - 14 * DAY, xmax = ASOF;
  const lg = [];
  if (CBY[kc]) lg.push({color: tok(YT(CY)), label: CY + ' Kalshi'});
  if (CBY[pc]) lg.push({color: tok(YT(CY)), label: CY + ' Polymarket', style: 'dash'});
  if (CBY[kn]) lg.push({color: tok(YT(NY)), label: NY + ' Kalshi'});
  if (CBY[pn]) lg.push({color: tok(YT(NY)), label: NY + ' PM' + (CBY[pn].cumulative ? ' cumulative' : ''), style: 'dash'});
  if (imp) lg.push({color: tok(YT(NY)), label: '* ' + NY + ' only (PM, netted)', style: 'dot'});
  document.getElementById('legend-cn').innerHTML = legendHTML(lg);
  const o = baseOpts({xmin, xmax, ymax: niceMax(visibleMax(ds, xmin, xmax)), padRight: edgePad(),
    plugins: {events: {on: true}, endLabels: {on: true}}});
  mount('ch-cn', {type: 'line', data: {datasets: ds}, options: o});
}

/* ---------- 4. venue gap ---------- */
function drawGap(){
  const g = D[state.freq];
  const ds = Object.keys(g.gap).map(y => series(g, g.gap[y], y + ' (PM − K)', tok(YT(+y)), {borderWidth: +y === CY ? 2.25 : 1.6}));
  let first = Infinity; ds.forEach(d => d.data.forEach(p => { if (p.y != null && p.x < first) first = p.x; }));
  const xmin = first - 14 * DAY, xmax = ASOF;
  let m = 0; ds.forEach(d => d.data.forEach(p => { if (p.y != null && p.x >= xmin && Math.abs(p.y) > m) m = Math.abs(p.y); }));
  const lim = Math.max(5, Math.ceil((m + 1) / 5) * 5);
  document.getElementById('legend-gap').innerHTML = legendHTML(Object.keys(g.gap).map(y => ({color: tok(YT(+y)), label: y + ' contracts'})));
  const zero = tok('--ink2'), grid = tok('--grid');
  const o = baseOpts({xmin, xmax, ymin: -lim, ymax: lim, padTop: 8, yfmt: v => (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v) + 'pp',
    label: it => ' ' + it.dataset.label + '   ' + fmtPP(it.raw.y)});
  o.scales.y.grid.color = c => c.tick && c.tick.value === 0 ? zero : grid;
  o.plugins.tooltip.itemSort = (a, b) => b.raw.y - a.raw.y;
  mount('ch-gap', {type: 'line', data: {datasets: ds}, options: o});
}

/* ---------- 5. NY Fed vs markets ---------- */
function drawNY(){
  const g = D.monthly;
  const ds = [
    series(g, g.nyfed, 'NY Fed model, 12m ahead', tok('--ink2'), {borderWidth: 2.25}),
    series(g, g.next, 'Kalshi next-year contract', tok(YT(NY)), {borderWidth: 2}),
    series(g, g.front, 'Kalshi current-year contract', tok(YT(CY)), {borderWidth: 1.6, borderDash: [6, 3]})
  ];
  document.getElementById('legend-ny').innerHTML = legendHTML([
    {color: tok('--ink2'), label: 'NY Fed model (12m ahead)'}, {color: tok(YT(NY)), label: 'Kalshi next-year'},
    {color: tok(YT(CY)), label: 'Kalshi current-year', style: 'dash'}]);
  const xmin = g.ms[0] - 10 * DAY, xmax = ASOF;
  const o = baseOpts({xmin, xmax, ymax: niceMax(visibleMax(ds, xmin, xmax)), padTop: 8});
  o.plugins.tooltip.callbacks.title = items => { if (!items.length) return ''; const d = new Date(items[0].raw.x); return MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear() + ' (month end)'; };
  mount('ch-ny', {type: 'line', data: {datasets: ds}, options: o});
}

/* ---------- tables ---------- */
function tables(){
  const res = D.contracts.filter(c => c.result !== 'open');
  const cell = v => v == null ? '<td class="na">–</td>' : '<td>' + fmt(v) + '</td>';
  const sw = y => '<span class="th-sw" style="background:var(' + YT(y) + ')"></span>';
  document.getElementById('tbl-track').innerHTML =
    '<thead><tr><th>Contract</th><th>Listed</th><th>At listing</th><th>Peak<br>(daily close)</th><th>Peak date</th><th>Entering year<br>(31 Dec)</th><th>Mid-year<br>(30 Jun)</th><th>Weeks &ge; 50%</th><th>Brier</th><th>vs base rate</th><th>Outcome</th></tr></thead><tbody>' +
    res.map(c => '<tr><td>' + sw(c.year) + c.year + ' ' + c.venue + '</td><td>' + fshort(c.open) + '</td>' + cell(c.first) + cell(c.peak) +
      '<td>' + fshort(c.peakDate) + '</td>' + cell(c.entering) + cell(c.midyear) + '<td>' + c.wks50 + ' / ' + c.weeks + '</td><td>' + c.brier.toFixed(3) +
      '</td><td>' + (c.brier > D.brierBase ? 'worse' : 'better') + '</td><td><span class="pill">' + c.result + '</span></td></tr>').join('') +
    '</tbody>' + (D.brierPooled != null ? '<tfoot><tr><td>Pooled, all weeks</td><td colspan="7"></td><td>' + D.brierPooled.toFixed(3) + '</td><td>base ' + D.brierBase.toFixed(4) + '</td><td></td></tr></tfoot>' : '');

  const cols = WIN.slice();
  const impY = D.weekly.imp[String(NY)] ? String(NY) : null;
  const head = '<thead><tr><th>Week ending</th>' + cols.map(k => '<th>' + sw(CBY[k].year) + CBY[k].year + ' ' + venueShort(CBY[k].venue) + (CBY[k].cumulative && k === 'P' + yy(NY) ? ' cum.' : '') + '</th>').join('') +
    (impY ? '<th>' + sw(NY) + NY + ' PM only</th>' : '') + '</tr></thead>';
  const W = D.weekly, rows = [];
  for (let i = W.t.length - 1; i >= 0; i--) {
    if (!cols.some(k => W.s[k][i] != null)) continue;
    rows.push('<tr><td>' + fshort(W.t[i]) + '</td>' + cols.map(k => cell(W.s[k][i])).join('') + (impY ? cell(W.imp[impY][i]) : '') + '</tr>');
  }
  document.getElementById('tbl-weekly').innerHTML = head + '<tbody>' + rows.join('') + '</tbody>';

  document.getElementById('tbl-spec').innerHTML =
    '<thead><tr><th>Contract</th><th>Resolves Yes if</th><th>Listed</th><th>Closes</th><th>Status</th><th>Latest</th><th>Volume</th><th>Open interest</th></tr></thead><tbody>' +
    D.contracts.slice().sort((a, b) => b.year - a.year || a.venue.localeCompare(b.venue)).map(c =>
      '<tr><td>' + sw(c.year) + '<a href="' + c.url + '" target="_blank" rel="noopener">' + c.year + ' ' + c.venue + '</a><br><span class="note">' + esc(c.ident.length > 28 ? c.ident.slice(0, 28) + '…' : c.ident) + '</span></td>' +
      '<td class="txt">' + esc(c.window) + '</td><td>' + fshort(c.open) + '</td><td>' + fshort(c.close) + '</td>' +
      '<td><span class="pill' + (c.result === 'open' ? ' open' : '') + '">' + (c.result === 'open' ? 'live' : 'settled ' + c.result) + '</span></td>' +
      '<td>' + fmt(c.last) + '</td><td>' + (c.venue === 'Kalshi' ? Math.round(c.volume).toLocaleString('en-US') + ' ct' : '$' + Math.round(c.volume).toLocaleString('en-US')) + '</td>' +
      '<td>' + (c.oi ? Math.round(c.oi).toLocaleString('en-US') + ' ct' : '<span class="na">–</span>') + '</td></tr>').join('') + '</tbody>';
}

function csvText(){
  const W = D.weekly, imps = Object.keys(W.imp);
  const lines = ['week_ending,' + KEYS.map(k => CBY[k].year + '_' + CBY[k].venue.toLowerCase()).join(',') + (imps.length ? ',' + imps.map(y => y + '_polymarket_' + y + '_only').join(',') : '')];
  W.t.forEach((t, i) => lines.push(t + ',' + KEYS.map(k => W.s[k][i] ?? '').join(',') + (imps.length ? ',' + imps.map(y => W.imp[y][i] ?? '').join(',') : '')));
  return lines.join('\n');
}
document.getElementById('copy-csv').addEventListener('click', ev => {
  const b = ev.currentTarget, txt = csvText(), fb = document.getElementById('csv-fallback');
  const done = m => { b.textContent = m; setTimeout(() => { b.textContent = 'Copy as CSV'; }, 1800); };
  const fallback = () => { fb.value = txt; fb.hidden = false; fb.focus(); fb.select(); done('Select and copy below'); };
  try { navigator.clipboard.writeText(txt).then(() => done('Copied ' + W_rows() + ' weeks'), fallback); } catch (e) { fallback(); }
});
const W_rows = () => D.weekly.t.length;

/* ---------- controls ---------- */
function bindSeg(id, key, after){
  const el = document.getElementById(id);
  const sync = () => el.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === state[key])));
  el.addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; state[key] = b.dataset.v; save(); sync(); after(); });
  sync();
}
function renderAll(){ kpis(); drawAll(); drawLife(); drawCN(); drawGap(); drawNY(); }
bindSeg('seg-freq', 'freq', () => { drawAll(); drawCN(); drawGap(); });
bindSeg('seg-venue', 'venue', drawAll);
bindSeg('seg-range', 'range', drawAll);
bindSeg('seg-life', 'life', drawLife);
tables();

Chart.defaults.font.family = 'IBM Plex Sans';
const start = () => renderAll();
(document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(start, start);
let tmr; const rerender = () => { clearTimeout(tmr); tmr = setTimeout(renderAll, 30); };
try { window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', rerender); } catch (e) {}
new MutationObserver(rerender).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
})();
</script>
'''

if __name__ == '__main__':
    main()
