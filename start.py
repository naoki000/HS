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
SHAPE_CACHE_PATH = 'arena_shape.cache'
CALIB_PATH = 'arena_calibration.json'
DECK_PATH = 'arena_deck.json'
HOST = '0.0.0.0'
PORT = 8080

# 認識に使う切り取り範囲。フレーム全体に対する比率で持つ。
# 配信が全画面か分割表示かで位置が変わるため、固定座標にしない。
# PC で合わせた値を iPad へそのまま持っていけるよう、ファイルに残す。
#
# 枠はカード全体を囲む。アートの形はミニオン＝縦長の楕円、呪文＝横長の角丸、
# 武器＝円と種別ごとに違うので、「絵に合わせて」では合わせようがない。
# カードの外枠なら種別に関係なく一意に決まり、そこからアート窓もマナ結晶も
# 実測値で割り出せる。
CARD_DEFAULT = {
    'left':   {'x': 0.113, 'y': 0.175, 'w': 0.150, 'h': 0.271},
    'middle': {'x': 0.269, 'y': 0.176, 'w': 0.158, 'h': 0.263},
    'right':  {'x': 0.436, 'y': 0.180, 'w': 0.150, 'h': 0.250},
}
SLOT_KEYS = ('left', 'middle', 'right')

# ---- 特徴量の形。build_card_db.py と必ず一致させること ----
TG = 64          # 保存するグレースケールサムネ（正方形・アート全体）
TC = 16          # 保存するカラーサムネ
PW, PH = 48, 32  # Stage2 の照合パッチ
GW, GH = 6, 4    # Stage1 の粗グリッド（PW/GW と PH/GH は割り切れること）
assert PW % GW == 0 and PH % GH == 0

# ---- 種別ごとのアート領域（shape 方式）----
# ミニオンは縦長の楕円、呪文は横長の角丸、武器は円と、枠の形が種別ごとに違う。
# 同じ長方形で切ると枠や名前バンドが混ざるので、種別ごとの窓とマスクで照合する。
# 窓とマスクは build_card_db.py --shapes がカードレンダーから実測して DB に入れる。
SW, SH = 40, 40    # 種別ごとに正規化した照合パッチ（窓を引き伸ばして正方形にする）
SGW, SGH = 8, 8    # その粗グリッド
assert SW % SGW == 0 and SH % SGH == 0
SCW, SCH = SGW * 2, SGH * 2    # 色パッチ
FW, FH = 48, 72    # 枠テンプレート（カード全体）
RW, RH = 256, 388  # カードレンダーの基準サイズ。実測値はこの座標系で持つ
# クライアントが送ってくるカード全体のパッチ。ここから種別ごとの窓を切る。
# 手でドラッグした枠は必ずずれるので、まわりに余白を付けて送ってもらい、
# サーバ側で枠テンプレートに合わせ直す。
CARD_MARGIN = 0.15
CARD_W, CARD_H = 152, 230
CARD_CW, CARD_CH = 38, 58
# 枠合わせの探索範囲。余白 15% ぶんまでのずれを拾えるようにする。
# 粗く全種別を見てから、勝った種別だけを段階的に細かく詰める。
# マナ結晶はカードの 0.2 ほどしかないので、ここが粗いと数字を切り損なう。
ALIGN_SCALES = (0.88, 0.94, 1.0, 1.06, 1.12)
ALIGN_SHIFT = (-0.12, -0.06, 0.0, 0.06, 0.12)
ALIGN_FINE = (0.02, 0.007, 0.0025)
# 窓が分からない種別はミニオン扱いにする。3択に出るのはほぼミニオンか呪文。
SHAPE_FALLBACK = 'MINION'

# クエリ側の校正ずれを吸収する微小変種。shape 方式は幾何が既知なので、
# 37変種のような大きな探索は要らない。残差ぶんだけ DB 側をずらす。
SHAPE_JITTER = {
    'scales': [0.94, 1.0, 1.06],
    'tx': [-0.05, 0.0, 0.05],
    'ty': [-0.05, 0.0, 0.05],
}
TYPE_BONUS = 0.020      # 枠テンプレートで判定した種別への加点
TYPE_MIN_MARGIN = 0.03  # 1位と2位の差がこれ未満なら種別を決めない
SHAPE_HIGHPASS = 4      # 局所平均を引く半径。0 で無効

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
# カード全体に対する結晶の位置。build_card_db.py の GEM_BOX と同じ実測値。
GEM_REL = {'x': 0.020, 'y': 0.040, 'w': 0.215, 'h': 0.175}
# 結晶は 25px ほどしかなく、カード全体のパッチ経由だと数字が潰れて読めない。
# 校正枠のこの範囲だけ原寸で別に送ってもらい、枠合わせの結果で切り直す。
GEM_PAD = (-0.10, -0.08, 0.34, 0.34)
GEM_W, GEM_H = 96, 120
COST_BONUS = 0.030     # コストが一致した候補への加点（旧方式のみ）
# 清潔なレンダーなら 96% 読めるが、実機相当まで落とすと 0.05 でやっと
# 採用率 35% / 誤読 11%。旧値 0.02 はアート画像で測った値で、実機では甘い。
COST_MIN_MARGIN = 0.05
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


