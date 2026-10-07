"""Bounded source analyzers for frontend checks and model tool evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


SUPPORTED_LANGUAGES = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "react-jsx", ".ts": "typescript", ".tsx": "react-tsx",
    ".mts": "typescript", ".cts": "typescript", ".vue": "vue3",
    ".html": "html", ".htm": "html",
}
SUPPORTED_DETECTORS = ("syntax", "elements", "a11y_name", "event_binding", "regex")
_INTERACTIVE = {"button", "input", "select", "textarea", "a", "summary"}
_ROLES = {"button", "checkbox", "radio", "textbox", "combobox", "link", "switch"}
_OPTIONAL_END = {"html", "head", "body", "li", "dt", "dd", "p", "rt", "rp", "optgroup", "option", "colgroup", "thead", "tbody", "tfoot", "tr", "td", "th"}
_SCRIPT = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.I | re.S)


@dataclass
class _Element:
    tag: str
    line: int
    attributes: dict[str, str | None] = field(default_factory=dict)
    text: str = ""
    children: list[Any] = field(default_factory=list)
    dynamic: bool = False


def _events(attributes):
    return {name: value for name, value in attributes.items()
            if name.startswith(("@", "v-on:")) or re.match(r"^on[A-Z]", name)
            or name.lower() in {"onclick", "onchange", "onsubmit", "oninput", "onkeydown"}
            or name.startswith("v-model")}


class _MarkupParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.diagnostics = []

    def handle_starttag(self, tag, attrs):
        element = _Element(tag, self.getpos()[0], dict(attrs))
        self.elements.append(element)
        if self.stack:
            self.stack[-1].children.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                if any(item.tag not in _OPTIONAL_END for item in self.stack[index + 1:]):
                    self.diagnostics.append({"line": self.getpos()[0], "message": "模板标签嵌套不匹配：" + tag})
                del self.stack[index:]
                break
        else:
            self.diagnostics.append({"line": self.getpos()[0], "message": "模板结束标签没有对应开始标签：" + tag})

    def handle_data(self, data):
        if any(item.tag in {"script", "style"} for item in self.stack):
            return
        for element in self.stack:
            element.text += data
            element.dynamic = element.dynamic or "{{" in data


def _walk(root):
    pending = [root]
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        if count > 100000:
            raise ValueError("源码 AST 超过节点分析上限")
        yield node
        pending.extend(reversed(node.named_children))


def _text(raw, node):
    return raw[node.start_byte:node.end_byte].decode("utf-8") if node else ""


def _script_analysis(content, *, jsx, line_offset=0):
    from tree_sitter import Language, Parser
    import tree_sitter_typescript

    grammar = (tree_sitter_typescript.language_tsx() if jsx
               else tree_sitter_typescript.language_typescript())
    raw = content.encode("utf-8")
    root = Parser(Language(grammar)).parse(raw).root_node
    elements, diagnostics, components, event_calls = [], [], [], []
    for node in _walk(root):
        line = node.start_point.row + 1 + line_offset
        if node.is_error or node.is_missing:
            diagnostics.append({"line": line, "column": node.start_point.column + 1,
                                "message": "缺少 " + node.type if node.is_missing else "源码语法错误"})
        if node.type in {"function_declaration", "class_declaration", "variable_declarator"}:
            name = _text(raw, node.child_by_field_name("name"))
            if name and name[0].isupper():
                components.append({"name": name, "line": line, "kind": "component"})
        if node.type == "call_expression":
            function = _text(raw, node.child_by_field_name("function"))
            if function.endswith(".addEventListener"):
                event_calls.append({"line": line, "expression": _text(raw, node)[:500],
                                    "kind": "event_binding"})
        if node.type not in {"jsx_element", "jsx_self_closing_element"}:
            continue
        opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
        attributes = {}
        for attribute in opening.named_children:
            if attribute.type != "jsx_attribute":
                continue
            name = _text(raw, attribute.named_children[0])
            value_node = attribute.named_children[1] if len(attribute.named_children) > 1 else None
            value = _text(raw, value_node) if value_node else None
            if value_node and value_node.type == "string":
                value = value[1:-1]
            attributes[name] = value
        text_nodes = [child for child in _walk(node) if child.type == "jsx_text"]
        element = _Element(_text(raw, opening.child_by_field_name("name")), line,
                           attributes, " ".join(_text(raw, child).strip() for child in text_nodes),
                           dynamic=any(child.type == "jsx_expression" for child in node.named_children))
        elements.append(element)
        if element.tag == "label":
            for child in _walk(node):
                if child.type in {"jsx_element", "jsx_self_closing_element"} and child != node:
                    child_opening = child.child_by_field_name("open_tag") if child.type == "jsx_element" else child
                    element.children.append((_text(raw, child_opening.child_by_field_name("name")),
                                             child.start_point.row + 1 + line_offset))
    return elements, diagnostics, components, event_calls


def _source_analysis(path, content):
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in {".vue", ".html", ".htm"}:
        return _script_analysis(content, jsx=suffix in {".js", ".mjs", ".cjs", ".jsx", ".tsx"})
    parser = _MarkupParser()
    parser.feed(content)
    parser.close()
    diagnostics = parser.diagnostics + [{"line": item.line, "message": "模板标签没有闭合：" + item.tag}
                                       for item in parser.stack if item.tag not in _OPTIONAL_END]
    components, event_calls = [], []
    for match in _SCRIPT.finditer(content):
        attrs = match.group("attrs")
        if re.search(r"\btype\s*=\s*['\"](?:application/ld\+json|application/json)['\"]", attrs, re.I):
            continue
        jsx = bool(re.search(r"\blang\s*=\s*['\"](?:tsx|jsx)['\"]", attrs, re.I))
        offset = content.count("\n", 0, match.start("body"))
        _, errors, script_components, script_events = _script_analysis(match.group("body"), jsx=jsx, line_offset=offset)
        diagnostics.extend(errors)
        components.extend(script_components)
        event_calls.extend(script_events)
    return parser.elements, diagnostics, components, event_calls


def _name(element, elements):
    attrs = element.attributes
    for attribute in ("aria-label", "title", "alt"):
        value = attrs.get(attribute)
        if value:
            return value.strip(), value.startswith("{")
    if any(name in attrs for name in (":aria-label", "v-bind:aria-label", ":aria-labelledby", "v-bind:aria-labelledby")):
        return "", True
    if attrs.get("aria-labelledby"):
        names = []
        for identity in attrs["aria-labelledby"].split():
            names.extend(item.text.strip() for item in elements if item.attributes.get("id") == identity)
        if names:
            return " ".join(names), False
        return "", True
    identity = attrs.get("id")
    for label in elements:
        if label.tag == "label" and ((identity and label.attributes.get("for", label.attributes.get("htmlFor")) == identity)
                                     or element in label.children or (element.tag, element.line) in label.children):
            return label.text.strip(), label.dynamic
    if element.tag == "input" and attrs.get("type") in {"submit", "reset", "button"}:
        return attrs.get("value") or {"submit": "Submit", "reset": "Reset"}.get(attrs["type"], ""), False
    if element.tag in {"button", "a", "summary"} or attrs.get("role") in _ROLES:
        return " ".join(element.text.split()), element.dynamic
    return "", False


def _record(element, elements, *, path, language):
    name, dynamic = _name(element, elements)
    return {"path": path, "line": element.line, "language": language, "tag": element.tag,
            "role": element.attributes.get("role"), "name": name,
            "name_dynamic": dynamic, "attributes": element.attributes,
            "events": _events(element.attributes), "kind": "element"}


def _selected(record, config):
    for key in ("tag", "role", "name"):
        if config.get(key) is not None and record.get(key) != config[key]:
            return False
    return True


def analyze_source(files: Mapping[str, str] | Iterable[tuple[str, str]],
                   config: dict[str, Any] | None = None, *, detector: str = "ast") -> dict[str, Any]:
    """Analyze authorized text only; pass means this configured check succeeded."""
    config = dict(config or {})
    adapter = config.get("analyzer", detector)
    check = config.get("check") or config.get("kind") or (adapter if adapter != "ast" else "elements")
    if check in {"ast", "tree-sitter"}:
        check = "elements"
    result = {"status": "pass", "detector": str(check), "findings": [], "items": [],
              "errors": [], "analyzed_files": [], "supported_languages": list(dict.fromkeys(SUPPORTED_LANGUAGES.values())),
              "parser": "tree-sitter-typescript/html.parser", "fallback_required": False}
    if adapter not in {"ast", "tree-sitter", *SUPPORTED_DETECTORS}:
        result["errors"].append({"message": "不支持的代码分析适配器：" + str(adapter), "detector": str(check)})
    elif check not in SUPPORTED_DETECTORS:
        result["errors"].append({"message": "不支持的代码分析器：" + str(check), "detector": str(check)})
    else:
        source_files = list(files.items() if isinstance(files, Mapping) else files)
        if len(source_files) > 200 or sum(len(content.encode("utf-8")) for _, content in source_files) > 8 * 1024 * 1024:
            result["errors"].append({"message": "源码分析超过文件数或总大小上限", "detector": str(check)})
            source_files = []
        matcher = None
        if check == "regex":
            try:
                expression = config.get("regex") or config.get("pattern")
                if not isinstance(expression, str) or not expression.strip():
                    raise ValueError("正则分析器需要 regex 或 pattern")
                matcher = re.compile(expression, re.MULTILINE)
            except (ValueError, re.error) as error:
                result["errors"].append({"message": str(error), "detector": "regex"})
                source_files = []
        for path, content in source_files:
            language = SUPPORTED_LANGUAGES.get(PurePosixPath(path).suffix.lower())
            globs = config.get("path_globs", ["**"])
            if globs and not any(fnmatchcase(path, glob) for glob in globs):
                continue
            if language is None and check != "regex":
                result["errors"].append({"path": path, "detector": str(check), "message": "不支持的源码语言"})
                continue
            if len(content.encode("utf-8")) > 2 * 1024 * 1024:
                result["errors"].append({"path": path, "detector": str(check), "message": "单文件超过分析上限"})
                continue
            result["analyzed_files"].append(path)
            if matcher is not None:
                for match in list(matcher.finditer(content))[:100]:
                    result["findings"].append({"path": path, "line": content.count("\n", 0, match.start()) + 1,
                        "message": str(config.get("message") or "源码命中禁止模式"), "detector": "regex",
                        "language": language or "text", "match": match.group(0)[:500]})
                continue
            try:
                elements, diagnostics, components, events = _source_analysis(path, content)
            except (ImportError, AttributeError, TypeError, ValueError) as error:
                result["errors"].append({"path": path, "detector": str(check), "language": language,
                                         "message": "源码分析器不可用：" + str(error)})
                continue
            if diagnostics:
                key = "findings" if check == "syntax" else "errors"
                result[key].extend({"path": path, "detector": str(check), "language": language, **item}
                                   for item in diagnostics[:100])
                continue
            records = [_record(element, elements, path=path, language=language) for element in elements]
            result["items"].extend(record for record in records if _selected(record, config))
            result["items"].extend({"path": path, "language": language, **item} for item in components + events)
            if check in {"a11y_name", "event_binding"}:
                for record in records:
                    interactive = record["tag"] in _INTERACTIVE or record["role"] in _ROLES or bool(record["events"])
                    if not interactive or not _selected(record, config):
                        continue
                    if record["tag"] == "input" and record["attributes"].get("type") == "hidden":
                        continue
                    if check == "a11y_name" and record["name_dynamic"]:
                        result["errors"].append({"path": path, "line": record["line"], "language": language,
                            "detector": str(check), "message": "可访问名称依赖运行时表达式，需页面或模型检查"})
                    elif check == "a11y_name" and not record["name"]:
                        result["findings"].append({"path": path, "line": record["line"], "language": language,
                            "detector": str(check), "message": "可操作元素缺少静态可确定的可访问名称", "tag": record["tag"]})
                    elif check == "event_binding" and not record["events"]:
                        result["errors"].append({"path": path, "line": record["line"], "language": language,
                            "detector": str(check), "message": "目标元素没有显式事件绑定；需要核对原生行为和委托事件", "tag": record["tag"]})
        if any(config.get(key) is not None for key in ("tag", "role", "name")) and not any(item.get("kind") == "element" for item in result["items"]) and not result["errors"]:
            result["errors"].append({"message": "没有匹配所选元素的源码证据", "detector": str(check)})
        if not result["analyzed_files"] and not result["errors"]:
            result["errors"].append({"message": "没有符合分析范围的源码文件", "detector": str(check)})
    result["items"] = result["items"][:500]
    result["findings"] = result["findings"][:200]
    result["errors"] = result["errors"][:100]
    result["fallback_required"] = bool(result["errors"])
    result["status"] = "error" if result["errors"] else "fail" if result["findings"] else "pass"
    return result
