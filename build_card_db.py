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
    from start import TG, TC, MANA_TW, MANA_TH
except ImportError:
    TG, TC, MANA_TW, MANA_TH = 64, 16, 28, 32

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
    size = os.path.getsize(args.db) if os.path.exists(args.db) else 0
    print('=' * 52)
    print('  総カード数   : %d' % total)
    print('  今回成功     : %d' % ok)
    print('  スキップ     : %d  (登録済み・今回対象外)' % skipped)
    print('  今回失敗     : %d' % ng)
    print('  DB登録済み   : %d' % have)
    print('  マナテンプレート : %d 種' % mana)
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
