#!/usr/bin/env python3
"""アイコン生成（icon-512.png / icon-180.png）

滑走路をモチーフにしたシンプルな図案。横田＝飛行場なので、
紺地に滑走路の白線とセンターラインの破線を置いただけのもの。
デザインを変えたときだけ再実行すればよい。

    python3 make_icon.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

NAVY = "#1B3A5C"     # 地色（テーマカラーと合わせる）
WHITE = "#FFFFFF"
SKY = "#5B8FC4"      # アクセント

S = 512


def make():
    img = Image.new("RGB", (S, S), NAVY)
    d = ImageDraw.Draw(img)

    # 滑走路：わずかに台形にして奥行きを出す
    top_w, bot_w = 128, 208
    top_y, bot_y = 96, 430
    cx = S / 2
    d.polygon(
        [(cx - top_w / 2, top_y), (cx + top_w / 2, top_y),
         (cx + bot_w / 2, bot_y), (cx - bot_w / 2, bot_y)],
        fill=SKY,
    )

    # センターラインの破線
    n, gap = 5, 14
    seg = (bot_y - top_y - gap * (n - 1)) / n
    for i in range(n):
        y0 = top_y + i * (seg + gap)
        y1 = y0 + seg
        t = (y0 - top_y) / (bot_y - top_y)
        w = 7 + t * 9          # 手前ほど太く
        d.rounded_rectangle([cx - w / 2, y0, cx + w / 2, y1], radius=w / 2, fill=WHITE)

    # 滑走路の両脇のエッジライン
    for sign in (-1, 1):
        d.line(
            [(cx + sign * top_w / 2 * 0.82, top_y),
             (cx + sign * bot_w / 2 * 0.86, bot_y)],
            fill=WHITE, width=6,
        )

    # 接地帯マーキング（手前）
    for sign in (-1, 1):
        for i in range(3):
            x = cx + sign * (36 + i * 22)
            d.rounded_rectangle([x - 6, 452, x + 6, 486], radius=3, fill=WHITE)

    img.save(os.path.join(HERE, "icon-512.png"))
    img.resize((180, 180), Image.LANCZOS).save(os.path.join(HERE, "icon-180.png"))
    print("wrote icon-512.png / icon-180.png")


if __name__ == "__main__":
    make()
