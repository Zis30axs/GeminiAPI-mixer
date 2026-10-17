"""Static security contracts for the public, dependency-free quota shell."""
import re
import unittest
from base64 import b64encode
from hashlib import sha256
from html.parser import HTMLParser

from gemini_web2api.quota_dashboard import CONTENT_SECURITY_POLICY, HTML_PAGE


class ShellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class QuotaDashboardTests(unittest.TestCase):
    def setUp(self):
        self.parser = ShellParser()
        self.parser.feed(HTML_PAGE)
        self.script = re.search(r"<script>(.*?)</script>", HTML_PAGE, re.S).group(1)

    def test_utf8_chinese_shell_has_no_external_assets_or_embedded_credentials(self):
        self.assertIn("模型与额度", HTML_PAGE.encode("utf-8").decode("utf-8"))
        self.assertIn(("meta", {"charset": "utf-8"}), self.parser.elements)
        self.assertIn(("html", {"lang": "zh-CN"}), self.parser.elements)
        for tag, attrs in self.parser.elements:
            self.assertFalse(any(name.startswith("on") for name in attrs))
            if tag in ("script", "iframe", "img", "link", "object", "embed"):
                self.assertNotIn("src", attrs)
                self.assertNotIn("href", attrs)
        self.assertNotRegex(HTML_PAGE, r"https?://|data:|sk-[A-Za-z0-9]{12}")

    def test_csp_hash_matches_the_only_inline_script(self):
        self.assertEqual(sum(tag == "script" for tag, _ in self.parser.elements), 1)
        digest = b64encode(sha256(self.script.encode("utf-8")).digest()).decode("ascii")
        self.assertIn("script-src 'sha256-" + digest + "'", CONTENT_SECURITY_POLICY)
        for directive in ("default-src 'none'", "style-src 'unsafe-inline'", "connect-src 'self'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"):
            self.assertIn(directive, CONTENT_SECURITY_POLICY)
        script_policy = next(value.strip() for value in CONTENT_SECURITY_POLICY.split(";") if value.strip().startswith("script-src"))
        self.assertNotIn("unsafe-inline", script_policy)
        self.assertNotIn("'self'", script_policy)

    def test_key_is_password_without_form_field_name_or_persistent_storage(self):
        inputs = [attrs for tag, attrs in self.parser.elements if tag == "input" and attrs.get("id") == "mixer-key"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["type"], "password")
        self.assertEqual(inputs[0]["autocomplete"], "off")
        self.assertNotIn("name", inputs[0])
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "URLSearchParams", "location.search", "location.hash", "sendBeacon", "console."):
            self.assertNotIn(forbidden, self.script)
        self.assertIn('let mixerKey = ""', self.script)
        self.assertIn('byId("mixer-key").value = ""', self.script)
        self.assertIn('mixerKey = ""', self.script[self.script.index('byId("disconnect").addEventListener'):])
        self.assertIn('window.addEventListener("pagehide"', self.script)

    def test_only_fixed_same_origin_authenticated_get_is_issued(self):
        self.assertIn('const ENDPOINT = "/v1/quota/antigravity"', self.script)
        self.assertEqual(self.script.count("fetch("), 1)
        self.assertIn("fetch(ENDPOINT,", self.script)
        for contract in ('method: "GET"', 'mode: "same-origin"', 'credentials: "omit"', 'redirect: "error"', 'cache: "no-store"', '"Authorization": "Bearer " + mixerKey'):
            self.assertIn(contract, self.script)
        self.assertIn('location.protocol !== "https:"', self.script)
        self.assertNotIn("/chat/completions", HTML_PAGE)
        self.assertNotIn("/responses", HTML_PAGE)

    def test_untrusted_values_use_text_nodes_and_errors_do_not_echo_response(self):
        self.assertIn("node.textContent = value", self.script)
        self.assertIn("document.createElement", self.script)
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "error.message", "response.text(", "response.json("):
            self.assertNotIn(forbidden, self.script)
        self.assertIn("if (!response.ok)", self.script)
        self.assertIn("HTTP 404", self.script)
        self.assertIn("认证失败", self.script)
        self.assertIn("size > MAX_BYTES", self.script)
        self.assertIn('new TextDecoder("utf-8", {fatal: true})', self.script)

    def test_refresh_has_timeout_concurrency_guard_and_opt_out(self):
        self.assertIn("if (busy || !mixerKey) return", self.script)
        self.assertIn("busy = true", self.script)
        self.assertIn("busy = false", self.script)
        self.assertIn("setInterval(refresh, 60000)", self.script)
        self.assertIn('byId("auto-refresh").checked && mixerKey', self.script)
        self.assertIn("clearInterval(refreshTimer)", self.script)
        self.assertIn("setTimeout(() => controller.abort(), 20000)", self.script)
        self.assertIn("version !== authVersion", self.script)
        self.assertIn("下方保留上次成功读取的数据", self.script)

    def test_unknown_percent_and_wallet_are_distinct_from_real_zero(self):
        self.assertIn('typeof value === "number" && Number.isFinite(value)', self.script)
        self.assertIn("value !== null", self.script)
        self.assertNotIn("Number(quota.remaining_percent)", self.script)
        self.assertNotRegex(self.script, r"remaining_percent\s*(?:\|\||\?\?)\s*0")
        self.assertIn("上游报告已用 100%", HTML_PAGE)
        self.assertIn("未确认的 Claude 额度", HTML_PAGE)
        self.assertIn("Google 模型配额分开计算", HTML_PAGE)
        self.assertIn("同一配额不重复累计", HTML_PAGE)
        self.assertIn("remaining_percent_range", self.script)

    def test_rows_show_evidence_availability_source_reset_and_snapshot_age(self):
        for contract in ("verification.status", "verification.tested_at", "availability.status", "quota.remaining_percent", "quota.reset_at", "quota.observed_at", "quota.age_seconds", "quota.stale", "quota.source", "model.alias_of", "model.quota_model_id", "model.thinking_control", "model.requested_effort"):
            self.assertIn(contract, self.script)
        self.assertIn("仅「已实测」", HTML_PAGE)
        self.assertIn("上游声明 · 未实测", HTML_PAGE)
        self.assertIn("复制模型名", HTML_PAGE)
        self.assertIn("navigator.clipboard.writeText(model.id)", self.script)


if __name__ == "__main__":
    unittest.main()
