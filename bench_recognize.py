#!/usr/bin/env python3
"""Arena Assistant - 認識方式の比較検証。

素のアート画像を照合すると精度は出て当たり前で、実機とはかけ離れている。
ここではカードレンダーを実機くらいの大きさまで落としてから切り出し、
情報量が減った状態で方式を比べる。

    python bench_recognize.py --n 80
    python bench_recognize.py --n 80 --card-h 140 --jpeg 45 --blur 1.2
    python bench_recognize.py --n 40 --methods shape

レンダーは _bench_cache/ に残すので、2回目以降はネットワークを使わない。
実機スクリーンショットがあるなら arena_test.html で確認する方が確実。
これはあくまで方式どうしの相対比較用。
"""

import argparse
import io
import os
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image, ImageFilter
except ImportError:
    print('Pillow がありません。  pip install Pillow')
    sys.exit(1)

import start as A

CACHE_DIR = '_bench_cache'
RENDER_URL = 'https://art.hearthstonejson.com/v1/render/latest/{loc}/256x/{id}.png'
UA = {'User-Agent': 'ArenaAssistant/bench'}


def render_of(cid, locale='jaJP'):
    path = os.path.join(CACHE_DIR, '%s.png' % cid)
    if not os.path.exists(path):
        os.makedirs(CACHE_DIR, exist_ok=True)
        req = urllib.request.Request(RENDER_URL.format(loc=locale, id=cid),
                                     headers=UA)
        data = urllib.request.urlopen(req, timeout=60).read()
        with open(path, 'wb') as f:
            f.write(data)
    im = Image.open(path)
    im.load()
    return im.convert('RGBA')


def fake_screen(card_im, rect, size, rng, args, tone):
    """カードを1枚だけ置いた疑似ゲーム画面を作る。

    実機で情報が減る原因は、解像度・にじみ・JPEG・色味に加えて、
    ドラフト画面でカードが少し傾いていることと、スポットライトのむら。
    切り抜き枠を軽度に外すのも実際に起きるので、それも別で入れる。
    """
    w, h = size
    bg = Image.new('RGB', (w, h), (26, 20, 16))
    px = bg.load()
    for y in range(0, h, 8):
        for x in range(0, w, 8):
            v = rng.randint(-8, 8)
            for dy in range(8):
                for dx in range(8):
                    if x + dx < w and y + dy < h:
                        px[x + dx, y + dy] = (40 + v, 30 + v, 22 + v)

    cw, ch = max(8, int(rect['w'] * w)), max(8, int(rect['h'] * h))
    card = card_im.resize((cw * 2, ch * 2), Image.LANCZOS)
    if args.rotate:
        card = card.rotate(rng.uniform(-args.rotate, args.rotate),
                           Image.BICUBIC, expand=False)
    card = card.resize((cw, ch), Image.LANCZOS)
    bg.paste(card, (int(rect['x'] * w), int(rect['y'] * h)), card)

    if args.shade:
        # ドラフト画面の光は一様ではなく、カードの中でも明るさが傾く
        gr = Image.linear_gradient('L').rotate(rng.uniform(0, 360),
                                               Image.BILINEAR).resize((w, h))
        a = args.shade
        bg = Image.composite(Image.eval(bg, lambda v: min(255, int(v * (1 + a)))),
                             Image.eval(bg, lambda v: int(v * (1 - a))), gr)
    if args.blur > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(args.blur))
    if tone:
        g, b_, c = tone
        bg = Image.eval(bg, lambda v: min(255, max(0, int((v / 255.0) ** g * 255 * c + b_))))
    if args.jpeg:
        buf = io.BytesIO()
        bg.save(buf, 'JPEG', quality=args.jpeg)
        buf.seek(0)
        bg = Image.open(buf)
        bg.load()
    return bg.convert('RGB')


def crop(im, rect):
    w, h = im.size
    return im.crop((int(rect['x'] * w), int(rect['y'] * h),
                    int((rect['x'] + rect['w']) * w),
                    int((rect['y'] + rect['h']) * h)))


