import os
import re
import json
import threading
import time
from collections import OrderedDict
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

FEISHU_APP_ID     = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]

# ── AI 自动切换 ───────────────────────────────────────────────
AI_API_KEY  = os.environ["AI_API_KEY"]
AI_BASE_URL = os.environ.get("AI_BASE_URL", "")
AI_MODEL    = os.environ.get("AI_MODEL", "claude-sonnet-4-6")

if not AI_BASE_URL:
    import anthropic
    _claude = anthropic.Anthropic(api_key=AI_API_KEY)
    def ai_call(system, user_text):
        msg = _claude.messages.create(
            model=AI_MODEL, max_tokens=2000, system=system,
            messages=[{"role": "user", "content": user_text}]
        )
        return msg.content[0].text
else:
    from openai import OpenAI
    _ai = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    def ai_call(system, user_text):
        resp = _ai.chat.completions.create(
            model=AI_MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": f"{system}\n\n---\n\n{user_text}"}]
        )
        return resp.choices[0].message.content

# ── 加载 skill 文件 ──────────────────────────────────────────
def load_skill(name):
    path = os.path.join(os.path.dirname(__file__), "skills", f"{name}.md")
    with open(path, encoding="utf-8") as f:
        return f.read()

SKILL_HUMANIZER              = load_skill("humanizer-zh")
SKILL_VIDEO_REWRITE_FARM     = load_skill("video-rewrite-farmer")
SKILL_VIDEO_REWRITE_DEAL     = load_skill("video-rewrite-dealer")
SKILL_BRAINSTORM_FARM        = load_skill("brainstorm-topics")
SKILL_BRAINSTORM_DEAL        = load_skill("brainstorm-dealers")
SKILL_SCRIPT_FARM            = load_skill("script-farmer")
SKILL_SCRIPT_DEAL            = load_skill("script-dealer")
SKILL_PRODUCT_PAIN           = load_skill("script-product-pain")
SKILL_PRODUCT_ITCH           = load_skill("script-product-itch")
SKILL_PRODUCT_STORY          = load_skill("script-product-story")

# ── 加载产品配置 ────────────────────────────────────────────
def load_products():
    path = os.path.join(os.path.dirname(__file__), "products.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)

PRODUCTS = load_products()

# 作物 emoji 映射
CROP_EMOJI = {
    "小麦": "🌾", "玉米": "🌽", "水稻": "🌾",
    "花生": "🥜", "大豆": "🫘", "瓜果蔬菜": "🥦",
    "棉花": "🫘",
}

# ── 飞书多维表格配置 ─────────────────────────────────────────
BITABLE_APP_TOKEN     = os.environ.get("BITABLE_APP_TOKEN", "")
BITABLE_TOPIC_TABLE   = os.environ.get("BITABLE_TOPIC_TABLE_ID", "")   # 头脑风暴
BITABLE_DEVELOP_TABLE = os.environ.get("BITABLE_DEVELOP_TABLE_ID", "") # 完善选题
BITABLE_REWRITE_TABLE = os.environ.get("BITABLE_REWRITE_TABLE_ID", "") # 改文案
USE_BITABLE = bool(BITABLE_APP_TOKEN)

# ── 会话状态 ──────────────────────────────────────────────────
pending_states = {}
STATE_TTL = 300

# ── 内存缓存（Bitable 未配置时的降级方案）──────────────────────
result_cache = {}
CACHE_TTL = 86400

def cache_set(key, value):
    result_cache[key] = {"data": value, "expires": time.time() + CACHE_TTL}

def cache_get(key):
    entry = result_cache.get(key)
    if entry and time.time() < entry["expires"]:
        return entry["data"]
    return None

def make_cache_key(chat_id):
    return f"{chat_id}_{int(time.time())}"

# ── 输入模板 ─────────────────────────────────────────────────
TEMPLATES_FARM = {
    "产品推广":  "产品：\n卖点：\n地区：",
    "解决方案":  "问题：\n生长阶段：\n地区：",
    "观点/吐槽": "话题：\n地区（可选）：",
    "案例故事":  "情况：\n地区：",
    "知识科普":  "主题：\n地区（可选）：",
}

TEMPLATES_DEAL = {
    "模式介绍":  "话题方向（可选）：\n地区（可选，默认江浙沪皖豫）：",
    "合作案例":  "情况：\n地区：",
    "经销商干货": "话题：\n地区（可选）：",
    "产品实证":  "产品：\n实验结果（可选）：\n地区：",
    "行业观点":  "话题：\n地区（可选）：",
}

TEMPLATE_DEVELOP_FARM         = "话题方向：\n已有想法（没有就写「无」）：\n背景信息（产品/场景/地区，可选）："
TEMPLATE_DEVELOP_FARMER_BRAND = "话题方向：\n已有想法（没有就写「无」）：\n地区（可选）："
TEMPLATE_DEVELOP_FARMER_PRODUCT = "话题方向：\n产品名称：\n核心卖点（1-3条）：\n产品解决的痛点：\n地区（可选）："
TEMPLATE_DEVELOP_DEAL         = "话题方向：\n已有想法（没有就写「无」）：\n地区（可选，默认江浙沪皖豫）："

# ── 解析头脑风暴输出 ─────────────────────────────────────────
def parse_brainstorm(text):
    blocks = re.split(r'【选题\d+】', text)
    topics = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        label_match = re.search(r'(?:按钮标签|选题名称)[：:]\s*(.+)', block)
        label = label_match.group(1).strip() if label_match else block[:12]
        topics.append((label, block))
    return topics[:3]

# ── 飞书基础 API ─────────────────────────────────────────────
_token_cache = {"token": "", "expires": 0}
_token_lock = threading.Lock()

def get_tenant_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"]:
        return _token_cache["token"]
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires"]:
            return _token_cache["token"]
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
        )
        data = r.json()
        _token_cache["token"] = data["tenant_access_token"]
        _token_cache["expires"] = time.time() + data.get("expire", 7200) - 60
        return _token_cache["token"]

