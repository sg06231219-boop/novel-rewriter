#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说翻改工具 v3.0 — 含书库功能"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import uvicorn
import re
import json
import os
import httpx
from collections import Counter
from datetime import datetime

app = FastAPI(title="小说翻改工具 v3.0")

DATA_DIR = os.environ.get("DATA_DIR", "data")
BOOKS_FILE = os.path.join(DATA_DIR, "books.json")
RULES_FILE = os.path.join(DATA_DIR, "rules.json")

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)

def _load_json(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

class RewriteResponse(BaseModel):
    original: str
    rewritten: str
    replacements: List[Dict]

class ExtractRequest(BaseModel):
    text: str

class BookCreate(BaseModel):
    title: str
    author: str = ""
    chapters: List[Dict] = []  # [{title, content}]

class ChapterAdd(BaseModel):
    title: str
    content: str

class RulesSave(BaseModel):
    name: str
    rules: List[ReplaceRule]

# ============ 核心逻辑 ============

def apply_rules(text: str, rules: List[ReplaceRule]) -> str:
    result = text
    for rule in sorted(rules, key=lambda r: len(r.original), reverse=True):
        if rule.original and rule.replacement:
            result = result.replace(rule.original, rule.replacement)
    return result

def call_zhipu(prompt: str, api_key: str) -> str:
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    with httpx.Client(timeout=120) as client:
        resp = client.post(url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }, json={
            "model": "glm-4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 4096
        })
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def ai_rewrite(text: str, api_key: str, intensity: str = "medium") -> str:
    desc = {"light": "轻微改写，只替换部分词汇，保持句式", "medium": "中等改写，变换句式和表达，剧情不变", "heavy": "大幅改写，换叙述风格，剧情不变"}
    prompt = f"""你是专业小说改写师。要求：1.{desc.get(intensity, desc['medium'])} 2.剧情完全不变 3.保持文风 4.不添加不删减 5.专有名词不变 6.只输出改写文本
原文：
{text}"""
    return call_zhipu(prompt, api_key)

LOC_SUFFIXES = ("城","山","谷","海","岛","湖","河","江","峰","崖",
                "洞","窟","林","原","漠","泽","渊","潭","溪","泉",
                "州","郡","省","镇","村","关","渡","桥","亭")
ORG_SUFFIXES = ("门","派","宗","阁","楼","庄","堡","寨","宫","殿",
                "堂","院","帮","盟","教","寺","观")
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

def extract_names_rule_based(text: str) -> Dict[str, List[str]]:
    candidates = Counter()
    for i, ch in enumerate(text):
        if ch in SURNAMES:
            for length in [2, 3, 4]:
                if i + length <= len(text):
                    name = text[i:i+length]
                    if all('\u4e00' <= c <= '\u9fff' for c in name):
                        candidates[name] += 1

    bad_endings = set("了着过地得来去出起上下里外中又是的而有所在把被")
    bad_phrases = {
        "一个","一些","一样","一直","一时","一切","一起","一般",
        "不是","不能","不可","不知","不过","不了","不要","不同","不会",
        "什么","怎么","这个","那个","这些","那些","这样","那样",
        "已经","正在","可以","应该","必须","可能","自己",
        "因为","所以","但是","而且","或者","如果","虽然","就是","还是",
        "只是","只有","不管","无论","他们","我们","你们",
        "出来","起来","下来","上去","过去","回来","过来","出去",
        "现在","当时","时候","这里","那里","之后","之前","以后","以前",
        "成为","作为","当作","看作","算是","出来","起来","下去",
    }

    filtered = {}
    for name, count in candidates.items():
        if name in bad_phrases: continue
        if len(name) == 2 and name[1] in bad_endings: continue
        if any(c in name for c in '，。！？、；：''""（）【】《》'): continue
        filtered[name] = count

    to_remove = set()
    for long_name in filtered:
        for short_name in filtered:
            if short_name == long_name or len(short_name) >= len(long_name): continue
            if long_name.startswith(short_name) and filtered[short_name] >= filtered[long_name]:
                to_remove.add(long_name)
    for n in to_remove: del filtered[n]

    loc_org_set = set()
    for suffixes, mx in [(LOC_SUFFIXES, 3), (ORG_SUFFIXES, 2)]:
        pat = re.compile(r'([\u4e00-\u9fff]{1,' + str(mx) + r'}(?:' + '|'.join(suffixes) + r'))')
        for m in pat.finditer(text): loc_org_set.add(m.group(1))

    for name in list(filtered.keys()):
        if len(name) == 2:
            for lo in loc_org_set:
                if lo.startswith(name) and name != lo:
                    del filtered[name]
                    break

    persons = [n for n, _ in Counter(filtered).most_common()][:30]

    loc_pat = re.compile(r'([\u4e00-\u9fff]{1,3}(?:' + '|'.join(LOC_SUFFIXES) + r'))')
    locs = set(loc_pat.findall(text))
    bad_locs = {"大山","小山","高山","深山","出山","山河","江山","大海","深海",
                "上海","北海","南海","东海","西海","江南","河南","河北","湖南","湖北",
                "山东","山西","广东","广西","海南","云南","出城","进城","攻城","守城","破城","入城",
                "出关","过关","关山"}
    locations = sorted(l for l in locs if l not in bad_locs)[:20]

    org_pat = re.compile(r'([\u4e00-\u9fff]{1,2}(?:' + '|'.join(ORG_SUFFIXES) + r'))')
    orgs = set(org_pat.findall(text))
    bad_orgs = {"出门","开门","关门","敲门","进门","热门","冷门","正派","反派",
                "老派","新派","气派","同盟","结盟","联盟","加盟","大殿","正殿",
                "偏殿","殿堂","天宫","龙宫","月宫","冷宫","大门","中门","后门",
                "前门","专门","部门","佛门"}
    organizations = sorted(o for o in orgs if o not in bad_orgs and not o.startswith("的"))[:20]

    return {"person": persons, "location": locations, "organization": organizations, "item": [], "other": []}

