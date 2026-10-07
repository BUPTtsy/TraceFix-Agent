from tracefix.rules.analyzers import analyze_source


def test_react_typescript_analysis_returns_components_bindings_and_names():
    result = analyze_source({"src/App.tsx": '''
function App() {
  return <button onClick={save} aria-label="保存">Save</button>;
}
'''})
    assert result["status"] == "pass"
    button = next(item for item in result["items"] if item.get("tag") == "button")
    assert button["name"] == "保存" and button["events"] == {"onClick": "{save}"}
    assert button["line"] == 3 and button["language"] == "react-tsx"
    assert any(item.get("kind") == "component" and item["name"] == "App" for item in result["items"])


def test_vue3_template_and_script_are_analyzed_together():
    result = analyze_source({"src/App.vue": '''<template>
<label for="email">邮箱</label><input id="email" v-model="email" />
<button @click="save">保存</button>
</template>
<script setup lang="ts">
const email: string = '';
</script>'''}, {"check": "a11y_name"})
    assert result["status"] == "pass"
    inputs = [item for item in result["items"] if item.get("tag") == "input"]
    assert inputs[0]["name"] == "邮箱" and "v-model" in inputs[0]["events"]
    assert inputs[0]["language"] == "vue3"


def test_javascript_syntax_and_event_listener_analysis():
    result = analyze_source({"src/app.js": 'document.querySelector("button").addEventListener("click", save);'})
    assert result["status"] == "pass"
    assert any(item["kind"] == "event_binding" for item in result["items"])
    invalid = analyze_source({"src/app.js": "const broken = ;"}, {"check": "syntax"})
    assert invalid["status"] == "fail"
    assert invalid["findings"][0]["line"] == 1
    assert analyze_source({"src/app.js": "const App = () => <button>保存</button>"})["status"] == "pass"


def test_html_accessible_name_check_reports_missing_name():
    result = analyze_source({"index.html": '<button>保存</button>\n<input type="text">'}, {"check": "a11y_name"})
    assert result["status"] == "fail"
    assert result["findings"] == [{"path": "index.html", "line": 2, "language": "html",
        "detector": "a11y_name", "message": "可操作元素缺少静态可确定的可访问名称", "tag": "input"}]


def test_dynamic_vue_name_and_invalid_script_request_fallback():
    dynamic = analyze_source({"App.vue": '<template><button :aria-label="name">{{ name }}</button></template>'}, {"check": "a11y_name"})
    assert dynamic["status"] == "error" and dynamic["fallback_required"]
    invalid = analyze_source({"App.vue": '<script setup lang="ts">const x = ;</script>'})
    assert invalid["status"] == "error" and invalid["errors"][0]["line"] == 1


def test_parser_unavailable_is_error_not_pass(monkeypatch):
    import tracefix.rules.analyzers as analyzers

    def unavailable(*args, **kwargs):
        raise ImportError("missing parser")

    monkeypatch.setattr(analyzers, "_source_analysis", unavailable)
    result = analyzers.analyze_source({"src/app.ts": "const value = 1"})
    assert result["status"] == "error" and result["fallback_required"]
    assert "missing parser" in result["errors"][0]["message"]


def test_regex_adapter_is_bounded_and_filters_paths():
    result = analyze_source({"src/app.js": "eval('1')", "other.py": "eval('2')"},
                            {"check": "regex", "regex": r"eval\(", "path_globs": ["src/*.js"]})
    assert result["status"] == "fail" and len(result["findings"]) == 1
    assert result["analyzed_files"] == ["src/app.js"]
    invalid = analyze_source({"src/app.js": "code"}, {"check": "regex", "regex": "["})
    assert invalid["status"] == "error"


def test_unsupported_detector_and_empty_scope_require_fallback():
    assert analyze_source({}, {"check": "semgrep"})["fallback_required"]
    assert analyze_source({"src/a.ts": "const value = 1"}, {"path_globs": ["other/**"]})["status"] == "error"
    assert analyze_source({"src/a.ts": "const value = 1"}, {"analyzer": "semgrep"})["status"] == "error"
    assert analyze_source({"index.html": "<p>文本</p>"}, {"check": "a11y_name", "tag": "button"})["status"] == "error"


def test_nested_react_label_names_input_and_missing_handler_requests_fallback():
    result = analyze_source({"src/App.jsx": '<label>邮箱<input /></label>'}, {"check": "a11y_name"})
    assert result["status"] == "pass"
    result = analyze_source({"index.html": '<button type="submit">保存</button>'}, {"check": "event_binding"})
    assert result["status"] == "error" and result["fallback_required"]


def test_malformed_html_syntax_is_failure():
    result = analyze_source({"index.html": '<div><button>保存</div>'}, {"check": "syntax"})
    assert result["status"] == "fail"
    assert result["findings"][0]["language"] == "html"
