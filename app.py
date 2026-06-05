#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说翻改工具 v6.1 — 交互体验升级版
新功能：SSE流式AI翻改、章节拖拽排序、书库搜索API、
      对比模式切换、快捷键增强、PWA支持、Rate Limiting、
      多AI后端、物品名提取、CORS、文件导入、规则清空、
      字体调节、主题切换、本地暂存、差异高亮修复、
      AI改写后再做名称替换、移动端适配、管理员后台、
      31本书每本5章扩充、整本书一键翻改、
      同步滚动、复制按钮、进度条"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import uvicorn
import re
import json
import os
import time
import httpx
from collections import Counter
from datetime import datetime
# chardet removed - not needed

app = FastAPI(title="小说翻改工具 v6.1")

ADMIN_PWD = os.environ.get("ADMIN_PASSWORD", "admin123")

# ============ Rate Limiting ============
_rate_limit_store: Dict[str, list] = {}
RATE_LIMIT_WINDOW = 60  # 1分钟窗口
RATE_LIMIT_MAX = 30     # 每窗口最大请求数


def _check_rate_limit(client_ip: str):
    """滑动窗口速率限制"""
    now = time.time()
    if client_ip not in _rate_limit_store:
        _rate_limit_store[client_ip] = []
    # 清理过期记录
    _rate_limit_store[client_ip] = [
        t for t in _rate_limit_store[client_ip]
        if now - t < RATE_LIMIT_WINDOW
    ]
    if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    _rate_limit_store[client_ip].append(now)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = os.environ.get("DATA_DIR", "data")
BOOKS_FILE = os.path.join(DATA_DIR, "books.json")
RULES_FILE = os.path.join(DATA_DIR, "rules.json")

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
    ai_provider: str = "zhipu"   # zhipu / deepseek / openai


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
    """返回 (改写后文本, 替换详情列表)"""
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
    """统一AI调用入口，支持智谱 / DeepSeek / OpenAI兼容
    stream=True时返回生成器，yield每个chunk的content"""
    if provider == "zhipu":
        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        model = "glm-4-flash"
    elif provider == "deepseek":
        url = "https://api.deepseek.com/chat/completions"
        model = "deepseek-chat"
    else:  # openai 兼容
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
LOC_SUFFIXES = ("城", "山", "谷", "海", "岛", "湖", "河", "江",
                  "峰", "崖", "洞", "窟", "林", "原", "漠", "泽",
                  "渊", "潭", "溪", "泉", "州", "郡", "省", "镇",
                  "村", "关", "渡", "桥", "亭")
ORG_SUFFIXES = ("门", "派", "宗", "阁", "楼", "庄", "堡", "寨",
                  "宫", "殿", "堂", "院", "帮", "盟", "教", "寺", "观")