# ============ API路由 ============

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/api/rewrite", response_model=RewriteResponse)
async def rewrite_text(req: RewriteRequest):
    try:
        original = req.text
        rewritten = apply_rules(req.text, req.rules)
        replacements = []
        for rule in req.rules:
            if rule.original in req.text:
                replacements.append({"original": rule.original, "replacement": rule.replacement, "count": req.text.count(rule.original)})
        if req.use_ai and req.api_key:
            try:
                rewritten = ai_rewrite(rewritten, req.api_key, req.ai_intensity)
            except Exception as e:
                replacements.append({"original": "⚠️", "replacement": f"AI改写失败: {e}", "count": 0})
        return RewriteResponse(original=original, rewritten=rewritten, replacements=replacements)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/extract")
async def extract_names(req: ExtractRequest):
    try:
        return {"names": extract_names_rule_based(req.text)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============ 书库 API ============

@app.get("/api/books")
async def list_books():
    books = _load_json(BOOKS_FILE, [])
    # 只返回摘要，不返回章节内容
    result = []
    for b in books:
        result.append({
            "id": b["id"],
            "title": b["title"],
            "author": b.get("author", ""),
            "chapter_count": len(b.get("chapters", [])),
            "created_at": b.get("created_at", ""),
            "updated_at": b.get("updated_at", ""),
        })
    return {"books": result}

@app.post("/api/books")
async def create_book(req: BookCreate):
    books = _load_json(BOOKS_FILE, [])
    book_id = f"b_{int(datetime.now().timestamp()*1000)}"
    now = datetime.now().isoformat()[:19]
    book = {
        "id": book_id,
        "title": req.title,
        "author": req.author,
        "chapters": req.chapters,
        "created_at": now,
        "updated_at": now,
    }
    books.append(book)
    _save_json(BOOKS_FILE, books)
    return {"id": book_id, "title": req.title}

@app.get("/api/books/{book_id}")
async def get_book(book_id: str):
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            return b
    raise HTTPException(status_code=404, detail="书籍不存在")

@app.delete("/api/books/{book_id}")
async def delete_book(book_id: str):
    books = _load_json(BOOKS_FILE, [])
    books = [b for b in books if b["id"] != book_id]
    _save_json(BOOKS_FILE, books)
    return {"ok": True}

@app.post("/api/books/{book_id}/chapters")
async def add_chapter(book_id: str, req: ChapterAdd):
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            ch_id = f"ch_{len(b.get('chapters', [])) + 1}"
            b.setdefault("chapters", []).append({
                "id": ch_id,
                "title": req.title,
                "content": req.content,
            })
            b["updated_at"] = datetime.now().isoformat()[:19]
            _save_json(BOOKS_FILE, books)
            return {"id": ch_id}
    raise HTTPException(status_code=404, detail="书籍不存在")

@app.put("/api/books/{book_id}/chapters/{ch_id}")
async def update_chapter(book_id: str, ch_id: str, req: ChapterAdd):
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            for ch in b.get("chapters", []):
                if ch["id"] == ch_id:
                    ch["title"] = req.title
                    ch["content"] = req.content
                    b["updated_at"] = datetime.now().isoformat()[:19]
                    _save_json(BOOKS_FILE, books)
                    return {"ok": True}
    raise HTTPException(status_code=404, detail="章节不存在")

@app.delete("/api/books/{book_id}/chapters/{ch_id}")
async def delete_chapter(book_id: str, ch_id: str):
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            b["chapters"] = [ch for ch in b.get("chapters", []) if ch["id"] != ch_id]
            b["updated_at"] = datetime.now().isoformat()[:19]
            _save_json(BOOKS_FILE, books)
            return {"ok": True}
    raise HTTPException(status_code=404, detail="书籍不存在")

# ============ 规则模板 API ============

@app.get("/api/rules")
async def list_rules():
    rules = _load_json(RULES_FILE, [])
    return {"rules": rules}

@app.post("/api/rules")
async def save_rules(req: RulesSave):
    rules = _load_json(RULES_FILE, [])
    rule_id = f"r_{int(datetime.now().timestamp()*1000)}"
    now = datetime.now().isoformat()[:19]
    entry = {
        "id": rule_id,
        "name": req.name,
        "rules": [{"original": r.original, "replacement": r.replacement} for r in req.rules],
        "created_at": now,
    }
    rules.append(entry)
    _save_json(RULES_FILE, rules)
    return {"id": rule_id}

@app.delete("/api/rules/{rule_id}")
async def delete_rules(rule_id: str):
    rules = _load_json(RULES_FILE, [])
    rules = [r for r in rules if r["id"] != rule_id]
    _save_json(RULES_FILE, rules)
    return {"ok": True}

# ============ 健康检查 ============

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "3.0"}

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
