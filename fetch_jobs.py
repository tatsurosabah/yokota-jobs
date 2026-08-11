#!/usr/bin/env python3
"""Yokota Jobs — 横田基地(LMO)求人の収集スクリプト

LMO(駐留軍等労働者労務管理機構)の求人応募サイトから横田支部の求人を取得し、
語学要件・賃金・雇用形態を抽出して jobs.json に蓄積する。

一度掲載された求人は募集終了後も active=false で残す。
4年分ためると「どの職種が年間何件出るか」「TOEIC何点あれば何件応募できるか」が
実データで見えるようになる、というのがこのツールの主目的。

GitHub Actions から週1で実行される。ローカル実行も可:
    python3 fetch_jobs.py                 # 通常
    python3 fetch_jobs.py --no-translate  # 翻訳をスキップ(高速)
    python3 fetch_jobs.py --limit 5       # 詳細取得を5件に制限(動作確認用)

macOS の python.org 版で証明書エラーが出る場合:
    YJ_INSECURE_SSL=1 python3 fetch_jobs.py
"""

import html as html_mod
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "jobs.json")

BASE = "https://oubo.lmo.go.jp/oubo_pub/keisai/"
JST = timezone(timedelta(hours=9))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# サーバに優しく。公的機関のサイトなので余裕をもって間隔をあける
SLEEP = 1.2

_SSL_CTX = None
if os.environ.get("YJ_INSECURE_SSL") == "1":
    _SSL_CTX = ssl._create_unverified_context()

# 検索フォームのカテゴリ chekbox 名 -> 内部カテゴリ
CATEGORIES = {
    "cbJimuGijyutsu":  "office",    # 事務・技術関係
    "cbGinouRoumu":    "trade",     # 技能・労務関係
    "cbKeibiSyoubou":  "security",  # 警備・消防関係
    "cbIryou":         "medical",   # 医療関係
    "cbKango":         "nursing",   # 看護関係
}

# 整理番号の3文字目 -> 労務契約の種別
#   M = 基本労務契約(MLC)  I = 諸機関労務協約(IHA)  H = 時給制臨時従業員
CONTRACT_BY_LETTER = {"M": "MLC", "I": "IHA", "H": "HOURLY"}

# 詳細ページの span id -> 出力キー
DETAIL_FIELDS = {
    "lblSeiriNo":               "id",
    "lblSyokusyu":              "title",
    "lblSyokui":                "position",
    "lblShigotoNaiyou":         "description",
    "lblSyugyouBasyo":          "location",
    "lblKoyouKikan":            "employment",
    "lblNenrei":                "age",
    "lblHitsuyouShikaku":       "requirements",
    "lblSyugyouJikan":          "hours",
    "lblChinginKeitai":         "wage_type",
    "lblChingin":               "salary_raw",
    "lblKyujitsu":              "holidays",
    "lblIkujiKyugyouUmu":       "childcare_leave",
    "lblTekiyouHoken":          "insurance",
    "lblJyuutakuUmu":           "housing",
    "lblMyCarTsuukinUmu":       "car_commute",
    "lblTsuukinTeate":          "commute_allowance",
    "lblSaiyouNinzuu":          "openings",
    "lblInternetUketsukeKigen": "deadline_net",
    "lblUketsukeBasyo":         "office",
    "lblBikou":                 "remarks",
}


# ------------------------------------------------------------------ HTTP

class Session:
    """ASP.NET WebForms 相手のセッション管理。

    このサイトは ViewState + Cookie で状態を持ち、詳細ページへの直接 GET は
    弾かれる。一覧を1回引いたあと、その ViewState を使い回して
    __EVENTARGUMENT='詳細$N' をポストすると各求人の詳細が取れる。
    """

    def __init__(self):
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(cj)]
        if _SSL_CTX:
            handlers.insert(0, urllib.request.HTTPSHandler(context=_SSL_CTX))
        self.op = urllib.request.build_opener(*handlers)
        self.op.addheaders = [("User-Agent", UA), ("Accept-Language", "ja,en")]

    def get(self, url, retries=2):
        for i in range(retries + 1):
            try:
                r = self.op.open(url, timeout=40)
                return r.geturl(), r.read().decode("utf-8", "replace")
            except Exception as e:
                if i == retries:
                    raise
                sys.stderr.write(f"  retry GET ({e})\n")
                time.sleep(2 * (i + 1))

    def post(self, url, page_html, extra, retries=2):
        data = _form_fields(page_html)
        data.setdefault("__EVENTTARGET", "")
        data.setdefault("__EVENTARGUMENT", "")
        data.update(extra)
        body = urllib.parse.urlencode(data).encode()
        for i in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": url,
                })
                r = self.op.open(req, timeout=40)
                return r.geturl(), r.read().decode("utf-8", "replace")
            except Exception as e:
                if i == retries:
                    raise
                sys.stderr.write(f"  retry POST ({e})\n")
                time.sleep(2 * (i + 1))


