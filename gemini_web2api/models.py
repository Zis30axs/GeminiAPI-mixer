"""Model definitions and mapping from Gemini frontend JS source."""
import json
import uuid

# MODE_CATEGORY enum from 028-6eb337387583.js:
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE, 8=ARGON

MODELS = {
    "gemini-4-argon": {
        "mode": 8, "think": 2,
        "desc": "Gemini 4 Argon Web route; fixed medium thinking",
    },
    "gemini-3.8-flash": {
        "mode": 1, "think": 4,
        "desc": "Flash Web route alias; served version depends on Google rollout",
    },
    "gemini-3.8-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Flash Web thinking route; fixed high thinking",
    },
    "gemini-3.8-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Flash Web dynamic thinking route",
    },
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Legacy Flash Web route alias; served version depends on Google rollout",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "Legacy Flash Web route alias; served version depends on Google rollout",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Legacy Flash Web route alias; served version depends on Google rollout",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-3.1-pro-enhanced": {
        "mode": 3, "think": 4,
        "desc": "Legacy Pro alias; fixed low thinking",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

THINKING_LEVELS = {"low": 4, "medium": 2, "high": 0}
for _base, _mode in (("gemini-4-argon", 8), ("gemini-3.1-pro", 3)):
    for _level, _think in THINKING_LEVELS.items():
        MODELS[f"{_base}-{_level}"] = {
            "mode": _mode, "think": _think,
            "desc": f"{_base} Web route; fixed {_level} thinking (client overrides ignored)",
        }
MODELS = {
    **{f"{base}-{level}": MODELS[f"{base}-{level}"]
       for base in ("gemini-4-argon", "gemini-3.1-pro") for level in THINKING_LEVELS},
    **MODELS,
}

# Public Web routing tickets, not account credentials. Keep routing strict:
# never retry without the ticket and silently substitute the default model.
# https://github.com/Sophomoresty/gemini-web2api/pull/105
MODEL_TICKETS = {
    1: "56fdd199312815e2",
    2: "56fdd199312815e2",
    3: "e6fa609c3fa255c0",
    5: "56fdd199312815e2",
    6: "8c46e95b1a07cecc",
    8: "95a4c0abcba24364",
}


def build_model_routing_headers(model_id: int, think_mode: int, model_name=None) -> dict:
    """Build Google's Web selector for a fixed server-side model preset."""
    if model_id in (1, 2, 5) and not (
        isinstance(model_name, str) and model_name.startswith("gemini-3.8-")
    ):
        # Preserve the established protocol for legacy Flash aliases. The new
        # explicit 3.8 routes opt into the ticket; availability can differ.
        return {}
    ticket = MODEL_TICKETS.get(model_id)
    if ticket is None:  # AUTO deliberately lets Google choose the model.
        return {}
    # Dynamic Flash thinking uses variant 1 even with think=0.
    variant = 1 if model_id == 5 else {4: 1, 2: 4, 0: 2}.get(think_mode)
    if variant is None:
        raise ValueError("Unsupported server thinking preset")
    selector = [
        1, None, None, None, ticket, None, None, 0,
        [4, 5, 6, 8, 4, 5, 6, 8], None, None, 2,
        None, None, model_id, variant, str(uuid.uuid4()),
    ]
    return {"X-Goog-Ext-525001261-Jspb": json.dumps(selector, separators=(",", ":"))}


def resolve_model(model_name: str, default: str = "gemini-3.6-flash"):
    """Resolve model name to (name, mode_id, think_mode, error, extra_fields).

    Thinking is selected by the named server preset. Legacy @think= suffixes
    are discarded, so a client cannot change a preset's fixed level.
    """
    if not isinstance(model_name, str):
        return None, None, None, "Model must be a string", None
    if "@think=" in model_name:
        model_name = model_name.split("@think=", 1)[0]
    cfg = MODELS.get(model_name)
    if not cfg:
        return None, None, None, f"Unknown model: {model_name}", None
    mode_id = cfg["mode"]
    think_mode = cfg["think"]
    extra = cfg.get("extra")
    return model_name, mode_id, think_mode, None, extra
