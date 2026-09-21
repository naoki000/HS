#!/usr/bin/env python3
"""Arena Assistant - 認識サーバ。

役割は「検索」だけ。DB は build_card_db.py が作る。ここでは一切作らない。

照合の考え方
    ゲーム画面のアート枠は、元アートの一部を一定の画角で映している。
    その画角は全カード共通なので、DB 側のサムネから crop/scale/offset の
    変種を作って合わせにいく。クエリ側を変形しても画角は広がらないため、
    変形は必ず DB 側に適用する。

    Stage1  27〜48 変種の粗グリッド（輝度・エッジ・色）で候補を数百枚に絞る
    Stage2  上位候補だけサムネから精密パッチを生成し、z正規化NCC で再順位付け

DB にはサムネだけを保存し、変種は起動時に生成する。
こうすると画角の校正を変えても DB を作り直さずに済む。
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
import sys
import threading
import time
import zlib

try:
    import numpy as np
except ImportError:
    np = None

DB_PATH = 'arena_features.sqlite3'
CACHE_PATH = 'arena_stage1.cache'
CALIB_PATH = 'arena_calibration.json'
DECK_PATH = 'arena_deck.json'
HOST = '0.0.0.0'
PORT = 8080

# 認識に使う切り取り範囲。フレーム全体に対する比率で持つ。
# 配信が全画面か分割表示かで位置が変わるため、固定座標にしない。
# PC で合わせた値を iPad へそのまま持っていけるよう、ファイルに残す。
ART_DEFAULT = {
    'left':   {'x': 0.150, 'y': 0.204, 'w': 0.073, 'h': 0.104},
    'middle': {'x': 0.308, 'y': 0.204, 'w': 0.077, 'h': 0.101},
    'right':  {'x': 0.473, 'y': 0.207, 'w': 0.073, 'h': 0.096},
}
SLOT_KEYS = ('left', 'middle', 'right')

# ---- 特徴量の形。build_card_db.py と必ず一致させること ----
TG = 64          # 保存するグレースケールサムネ（正方形・アート全体）
TC = 16          # 保存するカラーサムネ
PW, PH = 48, 32  # Stage2 の照合パッチ
GW, GH = 6, 4    # Stage1 の粗グリッド（PW/GW と PH/GH は割り切れること）
assert PW % GW == 0 and PH % GH == 0

# ---- 画角の校正。ここを変えても DB の作り直しは不要 ----
CAL = {
    'aspect': 1.60,                              # アート枠の 横/縦
    'scales': [0.72, 0.80, 0.88, 0.96],          # 元アート幅に対する表示幅
    'tx': [0.25, 0.5, 0.75],                     # 余白に対する水平位置
    'ty': [0.3, 0.5, 0.7],                       # 余白に対する垂直位置
}

STAGE1_KEEP = 800      # Stage1 から Stage2 へ渡す枚数
VARIANT_PROBE = 60     # 変種を決めるために全変種を試す上位枚数
STAGE2_RETURN = 8      # クライアントへ返す件数

# ---- マナコスト ----
# 結晶の数字をテンプレート照合で読み、一致するカードを加点する。
# 除外ではなく加点なのは、コストを誤認識しても正解を落とさないため。
MANA_TW, MANA_TH = 28, 32
# アート枠に対する結晶の位置。カードレンダーから実測した。
MANA_REL = {'x': -0.391, 'y': -0.191, 'w': 0.393, 'h': 0.492}
COST_BONUS = 0.030     # コストが一致した候補への加点
COST_MIN_MARGIN = 0.02  # 実測で、この差があれば誤読ゼロ・採用率88%
W_SSIM, W_EDGE, W_COLOR, W_PIXEL = 0.40, 0.30, 0.15, 0.15
MIN_ACCEPT = 0.45      # これ未満は「特定できない」
CLOSE_GAP = 0.030      # 1位と2位の差がこれ未満なら「判定不確実」

CARDS_URL = 'https://api.hearthstonejson.com/v1/latest/jaJP/cards.collectible.json'

# ---- アリーナの対象カードと勝率 ----
# 公式のローテーション表は無いので、HSReplay の実対戦集計に
# 登場したカードを「アリーナ対象」とみなす。ローリング集計なので
# ローテーション変更には自動で追従する。
# 期間は hsreplay.net の画面の既定値に揃える。ずらすとサイトと数値が合わない。
ARENA_RANGE = 'LAST_4_DAYS'
ARENA_URL = ('https://hsreplay.net/api/v1/arena/card_stats/free/'
             '?ArenaTimestampRangeFilter=' + ARENA_RANGE)
ARENA_POOL_PATH = 'arena_pool.json'
ARENA_TTL = 6 * 3600
# 素の urllib の UA だと Cloudflare に 403 される
ARENA_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36'),
    'Accept': 'application/json',
    'Accept-Language': 'ja,en;q=0.9',
    'Referer': 'https://hsreplay.net/ja/arena/cards/',
}

_lock = threading.Lock()
_pool_lock = threading.Lock()
_idx = None
_pool = None

# 判定の進捗を端末へ出す。iPad ではこれが唯一の手がかりになる。
VERBOSE = True


def log(msg):
    if VERBOSE:
        print(msg, flush=True)


class DBMissing(Exception):
    pass


# ------------------------------------------------------------------ 特徴量

def variants():
    """(scale, ox, oy, h) の一覧。位置は余白に対する比率で決めるので常に画内に収まる。"""
    # 先頭はアート全体。切り出し済みでない画像を直接入れても照合できるようにする。
    out = [(1.0, 0.0, 0.0, 1.0)]
    a = float(CAL['aspect'])
    for s in CAL['scales']:
        h = min(s / a, 1.0)
        for tx in CAL['tx']:
            for ty in CAL['ty']:
                out.append((s, tx * (1.0 - s), ty * (1.0 - h), h))
    return out


def _idx_map(n, start, size, out):
    lo = start * n
    hi = (start + size) * n
    return np.clip(np.linspace(lo, hi - 1, out), 0, n - 1).astype(np.int32)


def edge_map(a):
    """隣接差分の和。最後の行列は端を複製して形を保つ。"""
    gx = np.abs(np.diff(a, axis=-1, append=a[..., -1:]))
    gy = np.abs(np.diff(a, axis=-2, append=a[..., -1:, :]))
    return gx + gy


def block_mean(a, by, bx):
    """(..., H, W) を (..., H//by, W//bx) へ平均縮小。"""
    sh = a.shape[:-2] + (a.shape[-2] // by, by, a.shape[-1] // bx, bx)
    return a.reshape(sh).mean(axis=(-3, -1))


def znorm(v, axis=-1):
    m = v.mean(axis=axis, keepdims=True)
    s = v.std(axis=axis, keepdims=True) + 1e-6
    return (v - m) / s


def coarse_from_patch(gray, color):
    """Stage1 用の粗特徴。gray:(...,PH,PW) color:(...,GH*2,GW*2,3)"""
    y = block_mean(gray, PH // GH, PW // GW)
    e = block_mean(edge_map(gray), PH // GH, PW // GW) * 0.5
    c = block_mean(np.moveaxis(color, -1, -3).astype(np.float32), 2, 2)
    flat = [y.reshape(y.shape[:-2] + (-1,)),
            e.reshape(e.shape[:-2] + (-1,)),
            c.reshape(c.shape[:-3] + (-1,))]
    return np.clip(np.concatenate(flat, axis=-1), 0, 255)


# ------------------------------------------------------------------ 索引

def open_db():
    if not os.path.exists(DB_PATH):
        raise DBMissing(
            '%s がありません。build_card_db.py を実行してください。' % DB_PATH)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def cal_key():
    raw = json.dumps([CAL, TG, TC, PW, PH, GW, GH], sort_keys=True).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def build_index():
    if np is None:
        raise DBMissing('numpy が必要です。  pip install numpy')

    con = open_db()
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(cards)')}
        extra = ',races,spell_school' if 'races' in cols else ''
        has_col = 'collectible' in cols
        rows = con.execute(
            'SELECT id,name,card_class,classes,cost,ctype,gray,color%s%s FROM cards '
            'ORDER BY id' % (extra, ',collectible' if has_col else '')).fetchall()
    finally:
        con.close()
    if not rows:
        raise DBMissing('DB が空です。build_card_db.py を実行してください。')
    if not extra:
        log('!! cards に races 列がありません。'
            'python3 build_card_db.py --meta-only を実行してください。')

    ids, names, klass, classes, cost, ctype = [], [], [], [], [], []
    tags, draftable = [], []
    gbuf, cbuf = [], []
    for r in rows:
        if len(r['gray']) != TG * TG or len(r['color']) != TC * TC * 3:
            continue
        ids.append(r['id'])
        names.append(r['name'])
        klass.append(r['card_class'] or '')
        try:
            classes.append(json.loads(r['classes'] or '[]'))
        except ValueError:
            classes.append([])
        cost.append(r['cost'] if r['cost'] is not None else -1)
        ctype.append(r['ctype'] or '')
        # 種類・種族・周波数を一本のタグ列にまとめて絞り込みを単純にする
        t = [r['ctype']] if r['ctype'] else []
        if extra:
            try:
                t.extend(json.loads(r['races'] or '[]'))
            except ValueError:
                pass
            if r['spell_school']:
                t.append(r['spell_school'])
        tags.append(t)
        # 付属カードは3択に並ばないので、認識の候補からは外す
        draftable.append(bool(r['collectible']) if has_col else True)
        gbuf.append(r['gray'])
        cbuf.append(r['color'])

    n = len(ids)
    gray = np.frombuffer(b''.join(gbuf), dtype=np.uint8).reshape(n, TG, TG)
    color = np.frombuffer(b''.join(cbuf), dtype=np.uint8).reshape(n, TC, TC, 3)

    vs = variants()
    key = cal_key() + ':' + hashlib.sha1((''.join(ids)).encode()).hexdigest()[:16]
    coarse = _load_cache(key, (n, len(vs)))
    if coarse is None:
        coarse = _build_coarse(gray, color, vs)
        _save_cache(key, coarse)

    return {
        'ids': ids, 'names': names, 'klass': klass, 'classes': classes,
        'cost': cost, 'ctype': ctype, 'tags': tags,
        'draftable': np.array(draftable, dtype=bool),
        'costarr': np.array(cost, dtype=np.int32),
        'mana': load_mana(),
        'gray': gray, 'color': color,
        'edge': np.clip(edge_map(gray.astype(np.float32)), 0, 255).astype(np.uint8),
        'coarse': coarse.astype(np.float32),
        'variants': vs,
        'byid': {c: i for i, c in enumerate(ids)},
    }


def load_mana():
    """コストごとの結晶テンプレート。無ければコスト判定を使わない。"""
    try:
        con = open_db()
    except DBMissing:
        return None
    try:
        rows = con.execute('SELECT cost,w,h,feat FROM mana ORDER BY cost').fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    costs, vecs = [], []
    for r in rows:
        if r['w'] != MANA_TW or r['h'] != MANA_TH:
            continue
        v = np.frombuffer(r['feat'], dtype='<f4')
        if v.size != MANA_TW * MANA_TH:
            continue
        costs.append(int(r['cost']))
        vecs.append(v)
    if not costs:
        return None
    return {'costs': np.array(costs, dtype=np.int32), 'mat': np.stack(vecs)}


def classify_cost(gray_bytes):
    """結晶の画像からコストを読む。確信が低ければ None を返す。"""
    mana = index().get('mana')
    if not mana or len(gray_bytes) != MANA_TW * MANA_TH:
        return None, 0.0
    q = znorm(np.frombuffer(gray_bytes, dtype=np.uint8).astype(np.float32))
    sc = mana['mat'] @ q / q.size
    o = np.argsort(-sc)
    margin = float(sc[o[0]] - sc[o[1]]) if sc.size > 1 else 1.0
    if margin < COST_MIN_MARGIN:
        return None, margin
    return int(mana['costs'][o[0]]), margin


def _build_coarse(gray, color, vs):
    n = gray.shape[0]
    out = np.empty((n, len(vs), GW * GH * 5), dtype=np.uint8)
    gf = gray.astype(np.float32)
    cf = color.astype(np.float32)
    for k, (s, ox, oy, h) in enumerate(vs):
        yi = _idx_map(TG, oy, h, PH)
        xi = _idx_map(TG, ox, s, PW)
        ci = _idx_map(TC, oy, h, GH * 2)
        cj = _idx_map(TC, ox, s, GW * 2)
        g = gf[:, yi][:, :, xi]
        c = cf[:, ci][:, :, cj]
        out[:, k, :] = coarse_from_patch(g, c).astype(np.uint8)
    return out


def _load_cache(key, shape):
    try:
        with open(CACHE_PATH, 'rb') as f:
            if f.read(16).decode() != key[:16]:
                return None
            k2 = f.read(16).decode()
            if k2 != key[16:32]:
                return None
            a, b, c = struct.unpack('<III', f.read(12))
            if (a, b) != shape:
                return None
            return np.frombuffer(f.read(a * b * c), dtype=np.uint8).reshape(a, b, c)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _save_cache(key, arr):
    try:
        with open(CACHE_PATH, 'wb') as f:
            f.write(key[:16].encode())
            f.write(key[16:32].encode())
            f.write(struct.pack('<III', *arr.shape))
            f.write(arr.tobytes())
    except OSError:
        pass


def index():
    global _idx
    with _lock:
        if _idx is None:
            _idx = build_index()
        return _idx


def invalidate():
    global _idx
    with _lock:
        _idx = None


# ------------------------------------------------------------ アート枠の校正

def default_art():
    return {k: dict(v) for k, v in ART_DEFAULT.items()}


def load_art():
    try:
        with open(CALIB_PATH, encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return default_art()
    art = default_art()
    for k in SLOT_KEYS:
        v = (saved.get('art') or {}).get(k)
        if isinstance(v, dict) and all(isinstance(v.get(f), (int, float))
                                       for f in ('x', 'y', 'w', 'h')):
            art[k] = {f: float(v[f]) for f in ('x', 'y', 'w', 'h')}
    return art


def load_flip():
    """配信が 180° ひっくり返っているか。iPad の向きによって起きる。"""
    try:
        with open(CALIB_PATH, encoding='utf-8') as f:
            return bool(json.load(f).get('flip', True))
    except (OSError, ValueError):
        return True


def save_art(art, flip=None):
    clean = default_art()
    for k in SLOT_KEYS:
        v = (art or {}).get(k)
        if isinstance(v, dict):
            for f in ('x', 'y', 'w', 'h'):
                if isinstance(v.get(f), (int, float)):
                    clean[k][f] = round(float(v[f]), 4)
    payload = {'art': clean, 'flip': bool(load_flip() if flip is None else flip)}
    with open(CALIB_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


# ------------------------------------------------------- アリーナ対象と勝率

def fetch_arena_pool():
    req = Request(ARENA_URL, headers=ARENA_HEADERS)
    with urlopen(req, timeout=30) as r:
        raw = json.loads(r.read().decode('utf-8'))
    classes = {}
    packages = {}
    for key, lst in (raw.get('data') or {}).items():
        m = {}
        for c in lst:
            cid = c.get('card_id')
            if cid:
                m[cid] = {'drawn': c.get('drawn_win_rate'),
                          'pop': c.get('popularity'),
                          'win': c.get('win_rate'),
                          'played': c.get('played_win_rate'),
                          'games': c.get('num_games')}
                # このカードはどのカードに付属して来るのか
                for k in (c.get('package_key_card_ids') or []):
                    packages.setdefault(k, [])
                    if cid not in packages[k]:
                        packages[k].append(cid)
        if m:
            classes[key] = m
    if not classes:
        raise ValueError('アリーナ統計が空でした')
    return {'fetched': time.time(), 'range': ARENA_RANGE,
            'classes': classes, 'packages': packages}


def arena_pool(force=False):
    """取得に失敗したら前回分を使う。それも無ければ None。"""
    global _pool
    with _pool_lock:
        if _pool is None:
            try:
                with open(ARENA_POOL_PATH, encoding='utf-8') as f:
                    _pool = json.load(f)
            except (OSError, ValueError):
                _pool = None
        # 期間を変えたときは古いキャッシュを使わない
        stale = (_pool is None
                 or _pool.get('range') != ARENA_RANGE
                 or (time.time() - _pool.get('fetched', 0)) > ARENA_TTL)
        if force or stale:
            try:
                t0 = time.time()
                fresh = fetch_arena_pool()
                with open(ARENA_POOL_PATH, 'w', encoding='utf-8') as f:
                    json.dump(fresh, f, ensure_ascii=False)
                _pool = fresh
                log('アリーナ対象 %d枚 を取得 %.1fs'
                    % (len(fresh['classes'].get('ALL', {})), time.time() - t0))
            except Exception as e:  # noqa: BLE001 - 前回分で続行する
                log('!! アリーナ統計の取得に失敗: %s: %s' % (type(e).__name__, e))
        return _pool


def arena_maps(hero):
    """(クラス別, ALL) の card_id -> 統計。クラス別には中立も含まれる。"""
    pool = arena_pool()
    cls = (pool or {}).get('classes') or {}
    return cls.get((hero or '').upper()) or {}, cls.get('ALL') or {}


def arena_stat(prim, allm, cid):
    return prim.get(cid) or allm.get(cid)


# ------------------------------------------------------------ ピックの保存

def load_deck():
    try:
        with open(DECK_PATH, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {'hero': '', 'picks': []}
    return {'hero': str(d.get('hero') or ''),
            'picks': [c for c in (d.get('picks') or []) if isinstance(c, str)]}


def save_deck(deck):
    with open(DECK_PATH, 'w', encoding='utf-8') as f:
        json.dump(deck, f, ensure_ascii=False)
    return deck


def deck_view(deck):
    """同じカードをまとめて枚数を付け、デッキ勝率の降順で返す。"""
    idx = index()
    prim, allm = arena_maps(deck['hero'])
    counts = {}
    for cid in deck['picks']:
        counts[cid] = counts.get(cid, 0) + 1

    cards = []
    for cid, n in counts.items():
        i = idx['byid'].get(cid)
        c = {'cardId': cid, 'count': n,
             'name': idx['names'][i] if i is not None else cid,
             'cost': idx['cost'][i] if i is not None else None,
             'tags': idx['tags'][i] if i is not None else [],
             'token': bool(i is not None and not idx['draftable'][i])}
        if allm.get(cid):
            c['arenaAll'] = allm[cid]
        if prim.get(cid):
            c['arenaClass'] = prim[cid]
        cards.append(c)

    def win(c):
        a = c.get('arenaClass') or c.get('arenaAll') or {}
        return a.get('win')

    # 勝率不明は末尾へ。同率は名前順で安定させる
    cards.sort(key=lambda c: (win(c) is None, -(win(c) or 0), c['name']))
    return {'ok': True, 'hero': deck['hero'],
            'total': len(deck['picks']), 'cards': cards}


# ------------------------------------------------------------------ 照合

def eligible_mask(idx, hero, arena_only=False):
    """ヒーロークラス + NEUTRAL + そのクラスを含むマルチクラスカード。"""
    n = len(idx['ids'])
    if hero:
        hero = hero.upper()
        m = np.zeros(n, dtype=bool)
        for i in range(n):
            k = idx['klass'][i]
            cl = idx['classes'][i]
            m[i] = (k == hero or k == 'NEUTRAL' or hero in cl
                    or (not k and not cl))
    else:
        m = np.ones(n, dtype=bool)
    # 付属カードはドラフトの3択に出ない
    m = m & idx['draftable']
    if arena_only:
        prim, allm = arena_maps(hero)
        # クラス指定時はそのクラスのドラフト候補（中立込み）がそのまま使える
        ids = prim or allm
        if ids:
            am = np.fromiter((c in ids for c in idx['ids']), dtype=bool, count=n)
            # 絞った結果 0 枚になるなら絞らない。探せなくなる方が害が大きい
            if am.any():
                m = m & am
    return m


def query_descriptors(gray_bytes, color_bytes):
    g = np.frombuffer(gray_bytes, dtype=np.uint8).reshape(PH, PW).astype(np.float32)
    c = np.frombuffer(color_bytes, dtype=np.uint8).reshape(GH * 2, GW * 2, 3).astype(np.float32)
    return {
        'coarse': coarse_from_patch(g, c),
        'gray': g.ravel(),
        'zgray': znorm(g.ravel()),
        'zedge': znorm(edge_map(g).ravel()),
        'color': c.reshape(-1),
    }


def recognize(q, hero, want_debug, correct_id, cost_hint=None, arena_only=False):
    t0 = time.time()
    log('      recognize 開始')
    idx = index()
    log('      index 取得 %.2fs' % (time.time() - t0))
    vs = idx['variants']
    mask = eligible_mask(idx, hero, arena_only)
    pool = np.flatnonzero(mask)
    if pool.size == 0:
        return {'status': 'empty', 'candidates': []}
    t_mask = time.time()
    log('      候補絞り込み %d枚  %.2fs' % (pool.size, t_mask - t0))

    # ---- Stage1 ----
    # coarse 全体は100MB超なので、選択コピーを作らず連続スライスで回す
    cq = q['coarse']
    coarse = idx['coarse']
    n_all = coarse.shape[0]
    s1all = np.empty(n_all, dtype=np.float32)
    for a in range(0, n_all, 2048):
        blk = coarse[a:a + 2048]
        s1all[a:a + 2048] = np.abs(blk - cq).mean(axis=2).min(axis=1)
        log('        Stage1 %d/%d  %.2fs' % (min(a + 2048, n_all), n_all,
                                             time.time() - t_mask))
    s1all = 1.0 - s1all / 255.0
    s1all[~mask] = -1.0
    order1 = np.argsort(-s1all)[:int(pool.size)]
    keep = min(STAGE1_KEEP, order1.size)
    cand = order1[:keep]
    t_s1 = time.time()
    log('      Stage1 完了 → %d枚  %.2fs' % (keep, t_s1 - t_mask))

    # ---- Stage2 ----
    # 画角は3枚共通なので、変種はカードごとに選ばせず全体で1つに決める。
    # カードごとに最大を取ると、誤ったカードが自分に都合の良い変種を選べてしまう。
    G = idx['gray'][cand].astype(np.float32)
    E = idx['edge'][cand].astype(np.float32)
    C = idx['color'][cand].astype(np.float32)
    m = len(cand)

    def score(rows, k):
        s, ox, oy, h = vs[k]
        yi = _idx_map(TG, oy, h, PH)
        xi = _idx_map(TG, ox, s, PW)
        ci = _idx_map(TC, oy, h, GH * 2)
        cj = _idx_map(TC, ox, s, GW * 2)
        flat = G[rows][:, yi][:, :, xi].reshape(len(rows), -1)
        ef = E[rows][:, yi][:, :, xi].reshape(len(rows), -1)
        col = C[rows][:, ci][:, :, cj].reshape(len(rows), -1)
        ssim = znorm(flat) @ q['zgray'] / q['zgray'].size
        edge = znorm(ef) @ q['zedge'] / q['zedge'].size
        color = 1.0 - np.abs(col - q['color']).mean(axis=1) / 255.0
        pixel = 1.0 - np.abs(flat - q['gray']).mean(axis=1) / 255.0
        f = W_SSIM * ssim + W_EDGE * edge + W_COLOR * color + W_PIXEL * pixel
        return f, np.stack([ssim, edge, color, pixel], axis=1)

    probe = np.arange(min(VARIANT_PROBE, m))
    kbest, kscore = 0, -9e9
    for k in range(len(vs)):
        f, _ = score(probe, k)
        top = float(f.max())
        if top > kscore:
            kscore, kbest = top, k
        if (k + 1) % 10 == 0 or k + 1 == len(vs):
            log('        変種探索 %d/%d  %.2fs' % (k + 1, len(vs), time.time() - t_s1))

    rows = np.arange(m)
    best, parts = score(rows, kbest)
    bestv = np.full(m, kbest, dtype=np.int32)
    if cost_hint is not None:
        best = best + COST_BONUS * (idx['costarr'][cand] == cost_hint)
        log('      コスト %d に加点 %.3f' % (cost_hint, COST_BONUS))
    log('      Stage2 完了  %.2fs  (合計 %.2fs)'
        % (time.time() - t_s1, time.time() - t0))

    order2 = np.argsort(-best)
    top = order2[:STAGE2_RETURN]

    cands = []
    prim, allm = arena_maps(hero)
    for rank, j in enumerate(top, 1):
        i = int(cand[j])
        s, ox, oy, h = vs[int(bestv[j])]
        cid = idx['ids'][i]
        c = {
            'rank': rank,
            'cardId': cid,
            'name': idx['names'][i],
            'cardClass': idx['klass'][i],
            'cost': idx['cost'][i],
            'type': idx['ctype'][i],
            'finalScore': round(float(best[j]), 4),
            'ssim': round(float(parts[j][0]), 4),
            'edge': round(float(parts[j][1]), 4),
            'color': round(float(parts[j][2]), 4),
            'pixel': round(float(parts[j][3]), 4),
            'variant': {'scale': round(s, 3), 'ox': round(ox, 3),
                        'oy': round(oy, 3), 'h': round(h, 3)},
        }
        st = arena_stat(prim, allm, cid)
        if st:
            c['arena'] = st
        st_all = allm.get(cid)
        if st_all:
            c['arenaAll'] = st_all
        st_cls = prim.get(cid) if prim else None
        if st_cls:
            c['arenaClass'] = st_cls
        cands.append(c)

    top1 = cands[0]['finalScore'] if cands else 0.0
    top2 = cands[1]['finalScore'] if len(cands) > 1 else -1.0
    gap = top1 - top2 if len(cands) > 1 else 1.0
    if top1 < MIN_ACCEPT:
        status = 'nomatch'
    elif gap < CLOSE_GAP:
        status = 'uncertain'
    else:
        status = 'ok'

    res = {'status': status, 'gap': round(float(gap), 4),
           'poolSize': int(pool.size), 'stage1Keep': int(keep),
           'detectedCost': cost_hint,
           'candidates': cands}

    if correct_id and correct_id in idx['byid']:
        ci_ = idx['byid'][correct_id]
        r1 = int(np.flatnonzero(order1 == ci_)[0]) + 1 if (order1 == ci_).any() else -1
        pos2 = np.flatnonzero(cand[order2] == ci_)
        r2 = int(pos2[0]) + 1 if pos2.size else -1
        res['correct'] = {
            'cardId': correct_id,
            'name': idx['names'][ci_],
            'inPool': bool(mask[ci_]),
            'stage1Rank': r1, 'stage1Total': int(pool.size),
            'stage1Cutoff': int(keep),
            'excludedAfterStage1': bool(r1 > keep or r1 < 0),
            'stage2Rank': r2, 'stage2Total': int(keep),
            'finalRank': r2,
        }

    if want_debug:
        res['debug'] = {
            'variants': len(vs),
            'calibration': CAL,
            'weights': {'ssim': W_SSIM, 'edge': W_EDGE,
                        'color': W_COLOR, 'pixel': W_PIXEL},
        }
    return res


# ------------------------------------------------------------------ PNG

def png(rgb, w, h):
    """Pillow を使わずに PNG を組み立てる（デバッグ表示用）。"""
    raw = b''.join(b'\x00' + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))


# ------------------------------------------------------------------ HTTP

class Handler(SimpleHTTPRequestHandler):
    server_version = 'ArenaAssistant'
    # 単一スレッドなので keep-alive にしてはいけない。次の要求を待つ readline が
    # 他の接続を丸ごと止めてしまい、/castframe の裏で /api/deck が返らなくなる。
    protocol_version = 'HTTP/1.0'
    timeout = 10

    def address_string(self):
        # 既定実装は逆引きDNSを引くことがあり、1リクエスト数秒待たされる
        return self.client_address[0]

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _bin(self, data, ctype, cache='no-store'):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', cache)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        n = int(self.headers.get('Content-Length', '0') or 0)
        return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}

    # -------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == '/api/info':
                return self._info()
            if u.path == '/api/calibration':
                return self._json({'ok': True, 'art': load_art(), 'flip': load_flip()})
            if u.path == '/api/cards':
                return self._cards(q)
            if u.path == '/api/arena':
                return self._arena(q)
            if u.path == '/api/deck':
                return self._json(deck_view(load_deck()))
            if u.path == '/api/thumb':
                return self._thumb(q)
            if u.path == '/castframe':
                return self._castframe(q)
            if u.path == '/':
                self.path = '/arena_assistant.html'
            return super().do_GET()
        except DBMissing as e:
            return self._json({'ok': False, 'error': str(e)}, 503)
        except Exception as e:  # noqa: BLE001 - 応答を返しきる
            return self._json({'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}, 500)

    def _info(self):
        info = {'ok': True, 'numpy': np is not None,
                'db': os.path.abspath(DB_PATH),
                'dbExists': os.path.exists(DB_PATH),
                'patch': [PW, PH], 'grid': [GW, GH],
                'thumb': [TG, TC], 'calibration': CAL,
                'stage1Keep': STAGE1_KEEP}
        if not os.path.exists(DB_PATH):
            info.update({'ok': False,
                         'error': '%s がありません。build_card_db.py を実行してください。' % DB_PATH})
            return self._json(info, 503)
        idx = index()
        info.update({'cards': len(idx['ids']), 'variants': len(idx['variants']),
                     'mana': int(idx['mana']['costs'].size) if idx.get('mana') else 0,
                     'manaPatch': [MANA_TW, MANA_TH], 'manaRel': MANA_REL,
                     'bytes': os.path.getsize(DB_PATH)})
        pool = arena_pool()
        info['arena'] = ({'total': len(pool['classes'].get('ALL', {})),
                          'ageSec': int(time.time() - pool['fetched'])}
                         if pool else None)
        return self._json(info)

    def _cards(self, q):
        idx = index()
        term = (q.get('q', [''])[0] or '').strip().lower()
        hero = (q.get('hero', [''])[0] or '').strip()
        arena_only = q.get('arenaOnly', ['0'])[0] not in ('', '0', 'false')
        prim, allm = arena_maps(hero)
        pool = prim or allm
        out = []
        for i, name in enumerate(idx['names']):
            cid = idx['ids'][i]
            if term and term not in name.lower() and term not in cid.lower():
                continue
            st = arena_stat(prim, allm, cid)
            if arena_only and pool and st is None:
                continue
            c = {'cardId': cid, 'name': name,
                 'cardClass': idx['klass'][i], 'cost': idx['cost'][i]}
            if st:
                c['arena'] = st
            if allm.get(cid):
                c['arenaAll'] = allm[cid]
            if prim and prim.get(cid):
                c['arenaClass'] = prim[cid]
            out.append(c)
            if len(out) >= 40:
                break
        return self._json({'ok': True, 'cards': out})

    def _arena(self, q):
        pool = arena_pool(force=q.get('refresh', ['0'])[0] not in ('', '0', 'false'))
        if not pool:
            return self._json({'ok': False,
                               'error': 'アリーナ統計を取得できませんでした。'}, 503)
        cls = pool['classes']
        return self._json({
            'ok': True,
            'fetched': pool['fetched'],
            'range': pool.get('range', ARENA_RANGE),
            'ageSec': int(time.time() - pool['fetched']),
            'total': len(cls.get('ALL', {})),
            'byClass': {k: len(v) for k, v in cls.items()},
            'source': ARENA_URL,
        })

    def _thumb(self, q):
        """DB側サムネを返す。変種を指定すると照合時と同じ切り出しで返す。"""
        idx = index()
        cid = q.get('id', [''])[0]
        if cid not in idx['byid']:
            return self.send_error(404, 'unknown card')
        i = idx['byid'][cid]
        g = idx['gray'][i].astype(np.float32)
        if 's' in q:
            s = float(q['s'][0])
            ox = float(q.get('ox', ['0'])[0])
            oy = float(q.get('oy', ['0'])[0])
            h = float(q.get('h', [str(s / CAL['aspect'])])[0])
            g = g[_idx_map(TG, oy, h, PH)][:, _idx_map(TG, ox, s, PW)]
        ow = max(8, min(512, int(q.get('w', ['192'])[0])))
        oh = max(8, min(512, int(q.get('h2', [str(int(ow * g.shape[0] / g.shape[1]))])[0])))
        yi = np.clip(np.linspace(0, g.shape[0] - 1, oh), 0, g.shape[0] - 1).astype(np.int32)
        xi = np.clip(np.linspace(0, g.shape[1] - 1, ow), 0, g.shape[1] - 1).astype(np.int32)
        a = g[yi][:, xi].astype(np.uint8)
        rgb = np.repeat(a[:, :, None], 3, axis=2)
        return self._bin(png(rgb.tobytes(), ow, oh), 'image/png',
                         'public, max-age=3600')

    def _deck(self, body):
        deck = load_deck()
        act = str(body.get('action') or 'add')
        cid = str(body.get('cardId') or '')
        added = []
        if act == 'clear':
            deck = {'hero': '', 'picks': []}
        elif act == 'remove':
            if cid in deck['picks']:
                deck['picks'].remove(cid)   # 重複していても1枚だけ減らす
        elif act == 'add':
            if cid not in index()['byid']:
                return self._json({'ok': False, 'error': '不明なカードです: %s' % cid}, 400)
            deck['picks'].append(cid)
            hero = str(body.get('hero') or '')
            if hero:
                deck['hero'] = hero
            # 付属カードは元カードと一緒に手に入るので同時に記録する
            if body.get('withPackage', True):
                byid = index()['byid']
                for extra in ((arena_pool() or {}).get('packages') or {}).get(cid, []):
                    if extra in byid:
                        deck['picks'].append(extra)
                        added.append(extra)
        else:
            return self._json({'ok': False, 'error': 'action が不正です'}, 400)
        save_deck(deck)
        view = deck_view(deck)
        if added:
            idx = index()
            view['added'] = [idx['names'][idx['byid'][x]] for x in added]
        return self._json(view)

    def _shutdown(self):
        """iPad には Ctrl-C が無いので、画面から終了できるようにする。"""
        body = json.dumps({'ok': True}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        log('停止要求を受けました。終了します。')
        # serve_forever の中から shutdown() を呼ぶと待ち合わせで固まる。
        # 応答を返しきってから別スレッドでプロセスごと落とす。
        threading.Thread(target=_die, daemon=True).start()

    def _castframe(self, q):
        host = (q.get('host', [''])[0] or '').rstrip('/')
        if not host.startswith(('http://', 'https://')):
            return self.send_error(400, 'bad host')
        try:
            req = Request(host + '/frame', headers={'User-Agent': 'ArenaAssistant'})
            with urlopen(req, timeout=6) as r:
                data = r.read()
                ctype = r.headers.get('Content-Type', 'image/jpeg')
            return self._bin(data, ctype)
        except Exception as e:  # noqa: BLE001
            return self.send_error(502, str(e))

    # ------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == '/api/shutdown':
                return self._shutdown()
            if u.path == '/api/calibration':
                body = self._body()
                saved = save_art(body.get('art'), body.get('flip'))
                return self._json({'ok': True, 'art': saved['art'],
                                   'flip': saved['flip']})
            if u.path == '/api/deck':
                return self._deck(self._body())
            if u.path != '/api/recognize':
                return self._json({'ok': False, 'error': 'not found'}, 404)
            body = self._body()
            hero = str(body.get('hero', '') or '')
            debug = bool(body.get('debug'))
            arena_only = bool(body.get('arenaOnly'))
            queries = body.get('queries') or []
            if not queries:
                return self._json({'ok': False, 'error': 'queries が空です'}, 400)

            results = []
            t_all = time.time()
            log('判定 %d枚  hero=%s%s'
                % (len(queries), hero or 'ALL',
                   '  アリーナ対象のみ' if arena_only else ''))
            for n, item in enumerate(queries, 1):
                gb = base64.b64decode(item.get('gray', ''))
                cb = base64.b64decode(item.get('color', ''))
                if len(gb) != PW * PH or len(cb) != (GW * 2) * (GH * 2) * 3:
                    return self._json(
                        {'ok': False,
                         'error': 'query サイズ不一致 gray=%d(期待%d) color=%d(期待%d)'
                                  % (len(gb), PW * PH, len(cb), (GW * 2) * (GH * 2) * 3)}, 400)
                log('  [%d/%d] %s' % (n, len(queries), item.get('key', '')))
                t1 = time.time()
                qd = query_descriptors(gb, cb)
                log('      query 変換 %.2fs' % (time.time() - t1))
                cost_hint = None
                mb = base64.b64decode(item.get('mana', '') or '')
                if mb:
                    cost_hint, margin = classify_cost(mb)
                    log('      マナ読み取り %s (確信差 %.3f)'
                        % ('コスト %d' % cost_hint if cost_hint is not None else '不明',
                           margin))
                r = recognize(qd, hero, debug,
                              str(item.get('correctId', '') or
                                  body.get('correctId', '') or ''),
                              cost_hint, arena_only)
                top = (r.get('candidates') or [{}])[0]
                log('  [%d/%d] %s -> %s %s  %.2fs'
                    % (n, len(queries), item.get('key', ''), r.get('status'),
                       top.get('name', ''), time.time() - t1))
                r['key'] = item.get('key', '')
                results.append(r)
            log('判定完了  合計 %.2fs' % (time.time() - t_all))

            return self._json({'ok': True, 'results': results,
                               'stage1Keep': STAGE1_KEEP,
                               'patch': [PW, PH], 'colorGrid': [GW * 2, GH * 2]})
        except DBMissing as e:
            return self._json({'ok': False, 'error': str(e)}, 503)
        except Exception as e:  # noqa: BLE001
            log('!! %s: %s' % (type(e).__name__, e))
            return self._json({'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}, 500)


def _die():
    time.sleep(0.3)
    os._exit(0)


class Server(HTTPServer):
    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        # ブラウザのリロードやタブを閉じた際に必ず起きる。全文を出すと a-Shell が埋まる
        if isinstance(e, (ConnectionResetError, BrokenPipeError,
                          ConnectionAbortedError, TimeoutError)):
            log('  接続が切れました (%s)' % type(e).__name__)
            return
        super().handle_error(request, client_address)


def lan_ips():
    """同一ネットワークの端末から到達しうる IPv4 を列挙する。"""
    found = []
    # UDP connect は実際には送信しないので、既定経路の送信元 IP だけ得られる
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith('127.')]


def main():
    print('Arena Assistant  認識サーバ')
    print('  numpy     :', 'あり' if np is not None else 'なし（必須です）')
    print('  DB        :', os.path.abspath(DB_PATH))
    if not os.path.exists(DB_PATH):
        print()
        print('  !! %s がありません。' % DB_PATH)
        print('  !! 先に  python3 build_card_db.py  を実行してください。')
        print('  （サーバは起動しますが、認識APIは 503 を返します）')
    elif np is None:
        print()
        print('  !! numpy がありません。  pip install numpy')
    else:
        try:
            idx = index()
            print('  カード    : %d 枚' % len(idx['ids']))
            print('  変種      : %d 通り／枚' % len(idx['variants']))
        except Exception as e:  # noqa: BLE001
            print('  索引の構築に失敗:', e)
    pool = arena_pool()
    if pool:
        print('  アリーナ  : %d 枚（取得から %d 分）'
              % (len(pool['classes'].get('ALL', {})),
                 int((time.time() - pool['fetched']) / 60)))
    else:
        print('  アリーナ  : 取得できませんでした（絞り込みは使えません）')
    print()
    print('  このPCから')
    print('    運用  http://localhost:%d/arena_assistant.html' % PORT)
    print('    検証  http://localhost:%d/arena_test.html' % PORT)
    ips = lan_ips()
    if ips:
        print()
        print('  同じネットワークの端末から（%s で待受中）' % HOST)
        for ip in ips:
            print('    運用  http://%s:%d/arena_assistant.html' % (ip, PORT))
        print()
        print('  つながらない場合はファイアウォールで TCP %d を許可してください。' % PORT)
    else:
        print()
        print('  !! LAN の IP を取得できませんでした。ipconfig / ifconfig で確認してください。')
    print()
    print('  ※ 認証はありません。信頼できるネットワークでのみ使用してください。')
    try:
        # a-Shell ではサブスレッドで numpy が停止する事例があるため、
        # リクエストはメインスレッドで順に処理する。同時実行は不要。
        Server((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\n停止しました。')
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