ITEM_SUFFIXES = ("剑", "刀", "枪", "斧", "锤", "弓", "扇", "珠",
                  "塔", "鼎", "镜", "瓶", "灯", "印", "符", "丹",
                  "药", "草", "诀", "典", "图", "卷", "令", "牌")

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
                    name = text[i:i + length]
                    if all('\u4e00' <= c <= '\u9fff' for c in name):
                        candidates[name] += 1

    bad_endings = set("了着过地得来去出起上下里外中又是的而有所在把被")
    bad_phrases = {
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

    filtered = {}
    for name, count in candidates.items():
        if name in bad_phrases:
            continue
        if len(name) == 2 and name[1] in bad_endings:
            continue
        if any(c in name for c in '，。！？、；：''""（）【】《》'):
            continue
        filtered[name] = count

    to_remove = set()
    for long_name in filtered:
        for short_name in filtered:
            if short_name == long_name or len(short_name) >= len(long_name):
                continue
            if long_name.startswith(short_name) and filtered[short_name] >= filtered[long_name]:
                to_remove.add(long_name)
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
    bad_locs = {
        "大山", "小山", "高山", "深山", "出山", "山河", "江山", "大海", "深海",
        "上海", "北海", "南海", "东海", "西海", "江南", "河南", "河北", "湖南", "湖北",
        "山东", "山西", "广东", "广西", "海南", "云南", "出城", "进城", "攻城", "守城",
        "破城", "入城", "出关", "过关", "关山",
        "下山", "上山", "火山", "冰山", "铁山", "铜山", "银山", "金山",
    }
    locations = sorted(l for l in locs if l not in bad_locs)[:20]

    org_pat = re.compile(
        r'([\u4e00-\u9fff]{1,2}(?:' + '|'.join(ORG_SUFFIXES) + r'))'
    )
    orgs = set(org_pat.findall(text))
    bad_orgs = {
        "出门", "开门", "关门", "敲门", "进门", "热门", "冷门", "正派", "反派",
        "老派", "新派", "气派", "同盟", "结盟", "联盟", "加盟", "大殿", "正殿",
        "偏殿", "殿堂", "天宫", "龙宫", "月宫", "冷宫", "大门", "中门", "后门",
        "前门", "专门", "部门", "佛门",
        "入门", "出门", "关门", "开门", "邪门", "对门", "过门",
    }
    organizations = sorted(
        o for o in orgs if o not in bad_orgs and not o.startswith("的")
    )[:20]

    # 物品提取（新增）
    item_pat = re.compile(
        r'([\u4e00-\u9fff]{1,3}(?:' + '|'.join(ITEM_SUFFIXES) + r'))'
    )
    items = set(item_pat.findall(text))
    bad_items = {
        "大剑", "小刀", "火枪", "铁锤", "木弓", "纸扇", "电灯", "铜镜",
        "打刀", "拔剑", "配剑", "带刀", "拿枪", "举斧", "飞斧",
    }
    items_result = sorted(i for i in items if i not in bad_items)[:15]

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
        # 流程：先AI改写 → 再做名称替换（这样替换规则可以覆盖AI输出）
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
                    "replacement": f"AI改写失败: {e}",
                    "count": 0
                })

        # AI改写后，再应用名称替换规则
        rewritten, rule_reps = apply_rules(rewritten, req.rules)
        rep_details.extend(rule_reps)

        return RewriteResponse(
            original=original,
            rewritten=rewritten,
            replacements=rep_details
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/extract")
async def extract_names(req: ExtractRequest, request: Request):
    _check_rate_limit(request.client.host if request.client else "0.0.0.0")
    try:
        return {"names": extract_names_rule_based(req.text)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ SSE流式翻改 ============

@app.post("/api/rewrite/stream")
async def rewrite_stream(req: RewriteRequest, request: Request):
    """SSE流式AI翻改，实时返回生成内容"""
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
            yield f"data: {json.dumps({'type': 'error', 'msg': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============ 搜索 API ============

@app.get("/api/books/search")
async def search_books(q: str = "", scope: str = "title"):
    """搜索书库，scope: title(书名)/content(内容)"""
    if not q:
        return {"results": []}
    books = _load_json(BOOKS_FILE, [])
    results = []
    for b in books:
        if scope == "title":
            if q.lower() in b["title"].lower() or q.lower() in b.get("author", "").lower():
                results.append({
                    "id": b["id"],
                    "title": b["title"],
                    "author": b.get("author", ""),
                    "chapter_count": len(b.get("chapters", [])),
                })
        elif scope == "content":
            matched_chapters = []
            for ch in b.get("chapters", []):
                if q in ch.get("content", "") or q in ch.get("title", ""):
                    content = ch.get("content", "")
                    idx = content.find(q)
                    snippet = content[max(0, idx - 30):idx + len(q) + 30] if idx >= 0 else ch.get("title", "")
                    matched_chapters.append({
                        "id": ch["id"],
                        "title": ch["title"],
                        "snippet": snippet.replace(q, f"【{q}】"),
                    })
            if matched_chapters:
                results.append({
                    "id": b["id"],
                    "title": b["title"],
                    "author": b.get("author", ""),
                    "matched_chapters": matched_chapters,
                })
    return {"results": results, "total": len(results)}


# ============ 章节排序 API ============

class ChapterReorder(BaseModel):
    chapter_ids: List[str]


@app.put("/api/books/{book_id}/chapters/reorder")
async def reorder_chapters(book_id: str, req: ChapterReorder):
    """拖拽排序章节"""
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            ch_map = {ch["id"]: ch for ch in b.get("chapters", [])}
            new_chapters = []
            for cid in req.chapter_ids:
                if cid in ch_map:
                    new_chapters.append(ch_map[cid])
            for ch in b.get("chapters", []):
                if ch["id"] not in req.chapter_ids:
                    new_chapters.append(ch)
            b["chapters"] = new_chapters
            b["updated_at"] = datetime.now().isoformat()[:19]
            _save_json(BOOKS_FILE, books)
            return {"ok": True, "chapter_count": len(new_chapters)}
    raise HTTPException(status_code=404, detail="书籍不存在")


# ============ 书库导出 API ============

@app.get("/api/books/export")
async def export_books(book_id: str = None, format: str = "txt"):
    """导出书库内容，format: txt/json"""
    books = _load_json(BOOKS_FILE, [])
    if book_id:
        books = [b for b in books if b["id"] == book_id]
    if not books:
        raise HTTPException(status_code=404, detail="未找到书籍")

    if format == "json":
        from fastapi.responses import Response
        content = json.dumps(books, ensure_ascii=False, indent=2)
        return Response(content=content, media_type="application/json",
                       headers={"Content-Disposition": "attachment; filename=books_export.json"})
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
        from fastapi.responses import Response
        return Response(content=content, media_type="text/plain; charset=utf-8",
                       headers={"Content-Disposition": "attachment; filename=books_export.txt"})


# ---- 文件导入 ----
@app.post("/api/import")
async def import_file(data: dict):
    """从上传的文本内容导入（由前端读取文件后发来）"""
    try:
        content = data.get("content", "")
        return {"content": content[:500000]}  # 限制50万字符
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ 书库 API ============

@app.get("/api/books")
async def list_books():
    books = _load_json(BOOKS_FILE, [])
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
    book_id = f"b_{int(datetime.now().timestamp() * 1000)}"
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


class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


@app.put("/api/books/{book_id}/chapters/{ch_id}")
async def update_chapter(book_id: str, ch_id: str, req: ChapterUpdate):
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            for ch in b.get("chapters", []):
                if ch["id"] == ch_id:
                    if req.title is not None:
                        ch["title"] = req.title
                    if req.content is not None:
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
            b["chapters"] = [
                ch for ch in b.get("chapters", [])
                if ch["id"] != ch_id
            ]
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
    rule_id = f"r_{int(datetime.now().timestamp() * 1000)}"
    now = datetime.now().isoformat()[:19]
    entry = {
        "id": rule_id,
        "name": req.name,
        "rules": [{"original": r.original, "replacement": r.replacement}
                   for r in req.rules],
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


# ============ 整本书翻改 API ============

class BookRewriteRequest(BaseModel):
    rules: List[ReplaceRule]
    use_ai: bool = False
    ai_intensity: str = "medium"
    api_key: Optional[str] = None
    ai_provider: str = "zhipu"
    chapter_ids: Optional[List[str]] = None  # 指定章节ID，空=全部


@app.post("/api/books/{book_id}/rewrite")
async def rewrite_book(book_id: str, req: BookRewriteRequest):
    """整本书翻改，返回每个章节的翻改结果"""
    books = _load_json(BOOKS_FILE, [])
    book = None
    for b in books:
        if b["id"] == book_id:
            book = b
            break
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

        # AI改写
        if req.use_ai and req.api_key:
            try:
                rewritten = ai_rewrite(
                    rewritten, req.api_key,
                    req.ai_intensity, req.ai_provider
                )
            except Exception as e:
                rep_details.append({
                    "original": "⚠️",
                    "replacement": f"AI改写失败: {e}",
                    "count": 0
                })

        # 名称替换
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


# ============ 初始数据 ============

SEED_BOOKS = [
    {"title": '斗破苍穹', "author": '天蚕土豆', "chapters": [
        {"title": '第一章 陨落的天才', "content": '乌坦城萧家，少年盘膝而坐，掌心处一缕斗之气若隐若现。三年前他还是家族天才，如今却沦为笑柄。房间的门被推开，一位老者身躯虚幻，悬浮在半空之中，朗声笑道：小家伙，别灰心，有我在，你迟早会站在大陆之巅。药老捋了捋胡须，目光深邃地望着窗外。'},
        {"title": '第二章 休妻', "content": '云岚宗的纳兰嫣然登门退婚，萧炎握紧拳头，指节发白。他看着面前这个曾经视为至宝的女子，淡淡说道：三年之后，我上云岚宗，若你接不下我三招，你纳兰家族从此在我萧家面前抬不起头。纳兰嫣然冷笑一声，转身离去。'},
        {"title": '第三章 异火', "content": '萧炎在魔兽山脉深处，感受着周围炙热的气息。岩浆翻涌之间，一簇青色的火焰若隐若现，正是传说中的青莲地心火。药老沉声道：小心，异火暴烈，稍有不慎便是灰飞烟灭的下场。萧炎深吸一口气，运起焚诀，缓缓靠近那簇火焰。'},
        {"title": '第四章 炼药师', "content": '萧炎第一次尝试炼药，药老站在一旁指导。丹炉中的火焰必须控制在极小的范围内，稍大一分药材便化为灰烬，稍小一分则无法提炼药性。第一次，炉中传来闷响，炸了。药老面无表情：正常，我当年炸了三百炉才成功。萧炎擦了擦脸上的灰：三百炉？药老：别灰心，你天赋比我好，大概只需要两百炉。萧炎：……'},
        {"title": '第五章 迦南学院', "content": '乌坦城外，萧炎背着行囊，回头望了一眼萧家大院。三年之约已定，他必须变强。迦南学院是斗气大陆最负盛名的学府，那里有数不清的修炼资源和强者。薰儿站在城门口等他：萧炎哥哥，我在迦南学院等你。萧炎重重点头：等我。转身大步走向远方，身后传来萧战苍老的声音：孩子，别给萧家丢脸。'},
    ]},
    {"title": '凡人修仙传', "author": '忘语', "chapters": [
        {"title": '第一章 七玄门', "content": '青牛镇往东三十里，有一座无名荒山，山脚下便是七玄门的驻地。韩立站在山门前，手里攥着一封推荐信。门前看守的弟子斜眼看了他一下：新来的？进去吧，找执事弟子登记。韩立默默点头，迈步走了进去。'},
        {"title": '第二章 神秘小瓶', "content": '韩立回到住处，从怀中取出一个翠绿色的小瓶。这瓶子是他偶然得到的，瓶中能催熟灵草。他小心翼翼地收好小瓶：在这修仙世界里，没有背景没有天赋，唯有此物是我最大的倚仗。'},
        {"title": '第三章 黄枫谷', "content": '韩立通过了七玄门的考核，进入黄枫谷修炼。这处修仙门派坐落在一座灵脉之上，仙雾缭绕，灵气充沛。他被分配到外门，每天的任务就是打理灵药园。韩立不急不躁，默默用小瓶催熟灵草，再以丹药辅助修炼，修为稳步提升。马良从旁经过：韩师弟，你修为进境倒是稳当。韩立：稳比快好。'},
        {"title": '第四章 筑基', "content": '韩立终于突破炼气期，踏入筑基。这一步卡住了无数修士，他却凭借小瓶催熟的千年灵草配制的筑基丹，一次成功。筑基之后，他感受到天地灵气如潮水般涌入经脉，五感变得无比敏锐。厉飞雨在远处冷眼看着：一个没有背景的小子，居然这么快就筑基了？韩立不在意他人的目光，他深知修仙路上，每一步都要如履薄冰。'},
        {"title": '第五章 乱星海', "content": '一场大战之后，韩立被迫离开故土，横渡无边海，来到了乱星海。这里是散修的天堂，也是修士的炼狱。没有门派庇护，一切只能靠自己。韩立在星岛上摆了一个简陋的摊位，售卖低阶丹药换取灵石。一位老散修凑过来：道友，你这丹药品质不错，可有兴趣去星宫碰碰运气？韩立微微一笑：再说吧。'},
    ]},
    {"title": '诡秘之主', "author": '爱潜水的乌贼', "chapters": [
        {"title": '第一章 廷根', "content": '周明瑞从模糊的梦境中醒来，发现自己躺在一张硬木板床上。房间狭小，只有一扇窗户透进灰蒙蒙的光线。他摸了摸自己的脸——这不是他原来的脸。桌上放着一面小镜子，镜中人是标准的鲁恩人模样。他低声自语：我穿越了？门外传来脚步声，一个穿黑色风衣的男子推门而入。'},
        {"title": '第二章 占卜家', "content": '克莱恩翻阅着邓恩留下的笔记，上面记载着各种非凡途径的信息。占卜家途径、偷盗者途径——每一条途径都通向不同的命运。他合上笔记本：序列9占卜家，这是我踏入非凡世界的第一步。黑夜女神庇佑。'},
        {"title": '第三章 塔罗会', "content": '克莱恩在灰雾之上发现了一个神秘的空间，他可以在这里将不同的人拉入梦境，召开塔罗会。他戴上黑色面具，以愚者之名降临：诸位，欢迎来到塔罗会。正义小姐奥黛丽好奇地打量着四周：这里好神奇。倒吊人阿尔杰冷冷道：你到底是谁？克莱恩沉默片刻：我只是一个途径的探求者。'},
        {"title": '第四章 贝克兰德', "content": '克莱恩来到贝克兰德，这座鲁恩王国的首都繁华而危险。他在一家侦探社找了份工作，白天查案，夜晚则在灰雾之上研究非凡特性。公寓的房东太太热情得过分：克莱恩先生，今天的茶点准备好了。克莱恩礼貌微笑，心中暗自警惕——在这个世界，任何异常都可能致命。'},
        {"title": '第五章 秘偶大师', "content": '序列7，秘偶大师。克莱恩终于触摸到了占卜家途径更深层的秘密。他可以操控灵体之线，将活人变成自己的秘偶。这份力量让他兴奋，也让他恐惧。他在镜子前看着自己的眼睛——那里多了一丝不属于人类的冷漠。伦纳德在旁边弹着吉他：克莱恩，你最近越来越像个怪物了。克莱恩：谢谢夸奖。'},
    ]},
    {"title": '全职高手', "author": '蝴蝶蓝', "chapters": [
        {"title": '第一章 回到起点', "content": '叶修坐在电脑前，屏幕上的荣耀图标闪闪发光。十年职业生涯，他如今却被俱乐部驱逐。他笑了笑，拿起鼠标：没关系，从哪里开始，就从哪里重新开始。嘉世俱乐部的大门在他身后缓缓关上。'},
        {"title": '第二章 散人君莫笑', "content": '荣耀第十区开服，一个名叫君莫笑的散人横空出世。千机伞在手中翻转，各种低级技能被他组合出匪夷所思的连招。唐柔站在他身后问道：这个角色没有转职？叶修推了推眼镜：散人玩法，二十四个职业的技能都可以用。'},
        {"title": '第三章 兴欣网吧', "content": '叶修在兴欣网吧安顿下来，老板陈果是荣耀的忠实玩家，却不知道眼前这个烟不离手的男人就是传说中的叶神。叶修一边抽烟一边打荣耀，手速快得键盘都在冒烟。陈果瞪大了眼睛：你……你多少手速？叶修随口答道：也不快，随便打打。陈果看了眼手速统计：490APM……随便打打？！'},
        {"title": '第四章 组建战队', "content": '叶修开始招兵买马。唐柔是第一个加入的，她的操作天赋极高，只是经验不足。包子入侵是个街头混混，操作全凭直觉，却常常有出人意料的发挥。伍晨是个老实的前职业选手，因为假赛风波被驱逐。叶修看着这支东拼西凑的队伍：够了，兴欣战队，从今天开始。'},
        {"title": '第五章 挑战赛', "content": '荣耀挑战赛开打，兴欣战队从网咖赛一路杀上来。叶修坐在电脑前，千机伞在他手中如同有了生命。对面的职业选手根本无法理解——一个散人怎么可能这么强？叶修笑了笑，十年荣耀，一朝归来。他打出了一串完美的连招，对手直接被秒杀。解说席炸了：这是什么操作？！叶神回来了！'},
    ]},
    {"title": '斗罗大陆', "author": '唐家三少', "chapters": [
        {"title": '第一章 斗罗大陆', "content": '唐三感觉自己仿佛做了一个很长的梦。等他再次睁开眼睛的时候，发现自己身处一间简陋的木屋之中。他低头看了看自己的小手——这不是他的手。脑海中两股记忆涌来。他暗自运了运内力，发现玄天功居然还能运转。'},
        {"title": '第二章 武魂觉醒', "content": '武魂殿的长老来到圣魂村，为六岁的孩子们觉醒武魂。轮到唐三时，一道蓝光从掌心升起——蓝银草。长老摇了摇头，正要记下废武魂，唐三的右手却又浮现出一道金光：昊天锤。唐昊在一旁沉默不语。'},
        {"title": '第三章 史莱克学院', "content": '唐三进入了史莱克学院，这是一所只收怪物的学校。入学条件只有一个：十二岁以下，魂力达到二十一级以上。戴沐白、马红俊、宁荣荣、朱竹清、小奥——每个人都身怀绝技。弗兰德院长笑眯眯地看着他们：欢迎来到史莱克，从今天起你们就是怪物。唐三站在人群中，暗暗运转玄天功。'},
        {"title": '第四章 七怪聚首', "content": '史莱克七怪正式成团。戴沐白是大哥，唐三排名第三。赵无极副院长给他们来了个下马威——一场实战测试。唐三冷静地分析每个人的武魂特点，指挥小奥释放增幅香肠，宁荣荣开启七宝琉璃塔辅助，马红俊凤凰火焰开路。戴沐白瞪大了眼睛：老三，你这脑子是什么做的？唐三：经验而已。'},
        {"title": '第五章 星斗大森林', "content": '为了获得第三魂环，唐三踏入星斗大森林。这里栖息着无数魂兽，越深入越危险。一只千年的鬼藤拦住了去路，蓝银草在它面前不堪一击。唐三深吸一口气，暗器手法曼陀罗针脱手而出，鬼藤应声而断。小舞在树梢上好奇地看着他：三哥，你好厉害。唐三笑了笑：走，继续深入。'},
    ]},
    {"title": '诛仙', "author": '萧鼎', "chapters": [
        {"title": '第一章 青云', "content": '草庙村，一个偏僻的小村庄，坐落在群山环抱之中。少年张小凡父母早亡。这一天，村子里来了一个道人，自称是青云门外门弟子。张小凡的命运从此改变。天空中一道青光划过，青云门的弟子御剑飞行，洒下漫天星光。'},
        {"title": '第二章 拜师', "content": '张小凡被田不易收为大竹峰弟子。他资质平平，修炼进展缓慢，却从不抱怨。每天清晨他都会去后山喂那条叫大黄的狗。田灵儿偶尔路过，笑他笨，他也只是憨厚地笑笑。'},
        {"title": '第三章 噬魂棒', "content": '张小凡在后山发现了一根漆黑的短棒，入手冰凉，隐隐透着嗜血的气息。这是噬血珠和摄魂棒合二为一的法宝——噬魂。他不知道这根棒子的来历，只觉得它与自己莫名契合。林惊羽路过看到他手中的黑棒，面色微变：小凡，这东西……张小凡连忙藏到身后：没什么，就是根棍子。'},
        {"title": '第四章 七脉会武', "content": '青云门七脉会武开始，各峰弟子齐聚通天峰。张小凡的修为在所有参赛者中垫底，却凭着一股不服输的劲头闯进了前四。陆雪琪站在擂台对面，天琊剑寒光凛凛。张小凡握紧噬魂棒，心中一片茫然——他不想赢，也不想输，只是不知道该站在哪里。'},
        {"title": '第五章 满月井', "content": '死灵渊底，张小凡和陆雪琪坠入满月井中。井水倒映出他心中最深的执念——那个草庙村的小男孩，那个再也回不去的从前。陆雪琪问他：你看到了什么？张小凡沉默许久：我看到了碧瑶。她撑着合欢铃，站在满月之下，对他微微笑。张小凡握紧了拳头：我一定要救她。'},
    ]},
    {"title": '遮天', "author": '辰东', "chapters": [
        {"title": '第一章 星空古路', "content": '九具仙尸拉着一口青铜古棺，在无尽的星空中缓缓前行。叶凡只是泰山旅游的一个普通大学生，却被卷入这场浩大的星域穿越之中。他看着脚下的火星大地，心中只有一个念头：我要回家。庞博拍了拍他的肩膀：先活下去再说。'},
        {"title": '第二章 荒古禁地', "content": '荒古禁地，传说中的生命禁区。叶凡只觉得体内有什么东西在觉醒，那枚源天书上的文字仿佛活了过来。老疯子在远处疯狂大笑：好一个荒古圣体，万年后终于又出现了！'},
        {"title": '第三章 灵墟洞天', "content": '叶凡被灵墟洞天的长老收为弟子，但荒古圣体无法修行的消息很快传开。所有人都认为他是废体，连最基本的源气都无法凝聚。叶凡不气不恼，每天在洞天中苦练肉身。庞博偷偷给他送饭：叶子，别逞强了。叶凡擦了擦汗：我不是逞强，是在等。等什么？等圣体觉醒的那一天。'},
        {"title": '第四章 姬紫月', "content": '姬紫月是东荒姬家的小公主，精灵古怪，天不怕地不怕。她第一次见到叶凡就被他的倔强打动了：你这个废体居然还敢在洞天里修炼？叶凡：废不废，不是你说了算。姬紫月嘻嘻一笑：有意思，本小姐罩你了。叶凡：……不用了。姬紫月已经拉着他的袖子往藏经阁跑了。'},
        {"title": '第五章 四极秘境', "content": '叶凡以荒古圣体冲破枷锁，踏入四极秘境。这是修行路上的第一道天堑，无数天才止步于此。天劫降临，雷海翻涌，叶凡在雷电中重塑肉身。黑皇在远处兴奋得直搓爪子：好家伙，圣体渡劫，这可是万古未有的大场面！叶凡浑身浴血，仰天长啸：我命由我不由天！'},
    ]},
    {"title": '大奉打更人', "author": '卖报小郎君', "chapters": [
        {"title": '第一章 大奉王朝', "content": '许七安从醉梦中醒来，发现自己成了一名打更人。大奉王朝国运昌盛，京城里暗流涌动。打更人不仅仅是巡夜的差事，更是一个庞大的情报机构。他整了整衣冠：既然来了，就得好好活下去。'},
        {"title": '第二章 儒家修行', "content": '许七安盘膝而坐，面前摆着一本《大学》。儒家的修行方式极为特殊，只需读书明理便可提升境界。他念诵经典，体内浩然正气缓缓流转。窗外传来打更的梆子声：这日子倒是比前世有趣多了。'},
        {"title": '第三章 案牍库', "content": '许七安被派往案牍库整理旧档，看似是个闲差，实则暗藏玄机。在堆积如山的卷宗里，他发现了一桩五年前的悬案——桑泊案。线索指向朝廷深处，牵涉之人位高权重。许七安合上卷宗，嘴角微扬：前世当警察的经验，终于派上用场了。'},
        {"title": '第四章 佛门斗法', "content": '京城外的青楼里，许七安遇到了佛门弟子。佛教修行讲究明心见性，却也与儒家针锋相对。一场斗法在所难免，许七安运起浩然正气，口诵经典，金光护体。佛门和尚念了句阿弥陀佛：施主好生厉害。许七安：过奖，我只是书读得多。'},
        {"title": '第五章 元神出窍', "content": '许七安的修为突破到了元神境，可以做到元神出窍。他发现自己的元神状态竟然能看到一些肉眼看不到的东西——比如别人身上的因果线。这些线条交织成网，将朝堂上下千丝万缕的关系展露无遗。他默默记下一切：这朝堂水太深，没有这两把刷子，怕是活不过三集。'},
    ]},
    {"title": '雪中悍刀行', "author": '烽火戏诸侯', "chapters": [
        {"title": '第一章 北凉世子', "content": '北凉王府，徐凤年裹着狐裘，站在城头远眺。他是天下第一纨绔，北凉王的独子。三年游历归来，他不再是那个只会斗鸡走狗的世子。徐骁站在他身后：凤年，北凉的担子，迟早要你来扛。徐凤年没有回头，只是将手中的刀握得更紧了些。'},
        {"title": '第二章 老黄', "content": '老黄是王府的老仆，走路一瘸一拐。可徐凤年知道，这个老人曾经是江湖上赫赫有名的剑九黄。老黄端来一碗热粥：世子殿下，该用膳了。徐凤年接过碗：老黄，你当年为何弃剑？老黄沉默良久：因为遇见了一个扛刀的人。'},
        {"title": '第三章 武当山', "content": '徐凤年上武当山学剑，却遇到了一个更怪的老道士——洪洗象。这位武当掌门整日骑牛读书，从不修炼，却号称天下第一。徐凤年忍不住问：您真不修炼？洪洗象翻了一页书：为何要修炼？天道自然，到了便是到了。徐凤年：……那你什么时候到？洪洗象抬头看了看天：快了。'},
        {"title": '第四章 江湖行', "content": '徐凤年再次游历江湖，这一次不再是纨绔世子的嬉闹，而是北凉世子的使命。他走过春秋两国故地，见过亡国遗民的眼泪，也见过豪杰的热血。温华在路边等他：兄弟，听说你又要出门？徐凤年拍了拍他的肩：这次不一样，我要去见一个人。温华：谁？徐凤年：天下第二。'},
        {"title": '第五章 王仙芝', "content": '武帝城，王仙芝独坐城头六十年，天下第二从未被超越。徐凤年站在城下，抬头望去，只觉得那道身影仿佛与天地同高。王仙芝睁开眼：北凉世子？你来做什么？徐凤年拔刀：来领教天下第二的功夫。王仙芝笑了：好胆。一拳轰出，气浪如山。'},
    ]},
    {"title": '庆余年', "author": '猫腻', "chapters": [
        {"title": '第一章 澹州', "content": '范闲在澹州长大，名义上是范建的私生子。他自幼习武，又跟着费介学了用毒之术。五竹叔总是蒙着黑布站在角落里，从不说话。他望着京都的方向，心中暗想：娘亲留下的那个箱子，里面到底装着什么？'},
        {"title": '第二章 进京', "content": '范闲终于踏入了京都城门。这座天下的中心，远比他想象的更加波谲云诡。长公主的笑意、太子的试探——每个人都在他身上打着主意。他微微一笑：我既然来了，就不打算只做一枚棋子。'},
        {"title": '第三章 监察院', "content": '监察院是庆国最神秘的地方，陈萍萍坐在轮椅上，掌控着天下最庞大的情报网。范闲被带到这里时，看到满墙的档案和无数伏案工作的官员。陈萍萍推了推眼镜：你就是范闲？像你母亲。范闲心中一震：你认识我母亲？陈萍萍只是笑了笑，没有回答。'},
        {"title": '第四章 诗仙', "content": '范闲在京都诗会上一鸣惊人，醉酒写下千古绝唱。满座皆惊，无人相信这是一个少年能写出的诗句。靖王世子李弘成举杯：范兄大才。范闲暗自心虚——这些诗都是前世的存货。庆帝在宫中听到消息，微微点头：此子……有趣。'},
        {"title": '第五章 五竹', "content": '五竹是范闲最神秘的保护者，双眼蒙着黑布，从不说话，却能以一己之力对抗大宗师。范闲问他：叔，你到底是谁？五竹没有回答，只是默默地擦拭着手中的铁钎。范闲叹了口气：算了，反正有你在就行。五竹终于开口：嗯。这一个字，让范闲莫名安心。'},
    ]},
    {"title": '仙逆', "author": '耳根', "chapters": [
        {"title": '第一章 赵国', "content": '赵国边陲小镇，王林是个普通农家少年。他天资愚钝，却有一个不为人知的秘密——他的脑海中有一个神秘的空间。恒岳派来镇上选弟子，他凭借最后一丝运气被选中。踏入修仙界的第一天，他就明白了：仙路漫漫，唯有逆天而行，方有一线生机。'},
        {"title": '第二章 天逆珠', "content": '王林脑海中的神秘空间，其实是一颗天逆珠。这颗珠子能吸纳天地灵气，是修仙界至宝。王林在恒岳派的修炼突飞猛进，引起了不少人的注意。藤化元冷冷地看着他：一个资质低劣的废物，凭什么修为进境如此之快？王林不加理会，默默修炼。他知道，在这个弱肉强食的世界，只有实力才是硬道理。'},
        {"title": '第三章 杀戮', "content": '王林在修仙路上越走越远，双手也染上了越来越多的鲜血。他杀了藤家满门，只为报当年之仇。鲜血溅在他脸上，他没有擦。周遭的同门弟子看着他的背影，充满恐惧。王林转身离开，声音冰冷：修仙本就是逆天而行，你死我活，没什么好说的。'},
        {"title": '第四章 化神', "content": '王林突破化神，这是修仙路上的又一道天堑。化神之后，他可以掌控天地之力，举手投足间翻山倒海。他在洞府中闭关三百年，出关时白发如雪，目光却比从前更加锐利。清水站在洞口等他：恭喜前辈化神成功。王林：化神而已，我要走的路还很长。'},
        {"title": '第五章 仙逆', "content": '仙，何为仙？逆仙，何为逆？王林站在星空之下，回顾自己走过的路。从赵国边陲的农家少年，到如今令诸天颤抖的强者，他一步一步走来的每一步都踏着血与泪。他伸手触碰天际的星光：所谓仙逆，不过是逆天命而行，走一条自己的路。这条路，他王林走定了。'},
    ]},
    {"title": '一念永恒', "author": '耳根', "chapters": [
        {"title": '第一章 灵溪宗', "content": '白小纯是个怕死的少年，他来灵溪宗的唯一目的就是长生不老。他蹲在灶房里研究灵食，将一株灵草炖成了汤，结果整个灶房炸了。侯小妹从烟尘中走出来，黑着脸：白小纯！你又在搞什么？白小纯嘿嘿一笑：我在研究新的长寿秘方。'},
        {"title": '第二章 灵溪城', "content": '白小纯下山来到灵溪城，这座修仙者的城市比他想象的繁华百倍。灵药铺、法器店、丹药馆鳞次栉比。他摸了摸干瘪的储物袋，叹了口气：穷。一个老者叫住他：小友，可有兴趣帮我炼一炉丹？白小纯眼睛一亮：给钱吗？老者：给灵石。白小纯：成交！'},
        {"title": '第三章 筑基', "content": '白小纯终于突破炼气期筑基成功，但过程惨烈——他在筑基时太紧张，把丹药当零食吃多了，结果修为暴涨到筑基后期。杜凌菲在旁边看着他的修为波动，目瞪口呆：你怎么做到的？白小纯拍拍肚子：天赋。杜凌菲：……你管这叫天赋？白小纯：怕死就是最好的天赋。'},
        {"title": '第四章 血溪宗', "content": '血溪宗是灵溪宗的宿敌，两宗争斗已久。白小纯被迫卷入宗门大战，他只想躲起来等仗打完，却被分配到了最前线。他看着对面杀气腾腾的血溪宗弟子，双腿发软：能不能商量一下，我出灵石买和平？对面的弟子举刀就砍：做梦！白小纯尖叫着跑开，顺手扔了个雷符。轰——对面倒了一片。'},
        {"title": '第五章 长生', "content": '白小纯站在山巅，望着远处的云海。他终于明白了一个道理——长生不是目的，活着才有意义。身边的朋友、对手、师长，每一个人的存在都让他这条长生路不那么孤单。宋君婉从背后走来：发什么呆呢？白小纯回头一笑：在想，活着真好。宋君婉：……你今天怎么突然正常了？白小纯：大概是被炸傻了。'},
    ]},
    {"title": '盗墓笔记', "author": '南派三叔', "chapters": [
        {"title": '第一章 血尸', "content": '吴邪坐在铺子里百无聊赖，一个陌生人递来一份战国帛书的拓片。帛书上画着一张古怪的地图，标注着一座位于长沙的战国古墓。三叔看了拓片后脸色大变：这座墓，我找了几十年。吴邪跟着三叔下了斗，墓道深处传来低沉的喘息声，血尸的指甲在石壁上划出刺耳的声响。'},
        {"title": '第二章 七星鲁王宫', "content": '七星鲁王宫是周朝的诸侯墓，机关重重，步步杀机。胖子一脚踩空差点掉进暗河，张起灵一把将他拉了回来。闷油瓶一如既往面无表情，黑金古刀在黑暗中闪着寒光。吴邪暗暗心惊：这墓里不只有我们，还有人在前面走了很远。'},
        {"title": '第三章 战国帛书', "content": '吴邪翻阅着三叔留下的战国帛书，上面的文字晦涩难懂，却隐约指向一座战国古墓。帛书上的地图标注着一处深山中的位置，旁边画着诡异的图腾。吴邪决定去一探究竟。胖子在旁边嗑着瓜子：小三爷，这种好事怎么能少了我？吴邪：你去可以，别乱碰东西。胖子：放心，我有分寸。'},
        {"title": '第四章 秦岭神树', "content": '秦岭深处，一棵巨大的青铜神树矗立在地下溶洞中。树干粗如十人合抱，枝叶全是青铜铸造，上面挂满了奇异的铜铃。吴邪站在树下，只觉得头皮发麻——这棵树至少有几千年的历史。闷油瓶突然开口：别碰。吴邪：我还没打算碰。闷油瓶：你的手在发抖。'},
        {"title": '第五章 云顶天宫', "content": '长白山云顶天宫，万奴王的最终归处。吴邪站在冰川之上，望着这座埋藏千年的宫殿。入口处的冰壁上刻着一行文字，张起灵辨认许久：入者永生，出者无归。吴邪咽了口唾沫：这意思是进去就出不来了？张起灵已经走进了冰壁后的黑暗中。吴邪咬牙跟上。'},
    ]},
    {"title": '神墓', "author": '辰东', "chapters": [
        {"title": '第一章 神墓', "content": '万年前的大神独孤败天陨落了，他的墓地却成了后世修者心中的圣地。辰南从混沌中醒来，发现自己躺在一座巨大的古墓之中。周围是无数神魔的尸体，不朽的气息弥漫在空中。他摸了摸自己的身体——他还活着。一个苍老的声音在墓中回响：你终于醒了。'},
        {"title": '第二章 澹台圣地', "content": '辰南走出神墓，发现外面的世界已经沧海桑田。万年光阴流转，曾经的大陆早已面目全非。澹台圣地的圣女发现了这个从古墓中走出的青年，惊为天人。辰南望着天空：这个世界，比万年前更加疯狂了。魔主在暗处冷冷注视着他。'},
        {"title": '第三章 万年前', "content": '辰南从神墓的残留记忆中窥见了万年前的真相。那时候他是独孤败天的追随者，曾在太古战场上浴血厮杀。万年前的那场大战，诸天陨落，神魔喋血，连天道都被撕裂了。他只记得自己最后倒下时的画面——一个白衣女子挡在他面前，泪流满面。辰南：她是谁？记忆在那一刻断裂了。'},
        {"title": '第四章 雨馨', "content": '雨馨是辰南在万年前最放不下的人。他找到了她的转世，却发现她已不再记得前世的一切。辰南远远地看着她在花丛中嬉戏，没有上前打扰。梦可儿在旁边问：你认识她？辰南摇了摇头：不认识，只是觉得她的笑容很像一个人。'},
        {"title": '第五章 太古', "content": '辰南的修为恢复到了太古境，这个层次已经是凡人能触及的极限。他站在天穹之上俯瞰大地，众生如蝼蚁。但他并不觉得渺小——因为天之上还有天，道之上还有道。太古禁忌的身影在虚空中若隐若现：你终于走到了这一步。辰南握紧双拳：这才刚开始。'},
    ]},
    {"title": '吞噬星空', "author": '我吃西红柿', "chapters": [
        {"title": '第一章 罗峰', "content": '基地市的天空灰蒙蒙的，怪兽横行的荒野区才是武者们的战场。罗峰站在武馆门口，手里攥着刚刚通过准武者考核的成绩单。他从普通学生到准武者，用了三年。弟弟罗华坐在轮椅上，笑着朝他竖起大拇指。罗峰目光坚定：我一定会成为真正的武者，让家人过上好日子。'},
        {"title": '第二章 荒野区', "content": '罗峰第一次踏入荒野区，空气中弥漫着血腥和硝烟的味道。铁甲龙在远处咆哮，独角兽成群结队地奔跑。他握紧手中的战刀，缓缓呼出一口气。洪在屏幕那头说：记住，荒野区没有规则，活下来就是最大的胜利。雷神在一旁补充：还有，别太贪。'},
        {"title": '第三章 极限武馆', "content": '罗峰加入了极限武馆，这是全球最顶尖的武者训练基地。在这里，他第一次接触到了超越常人极限的力量体系。教官是一个身高两米的壮汉：在这里，没有天才和废物的区别，只有活着和死去的区别。罗峰握紧拳头，暗暗发誓要成为最强的那一个。'},
        {"title": '第四章 宇宙级', "content": '罗峰突破到宇宙级，这意味着他已经可以脱离地球，在太空中生存。他的身体强度足以抵御真空，一拳可以击碎小行星。巴巴塔在他脑海中兴奋得叫唤：主人，你终于够格了！宇宙这么大多好玩！罗峰：别急，先把地球的事情处理完。'},
        {"title": '第五章 虚空之塔', "content": '罗峰在宇宙中闯荡，终于来到了传说中的虚空之塔。这座塔是宇宙最古老的遗迹之一，每通过一层就能获得一份传承。罗峰站在塔前，仰头望去，塔尖消失在虚空中。巴巴塔：据传至今没人通关。罗峰微微一笑：那就让我来试试。他推开了第一层的大门。'},
    ]},
    {"title": '完美世界', "author": '辰东', "chapters": [
        {"title": '第一章 石村', "content": '大荒深处，一个叫石村的小村庄坐落在苍茫群山之间。村口有一块巨大的石头，据说是一尊远古神灵的遗骸。石昊是村里的孩子王，天生神力，却总被村长嫌调皮。他趴在巨石上晒太阳，忽然感觉石头里有什么东西在跳动。柳神在村口的老柳树下安静地看着这一切。'},
        {"title": '第二章 补天阁', "content": '补天阁是大荒中的顶级势力，专门收有天资的孩子修行。石昊凭借一己之力打上了山门，惊动了阁中长老。长老们面面相觑：这孩子的血脉……不像是人族的。石昊擦了擦鼻血：管他什么血脉，先让我进去吃饭再说。'},
        {"title": '第三章 火灵儿', "content": '石昊在补天阁遇到了火灵儿——一个脾气火爆的红衣少女。火灵儿第一次见到他就炸了：你就是那个石昊？听说你很嚣张？石昊摸了摸鼻子：还行吧。火灵儿：哼，本公主倒要看看你有什么本事。两人不打不相识，反而成了最好的朋友。'},
        {"title": '第四章 荒', "content": '石昊的称号——荒，传遍了整个荒域。他以一人之力横扫同辈，无人可挡。但荒域之外还有更广阔的天地，三千道州、九天十地，强者如云。石昊站在荒域边界的虚空中，目光坚定：荒域只是起点，我要去看看外面的世界。'},
        {"title": '第五章 仙域', "content": '石昊终于踏入了仙域，这是凡人梦寐以求的至高境界。仙域之中法则完善，灵气充沛，一草一木都蕴含着大道至理。石昊在仙域中开辟了自己的道场，他盘膝而坐，开始参悟更高层次的大道。清风拂面，他忽然想起了石村的那株柳树。'},
    ]},
    {"title": '牧神记', "author": '宅猪', "chapters": [
        {"title": '第一章 残老村', "content": '大墟的黄昏总是来得特别早。残老村中，一群残缺不全的老人围坐在篝火旁，给秦牧讲述着外面的世界。秦牧从小被他们养大，学会了药师的毒、屠夫的刀、瞎子的枪、聋子的画。他站在村口，望着外面的天地：我要出去看看。村长瘸着腿追出来：外面很危险！秦牧回头一笑：有你们教的本事，我怕什么？'},
        {"title": '第二章 延康', "content": '延康国是大墟之外最大的国度，国师提倡变法，废除旧神信仰，以人定胜天为纲。秦牧第一次踏入延康国境，就被这里的繁华震撼了。他挤在人群中看着国师变法的布告，心中暗想：这个世界比残老村复杂太多了。延丰帝在宫中叹道：变法之路，何其艰难。'},
        {"title": '第三章 延康变法', "content": '延康国的变法如火如荼，国师以雷厉风行的手段推行新政。秦牧在延康城中看到了翻天覆地的变化——旧的庙宇被拆除，新的学堂建立起来。但他也看到了变法的代价，那些失去信仰的百姓茫然无措。秦牧：变法是好事，但人心怎么办？国师沉默了。'},
        {"title": '第四章 天魔教', "content": '秦牧遇到了天魔教的圣女司婆婆，两人不打不相识。司婆婆教他天魔教的功法，秦牧却发现这些功法中藏着上古天庭的秘密。司婆婆：你以为天魔教是邪教？秦牧：难道不是？司婆婆冷笑：天魔教是最早反抗天庭的组织，只是历史被改写了。'},
        {"title": '第五章 十八重天', "content": '秦牧的修为突飞猛进，终于触碰到了十八重天的门槛。这是人族修士的极限，再往上便是神魔的领域。秦牧站在第十八重天的边界，回头望了一眼来时的路——残老村的灯火、延康的繁华、天魔教的风雨，一幕幕在眼前闪过。他深吸一口气，跨过了那道边界。'},
    ]},
    {"title": '大王饶命', "author": '会说话的肘子', "chapters": [
        {"title": '第一章 负面情绪', "content": '吕树从小在孤儿院长大，靠卖红薯为生。有一天他发现自己能吸收别人的负面情绪值来变强。别人越骂他，他越开心。吕小鱼在旁边啃着红薯：哥，你是不是有病？吕树嘿嘿一笑：你不懂，他们骂我一次，我就变强一分。这生意，稳赚不赔。'},
        {"title": '第二章 觉醒', "content": '吕树参加了天罗地网的觉醒测试，结果觉醒的是最没用的负情绪收集天赋。考官们面面相觑：这天赋……闻所未闻。吕树毫不在意，默默记下了每个考官的表情——愤怒值+2，嫌弃值+3，不屑值+5。他心中暗爽：谢谢各位老板。'},
        {"title": '第三章 负面情绪', "content": '吕树发现自己的异能有点特殊——他能收集别人的负面情绪值来修炼。别人越生气，他越强。于是他开始了一种全新的修炼方式：气人。同学：吕树你是不是有病？吕树：你生气了？同学：废话！吕树默默看了一眼负面情绪值+50，满意地点了点头。'},
        {"title": '第四章 遗迹', "content": '灵气复苏后，各地出现了远古遗迹。吕树被分配去探索一处小型遗迹，里面全是机关陷阱。他小心翼翼地走了三步，踩到了一块松动的地砖。轰——石箭从四面八方射来。吕树抱着头蹲在地上：我就知道没好事！刘盛隆在远处叹气：你能不能有点出息？'},
        {"title": '第五章 天罗', "content": '天罗是灵气复苏后最神秘的组织，网罗天下天才。吕树被天罗盯上了，对方派人来招揽。吕树直接拒绝：不加入。天罗使者：为什么？吕树：因为你们名字太难听了。天罗使者：……吕树认真地说：天罗地网，一听就是要送人去当炮灰的。'},
    ]},
    {"title": '赘婿', "author": '愤怒的香蕉', "chapters": [
        {"title": '第一章 江宁', "content": '江宁城苏家，宁毅作为赘婿嫁了进来，人人看不起。他坐在后院里翻着一本账册，嘴角挂着若有若无的笑。苏檀儿从窗前走过，冷冷看了他一眼：你若是个男人，就该出去闯荡，而不是待在家里吃软饭。宁毅合上账册：吃软饭也要讲究方法，你看这账，你们苏家至少亏了三成。'},
        {"title": '第二章 布局', "content": '宁毅不动声色地布局，从染坊的供应链到城中的商路，每一步都算无遗策。秦嗣源在朝堂上收到密报：江宁有个赘婿，手段不凡。宁毅站在苏家染坊的屋顶上，望着远方的烽烟：天下将乱，我不过提前准备罢了。陆红提在暗处默默守护。'},
        {"title": '第三章 布行', "content": '宁毅在苏家布行中发现了商机。他利用前世的商业知识，改良了染布工艺，让苏家的布匹品质大幅提升。苏檀儿起初不屑一顾，直到布行的订单翻了两倍。她看着账本，沉默良久：你……以前真的只是个赘婿？宁毅推了推眼镜：以前是，现在也是。'},
        {"title": '第四章 江宁', "content": '江宁城风云变幻，各方势力暗流涌动。宁毅在商战中步步为营，却也被卷入了更大的漩涡。秦嗣源在朝堂上被人弹劾，宁毅连夜赶写了一份奏章。秦嗣源看完后沉默：你一个赘婿，竟有如此见识。宁毅：见识不分出身。'},
        {"title": '第五章 乱世', "content": '乱世来了，金兵南下，天下大乱。宁毅站在城墙上看着远方升起的浓烟，终于明白了一件事——在这个时代，光有商业头脑是不够的。他脱下商人的锦袍，换上了战甲：既然乱世不能避，那就亲手终结它。苏檀儿站在身后，眼中含泪却坚定：我等你回来。'},
    ]},
    {"title": '灵剑山', "author": '国王陛下', "chapters": [
        {"title": '第一章 灵剑派', "content": '王陆参加灵剑派的入门考核，却发现这个修仙门派的画风完全不对。师父王舞是个酒鬼，整天泡在酒坛子里，考核全靠蒙。王陆站在山门前，看着摇摇欲坠的牌匾，心中暗想：这真的是传说中的五大宗门之一？王舞打了个酒嗝：小子，别嫌，进来就是了。'},
        {"title": '第二章 落云峰', "content": '落云峰是灵剑派最穷的山头，王舞是整个门派最不靠谱的长老。王陆被分配到这里，发现连修炼功法都要自己去藏经阁偷。他叹了口气：别人修仙靠天赋，我修仙靠脸皮厚。王舞翘着二郎腿：年轻人，修行之道在于悟，你悟了吗？王陆：悟了，师父你最懒。'},
        {"title": '第三章 入门考核', "content": '王陆参加灵剑派的入门考核，考核内容千奇百怪。第一关是过独木桥，桥下是万丈深渊。其他弟子战战兢兢，王陆大摇大摆走了过去——他在桥下偷偷用了轻功。考官看在眼里，嘴角抽搐：这小子……有点意思。'},
        {"title": '第四章 王舞', "content": '王陆的师父是灵剑派五长老王舞，一个整天喝酒打牌的废柴修士。王陆第一次见到她时，她正躺在摇椅上晒太阳。王陆：师父，您能教我什么？王舞翻了个身：教你如何快乐地活着。王陆：……这算什么修行？王舞：修行不就是为了快乐吗？'},
        {"title": '第五章 仙剑', "content": '王陆终于得到了自己的仙剑，但剑灵是个话痨，从他拔剑的那一刻起就没停过。王陆：你能不能安静一会儿？剑灵：不能！我等了三百年终于有人拔我了！王陆叹气：早知道选那把哑巴剑了。剑灵：你说什么？！王陆：没什么，很高兴认识你。'},
    ]},
    {"title": '修真聊天群', "author": '圣骑士的传说', "chapters": [
        {"title": '第一章 聊天群', "content": '宋书航不小心加入了一个名叫「九州一号群」的聊天群，群里的成员自称是修真者。他以为是个中二病交流群，直到有人在群里发了一段御剑飞行的视频。宋书航揉了揉眼睛：这是特效吧？黄山真君回复：道友，这是真的。白前辈发了一个微笑的表情。'},
        {"title": '第二章 炼体', "content": '宋书航按照群里前辈们给的功法开始修炼，结果第一次炼体就把自己练进了医院。药师在群里安慰道：第一次都这样，习惯了就好。宋书航躺在病床上，手机震个不停——群里正在讨论他炼体爆炸的事。白前辈：有意思，再来一次？宋书航：……'},
        {"title": '第三章 九州一号群', "content": '宋书航加入了一个叫九州一号群的QQ群，群里的成员全是修真者。他以为是中二病患者的聚集地，直到有人在群里发了一张御剑飞行的照片。宋书航揉了揉眼睛：这是P的吧？北方大汉回复：小友，你可以亲自来试试。宋书航：……我还没准备好。'},
        {"title": '第四章 炼丹', "content": '宋书航尝试炼丹，第一次就炸了炉。丹炉碎片飞溅，他趴在地上狼狈不堪。群里炸开了锅——黄山真君：@宋书航，你用的是哪种丹方？宋书航：就是群文件里那个基础丹方。黄山真君沉默了三秒：那个丹方是给元婴期修士用的……'},
        {"title": '第五章 渡劫', "content": '宋书航终于要渡劫了，天劫降临的那一天，他紧张得手都在抖。雷云在头顶翻涌，第一道天雷劈了下来。宋书航掏出群里前辈送的避雷法宝——一把铁伞。咔嚓一声，铁伞被劈成了渣。宋书航抬头望天：群友骗我！群里回复：哈哈哈哈哈哈哈！'},
    ]},
    {"title": '超神机械师', "author": '齐佩甲', "chapters": [
        {"title": '第一章 星海', "content": '韩萧重生回到了星际游戏开服之前，带着前世几十年的游戏记忆。他知道每一个隐藏任务的位置，每一个版本的改动，每一个BOSS的弱点。他打开角色面板，嘴角微微上扬：这一次，我要成为整个星海最强的机械师。本杰明在远处看着他：这个NPC怎么不太一样？'},
        {"title": '第二章 机械系', "content": '韩萧选择了机械系职业，这是前世公认最废的系别——前期弱后期也弱。但他知道一个所有人都不知道的秘密：机械系在第三个版本会迎来史诗级加强。他在仓库里组装第一台机甲，焊枪的火花照亮了黑暗的角落。玩家的世界即将被一个NPC颠覆。'},
        {"title": '第三章 星海', "content": '韩萧重生到星海游戏世界，成了一名NPC。他利用前世的游戏知识，在这个星际时代如鱼得水。但他很快发现，这个世界远比游戏复杂——NPC也有自己的意志和情感。他看着手上的机械臂：我到底是玩家还是NPC？这个问题，可能永远没有答案。'},
        {"title": '第四章 机械师', "content": '韩萧选择了机械师职业，可以用意念操控机甲和武器。他的第一个作品是一把改装后的能量手枪，威力远超同级装备。其他玩家看到他的装备属性后惊呆了：这真的是新手装备？韩萧微微一笑：知识就是力量，尤其是在星际时代。'},
        {"title": '第五章 黑星', "content": '韩萧的代号——黑星，开始在星际间传开。他从一个默默无闻的机械师，成长为令各方势力忌惮的存在。但他始终保持着一个原则：不主动惹事，但绝不怕事。有记者问他成功的秘诀，韩萧想了想：大概是重生吧。记者：……您真幽默。'},
    ]},
    {"title": '我师兄实在太稳健了', "author": '言归正传', "chapters": [
        {"title": '第一章 度仙门', "content": '李长寿是度仙门的大师兄，修为平平，却活得比谁都久。他的生存之道只有两个字：稳健。能不出手绝不出手，能躲就躲，绝不站C位。小师妹蓝灵娥崇拜地看着他：大师兄好厉害！李长寿心中暗叹：我只是比别人更怕死而已。有度仙翁在背后撑腰，他倒是安然无恙。'},
        {"title": '第二章 封神', "content": '封神大劫将至，各路修士纷纷入局。李长寿却反其道而行，在门派里疯狂布置防御阵法，修了个铁桶般的洞府。酒玖道人来访：长寿啊，封神大劫你也该出去历练了。李长寿：师父，我觉得洞府修炼更适合我。纸人替身已经准备好了，随时可以替死。'},
        {"title": '第三章 封神', "content": '李长寿发现自己身处封神大劫之中，这可把他吓坏了。封神之战死了多少大能？他可不想成为其中之一。于是他开始疯狂研究如何避劫——法宝要多、底牌要厚、逃跑路线要提前规划。有苏妲己：道友，你怎么比我还谨慎？李长寿：活着才有输出。'},
        {"title": '第四章 纸人', "content": '李长寿发明了一种纸人替身术，可以在遇到危险时让纸人替死。他随身携带九十九个纸人，层层叠叠地藏在身上。蓝灵儿看着他鼓鼓囊囊的衣服：师兄，你是不是胖了？李长寿：没有，这叫有备无患。蓝灵儿：……你的备也太多了吧。'},
        {"title": '第五章 度仙门', "content": '李长寿建立了度仙门，门下弟子不多，但每一个都被他调教得跟他一样稳健。门规第一条：保命第一。第二条：见势不妙立刻跑。第三条：跑不了就装死。新弟子看了门规后问：师父，我们这是修仙门派还是保命门派？李长寿：有区别吗？'},
    ]},
    {"title": '紫川', "author": '老猪', "chapters": [
        {"title": '第一章 紫川家', "content": '紫川家的三个年轻人，紫川秀、帝林、斯特林，从远东军校同期毕业。紫川秀嬉皮笑脸，帝林阴狠毒辣，斯特林刚正不阿。三个性格截然不同的人，却成了最好的兄弟。紫川秀站在校门口，伸了个懒腰：毕业了，该干点什么呢？帝林推了推眼镜：杀人。斯特林：……你能不能正常点？'},
        {"title": '第二章 远东', "content": '远东战火纷飞，魔族大军压境。紫川秀被派往远东前线，面对的却是内部的阴谋与背叛。流风霜在远东的雪原上策马而行，冷风吹起她的长发。紫川秀望着漫天飞雪：这场战争，没有人是赢家。帝林在帝都冷冷地下令：叛徒，杀无赦。'},
        {"title": '第三章 帝林', "content": '帝林是紫川三杰中最为冷酷的一个，手段狠辣，从不留情。紫川秀看着他处理叛徒的方式，忍不住打了个寒颤：你能不能温和一点？帝林面无表情：温和解决不了问题。紫川秀：……你说的也对。斯特林在旁边叹气：你们两个能不能不要一个比一个极端？'},
        {"title": '第四章 远征', "content": '紫川家与魔族的大战一触即发，紫川秀被任命为远征军统帅。他站在地图前，标注着双方的兵力部署——敌众我寡，形势不容乐观。帝林：要我用非常手段吗？紫川秀摇头：不用，打就打，我紫川秀从来不靠阴谋。斯特林：……你这是在说我？'},
        {"title": '第五章 末日', "content": '紫川家走到了命运的十字路口，内部叛乱、外部强敌，一切都在崩塌。紫川秀站在燃烧的帝都城头，看着漫天的火光。他拔出长剑：紫川家的荣耀不会在我手中断绝。身后，帝林和斯特林并肩而立。三杰再聚，便是末日也不畏惧。'},
    ]},
    {"title": '武动乾坤', "author": '天蚕土豆', "chapters": [
        {"title": '第一章 青阳镇', "content": '青阳镇林家，少年林动在家族比武中垫底，被堂兄林琅天一招击倒。所有人都在嘲笑他，只有妹妹林可儿在角落默默流泪。林动擦了擦嘴角的血，从石池底摸出一块黑色的符文石——祖符。他的修炼之路，从这块石头开始。貂爷在暗处嗤笑：又一个被命运选中的倒霉蛋。'},
        {"title": '第二章 符师', "content": '林动发现自己能感知天地间的元力符文，这在天玄大陆是最稀缺的天赋——符师。他默默修炼，不争不抢，却一步步走出了青阳镇。小貂蹲在他肩头：小子，你的运气不错嘛。林动：运气？我走过的每一步都是自己拼出来的。'},
        {"title": '第三章 祖符', "content": '林动在山洞中发现了一块古老的符文石——祖符。这块符石蕴含着天地最原始的力量，也是他崛起的关键。小貂在旁边兴奋得吱吱叫：主人发财了！这可是祖符啊！林动：祖符是什么？小貂：简单说，就是能让你的实力暴涨的东西。林动：这还等什么？'},
        {"title": '第四章 异魔', "content": '异魔降临，天地变色。这些来自异世界的生物拥有毁灭一切的力量，各大宗门纷纷陷入苦战。林动手持祖符，站在异魔面前：想毁掉这个世界？先过我这一关。异魔嗤笑一声：区区一个天玄境的人类？林动激活祖符，金光冲天：让你看看人类的决心。'},
        {"title": '第五章 乾坤', "content": '林动终于达到了乾坤境，这是天玄大陆修士的至高境界。他站在九天之上，俯瞰苍生，感受着天地间的每一缕气息。绫清竹站在他身旁：你做到了。林动微微一笑：不是我做到了，是所有人一起做到的。乾坤之大，一个人终究太渺小。'},
    ]},
    {"title": '择天记', "author": '猫腻', "chapters": [
        {"title": '第一章 国教学院', "content": '陈长生是个命不好的人——星相显示他活不过二十岁。为了改命，他从西宁小镇来到京都，进入国教学院。这座学院已经没落多年，只剩下他一个学生。落落跟在他身后：先生，这里好破。陈长生看了看院中的梧桐树：破是破了点，但安静。徐有容在天书上写下了他的名字。'},
        {"title": '第二章 改命', "content": '大朝试是改变命运的机会，陈长生必须拿下第一。他通读道藏三千卷，将所有功法倒背如流。秋山君站在他对面：你一个将死之人，何必挣扎？陈长生平静地回答：正因为时日无多，每一天都不能浪费。教宗在塔顶注视着这个少年，眼中闪过一丝惊讶。'},
        {"title": '第三章 国教', "content": '陈长生进入国教学院，这是天海圣后亲自创办的最高学府。他在这里遇到了唐三十六——一个看似纨绔实则深藏不露的少年。唐三十六看了他一眼：你就是那个短命鬼？陈长生：我命由我不由天。唐三十六笑了：有意思，交个朋友？'},
        {"title": '第四章 改命', "content": '陈长生的命格是天生短命，星盘注定他活不过二十岁。但他偏偏不信命，入京参加大朝试，就是要改自己的命。天海圣后在大殿中看着他：你想改命？陈长生跪在地上：是。天海圣后：古今无人成功。陈长生抬头：总要有第一个。'},
        {"title": '第五章 星辰', "content": '大朝试的最后一关，陈长生要在星海中找到属于自己的星辰。无数星光在虚空中闪烁，每一颗都代表一条命格。他闭上眼，用心去感受——终于，在无尽的星海深处，他找到了那颗属于自己的星辰。它微弱却坚定，就像他自己。陈长生睁开眼：我找到了。'},
    ]},
    {"title": '圣墟', "author": '辰东', "chapters": [
        {"title": '第一章 地球', "content": '楚风在昆仑山脉旅游时误入一个神秘的铜山，发现了一枚金色的种子。种子入体之后，他感觉到整个世界都变了——远处的山峰在呼吸，天上的云彩有规律地流转。黄牛在旁边嚼着紫色的灵草：小子，你踩到造化了。楚风低头看了看脚下的裂缝：这是什么？黄牛：这是进化之路的入口。'},
        {"title": '第二章 进化', "content": '地球上突然出现了各种异果，吃下就能进化。楚风凭借金种子的优势，一路碾压。周曦在远处看着他：这人怎么比我还不要脸？楚风擦了擦嘴角的果汁：能吃是福。宇宙深处，有人在观测这颗蓝色星球：实验体觉醒了。'},
        {"title": '第三章 阳间', "content": '楚风来到阳间，这里是比地球更高层次的进化世界。进化者们拥有移山倒海的力量，而地球只是最底层的试炼场。楚风站在阳间的城市中，感受到了前所未有的压迫感：原来地球之外还有这么广阔的天地。黑皇趴在他肩上：小子，别怕，有本皇罩你。'},
        {"title": '第四章 进化路', "content": '楚风踏上了进化之路，这是一条充满竞争和杀戮的道路。每一层进化都需要珍贵的资源，而资源的争夺从来都是血腥的。楚风第一次出手就杀了一个想要截杀他的进化者，鲜血溅在脸上，他没有犹豫。黑皇：杀伐果断，我喜欢。'},
        {"title": '第五章 大黑', "content": '黑皇终于露出了真面目——它不是一条普通的狗，而是远古大黑转世。楚风知道真相后沉默了许久：你从一开始就在利用我？黑皇难得地低下了头：一开始是，后来不是。楚风叹了口气：你这条狗，真是让人又气又笑。黑皇：汪。'},
    ]},
    {"title": '深空彼岸', "author": '辰东', "chapters": [
        {"title": '第一章 新纪元', "content": '世界在一夜之间变了。大雾弥漫三天不散，此后不断有人觉醒超凡力量。王泽是一个普通的退休记者，却在旧书摊上发现了一本能感应灵光的笔记。他翻开第一页：新纪元，不是开始，而是回归。赵清菡在旁边好奇地凑过来：写的什么？王泽合上笔记：写的我们的未来。'},
        {"title": '第二章 超凡', "content": '各大财阀和秘境组织争相拉拢超凡者，世界秩序正在重建。王泽凭借笔记中的线索，找到了第一处秘境入口。超凡种子在体内缓缓萌发，他感受到了前所未有的力量。许长生在远处平静地看着：又一个迟来的觉醒者。王泽：迟到总比不到好。'},
        {"title": '第三章 新星', "content": '许长生在新星上建立了自己的根据地，开始研究超凡物质的本质。他发现超凡物质与宇宙暗能量之间存在某种共振关系，这可能就是超凡力量的根本来源。导师看了他的论文后沉默良久：你的理论如果成立，将改写整个超凡物理学。'},
        {"title": '第四章 暗域', "content": '暗域是超凡世界的禁区，那里暗物质浓度极高，任何超凡力量都会被吞噬。许长生为了追寻真相，独自踏入了暗域。黑暗如同实体般压来，五感全部失灵，只剩意识在虚无中飘荡。他咬牙坚持：如果连暗域都走不过去，还谈什么追根溯源。'},
        {"title": '第五章 深空', "content": '许长生终于抵达了深空——宇宙的尽头。在这里，他看到了超凡力量的起源：一颗正在坍缩的奇点。超凡物质从奇点中喷涌而出，如同宇宙的心脏在跳动。他伸出手，触碰到了奇点的边缘，一瞬间，他看到了宇宙诞生前的景象。许长生：原来如此。'},
    ]},
    {"title": '夜的命名术', "author": '会说话的肘子', "chapters": [
        {"title": '第一章 表里世界', "content": '庆尘是表世界的一个孤儿，每天在天台上看对面里世界的霓虹灯。两个世界共享一片天空，却有着截然不同的规则。他被一个叫李叔同的男人选中，带入了里世界。李叔同穿着风衣：从今天起，你是我的人。庆尘：我能拒绝吗？李叔同微笑：你觉得呢？'},
        {"title": '第二章 时间行者', "content": '时间行者可以在表里世界之间穿梭，庆尘是其中最特殊的一个——他能记住两个世界的一切细节。他在里世界开始崭露头角，从最底层的巷战开始，一步步走向权力的中心。胡靖在远处看着他：这个人太冷静了，冷静得不像少年。庆尘：不冷静的人都死了。'},
        {"title": '第三章 表世界', "content": '庆尘在表世界中小心翼翼地生活，白天上学，夜晚回到里世界。两个世界的规则完全不同，他必须在两者之间找到平衡。同学问他为什么总是睡不醒的样子，他只能苦笑：晚上失眠。没人知道他每晚都在另一个世界里冒险。'},
        {"title": '第四章 命名', "content": '庆尘获得了命名的能力——给事物命名就能赋予其特殊属性。他试着给一把普通的小刀命名为破甲，小刀果然获得了穿透防御的能力。他兴奋地继续尝试：给一只猫命名为飞行，猫果然飘了起来。猫：喵？？庆尘：抱歉，我控制不住我自己。'},
        {"title": '第五章 里世界之王', "content": '庆尘在里世界的地位越来越高，有人开始称他为夜之王。但他并不想要这个称号，他只想回到表世界过普通人的生活。可命运不允许他后退，里世界的危机正在向表世界蔓延。庆尘站在两个世界的交界处：我别无选择，只能向前。'},
    ]},
    {"title": '万族之劫', "author": '老鹰吃小鸡', "chapters": [
        {"title": '第一章 文明学府', "content": '方运是文明学府的一名普通学子，在这个人族与万族征战的世界里，他只想安安稳稳地读书。然而万族的阴影越来越近，学府里的征兵令已经下了三次。方运合上书本：看来书是读不成了。白枫在一旁：早就该去打仗了，读书有什么用？方运：读书能让你知道为什么打仗。'},
        {"title": '第二章 诸天战场', "content": '诸天战场是万族和人族的交锋之地，每天都有无数修士陨落。方运踏入战场的那一刻，就知道自己再也回不了头。万族强者如云，人族节节败退。他在血与火中觉醒了自己的天命：此战，不胜即亡。人皇在远处注视着他：此子或为人族之希望。'},
        {"title": '第三章 文明学府', "content": '苏宇进入文明学府修行，这里是人族最顶尖的学府。学府中藏有无数功法和秘术，但每一门都需要贡献点兑换。苏宇贡献点不够，只能去猎杀万族获取。他在城外遇到了一只低阶万族，对方口吐人言：人族小子，何必赶尽杀绝？苏宇：因为你们不会对人类手软。'},
        {"title": '第四章 意志海', "content": '苏宇开辟了意志海，这是修士精神力的具现空间。他的意志海有些特殊——里面住着一群古怪的老头，全是远古强者留下的意志残片。他们抢着教苏宇功法，吵得不可开交。苏宇：能不能排队？老头们：先学我的！不，先学我的！苏宇捂住了耳朵。'},
        {"title": '第五章 万族', "content": '万族围攻人族，战争全面爆发。苏宇站在战场上，看着铺天盖地的万族大军，心中没有恐惧，只有悲凉。人族已经退无可退，身后就是家园。他握紧手中的战刀：万族要灭我人族？先从我的尸体上踏过去。身后万人齐声高呼：人族不灭！'},
    ]},
    {"title": '我欲封天', "author": '耳根', "chapters": [
        {"title": '第一章 依靠山', "content": '孟浩是个落魄书生，靠抄书为生。他最大的愿望就是考个功名，过上安稳日子。可是命运跟他开了个玩笑——他被依靠山宗收为弟子，踏入了修仙界。他站在山门前，看着云雾缭绕的仙山，嘀咕道：我只是想考个秀才，怎么就成仙了？许清在一旁冷冷道：闭嘴，进去。'},
        {"title": '第二章 靠山', "content": '孟浩发现修仙界比他想象的残酷百倍。没有靠山寸步难行，所以他决定——找一个最大的靠山。他翻遍了宗门典籍，最终把目光锁定在了宗主身上。宗主：你为什么天天跟着我？孟浩：前辈，我觉得您就是我命中注定的靠山。宗主：……滚。'},
        {"title": '第三章 依山傍水', "content": '孟浩被分配到依山傍水宗，这个名字听起来就很不靠谱。果然，整个宗门只有一座破庙和三个老头。孟浩看着眼前的景象，沉默了：这就是我的宗门？三个老头齐刷刷点头：小伙子，别嫌弃，虽然穷了点，但胜在清静。孟浩：……我要求退货。'},
        {"title": '第四章 靠山宗', "content": '依山傍水宗的真实身份是靠山宗——上古三大宗门之一。孟浩发现这个秘密时震惊得说不出话。三个老头嘿嘿一笑：惊不惊喜？意不意外？孟浩：你们为什么不早说？老头：说了你还会留下来吗？孟浩想了想：不会。老头：那不就得了。'},
        {"title": '第五章 封天', "content": '孟浩终于触碰到了封天的门槛——这是修行的终极目标，以己之力封印天道。他站在苍穹之下，仰望星空。身后的世界在崩塌，但他的意志比天更高。孟浩伸出手，指尖触碰到了天道的边缘：我欲封天，天奈我何？天道沉默了。'},
    ]},
]


def _seed_books():
    books = _load_json(BOOKS_FILE, [])
    if books:
        return
    now = datetime.now().isoformat()[:19]
    for i, seed in enumerate(SEED_BOOKS):
        book_id = f"b_seed_{i + 1:03d}"
        chs = []
        for j, ch in enumerate(seed["chapters"]):
            chs.append({
                "id": f"ch_seed_{i + 1:03d}_{j + 1:02d}",
                "title": ch["title"],
                "content": ch["content"]
            })
        books.append({
            "id": book_id,
            "title": seed["title"],
            "author": seed["author"],
            "chapters": chs,
            "created_at": now,
            "updated_at": now,
        })
    _save_json(BOOKS_FILE, books)


_seed_books()


# ============ 健康检查 ============

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "6.2"}


# ============ 管理员认证 ============

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key="novel-rewriter-secret-key-2026")


class AdminLogin(BaseModel):
    password: str


@app.post("/api/admin/login")
async def admin_login(req: AdminLogin):
    if req.password != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="密码错误")
    return {"ok": True, "token": ADMIN_PWD}