def shape_query(im, card_rect):
    """arena_assistant.html の shape 方式と同じ切り出し。

    枚を少し広めに送る。サーバが枠テンプレートで合わせ直す余地を残すため。
    """
    m = A.CARD_MARGIN
    big = {'x': card_rect['x'] - m * card_rect['w'],
           'y': card_rect['y'] - m * card_rect['h'],
           'w': card_rect['w'] * (1 + 2 * m),
           'h': card_rect['h'] * (1 + 2 * m)}
    sub = crop(im, big)
    g = sub.convert('L').resize((A.CARD_W, A.CARD_H), Image.LANCZOS).tobytes()
    c = sub.resize((A.CARD_CW, A.CARD_CH), Image.LANCZOS).tobytes()
    return g, c


def rect_query(im, card_rect):
    """従来方式。カード枠から割り出したアート矩形を 48x32 に潰す。"""
    art = A.sub_rect(card_rect, A.art_rel())
    sub = crop(im, art)
    g = sub.convert('L').resize((A.PW, A.PH), Image.LANCZOS).tobytes()
    c = sub.resize((A.GW * 2, A.GH * 2), Image.LANCZOS).tobytes()
    return A.query_descriptors(g, c)


def gem_bytes(im, card_rect):
    """結晶のまわりを広めに原寸で切る。切り直しはサーバがやる。"""
    p = A.GEM_PAD
    r = crop(im, {'x': card_rect['x'] + p[0] * card_rect['w'],
                  'y': card_rect['y'] + p[1] * card_rect['h'],
                  'w': (p[2] - p[0]) * card_rect['w'],
                  'h': (p[3] - p[1]) * card_rect['h']})
    return r.convert('L').resize((A.GEM_W, A.GEM_H), Image.LANCZOS).tobytes()


