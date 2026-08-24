#!/usr/bin/env python3
"""Yokota Jobs — 374 FSS の英語版求人告知(PDF)を取り込む

横田基地の民間人人事部(CPO)は、現地従業員(Local National)向けの求人告知を
**日英併記のPDF**で出している。LMO のウェブ求人が日本語のみなのに対し、
こちらは英語原文の職務内容が載っている。

    https://yokota374fss.com/cpo/

LMO 側には無い情報が取れるのが大きい:
  - 公式の英語職種名（機械翻訳が不要になる）
  - 配属先の部隊名（実際の職場。374 Civil Engineer Squadron など）
  - LPL（語学能力級レベル）と TOEIC 換算表
  - **募集範囲**（内部・外部 / 在日米軍従業員のみ）
    → 「在日米軍従業員のみ」の求人は外部の人は応募できない。これが分からないと
      応募できない求人を数えてしまう
  - 英語原文の職務内容・応募要件

fetch_jobs.py から呼ばれる。単体でも動く:
    python3 fetch_fss.py          # 取得して fss.json に保存
"""

import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "fss.json")

CPO_URL = "https://yokota374fss.com/cpo/"
JST = timezone(timedelta(hours=9))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_SSL_CTX = None
if os.environ.get("YJ_INSECURE_SSL") == "1":
    _SSL_CTX = ssl._create_unverified_context()

# 索引テーブルのデータ行。
#   例: '1-7 4(2) MLA 内部・外部'  '2-6 0 MLA 内部・外部'  '1-6 4 MLA 在日米軍従業員'
# 等級(と括弧内の代替等級)、LPL(と括弧内の代替)、契約種別、募集範囲が1行に潰れて出てくる。
ROW_RE = re.compile(
    r"^(\d-\d+)\s*(?:[（(](\d-\d+)[)）])?\s+"      # 等級
    r"(\d+)\s*(?:[（(](\d+)[)）])?\s+"             # LPL
    r"(MLA|MLC|IHA|MC)\s*"                          # 契約
    r"(内部[・･]外部|在日米軍従業員|.*)$"           # 募集範囲(日本語側)
)

# LPL -> TOEIC 下限。PDF 本文の換算表から。LPL0 は英語要件なし
LPL_TOEIC = {0: None, 1: 400, 2: 550, 3: 730, 4: 870}