# --------------------------------------------------- 種別ごとのアート領域

def span(n, a0, a1, out):
    """長さ n の軸の [a0,a1)（0..1 の比率）を out 個へ等間隔で割り当てる。"""
    return np.clip(np.linspace(a0 * n, a1 * n - 1, out), 0, n - 1).astype(np.int32)


def wznorm(v, w, axis=-1):
    """重み付きの平均0分散1。マスク外の画素を無視して正規化する。"""
    s = w.sum(axis=axis, keepdims=True) + 1e-6
    m = (v * w).sum(axis=axis, keepdims=True) / s
    d = v - m
    sd = np.sqrt((d * d * w).sum(axis=axis, keepdims=True) / s) + 1e-6
    return d / sd


def wblock_mean(a, w, by, bx):
    """マスクの重みを効かせたブロック平均。重みが無いブロックは 0 にする。"""
    num = block_mean(a * w, by, bx)
    den = block_mean(w, by, bx)
    return np.where(den > 0.05, num / np.maximum(den, 1e-6), 0.0)


def highpass(a, r=SHAPE_HIGHPASS):
    """局所平均を引く。カード枠がアートに掛けている陰影を打ち消すため。

    ゲーム画面のアートには枠の内側に向かって暗くなる陰影が乗っているが、
    DB 側は素のアートなので乗っていない。全体を平均0にするだけでは消えず、
    輪郭の近くほど大きな差として残る。
    """
    if not r:
        return a
    k = 2 * r + 1
    pad = [(0, 0)] * (a.ndim - 2) + [(r + 1, r), (r + 1, r)]
    p = np.pad(a, pad, mode='edge')
    c = np.cumsum(np.cumsum(p, axis=-1), axis=-2)
    box = (c[..., k:, k:] - c[..., :-k, k:]
           - c[..., k:, :-k] + c[..., :-k, :-k]) / float(k * k)
    return a - box


