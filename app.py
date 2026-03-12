import os
import re
import json
import threading
import time
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

SKILL_HUMANIZER        = load_skill("humanizer-zh")
SKILL_VIDEO_REWRITE    = load_skill("video-rewrite")
SKILL_BRAINSTORM_FARM  = load_skill("brainstorm-topics")
SKILL_BRAINSTORM_DEAL  = load_skill("brainstorm-dealers")
SKILL_DEVELOP          = load_skill("develop-topic")

# ── 飞书多维表格配置 ─────────────────────────────────────────
BITABLE_APP_TOKEN    = os.environ.get("BITABLE_APP_TOKEN", "")
BITABLE_TOPIC_TABLE  = os.environ.get("BITABLE_TOPIC_TABLE_ID", "")
BITABLE_DEVELOP_TABLE = os.environ.get("BITABLE_DEVELOP_TABLE_ID", "")
USE_BITABLE = bool(BITABLE_APP_TOKEN and BITABLE_TOPIC_TABLE and BITABLE_DEVELOP_TABLE)

# ── 会话状态 ─────────────────────────��───────────────────────
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

TEMPLATE_DEVELOP_FARM = "话题方向：\n已有想法（没有就写「无」）：\n背景信息（产品/场景/地区，可选）："
TEMPLATE_DEVELOP_DEAL = "话题方向：\n已有想法（没有就写「无」）：\n地区（可选，默认江浙沪皖豫）："

# ── 解析头脑风暴输出 ─────────────────────────────────────────
def parse_brainstorm(text):
    blocks = re.split(r'【选题\d+】', text)
    topics = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        label_match = re.search(r'按钮标签[：:]\s*(.+)', block)
        label = label_match.group(1).strip() if label_match else block[:12]
        topics.append((label, block))
    return topics[:3]

# ── 飞书基础 API ─────────────────────────────────────────────
def get_tenant_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    )
    return r.json()["tenant_access_token"]

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
    token = get_tenant_token()
    requests.patch(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"msg_type": "interactive", "content": json.dumps(card)}
    )

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

def bitable_get(table_id, record_id):
    """读取一条记录的 fields"""
    token = get_tenant_token()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"}
    )
    return r.json().get("data", {}).get("record", {}).get("fields", {})

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

def save_develop(content):
    """把完善结果写入多维表格，返回 record_id"""
    return bitable_create(BITABLE_DEVELOP_TABLE, {"完善内容": content})

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

def get_develop_content(record_id, cache_key):
    """优先从 Bitable 读，降级到内存缓存"""
    if USE_BITABLE and record_id:
        fields = bitable_get(BITABLE_DEVELOP_TABLE, record_id)
        content = fields.get("完善内容", "")
        if content:
            return content
    return cache_get(cache_key) if cache_key else None

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
            ]}
        ]
    }

def card_audience_select(flow):
    titles = {"brainstorm": "头脑风暴 · 选择目标受众", "develop": "完善选题 · 选择目标受众"}
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

