"""Security contracts for the public, dependency-free quota shell."""
import json
import re
import shutil
import subprocess
import unittest
from base64 import b64encode
from hashlib import sha256
from html.parser import HTMLParser
from unittest import mock
from urllib.parse import urljoin

from gemini_web2api.quota_dashboard import CONTENT_SECURITY_POLICY, HTML_PAGE, render_dashboard


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

    def origins(self, html):
        encoded = re.search(r"const TRUSTED_HTTP_ORIGINS = Object.freeze\((.*?)\);", html).group(1)
        return json.loads(encoded)

    def assert_script_hash(self, html, csp):
        parser = ShellParser()
        parser.feed(html)
        self.assertEqual(sum(tag == "script" for tag, _ in parser.elements), 1)
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        digest = b64encode(sha256(script.encode("utf-8")).digest()).decode("ascii")
        self.assertIn("script-src 'sha256-" + digest + "'", csp)

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
        self.assert_script_hash(HTML_PAGE, CONTENT_SECURITY_POLICY)
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
        self.assertIn('const ENDPOINT = "./v1/quota/antigravity"', self.script)
        self.assertEqual(self.script.count("fetch("), 1)
        self.assertIn("fetch(ENDPOINT,", self.script)
        for contract in ('method: "GET"', 'mode: "same-origin"', 'credentials: "omit"', 'redirect: "error"', 'cache: "no-store"', '"Authorization": "Bearer " + mixerKey'):
            self.assertIn(contract, self.script)
        self.assertIn('location.protocol === "https:"', self.script)
        self.assertNotIn("/chat/completions", HTML_PAGE)
        self.assertNotIn("/responses", HTML_PAGE)

    def test_relative_endpoint_preserves_reverse_proxy_prefix(self):
        endpoint = re.search(r'const ENDPOINT = "([^"]+)"', self.script).group(1)
        for prefix in ("", "/gemini", "/nested/gemini"):
            with self.subTest(prefix=prefix):
                url = "https://mixer.example" + prefix + "/quota"
                self.assertEqual(urljoin(url, endpoint), "https://mixer.example" + prefix + "/v1/quota/antigravity")
        self.assertNotIn("X-Forwarded", HTML_PAGE)
        self.assertNotIn("<base", HTML_PAGE)

    def test_legacy_constants_are_empty_allowlist_defaults(self):
        self.assertEqual(render_dashboard({}), (HTML_PAGE, CONTENT_SECURITY_POLICY))
        self.assertEqual(self.origins(HTML_PAGE), [])
        self.assertIn('["localhost", "127.0.0.1", "[::1]"]', self.script)
        self.assertIn("HTTP 不加密", HTML_PAGE)

    def test_operator_http_origins_are_canonical_exact_origins(self):
        html, csp = render_dashboard({"MIXER_DASHBOARD_HTTP_ORIGINS": (
            " http://EXAMPLE.test:80 , http://[2001:0db8::1]:8080,"
            "http://127.0.0.1:8080,http://frp.example.test:60000,http://example.test "
        )})
        self.assertEqual(self.origins(html), ["http://example.test", "http://[2001:db8::1]:8080", "http://127.0.0.1:8080", "http://frp.example.test:60000"])
        self.assertNotEqual(csp, CONTENT_SECURITY_POLICY)
        self.assert_script_hash(html, csp)
        self.assertNotIn("__MIXER_DASHBOARD_HTTP_ORIGINS__", html)

    def test_renderer_reads_only_operator_environment_not_request_headers(self):
        origin = "http://explicit.example:60941"
        with mock.patch.dict("os.environ", {"MIXER_DASHBOARD_HTTP_ORIGINS": origin}, clear=True):
            self.assertEqual(self.origins(render_dashboard()[0]), [origin])
            self.assertEqual(self.origins(HTML_PAGE), [])
        html, _ = render_dashboard({"HTTP_HOST": "attacker.example", "HTTP_X_FORWARDED_HOST": "attacker.example", "MIXER_DASHBOARD_HTTP_ORIGINS": ""})
        self.assertEqual(self.origins(html), [])
        self.assertNotIn("attacker.example", html)

    def test_invalid_operator_configuration_fails_closed_without_partial_grants(self):
        invalid = (
            None, True, [], {}, "http://valid.example,", ",http://valid.example",
            "http://valid.example,http://evil.example/path", "https://example.test",
            "//example.test", "http:example.test", "http://user:pass@example.test",
            "http://example.test@evil.test", "http://example.test/", "http://example.test/path",
            "http://example.test?", "http://example.test?key=value", "http://example.test#",
            "http://example.test#fragment", "http://example.test\\evil", "http://exam%70le.test",
            "http://example.test\r", "http://example.test\n", "http://example.test\t",
            "http://example.test\x00", "http://example.test\x7f", "http://ｅxample.test",
            "http://example.test:0", "http://example.test:65536", "http://example.test:",
            "http://example.test:notaport", "http://[::1", "http://[::1%25eth0]",
            "http://-example.test", "http://example..test", "http://example.test.",
            "http://127.1", "http://2130706433", "http://0177.0.0.1", "http://0x7f000001",
            "http://example.test\";alert(1)//", "http://example.test</script><script>alert(1)</script>",
            "http://" + "a" * 64 + ".test", "x" * 8193,
            ",".join("http://origin" + str(index) + ".test" for index in range(17)),
        )
        for value in invalid:
            with self.subTest(value=repr(value)[:80]):
                html, csp = render_dashboard({"MIXER_DASHBOARD_HTTP_ORIGINS": value})
                self.assertEqual(self.origins(html), [])
                self.assertEqual((html, csp), (HTML_PAGE, CONTENT_SECURITY_POLICY))
                self.assert_script_hash(html, csp)

    def test_http_gate_and_iframe_checks_guard_submit_and_refresh(self):
        self.assertIn("window.top !== window.self", self.script)
        self.assertIn("TRUSTED_HTTP_ORIGINS.includes(location.origin)", self.script)
        self.assertIn("const TRUSTED_HTTP_ORIGINS = Object.freeze([])", self.script)
        self.assertIn('byId("mixer-key").disabled = !trusted', self.script)
        refresh = self.script.split("async function refresh()", 1)[1].split('byId("connect-form").addEventListener', 1)[0]
        submit = self.script.split('byId("connect-form").addEventListener', 1)[1].split('byId("disconnect").addEventListener', 1)[0]
        self.assertLess(refresh.index("if (!trustedContext())"), refresh.index("fetch(ENDPOINT"))
        self.assertLess(submit.index("if (!trustedContext())"), submit.index("mixerKey = candidate"))

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional for isolated context-gate execution")
    def test_context_gate_executes_with_default_and_explicit_origin_cases(self):
        def run_cases(html, cases):
            script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
            gate = re.search(r"  function trustedContext\(\) \{.*?\n  \}", script, re.S).group(0)
            program = (
                "const TRUSTED_HTTP_ORIGINS = " + json.dumps(self.origins(html)) + ";\n"
                "let location; const window = {self: {}};\n" + gate + "\n"
                "const cases = " + json.dumps(cases) + ";\n"
                "process.stdout.write(JSON.stringify(cases.map(item => {location=item.location;"
                "window.top=item.framed ? {} : window.self;return trustedContext();})));"
            )
            result = subprocess.run([shutil.which("node")], input=program, text=True, capture_output=True, check=True, timeout=10)
            return json.loads(result.stdout)

        def context(protocol, hostname, origin, framed=False):
            return {"location": {"protocol": protocol, "hostname": hostname, "origin": origin}, "framed": framed}

        cases = [
            context("https:", "public.test", "https://public.test"),
            context("http:", "localhost", "http://localhost:8000"),
            context("http:", "127.0.0.1", "http://127.0.0.1:8000"),
            context("http:", "[::1]", "http://[::1]:8000"),
            context("http:", "public.test", "http://public.test:60941"),
            context("file:", "", "null"),
            context("https:", "public.test", "https://public.test", framed=True),
            context("http:", "localhost", "http://localhost:8000", framed=True),
        ]
        self.assertEqual(run_cases(HTML_PAGE, cases), [True, True, True, True, False, False, False, False])
        configured, _ = render_dashboard({"MIXER_DASHBOARD_HTTP_ORIGINS": "http://public.test:60941"})
        explicit_cases = [
            context("http:", "public.test", "http://public.test:60941"),
            context("http:", "public.test", "http://public.test:60942"),
            context("http:", "public.test", "http://public.test"),
            context("http:", "public.test.evil.test", "http://public.test.evil.test:60941"),
            context("http:", "public.test", "http://public.test:60941", framed=True),
        ]
        self.assertEqual(run_cases(configured, explicit_cases), [True, False, False, False, False])

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