def send_text(chat_id, text):
    token = get_tenant_token()
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": chat_id, "msg_type": "text",
              "content": json.dumps({"text": text})}
    )

def send_card(chat_id, card):
    token = get_tenant_token()
    r = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": chat_id, "msg_type": "interactive",
              "content": json.dumps(card)}
    )
    print(f"[send_card] chat_id={chat_id} status={r.status_code} body={r.text[:300]}")

def update_card(message_id, card):
    for attempt in range(3):
        try:
            token = get_tenant_token()
            r = requests.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"msg_type": "interactive", "content": json.dumps(card)},
                timeout=10
            )
            if r.status_code == 200:
                return
            print(f"[update_card] attempt={attempt} status={r.status_code} body={r.text[:200]}", flush=True)
        except requests.RequestException as e:
            print(f"[update_card] attempt={attempt} error={e}", flush=True)
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))

# ── 多维表格 API ─────────────────────────────────────────────
def bitable_create(table_id, fields):
    """写入一条记录，返回 record_id"""
    token = get_tenant_token()
    r = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": fields}
    )
    print(f"[bitable_create] status={r.status_code} body={r.text[:200]}", flush=True)
    return r.json().get("data", {}).get("record", {}).get("record_id", "")

def bitable_update(table_id, record_id, fields):
    """更新一条记录的字段"""
    token = get_tenant_token()
    r = requests.put(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": fields}
    )
    print(f"[bitable_update] status={r.status_code} body={r.text[:200]}", flush=True)

def bitable_get(table_id, record_id):
    """读取一条记录的 fields"""
    token = get_tenant_token()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"}
    )
    return r.json().get("data", {}).get("record", {}).get("fields", {})

def bitable_search_latest(table_id, chat_id):
    """查询某 chat_id 最新一条记录，返回 fields 或 {}"""
    token = get_tenant_token()
    r = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records/search",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "filter": {
                "conjunction": "and",
                "conditions": [{"field_name": "会话ID", "operator": "is", "value": [chat_id]}]
            },
            "sort": [{"field_name": "创建时间", "order": "DESC"}],
            "page_size": 1,
        }
    )
    items = r.json().get("data", {}).get("items", [])
    return items[0].get("fields", {}) if items else {}

def split_extra_output(text):
    """Split text at --- separator, returns (main_text, extra_text)."""
    idx = text.find('\n---')
    if idx == -1:
        return text, ""
    return text[:idx].strip(), text[idx:]

def parse_extra_output(text):
    """从 AI 输出的 --- 分隔线后，解析内部标题和标签字段"""
    result = {}
    parts = text.rsplit('---', 1)
    if len(parts) < 2:
        return result
    extra = parts[1]

    title_match = re.search(r'\*\*内部标题\*\*[^\n]*\n((?:\s*-.+\n?)+)', extra)
    if title_match:
        titles = re.findall(r'^\s*-\s*(.+)', title_match.group(1), re.MULTILINE)
        cleaned = [t.strip() for t in titles if t.strip()]
        if cleaned:
            result['内部标题'] = '\n'.join(cleaned[:3])

    for field in ['农作物', '内容类型', '农事作业', '具体问题']:
        m = re.search(rf'{field}[：:]\s*(.+)', extra)
        if m and m.group(1).strip():
            result[field] = m.group(1).strip()

    return result

