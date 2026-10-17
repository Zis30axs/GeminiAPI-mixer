"""Parse only the answer and the thought text publicly returned by Gemini Web."""
import json
import re


_BARD_ERROR = re.compile(r'BardErrorInfo(?:\\?"\s*,)?\s*\[\s*(\d+)\s*\]')


class GenerationText(str):
    """Answer text compatible with existing callers, with separate thought text."""

    def __new__(cls, content="", reasoning_content=""):
        value = super().__new__(cls, content)
        value.reasoning_content = reasoning_content
        return value


def _raise_upstream_error(raw):
    error = _BARD_ERROR.search(raw)
    if error:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{error.group(1)}]")


def _records(value):
    if not isinstance(value, list):
        return
    if len(value) > 2 and value[0] == "wrb.fr":
        yield value
    else:
        for child in value:
            if isinstance(child, list):
                yield from _records(child)


def _snapshots(line):
    _raise_upstream_error(line)
    try:
        outer = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return
    for record in _records(outer):
        if not isinstance(record[2], str):
            continue
        try:
            inner = json.loads(record[2])
        except (ValueError, TypeError, RecursionError):
            continue
        if not (isinstance(inner, list) and len(inner) > 4 and isinstance(inner[4], list)):
            continue
        # Match the primary candidate instead of concatenating alternate answers.
        if not inner[4] or not isinstance(inner[4][0], list):
            continue
        candidate = inner[4][0]
        content, thought = "", ""
        if len(candidate) > 1 and isinstance(candidate[1], list) and candidate[1]:
            if isinstance(candidate[1][0], str):
                content = candidate[1][0]
        if len(candidate) > 37:
            branch = candidate[37]
            if (isinstance(branch, list) and branch and isinstance(branch[0], list)
                    and branch[0] and isinstance(branch[0][0], str)):
                thought = branch[0][0]
        yield {"reasoning_content": thought, "content": content}


def extract_generation(raw, cleaner):
    """Read final snapshots without mixing thoughts into the final answer."""
    _raise_upstream_error(raw)
    final = {"content": "", "reasoning_content": ""}
    for line in raw.splitlines():
        for snapshot in _snapshots(line):
            for key, value in snapshot.items():
                if value and not final[key].startswith(value):
                    final[key] = value
    return GenerationText(cleaner(final["content"]), final["reasoning_content"].strip())


class StreamAccumulator:
    """Turn cumulative upstream snapshots into independent channel deltas."""

    def __init__(self, cleaner):
        self.cleaner = cleaner
        self.previous = {"reasoning_content": "", "content": ""}
        self.emitted_any = False

    def feed(self, line):
        deltas = []
        for snapshot in _snapshots(line):
            for key, value in snapshot.items():
                previous = self.previous[key]
                if not value or value == previous or previous.startswith(value):
                    continue
                if not value.startswith(previous):
                    raise RuntimeError(f"Gemini stream {key} changed after output began")
                delta = value[len(previous):]
                self.previous[key] = value
                if key == "content":
                    delta = self.cleaner(delta, strip=False)
                if delta:
                    deltas.append({key: delta})
                    self.emitted_any = True
        return deltas


def normalize_delta(value):
    """Accept new channel events and legacy string-only stream implementations."""
    if isinstance(value, str):
        return {"content": str(value)} if value else {}
    if isinstance(value, dict):
        return {key: value[key] for key in ("reasoning_content", "content")
                if isinstance(value.get(key), str) and value[key]}
    return {}
