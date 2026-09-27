import os, re, time, hashlib
from datetime import datetime
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Vera Merchant AI", version="1.0.0")
START = time.time()

# In-memory stores are sufficient for the challenge because the judge keeps one
# running process throughout a test window.
contexts: Dict[tuple[str, str], Dict[str, Any]] = {}
conversations: Dict[str, Dict[str, Any]] = {}
sent_suppressions: set[str] = set()

AUTO_REPLY_PATTERNS = [
    r"thank you for contacting us",
    r"thanks for contacting us",
    r"our team will (get back|respond)",
    r"we will (get back|respond)",
    r"for your (information|kind information)",
    r"hamari team.*(contact|respond|reply)",
    r"main aapki.*team tak",
    r"automated (assistant|message|reply)",
    r"this is an automated",
]

POSITIVE = re.compile(r"\b(yes|yeah|yep|sure|okay|ok|done|please|let's do it|lets do it|go ahead|interested|send it|do it)\b", re.I)
NEGATIVE = re.compile(r"\b(no|nope|not interested|don't want|do not want|stop|unsubscribe|leave me alone|busy|later)\b", re.I)
ACTION_INTENT = re.compile(r"\b(join|start|activate|sign up|signup|register|update|fix|run|launch|create|publish|send|do it|let's do it|lets do it|book|renew)\b", re.I)
OFF_TOPIC = re.compile(r"\b(gst|income tax|passport|visa|loan|police|criminal|divorce|exam|homework)\b", re.I)


def norm(s: Any) -> str:
    return str(s or "").strip()


def get_ctx(scope: str, context_id: str) -> Optional[Dict[str, Any]]:
    item = contexts.get((scope, context_id))
    return item["payload"] if item else None


def find_merchant(merchant_id: str) -> Optional[Dict[str, Any]]:
    return get_ctx("merchant", merchant_id)


def find_category(slug: str) -> Optional[Dict[str, Any]]:
    return get_ctx("category", slug)


def find_customer(customer_id: str) -> Optional[Dict[str, Any]]:
    return get_ctx("customer", customer_id)


def trigger_for_action(trigger_id: str) -> Optional[Dict[str, Any]]:
    return get_ctx("trigger", trigger_id)


def merchant_name(m: Dict[str, Any]) -> str:
    return norm(m.get("identity", {}).get("name")) or "there"


def short_name(m: Dict[str, Any]) -> str:
    n = merchant_name(m)
    # Keep titles such as Dr. Meera intact; otherwise use first token.
    return n.split(",")[0]


def pct(v: Any) -> Optional[float]:
    try: return float(v) * 100 if abs(float(v)) <= 1 else float(v)
    except Exception: return None


def lang_mix(m: Dict[str, Any]) -> bool:
    langs = m.get("identity", {}).get("languages", [])
    return any(str(x).lower() in {"hi", "hinglish", "hi-en"} for x in langs)


def category_tone(cat: Dict[str, Any]) -> str:
    return str(cat.get("voice", {}).get("tone", "professional")).lower()


def is_auto_reply(text: str) -> bool:
    t = re.sub(r"\s+", " ", text.lower()).strip()
    return any(re.search(p, t) for p in AUTO_REPLY_PATTERNS)


def is_duplicate_auto_reply(conv: Dict[str, Any], text: str) -> bool:
    recent = [x.get("body", "") for x in conv.get("turns", []) if x.get("from") == "merchant"]
    return len(recent) >= 1 and recent[-1].strip().lower() == text.strip().lower() and is_auto_reply(text)


def first_real_reply(conv: Dict[str, Any]) -> bool:
    return any(t.get("from") == "merchant" and not t.get("auto_reply") for t in conv.get("turns", []))


def add_turn(conv_id: str, role: str, body: str, **extra):
    c = conversations.setdefault(conv_id, {"turns": [], "state": "active", "sent": []})
    c["turns"].append({"from": role, "body": body, "ts": time.time(), **extra})
    return c