def _form_fields(h):
    """ページ内の hidden/text input を全部集める。GridView のポストバックには
    行内の hidden まで必要なので、まとめて送り返す。"""
    d = {}
    for m in re.finditer(r"<input([^>]*)>", h, re.I):
        attrs = m.group(1)
        name = re.search(r'name="([^"]+)"', attrs)
        if not name:
            continue
        typ = (re.search(r'type="([^"]+)"', attrs) or [None, "text"])[1]
        if typ.lower() in ("checkbox", "radio", "button", "submit", "image"):
            continue
        val = re.search(r'value="([^"]*)"', attrs)
        d[name.group(1)] = html_mod.unescape(val.group(1)) if val else ""
    return d


# ------------------------------------------------------------------ パース

def _clean(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(p|div|tr|li)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    s = s.replace("　", " ")
    lines = [re.sub(r"[ \t]+", " ", x).strip() for x in s.split("\n")]
    return "\n".join(x for x in lines if x).strip()


def span(h, span_id):
    m = re.search(r'id="%s"[^>]*>(.*?)</span>' % re.escape(span_id), h, re.S)
    return _clean(m.group(1)) if m else ""


def parse_salary(raw, wage_type=""):
    """'基本給242,000円地域手当38,720円' -> (242000, 38720, 280720, None)

    表記のゆれが多いので、順に効かせる:
      - 「備考欄参照」等は金額が書かれていないので全部 None
      - 「時給制1,430円」「時間給1,320円」は時給として返す(月額は出さない)
      - 基本給 + 地域手当 が本則
    """
    if not raw or re.search(r"参照|別途|応相談", raw):
        return None, None, None, None

    def num(pat, s=raw):
        m = re.search(pat, s)
        return int(m.group(1).replace(",", "")) if m else None

    # 時給制。'時給制' の '制' を挟むパターンがあるので許容する
    is_hourly = bool(re.search(r"時給|時間給", raw + wage_type))
    if is_hourly:
        h = num(r"(?:時間給|時給)制?\s*([\d,]+)\s*円") or num(r"([\d,]+)\s*円")
        return None, None, None, h

    base = num(r"基本給\s*([\d,]+)\s*円")
    allow = num(r"地域手当\s*([\d,]+)\s*円")
    if base is None:
        # 「月額250,000円」のような書き方への保険。
        # 桁を絞らないと手当額や時給を拾ってしまうので6桁以上に限定する
        cand = num(r"([\d,]{6,})\s*円")
        base = cand if (cand and cand >= 100000) else None
    total = (base or 0) + (allow or 0) or None
    return base, allow, total, None


def parse_english(requirements):
    """必要な免許資格等 から語学要件を抜く。

    典型例:
      【語学能力級レベル：2】
      TOEIC 550点～、ALCPT 75点～、TOEFL(PBT) 460点～ ... 英検２級以上
    語学要件が書かれていない求人(技能職に多い)は level=None を返す。
    """
    if not requirements:
        return None, None, ""
    lv = re.search(r"語学能力級?レベル[：:\s]*(\d+)", requirements)
    level = int(lv.group(1)) if lv else None
    tc = re.search(r"TOEIC\s*([\d,]+)\s*点", requirements)
    toeic = int(tc.group(1).replace(",", "")) if tc else None
    raw = ""
    if level is not None or toeic is not None:
        # 語学要件の行だけ抜き出す
        keep = [l for l in requirements.split("\n")
                if re.search(r"語学|TOEIC|TOEFL|英検|ALCPT|CASEC", l)]
        raw = "\n".join(keep)
    return level, toeic, raw


def parse_japanese(*texts):
    """日本語能力への言及があるか。

    LMO の求人票に「日本語レベル」という定型欄は無い。なので
    「書かれていない = 不問」と断定はできない。言及の有無だけを機械的に拾い、
    判断材料としてそのまま見せる方針にしている。
    """
    hits = []
    for t in texts:
        for line in (t or "").split("\n"):
            if re.search(r"日本語|和文|国語", line):
                hits.append(line.strip())
    return (len(hits) > 0), "\n".join(dict.fromkeys(hits))


def parse_age(s):
    if not s:
        return None, None
    m = re.search(r"(\d+)\s*歳?\s*[～~\-]\s*(\d+)\s*歳", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\s*歳以上", s)
    return (int(m.group(1)), None) if m else (None, None)


def employment_type(employment, seiri_no):
    letter = seiri_no[3:4].upper() if len(seiri_no) > 3 else ""
    if "時給制臨時" in employment or letter == "H":
        return "hourly"
    if "限定期間" in employment:
        return "limited"
    if "日雇" in employment:
        return "daily"
    if "常用" in employment:
        return "regular"
    return "other"


# ------------------------------------------------------------------ 翻訳

# 機械翻訳が誤る/不自然になる職種名は固定訳を当てる。
# 「コック」が cock と訳されるなど、そのまま見せられないものがあるため。
# 前方一致ではなく完全一致で引き、外れたら機械翻訳にフォールバックする。
TITLE_OVERRIDES = {
    "コック": "Cook",
    "ショートオーダーコック": "Short Order Cook",
    "ウエイター・ウエイトレス": "Waiter / Waitress",
    "バーテンダー": "Bartender",
    "ジャニター": "Janitor",
    "ハウスキーパー職": "Housekeeper",
    "サービスワーカー": "Service Worker",
    "会計技術職": "Accounting Technician",
    "秘書職": "Secretary",
    "補給専門職": "Supply Specialist",
    "運賃専門職": "Freight Rate Specialist",
    "電話交換職": "Telephone Operator",
    "警備員": "Security Guard",
    "歯科衛生職": "Dental Hygienist",
    "配管工": "Plumber",
    "電気工": "Electrician",
    "塗装工": "Painter",
    "自動車塗装工": "Automotive Painter",
    "自動車機械工": "Automotive Mechanic",
    "建物保守作業工": "Building Maintenance Worker",
    "発電装置修理工": "Power Generation Equipment Repairer",
    "ボイラー装置操作工": "Boiler Operator",
    "冷蔵及び空気調節機械工": "Refrigeration & Air Conditioning Mechanic",
    "航空機燃料補給車運転手": "Aircraft Refueling Vehicle Driver",
    "燃料配給組織機械工": "Fuel Distribution System Mechanic",
    "ラジオ、テレビ維持修理工": "Radio / TV Maintenance Technician",
    "自動車車体及びフェンダー修理工": "Auto Body & Fender Repairer",
}


def title_en(ja):
    """職種名の英訳。固定訳 -> 機械翻訳 の順。

    括弧書きの補足（例:「技師職（適応専門業務）」）は本体だけ固定訳に当て、
    括弧内は機械翻訳に回す。
    """
    ja = (ja or "").strip()
    if not ja:
        return ""
    if ja in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[ja]
    m = re.match(r"^([^（(]+)[（(](.+)[)）]$", ja)
    if m and m.group(1).strip() in TITLE_OVERRIDES:
        inner = translate(m.group(2).strip())
        head = TITLE_OVERRIDES[m.group(1).strip()]
        return f"{head} ({inner})" if inner else head
    return translate(ja)


_TR_CACHE = {}


def translate(text, target="en", source="ja", retries=2):
    """Google の非公式エンドポイントで翻訳。

    失敗時は None を返す(原文ではなく)。呼び出し側が「翻訳できなかった」を
    区別できないと、失敗結果をキャッシュしてしまい二度と再試行されなくなるため。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if text in _TR_CACHE:
        return _TR_CACHE[text]
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode({
        "client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text[:4500],
    })
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25, context=_SSL_CTX) as r:
                data = json.loads(r.read().decode("utf-8"))
            out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
            if out:
                _TR_CACHE[text] = out
                return out
        except Exception as e:
            if attempt == retries:
                sys.stderr.write(f"  翻訳失敗: {text[:20]}… ({e})\n")
        time.sleep(1.5 * (attempt + 1))
    return None


# ------------------------------------------------------------------ 収集

def collect_category_map(s, list_url, list_html):
    """カテゴリ別に検索し直して 整理番号 -> カテゴリ の対応を作る。

    一覧の行にはカテゴリ列が無いので、カテゴリのチェックボックスを1つずつ
    立てて検索し、返ってきた整理番号にラベルを貼る。
    """
    cat = {}
    for cb, name in CATEGORIES.items():
        try:
            _, h = s.post(list_url, list_html,
                          {"__EVENTTARGET": "btnSearchExcute", "cbYokotaSibu": "on",
                           cb: "on", "rbKeywordDiv": "0", "txtKeyword": ""})
            ids = re.findall(r'hidSeiriNo"[^>]*value="([^"]+)"', h)
            for i in ids:
                cat[i.strip()] = name
            print(f"  カテゴリ {name}: {len(ids)}件")
        except Exception as e:
            sys.stderr.write(f"  カテゴリ {name} 取得失敗: {e}\n")
        time.sleep(SLEEP)
    return cat


def fetch_all(limit=None):
    s = Session()
    print("LMO 求人応募サイトへ接続中...")
    u1, h1 = s.get(BASE)

    # 横田支部で全件検索
    list_url, list_html = s.post(u1, h1, {
        "__EVENTTARGET": "btnSearchExcute",
        "cbYokotaSibu": "on", "rbKeywordDiv": "0", "txtKeyword": "",
    })
    ids = re.findall(r'hidSeiriNo"[^>]*value="([^"]+)"', list_html)
    ids = [i.strip() for i in ids]
    print(f"横田支部の掲載: {len(ids)}件")
    if not ids:
        raise SystemExit("求人が0件。サイト構造が変わった可能性があります。")

    # 一覧から掲載開始日を拾っておく(詳細ページには無い項目)
    posted = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", list_html, re.S):
        d = re.search(r"(\d{4}/\d{2}/\d{2})", row)
        n = re.search(r'hidSeiriNo"[^>]*value="([^"]+)"', row)
        if d and n:
            posted[n.group(1).strip()] = d.group(1)

    print("カテゴリを判定中...")
    cat_map = collect_category_map(s, list_url, list_html)

    # 一覧を取り直す(カテゴリ検索で絞り込まれた状態になっているため)
    list_url, list_html = s.post(list_url, list_html, {
        "__EVENTTARGET": "btnSearchExcute",
        "cbYokotaSibu": "on", "rbKeywordDiv": "0", "txtKeyword": "",
    })

    targets = ids[:limit] if limit else ids
    print(f"詳細を取得中 ({len(targets)}件)...")
    out = []
    for idx, sid in enumerate(targets):
        try:
            _, dh = s.post(list_url, list_html, {
                "__EVENTTARGET": "tblKyuujinKeisaiList",
                "__EVENTARGUMENT": f"詳細${idx}",
            })
            if "session_error" in dh or "lblSeiriNo" not in dh:
                sys.stderr.write(f"  [{idx}] {sid} 詳細取得に失敗(セッション)\n")
                time.sleep(SLEEP)
                continue

            rec = {out_key: span(dh, span_id)
                   for span_id, out_key in DETAIL_FIELDS.items()}
            if not rec.get("id"):
                rec["id"] = sid

            base, allow, total, hourly = parse_salary(rec.get("salary_raw", ""),
                                                      rec.get("wage_type", ""))
            lv, toeic, eng_raw = parse_english(rec.get("requirements", ""))
            jp_req, jp_note = parse_japanese(rec.get("requirements", ""),
                                             rec.get("description", ""))
            amin, amax = parse_age(rec.get("age", ""))

            rec.update({
                "posted":          posted.get(sid, ""),
                "category":        cat_map.get(sid, "other"),
                "contract":        CONTRACT_BY_LETTER.get(sid[3:4].upper(), "OTHER"),
                "employment_type": employment_type(rec.get("employment", ""), sid),
                "salary_base":     base,
                "salary_allowance": allow,
                "salary_total":    total,
                "salary_hourly":   hourly,
                "eng_level":       lv,
                "eng_toeic":       toeic,
                "eng_raw":         eng_raw,
                "jp_mentioned":    jp_req,
                "jp_note":         jp_note,
                "age_min":         amin,
                "age_max":         amax,
            })
            out.append(rec)
            tag = f"TOEIC{toeic}" if toeic else ("lv" + str(lv) if lv else "英語要件なし")
            print(f"  [{idx+1}/{len(targets)}] {rec['title']} / {tag} / {total or hourly or '-'}")
        except Exception as e:
            sys.stderr.write(f"  [{idx}] {sid} 失敗: {e}\n")
        time.sleep(SLEEP)
    return out


# ------------------------------------------------------------------ マージ

def merge(old, new_items, do_translate=True):
    """既存 jobs.json と統合。

    - 消えた求人は削除せず active=false にする(時系列の蓄積が目的なので)
    - 翻訳は既存分を再利用して、新規ぶんだけ翻訳する
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    by_id = {it["id"]: it for it in (old.get("items") or [])}

    seen_now = set()
    for it in new_items:
        jid = it["id"]
        seen_now.add(jid)
        prev = by_id.get(jid, {})
        it["first_seen"] = prev.get("first_seen", today)
        it["last_seen"] = today
        it["active"] = True
        # 翻訳キャッシュの引き継ぎ。
        # 訳文が原文と同一のものは「翻訳に失敗した結果」なので引き継がず、次回再試行させる
        for k_src, k_dst in (("title", "title_en"), ("description", "description_en")):
            cached = prev.get(k_dst)
            if cached and cached != prev.get(k_src) and prev.get(k_src) == it.get(k_src):
                it[k_dst] = cached
        by_id[jid] = it

    for jid, it in by_id.items():
        if jid not in seen_now:
            it["active"] = False

    items = list(by_id.values())

    if do_translate:
        todo = [it for it in items if it.get("active") and not it.get("title_en")]
        print(f"翻訳中 ({len(todo)}件)...")
        failed = 0
        for it in todo:
            en = title_en(it.get("title", ""))
            if en:
                it["title_en"] = en
            else:
                failed += 1
            desc = it.get("description", "")
            if desc and not it.get("description_en"):
                den = translate(desc)
                if den:
                    it["description_en"] = den
            time.sleep(0.5)
        if failed:
            print(f"  {failed}件は翻訳できず(次回再試行されます)")
    # 翻訳が無いものは UI 側で原文にフォールバックする(title_en は空のままにする)

    # 固定訳は既存キャッシュより優先する。
    # 辞書に項目を足したとき、過去に取り込んだ求人にも遡って効かせるため。
    for it in items:
        ov = TITLE_OVERRIDES.get((it.get("title") or "").strip())
        if ov:
            it["title_en"] = ov

    items.sort(key=lambda x: (x.get("posted") or "", x.get("id") or ""), reverse=True)

    active = [i for i in items if i.get("active")]
    return {
        "updated": datetime.now(JST).isoformat(timespec="seconds"),
        "source": "LMO 駐留軍等労働者労務管理機構 / 横田支部",
        "source_url": "https://www.lmo.go.jp/recruitment/",
        "counts": {
            "active": len(active),
            "archived": len(items) - len(active),
            "total": len(items),
        },
        "items": items,
    }


def main():
    do_translate = "--no-translate" not in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    old = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                old = json.load(f)
        except Exception as e:
            sys.stderr.write(f"既存 jobs.json の読み込みに失敗(新規作成します): {e}\n")

    new_items = fetch_all(limit=limit)
    data = merge(old, new_items, do_translate=do_translate)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    c = data["counts"]
    print(f"\n完了: 掲載中 {c['active']}件 / 終了 {c['archived']}件 / 累計 {c['total']}件")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