def save_topics(topics, audience, content_type, session_id):
    """把 3 个选题写入多维表格，返回 [record_id, ...]"""
    record_ids = []
    for label, content in topics:
        rid = bitable_create(BITABLE_TOPIC_TABLE, {
            "选题标签": label,
            "选题内容": content,
            "受众": "种植户" if audience == "farmer" else "经销商",
            "内容类型": content_type,
            "会话ID": session_id,
        })
        record_ids.append(rid)
    return record_ids

def get_topic_content(record_id, cache_key, topic_idx):
    """优先从 Bitable 读，降级到内存缓存"""
    if USE_BITABLE and record_id:
        fields = bitable_get(BITABLE_TOPIC_TABLE, record_id)
        content = fields.get("选题内容", "")
        if content:
            return content
    # 降级：内存缓存
    topics = cache_get(cache_key) if cache_key else None
    if topics and topic_idx < len(topics):
        return topics[topic_idx][1]
    return None


# ── 卡片模板 ─────────────────────────────────────────────────
def card_main_menu():
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**请选择功能：**"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✏️ 改文案"},
                 "type": "primary", "value": {"action": "rewrite"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "💡 头脑风暴"},
                 "type": "default", "value": {"action": "brainstorm"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "🔍 完善选题"},
                 "type": "default", "value": {"action": "develop"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "📢 产品推广"},
                 "type": "default", "value": {"action": "product_promo"}},
            ]}
        ]
    }

def card_audience_select(flow):
    titles = {"brainstorm": "头脑风暴 · 选择目标受众", "develop": "完善选题 · 选择目标受众", "rewrite": "改文案 · 选择目标受众"}
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{titles.get(flow, '请选择目标受众')}**"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "🌾 面向种植户"},
                 "type": "primary",
                 "value": {"action": f"{flow}_audience", "audience": "farmer"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "🏪 面向经销商"},
                 "type": "default",
                 "value": {"action": f"{flow}_audience", "audience": "dealer"}},
            ]}
        ]
    }

def card_farmer_crop_select():
    def btn(label, crop):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default", "value": {"action": "develop_farmer_crop", "crop": crop}}
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**完善选题 · 选择作物**"}},
            {"tag": "action", "actions": [btn("🌾 小麦", "小麦"), btn("🌽 玉米", "玉米"), btn("🌾 水稻", "水稻")]},
            {"tag": "action", "actions": [btn("🥜 花生", "花生"), btn("🫘 棉花", "棉花"), btn("🥦 果蔬", "果蔬")]},
        ]
    }

def card_farmer_scale_select(crop):
    def btn(label, scale):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default", "value": {"action": "develop_farmer_scale", "crop": crop, "scale": scale}}
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**完善选题 · {crop} · 选择规模**"}},
            {"tag": "action", "actions": [
                btn("兼业小户", "兼业小户"), btn("专业种植户", "专业种植户"), btn("规模经营者", "规模经营者")
            ]},
        ]
    }

def card_farmer_identity_select(crop, scale):
    def btn(label, identity):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {"action": "develop_farmer_identity", "crop": crop, "scale": scale, "identity": identity}}
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**完善选题 · {crop} · {scale} · 发布者身份**"}},
            {"tag": "action", "actions": [btn("业务员", "业务员"), btn("经销商", "经销商"), btn("技术员", "技术员")]},
        ]
    }

def card_farmer_intent_select(crop, scale, identity):
    def btn(label, intent):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {"action": "develop_farmer_intent",
                          "crop": crop, "scale": scale, "identity": identity, "intent": intent}}
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**完善选题 · {crop} · {scale} · {identity} · 内容意图**"}},
            {"tag": "action", "actions": [btn("立人设 / 建信任", "立人设"), btn("推产品 / 转化", "推产品")]},
        ]
    }