def main():
    ap = argparse.ArgumentParser(description='認識方式を比べる')
    ap.add_argument('--n', type=int, default=60, help='試すカード枚数')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--screen', default='1280x960', help='疑似画面の大きさ')
    ap.add_argument('--card-h', type=int, default=0,
                    help='画面上のカードの高さ(px)。0 なら既定の校正値どおり')
    ap.add_argument('--blur', type=float, default=0.8)
    ap.add_argument('--jpeg', type=int, default=55, help='0 で JPEG 劣化なし')
    ap.add_argument('--miscal', type=float, default=0.012,
                    help='枠のずれ（カードの大きさに対する比率）')
    ap.add_argument('--rotate', type=float, default=1.5, help='カードの傾き(度)')
    ap.add_argument('--shade', type=float, default=0.12, help='明るさのむら')
    ap.add_argument('--hero', default='', help='クラス絞り込み')
    ap.add_argument('--arena-only', action='store_true')
    ap.add_argument('--mana', action='store_true', help='マナ加点も使う')
    ap.add_argument('--methods', default='rect,shape')
    ap.add_argument('--types', default='MINION,SPELL,WEAPON')
    args = ap.parse_args()

    A.VERBOSE = False
    rng = random.Random(args.seed)
    sw, sh = (int(v) for v in args.screen.lower().split('x'))

    idx = A.index()
    want = set(args.types.split(','))
    # アリーナ絞り込みを使うなら、出題もアリーナ対象から選ばないと必ず外れる
    pool_ids = None
    if args.arena_only:
        prim, allm = A.arena_maps(args.hero)
        pool_ids = set(prim or allm)
    picks = [i for i, t in enumerate(idx['ctype'])
             if t in want and idx['draftable'][i]
             and (pool_ids is None or idx['ids'][i] in pool_ids)]
    rng.shuffle(picks)
    picks = picks[:args.n]
    ids = [idx['ids'][i] for i in picks]

    print('DB %d枚 / 試行 %d枚 / 画面 %dx%d / blur %.1f / JPEG %s'
          ' / 傾き %.1f° / 枠ずれ %.1f%%'
          % (len(idx['ids']), len(ids), sw, sh, args.blur, args.jpeg or 'なし',
             args.rotate, 100 * args.miscal))

    base = A.default_card()['middle']
    # カードの縦横比は 256:388 で決まっている。疑似画面では必ずそれに合わせる。
    # 伸びた状態で貼ると、方式の差ではなく歪みを測ることになる。
    if args.card_h:
        base = dict(base, h=args.card_h / float(sh))
    base['w'] = base['h'] * (388.0 / 256.0) ** -1 * (sh / float(sw))
    ch_px = int(base['h'] * sh)
    print('カード %dx%dpx（アート窓 %dpx幅）'
          % (int(base['w'] * sw), ch_px,
             int(base['w'] * sw * (idx['shapes']['MINION']['u1']
                                   - idx['shapes']['MINION']['u0']))))

    print('  レンダーを用意中...')
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda c: render_of(c), ids))

    methods = args.methods.split(',')
    stat = {m: {'top1': 0, 'top3': 0, 'id1': 0, 'ms': 0.0, 'bytype': {}}
            for m in methods}
    typed = {'hit': 0, 'miss': 0, 'none': 0}

    for n, i in enumerate(picks, 1):
        cid = idx['ids'][i]
        ctype = idx['ctype'][i]
        card = render_of(cid)
        d = args.miscal
        truth = base
        shown = {'x': base['x'] + rng.uniform(-d, d) * base['w'],
                 'y': base['y'] + rng.uniform(-d, d) * base['h'],
                 'w': base['w'] * (1 + rng.uniform(-d, d)),
                 'h': base['h'] * (1 + rng.uniform(-d, d))}
        tone = (rng.uniform(0.9, 1.12), rng.uniform(-10, 10), rng.uniform(0.92, 1.08))
        im = fake_screen(card, truth, (sw, sh), rng, args, tone)
        gem = gem_bytes(im, shown) if args.mana else b''
        cost = None

        for m in methods:
            t0 = time.time()
            if m == 'shape':
                cg, cc = shape_query(im, shown)
                q = A.shape_descriptors(cg, cc)
                th = q['ctype']
                if th is None:
                    typed['none'] += 1
                elif th == ctype:
                    typed['hit'] += 1
                else:
                    typed['miss'] += 1
                c2 = (A.classify_cost(A.shape_gem(gem, q['rect']))[0]
                      if args.mana else None)
                r = A.recognize(q, args.hero, False, '', c2, args.arena_only, th)
            else:
                q = rect_query(im, shown)
                r = A.recognize(q, args.hero, False, '', cost, args.arena_only)
            ms = (time.time() - t0) * 1000
            cands = r.get('candidates') or []
            # 同じカードが CORE_ / VAN_ などの別 ID で何枚も入っている。
            # アートが同一なので ID では区別しようがなく、実用上も名前が合えば正解。
            names = [c['name'] for c in cands]
            truth_name = idx['names'][i]
            s = stat[m]
            s['ms'] += ms
            bt = s['bytype'].setdefault(ctype, [0, 0, 0])
            bt[2] += 1
            if cands and cands[0]['cardId'] == cid:
                s['id1'] += 1
            if names[:1] == [truth_name]:
                s['top1'] += 1
                bt[0] += 1
            if truth_name in names[:3]:
                s['top3'] += 1
                bt[1] += 1
        if n % 10 == 0 or n == len(picks):
            line = '  %3d/%d  ' % (n, len(picks))
            line += '  '.join('%s Top1 %.0f%%' % (m, 100.0 * stat[m]['top1'] / n)
                              for m in methods)
            print(line, flush=True)

    n = len(picks)
    print()
    print('=' * 64)
    print('%-7s %9s %9s %10s %10s' % ('方式', 'Top1(名)', 'Top3(名)', 'Top1(ID)',
                                     '1枚あたり'))
    for m in methods:
        s = stat[m]
        print('%-7s %8.1f%% %8.1f%% %9.1f%% %8.0f ms'
              % (m, 100.0 * s['top1'] / n, 100.0 * s['top3'] / n,
                 100.0 * s['id1'] / n, s['ms'] / n))
    print()
    for m in methods:
        parts = ['%s %d/%d' % (t, v[0], v[2])
                 for t, v in sorted(stat[m]['bytype'].items())]
        print('  %-7s 種別ごとの Top1 : %s' % (m, '  '.join(parts)))
    if 'shape' in methods:
        print('  種別判定 : 正解 %d / 誤り %d / 判定せず %d'
              % (typed['hit'], typed['miss'], typed['none']))
    print('=' * 64)
    return 0


if __name__ == '__main__':
    sys.exit(main())
