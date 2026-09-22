#!/usr/bin/env python3
"""Arena Assistant - カード特徴DBの構築。

このスクリプトだけが DB を作る。start.py は作らない。

保存するのは「アートのサムネ」だけ。
照合に使う変種（crop/scale/offset）は start.py が起動時に生成する。
画角の校正を変えても DB を作り直さずに済むようにするため。

必要なもの
    Pillow      pip install Pillow
    numpy は不要（認識サーバ側では必要）

途中で止めても、再実行すれば登録済みのカードは飛ばして続きから再開する。
"""

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
except ImportError:
    print('Pillow がありません。  pip install Pillow')
    sys.exit(1)

# サムネの大きさは認識側と一致していなければならない
try:
    from start import TG, TC, MANA_TW, MANA_TH, SW, SH, FW, FH, RW, RH
except ImportError:
    TG, TC, MANA_TW, MANA_TH = 64, 16, 28, 32
    SW, SH, FW, FH, RW, RH = 40, 40, 48, 72, 256, 388

DB_PATH = 'arena_features.sqlite3'
CACHE_PATH = 'arena_stage1.cache'
CARDS_URL = 'https://api.hearthstonejson.com/v1/latest/{loc}/cards.collectible.json'
# 付属カード（収集不可）も含む全カード。--tokens のときだけ使う
ALL_CARDS_URL = 'https://api.hearthstonejson.com/v1/latest/{loc}/cards.json'
# どの付属カードがアリーナで配られるかは HSReplay の実対戦集計から得る
ARENA_STATS_URL = ('https://hsreplay.net/api/v1/arena/card_stats/free/'
                   '?ArenaTimestampRangeFilter=LAST_7_DAYS')
ARENA_UA = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36'),
    'Accept': 'application/json',
    'Referer': 'https://hsreplay.net/ja/arena/cards/',
}
RENDER_URL = 'https://art.hearthstonejson.com/v1/render/latest/{loc}/256x/{id}.png'
ART_URL = {
    '256x': 'https://art.hearthstonejson.com/v1/256x/{id}.jpg',
    '512x': 'https://art.hearthstonejson.com/v1/512x/{id}.jpg',
    'tiles': 'https://art.hearthstonejson.com/v1/tiles/{id}.jpg',
}
SKIP_SETS = {'HERO_SKINS'}
SKIP_TYPES = {'HERO', 'ENCHANTMENT'}
UA = {'User-Agent': 'ArenaAssistant/build'}

# カード全体に対するマナ結晶の位置。レンダー画像から実測した。
GEM_BOX = (0.020, 0.040, 0.235, 0.215)
COSTS = list(range(0, 11))
PER_COST = 14