def card_develop_result(header, content, cache_key, record_id):
    """完善选题结果，带生成脚本按钮"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{header}**"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "✏️ 生成脚本"},
                 "type": "primary",
                 "value": {"action": "generate_script",
                           "cache_key": cache_key,
                           "record_id": record_id}}
            ]}
        ]
    }

# ── 后台任务 ─────────────────────────────────────────────────
def do_rewrite_send(chat_id, text):
    try:
        draft = ai_call(SKILL_VIDEO_REWRITE, text)
        humanizer_input = (
            "注意：这是短视频脚本，第一句是刻意设计的钩子，"
            "去AI味时保留其直接性和冲击力，不要改成平淡的开场白。\n\n"
            + draft
        )
        result = ai_call(SKILL_HUMANIZER, humanizer_input)
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

def do_develop_send(chat_id, audience, user_input):
    try:
        label  = "种植户" if audience == "farmer" else "经销商"
        prompt = f"受众：面向{label}\n\n{user_input}"
        result = ai_call(SKILL_DEVELOP, prompt)
        header = f"完善选题（面向{label}）"

        cache_key = make_cache_key(chat_id)
        cache_set(cache_key, result)

        record_id = ""
        if USE_BITABLE:
            try:
                record_id = save_develop(result)
            except Exception as e:
                print(f"[bitable] save_develop failed: {e}", flush=True)

        send_card(chat_id, card_develop_result(header, result, cache_key, record_id))
    except Exception as e:
        send_card(chat_id, card_result("出错了", str(e)))

def do_generate_script(chat_id, develop_content):
    try:
        draft = ai_call(SKILL_VIDEO_REWRITE, develop_content)
        humanizer_input = (
            "注意：这是短视频脚本，第一句是刻意设计的钩子，"
            "去AI味时保留其直接性和冲击力，不要改成平淡的开场白。\n\n"
            + draft
        )
        result = ai_call(SKILL_HUMANIZER, humanizer_input)
        send_card(chat_id, card_result("生成脚本", result))
    except Exception as e:
        send_card(chat_id, card_result("出错了", str(e)))

# ── 去重 ─────────────────────────────────────────────────────
processed_events = set()

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
    processed_events.add(event_id)
    if len(processed_events) > 1000:
        processed_events.clear()

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
            threading.Thread(target=do_rewrite_send, args=(chat_id, text)).start()
        elif flow == "brainstorm":
            send_card(chat_id, card_loading(f"正在生成「{content_type}」选题..."))
            threading.Thread(
                target=do_brainstorm_send,
                args=(chat_id, audience, content_type, text)
            ).start()
        elif flow == "develop":
            send_card(chat_id, card_loading("正在完善选题..."))
            threading.Thread(
                target=do_develop_send,
                args=(chat_id, audience, text)
            ).start()

        return jsonify({"code": 0})

    send_card(chat_id, card_main_menu())
    return jsonify({"code": 0})


def handle_card_action(action, chat_id, msg_id):
    act = action.get("action")

    if act == "rewrite":
        pending_states[chat_id] = {"flow": "rewrite", "expires": time.time() + STATE_TTL}
        update_card(msg_id, card_template_prompt("改文案", "（直接把要改的文案发过来）"))

    elif act == "brainstorm":
        update_card(msg_id, card_audience_select("brainstorm"))

    elif act == "develop":
        update_card(msg_id, card_audience_select("develop"))

    elif act == "brainstorm_audience":
        audience = action.get("audience")
        update_card(msg_id, card_content_types(audience))

    elif act == "develop_audience":
        audience = action.get("audience")
        template = TEMPLATE_DEVELOP_FARM if audience == "farmer" else TEMPLATE_DEVELOP_DEAL
        label    = "种植户" if audience == "farmer" else "经销商"
        pending_states[chat_id] = {
            "flow": "develop", "audience": audience,
            "expires": time.time() + STATE_TTL
        }
        update_card(msg_id, card_template_prompt(f"完善选题 · 面向{label}", template))

    elif act == "brainstorm_type":
        audience     = action.get("audience")
        content_type = action.get("type")
        templates    = TEMPLATES_FARM if audience == "farmer" else TEMPLATES_DEAL
        template     = templates.get(content_type, "")
        label        = "种植户" if audience == "farmer" else "经销商"
        pending_states[chat_id] = {
            "flow": "brainstorm", "audience": audience,
            "content_type": content_type, "expires": time.time() + STATE_TTL
        }
        update_card(msg_id, card_template_prompt(f"{content_type} · 面向{label}", template))

    elif act == "deepen":
        record_id = action.get("record_id", "")
        cache_key = action.get("cache_key", "")
        topic_idx = action.get("topic_idx", 0)
        audience  = action.get("audience", "farmer")

        content = get_topic_content(record_id, cache_key, topic_idx)
        if not content:
            update_card(msg_id, card_result("已过期", "请重新发起头脑风暴"))
            return

        update_card(msg_id, card_loading("正在完善选题..."))
        threading.Thread(target=do_develop_send, args=(chat_id, audience, content)).start()

    elif act == "generate_script":
        record_id = action.get("record_id", "")
        cache_key = action.get("cache_key", "")

        content = get_develop_content(record_id, cache_key)
        if not content:
            update_card(msg_id, card_result("已过期", "请重新完善选题"))
            return

        update_card(msg_id, card_loading("正在生成脚本..."))
        threading.Thread(target=do_generate_script, args=(chat_id, content)).start()


@app.route("/card", methods=["POST"])
def card_action():
    data = request.json or {}

    if "challenge" in data and "action" not in data:
        return jsonify({"challenge": data["challenge"]})

    action  = data.get("action", {}).get("value", {})
    chat_id = data.get("open_chat_id", "")
    msg_id  = data.get("open_message_id", "")

    threading.Thread(target=handle_card_action, args=(action, chat_id, msg_id)).start()
    return jsonify({"code": 0})


if __name__ == "__main__":
    app.run(port=5000)
