#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说翻改工具 v7.4.0
新功能：种子数据外部JSON化（解决冷启动超时）、
      SQLite数据持久化（解决Render重启丢数据问题）、
      搜索结果高亮、整本导出JSON、拖拽排序data-id、
      PWA manifest icons修复、版本号统一
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response, FileResponse
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
from ebooklib import epub
import uuid
import io

app = FastAPI(title="Novel Rewriter v7.4.0")

ADMIN_PWD = os.environ.get("ADMIN_PASSWORD", "admin123")
DB_PATH = os.environ.get("DB_PATH", "data/novel_rewriter.db")

# ============ SQLite 初始化 ============

def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 书籍表
    c.execute("""CREATE TABLE IF NOT EXISTS books (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        author TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT
    )""")
    # 章节表
    c.execute("""CREATE TABLE IF NOT EXISTS chapters (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 0,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    )""")
    # 规则模板表
    c.execute("""CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        rules_json TEXT NOT NULL,
        created_at TEXT
    )""")
    # 迁移：如果表存在但缺少 sort_order 列，则添加
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

# ============ 种子数据 ============
# 种子数据已提取至 seed_books.json，启动时按需加载

_SEED_BOOKS_CACHE = None

def _load_seed_books():
    """从 seed_books.json 加载种子数据（带缓存）"""
    global _SEED_BOOKS_CACHE
    if _SEED_BOOKS_CACHE is None:
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed_books.json')
        with open(json_path, 'r', encoding='utf-8') as f:
            _SEED_BOOKS_CACHE = json.load(f)
    return _SEED_BOOKS_CACHE


def _seed_db():
    """如果数据库为空，灌入种子数据"""
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

# ============ 数据访问辅助 ============

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
    # 重新排序
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
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    RATE_LIMIT[ip].append(now)
    # 清理过期IP（防止内存泄漏）
    if len(RATE_LIMIT) > 1000:
        cutoff = now - 300
        for key in list(RATE_LIMIT.keys()):
            if all(t < cutoff for t in RATE_LIMIT[key]):
                del RATE_LIMIT[key]

# ============ 管理员Token ============

def _get_admin_token(request: Request, token: str = ""):
    from starlette.requests import Request as StarletteRequest
    session = request.session
    if session.get("admin_authed"):
        return ADMIN_PWD
    if token and token == ADMIN_PWD:
        return ADMIN_PWD
    return None

# ============ 数据模型 ============

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

# ============ 核心逻辑 ============

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
        raise ValueError("AI 返回格式异常")

def ai_rewrite(text: str, api_key: str,
               intensity: str = "medium", provider: str = "zhipu") -> str:
    desc = {
        "light": "轻微改写，只替换部分词汇，保持句式结构",
        "medium": "中等改写，变换句式和表达，保持剧情不变",
        "heavy": "大幅改写，换叙述风格，保持剧情框架不变"
    }
    prompt = f"""你是专业小说改写师。要求：
1. {desc.get(intensity, desc['medium'])}
2. 剧情完全不变，人物/地点/物品名称不变
3. 保持原有文风
4. 不添加不删减内容
5. 只输出改写后的文本，不要解释

原文：
{text}"""
    return call_ai(prompt, api_key, provider)

# ---- 名称提取 ----

SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华"
    "金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞"
    "任袁柳鲍史唐薛雷贺倪汤殷罗毕郝安常齐康伍余元卜顾孟平黄和穆萧"
    "尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮"
    "蓝闵席季麻强贾路娄危江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万"
    "支柯管卢莫经房干解应宗丁邓郁单洪包诸左石崔龚程裴陆荣曲家封储靳段"
    "富巫乌焦巴弓牧山谷车侯班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙"
    "叶幸司黎薄印宿白怀蒲邰从鄂索咸籍赖卓屠蒙池乔曾沙养鞠须丰巢关相查"
    "后荆红游权盖益桓公药古魔邪赤青白紫玄天幽冥血影灵仙剑圣尊帝皇王"
)
LOC_SUFFIXES = ("城", "山", "谷", "海", "岛", "湖", "河", "江",
                  "峰", "崖", "洞", "窟", "林", "原", "漠", "泽",
                  "渊", "潭", "溪", "泉", "州", "郡", "省", "镇",
                  "村", "关", "渡", "桥", "亭")
