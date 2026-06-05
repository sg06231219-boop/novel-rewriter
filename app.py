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

# ============ 初始数据 ============

SEED_BOOKS = [
    {"title":"斗破苍穹","author":"天蚕土豆","chapters":[
        {"title":"第一章 陨落的天才","content":"乌坦城萧家，少年盘膝而坐，掌心处一缕斗之气若隐若现。三年前他还是家族天才，如今却沦为笑柄。房间的门被推开，一位老者身躯虚幻，悬浮在半空之中，朗声笑道：小家伙，别灰心，有我在，你迟早会站在大陆之巅。药老捋了捋胡须，目光深邃地望着窗外。"},
        {"title":"第二章 休妻","content":"云岚宗的纳兰嫣然登门退婚，萧炎握紧拳头，指节发白。他看着面前这个曾经视为至宝的女子，淡淡说道：三年之后，我上云岚宗，若你接不下我三招，你纳兰家族从此在我萧家面前抬不起头。纳兰嫣然冷笑一声，转身离去。"},
        {"title":"第三章 异火","content":"萧炎在魔兽山脉深处，感受着周围炙热的气息。岩浆翻涌之间，一簇青色的火焰若隐若现，正是传说中的青莲地心火。药老沉声道：小心，异火暴烈，稍有不慎便是灰飞烟灭的下场。萧炎深吸一口气，运起焚诀，缓缓靠近那簇火焰。"}
    ]},
    {"title":"凡人修仙传","author":"忘语","chapters":[
        {"title":"第一章 七玄门","content":"青牛镇往东三十里，有一座无名荒山，山脚下便是七玄门的驻地。韩立站在山门前，手里攥着一封推荐信。门前看守的弟子斜眼看了他一下：新来的？进去吧，找执事弟子登记。韩立默默点头，迈步走了进去。"},
        {"title":"第二章 神秘小瓶","content":"韩立回到住处，从怀中取出一个翠绿色的小瓶。这瓶子是他偶然得到的，瓶中能催熟灵草。他小心翼翼地收好小瓶：在这修仙世界里，没有背景没有天赋，唯有此物是我最大的倚仗。"}
    ]},
    {"title":"诡秘之主","author":"爱潜水的乌贼","chapters":[
        {"title":"第一章 廷根","content":"周明瑞从模糊的梦境中醒来，发现自己躺在一张硬木板床上。房间狭小，只有一扇窗户透进灰蒙蒙的光线。他摸了摸自己的脸——这不是他原来的脸。桌上放着一面小镜子，镜中人是标准的鲁恩人模样。他低声自语：我穿越了？门外传来脚步声，一个穿黑色风衣的男子推门而入。"},
        {"title":"第二章 占卜家","content":"克莱恩翻阅着邓恩留下的笔记，上面记载着各种非凡途径的信息。占卜家途径、偷盗者途径——每一条途径都通向不同的命运。他合上笔记本：序列9占卜家，这是我踏入非凡世界的第一步。黑夜女神庇佑。"}
    ]},
    {"title":"全职高手","author":"蝴蝶蓝","chapters":[
        {"title":"第一章 回到起点","content":"叶修坐在电脑前，屏幕上的荣耀图标闪闪发光。十年职业生涯，他如今却被俱乐部驱逐。他笑了笑，拿起鼠标：没关系，从哪里开始，就从哪里重新开始。嘉世俱乐部的大门在他身后缓缓关上。"},
        {"title":"第二章 散人君莫笑","content":"荣耀第十区开服，一个名叫君莫笑的散人横空出世。千机伞在手中翻转，各种低级技能被他组合出匪夷所思的连招。唐柔站在他身后问道：这个角色没有转职？叶修推了推眼镜：散人玩法，二十四个职业的技能都可以用。"}
    ]},
    {"title":"斗罗大陆","author":"唐家三少","chapters":[
        {"title":"第一章 斗罗大陆","content":"唐三感觉自己仿佛做了一个很长的梦。等他再次睁开眼睛的时候，发现自己身处一间简陋的木屋之中。他低头看了看自己的小手——这不是他的手。脑海中两股记忆涌来。他暗自运了运内力，发现玄天功居然还能运转。"},
        {"title":"第二章 武魂觉醒","content":"武魂殿的长老来到圣魂村，为六岁的孩子们觉醒武魂。轮到唐三时，一道蓝光从掌心升起——蓝银草。长老摇了摇头，正要记下废武魂，唐三的右手却又浮现出一道金光：昊天锤。唐昊在一旁沉默不语。"}
    ]},
    {"title":"诛仙","author":"萧鼎","chapters":[
        {"title":"第一章 青云","content":"草庙村，一个偏僻的小村庄，坐落在群山环抱之中。少年张小凡父母早亡。这一天，村子里来了一个道人，自称是青云门外门弟子。张小凡的命运从此改变。天空中一道青光划过，青云门的弟子御剑飞行，洒下漫天星光。"},
        {"title":"第二章 拜师","content":"张小凡被田不易收为大竹峰弟子。他资质平平，修炼进展缓慢，却从不抱怨。每天清晨他都会去后山喂那条叫大黄的狗。田灵儿偶尔路过，笑他笨，他也只是憨厚地笑笑。"}
    ]},
    {"title":"遮天","author":"辰东","chapters":[
        {"title":"第一章 星空古路","content":"九具仙尸拉着一口青铜古棺，在无尽的星空中缓缓前行。叶凡只是泰山旅游的一个普通大学生，却被卷入这场浩大的星域穿越之中。他看着脚下的火星大地，心中只有一个念头：我要回家。庞博拍了拍他的肩膀：先活下去再说。"},
        {"title":"第二章 荒古禁地","content":"荒古禁地，传说中的生命禁区。叶凡只觉得体内有什么东西在觉醒，那枚源天书上的文字仿佛活了过来。老疯子在远处疯狂大笑：好一个荒古圣体，万年后终于又出现了！"}
    ]},
    {"title":"大奉打更人","author":"卖报小郎君","chapters":[
        {"title":"第一章 大奉王朝","content":"许七安从醉梦中醒来，发现自己成了一名打更人。大奉王朝国运昌盛，京城里暗流涌动。打更人不仅仅是巡夜的差事，更是一个庞大的情报机构。他整了整衣冠：既然来了，就得好好活下去。"},
        {"title":"第二章 儒家修行","content":"许七安盘膝而坐，面前摆着一本《大学》。儒家的修行方式极为特殊，只需读书明理便可提升境界。他念诵经典，体内浩然正气缓缓流转。窗外传来打更的梆子声：这日子倒是比前世有趣多了。"}
    ]},
    {"title":"雪中悍刀行","author":"烽火戏诸侯","chapters":[
        {"title":"第一章 北凉世子","content":"北凉王府，徐凤年裹着狐裘，站在城头远眺。他是天下第一纨绔，北凉王的独子。三年游历归来，他不再是那个只会斗鸡走狗的世子。徐骁站在他身后：凤年，北凉的担子，迟早要你来扛。徐凤年没有回头，只是将手中的刀握得更紧了些。"},
        {"title":"第二章 老黄","content":"老黄是王府的老仆，走路一瘸一拐。可徐凤年知道，这个老人曾经是江湖上赫赫有名的剑九黄。老黄端来一碗热粥：世子殿下，该用膳了。徐凤年接过碗：老黄，你当年为何弃剑？老黄沉默良久：因为遇见了一个扛刀的人。"}
    ]},
    {"title":"庆余年","author":"猫腻","chapters":[
        {"title":"第一章 澹州","content":"范闲在澹州长大，名义上是范建的私生子。他自幼习武，又跟着费介学了用毒之术。五竹叔总是蒙着黑布站在角落里，从不说话。他望着京都的方向，心中暗想：娘亲留下的那个箱子，里面到底装着什么？"},
        {"title":"第二章 进京","content":"范闲终于踏入了京都城门。这座天下的中心，远比他想象的更加波谲云诡。长公主的笑意、太子的试探——每个人都在他身上打着主意。他微微一笑：我既然来了，就不打算只做一枚棋子。"}
    ]},
    {"title":"仙逆","author":"耳根","chapters":[
        {"title":"第一章 赵国","content":"赵国边陲小镇，王林是个普通农家少年。他天资愚钝，却有一个不为人知的秘密——他的脑海中有一个神秘的空间。恒岳派来镇上选弟子，他凭借最后一丝运气被选中。踏入修仙界的第一天，他就明白了：仙路漫漫，唯有逆天而行，方有一线生机。"}
    ]},
    {"title":"一念永恒","author":"耳根","chapters":[
        {"title":"第一章 灵溪宗","content":"白小纯是个怕死的少年，他来灵溪宗的唯一目的就是长生不老。他蹲在灶房里研究灵食，将一株灵草炖成了汤，结果整个灶房炸了。侯小妹从烟尘中走出来，黑着脸：白小纯！你又在搞什么？白小纯嘿嘿一笑：我在研究新的长寿秘方。"}
    ]},
    {"title":"盗墓笔记","author":"南派三叔","chapters":[
        {"title":"第一章 血尸","content":"吴邪坐在铺子里百无聊赖，一个陌生人递来一份战国帛书的拓片。帛书上画着一张古怪的地图，标注着一座位于长沙的战国古墓。三叔看了拓片后脸色大变：这座墓，我找了几十年。吴邪跟着三叔下了斗，墓道深处传来低沉的喘息声，血尸的指甲在石壁上划出刺耳的声响。"},
        {"title":"第二章 七星鲁王宫","content":"七星鲁王宫是周朝的诸侯墓，机关重重，步步杀机。胖子一脚踩空差点掉进暗河，张起灵一把将他拉了回来。闷油瓶一如既往面无表情，黑金古刀在黑暗中闪着寒光。吴邪暗暗心惊：这墓里不只有我们，还有人在前面走了很远。"}
    ]},
    {"title":"神墓","author":"辰东","chapters":[
        {"title":"第一章 神墓","content":"万年前的大神独孤败天陨落了，他的墓地却成了后世修者心中的圣地。辰南从混沌中醒来，发现自己躺在一座巨大的古墓之中。周围是无数神魔的尸体，不朽的气息弥漫在空中。他摸了摸自己的身体——他还活着。一个苍老的声音在墓中回响：你终于醒了。"},
        {"title":"第二章 澹台圣地","content":"辰南走出神墓，发现外面的世界已经沧海桑田。万年光阴流转，曾经的大陆早已面目全非。澹台圣地的圣女发现了这个从古墓中走出的青年，惊为天人。辰南望着天空：这个世界，比万年前更加疯狂了。魔主在暗处冷冷注视着他。"}
    ]},
    {"title":"吞噬星空","author":"我吃西红柿","chapters":[
        {"title":"第一章 罗峰","content":"基地市的天空灰蒙蒙的，怪兽横行的荒野区才是武者们的战场。罗峰站在武馆门口，手里攥着刚刚通过准武者考核的成绩单。他从普通学生到准武者，用了三年。弟弟罗华坐在轮椅上，笑着朝他竖起大拇指。罗峰目光坚定：我一定会成为真正的武者，让家人过上好日子。"},
        {"title":"第二章 荒野区","content":"罗峰第一次踏入荒野区，空气中弥漫着血腥和硝烟的味道。铁甲龙在远处咆哮，独角兽成群结队地奔跑。他握紧手中的战刀，缓缓呼出一口气。洪在屏幕那头说：记住，荒野区没有规则，活下来就是最大的胜利。雷神在一旁补充：还有，别太贪。"}
    ]},
    {"title":"完美世界","author":"辰东","chapters":[
        {"title":"第一章 石村","content":"大荒深处，一个叫石村的小村庄坐落在苍茫群山之间。村口有一块巨大的石头，据说是一尊远古神灵的遗骸。石昊是村里的孩子王，天生神力，却总被村长嫌调皮。他趴在巨石上晒太阳，忽然感觉石头里有什么东西在跳动。柳神在村口的老柳树下安静地看着这一切。"},
        {"title":"第二章 补天阁","content":"补天阁是大荒中的顶级势力，专门收有天资的孩子修行。石昊凭借一己之力打上了山门，惊动了阁中长老。长老们面面相觑：这孩子的血脉……不像是人族的。石昊擦了擦鼻血：管他什么血脉，先让我进去吃饭再说。"}
    ]},
    {"title":"牧神记","author":"宅猪","chapters":[
        {"title":"第一章 残老村","content":"大墟的黄昏总是来得特别早。残老村中，一群残缺不全的老人围坐在篝火旁，给秦牧讲述着外面的世界。秦牧从小被他们养大，学会了药师的毒、屠夫的刀、瞎子的枪、聋子的画。他站在村口，望着外面的天地：我要出去看看。村长瘸着腿追出来：外面很危险！秦牧回头一笑：有你们教的本事，我怕什么？"},
        {"title":"第二章 延康","content":"延康国是大墟之外最大的国度，国师提倡变法，废除旧神信仰，以人定胜天为纲。秦牧第一次踏入延康国境，就被这里的繁华震撼了。他挤在人群中看着国师变法的布告，心中暗想：这个世界比残老村复杂太多了。延丰帝在宫中叹道：变法之路，何其艰难。"}
    ]},
    {"title":"大王饶命","author":"会说话的肘子","chapters":[
        {"title":"第一章 负面情绪","content":"吕树从小在孤儿院长大，靠卖红薯为生。有一天他发现自己能吸收别人的负面情绪值来变强。别人越骂他，他越开心。吕小鱼在旁边啃着红薯：哥，你是不是有病？吕树嘿嘿一笑：你不懂，他们骂我一次，我就变强一分。这生意，稳赚不赔。"},
        {"title":"第二章 觉醒","content":"吕树参加了天罗地网的觉醒测试，结果觉醒的是最没用的负情绪收集天赋。考官们面面相觑：这天赋……闻所未闻。吕树毫不在意，默默记下了每个考官的表情——愤怒值+2，嫌弃值+3，不屑值+5。他心中暗爽：谢谢各位老板。"}
    ]},
    {"title":"赘婿","author":"愤怒的香蕉","chapters":[
        {"title":"第一章 江宁","content":"江宁城苏家，宁毅作为赘婿嫁了进来，人人看不起。他坐在后院里翻着一本账册，嘴角挂着若有若无的笑。苏檀儿从窗前走过，冷冷看了他一眼：你若是个男人，就该出去闯荡，而不是待在家里吃软饭。宁毅合上账册：吃软饭也要讲究方法，你看这账，你们苏家至少亏了三成。"},
        {"title":"第二章 布局","content":"宁毅不动声色地布局，从染坊的供应链到城中的商路，每一步都算无遗策。秦嗣源在朝堂上收到密报：江宁有个赘婿，手段不凡。宁毅站在苏家染坊的屋顶上，望着远方的烽烟：天下将乱，我不过提前准备罢了。陆红提在暗处默默守护。"}
    ]},
    {"title":"灵剑山","author":"国王陛下","chapters":[
        {"title":"第一章 灵剑派","content":"王陆参加灵剑派的入门考核，却发现这个修仙门派的画风完全不对。师父王舞是个酒鬼，整天泡在酒坛子里，考核全靠蒙。王陆站在山门前，看着摇摇欲坠的牌匾，心中暗想：这真的是传说中的五大宗门之一？王舞打了个酒嗝：小子，别嫌，进来就是了。"},
        {"title":"第二章 落云峰","content":"落云峰是灵剑派最穷的山头，王舞是整个门派最不靠谱的长老。王陆被分配到这里，发现连修炼功法都要自己去藏经阁偷。他叹了口气：别人修仙靠天赋，我修仙靠脸皮厚。王舞翘着二郎腿：年轻人，修行之道在于悟，你悟了吗？王陆：悟了，师父你最懒。"}
    ]},
    {"title":"修真聊天群","author":"圣骑士的传说","chapters":[
        {"title":"第一章 聊天群","content":"宋书航不小心加入了一个名叫「九州一号群」的聊天群，群里的成员自称是修真者。他以为是个中二病交流群，直到有人在群里发了一段御剑飞行的视频。宋书航揉了揉眼睛：这是特效吧？黄山真君回复：道友，这是真的。白前辈发了一个微笑的表情。"},
        {"title":"第二章 炼体","content":"宋书航按照群里前辈们给的功法开始修炼，结果第一次炼体就把自己练进了医院。药师在群里安慰道：第一次都这样，习惯了就好。宋书航躺在病床上，手机震个不停——群里正在讨论他炼体爆炸的事。白前辈：有意思，再来一次？宋书航：……"}
    ]},
    {"title":"超神机械师","author":"齐佩甲","chapters":[
        {"title":"第一章 星海","content":"韩萧重生回到了星际游戏开服之前，带着前世几十年的游戏记忆。他知道每一个隐藏任务的位置，每一个版本的改动，每一个BOSS的弱点。他打开角色面板，嘴角微微上扬：这一次，我要成为整个星海最强的机械师。本杰明在远处看着他：这个NPC怎么不太一样？"},
        {"title":"第二章 机械系","content":"韩萧选择了机械系职业，这是前世公认最废的系别——前期弱后期也弱。但他知道一个所有人都不知道的秘密：机械系在第三个版本会迎来史诗级加强。他在仓库里组装第一台机甲，焊枪的火花照亮了黑暗的角落。玩家的世界即将被一个NPC颠覆。"}
    ]},
    {"title":"我师兄实在太稳健了","author":"言归正传","chapters":[
        {"title":"第一章 度仙门","content":"李长寿是度仙门的大师兄，修为平平，却活得比谁都久。他的生存之道只有两个字：稳健。能不出手绝不出手，能躲就躲，绝不站C位。小师妹蓝灵娥崇拜地看着他：大师兄好厉害！李长寿心中暗叹：我只是比别人更怕死而已。有度仙翁在背后撑腰，他倒是安然无恙。"},
        {"title":"第二章 封神","content":"封神大劫将至，各路修士纷纷入局。李长寿却反其道而行，在门派里疯狂布置防御阵法，修了个铁桶般的洞府。酒玖道人来访：长寿啊，封神大劫你也该出去历练了。李长寿：师父，我觉得洞府修炼更适合我。纸人替身已经准备好了，随时可以替死。"}
    ]},
    {"title":"紫川","author":"老猪","chapters":[
        {"title":"第一章 紫川家","content":"紫川家的三个年轻人，紫川秀、帝林、斯特林，从远东军校同期毕业。紫川秀嬉皮笑脸，帝林阴狠毒辣，斯特林刚正不阿。三个性格截然不同的人，却成了最好的兄弟。紫川秀站在校门口，伸了个懒腰：毕业了，该干点什么呢？帝林推了推眼镜：杀人。斯特林：……你能不能正常点？"},
        {"title":"第二章 远东","content":"远东战火纷飞，魔族大军压境。紫川秀被派往远东前线，面对的却是内部的阴谋与背叛。流风霜在远东的雪原上策马而行，冷风吹起她的长发。紫川秀望着漫天飞雪：这场战争，没有人是赢家。帝林在帝都冷冷地下令：叛徒，杀无赦。"}
    ]},
    {"title":"武动乾坤","author":"天蚕土豆","chapters":[
        {"title":"第一章 青阳镇","content":"青阳镇林家，少年林动在家族比武中垫底，被堂兄林琅天一招击倒。所有人都在嘲笑他，只有妹妹林可儿在角落默默流泪。林动擦了擦嘴角的血，从石池底摸出一块黑色的符文石——祖符。他的修炼之路，从这块石头开始。貂爷在暗处嗤笑：又一个被命运选中的倒霉蛋。"},
        {"title":"第二章 符师","content":"林动发现自己能感知天地间的元力符文，这在天玄大陆是最稀缺的天赋——符师。他默默修炼，不争不抢，却一步步走出了青阳镇。小貂蹲在他肩头：小子，你的运气不错嘛。林动：运气？我走过的每一步都是自己拼出来的。"}
    ]},
    {"title":"择天记","author":"猫腻","chapters":[
        {"title":"第一章 国教学院","content":"陈长生是个命不好的人——星相显示他活不过二十岁。为了改命，他从西宁小镇来到京都，进入国教学院。这座学院已经没落多年，只剩下他一个学生。落落跟在他身后：先生，这里好破。陈长生看了看院中的梧桐树：破是破了点，但安静。徐有容在天书上写下了他的名字。"},
        {"title":"第二章 改命","content":"大朝试是改变命运的机会，陈长生必须拿下第一。他通读道藏三千卷，将所有功法倒背如流。秋山君站在他对面：你一个将死之人，何必挣扎？陈长生平静地回答：正因为时日无多，每一天都不能浪费。教宗在塔顶注视着这个少年，眼中闪过一丝惊讶。"}
    ]},
    {"title":"圣墟","author":"辰东","chapters":[
        {"title":"第一章 地球","content":"楚风在昆仑山脉旅游时误入一个神秘的铜山，发现了一枚金色的种子。种子入体之后，他感觉到整个世界都变了——远处的山峰在呼吸，天上的云彩有规律地流转。黄牛在旁边嚼着紫色的灵草：小子，你踩到造化了。楚风低头看了看脚下的裂缝：这是什么？黄牛：这是进化之路的入口。"},
        {"title":"第二章 进化","content":"地球上突然出现了各种异果，吃下就能进化。楚风凭借金种子的优势，一路碾压。周曦在远处看着他：这人怎么比我还不要脸？楚风擦了擦嘴角的果汁：能吃是福。宇宙深处，有人在观测这颗蓝色星球：实验体觉醒了。"}
    ]},
    {"title":"深空彼岸","author":"辰东","chapters":[
        {"title":"第一章 新纪元","content":"世界在一夜之间变了。大雾弥漫三天不散，此后不断有人觉醒超凡力量。王泽是一个普通的退休记者，却在旧书摊上发现了一本能感应灵光的笔记。他翻开第一页：新纪元，不是开始，而是回归。赵清菡在旁边好奇地凑过来：写的什么？王泽合上笔记：写的我们的未来。"},
        {"title":"第二章 超凡","content":"各大财阀和秘境组织争相拉拢超凡者，世界秩序正在重建。王泽凭借笔记中的线索，找到了第一处秘境入口。超凡种子在体内缓缓萌发，他感受到了前所未有的力量。许长生在远处平静地看着：又一个迟来的觉醒者。王泽：迟到总比不到好。"}
    ]},
    {"title":"夜的命名术","author":"会说话的肘子","chapters":[
        {"title":"第一章 表里世界","content":"庆尘是表世界的一个孤儿，每天在天台上看对面里世界的霓虹灯。两个世界共享一片天空，却有着截然不同的规则。他被一个叫李叔同的男人选中，带入了里世界。李叔同穿着风衣：从今天起，你是我的人。庆尘：我能拒绝吗？李叔同微笑：你觉得呢？"},
        {"title":"第二章 的时间行者","content":"时间行者可以在表里世界之间穿梭，庆尘是其中最特殊的一个——他能记住两个世界的一切细节。他在里世界开始崭露头角，从最底层的巷战开始，一步步走向权力的中心。胡靖在远处看着他：这个人太冷静了，冷静得不像少年。庆尘：不冷静的人都死了。"}
    ]},
    {"title":"万族之劫","author":"老鹰吃小鸡","chapters":[
        {"title":"第一章 文明学府","content":"方运是文明学府的一名普通学子，在这个人族与万族征战的世界里，他只想安安稳稳地读书。然而万族的阴影越来越近，学府里的征兵令已经下了三次。方运合上书本：看来书是读不成了。白枫在一旁：早就该去打仗了，读书有什么用？方运：读书能让你知道为什么打仗。"},
        {"title":"第二章 诸天战场","content":"诸天战场是万族和人族的交锋之地，每天都有无数修士陨落。方运踏入战场的那一刻，就知道自己再也回不了头。万族强者如云，人族节节败退。他在血与火中觉醒了自己的天命：此战，不胜即亡。人皇在远处注视着他：此子或为人族之希望。"}
    ]},
    {"title":"我欲封天","author":"耳根","chapters":[
        {"title":"第一章 依靠山","content":"孟浩是个落魄书生，靠抄书为生。他最大的愿望就是考个功名，过上安稳日子。可是命运跟他开了个玩笑——他被依靠山宗收为弟子，踏入了修仙界。他站在山门前，看着云雾缭绕的仙山，嘀咕道：我只是想考个秀才，怎么就成仙了？许清在一旁冷冷道：闭嘴，进去。"},
        {"title":"第二章 靠山","content":"孟浩发现修仙界比他想象的残酷百倍。没有靠山寸步难行，所以他决定——找一个最大的靠山。他翻遍了宗门典籍，最终把目光锁定在了宗主身上。宗主：你为什么天天跟着我？孟浩：前辈，我觉得您就是我命中注定的靠山。宗主：……滚。"}
    ]},
]

def _seed_books():
    """如果书库为空，灌入初始热门小说"""
    books = _load_json(BOOKS_FILE, [])
    if books:
        return  # 已有数据，不重复灌
    now = datetime.now().isoformat()[:19]
    for i, seed in enumerate(SEED_BOOKS):
        book_id = f"b_seed_{i+1:03d}"
        chs = []
        for j, ch in enumerate(seed["chapters"]):
            chs.append({"id": f"ch_seed_{i+1:03d}_{j+1:02d}", "title": ch["title"], "content": ch["content"]})
        books.append({
            "id": book_id,
            "title": seed["title"],
            "author": seed["author"],
            "chapters": chs,
            "created_at": now,
            "updated_at": now,
        })
    _save_json(BOOKS_FILE, books)

# 启动时自动种数据
_seed_books()

# ============ 健康检查 ============

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "3.0"}

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
