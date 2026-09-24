# -*- coding: utf-8 -*-
"""Arena Assistant を Pythonista 1つにまとめたもの。

これまで3つに分かれていたものを1本にする。

    arena_judge.py        a-Shell で判定を回す部分
    arena_assistant.html  ブラウザの操作画面
    start.py              認識エンジン（HTTP サーバ兼ライブラリ）

画面の取り込みは**やらない**。ReplayKit も ScreenCaptureKit も
Pythonista からは使えないことが実機で確定したため、
画面はキャスト系アプリが配信している URL から貰う。

    Hearthstone --(キャストアプリ)--> http://<iPad>:port/...  --> ここ

start.py はそのまま読み込んで使う。認識の実装を二重に持つと必ずずれるので、
アルゴリズムには一切触らない。

必要なもの（すべて親フォルダに置く）
    start.py
    arena_features.sqlite3
    arena_pool.json          無ければ起動時に取りに行く
    arena_deck.json          無ければ空で始まる
"""

import io
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

import ui
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import start as A                                            # noqa: E402
# 切り出しの寸法合わせは a-Shell 版と1つの実装を共有する。
# ここで書き直すと必ずずれるので、import して使う。
import arena_judge as J                                      # noqa: E402

# start.py のパスはすべて相対。Pythonista の CWD はスクリプトの場所なので
# 親フォルダを指すように直す。chdir はしない（他に影響するため）。
for _name in ('DB_PATH', 'CACHE_PATH', 'SHAPE_CACHE_PATH', 'CALIB_PATH',
              'DECK_PATH', 'ARENA_POOL_PATH'):
    setattr(A, _name, os.path.join(ROOT, getattr(A, _name)))

A.VERBOSE = False

SOURCE_PATH = os.path.join(ROOT, 'arena_source.json')

# ホストだけ渡されたときに試すパス。
# 旧 arena_judge.py と start.py の /castframe は '/frame' 決め打ちだったが、
# キャスト系アプリはアプリごとに違うので、順に叩いて探す。
CANDIDATE_PATHS = ('/frame', '/snapshot.jpg', '/shot.jpg', '/screenshot.jpg',
                   '/capture.jpg', '/image.jpg', '/live.jpg',
                   '/video', '/stream', '/stream.mjpg', '/mjpeg', '/')
HS_URL = 'https://hsreplay.net/ja/arena/cards/#text='
HS_SUFFIX = '&view=advanced'
SLOTS = (('left', '左'), ('middle', '中央'), ('right', '右'))
HEROES = ('', 'DEATHKNIGHT', 'DEMONHUNTER', 'DRUID', 'HUNTER', 'MAGE',
          'PALADIN', 'PRIEST', 'ROGUE', 'SHAMAN', 'WARLOCK', 'WARRIOR')

BG = '#14161c'
CARD_BG = '#1e2230'
FG = '#e8ecf4'
DIM = '#8b93a7'
OK = '#5ad18b'
NG = '#ef6d6d'
TOP = '#ffd166'

STATUS_TEXT = {'ok': '一致', 'uncertain': '判定不確実',
               'nomatch': '特定できず', 'empty': '候補なし'}


# ----------------------------------------------------------------- 画面の取得

def load_source():
    try:
        with open(SOURCE_PATH, encoding='utf-8') as f:
            return str(json.load(f).get('url') or '')
    except (OSError, ValueError):
        return ''


def save_source(url):
    with open(SOURCE_PATH, 'w', encoding='utf-8') as f:
        json.dump({'url': url}, f, ensure_ascii=False)


def _read_mjpeg(resp, boundary):
    """multipart/x-mixed-replace から1コマだけ取り出す。"""
    mark = b'--' + boundary.encode('ascii')
    buf = b''
    while len(buf) < 12 * 1024 * 1024:
        chunk = resp.read(8192)
        if not chunk:
            break
        buf += chunk
        start = buf.find(mark)
        if start < 0:
            continue
        head_end = buf.find(b'\r\n\r\n', start)
        if head_end < 0:
            continue
        body = head_end + 4
        nxt = buf.find(mark, body)
        if nxt >= 0:
            return buf[body:nxt]
    raise ValueError('MJPEG のコマを取り出せませんでした')


def norm_url(s):
    """'192.168.11.6' のような入力でも通るようにする。"""
    s = (s or '').strip()
    if not s:
        return ''
    if not s.startswith(('http://', 'https://')):
        s = 'http://' + s
    return s.rstrip('/')