def card_content_types(audience):
    if audience == "farmer":
        types = ["产品推广", "解决方案", "观点/吐槽", "案例故事", "知识科普"]
    else:
        types = ["模式介绍", "合作案例", "经销商干货", "产品实证", "行业观点"]
    row1, row2 = types[:3], types[3:]
    def btn(t):
        return {"tag": "button", "text": {"tag": "plain_text", "content": t},
                "type": "default",
                "value": {"action": "brainstorm_type", "audience": audience, "type": t}}
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**选择内容类型：**"}},
        {"tag": "action", "actions": [btn(t) for t in row1]},
    ]
    if row2:
        elements.append({"tag": "action", "actions": [btn(t) for t in row2]})
    return {"config": {"wide_screen_mode": True}, "elements": elements}

def card_loading(title):
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"⏳ **{title}**\n\n正在生成，请稍候..."}}
        ]
    }

def card_result(title, content):
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
        ]
    }

def card_template_prompt(title, template):
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**{title}**\n\n请按以下格式回复：\n\n```\n{template}\n```"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "取消"},
                 "type": "danger", "value": {"action": "cancel"}},
            ]}
        ]
    }

def card_brainstorm_result(header, full_text, topics, cache_key, audience, record_ids):
    """头脑风暴结果，带深化按钮。record_ids 可为空列表（降级到缓存模式）"""
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{header}**"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": full_text}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**选一个深化：**"}},
    ]
    buttons = []
    for i, (label, _) in enumerate(topics):
        val = {
            "action": "deepen",
            "audience": audience,
            "cache_key": cache_key,
            "topic_idx": i,
            "record_id": record_ids[i] if i < len(record_ids) else "",
        }
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"深化：{label}"},
            "type": "default",
            "value": val,
        })
    elements.append({"tag": "action", "actions": buttons})
    return {"config": {"wide_screen_mode": True}, "elements": elements}


# ── 产品推广卡片 ──────────────────────────────────────────────
def card_product_select():
    buttons = []
    for pid, p in PRODUCTS.items():
        emoji = p.get("emoji", "📦")
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{emoji} {p['name']}"},
            "type": "default",
            "value": {"action": "product_promo_product", "product_id": pid}
        })
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**产品推广 · 选择产品**"}},
            {"tag": "action", "actions": buttons},
        ]
    }

def card_product_crop_select(product_id):
    p = PRODUCTS.get(product_id)
    if not p:
        return card_result("出错了", "产品不存在")
    crops = p.get("crops", [])
    def btn(crop):
        emoji = CROP_EMOJI.get(crop, "🌱")
        return {"tag": "button", "text": {"tag": "plain_text", "content": f"{emoji} {crop}"},
                "type": "default", "value": {"action": "product_promo_crop", "product_id": product_id, "crop": crop}}
    # 每行3个按钮
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": f"**产品推广 · {p['name']} · 选择作物**"}}]
    for i in range(0, len(crops), 3):
        elements.append({"tag": "action", "actions": [btn(c) for c in crops[i:i+3]]})
    return {"config": {"wide_screen_mode": True}, "elements": elements}

def card_product_audience_select(product_id, crop):
    p = PRODUCTS.get(product_id, {})
    def btn(label, audience):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {"action": "product_promo_audience",
                          "product_id": product_id, "crop": crop, "audience": audience}}
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**产品推广 · {p.get('name', '')} · {crop} · 选择受众**"}},
            {"tag": "action", "actions": [
                btn("🌾 种植大户", "farmer"), btn("🏪 经销商", "dealer")
            ]},
        ]
    }

def card_product_identity_select(product_id, crop, audience):
    p = PRODUCTS.get(product_id, {})
    def btn(label, identity):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {"action": "product_promo_identity",
                          "product_id": product_id, "crop": crop,
                          "audience": audience, "identity": identity}}
    label = "种植大户" if audience == "farmer" else "经销商"
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**产品推广 · {p.get('name', '')} · {crop} · {label} · 发布者身份**"}},
            {"tag": "action", "actions": [btn("业务员", "业务员"), btn("经销商", "经销商"), btn("技术员", "技术员")]},
        ]
    }

def card_product_angle_select(product_id, crop, audience, identity):
    p = PRODUCTS.get(product_id, {})
    def btn(text, angle):
        return {"tag": "button", "text": {"tag": "plain_text", "content": text},
                "type": "default",
                "value": {"action": "product_promo_angle",
                          "product_id": product_id, "crop": crop,
                          "audience": audience, "identity": identity, "angle": angle}}
    label = "种植大户" if audience == "farmer" else "经销商"
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**产品推广 · {p.get('name', '')} · {crop} · {label} · {identity} · 选择切入角度**"}},
            {"tag": "action", "actions": [
                btn("🎯 痛点", "痛点"), btn("✨ 痒点", "痒点"), btn("📖 故事", "故事")
            ]},
        ]
    }