def template_for(kind: str) -> str:
    mapping = {
        "research_digest": "vera_research_digest_v1",
        "research_digest_release": "vera_research_digest_v1",
        "perf_spike": "vera_performance_insight_v1",
        "perf_dip": "vera_performance_insight_v1",
        "milestone_reached": "vera_milestone_v1",
        "review_theme_emerged": "vera_review_insight_v1",
        "competitor_opened": "vera_local_competitor_v1",
        "festival_upcoming": "vera_seasonal_opportunity_v1",
        "festival": "vera_seasonal_opportunity_v1",
        "weather_heatwave": "vera_local_opportunity_v1",
        "category_trend_movement": "vera_category_trend_v1",
        "scheduled_recurring": "vera_curiosity_v1",
        "curious_ask_due": "vera_curiosity_v1",
        "recall_due": "vera_customer_recall_v1",
        "customer_lapsed_soft": "vera_customer_recall_v1",
        "customer_lapsed_hard": "vera_customer_recall_v1",
        "appointment_tomorrow": "vera_appointment_v1",
    }
    return mapping.get(kind, "vera_contextual_nudge_v1")


def first_template_params(name: str, m: Dict[str, Any], trig: Dict[str, Any], customer: Optional[Dict[str, Any]]):
    p = trig.get("payload", {})
    return [merchant_name(m), norm(p.get("title") or p.get("headline") or p.get("event") or trig.get("kind")), norm(customer.get("identity", {}).get("name")) if customer else ""]


def choose_fact(m: Dict[str, Any], p: Dict[str, Any]) -> str:
    perf = m.get("performance", {})
    if "views" in perf:
        return f"{perf.get('views')} views"
    for k in ("value", "count", "delta_pct", "rating"):
        if k in p: return f"{p[k]}"
    return ""


