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

import arena_server as A

# 段階ごとの進捗は普段は不要。--verbose で戻せる。
A.VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith('-')]

HOST_FILE = 'arena_host.txt'
HEROES = ('DEATHKNIGHT', 'DEMONHUNTER', 'DRUID', 'HUNTER', 'MAGE', 'PALADIN',
          'PRIEST', 'ROGUE', 'SHAMAN', 'WARLOCK', 'WARRIOR')
HS_URL = 'https://hsreplay.net/ja/arena/cards/#text='
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
    return im.convert('RGB')


def slot_query(im, rect):
    """校正済みの矩形で切り出し、認識サーバと同じ形に落とす。"""
    w, h = im.size
    box = (int(rect['x'] * w), int(rect['y'] * h),
           int((rect['x'] + rect['w']) * w), int((rect['y'] + rect['h']) * h))
    sub = im.crop(box)
    g = sub.convert('L').resize((A.PW, A.PH), Image.LANCZOS).tobytes()
    c = sub.resize((A.GW * 2, A.GH * 2), Image.LANCZOS).tobytes()
    return A.query_descriptors(g, c)


def judge(host, hero, art):
    t0 = time.time()
    try:
        im = grab(host)
    except Exception as e:  # noqa: BLE001
        print('画面を取得できません: %s' % e)
        print('  %s に配信が出ているか確認してください。' % host)
        return
    print('取得 %dx%d  %.1fs' % (im.size[0], im.size[1], time.time() - t0))

    picks = []
    for key, label in SLOTS:
        r = A.recognize(slot_query(im, art[key]), hero, False, '')
        cands = r.get('candidates') or []
        head = {'ok': '一致', 'uncertain': '判定不確実',
                'nomatch': '特定できず', 'empty': '候補なし'}[r['status']]
        print()
        print('%s  [%s]  1位と2位の差 %.3f' % (label, head, r.get('gap', 0)))
        for c in cands[:3]:
            print('   %d. %-24s %.3f   ssim %.3f edge %.3f color %.3f'
                  % (c['rank'], c['name'], c['finalScore'],
                     c['ssim'], c['edge'], c['color']))
        if r['status'] != 'nomatch' and cands:
            picks.append(cands[0]['name'])

    print()
    if picks:
        print(HS_URL + urllib.parse.quote(','.join(picks)))
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
    print('カード %d 枚 / 変種 %d 通り  %.1fs'
          % (len(idx['ids']), len(idx['variants']), time.time() - t0))
    art = A.load_art()
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
        art = A.load_art()
        judge(host, hero, art)


if __name__ == '__main__':
    sys.exit(main())