ORG_SUFFIXES = ("门", "派", "宗", "阁", "楼", "庄", "堡", "寨",
                  "宫", "殿", "堂", "院", "帮", "盟", "教", "寺", "观")
ITEM_SUFFIXES = ("剑", "刀", "枪", "斧", "锤", "弓", "扇", "珠",
                  "塔", "鼎", "镜", "瓶", "灯", "印", "符", "丹",
                  "药", "草", "诀", "典", "图", "卷", "令", "牌")

BAD_PHRASES = {
    "一个", "一些", "一样", "一直", "一时", "一切", "一起", "一般",
    "不是", "不能", "不可", "不知", "不过", "不了", "不要", "不同", "不会",
    "什么", "怎么", "这个", "那个", "这些", "那些", "这样", "那样",
    "已经", "正在", "可以", "应该", "必须", "可能", "自己",
    "因为", "所以", "但是", "而且", "或者", "如果", "虽然", "就是", "还是",
    "只是", "只有", "不管", "无论", "他们", "我们", "你们",
    "出来", "起来", "下来", "上去", "过去", "回来", "过来", "出去",
    "现在", "当时", "时候", "这里", "那里", "之后", "之前", "以后", "以前",
    "成为", "作为", "当作", "看作", "算是", "出来", "起来", "下去",
}
BAD_ENDINGS = set("了着过地得来去出起上下里外中又是的而有所在把被")

# 地名/组织名的噪声前缀：介词、动词、虚词等
BAD_NAME_PREFIXES = set("在于是从由向往朝对比为给与跟同和及或但而站坐躺住停走跑飞")