@app.get("/api/admin/stats")
async def admin_stats(token: str = ""):
    if token != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="未授权")
    books = _load_json(BOOKS_FILE, [])
    rules = _load_json(RULES_FILE, [])
    total_ch = sum(len(b.get("chapters", [])) for b in books)
    total_chars = sum(
        sum(len(ch.get("content", "")) for ch in b.get("chapters", []))
        for b in books
    )
    return {
        "book_count": len(books),
        "chapter_count": total_ch,
        "total_chars": total_chars,
        "template_count": len(rules),
    }


@app.post("/api/admin/seed")
async def admin_reseed(token: str = ""):
    if token != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="未授权")
    _save_json(BOOKS_FILE, [])
    _seed_books()
    books = _load_json(BOOKS_FILE, [])
    return {"ok": True, "book_count": len(books)}


@app.put("/api/admin/books/{book_id}/chapters/batch")
async def batch_add_chapters(book_id: str, data: dict, token: str = ""):
    if token != ADMIN_PWD:
        raise HTTPException(status_code=401, detail="未授权")
    books = _load_json(BOOKS_FILE, [])
    for b in books:
        if b["id"] == book_id:
            chapters = data.get("chapters", [])
            existing = len(b.get("chapters", []))
            for i, ch in enumerate(chapters):
                b.setdefault("chapters", []).append({
                    "id": f"ch_{book_id}_{existing + i + 1:02d}",
                    "title": ch.get("title", f"第{existing + i + 1}章"),
                    "content": ch.get("content", ""),
                })
            b["updated_at"] = datetime.now().isoformat()[:19]
            _save_json(BOOKS_FILE, books)
            return {"ok": True, "added": len(chapters)}
    raise HTTPException(status_code=404, detail="书籍不存在")


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