def _get_current_seasonal_tip():
    """根据当前月份返回 seasonal_tips 中的提示"""
    month = time.localtime().tm_mon
    month_map = {
        4: "4-5月", 5: "4-5月",
        6: "6-7月", 7: "6-7月",
        8: "8-9月", 9: "8-9月",
        10: "10-11月", 11: "10-11月",
    }
    return month_map.get(month, "")

def card_product_input_prompt(product_id, crop, audience, identity, angle):
    p = PRODUCTS.get(product_id)
    if not p:
        return card_result("出错了", "产品不存在")
    label = "种植大户" if audience == "farmer" else "经销商"
    # 构建预填的产品信息
    functions_str = "\n".join([f"• {f}" for f in p.get("functions", [])])
    seasonal_tip = ""
    tips = p.get("seasonal_tips", {})
    current_period = _get_current_seasonal_tip()
    if current_period and current_period in tips:
        seasonal_tip = f"\n当前推广重点：{tips[current_period]}"
    prefilled = (
        f"产品：{p['full_name']}\n"
        f"研发背景：{p['research']}\n"
        f"核心成分：{p['ingredients']}\n"
        f"配方：{p['formula']}\n"
        f"主要功能：\n{functions_str}\n"
        f"使用方法：{p['usage']}\n"
        f"{seasonal_tip}\n"
        f"\n"
        f"已选参数：\n"
        f"• 作物：{crop}\n"
        f"• 目标受众：{label}\n"
        f"• 发布者身份：{identity}\n"
        f"• 切入点：{angle}\n"
        f"\n"
        f"请补充以下信息（越具体效果越好）：\n"
        f"推广场景（如：春季追肥/药害急救/抗旱保收）：\n"
        f"想强调的卖点（可选，默认用产品核心卖点）：\n"
        f"地区（可选）：\n"
        f"补充信息（可选）："
    )
    title = f"产品推广 · {p['name']} · {crop} · {label} · {identity} · {angle}"
    return card_template_prompt(title, prefilled)


# ── 后台任务 ─────────────────────────────────────────────────
def do_rewrite_send(chat_id, text, audience="farmer"):
    try:
        skill = SKILL_VIDEO_REWRITE_FARM if audience == "farmer" else SKILL_VIDEO_REWRITE_DEAL
        draft = ai_call(skill, text)
        draft_main, draft_extra = split_extra_output(draft)
        humanizer_input = (
            "注意：这是短视频脚本，第一句是刻意设计的钩子，"
            "去AI味时保留其直接性和冲击力，不要改成平淡的开场白。"
            "严格控制在400字以内。\n\n"
            + draft_main
        )
        result = ai_call(SKILL_HUMANIZER, humanizer_input) + draft_extra

        if USE_BITABLE and BITABLE_REWRITE_TABLE:
            try:
                extra = parse_extra_output(result)
                fields = {"原始文案": text, "改写结果": result}
                fields.update(extra)
                bitable_create(BITABLE_REWRITE_TABLE, fields)
            except Exception as e:
                print(f"[bitable] rewrite save failed: {e}", flush=True)

        send_card(chat_id, card_result("改后文案", result))
    except Exception as e:
        send_card(chat_id, card_result("出错了", str(e)))

def do_brainstorm_send(chat_id, audience, content_type, user_input):
    try:
        skill  = SKILL_BRAINSTORM_FARM if audience == "farmer" else SKILL_BRAINSTORM_DEAL
        prompt = f"内容类型：{content_type}\n\n{user_input}"
        result = ai_call(skill, prompt)
        label  = "种植户" if audience == "farmer" else "经销商"
        header = f"{content_type} · 选题方案（面向{label}）"

        topics    = parse_brainstorm(result)
        cache_key = make_cache_key(chat_id)
        cache_set(cache_key, topics)

        record_ids = []
        if USE_BITABLE and topics:
            try:
                record_ids = save_topics(topics, audience, content_type, cache_key)
            except Exception as e:
                print(f"[bitable] save_topics failed: {e}", flush=True)

        if topics:
            send_card(chat_id, card_brainstorm_result(
                header, result, topics, cache_key, audience, record_ids
            ))
        else:
            send_card(chat_id, card_result(header, result))
    except Exception as e:
        send_card(chat_id, card_result("出错了", str(e)))