def http_get(url, timeout=60, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en,ja"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def find_latest_pdf():
    """CPO ページから最新の Vacancy Announcement PDF の URL を探す。

    ファイル名に日付が入る（Yokota-Vacancy-Announcement-for-LN-6-Aug-26.pdf）が、
    表記ゆれがあるのでファイル名では並べ替えず、URL 中の年月ディレクトリで新しい順にする。
    """
    html = http_get(CPO_URL)
    cands = re.findall(
        r'href="(https://yokota374fss\.com/wp-content/uploads/(\d{4})/(\d{2})/[^"]*'
        r'Vacancy[^"]*\.pdf)"', html, re.I)
    if not cands:
        raise SystemExit("Vacancy Announcement の PDF が見つかりません。"
                         "CPO ページの構成が変わった可能性があります。")
    cands.sort(key=lambda c: (c[1], c[2]), reverse=True)
    return cands[0][0]


def _norm(s):
    """突き合わせ用の正規化。

    PDF のテキスト抽出は語中に空白が入ることがある（'E nvironmental' など）ので、
    空白を全部落としてから比較する。
    """
    return re.sub(r"\s+", "", (s or "")).replace("－", "-").replace("　", "")


def _norm_grade(g):
    """等級表記を揃える。索引は '1-7'、個票は 'BWT 1-07' と桁が違う。"""
    m = re.match(r"(\d)\s*-\s*0*(\d+)", g or "")
    return f"{m.group(1)}-{int(m.group(2))}" if m else (g or "")


def parse_index(pages_text):
    """索引テーブル（先頭数ページ）から求人一覧を作る。

    データ行の直前4行が [和名, 英名, 和組織, 英組織] という並びになっている。
    行数が足りない/崩れている場合はその求人を捨てず、取れた分だけ入れる。
    """
    lines = [l.strip() for l in pages_text.split("\n") if l.strip()]
    out = []
    for i, line in enumerate(lines):
        m = ROW_RE.match(line)
        if not m:
            continue
        grade, grade_alt, lpl, lpl_alt, contract, area_ja = m.groups()

        prev = lines[max(0, i - 4):i]
        # 後ろから [英組織, 和組織, 英名, 和名] の順に埋める
        org_en = prev[-1] if len(prev) >= 1 else ""
        org_ja = prev[-2] if len(prev) >= 2 else ""
        title_en = prev[-3] if len(prev) >= 3 else ""
        title_ja = prev[-4] if len(prev) >= 4 else ""

        # 和名に * や ** の注記が付くので落とす
        title_ja = re.sub(r"[\*＊]+$", "", title_ja).strip()
        title_en = re.sub(r"[\*＊]+$", "", title_en).strip()

        # 次行に 'INT/EXT 6/4' や 'USFJ Employee' が来る
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        internal_only = ("在日米軍従業員" in area_ja) or ("USFJ Employee" in nxt)
        open_day = (re.search(r"(\d{1,2}/\d{1,2})", nxt) or [None, ""])[1] \
            if re.search(r"(\d{1,2}/\d{1,2})", nxt) else ""
        is_new = ("新規" in area_ja) or ("New" in nxt) or not open_day

        out.append({
            "title_ja": title_ja,
            "title_en_official": title_en,
            "org_ja": org_ja,
            "org_en": org_en,
            "grade": _norm_grade(grade),
            "grade_alt": _norm_grade(grade_alt) if grade_alt else "",
            "lpl": int(lpl),
            "lpl_alt": int(lpl_alt) if lpl_alt else None,
            "lpl_toeic": LPL_TOEIC.get(int(lpl)),
            "contract_fss": contract,
            "internal_only": internal_only,
            "open_day": open_day,
            "is_new": is_new,
        })
    return out


def parse_details(pages):
    """個票ページ（英語原文）を職種ごとに切り出す。

    各個票は 'Position Title:' で始まり、英語の職務内容が続く。
    英語の求人本文がそのまま取れるので、機械翻訳ではない原文を持てる。
    戻り値: [(英語職種名, 事務所コード, 等級, 本文)]
    """
    # 見出しの表記ゆれ: 'Position Title:' / 'Position Title, Number:'
    HEAD = re.compile(r"Position\s*Title[^:\n]*:\s*\n?\s*(.+)", re.I)

    blocks, cur = [], []
    for p in pages:
        t = p.extract_text() or ""
        if HEAD.search(t) and cur:
            blocks.append("\n".join(cur))
            cur = [t]
        else:
            cur.append(t)
    if cur:
        blocks.append("\n".join(cur))

    out = []
    for b in blocks:
        m = HEAD.search(b)
        if not m:
            continue
        raw = m.group(1).strip()
        # 個票には2種類ある:
        #   英語のみ … 'Accounting Technician, #0008'
        #   日英併記 … 'Plumber, #2218 配管工 ２２１８番'（技能職に多い）
        # 併記型は最初の日本語文字より前だけが英語職種名なので、そこで切る
        title = re.split(r"[ぁ-んァ-ヶ一-龥]", raw)[0]
        title = re.sub(r"[,、]?\s*[#＃]?\s*[\d０-９]{3,}\s*$", "", title).strip(" ,、/／")

        office = ""
        mo = re.search(r"\n\s*([0-9A-Z]{2,}[0-9A-Z\s]*/[0-9A-Z\-]+)", b[m.end():m.end() + 240])
        if mo:
            office = re.sub(r"\s+", " ", mo.group(1)).strip()
        mg = re.search(r"BWT\s*(\d\s*-\s*\d+)", b)
        grade = _norm_grade(mg.group(1)) if mg else ""

        # 本文に日本語が混じるかで、英語のみの募集か日英併記かを判定する。
        # 見出しの定型語(採用基準など)だけで判定すると誤るので、本文全体の和字比率で見る
        ja_chars = len(re.findall(r"[ぁ-んァ-ヶ一-龥]", b))
        lang = "bilingual" if ja_chars > 40 else "en"

        out.append((title, office, grade, b.strip(), lang))
    return out


def fetch():
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf が必要です:  python3 -m pip install pypdf")
    import io

    url = find_latest_pdf()
    print(f"英語版求人告知: {url}")
    raw = http_get(url, binary=True)
    print(f"  {len(raw)//1024} KB ダウンロード")

    reader = PdfReader(io.BytesIO(raw))
    n = len(reader.pages)
    print(f"  {n} ページ")

    # 索引は先頭数ページ。'Minimum Qualification' が出るまでを索引とみなす
    idx_end = n
    for i, p in enumerate(reader.pages):
        if re.search(r"Minimum\s+Qualification", p.extract_text() or "", re.I):
            idx_end = i
            break
    index_text = "\n".join((reader.pages[i].extract_text() or "") for i in range(idx_end))
    items = parse_index(index_text)
    print(f"  索引から {len(items)}件")

    details = parse_details(reader.pages[idx_end:])
    print(f"  英語個票 {len(details)}件")

    # 個票を索引に紐付ける（英語職種名 + 等級 → 職種名のみ の順に緩める）
    by_key = {}
    for title, office, grade, body, lang in details:
        rec = (office, body, lang)
        by_key.setdefault((_norm(title).lower(), grade), []).append(rec)
        by_key.setdefault((_norm(title).lower(), ""), []).append(rec)

    attached = 0
    for it in items:
        t = _norm(it["title_en_official"]).lower()
        hit = by_key.get((t, it["grade"])) or by_key.get((t, ""))
        if hit:
            office, body, lang = hit.pop(0) if len(hit) > 1 else hit[0]
            it["office"] = office
            it["desc_en_official"] = body
            it["posting_lang"] = lang
            attached += 1
        else:
            it["office"] = ""
            it["desc_en_official"] = ""
            it["posting_lang"] = ""
    print(f"  英語本文を紐付け: {attached}件")

    return {
        "updated": datetime.now(JST).isoformat(timespec="seconds"),
        "source_url": url,
        "count": len(items),
        "items": items,
    }


def main():
    data = fetch()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    pub = sum(1 for i in data["items"] if not i["internal_only"])
    print(f"\n完了: {data['count']}件（うち外部応募可 {pub}件）-> {OUT_PATH}")


if __name__ == "__main__":
    main()
