import json
import base64

from magpie.core.js_intelligence import extract
from magpie.core.parsers.base import OfflineContext
from magpie.core.parsers.har import HarParser
from magpie.core.parsers.js_manifest import JsManifestParser


def test_literal_calls_offsets_and_false_calls():
    text = "// fetch('https://fake.example.test/x')\nconst x=\"axios.get('https://fake.example.test/y')\";\nfetch('https://api.example.test/a?q=1');\naxios.post('/relative', body);"
    result = extract(text)
    assert result["status"] == "bounded_rules_complete"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["url"] == "https://api.example.test/a?q=1"
    assert text[result["candidates"][0]["start"]:result["candidates"][0]["end"]] == "'https://api.example.test/a?q=1'"
    assert result["candidates"][1]["disposition"] == "needs_base_confirmation"


def test_dynamic_templates_and_method_are_not_promoted():
    result = extract("fetch(`https://api.example.test/${id}`); fetch('/x', {method: chosen});")
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["absolute"] is False
    assert result["candidates"][0]["method"] is None
    assert result["status"] == "partial"


def test_overlong_static_url_is_reported_but_never_promoted():
    url = "https://api.example.test/" + "x" * 2100
    result = extract(f"fetch('{url}')")
    assert result["status"] == "partial"
    candidate = result["candidates"][0]
    assert candidate["url"] == url
    assert candidate["absolute"] is False
    assert candidate["disposition"] == "unsupported_limit"
    assert "未创建接口事实" in candidate["reason"]


def test_candidate_cap_is_visible_and_marks_scan_truncated():
    result = extract("fetch('https://a.example.test/1'); fetch('https://a.example.test/2')", max_candidates=1)
    assert result["truncated"] is True
    assert "仅保留前1个候选" in result["reason"]
    assert len(result["candidates"]) == 1


def test_extractor_honors_cancellation_at_token_boundaries():
    result = extract(" ".join("x" for _ in range(10000)), cancelled=lambda: True)
    assert result["status"] == "cancelled"


def test_manifest_parser_preserves_hash_candidate_provenance_without_body():
    manifest = {"magpie_js": 1, "url": "https://app.example.test/app.js",
                "content": "// 🐦\nfetch('https://api.example.test/v1?q=secret');"}
    ctx = OfflineContext()
    records = list(JsManifestParser().parse([json.dumps(manifest)], ctx))
    observations = [r for r in records if getattr(r, "type", "") in {"js_candidate", "js_content_version"}]
    candidate = json.loads(next(r.value for r in observations if r.type == "js_candidate"))
    assert candidate["content_sha256"]
    assert candidate["record_path"] == "$.content"
    assert candidate["start"] > 0 and candidate["snippet"]
    assert "secret" not in candidate["url"]
    assert "fetch('https://api.example.test/v1?q=secret')" not in json.dumps([r.value for r in observations])


def test_manifest_sniff_requires_top_level_marker():
    assert JsManifestParser.sniff('{"magpie_js":1,"url":"https://a.example.test/a.js"}', "x.json") == 1
    assert JsManifestParser.sniff('{"url":"https://a.example.test/a.js","content":"","magpie_js":1}', "x.json") == 1
    assert JsManifestParser.sniff('{"note":"magpie_js 1"}', "x.json") == 0


def test_manifest_reports_unknown_fields_and_rejects_boolean_version():
    ctx = OfflineContext()
    records = list(JsManifestParser().parse([json.dumps({"magpie_js": 1,
        "url": "https://app.example.test/app.js", "content": "", "future": "kept visible"})], ctx))
    assert records
    assert ctx.unknown_fields == {"JS manifest.future": 1}
    bad_ctx = OfflineContext()
    assert list(JsManifestParser().parse(['{"magpie_js":true,"url":"https://app.example.test/app.js","content":""}'], bad_ctx)) == []
    assert bad_ctx.bad_lines


def test_har_content_modes_and_record_paths():
    plain = "fetch('https://api.example.test/plain');"
    encoded = "axios.get('https://api.example.test/encoded');"
    entries = [
        {"startedDateTime": "2026-01-01T00:00:00Z", "request": {"method": "GET", "url": "https://cdn.example.test/plain.js"},
         "response": {"content": {"mimeType": "application/javascript", "text": plain}}},
        {"startedDateTime": "2026-01-01T00:00:01Z", "request": {"method": "GET", "url": "https://cdn.example.test/bundle"},
         "response": {"content": {"mimeType": "text/ecmascript", "encoding": "base64",
                                     "text": base64.b64encode(encoded.encode()).decode()}}},
        {"startedDateTime": "2026-01-01T00:00:02Z", "request": {"method": "GET", "url": "https://cdn.example.test/missing.mjs"},
         "response": {"content": {"mimeType": "application/javascript"}}},
        {"startedDateTime": "2026-01-01T00:00:03Z", "request": {"method": "GET", "url": "https://cdn.example.test/bad.js"},
         "response": {"content": {"encoding": "base64", "text": "%%%"}}},
        {"startedDateTime": "2026-01-01T00:00:04Z", "request": {"method": "GET", "url": "https://cdn.example.test/encoded.js"},
         "response": {"content": {"encoding": "gzip", "text": plain}}},
    ]
    ctx = OfflineContext()
    records = list(HarParser().parse([json.dumps({"log": {"entries": entries}})], ctx))
    versions = [json.loads(r.value) for r in records if getattr(r, "type", "") == "js_content_version"]
    statuses = [r.value for r in records if getattr(r, "type", "") == "js_content_status"]
    assert len(versions) == 2
    assert {v["record_path"] for v in versions} == {
        "$.log.entries[0].response.content.text", "$.log.entries[1].response.content.text"}
    assert set(statuses) == {"content_not_imported", "base64_invalid", "unsupported_encoding"}
    assert not ctx.bad_lines