def do_script_send(chat_id, audience, user_input, topic_record_id="", table_id=None,
                   crop="", scale="", identity="", intent=""):
    try:
        if table_id is None:
            table_id = BITABLE_DEVELOP_TABLE
        label = "种植户" if audience == "farmer" else "经销商"
        skill = SKILL_SCRIPT_FARM if audience == "farmer" else SKILL_SCRIPT_DEAL

        content_for_script = user_input
        if audience == "farmer" and any([crop, scale, identity, intent]):
            context = f"作物：{crop}\n规模：{scale}\n发布者身份：{identity}\n内容意图：{intent}\n\n"
            content_for_script = context + user_input

        draft = ai_call(skill, content_for_script)
        draft_main, draft_extra = split_extra_output(draft)
        humanizer_input = (
            "注意：这是短视频脚本，第一句是刻意设计的钩子，"
            "去AI味时保留其直接性和冲击力，不要改成平淡的开场白。"
            "严格控制在400字以内。\n\n"
            + draft_main
        )
        result = ai_call(SKILL_HUMANIZER, humanizer_input) + draft_extra

        record_id = topic_record_id
        if USE_BITABLE and table_id:
            try:
                extra = parse_extra_output(result)
                if record_id:
                    fields = {"最终脚本": result}
                    fields.update(extra)
                    bitable_update(table_id, record_id, fields)
                else:
                    fields = {"最终脚本": result, "受众": label}
                    fields.update(extra)
                    record_id = bitable_create(table_id, fields)
            except Exception as e:
                print(f"[bitable] script save failed: {e}", flush=True)

        send_card(chat_id, card_result(f"脚本（面向{label}）", result))
    except Exception as e:
        send_card(chat_id, card_result("出错了", str(e)))

def do_product_script_send(chat_id, product_id, crop, audience, identity, angle, user_input):
    """产品推广脚本生成：按角度选 skill，拼接产品上下文后调用 AI"""
    try:
        p = PRODUCTS.get(product_id)
        if not p:
            send_card(chat_id, card_result("出错了", "产品不存在"))
            return

        # 1. 选择 skill
        skill_map = {"痛点": SKILL_PRODUCT_PAIN, "痒点": SKILL_PRODUCT_ITCH, "故事": SKILL_PRODUCT_STORY}
        skill = skill_map.get(angle, SKILL_PRODUCT_PAIN)

        # 2. 构建产品上下文
        label = "种植大户" if audience == "farmer" else "经销商"
        functions_str = "、".join(p.get("functions", []))
        context_lines = [
            f"产品：{p['full_name']}",
            f"研发背景：{p['research']}",
            f"核心成分：{p['ingredients']}",
            f"配方：{p['formula']}",
            f"功效：{functions_str}",
            f"用法：{p['usage']}",
        ]
        # 效果实证（有数据时加入）
        proof_points = p.get("proof_points", [])
        if proof_points:
            context_lines.append(f"效果实证：{'；'.join(proof_points)}")
        # 亩均成本（有数据时加入）
        cost = p.get("cost_per_mu", "")
        if cost:
            context_lines.append(f"亩均成本：{cost}")
        # 时令提示（有匹配时加入）
        tips = p.get("seasonal_tips", {})
        current_period = _get_current_seasonal_tip()
        if current_period and current_period in tips:
            context_lines.append(f"当前推广重点：{tips[current_period]}")
        # 当前运营困境与策略（有关键信息时加入）
        ctx = p.get("current_context", {})
        if ctx:
            context_lines.append("")
            context_lines.append("=== 当前运营背景（必须严格遵守）===")
            if ctx.get("timing"):
                context_lines.append(f"时间节点：{ctx['timing']}")
            if ctx.get("situation"):
                context_lines.append(f"当前困境：{ctx['situation']}")
            if ctx.get("next_window"):
                context_lines.append(f"下一窗口：{ctx['next_window']}")
            if ctx.get("strategy"):
                context_lines.append(f"内容策略：{ctx['strategy']}")
            assets = ctx.get("assets", [])
            if assets:
                context_lines.append(f"可用素材：{'；'.join(assets)}")
            guidance = ctx.get("content_guidance", {})
            if audience in guidance:
                context_lines.append(f"写作要求：{guidance[audience]}")
            context_lines.append("=================================")

        context_lines.extend([
            "",
            f"作物：{crop}",
            f"受众：{label}",
            f"发布者身份：{identity}",
            f"切入角度：{angle}",
            "",
            f"用户补充：{user_input}",
        ])
        context = "\n".join(context_lines)

        # 3. 第一趟：skill 生成脚本
        draft = ai_call(skill, context)
        draft_main, draft_extra = split_extra_output(draft)

        # 4. 第二趟：humanizer 去 AI 味
        humanizer_input = (
            "注意：这是短视频脚本，第一句是刻意设计的钩子，"
            "去AI味时保留其直接性和冲击力，不要改成平淡的开场白。"
            "严格控制在400字以内。\n\n"
            + draft_main
        )
        result = ai_call(SKILL_HUMANIZER, humanizer_input) + draft_extra

        # 5. 保存到 Bitable（复用 BITABLE_DEVELOP_TABLE，加流程字段区分）
        if USE_BITABLE and BITABLE_DEVELOP_TABLE:
            try:
                extra = parse_extra_output(result)
                fields = {
                    "最终脚本": result,
                    "受众": label,
                    "流程": "产品推广",
                    "产品": p["name"],
                    "切入角度": angle,
                }
                fields.update(extra)
                bitable_create(BITABLE_DEVELOP_TABLE, fields)
            except Exception as e:
                print(f"[bitable] product script save failed: {e}", flush=True)

        # 6. 发送结果
        send_card(chat_id, card_result(f"产品推广脚本 · {p['name']} · {angle}（面向{label}）", result))
    except Exception as e:
        send_card(chat_id, card_result("出错了", str(e)))