BAD_LOCS = {
    "大山", "小山", "高山", "深山", "出山", "山河", "江山", "大海", "深海",
    "上海", "北海", "南海", "东海", "西海", "江南", "河南", "河北", "湖南", "湖北",
    "山东", "山西", "广东", "广西", "海南", "云南", "出城", "进城", "攻城", "守城",
    "破城", "入城", "出关", "过关", "关山",
    "下山", "上山", "火山", "冰山", "铁山", "铜山", "银山", "金山",
}
BAD_ORGS = {
    "出门", "开门", "关门", "敲门", "进门", "热门", "冷门", "正派", "反派",
    "老派", "新派", "气派", "同盟", "结盟", "联盟", "加盟", "大殿", "正殿",
    "偏殿", "殿堂", "天宫", "龙宫", "月宫", "冷宫", "大门", "中门", "后门",
    "前门", "专门", "部门", "佛门",
    "入门", "出门", "关门", "开门", "邪门", "对门", "过门",
}
BAD_ITEMS = {
    "大剑", "小刀", "火枪", "铁锤", "木弓", "纸扇", "电灯", "铜镜",
    "打刀", "拔剑", "配剑", "带刀", "拿枪", "举斧", "飞斧",
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

    # 修复：前缀覆盖检测更保守，短名需要显著多于长名才移除长名
    # 避免"李慕婉"(3字)被"李慕"(2字前缀)误删
    to_remove = set()
    for long_name in filtered:
        for short_name in filtered:
            if short_name == long_name or len(short_name) >= len(long_name):
                continue
            if long_name.startswith(short_name):
                ratio = filtered[short_name] / max(filtered[long_name], 1)
                # 短名需>=3倍频率才抑制长名（原逻辑为>=1倍，过于激进）
                if ratio >= 3.0:
                    to_remove.add(long_name)
                elif ratio < 0.5:
                    # 长名远多于短名，短名可能是长名的误提取片段
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
        and not o.startswith("的")
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

# ============ API 路由 ============

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
                    "original": "⚠️",
                    "replacement": "AI改写失败，请重试",
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

# ============ SSE流式翻改 ============

@app.post("/api/rewrite/stream")
async def rewrite_stream(req: RewriteRequest, request: Request):
    _check_rate_limit(request.client.host if request.client else "0.0.0.0")
    if not req.use_ai or not req.api_key:
        raise HTTPException(status_code=400, detail="流式翻改需要启用AI并提供API Key")

    async def event_generator():
        import asyncio
        try:
            yield f"data: {json.dumps({'type': 'status', 'msg': 'AI改写中...'}, ensure_ascii=False)}\n\n"
            desc = {
                "light": "轻微改写，只替换部分词汇，保持句式结构",
                "medium": "中等改写，变换句式和表达，保持剧情不变",
                "heavy": "大幅改写，换叙述风格，保持剧情框架不变"
            }
            prompt = f"""你是专业小说改写师。要求：
1. {desc.get(req.ai_intensity, desc['medium'])}
2. 剧情完全不变，人物/地点/物品名称不变
3. 保持原有文风
4. 不添加不删减内容
5. 只输出改写后的文本，不要解释

原文：
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

# ============ 搜索 API ============

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
        # 内容搜索
        cur.execute("SELECT id, title, author FROM books")
        books = cur.fetchall()
        results = []
        for b in books:
            cur2 = conn.cursor()
            if False:
                pass
            # 搜索章节内容
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
                        "snippet": snippet.replace(q, f"【{q}】"),
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

# ============ 章节排序 API ============

class ChapterReorder(BaseModel):
    chapter_ids: List[str]

@app.put("/api/books/{book_id}/chapters/reorder")
async def reorder_chapters(book_id: str, req: ChapterReorder):
    _reorder_chapters(book_id, req.chapter_ids)
    return {"ok": True}

# ============ EPUB 生成 ============

def _generate_epub(books: list) -> tuple:
    """生成 EPUB 文件字节流，返回 (bytes, filename)"""
    book_epub = epub.EpubBook()

    if len(books) == 1:
        b = books[0]
        book_epub.set_title(b.get("title", "小说合集"))
        book_epub.add_author(b.get("author", "未知作者"))
        filename = f"{b['title']}.epub"
    else:
        book_epub.set_title("小说合集")
        book_epub.add_author("多作者")
        filename = "novel_collection.epub"
    book_epub.set_language("zh")
    book_epub.add_metadata(None, "meta", "", {"name": "generator", "content": "Novel Rewriter"})

    spine = []
    toc = []

    style = epub.EpubItem(
        uid="style", file_name="style/default.css", media_type="text/css",
        content=b"body { font-family: 'Noto Sans CJK SC', 'SimSun', serif; line-height: 1.9; padding: 0 1em; } "
               b"h1 { text-align: center; margin-top: 1em; } "
               b"h2 { text-align: center; color: #555; } "
               b"h3 { margin-top: 1.5em; } "
               b"p { text-indent: 2em; margin: 0.5em 0; }"
    )
    book_epub.add_item(style)

    cover_html = f'<html><head><title>目录</title><link rel="stylesheet" href="style/default.css" type="text/css"/></head><body><h1>{book_epub.title}</h1><h2>{book_epub.get_metadata("DC", "creator")}</h2></body></html>'
    cover = epub.EpubHtml(title="封面", file_name="cover.xhtml", lang="zh")
    cover.content = cover_html
    cover.add_item(style)
    book_epub.add_item(cover)
    spine.append(cover)
    toc.append(epub.Link("cover.xhtml", "封面", "cover"))

    ch_num = 0
    for book_idx, b in enumerate(books):
        chapters = b.get("chapters", [])
        if len(books) > 1 and chapters:
            book_title_html = f'<html><head><title>{b["title"]}</title></head><body><h1>{b["title"]}</h1><h2>{b.get("author", "")}</h2></body></html>'
            book_title_page = epub.EpubHtml(title=b["title"], file_name=f"book_{book_idx}.xhtml", lang="zh")
            book_title_page.content = book_title_html
            book_title_page.add_item(style)
            book_epub.add_item(book_title_page)
            spine.append(book_title_page)
            toc.append(epub.Link(f"book_{book_idx}.xhtml", b["title"], f"book_{book_idx}"))

        toc_children = []
        for ch in chapters:
            ch_num += 1
            file_id = f"ch_{ch_num:04d}"
            title = ch.get("title", f"第{ch_num}章")
            content = ch.get("content", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            paragraphs = content.split("\n")
            p_html = "".join(f"<p>{p or '&nbsp;'}</p>" for p in paragraphs)
            ch_html = f'<html><head><title>{title}</title><link rel="stylesheet" href="style/default.css" type="text/css"/></head><body><h3>{title}</h3>{p_html}</body></html>'
            ch_item = epub.EpubHtml(title=title, file_name=f"{file_id}.xhtml", lang="zh")
            ch_item.content = ch_html
            ch_item.add_item(style)
            book_epub.add_item(ch_item)
            spine.append(ch_item)
            toc_children.append(epub.Link(f"{file_id}.xhtml", title, file_id))

        if len(books) > 1 and toc_children:
            toc[-1].children = toc_children
        else:
            toc.extend(toc_children)

    book_epub.spine = spine
    book_epub.toc = toc
    book_epub.add_item(epub.EpubNcx())
    book_epub.add_item(epub.EpubNav())

    buf = io.BytesIO()
    epub.write_epub(buf, book_epub, {})
    return buf.getvalue(), filename


# ============ 书库导出 API ============

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
        raise HTTPException(status_code=404, detail="未找到书籍")

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
    elif format == "epub":
        epub_bytes, epub_filename = _generate_epub(books)
        return Response(
            content=epub_bytes,
            media_type="application/epub+zip",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{epub_filename}"}
        )
    else:
        lines = []
        for b in books:
            lines.append(f"《{b['title']}》 作者：{b.get('author', '未知')}")
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

# ---- 文件导入 ----

@app.post("/api/import")
async def import_file(data: dict):
    content = data.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    truncated = len(content) > 500000
    return {"content": content[:500000], "length": len(content), "truncated": truncated}

# ============ 书库 API ============

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
        raise HTTPException(status_code=404, detail="书籍不存在")
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
        raise HTTPException(401, "未授权")
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
        raise HTTPException(401, "未授权")
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

# ============ 规则模板 API ============

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

# ============ 整本书翻改 API ============

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
        raise HTTPException(status_code=404, detail="书籍不存在")

    chapters = book.get("chapters", [])
    if req.chapter_ids:
        chapters = [ch for ch in chapters if ch["id"] in req.chapter_ids]

    if not chapters:
        raise HTTPException(status_code=400, detail="没有可翻改的章节")

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
                    "original": "⚠️",
                    "replacement": "AI改写失败，请重试",
                    "count": 0
                })

        rewritten, rule_reps = apply_rules(rewritten, req.rules)
        rep_details.extend(rule_reps)

        total = sum(r["count"] for r in rep_details if r["original"] != "⚠️")
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

# ============ 健康检查 ============

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "7.4.0"}

# ============ 管理员认证 ============

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key="novel-rewriter-secret-key-2026")

class AdminLogin(BaseModel):
    password: str

@app.post("/api/admin/login")
async def admin_login(req: AdminLogin, request: Request):
    if req.password != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="密码错误")
    request.session["admin_authed"] = True
    return {"ok": True, "token": ADMIN_PWD}

@app.get("/api/admin/stats")
async def admin_stats(request: Request, token: str = ""):
    if _get_admin_token(request, token) != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="未授权")
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
        raise HTTPException(status_code=401, detail="未授权")
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
        raise HTTPException(status_code=401, detail="未授权")
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,))
    existing = cur.fetchone()[0]
    chapters = data.get("chapters", [])
    for i, ch in enumerate(chapters):
        ch_id = f"ch_{book_id}_{existing+i+1:02d}"
        cur.execute(
            "INSERT INTO chapters (id, book_id, title, content, sort_order) VALUES (?,?,?,?,?)",
            (ch_id, book_id, ch.get("title", f"第{existing+i+1}章"), ch.get("content", ""), existing+i)
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

@app.get("/robots.txt")
async def robots():
    return FileResponse("static/robots.txt", media_type="text/plain")

@app.get("/sitemap.xml")
async def sitemap():
    return FileResponse("static/sitemap.xml", media_type="application/xml")

app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