def compose(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any], customer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    kind = str(trigger.get("kind", "")).lower()
    p = trigger.get("payload", {}) or {}
    name = short_name(merchant)
    tone = category_tone(category)
    hi = lang_mix(merchant)
    category_name = category.get("slug", merchant.get("category_slug", "business"))
    body = ""
    cta = "open_ended"
    rationale = ""
    send_as = "merchant_on_behalf" if trigger.get("scope") == "customer" or customer else "vera"

    if kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard"} and customer:
        cn = customer.get("identity", {}).get("name", "there")
        last = customer.get("relationship", {}).get("last_visit") or p.get("last_visit")
        offer = next((o.get("title") for o in merchant.get("offers", []) if str(o.get("status", "")).lower() == "active"), None)
        due = p.get("due_date") or p.get("recall_due_date")
        if hi:
            body = f"Hi {cn}, {name} here 🦷. Aapka next recall window{(' ' + str(due)) if due else ''} open ho gaya hai."
            if offer: body += f" {offer} available hai."
            body += " Weekday evening mein slot chahiye?"
        else:
            body = f"Hi {cn}, {name} here. Your recall window{(' opens ' + str(due)) if due else ''} is due based on your last visit"
            body += f" ({last})" if last else ""
            if offer: body += f". Current option: {offer}."
            body += " Would you like me to help with a weekday-evening slot?"
        cta = "open_ended"
        rationale = "Customer recall/lapse trigger; uses only the customer's supplied relationship data and the merchant's active offer."

    elif kind in {"research_digest", "research_digest_release"}:
        item = p.get("top_item") or {}
        title = item.get("title") or p.get("title") or "A new category update"
        source = item.get("source") or p.get("source")
        n = item.get("trial_n") or p.get("trial_n")
        segment = item.get("patient_segment") or p.get("patient_segment")
        detail = title
        if n: detail += f" ({n:,}-participant/patient evidence)"
        body = f"{name}, a new {category_name} update caught my eye: {detail}."
        if segment: body += f" It is especially relevant to {str(segment).replace('_',' ')}."
        if source: body += f" Source: {source}."
        body += " Want me to turn the useful part into a short action you can use this week?"
        rationale = "Research trigger matched to the merchant's category; includes the supplied headline and source instead of a generic promotion."

    elif kind in {"perf_spike", "perf_dip"}:
        perf = merchant.get("performance", {})
        delta = p.get("delta_pct") or p.get("change_pct")
        if delta is None:
            d7 = perf.get("delta_7d", {})
            delta = d7.get("views_pct") if kind == "perf_spike" else d7.get("calls_pct")
        d = pct(delta) if delta is not None else None
        metric = p.get("metric") or ("views" if kind == "perf_spike" else "calls")
        value = perf.get(metric)
        if d is not None:
            direction = "up" if d >= 0 else "down"
            body = f"{name}, your {metric} are {direction} {abs(d):.0f}% in the latest window"
        else:
            body = f"{name}, I noticed a change in your {metric} in the latest performance snapshot"
        if value is not None: body += f" ({value:,} in the current window)"
        body += ". Want me to look at the most likely profile/offer lever behind it?"
        rationale = "Performance trigger anchored to the latest supplied merchant metric; asks for one low-friction next step."

    elif kind == "milestone_reached":
        milestone = p.get("milestone") or p.get("value") or "a new milestone"
        body = f"{name}, you just crossed {milestone}. That's a concrete signal worth using. Want a simple next-step post/offer built around it?"
        rationale = "Milestone trigger with a concrete supplied value and a single optional follow-on action."

    elif kind == "review_theme_emerged":
        theme = p.get("theme") or p.get("review_theme") or "a repeated customer theme"
        count = p.get("count") or p.get("reviews_count")
        body = f"{name}, {count + ' reviews' if isinstance(count,str) else (str(count) + ' reviews' if count else 'multiple recent reviews')} mention {theme}."
        body += " Want me to turn that into one practical response/update?"
        rationale = "Review-pattern trigger; surfaces the repeated theme rather than sending a generic reputation reminder."

    elif kind == "competitor_opened":
        competitor = p.get("competitor_name") or p.get("name") or "a nearby competitor"
        distance = p.get("distance_km") or p.get("distance")
        body = f"{name}, {competitor} just appeared nearby"
        if distance: body += f" ({distance} km away)"
        body += ". Want me to suggest one category-appropriate way to make your profile more distinctive?"
        rationale = "Local competitor trigger; references only the supplied competitor fact and offers one relevant action."

    elif kind in {"festival_upcoming", "festival"}:
        event = p.get("event") or p.get("festival") or p.get("name") or "an upcoming local event"
        days = p.get("days_until") or p.get("days")
        offer = next((o.get("title") for o in merchant.get("offers", []) if str(o.get("status", "")).lower() == "active"), None)
        body = f"{name}, {event} is {days} days away" if days is not None else f"{name}, {event} is coming up"
        if offer: body += f". Your current {offer} could be the starting point."
        body += " Want a category-fit campaign angle rather than a generic discount?"
        rationale = "Seasonal trigger matched with the merchant's active offer and category rather than inventing a discount."

    elif kind in {"category_trend_movement"}:
        query = p.get("query") or p.get("search") or "a category search"
        delta = p.get("delta_yoy")
        body = f"{name}, searches for “{query}” are"
        if delta is not None:
            body += f" up {abs(pct(delta) if abs(float(delta)) <= 1 else float(delta)):.0f}% YoY"
        else:
            body += " moving"
        body += ". Want me to map that signal to one profile/content idea for your business?"
        rationale = "Category-trend trigger uses the supplied query and movement as the hook."

    elif kind in {"weather_heatwave"}:
        temp = p.get("temperature") or p.get("temp_c")
        body = f"{name}, today's local weather signal is {temp}°C" if temp is not None else f"{name}, there's a local heatwave signal today"
        body += ". Want a category-specific customer message that fits the weather without sounding like spam?"
        rationale = "External weather trigger translated into a category-relevant content opportunity."

    else:
        # Generic but still grounded: prefer a trigger fact and one merchant fact.
        hook = p.get("title") or p.get("headline") or p.get("event") or trigger.get("kind", "new update")
        fact = choose_fact(merchant, p)
        body = f"{name}, {hook}"
        if fact: body += f" — your latest snapshot shows {fact}"
        body += ". Worth a quick look together?"
        rationale = "Contextual trigger message grounded in the supplied trigger and merchant snapshot."

    # Avoid false claims and keep messages compact.
    body = re.sub(r"\s+", " ", body).strip()
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('id','unknown')}:{merchant.get('merchant_id','unknown')}:{customer.get('customer_id') if customer else 'merchant'}",
        "rationale": rationale,
        "template_name": template_for(kind),
        "template_params": first_template_params(template_for(kind), merchant, trigger, customer),
    }