def _open(url, timeout=8):
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(
        '%s%st=%d' % (url, sep, int(time.time() * 1000)),
        headers={'User-Agent': 'ArenaAssistant'})
    return urllib.request.urlopen(req, timeout=timeout)


def _read_frame(resp):
    """応答から画像1枚ぶんのバイト列を取り出す。"""
    ctype = resp.headers.get('Content-Type', '')
    if 'multipart/' in ctype:
        boundary = ''
        for part in ctype.split(';'):
            part = part.strip()
            if part.startswith('boundary='):
                boundary = part[9:].strip('"')
        if not boundary:
            raise ValueError('boundary がありません: %s' % ctype)
        return _read_mjpeg(resp, boundary)
    if 'text/' in ctype or 'json' in ctype:
        raise ValueError('画像ではありません (%s)' % ctype)
    return resp.read()


def probe_source(url):
    """パスが省略されていたら、配信していそうなパスを順に試す。

    どれが当たったかを返す。当たらなければ、試した結果をまとめて例外にする。
    """
    base = norm_url(url)
    if not base:
        raise ValueError('URL が空です')
    rest = base.split('://', 1)[1]
    if '/' in rest:                      # パスまで書いてあるならそのまま使う
        return base

    tried = []
    for path in CANDIDATE_PATHS:
        cand = base + path
        try:
            with _open(cand, timeout=4) as r:
                data = _read_frame(r)
            Image.open(io.BytesIO(data)).load()
            return cand
        except urllib.error.HTTPError as e:
            tried.append('%s -> %d' % (path, e.code))
        except Exception as e:           # noqa: BLE001
            tried.append('%s -> %s' % (path, type(e).__name__))
    raise ValueError('配信が見つかりません。試したパス:\n  '
                     + '\n  '.join(tried))


def grab(url):
    """1コマ取る。ふつうの JPEG でも MJPEG でも受ける。"""
    with _open(url) as r:
        data = _read_frame(r)
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert('RGB')
    if A.load_flip():
        im = im.transpose(Image.ROTATE_180)
    return im


# --------------------------------------------------------------------- 判定

def judge(im, hero, card):
    """3枠ぶん判定する。**UI スレッド以外から呼ぶ。**"""
    out = []
    for key, label_text in SLOTS:
        rect = card[key]
        cg, cc = J.slot_query(im, rect)
        q = A.shape_descriptors(cg, cc)
        gem = J.slot_gem(im, rect)
        cost = A.classify_cost(A.shape_gem(gem, q['rect']))[0] if gem else None
        r = A.recognize(q, hero, False, '', cost, False, q['ctype'])
        r.update({'key': key, 'label': label_text, 'cost': cost,
                  'ctype': q['ctype'] or ''})
        out.append(r)
    return out


def win_rates(cand, hero):
    """(ヒーロー勝率, 全体勝率)。無ければ None。"""
    if not cand:
        return None, None
    cls = (cand.get('arenaClass') or {}).get('win') if hero else None
    allw = (cand.get('arenaAll') or {}).get('win')
    if cls is None and hero:
        cls = (cand.get('arena') or {}).get('win')
    return cls, allw


def pct(v):
    return '—' if v is None else '%.1f%%' % (v * 100 if v <= 1 else v)


# --------------------------------------------------------------- 画面の部品

def label(text, size=14, color=FG, align=ui.ALIGN_LEFT):
    lb = ui.Label()
    lb.text = text
    lb.font = ('<System>', size)
    lb.text_color = color
    lb.alignment = align
    lb.number_of_lines = 0
    return lb


def button(title, action, bg='#2d3550'):
    b = ui.Button()
    b.title = title
    b.action = action
    b.font = ('<System-Bold>', 14)
    b.tint_color = FG
    b.background_color = bg
    b.corner_radius = 8
    return b