def shape_coarse(gray, color, mask, cmask):
    """Stage1 用の粗特徴（種別マスク版）。gray:(...,SH,SW) color:(...,SCH,SCW,3)"""
    y = wblock_mean(gray, mask, SH // SGH, SW // SGW)
    e = wblock_mean(edge_map(gray), mask, SH // SGH, SW // SGW) * 0.5
    cm = np.moveaxis(color, -1, -3).astype(np.float32) * cmask
    c = block_mean(cm, SCH // SGH, SCW // SGW)
    flat = [y.reshape(y.shape[:-2] + (-1,)),
            e.reshape(e.shape[:-2] + (-1,)),
            c.reshape(c.shape[:-3] + (-1,))]
    return np.clip(np.concatenate(flat, axis=-1), 0, 255)


def art_box(sh, jitter=(1.0, 0.0, 0.0)):
    """カード座標のアート窓を、素アート（0..1）上の矩形に直す。

    build_card_db.py --shapes が測った render(u,v) -> art(s*u+ox, s*v+oy) を使う。
    jitter は校正ずれを吸収するための拡大率と平行移動（窓の大きさに対する比率）。
    """
    s, ox, oy = sh['s'], sh['ox'], sh['oy']
    x0 = (s * sh['u0'] * RW + ox) / 256.0
    x1 = (s * sh['u1'] * RW + ox) / 256.0
    y0 = (s * sh['v0'] * RH + oy) / 256.0
    y1 = (s * sh['v1'] * RH + oy) / 256.0
    k, tx, ty = jitter
    w, h = (x1 - x0), (y1 - y0)
    cx, cy = x0 + w / 2 + tx * w, y0 + h / 2 + ty * h
    return cx - w * k / 2, cy - h * k / 2, cx + w * k / 2, cy + h * k / 2


def shape_jitter():
    out = []
    for k in SHAPE_JITTER['scales']:
        for tx in SHAPE_JITTER['tx']:
            for ty in SHAPE_JITTER['ty']:
                out.append((k, tx, ty))
    # 先頭を無変形にしておくと、Stage1 の粗特徴と同じ切り出しになる
    out.sort(key=lambda v: (abs(v[0] - 1.0) + abs(v[1]) + abs(v[2])))
    return out


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


def load_shapes():
    """種別ごとのアート窓・可視マスク・枠テンプレート。無ければ shape 方式は使えない。"""
    try:
        con = open_db()
    except DBMissing:
        return {}
    try:
        rows = con.execute('SELECT * FROM shapes').fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    out = {}
    for r in rows:
        if (r['mw'], r['mh'], r['fw'], r['fh']) != (SW, SH, FW, FH):
            continue
        m = np.frombuffer(r['mask'], np.uint8).reshape(SH, SW).astype(np.float32) / 255.0
        out[r['ctype']] = {
            's': float(r['s']), 'ox': float(r['ox']), 'oy': float(r['oy']),
            'u0': float(r['u0']), 'v0': float(r['v0']),
            'u1': float(r['u1']), 'v1': float(r['v1']),
            'mask': m,
            'cmask': m[span(SH, 0, 1, SCH)][:, span(SW, 0, 1, SCW)],
            'frame': np.frombuffer(r['frame'], np.uint8)
                       .reshape(FH, FW).astype(np.float32),
            'fweight': np.frombuffer(r['fweight'], np.uint8)
                         .reshape(FH, FW).astype(np.float32) / 255.0,
        }
        sh = out[r['ctype']]
        sh['zframe'] = wznorm(sh['frame'].ravel(), sh['fweight'].ravel())
    return out


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
    coarse = _load_cache(CACHE_PATH, key, (n, len(vs), GW * GH * 5))
    if coarse is None:
        coarse = _build_coarse(gray, color, vs)
        _save_cache(CACHE_PATH, key, coarse)

    shapes = load_shapes()
    stype = np.array([t if t in shapes else SHAPE_FALLBACK for t in ctype])
    scoarse = None
    if shapes:
        skey = key + ':shape:' + str(sorted(shapes))
        scoarse = _load_cache(SHAPE_CACHE_PATH, skey, (n, SGW * SGH * 5))
        if scoarse is None:
            scoarse = _build_shape_coarse(gray, color, stype, shapes)
            _save_cache(SHAPE_CACHE_PATH, skey, scoarse)

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
        'shapes': shapes,
        'stype': stype,
        'trows': {t: np.flatnonzero(stype == t) for t in shapes},
        'scoarse': scoarse,
        'sjitter': shape_jitter(),
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


def _build_shape_coarse(gray, color, stype, shapes):
    """種別ごとの窓で切り出した粗特徴。変種は作らない（幾何が既知なので不要）。"""
    n = gray.shape[0]
    out = np.zeros((n, SGW * SGH * 5), dtype=np.uint8)
    gf = gray.astype(np.float32)
    cf = color.astype(np.float32)
    for t, sh in shapes.items():
        rows = np.flatnonzero(stype == t)
        if rows.size == 0:
            continue
        x0, y0, x1, y1 = art_box(sh)
        yi, xi = span(TG, y0, y1, SH), span(TG, x0, x1, SW)
        ci, cj = span(TC, y0, y1, SCH), span(TC, x0, x1, SCW)
        for a in range(0, rows.size, 1024):
            r = rows[a:a + 1024]
            g = gf[r][:, yi][:, :, xi]
            c = cf[r][:, ci][:, :, cj]
            out[r] = shape_coarse(g, c, sh['mask'], sh['cmask']).astype(np.uint8)
    return out


def _load_cache(path, key, shape):
    key = hashlib.sha1(key.encode()).hexdigest()[:32]
    try:
        with open(path, 'rb') as f:
            if f.read(32).decode() != key:
                return None
            nd = struct.unpack('<I', f.read(4))[0]
            if nd != len(shape):
                return None
            dims = struct.unpack('<%dI' % nd, f.read(4 * nd))
            if dims != tuple(shape):
                return None
            cnt = 1
            for d in dims:
                cnt *= d
            return np.frombuffer(f.read(cnt), dtype=np.uint8).reshape(dims)
    except (OSError, ValueError, UnicodeDecodeError, struct.error):
        return None


def _save_cache(path, key, arr):
    key = hashlib.sha1(key.encode()).hexdigest()[:32]
    try:
        with open(path, 'wb') as f:
            f.write(key.encode())
            f.write(struct.pack('<I', arr.ndim))
            f.write(struct.pack('<%dI' % arr.ndim, *arr.shape))
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


# ------------------------------------------------------------ カード枠の校正

def default_card():
    return {k: dict(v) for k, v in CARD_DEFAULT.items()}


def _rect(v):
    if isinstance(v, dict) and all(isinstance(v.get(f), (int, float))
                                   for f in ('x', 'y', 'w', 'h')):
        return {f: float(v[f]) for f in ('x', 'y', 'w', 'h')}
    return None


def sub_rect(card, rel):
    """カード枠の中の相対矩形（0..1）をフレーム比率へ直す。"""
    return {'x': card['x'] + rel[0] * card['w'],
            'y': card['y'] + rel[1] * card['h'],
            'w': (rel[2] - rel[0]) * card['w'],
            'h': (rel[3] - rel[1]) * card['h']}


def art_rel(ctype=SHAPE_FALLBACK):
    """カード枠に対するアート窓の位置。実測値が無ければ従来の既定値。"""
    sh = index()['shapes'].get(ctype) if np is not None else None
    if not sh:
        return (0.250, 0.108, 0.738, 0.492)
    return (sh['u0'], sh['v0'], sh['u1'], sh['v1'])


def load_card():
    """カード枠。旧形式（アート枠）しか無ければミニオンの窓から逆算する。"""
    try:
        with open(CALIB_PATH, encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return default_card()
    out = default_card()
    for k in SLOT_KEYS:
        v = _rect((saved.get('card') or {}).get(k))
        if v:
            out[k] = v
            continue
        old = _rect((saved.get('art') or {}).get(k))
        if old:
            u0, v0, u1, v1 = (0.250, 0.108, 0.738, 0.492)
            w, h = old['w'] / (u1 - u0), old['h'] / (v1 - v0)
            out[k] = {'x': old['x'] - u0 * w, 'y': old['y'] - v0 * h,
                      'w': w, 'h': h}
    return out


def load_art():
    """旧方式（長方形1種）用のアート枠。カード枠から割り出す。"""
    rel = art_rel()
    return {k: sub_rect(v, rel) for k, v in load_card().items()}


def load_flip():
    """配信が 180° ひっくり返っているか。iPad の向きによって起きる。"""
    try:
        with open(CALIB_PATH, encoding='utf-8') as f:
            return bool(json.load(f).get('flip', True))
    except (OSError, ValueError):
        return True


def save_card(card, flip=None):
    clean = default_card()
    for k in SLOT_KEYS:
        v = _rect((card or {}).get(k))
        if v:
            clean[k] = {f: round(v[f], 4) for f in ('x', 'y', 'w', 'h')}
    payload = {'card': clean, 'flip': bool(load_flip() if flip is None else flip)}
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
        'method': 'rect',
        'coarse': coarse_from_patch(g, c),
        'gray': g.ravel(),
        'zgray': znorm(g.ravel()),
        'zedge': znorm(edge_map(g).ravel()),
        'color': c.reshape(-1),
    }


def align_card(G):
    """枠テンプレートに合わせて、カードの位置・大きさ・種別を決める。

    手でドラッグした枠は必ずずれる。枠の絵はカードが変われば変わらないので、
    種別ごとのレンダー中央値がそのままテンプレートになる。アートが見えている
    画素は重みを落としてあるので、絵の中身には引きずられない。
    戻り値はパッチ内の比率で (種別, x0, y0, 大きさ, 1位と2位の差)。
    """
    shapes = index()['shapes']
    base = 1.0 / (1.0 + 2.0 * CARD_MARGIN)

    def at(t, k, tx, ty):
        s = base * k
        x0, y0 = 0.5 + tx * base - s / 2, 0.5 + ty * base - s / 2
        g = G[span(CARD_H, y0, y0 + s, FH)][:, span(CARD_W, x0, x0 + s, FW)].ravel()
        sh = shapes[t]
        w = sh['fweight'].ravel()
        v = float((wznorm(g, w) * sh['zframe'] * w).sum() / (w.sum() + 1e-6))
        return v, x0, y0, s

    best = {}
    for t in shapes:
        top = None
        for k in ALIGN_SCALES:
            for tx in ALIGN_SHIFT:
                for ty in ALIGN_SHIFT:
                    r = at(t, k, tx, ty)
                    if top is None or r[0] > top[0][0]:
                        top = (r, (k, tx, ty))
        best[t] = top

    order = sorted(best.items(), key=lambda kv: -kv[1][0][0])

    # 上位2種別だけ段階的に詰める。詰めてから比べないと、粗い格子の当たり外れが
    # そのまま種別の確信差になってしまう。3位以下は逆転しない。
    def refine(t, top):
        for step in ALIGN_FINE:
            k0, tx0, ty0 = top[1]
            for dk in (-step, 0.0, step):
                for dx in (-step, 0.0, step):
                    for dy in (-step, 0.0, step):
                        r = at(t, k0 + dk, tx0 + dx, ty0 + dy)
                        if r[0] > top[0][0]:
                            top = (r, (k0 + dk, tx0 + dx, ty0 + dy))
        return top

    tops = [(t, refine(t, v)) for t, v in order[:2]]
    tops.sort(key=lambda kv: -kv[1][0][0])
    t1, top = tops[0]
    margin = (top[0][0] - tops[1][1][0][0]) if len(tops) > 1 else 1.0
    r1 = top[0]
    return t1, r1[1], r1[2], r1[3], margin


def shape_descriptors(card_gray_bytes, card_color_bytes):
    """カード全体のパッチから、種別ごとのアート窓パッチを作る。

    どの種別かをクエリ側で当てる必要はない。DB はカードごとに種別を持っているので、
    候補カード自身の種別の窓とマスクで採点すればよい。クエリ側は種別ぶん
    （4通り）パッチを用意しておくだけで済む。枠合わせは種別に関係なく
    1回だけ行い、その結果を全種別の窓に使う。
    """
    shapes = index()['shapes']
    if not shapes:
        raise DBMissing('種別ごとのアート窓がありません。'
                        'python3 build_card_db.py --shapes を実行してください。')
    G = np.frombuffer(card_gray_bytes, dtype=np.uint8).reshape(
        CARD_H, CARD_W).astype(np.float32)
    C = np.frombuffer(card_color_bytes, dtype=np.uint8).reshape(
        CARD_CH, CARD_CW, 3).astype(np.float32)
    ctype, cx0, cy0, cs, tmargin = align_card(G)
    per = {}
    for t, sh in shapes.items():
        wx0, wx1 = cx0 + sh['u0'] * cs, cx0 + sh['u1'] * cs
        wy0, wy1 = cy0 + sh['v0'] * cs, cy0 + sh['v1'] * cs
        g = G[span(CARD_H, wy0, wy1, SH)][:, span(CARD_W, wx0, wx1, SW)]
        c = C[span(CARD_CH, wy0, wy1, SCH)][:, span(CARD_CW, wx0, wx1, SCW)]
        w = sh['mask'].ravel()
        per[t] = {
            'coarse': shape_coarse(g, c, sh['mask'], sh['cmask']),
            'gray': g.ravel(),
            'zgray': wznorm(g.ravel(), w),
            'zedge': wznorm(edge_map(g).ravel(), w),
            'color': c.reshape(-1),
            'w': w, 'wsum': float(w.sum()),
            'cw': np.repeat(sh['cmask'].ravel(), 3),
            'window': [round(wx0, 4), round(wy0, 4), round(wx1, 4), round(wy1, 4)],
        }
        per[t]['cwsum'] = float(per[t]['cw'].sum())
    return {'method': 'shape', 'card': G, 'per': per,
            'ctype': ctype if tmargin >= TYPE_MIN_MARGIN else None,
            'typeMargin': tmargin,
            'rect': [round(cx0, 4), round(cy0, 4), round(cs, 4)]}


def bilinear(a, x0, y0, x1, y1, ow, oh):
    """a の矩形を ow x oh へ双線形で伸縮する。

    マナ結晶のように 20〜30px しかないものを最近傍で伸縮すると列が飛んで、
    テンプレート照合が当たらなくなる。
    """
    h, w = a.shape
    xs = np.clip(np.linspace(x0 * w, x1 * w - 1, ow), 0, w - 1)
    ys = np.clip(np.linspace(y0 * h, y1 * h - 1, oh), 0, h - 1)
    x_i = np.floor(xs).astype(np.int32)
    y_i = np.floor(ys).astype(np.int32)
    x_n = np.minimum(x_i + 1, w - 1)
    y_n = np.minimum(y_i + 1, h - 1)
    fx = (xs - x_i)[None, :]
    fy = (ys - y_i)[:, None]
    top = a[y_i][:, x_i] * (1 - fx) + a[y_i][:, x_n] * fx
    bot = a[y_n][:, x_i] * (1 - fx) + a[y_n][:, x_n] * fx
    return top * (1 - fy) + bot * fy


def shape_gem(gem_bytes, rect):
    """原寸で送られた結晶まわりから、枠合わせの結果で結晶だけを切り出す。"""
    if len(gem_bytes) != GEM_W * GEM_H:
        return b''
    G = np.frombuffer(gem_bytes, dtype=np.uint8).reshape(
        GEM_H, GEM_W).astype(np.float32)
    base = 1.0 / (1.0 + 2.0 * CARD_MARGIN)
    b0 = CARD_MARGIN / (1.0 + 2.0 * CARD_MARGIN)
    # パッチ内の比率 → 校正枠に対する比率
    ax = (rect[0] - b0) / base
    ay = (rect[1] - b0) / base
    az = rect[2] / base
    r = GEM_REL
    gx0, gy0 = ax + r['x'] * az, ay + r['y'] * az
    gx1, gy1 = gx0 + r['w'] * az, gy0 + r['h'] * az
    px0, py0, px1, py1 = GEM_PAD
    u0, u1 = (gx0 - px0) / (px1 - px0), (gx1 - px0) / (px1 - px0)
    v0, v1 = (gy0 - py0) / (py1 - py0), (gy1 - py0) / (py1 - py0)
    if not (0 <= u0 < u1 <= 1 and 0 <= v0 < v1 <= 1):
        return b''
    # 点サンプルだと数字の細い線が飛ぶので、粗く取ってから平均する
    g = block_mean(bilinear(G, u0, v0, u1, v1, MANA_TW * 3, MANA_TH * 3), 3, 3)
    return np.clip(g, 0, 255).astype(np.uint8).tobytes()


def _finish(idx, mask, pool, cand, order1, keep, best, parts, hero,
            correct_id, cost_hint, per_variant, extra):
    """上位を並べて返す。Stage2 の採点方法が違っても結果の形は共通にする。"""
    order2 = np.argsort(-best)
    cands = []
    seen = set()
    prim, allm = arena_maps(hero)
    for j in order2:
        i = int(cand[j])
        # 同じカードが CORE_ / VAN_ など別IDで何枚も入っている。アートが同一なので
        # 区別しようがなく、並べても選べない。名前でまとめて一番良いものだけ出す。
        name = idx['names'][i]
        if name in seen:
            continue
        seen.add(name)
        cid = idx['ids'][i]
        c = {
            'rank': len(cands) + 1,
            'cardId': cid,
            'name': name,
            'cardClass': idx['klass'][i],
            'cost': idx['cost'][i],
            'type': idx['ctype'][i],
            'finalScore': round(float(best[j]), 4),
            'ssim': round(float(parts[j][0]), 4),
            'edge': round(float(parts[j][1]), 4),
            'color': round(float(parts[j][2]), 4),
            'pixel': round(float(parts[j][3]), 4),
            'variant': per_variant(j),
        }
        st = arena_stat(prim, allm, cid)
        if st:
            c['arena'] = st
        if allm.get(cid):
            c['arenaAll'] = allm[cid]
        if prim and prim.get(cid):
            c['arenaClass'] = prim[cid]
        cands.append(c)
        if len(cands) >= STAGE2_RETURN:
            break

    top1 = cands[0]['finalScore'] if cands else 0.0
    gap = (top1 - cands[1]['finalScore']) if len(cands) > 1 else 1.0
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
    res.update(extra)

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
    return res


def recognize(q, hero, want_debug, correct_id, cost_hint=None, arena_only=False,
              type_hint=None):
    if q.get('method') == 'shape':
        return recognize_shape(q, hero, want_debug, correct_id, cost_hint,
                               arena_only, type_hint)
    return recognize_rect(q, hero, want_debug, correct_id, cost_hint, arena_only)


def recognize_shape(q, hero, want_debug, correct_id, cost_hint=None,
                    arena_only=False, type_hint=None):
    """種別ごとのアート窓とマスクで照合する。

    候補カードは自分の種別の窓で採点されるので、クエリの種別を当てる必要がない。
    幾何は実測値で決まっているため、Stage1 に変種は要らない。校正のずれだけを
    Stage2 で小さくずらして吸収する。
    """
    t0 = time.time()
    idx = index()
    shapes = idx['shapes']
    if not shapes:
        raise DBMissing('種別ごとのアート窓がありません。'
                        'python3 build_card_db.py --shapes を実行してください。')
    mask = eligible_mask(idx, hero, arena_only)
    pool = np.flatnonzero(mask)
    if pool.size == 0:
        return {'status': 'empty', 'candidates': []}
    t_mask = time.time()
    log('      候補絞り込み %d枚  %.2fs' % (pool.size, t_mask - t0))

    # ---- Stage1 ----  カードの種別に合った粗特徴どうしを比べる
    s1all = np.full(idx['scoarse'].shape[0], 9e9, dtype=np.float32)
    for t, rows in idx['trows'].items():
        if rows.size == 0:
            continue
        cq = q['per'][t]['coarse']
        for a in range(0, rows.size, 4096):
            r = rows[a:a + 4096]
            s1all[r] = np.abs(idx['scoarse'][r].astype(np.float32) - cq).mean(axis=1)
    s1all = 1.0 - s1all / 255.0
    s1all[~mask] = -1.0
    order1 = np.argsort(-s1all)[:int(pool.size)]
    keep = min(STAGE1_KEEP, order1.size)
    cand = order1[:keep]
    t_s1 = time.time()
    log('      Stage1 完了 → %d枚  %.2fs' % (keep, t_s1 - t_mask))

    # ---- Stage2 ----
    ctypes = idx['stype'][cand]
    groups = {t: np.flatnonzero(ctypes == t) for t in shapes}
    groups = {t: r for t, r in groups.items() if r.size}
    jits = idx['sjitter']
    G = idx['gray'][cand].astype(np.float32)
    C = idx['color'][cand].astype(np.float32)

    def score(rows, t, k):
        sh = shapes[t]
        qd = q['per'][t]
        x0, y0, x1, y1 = art_box(sh, jits[k])
        yi, xi = span(TG, y0, y1, SH), span(TG, x0, x1, SW)
        ci, cj = span(TC, y0, y1, SCH), span(TC, x0, x1, SCW)
        g = G[rows][:, yi][:, :, xi]
        col = C[rows][:, ci][:, :, cj].reshape(len(rows), -1)
        flat = g.reshape(len(rows), -1)
        ef = edge_map(g).reshape(len(rows), -1)
        w, ws = qd['w'], qd['wsum']
        cw, cws = qd['cw'], qd['cwsum']
        ssim = (wznorm(flat, w) * (qd['zgray'] * w)).sum(axis=1) / ws
        edge = (wznorm(ef, w) * (qd['zedge'] * w)).sum(axis=1) / ws
        color = 1.0 - (np.abs(col - qd['color']) * cw).sum(axis=1) / cws / 255.0
        pixel = 1.0 - (np.abs(flat - qd['gray']) * w).sum(axis=1) / ws / 255.0
        f = W_SSIM * ssim + W_EDGE * edge + W_COLOR * color + W_PIXEL * pixel
        return f, np.stack([ssim, edge, color, pixel], axis=1)

    # ずれは3枚とも同じなので、上位だけで1つに決めてから全候補に同じ変種を使う
    kbest, kscore = 0, -9e9
    for k in range(len(jits)):
        top = -9e9
        for t, rows in groups.items():
            probe = rows[:max(1, VARIANT_PROBE // max(1, len(groups)))]
            f, _ = score(probe, t, k)
            top = max(top, float(f.max()))
        if top > kscore:
            kscore, kbest = top, k

    best = np.zeros(len(cand), dtype=np.float32)
    parts = np.zeros((len(cand), 4), dtype=np.float32)
    for t, rows in groups.items():
        f, p = score(rows, t, kbest)
        best[rows] = f
        parts[rows] = p
    # コストは加点に使わない。実機相当だと 1割誤読する一方、アート照合は
    # この方式だと既に Top1 100% で、加点で得られるものがない。表示には使う。
    if type_hint:
        best = best + TYPE_BONUS * (ctypes == type_hint)
    log('      Stage2 完了  %.2fs  (合計 %.2fs)'
        % (time.time() - t_s1, time.time() - t0))

    k, tx, ty = jits[kbest]

    def per_variant(_j):
        return {'scale': round(k, 3), 'ox': round(tx, 3), 'oy': round(ty, 3),
                'h': round(k, 3)}

    extra = {'method': 'shape', 'detectedType': type_hint,
             'jitter': {'scale': k, 'tx': tx, 'ty': ty},
             'cardRect': q.get('rect'),
             'windows': {t: v['window'] for t, v in q['per'].items()}}
    res = _finish(idx, mask, pool, cand, order1, keep, best, parts, hero,
                  correct_id, cost_hint, per_variant, extra)
    if want_debug:
        res['debug'] = {
            'shapes': {t: {'u0': s['u0'], 'v0': s['v0'],
                           'u1': s['u1'], 'v1': s['v1'],
                           'art': [round(v, 4) for v in art_box(s, jits[kbest])]}
                       for t, s in shapes.items()},
            'jitters': len(jits),
            'patch': [SW, SH],
            'weights': {'ssim': W_SSIM, 'edge': W_EDGE,
                        'color': W_COLOR, 'pixel': W_PIXEL},
        }
    return res


def recognize_rect(q, hero, want_debug, correct_id, cost_hint=None, arena_only=False):
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

    best, parts = score(np.arange(m), kbest)
    if cost_hint is not None:
        best = best + COST_BONUS * (idx['costarr'][cand] == cost_hint)
    log('      Stage2 完了  %.2fs  (合計 %.2fs)'
        % (time.time() - t_s1, time.time() - t0))

    s, ox, oy, h = vs[kbest]

    def per_variant(_j):
        return {'scale': round(s, 3), 'ox': round(ox, 3),
                'oy': round(oy, 3), 'h': round(h, 3)}

    res = _finish(idx, mask, pool, cand, order1, keep, best, parts, hero,
                  correct_id, cost_hint, per_variant, {'method': 'rect'})
    if want_debug:
        res['debug'] = {
            'variants': len(vs),
            'calibration': CAL,
            'weights': {'ssim': W_SSIM, 'edge': W_EDGE,
                        'color': W_COLOR, 'pixel': W_PIXEL},
        }
    return res


# ------------------------------------------------------------------ PNG

def png(rgb, w, h, alpha=False):
    """Pillow を使わずに PNG を組み立てる（デバッグ表示用）。"""
    n = 4 if alpha else 3
    raw = b''.join(b'\x00' + rgb[y * w * n:(y + 1) * w * n] for y in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8,
                                         6 if alpha else 2, 0, 0, 0))
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
                return self._json(self._calib())
            if u.path == '/api/cards':
                return self._cards(q)
            if u.path == '/api/arena':
                return self._arena(q)
            if u.path == '/api/deck':
                return self._json(deck_view(load_deck()))
            if u.path == '/api/thumb':
                return self._thumb(q)
            if u.path == '/api/mask':
                return self._mask(q)
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
                     'manaPatch': [MANA_TW, MANA_TH], 'gemRel': GEM_REL,
                     'shapes': sorted(idx['shapes']),
                     'shapePatch': [SW, SH],
                     'cardPatch': [CARD_W, CARD_H, CARD_CW, CARD_CH],
                     'jitters': len(idx['sjitter']),
                     'bytes': os.path.getsize(DB_PATH)})
        pool = arena_pool()
        info['arena'] = ({'total': len(pool['classes'].get('ALL', {})),
                          'ageSec': int(time.time() - pool['fetched'])}
                         if pool else None)
        return self._json(info)

    def _calib(self):
        """カード枠と、そこから割り出した種別ごとのアート窓。"""
        out = {'ok': True, 'card': load_card(), 'flip': load_flip(),
               'gemRel': GEM_REL, 'gemPad': GEM_PAD, 'gemPatch': [GEM_W, GEM_H],
               'cardPatch': [CARD_W, CARD_H, CARD_CW, CARD_CH]}
        try:
            shapes = index()['shapes']
        except (DBMissing, Exception):  # noqa: BLE001 - 校正だけは常に返す
            shapes = {}
        out['shapes'] = {t: {'u0': s['u0'], 'v0': s['v0'],
                             'u1': s['u1'], 'v1': s['v1']}
                         for t, s in shapes.items()}
        out['art'] = load_art()
        return out

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
        """DB側サムネを返す。変種や種別を指定すると照合時と同じ切り出しで返す。"""
        idx = index()
        cid = q.get('id', [''])[0]
        if cid not in idx['byid']:
            return self.send_error(404, 'unknown card')
        i = idx['byid'][cid]
        g = idx['gray'][i].astype(np.float32)
        alpha = None
        if 'ctype' in q:
            # shape 方式で実際に比べた領域。マスクの外は暗く落として見せる
            t = q['ctype'][0]
            sh = idx['shapes'].get(t) or idx['shapes'].get(SHAPE_FALLBACK)
            if sh is None:
                return self.send_error(404, 'unknown shape')
            jit = (float(q.get('k', ['1'])[0]), float(q.get('tx', ['0'])[0]),
                   float(q.get('ty', ['0'])[0]))
            x0, y0, x1, y1 = art_box(sh, jit)
            g = g[span(TG, y0, y1, SH)][:, span(TG, x0, x1, SW)]
            alpha = sh['mask']
        elif 's' in q:
            s = float(q['s'][0])
            ox = float(q.get('ox', ['0'])[0])
            oy = float(q.get('oy', ['0'])[0])
            h = float(q.get('h', [str(s / CAL['aspect'])])[0])
            g = g[_idx_map(TG, oy, h, PH)][:, _idx_map(TG, ox, s, PW)]
        ow = max(8, min(512, int(q.get('w', ['192'])[0])))
        oh = max(8, min(512, int(q.get('h2', [str(int(ow * g.shape[0] / g.shape[1]))])[0])))
        yi = np.clip(np.linspace(0, g.shape[0] - 1, oh), 0, g.shape[0] - 1).astype(np.int32)
        xi = np.clip(np.linspace(0, g.shape[1] - 1, ow), 0, g.shape[1] - 1).astype(np.int32)
        a = g[yi][:, xi]
        if alpha is not None:
            a = a * (0.18 + 0.82 * alpha[yi][:, xi])
        a = a.astype(np.uint8)
        rgb = np.repeat(a[:, :, None], 3, axis=2)
        return self._bin(png(rgb.tobytes(), ow, oh), 'image/png',
                         'public, max-age=3600')

    def _mask(self, q):
        """種別ごとの可視マスク。検証ページがクエリ側にも同じ形をかけるため。

        CSS の mask-image は既定でアルファを見るので、輝度ではなくアルファに入れる。
        """
        sh = index()['shapes'].get(q.get('ctype', [''])[0])
        if sh is None:
            return self.send_error(404, 'unknown shape')
        a = (sh['mask'] * 255).astype(np.uint8)
        rgba = np.empty((SH, SW, 4), np.uint8)
        rgba[..., :3] = 255
        rgba[..., 3] = a
        return self._bin(png(rgba.tobytes(), SW, SH, True), 'image/png',
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
                save_card(body.get('card'), body.get('flip'))
                return self._json(self._calib())
            if u.path == '/api/deck':
                return self._deck(self._body())
            if u.path != '/api/recognize':
                return self._json({'ok': False, 'error': 'not found'}, 404)
            body = self._body()
            hero = str(body.get('hero', '') or '')
            debug = bool(body.get('debug'))
            arena_only = bool(body.get('arenaOnly'))
            method = str(body.get('method') or 'shape')
            queries = body.get('queries') or []
            if not queries:
                return self._json({'ok': False, 'error': 'queries が空です'}, 400)
            if method == 'shape' and not index()['shapes']:
                return self._json(
                    {'ok': False,
                     'error': '種別ごとのアート窓がありません。'
                              'python3 build_card_db.py --shapes を実行してください。'}, 503)

            results = []
            t_all = time.time()
            log('判定 %d枚  hero=%s  方式=%s%s'
                % (len(queries), hero or 'ALL', method,
                   '  アリーナ対象のみ' if arena_only else ''))
            for n, item in enumerate(queries, 1):
                t1 = time.time()
                type_hint = None
                if method == 'shape':
                    cg = base64.b64decode(item.get('card', ''))
                    cc = base64.b64decode(item.get('cardColor', ''))
                    if (len(cg) != CARD_W * CARD_H
                            or len(cc) != CARD_CW * CARD_CH * 3):
                        return self._json(
                            {'ok': False,
                             'error': 'card サイズ不一致 gray=%d(期待%d) color=%d(期待%d)'
                                      % (len(cg), CARD_W * CARD_H,
                                         len(cc), CARD_CW * CARD_CH * 3)}, 400)
                    qd = shape_descriptors(cg, cc)
                    type_hint = qd['ctype']
                    log('      枠合わせ %s  種別 %s (確信差 %.3f)'
                        % (qd['rect'], type_hint or '不明', qd['typeMargin']))
                    gp = base64.b64decode(item.get('gem', '') or '')
                    if gp:
                        item = dict(item, mana=base64.b64encode(
                            shape_gem(gp, qd['rect'])).decode())
                else:
                    gb = base64.b64decode(item.get('gray', ''))
                    cb = base64.b64decode(item.get('color', ''))
                    if len(gb) != PW * PH or len(cb) != (GW * 2) * (GH * 2) * 3:
                        return self._json(
                            {'ok': False,
                             'error': 'query サイズ不一致 gray=%d(期待%d) color=%d(期待%d)'
                                      % (len(gb), PW * PH, len(cb),
                                         (GW * 2) * (GH * 2) * 3)}, 400)
                    qd = query_descriptors(gb, cb)
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
                              cost_hint, arena_only, type_hint)
                top = (r.get('candidates') or [{}])[0]
                log('  [%d/%d] %s -> %s %s  %.2fs'
                    % (n, len(queries), item.get('key', ''), r.get('status'),
                       top.get('name', ''), time.time() - t1))
                r['key'] = item.get('key', '')
                results.append(r)
            log('判定完了  合計 %.2fs' % (time.time() - t_all))

            return self._json({'ok': True, 'results': results,
                               'method': method,
                               'stage1Keep': STAGE1_KEEP,
                               'patch': [PW, PH], 'colorGrid': [GW * 2, GH * 2],
                               'shapePatch': [SW, SH]})
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
            if idx['shapes']:
                print('  アート窓  : %s' % ', '.join(sorted(idx['shapes'])))
            else:
                print('  !! 種別ごとのアート窓がありません。')
                print('  !! python3 build_card_db.py --shapes を実行してください。')
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