# ── 去重（LRU 淘汰，避免全量 clear 导致重复处理）──────────────
_DEDUP_MAX = 500
processed_events = OrderedDict()      # event_id -> True

# ── 路由 ─────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "ok"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    msg   = event.get("message", {})

    event_id = data.get("header", {}).get("event_id", "")
    if event_id in processed_events:
        return jsonify({"code": 0})
    processed_events[event_id] = True
    while len(processed_events) > _DEDUP_MAX:
        processed_events.popitem(last=False)

    if msg.get("message_type") != "text":
        return jsonify({"code": 0})

    chat_id = msg.get("chat_id", "")
    content = json.loads(msg.get("content", "{}"))
    text    = content.get("text", "").strip()

    state = pending_states.get(chat_id)
    if state and time.time() < state["expires"]:
        flow         = state["flow"]
        audience     = state.get("audience")
        content_type = state.get("content_type")
        del pending_states[chat_id]

        if flow == "rewrite":
            send_card(chat_id, card_loading("正在改文案..."))
            threading.Thread(target=do_rewrite_send, args=(chat_id, text, audience or "farmer")).start()
        elif flow == "brainstorm":
            send_card(chat_id, card_loading(f"正在生成「{content_type}」选题..."))
            threading.Thread(
                target=do_brainstorm_send,
                args=(chat_id, audience, content_type, text)
            ).start()
        elif flow == "develop":
            crop     = state.get("crop", "")
            scale    = state.get("scale", "")
            identity = state.get("identity", "")
            intent   = state.get("intent", "")
            send_card(chat_id, card_loading("正在生成脚本..."))
            threading.Thread(
                target=do_script_send,
                args=(chat_id, audience, text, "", None, crop, scale, identity, intent)
            ).start()
        elif flow == "product_promo":
            product_id = state.get("product_id", "")
            crop       = state.get("crop", "")
            identity   = state.get("identity", "")
            angle      = state.get("angle", "痛点")
            send_card(chat_id, card_loading("正在生成产品推广脚本..."))
            threading.Thread(
                target=do_product_script_send,
                args=(chat_id, product_id, crop, audience, identity, angle, text)
            ).start()

        return jsonify({"code": 0})

    if text == "文案":
        send_card(chat_id, card_main_menu())
    return jsonify({"code": 0})


