#!/usr/bin/env python3
"""ホテルの期間限定の企画（アフタヌーンティー・レストランフェア・宿泊プラン・イベント）を集めて events.json にする。

出典は PR TIMES（prtimes.jp。robots.txt で許可されている範囲）。
保存するのは「短いタイトル・期間・種類・出典名・元ページの URL・取得日」だけで、本文や画像はコピーしない。
アクセスは 1 秒に 1 回以下。

使い方:
    python3 tools/collect_events.py                     # hotels.json の全ホテル
    python3 tools/collect_events.py --only-fhr          # FHR 対象だけ（試し）
    python3 tools/collect_events.py --limit 5 --dry-run # 5 軒だけ、書き込まない
"""
import argparse, datetime as dt, html, json, os, re, sys, time, unicodedata, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "HotelMapEventsBot/1.0 (+https://github.com/masa19941001/hotelmap-data)"
SEARCH = "https://prtimes.jp/main/action.php?run=html&page=searchkey&search_word={q}"
BASE = "https://prtimes.jp"
JST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(JST).date()
MAX_AGE_DAYS = 150          # これより古いリリースは見ない
PER_HOTEL = 6               # 1 軒あたり開くリリースの数
_last = [0.0]


def get(url):
    wait = 1.1 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print("  取得できず:", url, e, file=sys.stderr)
        return ""


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s・･\-‐–—'’.,、。&＆()（）「」『』]|ザ|the", "", s)


def kind_of(title):
    t = title
    if "アフタヌーンティー" in t or "afternoon tea" in t.lower():
        return "afternoonTea"
    if re.search(r"宿泊|ステイ|ご滞在|プラン|STAY", t, re.I):
        return "stay"
    if re.search(r"レストラン|ディナー|ランチ|ブッフェ|ビュッフェ|フェア|メニュー|コース|スイーツ|ケーキ|バー|カクテル|ペアリング|鮨|寿司|料理", t):
        return "restaurant"
    return "event"


DATE = r"(?:(20\d\d)年)?\s*(\d{1,2})月(\d{1,2})日"


def period(text, published):
    """本文から最初の「○年○月○日～○月○日」を取り出す"""
    m = re.search(DATE + r"[^0-9～〜~－\-]{0,12}[～〜~－\-]\s*" + DATE, text)
    if not m:
        return None
    y1 = int(m.group(1) or published.year)
    s = dt.date(y1, int(m.group(2)), int(m.group(3)))
    y2 = int(m.group(4) or y1)
    e = dt.date(y2, int(m.group(5)), int(m.group(6)))
    if e < s:                       # 年をまたぐ（12月～2月）のに年が書いていない
        e = e.replace(year=e.year + 1)
    if (e - s).days > 400:
        return None
    return s, e


# 企画ではないお知らせ（改装・人事・営業変更など）は載せない
NOT_EVENT = re.compile(r"改修|改装|刷新|リニューアル工事|工事に伴|営業内容|休業|お知らせ|人事|総支配人|総料理長|就任|受賞|開業\s*\d|グランドオープン")


BOILERPLATE = [
    r"\s*\d{1,2}月\d{1,2}日\s*[（(][^）)]{1,3}[）)]\s*(より|から)?\s*",
    r"\s*\d{1,2}月\d{1,2}日\s*(より|から)\s*",
    r"(の|を)?\s*(ご?予約受付を?開始|予約開始|販売を?開始|提供を?開始|発売を?開始|発売|開催決定|開催|展開|販売|提供)(いた)?(します|する|中)?\s*[。！!]?\s*$",
]


