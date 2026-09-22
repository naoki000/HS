#!/usr/bin/env python3
"""Arena Assistant - a-Shell だけで判定する。

ブラウザから叩くと iOS が a-Shell を止めてしまい応答が返らない。
このスクリプトは前面の a-Shell 内で完結するので、その影響を受けない。

使い方
    python3 arena_judge.py 192.168.11.6 ROGUE
    python3 arena_judge.py                  接続先とクラスは前回値を使う

    起動すると待機状態になる。ピックのたびに Enter を押すだけで判定する。
    索引は最初の1回だけ読み込むので、2回目以降はすぐ結果が出る。

    Enter        いまの画面を判定
    r ROGUE      クラスを変更
    h 192.168.1.5  接続先を変更
    q            終了

必要なもの
    Pillow   画像のデコードに使う
    numpy    照合に使う
"""

import io
import sys
import time
import urllib.parse
import urllib.request

try:
    from PIL import Image
except ImportError:
    print('Pillow がありません。  pip install Pillow')
    sys.exit(1)

import start as A

# 段階ごとの進捗は普段は不要。--verbose で戻せる。
A.VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith('-')]

HOST_FILE = 'arena_host.txt'
HEROES = ('DEATHKNIGHT', 'DEMONHUNTER', 'DRUID', 'HUNTER', 'MAGE', 'PALADIN',
          'PRIEST', 'ROGUE', 'SHAMAN', 'WARLOCK', 'WARRIOR')
HS_URL = 'https://hsreplay.net/ja/arena/cards/#text='
HS_SUFFIX = '&view=advanced'
SLOTS = (('left', '左'), ('middle', '中央'), ('right', '右'))


def load_prefs():
    try:
        with open(HOST_FILE, encoding='utf-8') as f:
            parts = f.read().strip().split()
    except OSError:
        return '', ''
    return (parts + ['', ''])[0], (parts + ['', ''])[1]


def save_prefs(host, hero):
    try:
        with open(HOST_FILE, 'w', encoding='utf-8') as f:
            f.write('%s %s' % (host, hero))
    except OSError:
        pass


def norm_host(h):
    h = (h or '').strip().rstrip('/')
    if h and not h.startswith(('http://', 'https://')):
        h = 'http://' + h
    return h


def grab(host):
    url = '%s/frame?t=%d' % (host, int(time.time() * 1000))
    req = urllib.request.Request(url, headers={'User-Agent': 'ArenaJudge'})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert('RGB')
    # 配信が上下逆のときはここで戻す。切り出し座標は触らない。
    if A.load_flip():
        im = im.transpose(Image.ROTATE_180)
    return im


def slot_query(im, rect):
    """校正済みのカード枠で切り出し、認識サーバと同じ形に落とす。

    枠のまわりに余白を付けて渡す。サーバが枠テンプレートで位置を合わせ直す。
    """
    w, h = im.size
    m = A.CARD_MARGIN
    x0 = (rect['x'] - m * rect['w']) * w
    y0 = (rect['y'] - m * rect['h']) * h
    x1 = x0 + rect['w'] * (1 + 2 * m) * w
    y1 = y0 + rect['h'] * (1 + 2 * m) * h
    sub = im.crop((int(x0), int(y0), int(x1), int(y1)))
    g = sub.convert('L').resize((A.CARD_W, A.CARD_H), Image.LANCZOS).tobytes()
    c = sub.resize((A.CARD_CW, A.CARD_CH), Image.LANCZOS).tobytes()
    return g, c


def slot_gem(im, rect):
    """マナ結晶のまわりを原寸で切る。結晶そのものの切り出しはサーバがやる。"""
    w, h = im.size
    p = A.GEM_PAD
    x0 = (rect['x'] + p[0] * rect['w']) * w
    y0 = (rect['y'] + p[1] * rect['h']) * h
    x1 = (rect['x'] + p[2] * rect['w']) * w
    y1 = (rect['y'] + p[3] * rect['h']) * h
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
        return b''
    gem = im.crop((int(x0), int(y0), int(x1), int(y1)))
    return gem.convert('L').resize((A.GEM_W, A.GEM_H), Image.LANCZOS).tobytes()