class Overlay(ui.View):
    """フレームの上にカード枠を描き、ドラッグで位置を合わせる。"""

    def __init__(self, on_change):
        super().__init__()
        self.background_color = 'clear'
        self.rects = {}
        self.active = 'left'
        self.on_change = on_change
        self._drag = None

    def draw(self):
        for key, name in SLOTS:
            r = self.rects.get(key)
            if not r:
                continue
            path = ui.Path.rect(r['x'] * self.width, r['y'] * self.height,
                                r['w'] * self.width, r['h'] * self.height)
            path.line_width = 2
            ui.set_color(TOP if key == self.active else OK)
            path.stroke()

    def touch_began(self, t):
        self._drag = (t.location.x / max(1, self.width),
                      t.location.y / max(1, self.height))

    def touch_moved(self, t):
        if not self._drag:
            return
        self._apply(t)

    def touch_ended(self, t):
        if not self._drag:
            return
        self._apply(t)
        self._drag = None
        self.on_change(self.rects)

    def _apply(self, t):
        x0, y0 = self._drag
        x1 = min(1.0, max(0.0, t.location.x / max(1, self.width)))
        y1 = min(1.0, max(0.0, t.location.y / max(1, self.height)))
        self.rects[self.active] = {'x': min(x0, x1), 'y': min(y0, y1),
                                   'w': abs(x1 - x0), 'h': abs(y1 - y0)}
        self.set_needs_display()


class QuickRow(ui.View):
    """簡易版の1枠。名前とヒーロー勝率・全体勝率、保存ボタンだけ。"""

    def __init__(self, label_text, on_save):
        super().__init__()
        self.background_color = CARD_BG
        self.corner_radius = 10
        self.border_width = 2
        self.border_color = 'clear'
        self.slot = label(label_text, 12, DIM)
        # ui.View.name はタイトル用の文字列プロパティ。ここで潰さない
        self.card_name = label('—', 15)
        self.hero = label('—', 13)
        self.all = label('—', 13, DIM)
        self.save = button('保存', on_save)
        for v in (self.slot, self.card_name, self.hero, self.all, self.save):
            self.add_subview(v)

    def layout(self):
        w = self.width - 16
        self.slot.frame = (8, 6, w, 14)
        self.card_name.frame = (8, 22, w, 20)
        self.hero.frame = (8, 44, w, 18)
        self.all.frame = (8, 62, w, 18)
        self.save.frame = (8, self.height - 34, w, 28)


# ------------------------------------------------------------------ 本体