def handle_card_action(action, chat_id):
    """处理卡片动作，返回下一步的卡片（供 /card 回调直接返回给飞书）。
    返回 None 表示不更新卡片。"""
    act = action.get("action")
    print(f"[card] act={act} chat_id={chat_id} action={action}", flush=True)

    if act == "cancel":
        pending_states.pop(chat_id, None)
        return card_main_menu()

    if act == "rewrite":
        return card_audience_select("rewrite")

    if act == "brainstorm":
        return card_audience_select("brainstorm")

    if act == "develop":
        return card_audience_select("develop")

    if act == "brainstorm_audience":
        return card_content_types(action.get("audience"))

    if act == "develop_audience":
        audience = action.get("audience")
        if audience == "farmer":
            return card_farmer_crop_select()
        pending_states[chat_id] = {
            "flow": "develop", "audience": "dealer",
            "expires": time.time() + STATE_TTL
        }
        return card_template_prompt("完善选题 · 面向经销商", TEMPLATE_DEVELOP_DEAL)

    if act == "develop_farmer_crop":
        return card_farmer_scale_select(action.get("crop", ""))

    if act == "develop_farmer_scale":
        return card_farmer_identity_select(action.get("crop", ""), action.get("scale", ""))

    if act == "develop_farmer_identity":
        return card_farmer_intent_select(
            action.get("crop", ""), action.get("scale", ""), action.get("identity", ""))

    if act == "develop_farmer_intent":
        crop     = action.get("crop", "")
        scale    = action.get("scale", "")
        identity = action.get("identity", "")
        intent   = action.get("intent", "")
        template = TEMPLATE_DEVELOP_FARMER_PRODUCT if intent == "推产品" else TEMPLATE_DEVELOP_FARMER_BRAND
        pending_states[chat_id] = {
            "flow": "develop", "audience": "farmer",
            "crop": crop, "scale": scale, "identity": identity, "intent": intent,
            "expires": time.time() + STATE_TTL
        }
        label = f"{crop} · {scale} · {identity} · {intent}"
        return card_template_prompt(f"完善选题 · {label}", template)

    if act == "rewrite_audience":
        audience = action.get("audience")
        label    = "种植户" if audience == "farmer" else "经销商"
        pending_states[chat_id] = {
            "flow": "rewrite", "audience": audience,
            "expires": time.time() + STATE_TTL
        }
        return card_template_prompt(f"改文案 · 面向{label}", "（直接把要改的文案发过来）")

    if act == "brainstorm_type":
        audience     = action.get("audience")
        content_type = action.get("type")
        templates    = TEMPLATES_FARM if audience == "farmer" else TEMPLATES_DEAL
        template     = templates.get(content_type, "")
        label        = "种植户" if audience == "farmer" else "经销商"
        pending_states[chat_id] = {
            "flow": "brainstorm", "audience": audience,
            "content_type": content_type, "expires": time.time() + STATE_TTL
        }
        return card_template_prompt(f"{content_type} · 面向{label}", template)

    if act == "deepen":
        record_id = action.get("record_id", "")
        cache_key = action.get("cache_key", "")
        topic_idx = action.get("topic_idx", 0)
        audience  = action.get("audience", "farmer")

        content = get_topic_content(record_id, cache_key, topic_idx)
        if not content:
            return card_result("已过期", "请重新发起头脑风暴")

        # 深化需要后台 AI 生成，先返回 loading 卡片，后台线程完成后发送新卡片
        threading.Thread(
            target=do_script_send,
            args=(chat_id, audience, content, record_id, BITABLE_TOPIC_TABLE)
        ).start()
        return card_loading("正在生成脚本...")

    if act == "product_promo":
        return card_product_select()

    if act == "product_promo_product":
        return card_product_crop_select(action.get("product_id"))

    if act == "product_promo_crop":
        return card_product_audience_select(
            action.get("product_id"), action.get("crop"))

    if act == "product_promo_audience":
        return card_product_identity_select(
            action.get("product_id"), action.get("crop"), action.get("audience"))

    if act == "product_promo_identity":
        return card_product_angle_select(
            action.get("product_id"), action.get("crop"),
            action.get("audience"), action.get("identity"))

    if act == "product_promo_angle":
        product_id = action.get("product_id")
        crop       = action.get("crop")
        audience   = action.get("audience")
        identity   = action.get("identity")
        angle      = action.get("angle")
        pending_states[chat_id] = {
            "flow": "product_promo",
            "product_id": product_id,
            "crop": crop,
            "audience": audience,
            "identity": identity,
            "angle": angle,
            "expires": time.time() + STATE_TTL,
        }
        return card_product_input_prompt(product_id, crop, audience, identity, angle)

    return None


@app.route("/card", methods=["POST"])
def card_action():
    data = request.json or {}

    if "challenge" in data and "action" not in data:
        return jsonify({"challenge": data["challenge"]})

    action  = data.get("action", {}).get("value", {})
    chat_id = data.get("open_chat_id", "")
    print(f"[card] action={action} chat_id={chat_id}", flush=True)

    # 同步处理卡片动作，直接返回新卡片给飞书（比异步 PATCH 更可靠）
    card = handle_card_action(action, chat_id)
    if card:
        return jsonify(card)
    return jsonify({})


if __name__ == "__main__":
    app.run(port=5000)