def schema(con):
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        card_class TEXT,
        classes TEXT,
        cost INTEGER,
        ctype TEXT,
        races TEXT,
        spell_school TEXT,
        collectible INTEGER DEFAULT 1,
        art_src TEXT,
        gray BLOB NOT NULL,
        color BLOB NOT NULL)''')
    # 既存DBへの移行。アートは持ったまま列だけ増やす
    have = {r[1] for r in con.execute('PRAGMA table_info(cards)')}
    for col, decl in (('races', 'TEXT'), ('spell_school', 'TEXT'),
                      ('collectible', 'INTEGER DEFAULT 1')):
        if col not in have:
            con.execute('ALTER TABLE cards ADD COLUMN %s %s' % (col, decl))
            print('cards に %s 列を追加しました。' % col)
    con.execute('''CREATE TABLE IF NOT EXISTS failures (
        id TEXT PRIMARY KEY, reason TEXT, tries INTEGER DEFAULT 1)''')
    con.execute('''CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY, v TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS mana (
        cost INTEGER PRIMARY KEY, w INTEGER, h INTEGER, feat BLOB NOT NULL)''')
    # 種別ごとのアート窓と可視マスク。カードレンダーから実測する
    con.execute('''CREATE TABLE IF NOT EXISTS shapes (
        ctype TEXT PRIMARY KEY,
        samples INTEGER,
        s REAL, ox REAL, oy REAL,
        u0 REAL, v0 REAL, u1 REAL, v1 REAL,
        mw INTEGER, mh INTEGER, mask BLOB NOT NULL,
        fw INTEGER, fh INTEGER, frame BLOB NOT NULL, fweight BLOB NOT NULL)''')
    return con


def get(url, timeout=30):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def load_card_list(locale):
    url = CARDS_URL.format(loc=locale)
    print('カード一覧を取得: %s' % url)
    cards = json.loads(get(url))
    out = []
    for c in cards:
        if c.get('set') in SKIP_SETS or c.get('type') in SKIP_TYPES:
            continue
        if not c.get('id') or not c.get('name'):
            continue
        out.append({
            'id': c['id'],
            'name': c['name'],
            'cardClass': c.get('cardClass') or '',
            'classes': c.get('classes') or [],
            'cost': c.get('cost'),
            'type': c.get('type') or '',
            'races': c.get('races') or [],
            'spellSchool': c.get('spellSchool') or '',
            'collectible': 1,
        })
    print('対象カード: %d 枚' % len(out))
    return out


def thumbs(data, src):
    """アート画像から グレー TG×TG と カラー TC×TC を作る。"""
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode != 'RGB':
        im = im.convert('RGB')
    if src == 'tiles':
        # tiles は横長の帯なので、正方形の画角前提の照合とは相性が悪い
        w, h = im.size
        side = min(w, h)
        im = im.crop(((w - side) // 2, 0, (w - side) // 2 + side, side))
    gray = im.convert('L').resize((TG, TG), Image.LANCZOS).tobytes()
    color = im.resize((TC, TC), Image.LANCZOS).tobytes()
    if len(gray) != TG * TG or len(color) != TC * TC * 3:
        raise ValueError('thumb size mismatch')
    return gray, color


def fetch_one(card, src):
    url = ART_URL[src].format(id=card['id'])
    try:
        g, c = thumbs(get(url), src)
        return card, g, c, None
    except urllib.error.HTTPError as e:
        return card, None, None, 'HTTP %s' % e.code
    except Exception as e:  # noqa: BLE001 - 1枚の失敗で全体を止めない
        return card, None, None, '%s: %s' % (type(e).__name__, e)


def gem_feature(card_id, locale):
    """カードレンダーからマナ結晶を切り出し、平均0分散1へ規格化する。"""
    im = Image.open(io.BytesIO(get(RENDER_URL.format(loc=locale, id=card_id))))
    im.load()
    if im.mode == 'RGBA':
        bg = Image.new('RGB', im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert('RGB')
    w, h = im.size
    gem = im.crop((int(GEM_BOX[0] * w), int(GEM_BOX[1] * h),
                   int(GEM_BOX[2] * w), int(GEM_BOX[3] * h)))
    small = gem.convert('L').resize((MANA_TW, MANA_TH), Image.LANCZOS)
    a = [float(v) for v in small.tobytes()]
    m = sum(a) / len(a)
    sd = (sum((x - m) ** 2 for x in a) / len(a)) ** 0.5 + 1e-6
    return [(x - m) / sd for x in a]


def build_mana(con, cards, locale, workers):
    """コストごとの平均テンプレートを作る。認識時の候補絞り込みに使う。"""
    import struct
    from collections import defaultdict

    by_cost = defaultdict(list)
    for c in cards:
        if c['cost'] in COSTS and len(by_cost[c['cost']]) < PER_COST:
            by_cost[c['cost']].append(c['id'])

    todo = [(cost, cid) for cost, ids in by_cost.items() for cid in ids]
    print('マナ結晶のテンプレートを作成中（%d 枚）...' % len(todo))

    def one(item):
        cost, cid = item
        try:
            return cost, gem_feature(cid, locale)
        except Exception:  # noqa: BLE001
            return cost, None

    acc = defaultdict(list)
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for cost, f in ex.map(one, todo):
            if f:
                acc[cost].append(f)

    n = 0
    for cost, feats in sorted(acc.items()):
        if len(feats) < 4:
            continue
        avg = [sum(col) / len(col) for col in zip(*feats)]
        m = sum(avg) / len(avg)
        sd = (sum((x - m) ** 2 for x in avg) / len(avg)) ** 0.5 + 1e-6
        avg = [(x - m) / sd for x in avg]
        con.execute('INSERT OR REPLACE INTO mana VALUES(?,?,?,?)',
                    (cost, MANA_TW, MANA_TH,
                     struct.pack('<%df' % len(avg), *avg)))
        n += 1
    con.commit()
    print('  テンプレート %d 種（コスト %s）'
          % (n, ', '.join(str(c) for c in sorted(acc) if len(acc[c]) >= 4)))
    return n


def raw_render(card_id, locale):
    """カードレンダーを RGB で返す。透過部分は黒で埋める。"""
    im = Image.open(io.BytesIO(get(RENDER_URL.format(loc=locale, id=card_id))))
    im.load()
    if im.mode == 'RGBA':
        bg = Image.new('RGB', im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert('RGB')
    return im.resize((RW, RH), Image.LANCZOS)


def raw_art(card_id):
    im = Image.open(io.BytesIO(get(ART_URL['256x'].format(id=card_id))))
    im.load()
    return im.convert('RGB')


def _ncc_map(np, a, t):
    """valid 位置の正規化相互相関マップ。a は探索対象、t はテンプレート。"""
    th, tw = t.shape
    H, W = a.shape
    if H < th or W < tw:
        return None
    t0 = t - t.mean()
    tn = float(np.sqrt((t0 * t0).sum()))
    if tn < 1e-6:
        return None
    fh, fw = H + th - 1, W + tw - 1
    f = np.fft.rfft2(a, (fh, fw)) * np.fft.rfft2(t0[::-1, ::-1], (fh, fw))
    corr = np.fft.irfft2(f, (fh, fw))[th - 1:H, tw - 1:W]
    ii = np.zeros((H + 1, W + 1)); ii[1:, 1:] = np.cumsum(np.cumsum(a, 0), 1)
    i2 = np.zeros((H + 1, W + 1)); i2[1:, 1:] = np.cumsum(np.cumsum(a * a, 0), 1)

    def box(m):
        return m[th:, tw:] - m[:-th, tw:] - m[th:, :-tw] + m[:-th, :-tw]

    s1, s2 = box(ii), box(i2)
    var = np.maximum(s2 - s1 * s1 / (th * tw), 0.0)
    den = np.sqrt(var) * tn
    return np.where(den > 1e-6, corr / np.maximum(den, 1e-6), -1.0)


# 全種別で確実にアートの内側になる窓（レンダー 256x388 上の座標）
FIT_TPL = (100, 80, 156, 140)


def fit_art(np, render_gray, art_im):
    """render(u,v) -> art(s*u+ox, s*v+oy) の (ncc, s, ox, oy) を推定する。"""
    tpl = render_gray[FIT_TPL[1]:FIT_TPL[3], FIT_TPL[0]:FIT_TPL[2]]
    best = None
    for i in range(52):
        s = 0.55 + i * 0.025
        k = int(round(256 / s))
        a = np.asarray(art_im.convert('L').resize((k, k), Image.LANCZOS),
                       dtype=np.float64)
        m = _ncc_map(np, a, tpl)
        if m is None:
            continue
        j = int(np.argmax(m))
        v = float(m.flat[j])
        y, x = divmod(j, m.shape[1])
        if best is None or v > best[0]:
            best = (v, s, (x - FIT_TPL[0]) * s, (y - FIT_TPL[1]) * s)
    return best


def _grow(np, seed, allow):
    """seed から allow の中だけを塗り広げる。連結成分を1つ取り出すのに使う。"""
    cur = seed & allow
    for _ in range(600):
        nxt = cur.copy()
        nxt[1:, :] |= cur[:-1, :]
        nxt[:-1, :] |= cur[1:, :]
        nxt[:, 1:] |= cur[:, :-1]
        nxt[:, :-1] |= cur[:, 1:]
        nxt &= allow
        if int(nxt.sum()) == int(cur.sum()):
            break
        cur = nxt
    return cur


def _clean_mask(np, corr, thresh=0.72):
    """相関マップから、中央の連結成分だけを穴埋めして取り出す。"""
    raw = corr > thresh
    seed = np.zeros(raw.shape, bool)
    seed[RH // 4:RH // 2, RW // 3:2 * RW // 3] = True
    blob = _grow(np, seed & raw, raw)
    if not blob.any():
        blob = raw
    # 外側から補集合を塗り、届かなかった穴をアート扱いに戻す
    border = np.zeros(raw.shape, bool)
    border[0, :] = border[-1, :] = True
    border[:, 0] = border[:, -1] = True
    outside = _grow(np, border & ~blob, ~blob)
    return blob | ~(blob | outside)


def _resample(np, a, box, ow, oh):
    """a の box=(x0,y0,x1,y1) を ow x oh へ最近傍で伸縮する。"""
    x0, y0, x1, y1 = box
    xi = np.clip(np.linspace(x0, x1 - 1, ow), 0, a.shape[1] - 1).astype(np.int32)
    yi = np.clip(np.linspace(y0, y1 - 1, oh), 0, a.shape[0] - 1).astype(np.int32)
    return a[yi][:, xi]


def build_shapes(con, cards, locale, workers, per_type=48, types=None):
    """種別ごとのアート窓・可視マスク・枠テンプレートをレンダーから実測する。

    やり方。
      1. レンダーと素アートを NCC で位置合わせし、render->art の相似変換を得る
      2. 同じ種別のカードを重ね、画素ごとに「レンダーの値がアートの値と連動するか」
         を相関で測る。枠・名前バンド・文字はカードが変わっても動かないので相関が
         落ち、アートが見えている画素だけが残る
      3. 残った連結成分の外接矩形をアート窓、中身を可視マスクとして保存する
      4. レンダーの中央値はアートが消えて枠だけが残るので、種別判定の
         テンプレートとして一緒に保存する
    """
    try:
        import numpy as np
    except ImportError:
        print('--shapes には numpy が必要です。  pip install numpy')
        return 0

    want = types or ['MINION', 'SPELL', 'WEAPON', 'LOCATION']
    by_type = {}
    for t in want:
        ids = [c['id'] for c in cards if c['type'] == t]
        # セットが偏ると枠の世代も偏るので、全体から等間隔で拾う
        step = max(1, len(ids) // per_type)
        by_type[t] = ids[::step][:per_type]

    def one(cid):
        try:
            r = raw_render(cid, locale)
            a = raw_art(cid)
        except Exception:  # noqa: BLE001 - 1枚落ちても続ける
            return cid, None
        g = np.asarray(r.convert('L'), dtype=np.float64)
        f = fit_art(np, g, a)
        if not f or f[0] < 0.85:
            return cid, None
        return cid, (np.asarray(r, dtype=np.float32), a, f)

    n_done = 0
    for t in want:
        ids = by_type[t]
        if len(ids) < 8:
            print('  %-9s 標本が足りません（%d枚）' % (t, len(ids)))
            continue
        print('  %-9s %d枚を取得中...' % (t, len(ids)))
        got = []
        with ThreadPoolExecutor(max(1, workers)) as ex:
            for cid, v in ex.map(one, ids):
                if v:
                    got.append(v)
        if len(got) < 8:
            print('  %-9s 位置合わせできたのが %d枚だけでした' % (t, len(got)))
            continue

        fits = np.array([[v[2][1], v[2][2], v[2][3]] for v in got])
        s, ox, oy = (float(x) for x in np.median(fits, axis=0))
        # 全カードを同じ幾何で重ねる。個別の当てはめだと窓がにじむ
        ui = np.clip(np.arange(RW) * s + ox, 0, 255).astype(np.int32)
        vi = np.clip(np.arange(RH) * s + oy, 0, 255).astype(np.int32)
        R = np.stack([v[0].mean(axis=2) for v in got])
        A = np.stack([np.asarray(v[1].convert('L'), dtype=np.float32)[vi][:, ui]
                      for v in got])
        corr = ((R - R.mean(0)) * (A - A.mean(0))).mean(0)
        corr /= (R.std(0) * A.std(0) + 1e-3)
        corr = np.clip(corr, 0.0, 1.0)

        vis = _clean_mask(np, corr)
        ys, xs = np.nonzero(vis)
        box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        mask = (_resample(np, corr * vis, box, SW, SH) * 255).astype(np.uint8)
        frame = np.median(R, axis=0)
        fbuf = _resample(np, frame, (0, 0, RW, RH), FW, FH).astype(np.uint8)
        wbuf = (_resample(np, 1.0 - corr, (0, 0, RW, RH), FW, FH)
                * 255).astype(np.uint8)

        con.execute('INSERT OR REPLACE INTO shapes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (t, len(got), s, ox, oy,
                     box[0] / RW, box[1] / RH, box[2] / RW, box[3] / RH,
                     SW, SH, mask.tobytes(),
                     FW, FH, fbuf.tobytes(), wbuf.tobytes()))
        n_done += 1
        print('  %-9s n=%2d  s=%.4f ox=%.1f oy=%.1f  窓 x %.3f..%.3f y %.3f..%.3f '
              ' 可視 %.1f%%'
              % (t, len(got), s, ox, oy, box[0] / RW, box[2] / RW,
                 box[1] / RH, box[3] / RH, 100.0 * mask.mean() / 255.0))
    con.commit()
    return n_done


def update_meta(con, cards):
    """登録済みカードの付帯情報だけを更新する。アートは触らない。"""
    have = {r[0] for r in con.execute('SELECT id FROM cards')}
    rows = [(c['name'], c['cardClass'],
             json.dumps(c['classes'], ensure_ascii=False),
             c['cost'], c['type'],
             json.dumps(c['races'], ensure_ascii=False),
             c['spellSchool'], c['id'])
            for c in cards if c['id'] in have]
    con.executemany(
        'UPDATE cards SET name=?,card_class=?,classes=?,cost=?,ctype=?,'
        'races=?,spell_school=?,collectible=1 WHERE id=?', rows)
    con.commit()
    return len(rows)


def add_tokens(con, locale, src, workers):
    """アリーナで配られるのに収集不可なカード（付属カード）を足す。"""
    req = urllib.request.Request(ARENA_STATS_URL, headers=ARENA_UA)
    stats = json.loads(urllib.request.urlopen(req, timeout=60).read())
    ids = {c['card_id'] for c in (stats.get('data') or {}).get('ALL') or []}
    have = {r[0] for r in con.execute('SELECT id FROM cards')}
    missing = sorted(ids - have)
    print('アリーナ対象 %d 枚 / DB未登録 %d 枚' % (len(ids), len(missing)))
    if not missing:
        return 0

    print('全カード一覧を取得: %s' % ALL_CARDS_URL.format(loc=locale))
    byid = {c['id']: c for c in json.loads(get(ALL_CARDS_URL.format(loc=locale), 120))}

    todo = []
    for cid in missing:
        c = byid.get(cid)
        if not c or not c.get('name'):
            print('  %-14s 一覧にもありません' % cid)
            continue
        todo.append({
            'id': cid, 'name': c['name'],
            'cardClass': c.get('cardClass') or '',
            'classes': c.get('classes') or [],
            'cost': c.get('cost'), 'type': c.get('type') or '',
            'races': c.get('races') or [],
            'spellSchool': c.get('spellSchool') or '',
            # 3択には出ないので認識の候補からは外す
            'collectible': 0,
        })

    pending, ok, ng = [], 0, 0
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for card, g, c, err in ex.map(lambda x: fetch_one(x, src), todo):
            if err:
                ng += 1
                print('  %-14s %-24s %s' % (card['id'], card['name'], err))
                continue
            ok += 1
            pending.append((
                card['id'], card['name'], card['cardClass'],
                json.dumps(card['classes'], ensure_ascii=False),
                card['cost'], card['type'],
                json.dumps(card['races'], ensure_ascii=False),
                card['spellSchool'], 0, src, g, c))
    _flush(con, pending)
    print('付属カード %d 枚を追加（失敗 %d 枚）' % (ok, ng))
    return ok


def main():
    ap = argparse.ArgumentParser(description='カード特徴DBを作る')
    ap.add_argument('--db', default=DB_PATH)
    ap.add_argument('--src', default='256x', choices=sorted(ART_URL),
                    help='アート画像の種類（既定 256x）')
    ap.add_argument('--locale', default='jaJP', help='カード名の言語（既定 jaJP）')
    ap.add_argument('--workers', type=int, default=12, help='同時ダウンロード数')
    ap.add_argument('--limit', type=int, default=0, help='先頭N枚だけ処理（動作確認用）')
    ap.add_argument('--retry-failed', action='store_true',
                    help='前回失敗したカードだけ再試行する')
    ap.add_argument('--rebuild', action='store_true', help='DBを消して最初から作る')
    ap.add_argument('--mana-only', action='store_true',
                    help='マナコストのテンプレートだけを作り直す')
    ap.add_argument('--shapes', action='store_true',
                    help='種別ごとのアート窓・可視マスクをレンダーから実測する')
    ap.add_argument('--shape-samples', type=int, default=48,
                    help='--shapes で種別ごとに使うカード枚数（既定48）')
    ap.add_argument('--meta-only', action='store_true',
                    help='アートを再取得せず、名前・コスト・種族などだけ更新する')
    ap.add_argument('--tokens', action='store_true',
                    help='アリーナで配られる付属カード（収集不可）を追加する')
    args = ap.parse_args()

    if args.rebuild and os.path.exists(args.db):
        os.remove(args.db)
        print('既存DBを削除しました')
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)

    con = schema(sqlite3.connect(args.db, timeout=60))
    try:
        cards = load_card_list(args.locale)
    except Exception as e:  # noqa: BLE001
        print('カード一覧を取得できません: %s' % e)
        return 1

    done = {r[0] for r in con.execute(
        'SELECT id FROM cards WHERE art_src=?', (args.src,))}
    failed = {r[0]: r[1] for r in con.execute('SELECT id,reason FROM failures')}
    if args.mana_only:
        build_mana(con, cards, args.locale, args.workers)
        con.close()
        return 0

    if args.shapes:
        print('種別ごとのアート領域をカードレンダーから実測します。')
        build_shapes(con, cards, args.locale, args.workers, args.shape_samples)
        con.close()
        print('次は  python3 start.py  を起動してください。')
        return 0

    if args.meta_only:
        n = update_meta(con, cards)
        con.close()
        print('%d 枚の情報を更新しました。アートは触っていません。' % n)
        print('次は  python3 start.py  を起動してください。')
        return 0

    if args.tokens:
        add_tokens(con, args.locale, args.src, args.workers)
        con.close()
        print('次は  python3 start.py  を起動してください。')
        return 0

    if args.retry_failed:
        todo = [c for c in cards if c['id'] in failed]
        print('再試行対象: %d 枚' % len(todo))
    else:
        todo = [c for c in cards if c['id'] not in done]
    if args.limit:
        todo = todo[:args.limit]

    total = len(cards)
    skipped = len(cards) - len(todo)
    print('登録済み %d 枚 / 今回処理 %d 枚' % (len(done), len(todo)))
    if not todo:
        print('すべて登録済みです。')
        if not con.execute('SELECT COUNT(*) FROM mana').fetchone()[0]:
            build_mana(con, cards, args.locale, args.workers)
        if not con.execute('SELECT COUNT(*) FROM shapes').fetchone()[0]:
            build_shapes(con, cards, args.locale, args.workers, args.shape_samples)
        _summary(con, total, 0, skipped, 0, 0.0, args)
        return 0

    t0 = time.time()
    ok = ng = 0
    pending = []
    try:
        with ThreadPoolExecutor(max(1, args.workers)) as ex:
            for i, (card, g, c, err) in enumerate(
                    ex.map(lambda x: fetch_one(x, args.src), todo), 1):
                if err:
                    ng += 1
                    con.execute(
                        'INSERT INTO failures(id,reason,tries) VALUES(?,?,1) '
                        'ON CONFLICT(id) DO UPDATE SET reason=excluded.reason, '
                        'tries=tries+1', (card['id'], err))
                else:
                    ok += 1
                    pending.append((
                        card['id'], card['name'], card['cardClass'],
                        json.dumps(card['classes'], ensure_ascii=False),
                        card['cost'], card['type'],
                        json.dumps(card['races'], ensure_ascii=False),
                        card['spellSchool'], card.get('collectible', 1),
                        args.src, g, c))
                    con.execute('DELETE FROM failures WHERE id=?', (card['id'],))

                if len(pending) >= 200:
                    _flush(con, pending)
                if i % 100 == 0 or i == len(todo):
                    rate = i / max(time.time() - t0, 1e-6)
                    left = (len(todo) - i) / max(rate, 1e-6)
                    sys.stdout.write(
                        '\r  %d / %d   成功%d 失敗%d   %.0f枚/秒  残り%.0f秒   '
                        % (i, len(todo), ok, ng, rate, left))
                    sys.stdout.flush()
    except KeyboardInterrupt:
        print('\n中断しました。再実行すると続きから再開します。')
    finally:
        _flush(con, pending)
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    ('art_src', args.src))
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    ('locale', args.locale))
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    ('thumb', json.dumps([TG, TC])))
        con.commit()

    print()
    if not con.execute('SELECT COUNT(*) FROM mana').fetchone()[0]:
        build_mana(con, cards, args.locale, args.workers)
    if not con.execute('SELECT COUNT(*) FROM shapes').fetchone()[0]:
        build_shapes(con, cards, args.locale, args.workers, args.shape_samples)
    _summary(con, total, ok, skipped, ng, time.time() - t0, args)
    return 0


def _flush(con, pending):
    if not pending:
        return
    con.executemany(
        'INSERT OR REPLACE INTO cards '
        '(id,name,card_class,classes,cost,ctype,races,spell_school,collectible,'
        'art_src,gray,color) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', pending)
    con.commit()
    pending.clear()


def _summary(con, total, ok, skipped, ng, secs, args):
    have = con.execute('SELECT COUNT(*) FROM cards').fetchone()[0]
    fails = con.execute('SELECT COUNT(*) FROM failures').fetchone()[0]
    mana = con.execute('SELECT COUNT(*) FROM mana').fetchone()[0]
    shapes = con.execute('SELECT COUNT(*) FROM shapes').fetchone()[0]
    size = os.path.getsize(args.db) if os.path.exists(args.db) else 0
    print('=' * 52)
    print('  総カード数   : %d' % total)
    print('  今回成功     : %d' % ok)
    print('  スキップ     : %d  (登録済み・今回対象外)' % skipped)
    print('  今回失敗     : %d' % ng)
    print('  DB登録済み   : %d' % have)
    print('  マナテンプレート : %d 種' % mana)
    print('  アート窓     : %d 種別' % shapes)
    print('  未解決の失敗 : %d' % fails)
    print('  処理時間     : %.1f 秒' % secs)
    print('  DBサイズ     : %.1f MB  (%s)' % (size / 1e6, os.path.abspath(args.db)))
    print('=' * 52)
    if fails:
        print('失敗分を再試行する場合:')
        print('  python3 build_card_db.py --retry-failed')
        for cid, reason in con.execute(
                'SELECT id,reason FROM failures LIMIT 5'):
            print('    %-22s %s' % (cid, reason))
    if have:
        print('次は  python3 start.py  を起動してください。')


if __name__ == '__main__':
    sys.exit(main())