def judge(host, hero, card):
    t0 = time.time()
    try:
        im = grab(host)
    except Exception as e:  # noqa: BLE001
        print('画面を取得できません: %s' % e)
        print('  %s に配信が出ているか確認してください。' % host)
        return
    print('取得 %dx%d  %.1fs%s' % (im.size[0], im.size[1], time.time() - t0,
                                 '  (180°補正)' if A.load_flip() else ''))

    picks = []
    for key, label in SLOTS:
        rect = card[key]
        cg, cc = slot_query(im, rect)
        q = A.shape_descriptors(cg, cc)
        gem = slot_gem(im, rect)
        cost = A.classify_cost(A.shape_gem(gem, q['rect']))[0] if gem else None
        r = A.recognize(q, hero, False, '', cost, False, q['ctype'])
        cands = r.get('candidates') or []
        head = {'ok': '一致', 'uncertain': '判定不確実',
                'nomatch': '特定できず', 'empty': '候補なし'}[r['status']]
        mana = ('マナ %d' % cost) if cost is not None else 'マナ不明'
        print()
        print('%s  [%s]  %s  %s  1位の差 %.3f'
              % (label, head, q['ctype'] or '種別不明', mana, r.get('gap', 0)))
        for c in cands[:3]:
            print('   %d. %-24s %.3f   ssim %.3f edge %.3f color %.3f'
                  % (c['rank'], c['name'], c['finalScore'],
                     c['ssim'], c['edge'], c['color']))
        if r['status'] != 'nomatch' and cands:
            picks.append(cands[0]['name'])

    print()
    if picks:
        print(HS_URL + urllib.parse.quote(','.join(picks)) + HS_SUFFIX)
    else:
        print('一致するカードを特定できませんでした。')
    print('合計 %.1fs' % (time.time() - t0))


def main():
    host, hero = load_prefs()
    if len(ARGS) > 0:
        host = ARGS[0]
    if len(ARGS) > 1:
        hero = ARGS[1].upper()
    host = norm_host(host)
    if not host:
        print('接続先を指定してください。  python3 arena_judge.py 192.168.11.6 ROGUE')
        return 1
    if hero and hero not in HEROES:
        print('不明なクラス: %s' % hero)
        return 1
    save_prefs(host, hero)

    print('接続先 %s   クラス %s' % (host, hero or '全クラス'))
    print('索引を読み込んでいます...')
    t0 = time.time()
    try:
        idx = A.index()
    except A.DBMissing as e:
        print(e)
        return 1
    print('カード %d 枚 / アート窓 %s  %.1fs'
          % (len(idx['ids']), '・'.join(sorted(idx['shapes'])) or 'なし',
             time.time() - t0))
    if not idx['shapes']:
        print('種別ごとのアート窓がありません。')
        print('  python3 build_card_db.py --shapes を実行してください。')
        return 1
    card = A.load_card()
    print()
    print('Enter で判定 /  r クラス /  h 接続先 /  q 終了')

    while True:
        try:
            cmd = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if cmd in ('q', 'quit', 'exit'):
            return 0
        if cmd.startswith('r'):
            v = cmd[1:].strip().upper()
            if v in HEROES or v == '':
                hero = v
                save_prefs(host, hero)
                print('クラス: %s' % (hero or '全クラス'))
            else:
                print('不明なクラス: %s' % v)
            continue
        if cmd.startswith('h'):
            v = norm_host(cmd[1:])
            if v:
                host = v
                save_prefs(host, hero)
                print('接続先: %s' % host)
            continue
        art = A.load_card()
        judge(host, hero, art)

if __name__ == '__main__':
    sys.exit(main())
