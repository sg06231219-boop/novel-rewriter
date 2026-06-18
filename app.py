#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灏忚缈绘敼宸ュ叿 v7.1.0
鏂板姛鑳斤細绉嶅瓙鏁版嵁澶栭儴JSON鍖栵紙瑙ｅ喅鍐峰惎鍔ㄨ秴鏃讹級銆?
      SQLite鏁版嵁鎸佷箙鍖栵紙瑙ｅ喅Render閲嶅惎涓㈡暟鎹棶棰橈級銆?
      鎼滅储缁撴灉楂樹寒銆佹暣鏈鍑篔SON銆佹嫋鎷芥帓搴廳ata-id銆?
      PWA manifest icons淇銆佺増鏈彿缁熶竴
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import uvicorn
import re
import json
import os
import httpx
from collections import Counter
from datetime import datetime
import sqlite3
import asyncio

app = FastAPI(title="Novel Rewriter v7.1.0")

ADMIN_PWD = os.environ.get("ADMIN_PASSWORD", "admin123")
DB_PATH = os.environ.get("DB_PATH", "data/novel_rewriter.db")

# ============ SQLite 鍒濆鍖?============

def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 涔︾睄琛?
    c.execute("""CREATE TABLE IF NOT EXISTS books (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        author TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT
    )""")
    # 绔犺妭琛?
    c.execute("""CREATE TABLE IF NOT EXISTS chapters (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 0,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    )""")
    # 瑙勫垯妯℃澘琛?
    c.execute("""CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        rules_json TEXT NOT NULL,
        created_at TEXT
    )""")
    # 杩佺Щ锛氬鏋滆〃瀛樺湪浣嗙己灏?sort_order 鍒楋紝鍒欐坊鍔?
    try:
        c.execute("ALTER TABLE chapters ADD COLUMN sort_order INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============ 绉嶅瓙鏁版嵁 ============
# 绉嶅瓙鏁版嵁宸叉彁鍙栬嚦 seed_books.json锛屽惎鍔ㄦ椂鎸夐渶鍔犺浇

_SEED_BOOKS_CACHE = None

def _load_seed_books():
    """浠?seed_books.json 鍔犺浇绉嶅瓙鏁版嵁锛堝甫缂撳瓨锛?""
    global _SEED_BOOKS_CACHE
    if _SEED_BOOKS_CACHE is None:
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed_books.json')
        with open(json_path, 'r', encoding='utf-8') as f:
            _SEED_BOOKS_CACHE = json.load(f)
    return _SEED_BOOKS_CACHE


def _seed_db():
    """濡傛灉鏁版嵁搴撲负绌猴紝鐏屽叆绉嶅瓙鏁版嵁"""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM books")
    if cur.fetchone()[0] > 0:
        conn.close()
        return
    now = datetime.now().isoformat()[:19]
    seed_books = _load_seed_books()
    for i, seed in enumerate(seed_books):
        book_id = f"b_seed_{i+1:03d}"
        cur.execute(
            "INSERT INTO books (id, title, author, created_at, updated_at) VALUES (?,?,?,?,?)",
            (book_id, seed["title"], seed.get("author", ""), now, now)
        )
        for j, ch in enumerate(seed["chapters"]):
            ch_id = f"ch_seed_{i+1:03d}_{j+1:02d}"
            cur.execute(
                "INSERT INTO chapters (id, book_id, title, content, sort_order) VALUES (?,?,?,?,?)",
                (ch_id, book_id, ch["title"], ch["content"], j)
            )
    conn.commit()
    conn.close()

_init_db()
_seed_db()

# ============ 鏁版嵁璁块棶杈呭姪 ============

def _load_books():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, author, created_at, updated_at FROM books ORDER BY created_at")
    rows = cur.fetchall()
    books = []
    for r in rows:
        cur2 = conn.cursor()
        cur2.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (r["id"],))
        ch_count = cur2.fetchone()[0]
        books.append({
            "id": r["id"],
            "title": r["title"],
            "author": r["author"],
            "chapter_count": ch_count,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    conn.close()
    return books

def _get_book(book_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM books WHERE id=?", (book_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return None
    cur.execute("SELECT id, title, content, sort_order FROM chapters WHERE book_id=? ORDER BY sort_order, id", (book_id,))
    chapters = [{"id": c["id"], "title": c["title"], "content": c["content"]} for c in cur.fetchall()]
    book = dict(r)
    book["chapters"] = chapters
    conn.close()
    return book

def _save_book(book_id, title, author):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE books SET title=?, author=?, updated_at=? WHERE id=?",
                (title, author, datetime.now().isoformat()[:19], book_id))
    conn.commit()
    conn.close()

def _add_book(title, author, chapters):
    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat()[:19]
    book_id = f"b_{int(datetime.now().timestamp()*1000)}"
    cur.execute("INSERT INTO books (id, title, author, created_at, updated_at) VALUES (?,?,?,?,?)",
                (book_id, title, author, now, now))
    for i, ch in enumerate(chapters):
        ch_id = f"ch_{book_id}_{i+1}"
        cur.execute(
            "INSERT INTO chapters (id, book_id, title, content, sort_order) VALUES (?,?,?,?,?)",
            (ch_id, book_id, ch.get("title", ""), ch.get("content", ""), i)
        )
    conn.commit()
    conn.close()
    return book_id

def _delete_book(book_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM books WHERE id=?", (book_id,))
    cur.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
    conn.commit()
    conn.close()

def _add_chapter(book_id, title, content):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,))
    count = cur.fetchone()[0]
    ch_id = f"ch_{book_id}_{count+1}"
    cur.execute(
        "INSERT INTO chapters (id, book_id, title, content, sort_order) VALUES (?,?,?,?,?)",
        (ch_id, book_id, title, content, count)
    )
    conn.commit()
    conn.close()
    return ch_id

def _update_chapter(book_id, ch_id, title=None, content=None):
    conn = _get_conn()
    cur = conn.cursor()
    fields = []
    vals = []
    if title is not None:
        fields.append("title=?")
        vals.append(title)
    if content is not None:
        fields.append("content=?")
        vals.append(content)
    if fields:
        cur.execute(f"UPDATE chapters SET {','.join(fields)} WHERE id=? AND book_id=?",
                    vals + [ch_id, book_id])
        cur.execute("UPDATE books SET updated_at=? WHERE id=?",
                    (datetime.now().isoformat()[:19], book_id))
        conn.commit()
    conn.close()

def _delete_chapter(book_id, ch_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM chapters WHERE id=? AND book_id=?", (ch_id, book_id))
    # 閲嶆柊鎺掑簭
    cur.execute("SELECT id FROM chapters WHERE book_id=? ORDER BY sort_order, id", (book_id,))
    for i, r in enumerate(cur.fetchall()):
        cur.execute("UPDATE chapters SET sort_order=? WHERE id=?", (i, r["id"]))
    cur.execute("UPDATE books SET updated_at=? WHERE id=?",
                (datetime.now().isoformat()[:19], book_id))
    conn.commit()
    conn.close()

def _reorder_chapters(book_id, chapter_ids):
    conn = _get_conn()
    cur = conn.cursor()
    for i, ch_id in enumerate(chapter_ids):
        cur.execute("UPDATE chapters SET sort_order=? WHERE id=? AND book_id=?",
                    (i, ch_id, book_id))
    cur.execute("UPDATE books SET updated_at=? WHERE id=?",
                (datetime.now().isoformat()[:19], book_id))
    conn.commit()
    conn.close()

def _load_rules():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, rules_json, created_at FROM rules ORDER BY created_at")
    rows = cur.fetchall()
    rules = []
    for r in rows:
        rules.append({
            "id": r["id"],
            "name": r["name"],
            "rules": json.loads(r["rules_json"]),
            "created_at": r["created_at"],
        })
    conn.close()
    return rules

def _save_rule(name, rules_list):
    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat()[:19]
    rule_id = f"r_{int(datetime.now().timestamp()*1000)}"
    cur.execute(
        "INSERT INTO rules (id, name, rules_json, created_at) VALUES (?,?,?,?)",
        (rule_id, name, json.dumps(rules_list, ensure_ascii=False), now)
    )
    conn.commit()
    conn.close()
    return rule_id

def _delete_rule(rule_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()

# ============ Rate Limiting ============

RATE_LIMIT = {}
def _check_rate_limit(ip: str):
    if ip not in RATE_LIMIT:
        RATE_LIMIT[ip] = []
    now = time.time()
    RATE_LIMIT[ip] = [t for t in RATE_LIMIT[ip] if now - t < 60]
    if len(RATE_LIMIT[ip]) >= 30:
        raise HTTPException(status_code=429, detail="璇锋眰杩囦簬棰戠箒锛岃绋嶅悗鍐嶈瘯")
    RATE_LIMIT[ip].append(now)
    # 娓呯悊杩囨湡IP锛堥槻姝㈠唴瀛樻硠婕忥級
    if len(RATE_LIMIT) > 1000:
        cutoff = now - 300
        for key in list(RATE_LIMIT.keys()):
            if all(t < cutoff for t in RATE_LIMIT[key]):
                del RATE_LIMIT[key]

# ============ 绠＄悊鍛楾oken ============

def _get_admin_token(request: Request, token: str = ""):
    from starlette.requests import Request as StarletteRequest
    session = request.session
    if session.get("admin_authed"):
        return ADMIN_PWD
    if token and token == ADMIN_PWD:
        return ADMIN_PWD
    return None

# ============ 鏁版嵁妯″瀷 ============

class ReplaceRule(BaseModel):
    original: str
    replacement: str

class RewriteRequest(BaseModel):
    text: str
    rules: List[ReplaceRule]
    use_ai: bool = False
    ai_intensity: str = "medium"
    api_key: Optional[str] = None
    ai_provider: str = "zhipu"

class RewriteResponse(BaseModel):
    original: str
    rewritten: str
    replacements: List[Dict]

class ExtractRequest(BaseModel):
    text: str

class BookCreate(BaseModel):
    title: str
    author: str = ""
    chapters: List[Dict] = []

class ChapterAdd(BaseModel):
    title: str
    content: str

class RulesSave(BaseModel):
    name: str
    rules: List[ReplaceRule]

# ============ 鏍稿績閫昏緫 ============

def apply_rules(text: str, rules: List[ReplaceRule]) -> tuple:
    result = text
    rep_details = []
    for rule in sorted(rules, key=lambda r: len(r.original), reverse=True):
        if rule.original and rule.replacement:
            count = result.count(rule.original)
            if count > 0:
                result = result.replace(rule.original, rule.replacement)
                rep_details.append({
                    "original": rule.original,
                    "replacement": rule.replacement,
                    "count": count
                })
    return result, rep_details

def call_ai(prompt: str, api_key: str, provider: str, stream: bool = False):
    if provider == "zhipu":
        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        model = "glm-4-flash"
    elif provider == "deepseek":
        url = "https://api.deepseek.com/chat/completions"
        model = "deepseek-chat"
    else:
        url = "https://api.openai.com/v1/chat/completions"
        model = "gpt-3.5-turbo"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    if stream:
        payload["stream"] = True

    if stream:
        def generate():
            with httpx.Client(timeout=120) as client:
                with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        return generate()
    else:
        with httpx.Client(timeout=120) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
        data = resp.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        raise ValueError("AI 杩斿洖鏍煎紡寮傚父")

def ai_rewrite(text: str, api_key: str,
               intensity: str = "medium", provider: str = "zhipu") -> str:
    desc = {
        "light": "杞诲井鏀瑰啓锛屽彧鏇挎崲閮ㄥ垎璇嶆眹锛屼繚鎸佸彞寮忕粨鏋?,
        "medium": "涓瓑鏀瑰啓锛屽彉鎹㈠彞寮忓拰琛ㄨ揪锛屼繚鎸佸墽鎯呬笉鍙?,
        "heavy": "澶у箙鏀瑰啓锛屾崲鍙欒堪椋庢牸锛屼繚鎸佸墽鎯呮鏋朵笉鍙?
    }
    prompt = f"""浣犳槸涓撲笟灏忚鏀瑰啓甯堛€傝姹傦細
1. {desc.get(intensity, desc['medium'])}
2. 鍓ф儏瀹屽叏涓嶅彉锛屼汉鐗?鍦扮偣/鐗╁搧鍚嶇О涓嶅彉
3. 淇濇寔鍘熸湁鏂囬
4. 涓嶆坊鍔犱笉鍒犲噺鍐呭
5. 鍙緭鍑烘敼鍐欏悗鐨勬枃鏈紝涓嶈瑙ｉ噴

鍘熸枃锛?
{text}"""
    return call_ai(prompt, api_key, provider)

# ---- 鍚嶇О鎻愬彇 ----

SURNAMES = set(
    "璧甸挶瀛欐潕鍛ㄥ惔閮戠帇鍐檲瑜氬崼钂嬫矆闊╂潹鏈辩Е灏よ浣曞悤鏂藉紶瀛旀浌涓ュ崕"
    "閲戦瓘闄跺鎴氳阿閭瑰柣鏌忔按绐︾珷浜戣嫃娼樿憶濂氳寖褰儙椴侀煢鏄岄┈鑻楀嚖鑺辨柟淇?
    "浠昏鏌抽矋鍙插攼钖涢浄璐哄€堡娈风綏姣曢儩瀹夊父榻愬悍浼嶄綑鍏冨崪椤惧瓱骞抽粍鍜岀﹩钀?
    "灏瑰閭垫箾姹姣涚鐙勭背璐濇槑鑷ц浼忔垚鎴磋皥瀹嬭寘搴炵唺绾垝灞堥」绁濊懀姊佹潨闃?
    "钃濋椀甯楹诲己璐捐矾濞勫嵄姹熺棰滈儹姊呯洓鏋楅挓寰愰偙楠嗛珮澶忚敗鐢版▕鑳″噷闇嶈櫈涓?
    "鏀煰绠″崲鑾粡鎴垮共瑙ｅ簲瀹椾竵閭撻儊鍗曟椽鍖呰宸︾煶宕旈練绋嬭４闄嗚崳鏇插灏佸偍闈虫"
    "瀵屽帆涔岀劍宸村紦鐗у北璋疯溅渚彮浠扮浠蹭紛瀹畞浠囨牼鏆寸敇鍘夋垘绁栨绗﹀垬鏅┕鏉熼緳"
    "鍙跺垢鍙搁粠钖勫嵃瀹跨櫧鎬€钂查偘浠庨剛绱㈠捀绫嶈禆鍗撳睜钂欐睜涔旀浘娌欏吇闉犻』涓板发鍏崇浉鏌?
    "鍚庤崋绾㈡父鏉冪洊鐩婃鍏嵂鍙ら瓟閭丹闈掔櫧绱巹澶╁菇鍐ヨ褰辩伒浠欏墤鍦ｅ皧甯濈殗鐜?
)
LOC_SUFFIXES = ("鍩?, "灞?, "璋?, "娴?, "宀?, "婀?, "娌?, "姹?,
                  "宄?, "宕?, "娲?, "绐?, "鏋?, "鍘?, "婕?, "娉?,
                  "娓?, "娼?, "婧?, "娉?, "宸?, "閮?, "鐪?, "闀?,
                  "鏉?, "鍏?, "娓?, "妗?, "浜?)
ORG_SUFFIXES = ("闂?, "娲?, "瀹?, "闃?, "妤?, "搴?, "鍫?, "瀵?,
                  "瀹?, "娈?, "鍫?, "闄?, "甯?, "鐩?, "鏁?, "瀵?, "瑙?)
ITEM_SUFFIXES = ("鍓?, "鍒€", "鏋?, "鏂?, "閿?, "寮?, "鎵?, "鐝?,
                  "濉?, "榧?, "闀?, "鐡?, "鐏?, "鍗?, "绗?, "涓?,
                  "鑽?, "鑽?, "璇€", "鍏?, "鍥?, "鍗?, "浠?, "鐗?)

BAD_PHRASES = {
    "涓€涓?, "涓€浜?, "涓€鏍?, "涓€鐩?, "涓€鏃?, "涓€鍒?, "涓€璧?, "涓€鑸?,
    "涓嶆槸", "涓嶈兘", "涓嶅彲", "涓嶇煡", "涓嶈繃", "涓嶄簡", "涓嶈", "涓嶅悓", "涓嶄細",
    "浠€涔?, "鎬庝箞", "杩欎釜", "閭ｄ釜", "杩欎簺", "閭ｄ簺", "杩欐牱", "閭ｆ牱",
    "宸茬粡", "姝ｅ湪", "鍙互", "搴旇", "蹇呴』", "鍙兘", "鑷繁",
    "鍥犱负", "鎵€浠?, "浣嗘槸", "鑰屼笖", "鎴栬€?, "濡傛灉", "铏界劧", "灏辨槸", "杩樻槸",
    "鍙槸", "鍙湁", "涓嶇", "鏃犺", "浠栦滑", "鎴戜滑", "浣犱滑",
    "鍑烘潵", "璧锋潵", "涓嬫潵", "涓婂幓", "杩囧幓", "鍥炴潵", "杩囨潵", "鍑哄幓",
    "鐜板湪", "褰撴椂", "鏃跺€?, "杩欓噷", "閭ｉ噷", "涔嬪悗", "涔嬪墠", "浠ュ悗", "浠ュ墠",
    "鎴愪负", "浣滀负", "褰撲綔", "鐪嬩綔", "绠楁槸", "鍑烘潵", "璧锋潵", "涓嬪幓",
}
BAD_ENDINGS = set("浜嗙潃杩囧湴寰楁潵鍘诲嚭璧蜂笂涓嬮噷澶栦腑鍙堟槸鐨勮€屾湁鎵€鍦ㄦ妸琚?)

# 鍦板悕/缁勭粐鍚嶇殑鍣０鍓嶇紑锛氫粙璇嶃€佸姩璇嶃€佽櫄璇嶇瓑
BAD_NAME_PREFIXES = set("鍦ㄤ簬鏄粠鐢卞悜寰€鏈濆姣斾负缁欎笌璺熷悓鍜屽強鎴栦絾鑰岀珯鍧愯汉浣忓仠璧拌窇椋?)

BAD_LOCS = {
    "澶у北", "灏忓北", "楂樺北", "娣卞北", "鍑哄北", "灞辨渤", "姹熷北", "澶ф捣", "娣辨捣",
    "涓婃捣", "鍖楁捣", "鍗楁捣", "涓滄捣", "瑗挎捣", "姹熷崡", "娌冲崡", "娌冲寳", "婀栧崡", "婀栧寳",
    "灞变笢", "灞辫タ", "骞夸笢", "骞胯タ", "娴峰崡", "浜戝崡", "鍑哄煄", "杩涘煄", "鏀诲煄", "瀹堝煄",
    "鐮村煄", "鍏ュ煄", "鍑哄叧", "杩囧叧", "鍏冲北",
    "涓嬪北", "涓婂北", "鐏北", "鍐板北", "閾佸北", "閾滃北", "閾跺北", "閲戝北",
}
BAD_ORGS = {
    "鍑洪棬", "寮€闂?, "鍏抽棬", "鏁查棬", "杩涢棬", "鐑棬", "鍐烽棬", "姝ｆ淳", "鍙嶆淳",
    "鑰佹淳", "鏂版淳", "姘旀淳", "鍚岀洘", "缁撶洘", "鑱旂洘", "鍔犵洘", "澶ф", "姝ｆ",
    "鍋忔", "娈垮爞", "澶╁", "榫欏", "鏈堝", "鍐峰", "澶ч棬", "涓棬", "鍚庨棬",
    "鍓嶉棬", "涓撻棬", "閮ㄩ棬", "浣涢棬",
    "鍏ラ棬", "鍑洪棬", "鍏抽棬", "寮€闂?, "閭棬", "瀵归棬", "杩囬棬",
}
BAD_ITEMS = {
    "澶у墤", "灏忓垁", "鐏灙", "閾侀敜", "鏈ㄥ紦", "绾告墖", "鐢电伅", "閾滈暅",
    "鎵撳垁", "鎷斿墤", "閰嶅墤", "甯﹀垁", "鎷挎灙", "涓炬枾", "椋炴枾",
}

def extract_names_rule_based(text: str) -> Dict[str, List[str]]:
    candidates = Counter()
    for i, ch in enumerate(text):
        if ch in SURNAMES:
            for length in [2, 3, 4]:
                if i + length <= len(text):
                    name = text[i:i + length]
                    if all('\u4e00' <= c <= '\u9fff' for c in name):
                        candidates[name] += 1

    filtered = {}
    for name, count in candidates.items():
        if name in BAD_PHRASES:
            continue
        if len(name) == 2 and name[1] in BAD_ENDINGS:
            continue
        filtered[name] = count

    # 淇锛氬墠缂€瑕嗙洊妫€娴嬫洿淇濆畧锛岀煭鍚嶉渶瑕佹樉钁楀浜庨暱鍚嶆墠绉婚櫎闀垮悕
    # 閬垮厤"鏉庢厱濠?(3瀛?琚?鏉庢厱"(2瀛楀墠缂€)璇垹
    to_remove = set()
    for long_name in filtered:
        for short_name in filtered:
            if short_name == long_name or len(short_name) >= len(long_name):
                continue
            if long_name.startswith(short_name):
                ratio = filtered[short_name] / max(filtered[long_name], 1)
                # 鐭悕闇€>=3鍊嶉鐜囨墠鎶戝埗闀垮悕锛堝師閫昏緫涓?=1鍊嶏紝杩囦簬婵€杩涳級
                if ratio >= 3.0:
                    to_remove.add(long_name)
                elif ratio < 0.5:
                    # 闀垮悕杩滃浜庣煭鍚嶏紝鐭悕鍙兘鏄暱鍚嶇殑璇彁鍙栫墖娈?
                    to_remove.add(short_name)
    for n in to_remove:
        del filtered[n]

    loc_org_set = set()
    for suffixes, mx in [(LOC_SUFFIXES, 3), (ORG_SUFFIXES, 2)]:
        pat = re.compile(
            r'([\u4e00-\u9fff]{1,' + str(mx) + r'}(?:' +
            '|'.join(suffixes) + r'))'
        )
        for m in pat.finditer(text):
            loc_org_set.add(m.group(1))

    for name in list(filtered.keys()):
        if len(name) == 2:
            for lo in loc_org_set:
                if lo.startswith(name) and name != lo:
                    del filtered[name]
                    break

    persons = [n for n, _ in Counter(filtered).most_common()][:30]

    loc_pat = re.compile(
        r'([\u4e00-\u9fff]{1,3}(?:' + '|'.join(LOC_SUFFIXES) + r'))'
    )
    locs = set(loc_pat.findall(text))
    locations = sorted(l for l in locs if l not in BAD_LOCS
        and l[0] not in BAD_NAME_PREFIXES
        and not any(c in BAD_NAME_PREFIXES for c in l))[:20]

    org_pat = re.compile(
        r'([\u4e00-\u9fff]{1,2}(?:' + '|'.join(ORG_SUFFIXES) + r'))'
    )
    orgs = set(org_pat.findall(text))
    organizations = sorted(
        o for o in orgs if o not in BAD_ORGS
        and o[0] not in BAD_NAME_PREFIXES
        and not any(c in BAD_NAME_PREFIXES for c in o)
        and not o.startswith("鐨?)
    )[:20]

    item_pat = re.compile(
        r'([\u4e00-\u9fff]{1,3}(?:' + '|'.join(ITEM_SUFFIXES) + r'))'
    )
    items = set(item_pat.findall(text))
    items_result = sorted(i for i in items if i not in BAD_ITEMS)[:15]

    return {
        "person": persons,
        "location": locations,
        "organization": organizations,
        "item": items_result,
        "other": []
    }

# ============ API 璺敱 ============

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/api/rewrite", response_model=RewriteResponse)
async def rewrite_text(req: RewriteRequest, request: Request):
    _check_rate_limit(request.client.host if request.client else "0.0.0.0")
    try:
        original = req.text
        rewritten = req.text
        rep_details = []

        if req.use_ai and req.api_key:
            try:
                rewritten = ai_rewrite(
                    rewritten, req.api_key,
                    req.ai_intensity, req.ai_provider
                )
            except Exception as e:
                rep_details.append({
                    "original": "鈿狅笍",
                    "replacement": f"AI鏀瑰啓澶辫触: {e}",
                    "count": 0
                })

        rewritten, rule_reps = apply_rules(rewritten, req.rules)
        rep_details.extend(rule_reps)

        return RewriteResponse(
            original=original,
            rewritten=rewritten,
            replacements=rep_details
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/api/extract")
async def extract_names(req: ExtractRequest):
    try:
        return {"names": extract_names_rule_based(req.text)}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

# ============ SSE娴佸紡缈绘敼 ============

@app.post("/api/rewrite/stream")
async def rewrite_stream(req: RewriteRequest, request: Request):
    _check_rate_limit(request.client.host if request.client else "0.0.0.0")
    if not req.use_ai or not req.api_key:
        raise HTTPException(status_code=400, detail="娴佸紡缈绘敼闇€瑕佸惎鐢ˋI骞舵彁渚汚PI Key")

    async def event_generator():
        import asyncio
        try:
            yield f"data: {json.dumps({'type': 'status', 'msg': 'AI鏀瑰啓涓?..'}, ensure_ascii=False)}\n\n"
            desc = {
                "light": "杞诲井鏀瑰啓锛屽彧鏇挎崲閮ㄥ垎璇嶆眹锛屼繚鎸佸彞寮忕粨鏋?,
                "medium": "涓瓑鏀瑰啓锛屽彉鎹㈠彞寮忓拰琛ㄨ揪锛屼繚鎸佸墽鎯呬笉鍙?,
                "heavy": "澶у箙鏀瑰啓锛屾崲鍙欒堪椋庢牸锛屼繚鎸佸墽鎯呮鏋朵笉鍙?
            }
            prompt = f"""浣犳槸涓撲笟灏忚鏀瑰啓甯堛€傝姹傦細
1. {desc.get(req.ai_intensity, desc['medium'])}
2. 鍓ф儏瀹屽叏涓嶅彉锛屼汉鐗?鍦扮偣/鐗╁搧鍚嶇О涓嶅彉
3. 淇濇寔鍘熸湁鏂囬
4. 涓嶆坊鍔犱笉鍒犲噺鍐呭
5. 鍙緭鍑烘敼鍐欏悗鐨勬枃鏈紝涓嶈瑙ｉ噴

鍘熸枃锛?
{req.text}"""
            stream_gen = call_ai(prompt, req.api_key, req.ai_provider, stream=True)
            full_text = ""
            for chunk in stream_gen:
                full_text += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)

            rewritten, rep_details = apply_rules(full_text, req.rules)
            total = sum(r["count"] for r in rep_details)
            yield f"data: {json.dumps({'type': 'done', 'rewritten': rewritten, 'replacements': rep_details, 'replace_count': total}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'Rewrite stream error'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ============ 鎼滅储 API ============

@app.get("/api/books/search")
async def search_books(q: str = "", scope: str = "title", limit: int = 20, offset: int = 0):
    if not q:
        return {"results": [], "total": 0}
    limit = min(limit, 50)
    conn = _get_conn()
    cur = conn.cursor()

    if scope == "title":
        cur.execute(
            "SELECT id, title, author, created_at FROM books WHERE title LIKE ? OR author LIKE ? ORDER BY created_at LIMIT ? OFFSET ?",
            (f"%{q}%", f"%{q}%", limit+offset, 0)
        )
        rows = cur.fetchall()
        results = []
        for r in rows:
            cur2 = conn.cursor()
            cur2.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (r["id"],))
            ch_count = cur2.fetchone()[0]
            results.append({
                "id": r["id"],
                "title": r["title"],
                "author": r["author"],
                "chapter_count": ch_count,
            })
        total = len(results)
        conn.close()
        return {"results": results[offset:offset+limit], "total": total}
    else:
        # 鍐呭鎼滅储
        cur.execute("SELECT id, title, author FROM books")
        books = cur.fetchall()
        results = []
        for b in books:
            cur2 = conn.cursor()
            if False:
                pass
            # 鎼滅储绔犺妭鍐呭
            cur2.execute(
                "SELECT id, title, content FROM chapters WHERE book_id=? AND (title LIKE ? OR content LIKE ?) ORDER BY sort_order LIMIT 5",
                (b["id"], f"%{q}%", f"%{q}%")
            )
            matched = cur2.fetchall()
            if matched:
                matched_chapters = []
                for ch in matched:
                    content = ch["content"]
                    idx = content.find(q)
                    snippet = content[max(0, idx-30):idx+len(q)+30] if idx >= 0 else ch["title"]
                    matched_chapters.append({
                        "id": ch["id"],
                        "title": ch["title"],
                        "snippet": snippet.replace(q, f"銆恵q}銆?),
                    })
                results.append({
                    "id": b["id"],
                    "title": b["title"],
                    "author": b["author"],
                    "matched_chapters": matched_chapters,
                })
        total = len(results)
        conn.close()
        return {"results": results[offset:offset+limit], "total": total}

# ============ 绔犺妭鎺掑簭 API ============

class ChapterReorder(BaseModel):
    chapter_ids: List[str]

@app.put("/api/books/{book_id}/chapters/reorder")
async def reorder_chapters(book_id: str, req: ChapterReorder):
    _reorder_chapters(book_id, req.chapter_ids)
    return {"ok": True}

# ============ 涔﹀簱瀵煎嚭 API ============

@app.get("/api/books/export")
async def export_books(book_id: str = None, format: str = "txt"):
    conn = _get_conn()
    cur = conn.cursor()
    if book_id:
        cur.execute("SELECT id FROM books WHERE id=?", (book_id,))
        ids = [r["id"] for r in cur.fetchall()]
    else:
        cur.execute("SELECT id FROM books")
        ids = [r["id"] for r in cur.fetchall()]
    if not ids:
        conn.close()
        raise HTTPException(status_code=404, detail="鏈壘鍒颁功绫?)

    books = []
    for bid in ids:
        book = _get_book(bid)
        if book:
            books.append(book)
    conn.close()

    if format == "json":
        content = json.dumps(books, ensure_ascii=False, indent=2)
        return HTMLResponse(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=books_export.json"}
        )
    else:
        lines = []
        for b in books:
            lines.append(f"銆妠b['title']}銆?浣滆€咃細{b.get('author', '鏈煡')}")
            lines.append("=" * 40)
            for ch in b.get("chapters", []):
                lines.append(f"\n{ch['title']}")
                lines.append("-" * 30)
                lines.append(ch.get("content", ""))
            lines.append("\n" + "=" * 40 + "\n")
        content = "\n".join(lines)
        return HTMLResponse(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=books_export.txt"}
        )

# ---- 鏂囦欢瀵煎叆 ----

@app.post("/api/import")
async def import_file(data: dict):
    content = data.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="鍐呭涓嶈兘涓虹┖")
    truncated = len(content) > 500000
    return {"content": content[:500000], "length": len(content), "truncated": truncated}

# ============ 涔﹀簱 API ============

@app.get("/api/books")
async def list_books():
    books = _load_books()
    result = []
    for b in books:
        result.append({
            "id": b["id"],
            "title": b["title"],
            "author": b.get("author", ""),
            "chapter_count": b["chapter_count"],
            "created_at": b.get("created_at", ""),
            "updated_at": b.get("updated_at", ""),
        })
    return {"books": result}

@app.post("/api/books")
async def create_book(req: BookCreate):
    book_id = _add_book(req.title, req.author, req.chapters)
    return {"id": book_id, "title": req.title}

@app.get("/api/books/{book_id}")
async def get_book(book_id: str):
    book = _get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="涔︾睄涓嶅瓨鍦?)
    return book

@app.delete("/api/books/{book_id}")
async def delete_book(book_id: str):
    _delete_book(book_id)
    return {"ok": True}

class BatchDelete(BaseModel):
    ids: List[str]

class BatchUpdate(BaseModel):
    ids: List[str]
    author: Optional[str] = None

@app.post("/api/admin/books/batch-delete")
async def batch_delete_books(req: BatchDelete, request: Request, token: str = ""):
    if not _get_admin_token(request, token):
        raise HTTPException(401, "鏈巿鏉?)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        f"DELETE FROM books WHERE id IN ({','.join('?'*len(req.ids))})",
        req.ids
    )
    conn.commit()
    return {"ok": True, "deleted": cur.rowcount}

@app.post("/api/admin/books/batch-update")
async def batch_update_books(req: BatchUpdate, request: Request, token: str = ""):
    if not _get_admin_token(request, token):
        raise HTTPException(401, "鏈巿鏉?)
    if not req.author:
        raise HTTPException(400, "author is required")
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE books SET author=?, updated_at=? WHERE id IN ({','.join('?'*len(req.ids))})",
        [req.author, datetime.now().isoformat()] + req.ids
    )
    conn.commit()
    return {"ok": True, "updated": cur.rowcount}

class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None

@app.put("/api/books/{book_id}")
async def update_book(book_id: str, req: BookUpdate):
    if req.title is not None or req.author is not None:
        conn = _get_conn()
        cur = conn.cursor()
        fields = []
        vals = []
        if req.title is not None:
            fields.append("title=?")
            vals.append(req.title)
        if req.author is not None:
            fields.append("author=?")
            vals.append(req.author)
        vals.extend([datetime.now().isoformat()[:19], book_id])
        cur.execute(f"UPDATE books SET {','.join(fields)}, updated_at=? WHERE id=?", vals)
        conn.commit()
        conn.close()
    return {"ok": True}

@app.post("/api/books/{book_id}/chapters")
async def add_chapter(book_id: str, req: ChapterAdd):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,))
    count = cur.fetchone()[0]
    ch_id = f"ch_{book_id}_{count+1}"
    cur.execute(
        "INSERT INTO chapters (id, book_id, title, content, sort_order) VALUES (?,?,?,?,?)",
        (ch_id, book_id, req.title, req.content, count)
    )
    cur.execute("UPDATE books SET updated_at=? WHERE id=?",
                (datetime.now().isoformat()[:19], book_id))
    conn.commit()
    conn.close()
    return {"id": ch_id}

class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None

@app.put("/api/books/{book_id}/chapters/{ch_id}")
async def update_chapter(book_id: str, ch_id: str, req: ChapterUpdate):
    _update_chapter(book_id, ch_id, title=req.title, content=req.content)
    return {"ok": True}

@app.delete("/api/books/{book_id}/chapters/{ch_id}")
async def delete_chapter(book_id: str, ch_id: str):
    _delete_chapter(book_id, ch_id)
    return {"ok": True}

# ============ 瑙勫垯妯℃澘 API ============

@app.get("/api/rules")
async def list_rules():
    return {"rules": _load_rules()}

@app.post("/api/rules")
async def save_rules(req: RulesSave):
    rule_id = _save_rule(req.name, [{"original": r.original, "replacement": r.replacement} for r in req.rules])
    return {"id": rule_id}

@app.delete("/api/rules/{rule_id}")
async def delete_rules(rule_id: str):
    _delete_rule(rule_id)
    return {"ok": True}

# ============ 鏁存湰涔︾炕鏀?API ============

class BookRewriteRequest(BaseModel):
    rules: List[ReplaceRule]
    use_ai: bool = False
    ai_intensity: str = "medium"
    api_key: Optional[str] = None
    ai_provider: str = "zhipu"
    chapter_ids: Optional[List[str]] = None

@app.post("/api/books/{book_id}/rewrite")
async def rewrite_book(book_id: str, req: BookRewriteRequest):
    book = _get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="涔︾睄涓嶅瓨鍦?)

    chapters = book.get("chapters", [])
    if req.chapter_ids:
        chapters = [ch for ch in chapters if ch["id"] in req.chapter_ids]

    if not chapters:
        raise HTTPException(status_code=400, detail="娌℃湁鍙炕鏀圭殑绔犺妭")

    results = []
    for ch in chapters:
        text = ch.get("content", "")
        rewritten = text
        rep_details = []

        if req.use_ai and req.api_key:
            try:
                rewritten = ai_rewrite(
                    rewritten, req.api_key,
                    req.ai_intensity, req.ai_provider
                )
            except Exception as e:
                rep_details.append({
                    "original": "鈿狅笍",
                    "replacement": f"AI鏀瑰啓澶辫触: {e}",
                    "count": 0
                })

        rewritten, rule_reps = apply_rules(rewritten, req.rules)
        rep_details.extend(rule_reps)

        total = sum(r["count"] for r in rep_details if r["original"] != "鈿狅笍")
        results.append({
            "id": ch["id"],
            "title": ch["title"],
            "original": text,
            "rewritten": rewritten,
            "replacements": rep_details,
            "replace_count": total,
        })

    return {
        "book_id": book_id,
        "book_title": book["title"],
        "total_chapters": len(results),
        "total_replacements": sum(r["replace_count"] for r in results),
        "chapters": results,
    }

# ============ 鍋ュ悍妫€鏌?============

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "7.3.0"}

# ============ 绠＄悊鍛樿璇?============

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key="novel-rewriter-secret-key-2026")

class AdminLogin(BaseModel):
    password: str

@app.post("/api/admin/login")
async def admin_login(req: AdminLogin, request: Request):
    if req.password != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="瀵嗙爜閿欒")
    request.session["admin_authed"] = True
    return {"ok": True, "token": ADMIN_PWD}

@app.get("/api/admin/stats")
async def admin_stats(request: Request, token: str = ""):
    if _get_admin_token(request, token) != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="鏈巿鏉?)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM books")
    book_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM chapters")
    chapter_count = cur.fetchone()[0]
    cur.execute("SELECT SUM(LENGTH(content)) FROM chapters")
    total_chars = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM rules")
    template_count = cur.fetchone()[0]
    conn.close()
    return {
        "book_count": book_count,
        "chapter_count": chapter_count,
        "total_chars": total_chars,
        "template_count": template_count,
    }

@app.post("/api/admin/seed")
async def admin_reseed(request: Request, token: str = ""):
    if _get_admin_token(request, token) != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="鏈巿鏉?)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM chapters")
    cur.execute("DELETE FROM books")
    conn.commit()
    conn.close()
    _seed_db()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM books")
    count = cur.fetchone()[0]
    conn.close()
    return {"ok": True, "book_count": count}

@app.put("/api/admin/books/{book_id}/chapters/batch")
async def batch_add_chapters(book_id: str, data: dict, request: Request, token: str = ""):
    if _get_admin_token(request, token) != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="鏈巿鏉?)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,))
    existing = cur.fetchone()[0]
    chapters = data.get("chapters", [])
    for i, ch in enumerate(chapters):
        ch_id = f"ch_{book_id}_{existing+i+1:02d}"
        cur.execute(
            "INSERT INTO chapters (id, book_id, title, content, sort_order) VALUES (?,?,?,?,?)",
            (ch_id, book_id, ch.get("title", f"绗瑊existing+i+1}绔?), ch.get("content", ""), existing+i)
        )
    cur.execute("UPDATE books SET updated_at=? WHERE id=?",
                (datetime.now().isoformat()[:19], book_id))
    conn.commit()
    conn.close()
    return {"ok": True, "added": len(chapters)}

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("static/admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

from starlette.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