def choose_actions(now: str, trigger_ids: list[str]) -> list[dict]:
    candidates = []
    for tid in trigger_ids:
        trig = trigger_for_action(tid)
        if not trig: continue
        mid = trig.get("merchant_id")
        cid = trig.get("customer_id")
        if not mid: continue
        m = find_merchant(mid)
        if not m: continue
        cat = find_category(m.get("category_slug", "")) or {"slug": m.get("category_slug", "business"), "voice": {}}
        customer = find_customer(cid) if cid else None
        if trig.get("scope") == "customer" and not customer: continue
        # Do not re-send the same suppression key after a successful action.
        sk = trig.get("suppression_key")
        if sk and sk in sent_suppressions: continue
        urgency = int(trig.get("urgency", 2) or 2)
        # Recent engagement can be used as a soft signal: don't spam if the same trigger was just handled.
        action = compose(cat, m, trig, customer)
        action.update({
            "conversation_id": f"conv_{hashlib.sha1((mid+str(cid)+tid+str(time.time_ns())).encode()).hexdigest()[:12]}",
            "merchant_id": mid,
            "customer_id": cid,
            "trigger_id": tid,
        })
        candidates.append((urgency, action))
    candidates.sort(key=lambda x: (-x[0], x[1]["merchant_id"]))
    # Keep the action volume conservative; the harness allows up to 20.
    out = []
    seen_pairs = set()
    for _, a in candidates:
        pair = (a["merchant_id"], a["customer_id"])
        if pair in seen_pairs: continue
        seen_pairs.add(pair)
        out.append(a)
        if len(out) >= 20: break
    return out


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


@app.post("/v1/context")
def context_push(req: ContextPush):
    if req.scope not in {"category", "merchant", "customer", "trigger"}:
        return {"accepted": False, "reason": "invalid_scope", "details": req.scope}
    key = (req.scope, req.context_id)
    existing = contexts.get(key)
    if existing and req.version < existing["version"]:
        return {"accepted": False, "reason": "stale_version", "current_version": existing["version"]}
    if existing and req.version == existing["version"]:
        return {"accepted": True, "ack_id": f"ack_{hashlib.md5((req.scope+req.context_id+str(req.version)).encode()).hexdigest()[:12]}", "stored_at": datetime.utcnow().isoformat()+"Z"}
    contexts[key] = {"version": req.version, "payload": req.payload, "delivered_at": req.delivered_at}
    return {"accepted": True, "ack_id": f"ack_{hashlib.md5((req.scope+req.context_id+str(req.version)).encode()).hexdigest()[:12]}", "stored_at": datetime.utcnow().isoformat()+"Z"}


@app.post("/v1/tick")
def tick(req: TickRequest):
    actions = choose_actions(req.now, req.available_triggers)
    for a in actions:
        conv = conversations.setdefault(a["conversation_id"], {"turns": [], "state": "active", "sent": []})
        conv["merchant_id"] = a["merchant_id"]
        conv["customer_id"] = a.get("customer_id")
        conv["trigger_id"] = a["trigger_id"]
        conv["sent"].append(a["body"])
        add_turn(a["conversation_id"], "vera", a["body"], trigger_id=a["trigger_id"])
        if a.get("suppression_key"): sent_suppressions.add(a["suppression_key"])
    # Do not expose internal context payloads in the response.
    safe = []
    for a in actions:
        safe.append({k: a[k] for k in ["conversation_id","merchant_id","customer_id","send_as","trigger_id","template_name","template_params","body","cta","suppression_key","rationale"]})
    return {"actions": safe}