class ArenaView(ui.View):

    def __init__(self):
        super().__init__()
        self.name = 'Arena Assistant'
        self.background_color = BG

        self.hero = A.load_deck().get('hero') or ''
        self.card = A.load_card()
        self.results = []
        self.image = None
        self.busy = False
        self._queue = []
        self._qlock = threading.Lock()
        self._ready = False
        self._resolved = None
        self._source_text = load_source()

        self.status = label('索引を読み込んでいます…', 13, DIM)

        self.url_field = ui.TextField()
        self.url_field.text = load_source()
        self.url_field.placeholder = 'キャスト配信の URL'
        self.url_field.background_color = CARD_BG
        self.url_field.text_color = FG
        self.url_field.bordered = False
        self.url_field.corner_radius = 8
        self.url_field.autocapitalization_type = ui.AUTOCAPITALIZE_NONE
        self.url_field.autocorrection_type = False

        self.hero_btn = button('クラス: %s' % (self.hero or '全体'),
                               self.pick_hero)
        self.go = button('判定', self.on_judge, '#3c5bd6')

        self.quick = [QuickRow(lb, self._saver(i))
                      for i, (_, lb) in enumerate(SLOTS)]

        self.deck_title = label('保存したカード', 13, DIM)
        self.deck = label('—', 13)

        self.shot = ui.ImageView()
        self.shot.content_mode = ui.CONTENT_SCALE_ASPECT_FIT
        self.shot.background_color = CARD_BG
        self.overlay = Overlay(self.on_rects)
        self.overlay.rects = self.card

        self.slot_btn = button('調整する枠: 左', self.pick_slot)
        self.save_cal = button('枠を保存', self.on_save_cal)
        self.flip_btn = button('上下反転: %s' % ('する' if A.load_flip()
                                                else 'しない'), self.on_flip)

        self.detail_title = label('詳細', 13, DIM)
        self.detail = label('—', 12, DIM)
        self.link = button('HSReplay で開く', self.on_link, '#2d3550')

        self.scroll = ui.ScrollView()
        self.scroll.background_color = BG
        for v in (self.status, self.url_field, self.hero_btn, self.go,
                  self.deck_title, self.deck, self.shot, self.overlay,
                  self.slot_btn, self.save_cal, self.flip_btn,
                  self.detail_title, self.detail, self.link):
            self.scroll.add_subview(v)
        for q in self.quick:
            self.scroll.add_subview(q)
        self.add_subview(self.scroll)

        threading.Thread(target=self._warmup, daemon=True).start()
        self.refresh_deck()

    # ------------------------------------------------------------- 配置

    def layout(self):
        self.scroll.frame = (0, 0, self.width, self.height)
        pad, w = 12, self.width - 24
        y = 12

        self.status.frame = (pad, y, w, 32)
        y += 38
        self.url_field.frame = (pad, y, w, 34)
        y += 42
        half = (w - 8) / 2
        self.hero_btn.frame = (pad, y, half, 36)
        self.go.frame = (pad + half + 8, y, half, 36)
        y += 46

        # 簡易版。SP 幅でも3列を保つ
        qw = (w - 16) / 3
        for i, q in enumerate(self.quick):
            q.frame = (pad + i * (qw + 8), y, qw, 132)
        y += 142

        self.deck_title.frame = (pad, y, w, 18)
        y += 20
        dh = max(20, self.deck.text.count('\n') * 18 + 20)
        self.deck.frame = (pad, y, w, dh)
        y += dh + 16

        ih = w * 0.62
        self.shot.frame = (pad, y, w, ih)
        self.overlay.frame = (pad, y, w, ih)
        y += ih + 8
        third = (w - 16) / 3
        self.slot_btn.frame = (pad, y, third, 34)
        self.save_cal.frame = (pad + third + 8, y, third, 34)
        self.flip_btn.frame = (pad + 2 * (third + 8), y, third, 34)
        y += 44

        self.detail_title.frame = (pad, y, w, 18)
        y += 20
        dh = max(20, self.detail.text.count('\n') * 16 + 20)
        self.detail.frame = (pad, y, w, dh)
        y += dh + 12
        self.link.frame = (pad, y, w, 38)
        y += 50

        self.scroll.content_size = (self.width, y)

    # ------------------------------------------------------------ 準備

    def _post(self, kind, payload):
        """別スレッドからの連絡。索引の読み込み完了と判定完了が重なるので
        1枠だと取りこぼす。"""
        with self._qlock:
            self._queue.append((kind, payload))

    def _warmup(self):
        """索引の読み込みは重い。UI を止めないよう別スレッドでやる。"""
        try:
            idx = A.index()
        except Exception as e:      # noqa: BLE001
            self._post('error', '索引を読めません: %s' % e)
            return
        if not idx['shapes']:
            self._post('error',
                       'アート窓がありません。build_card_db.py --shapes')
            return
        A.arena_pool()
        self._post('ready', 'カード %d 枚 / 窓 %s'
                   % (len(idx['ids']), '・'.join(sorted(idx['shapes']))))

    # ------------------------------------------------------------ 操作

    def pick_hero(self, sender):
        i = HEROES.index(self.hero) if self.hero in HEROES else 0
        self.hero = HEROES[(i + 1) % len(HEROES)]
        self.hero_btn.title = 'クラス: %s' % (self.hero or '全体')
        deck = A.load_deck()
        deck['hero'] = self.hero
        A.save_deck(deck)
        self.refresh_deck()

    def pick_slot(self, sender):
        keys = [k for k, _ in SLOTS]
        i = keys.index(self.overlay.active)
        self.overlay.active = keys[(i + 1) % len(keys)]
        self.slot_btn.title = '調整する枠: %s' % dict(SLOTS)[self.overlay.active]
        self.overlay.set_needs_display()

    def on_rects(self, rects):
        self.card = {k: rects.get(k) or self.card[k] for k, _ in SLOTS}

    def on_save_cal(self, sender):
        A.save_card(self.card)
        self.status.text = '枠を保存しました'

    def on_flip(self, sender):
        flip = not A.load_flip()
        A.save_card(self.card, flip)
        self.flip_btn.title = '上下反転: %s' % ('する' if flip else 'しない')

    def on_link(self, sender):
        names = [r['candidates'][0]['name'] for r in self.results
                 if r.get('candidates') and r['status'] != 'nomatch']
        if not names:
            self.status.text = '開けるカードがありません'
            return
        import webbrowser
        webbrowser.open(HS_URL + urllib.parse.quote(','.join(names))
                        + HS_SUFFIX)

    def _saver(self, i):
        def act(sender):
            if i >= len(self.results):
                return
            cands = self.results[i].get('candidates') or []
            if not cands:
                return
            deck = A.load_deck()
            deck['hero'] = self.hero
            deck['picks'].append(cands[0]['cardId'])
            A.save_deck(deck)
            self.refresh_deck()
            self.status.text = '%s を保存しました' % cands[0]['name']
        return act

    def on_judge(self, sender):
        if self.busy:
            return
        if not self._ready:
            self.status.text = 'まだ索引を読み込み中です'
            return
        url = (self.url_field.text or '').strip()
        if not url:
            self.status.text = '配信の URL を入れてください'
            return
        if norm_url(url) != norm_url(self._source_text):
            self._resolved = None          # 入力が変わったら探し直す
        self._source_text = url
        save_source(url)
        self.busy = True
        self.status.text = ('取得して判定しています…' if self._resolved
                            else '配信を探しています…')
        threading.Thread(target=self._judge_worker, args=(url,),
                         daemon=True).start()

    def _judge_worker(self, url):
        """numpy と PIL だけ。ObjC には触らないので別スレッドで安全。"""
        t0 = time.time()
        try:
            if not self._resolved:
                self._resolved = probe_source(url)
            im = grab(self._resolved)
            res = judge(im, self.hero, self.card)
        except Exception as e:      # noqa: BLE001
            self._resolved = None
            self._post('error', '%s: %s' % (type(e).__name__, e))
            return
        buf = io.BytesIO()
        im.resize((im.size[0] // 2, im.size[1] // 2),
                  Image.BILINEAR).save(buf, 'JPEG', quality=70)
        self._post('done', (res, buf.getvalue(), time.time() - t0,
                            self._resolved))

    # ------------------------------------------------------------ 更新

    def update(self):
        if not self.on_screen:
            return
        with self._qlock:
            jobs, self._queue = self._queue, []
        for job in jobs:
            self._handle(job)
        ui.delay(self.update, 0.2)

    def _handle(self, job):
        kind, payload = job
        if kind == 'ready':
            self._ready = True
            self.status.text = payload
        elif kind == 'error':
            self.busy = False
            self.status.text = payload
        elif kind == 'done':
            res, jpeg, dt, resolved = payload
            self.busy = False
            self.results = res
            self.shot.image = ui.Image.from_data(jpeg)
            self.status.text = '%s  %.1f秒' % (resolved, dt)
            save_source(resolved)
            self.url_field.text = resolved
            self.render()

    def render(self):
        best, best_win = -1, None
        rates = []
        for r in self.results:
            cands = r.get('candidates') or []
            hw, aw = win_rates(cands[0] if cands else None, self.hero)
            rates.append((hw, aw))
            if hw is not None and (best_win is None or hw > best_win):
                best_win, best = hw, len(rates) - 1

        for i, r in enumerate(self.results[:3]):
            q = self.quick[i]
            cands = r.get('candidates') or []
            hw, aw = rates[i]
            q.card_name.text = cands[0]['name'] if cands else '—'
            # ヒーロー勝率を先、全体を後（画面の指定どおり）
            q.hero.text = '%s %s' % (self.hero or '全体', pct(hw))
            q.all.text = '全体 %s' % pct(aw)
            q.card_name.text_color = (NG if r['status'] == 'nomatch'
                                      else FG if r['status'] == 'ok' else TOP)
            q.border_color = TOP if i == best else 'clear'

        lines = []
        for r in self.results:
            head = STATUS_TEXT.get(r['status'], r['status'])
            mana = ('マナ %d' % r['cost']) if r['cost'] is not None else 'マナ不明'
            lines.append('%s [%s] %s %s  差 %.3f'
                         % (r['label'], head, r['ctype'] or '種別不明', mana,
                            r.get('gap', 0)))
            for c in (r.get('candidates') or [])[:3]:
                lines.append('   %d. %-22s %.3f' % (c['rank'], c['name'],
                                                    c['finalScore']))
        self.detail.text = '\n'.join(lines) or '—'
        self.layout()

    def refresh_deck(self):
        try:
            view = A.deck_view(A.load_deck())
        except Exception:      # noqa: BLE001 - 索引がまだなら後で出る
            return
        if not view['cards']:
            self.deck.text = 'まだありません'
            return
        rows = ['%d枚  計 %d' % (len(view['cards']), view['total'])]
        for c in view['cards'][:20]:
            a = c.get('arenaClass') or c.get('arenaAll') or {}
            rows.append('%s x%d  %s' % (c['name'], c['count'], pct(a.get('win'))))
        self.deck.text = '\n'.join(rows)


def main():
    v = ArenaView()
    v.present('fullscreen', hide_title_bar=False)
    v.update()
    v.wait_modal()


if __name__ == '__main__':
    main()