def make_short(title, names, limit=44):
    """リリースのタイトルから、ホテル名・会社名の括弧・定型句を除いて企画名だけにする"""
    t = re.sub(r"…$", "", title)
    t = re.sub(r"^\s*【[^】]*】\s*", "", t)
    if "】" in t and "【" not in t.split("】")[0]:      # 括弧の閉じだけ残っている場合は、その後ろを使う
        t = t.split("】", 1)[1]
    for n in sorted([n for n in names if n], key=len, reverse=True):
        chars = [c for c in n if not re.match(r"[\s・･]", c)]
        if len(chars) >= 3:
            # 先頭のホテル名だけ消す（途中のものを消すと文が壊れる）
            t = re.sub(r"^\s*" + r"[\s・･]*".join(re.escape(c) for c in chars), "", t, flags=re.I)
    for pat in BOILERPLATE:
        t = re.sub(pat, " ", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^[\s、，,:：\-–—×/／のはがにてで]+", "", t).strip(" 、，,:：-–—/／")
    if len(t) < 4:                                       # 削りすぎたら元のタイトルに戻す
        t = title
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _bigrams(text):
    t = norm(text.rstrip("…"))
    return {t[i:i + 2] for i in range(len(t) - 1)}


def same_event(a, b):
    """同じ企画の別リリースか。期間と種類が同じで、企画名（ホテル名を除いたもの）がよく似ているときだけ"""
    if (a["start"], a["end"], a["kind"]) != (b["start"], b["end"], b["kind"]):
        return False
    x, y = _bigrams(a.get("shortTitle") or a["title"]), _bigrams(b.get("shortTitle") or b["title"])
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.5


def dedupe(events):
    """同じ企画の別リリースだけを 1 件にする（同じホテルでも企画名や期間が違えば別件）"""
    out = []
    for e in sorted(events, key=lambda e: (e["start"], e["end"])):
        if not any(same_event(o, e) for o in out):
            out.append(e)
    return out


def meta(t, key):
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(key), t)
    return html.unescape(m.group(1)) if m else ""


def short(title, limit=60):
    title = re.sub(r"\s+", " ", title).strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def collect_for(hotel):
    names = [hotel["name"]] + ([hotel["nameEn"]] if hotel.get("nameEn") else [])
    keys = [norm(n) for n in names if len(norm(n)) >= 4]
    page = get(SEARCH.format(q=urllib.parse.quote(hotel["name"])))
    links = []
    for href in re.findall(r'href="(/main/html/rd/p/\d+\.\d+\.html)"', page):
        if href not in links:
            links.append(href)
    found = []
    for href in links[:PER_HOTEL]:
        t = get(BASE + href)
        if not t:
            continue
        title = meta(t, "og:title")
        m = re.search(r'"datePublished":"(\d{4}-\d{2}-\d{2})', t)
        if not title or not m:
            continue
        pub = dt.date.fromisoformat(m.group(1))
        if (TODAY - pub).days > MAX_AGE_DAYS:
            continue
        body = re.sub(r"<[^>]+>", " ", t)
        body = html.unescape(re.sub(r"\s+", " ", body))
        # そのホテルのリリースか（タイトルか本文の冒頭にホテル名が出てくる）
        head = norm(title + body[:1500])
        if not any(k in head for k in keys):
            continue
        p = period(body, pub)
        if not p or p[1] < TODAY or NOT_EVENT.search(title):
            continue
        found.append({
            "title": short(title),
            "shortTitle": make_short(title, names),
            "start": p[0].isoformat(),
            "end": p[1].isoformat(),
            "kind": kind_of(title),
            "sourceName": "PR TIMES",
            "url": BASE + href,
            "fetched": TODAY.isoformat(),
        })
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hotels", default=os.path.join(ROOT, "hotels.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "events.json"))
    ap.add_argument("--only-fhr", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    hotels = json.load(open(a.hotels, encoding="utf-8"))
    hotels = hotels["hotels"] if isinstance(hotels, dict) else hotels
    hotels = [h for h in hotels if not h.get("soon")]
    if a.only_fhr:
        hotels = [h for h in hotels if h.get("fhr")]
    if a.limit:
        hotels = hotels[: a.limit]

    events, misses = {}, []
    for i, h in enumerate(hotels, 1):
        ev = collect_for(h)
        # 同じ URL は 1 回だけ
        seen, uniq = set(), []
        for e in dedupe(ev):
            if e["url"] not in seen:
                seen.add(e["url"]); uniq.append(e)
        if uniq:
            events[str(h["id"])] = uniq
        else:
            misses.append(h["name"])
        print(f"[{i}/{len(hotels)}] {h['name']}: {len(uniq)} 件", flush=True)

    out = {"version": 1, "updated": dt.datetime.now(JST).isoformat(timespec="seconds"),
           "source": "PR TIMES（タイトル・期間・リンクのみ）", "events": events}
    total = sum(len(v) for v in events.values())
    print(f"\n企画が見つかったホテル: {len(events)} / {len(hotels)} 軒、企画 {total} 件")
    if not a.dry_run:
        old = json.load(open(a.out, encoding="utf-8")) if os.path.exists(a.out) else {}
        if old.get("events") == events:
            print("変更なし（events.json はそのまま）")
            return
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("events.json を書き換えました")


if __name__ == "__main__":
    main()