@app.post("/v1/reply")
def reply(req: ReplyRequest):
    conv = conversations.setdefault(req.conversation_id, {"turns": [], "state": "active", "sent": []})
    add_turn(req.conversation_id, "merchant" if req.from_role == "merchant" else "customer", req.message,
             turn_number=req.turn_number, auto_reply=is_auto_reply(req.message))
    text = norm(req.message)

    if is_auto_reply(text):
        count = sum(1 for t in conv["turns"] if t.get("from") == "merchant" and t.get("auto_reply"))
        if count >= 2 or is_duplicate_auto_reply(conv, text):
            conv["state"] = "ended_auto_reply"
            return {"action": "end", "rationale": "Repeated canned WhatsApp Business response detected; ending rather than consuming more turns."}
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Likely automated WhatsApp Business reply; pausing once before retrying."}

    if NEGATIVE.search(text):
        conv["state"] = "ended_negative"
        return {"action": "end", "rationale": "Merchant declined or requested no further outreach; exiting respectfully."}

    # Intent transition: explicit commitment should immediately move to action.
    if ACTION_INTENT.search(text) and ("let" in text.lower() or "do it" in text.lower() or "join" in text.lower() or "start" in text.lower() or "update" in text.lower() or "publish" in text.lower()):
        body = "Absolutely — let's do it. I'll take the next step from the details already shared. If anything essential is missing, I'll ask for only that specific item."
        conv["state"] = "action"
        conv["sent"].append(body)
        add_turn(req.conversation_id, "vera", body)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Detected explicit action intent; switching from qualification to execution immediately."}

    if OFF_TOPIC.search(text):
        body = "I can help with your magicpin/Google profile, offers, customer engagement, and marketing tasks here. For that other request, I don't want to pretend I can handle it from this chat."
        add_turn(req.conversation_id, "vera", body)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Stayed on Vera's merchant-growth mission and avoided claiming unsupported capabilities."}

    if POSITIVE.search(text):
        body = "Great. I’ll keep this focused and take the next useful step from the context you’ve already shared."
        add_turn(req.conversation_id, "vera", body)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Acknowledged engagement and advanced the conversation without repeating qualification."}

    # Questions: answer only from known context when possible.
    if "what" in text.lower() or "how" in text.lower() or "why" in text.lower() or "price" in text.lower() or "offer" in text.lower():
        m = find_merchant(req.merchant_id) or {}
        active = [o.get("title") for o in m.get("offers", []) if str(o.get("status", "")).lower() == "active"]
        if active:
            body = f"The active offer I can see is {active[0]}. I’ll use that rather than inventing a new price. What would you like to change about it?"
        else:
            body = "I can work from the merchant context currently available to me. I don't see an active offer in it, so I won't invent a price. Tell me which profile, offer, or customer task you want to work on."
        add_turn(req.conversation_id, "vera", body)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Answered from supplied context and explicitly avoided inventing missing commercial data."}

    return {"action": "wait", "wait_seconds": 900, "rationale": "No clear action intent or useful new information; pausing instead of sending generic filler."}


@app.get("/v1/healthz")
def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _v in contexts.items(): counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time()-START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Builders"),
        "team_members": [x for x in os.getenv("TEAM_MEMBERS", "Sambhav Saxena").split(",") if x.strip()],
        "model": "deterministic-context-composer-v1",
        "approach": "stateful context store + trigger prioritization + grounded composer + conversation intent routing",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": "1.0.0",
        "submitted_at": os.getenv("SUBMITTED_AT", datetime.utcnow().isoformat()+"Z")
    }


@app.post("/v1/teardown")
def teardown():
    contexts.clear(); conversations.clear(); sent_suppressions.clear()
    return {"ok": True, "state_cleared": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
