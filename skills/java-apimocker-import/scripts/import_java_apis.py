#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

TARGET_MODULES = ["exam-ms-edge", "exam-ms-edge-app", "exam-ms-todmanage"]
SCHEMA_MODULES = TARGET_MODULES + ["exam-ms-common"]
DEFAULT_REPO_ROOT = "/Users/ccy/exam"
DEFAULT_CONFIG_PATH = "/Users/ccy/.agents/skills/java-apimocker-import/config.json"
MAX_WARNING_OUTPUT = 50


@dataclass
class MethodParam:
    name: str
    type_name: str
    annotations: List[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class Endpoint:
    method: str
    path: str
    file: str
    line: int
    class_name: str
    method_name: str
    module: str
    return_type: str = "Object"
    params: List[MethodParam] = field(default_factory=list)
    package_name: str = ""
    imports: Dict[str, str] = field(default_factory=dict)


@dataclass
class JavaField:
    name: str
    type_name: str
    description: Optional[str] = None
    required: bool = False


@dataclass
class JavaClassDef:
    simple_name: str
    fqcn: str
    file: str
    package_name: str
    imports: Dict[str, str]
    fields: List[JavaField] = field(default_factory=list)
    extends_type: Optional[str] = None
    type_params: List[str] = field(default_factory=list)


JAVA_TYPE_TO_SCHEMA: Dict[str, Dict[str, str]] = {
    "byte": {"type": "integer", "format": "int32"},
    "short": {"type": "integer", "format": "int32"},
    "int": {"type": "integer", "format": "int32"},
    "long": {"type": "integer", "format": "int64"},
    "float": {"type": "number", "format": "float"},
    "double": {"type": "number", "format": "double"},
    "boolean": {"type": "boolean"},
    "char": {"type": "string"},
    "Byte": {"type": "integer", "format": "int32"},
    "Short": {"type": "integer", "format": "int32"},
    "Integer": {"type": "integer", "format": "int32"},
    "Long": {"type": "integer", "format": "int64"},
    "Float": {"type": "number", "format": "float"},
    "Double": {"type": "number", "format": "double"},
    "Boolean": {"type": "boolean"},
    "Character": {"type": "string"},
    "String": {"type": "string"},
    "BigDecimal": {"type": "number"},
    "BigInteger": {"type": "integer"},
    "Date": {"type": "string", "format": "date-time"},
    "LocalDate": {"type": "string", "format": "date"},
    "LocalDateTime": {"type": "string", "format": "date-time"},
    "LocalTime": {"type": "string"},
    "Instant": {"type": "string", "format": "date-time"},
    "Object": {"type": "object"},
}

COLLECTION_TYPES = {"List", "Set", "Collection", "Iterable", "ArrayList", "LinkedList"}
MAP_TYPES = {"Map", "HashMap", "LinkedHashMap", "ConcurrentHashMap"}
WRAPPER_TYPES = {"Result", "ResponseEntity", "Optional"}
PARAMLESS_MODIFIERS = {"final"}
METHOD_MODIFIERS = {"static", "final", "synchronized", "abstract", "default", "native", "strictfp"}


def normalize_path(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "/"
    value = value.replace('"', "").replace("'", "")
    if not value.startswith("/"):
        value = "/" + value
    value = re.sub(r"/+", "/", value)
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


def join_paths(class_path: str, method_path: str) -> str:
    class_norm = normalize_path(class_path)
    method_norm = normalize_path(method_path)
    if class_norm == "/" and method_norm == "/":
        return "/"
    if class_norm == "/":
        return method_norm
    if method_norm == "/":
        return class_norm
    return normalize_path(f"{class_norm}/{method_norm.lstrip('/')}")


def strip_comments(line: str) -> str:
    idx = line.find("//")
    return line if idx < 0 else line[:idx]


def parse_package_and_imports(lines: List[str]) -> Tuple[str, Dict[str, str]]:
    package_name = ""
    imports: Dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if line.startswith("package "):
            m = re.match(r"package\s+([\w\.]+)\s*;", line)
            if m:
                package_name = m.group(1)
        elif line.startswith("import "):
            m = re.match(r"import\s+([\w\.\*]+)\s*;", line)
            if m:
                fqcn = m.group(1)
                if not fqcn.endswith(".*"):
                    imports[fqcn.split(".")[-1]] = fqcn
    return package_name, imports


def read_annotation_block(lines: List[str], start_index: int) -> Tuple[str, int]:
    buf = []
    depth = 0
    i = start_index
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        seg = strip_comments(raw)
        buf.append(seg.strip())
        depth += seg.count("(") - seg.count(")")
        i += 1
        if depth <= 0 and ")" in seg:
            break
        if depth <= 0 and "(" not in "".join(buf):
            break
    return " ".join([x for x in buf if x]), i


def extract_annotation_name(annotation_text: str) -> str:
    m = re.search(r"@(\w+)", annotation_text)
    return m.group(1) if m else ""


def annotation_args(annotation_text: str) -> str:
    start = annotation_text.find("(")
    end = annotation_text.rfind(")")
    if start < 0 or end < 0 or end <= start:
        return ""
    return annotation_text[start + 1 : end]


def extract_paths(args: str) -> List[str]:
    args = args.strip()
    if not args:
        return ["/"]

    paths: List[str] = []
    kv_matches = re.finditer(r"(?:^|,)\s*(value|path)\s*=\s*(\{[^}]*\}|\"[^\"]*\"|'[^']*')", args)
    for match in kv_matches:
        value_expr = match.group(2)
        literals = re.findall(r"\"([^\"]*)\"|'([^']*)'", value_expr)
        for a, b in literals:
            literal = a or b
            if literal is not None:
                paths.append(normalize_path(literal))

    if paths:
        return list(dict.fromkeys(paths))

    if "=" not in args:
        literals = re.findall(r"\"([^\"]*)\"|'([^']*)'", args)
        for a, b in literals:
            literal = a or b
            if literal is not None:
                paths.append(normalize_path(literal))

    if not paths:
        return ["/"]
    return list(dict.fromkeys(paths))


def extract_methods(annotation_name: str, args: str) -> Tuple[List[str], bool]:
    direct = {
        "GetMapping": ["GET"],
        "PostMapping": ["POST"],
        "PutMapping": ["PUT"],
        "DeleteMapping": ["DELETE"],
        "PatchMapping": ["PATCH"],
    }
    if annotation_name in direct:
        return direct[annotation_name], False

    if annotation_name != "RequestMapping":
        return [], False

    methods = re.findall(r"RequestMethod\.([A-Z]+)", args)
    methods = [m.upper() for m in methods]
    if methods:
        return list(dict.fromkeys(methods)), False

    return [], True


def parse_mapping_annotation(annotation_text: str) -> Tuple[str, List[str], List[str], bool]:
    name = extract_annotation_name(annotation_text)
    args = annotation_args(annotation_text)
    paths = extract_paths(args)
    methods, missing_method = extract_methods(name, args)
    return name, paths, methods, missing_method


def split_top_level(text: str, delim: str = ",") -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    angle = 0
    paren = 0
    bracket = 0
    brace = 0
    in_string = False
    quote_char = ""

    for ch in text:
        if in_string:
            buf.append(ch)
            if ch == quote_char:
                in_string = False
            continue

        if ch in {'"', "'"}:
            in_string = True
            quote_char = ch
            buf.append(ch)
            continue

        if ch == "<":
            angle += 1
        elif ch == ">" and angle > 0:
            angle -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")" and paren > 0:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket > 0:
            bracket -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace > 0:
            brace -= 1

        if ch == delim and angle == 0 and paren == 0 and bracket == 0 and brace == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue

        buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def strip_param_annotations(raw: str) -> str:
    s = raw
    out: List[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "@":
            i += 1
            while i < len(s) and (s[i].isalnum() or s[i] in {"_", "."}):
                i += 1
            while i < len(s) and s[i].isspace():
                i += 1
            if i < len(s) and s[i] == "(":
                depth = 1
                i += 1
                while i < len(s) and depth > 0:
                    if s[i] == "(":
                        depth += 1
                    elif s[i] == ")":
                        depth -= 1
                    i += 1
            while i < len(s) and s[i].isspace():
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def read_method_signature(lines: List[str], start_index: int) -> str:
    buf: List[str] = []
    depth = 0
    seen_open = False
    i = start_index
    guard = 0
    while i < len(lines) and guard < 40:
        guard += 1
        seg = strip_comments(lines[i]).strip()
        if seg:
            buf.append(seg)
        if "(" in seg:
            seen_open = True
        depth += seg.count("(") - seg.count(")")

        joined = " ".join(buf)
        if seen_open and depth <= 0 and ")" in joined:
            if "{" in seg or ";" in seg:
                break
            if i + 1 < len(lines):
                nxt = strip_comments(lines[i + 1]).strip()
                if nxt.startswith("throws ") or nxt.startswith("{") or nxt.endswith("{"):
                    if nxt:
                        buf.append(nxt)
                    break
            break

        i += 1

    return re.sub(r"\s+", " ", " ".join(buf)).strip()


def parse_method_signature(lines: List[str], start_index: int) -> Tuple[Optional[str], str, List[MethodParam]]:
    sig = read_method_signature(lines, start_index)
    if "(" not in sig or ")" not in sig:
        return None, "Object", []

    left, right = sig.split("(", 1)
    params_part = right.rsplit(")", 1)[0].strip()

    left = left.strip()
    m = re.search(r"\b(public|protected|private)\b\s+(.*)$", left)
    if not m:
        return None, "Object", []

    tail = m.group(2).strip()
    tokens = tail.split()
    while tokens and tokens[0] in METHOD_MODIFIERS:
        tokens.pop(0)
    if len(tokens) < 2:
        return None, "Object", []

    method_name = tokens[-1]
    return_type = " ".join(tokens[:-1]).strip()
    return_type = re.sub(r"@\w+(?:\s*\([^)]*\))?", "", return_type).strip() or "Object"

    params: List[MethodParam] = []
    if params_part:
        for idx, raw_part in enumerate(split_top_level(params_part, ","), start=1):
            annotations = re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", raw_part)
            clean = strip_param_annotations(raw_part)
            clean = re.sub(r"\b(?:final)\b", "", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            if not clean:
                continue

            clean = clean.replace("...", "[]")
            pm = re.search(r"(.+?)\s+([A-Za-z_][A-Za-z0-9_]*)$", clean)
            if pm:
                type_name = pm.group(1).strip()
                name = pm.group(2).strip()
            else:
                type_name = clean
                name = f"param{idx}"

            params.append(MethodParam(name=name, type_name=type_name, annotations=annotations, raw=raw_part.strip()))

    return method_name, return_type, params


def find_controller_files(repo_root: Path) -> List[Path]:
    files: List[Path] = []
    for module in TARGET_MODULES:
        module_dir = repo_root / module
        if not module_dir.exists():
            continue
        files.extend(sorted(module_dir.rglob("*Controller.java")))
    return files


def module_from_path(path: Path) -> str:
    p = str(path)
    for module in TARGET_MODULES:
        marker = f"/{module}/"
        if marker in p:
            return module
    return "unknown"


def parse_controller_file(path: Path) -> Tuple[List[Endpoint], List[str]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    package_name, imports = parse_package_and_imports(lines)

    endpoints: List[Endpoint] = []
    warnings: List[str] = []

    class_name = ""
    class_paths = ["/"]
    pending_annotations: List[Tuple[str, int]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("@") and ("Mapping(" in stripped or stripped.startswith("@RequestMapping")):
            ann_text, next_index = read_annotation_block(lines, i)
            pending_annotations.append((ann_text, i + 1))
            i = next_index
            continue

        class_match = re.search(r"\bclass\s+(\w+)", line)
        if class_match:
            class_name = class_match.group(1)
            class_level_paths: List[str] = []
            for ann_text, _ in pending_annotations:
                ann_name, paths, _, _ = parse_mapping_annotation(ann_text)
                if ann_name == "RequestMapping":
                    class_level_paths.extend(paths)
            class_paths = class_level_paths or ["/"]
            pending_annotations = []
            i += 1
            continue

        method_match = re.search(r"\b(public|protected|private)\b[^;]*\(", line)
        if method_match and class_name:
            parsed_method_name, return_type, params = parse_method_signature(lines, i)
            method_name = parsed_method_name or "unknownMethod"
            method_line = i + 1

            if pending_annotations:
                for ann_text, ann_line in pending_annotations:
                    ann_name, paths, methods, missing_method = parse_mapping_annotation(ann_text)
                    if ann_name not in {
                        "GetMapping",
                        "PostMapping",
                        "PutMapping",
                        "DeleteMapping",
                        "PatchMapping",
                        "RequestMapping",
                    }:
                        continue

                    if ann_name == "RequestMapping" and missing_method:
                        warnings.append(
                            f"skip {path}:{ann_line} -> @RequestMapping missing method on {class_name}.{method_name}"
                        )
                        continue

                    if not methods:
                        continue

                    for cp in class_paths:
                        for mp in paths:
                            full = join_paths(cp, mp)
                            for m in methods:
                                endpoints.append(
                                    Endpoint(
                                        method=m,
                                        path=full,
                                        file=str(path),
                                        line=method_line,
                                        class_name=class_name,
                                        method_name=method_name,
                                        module=module_from_path(path),
                                        return_type=return_type,
                                        params=params,
                                        package_name=package_name,
                                        imports=imports,
                                    )
                                )
            pending_annotations = []
            i += 1
            continue

        if stripped and not stripped.startswith("@"):
            if pending_annotations and not stripped.startswith("*") and not stripped.startswith("/"):
                if not re.search(r"\b(class|public|protected|private)\b", stripped):
                    pending_annotations = []

        i += 1

    return endpoints, warnings


def parse_all_endpoints(repo_root: Path) -> Tuple[List[Endpoint], List[str]]:
    all_eps: List[Endpoint] = []
    all_warns: List[str] = []
    for file_path in find_controller_files(repo_root):
        eps, warns = parse_controller_file(file_path)
        all_eps.extend(eps)
        all_warns.extend(warns)

    uniq = {}
    for ep in all_eps:
        key = (ep.method, ep.path, ep.file, ep.line, ep.class_name, ep.method_name, ep.module)
        uniq[key] = ep
    return list(uniq.values()), all_warns


def warning_payload(warnings: List[str], limit: int = MAX_WARNING_OUTPUT) -> Dict[str, object]:
    out = {
        "warningCount": len(warnings),
        "warnings": warnings[:limit],
    }
    if len(warnings) > limit:
        out["warningTruncated"] = True
    return out


def parse_input_filter(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    s = value.strip()
    anno = re.search(
        r"@(?:RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\s*\((.*)\)",
        s,
        flags=re.IGNORECASE,
    )
    if anno:
        name_match = re.search(r"@(\w+)", s)
        name = name_match.group(1) if name_match else ""
        args = anno.group(1)
        paths = extract_paths(args)
        path = normalize_path(paths[0] if paths else "/")

        method = None
        direct_name = name.lower()
        direct_map = {
            "getmapping": "GET",
            "postmapping": "POST",
            "putmapping": "PUT",
            "deletemapping": "DELETE",
            "patchmapping": "PATCH",
        }
        if direct_name in direct_map:
            method = direct_map[direct_name]
        elif direct_name == "requestmapping":
            methods = re.findall(r"RequestMethod\.([A-Z]+)", args)
            if methods:
                method = methods[0].upper()

        return method, path

    return None, normalize_path(s)


def filter_endpoints(endpoints: List[Endpoint], input_value: Optional[str]) -> List[Endpoint]:
    method_filter, path_filter = parse_input_filter(input_value)
    if not path_filter:
        return endpoints
    result = []
    for ep in endpoints:
        if ep.path != path_filter:
            continue
        if method_filter and ep.method != method_filter:
            continue
        result.append(ep)
    return result


def clean_type_expr(type_name: str) -> str:
    s = re.sub(r"\s+", " ", (type_name or "Object").strip())
    s = s.replace("? extends ", "").replace("? super ", "")
    return s or "Object"


def split_base_and_args(type_name: str) -> Tuple[str, List[str], bool]:
    t = clean_type_expr(type_name)
    is_array = t.endswith("[]")
    while t.endswith("[]"):
        t = t[:-2].strip()

    lt = t.find("<")
    if lt < 0:
        return t, [], is_array

    gt = t.rfind(">")
    if gt < 0 or gt < lt:
        return t, [], is_array

    base = t[:lt].strip()
    arg_str = t[lt + 1 : gt].strip()
    args = split_top_level(arg_str, ",") if arg_str else []
    return base, args, is_array


def simple_name(type_name: str) -> str:
    return clean_type_expr(type_name).split(".")[-1]


def sanitize_component_name(value: str) -> str:
    x = re.sub(r"[^A-Za-z0-9_]", "_", value)
    x = re.sub(r"_+", "_", x).strip("_")
    return x or "Anonymous"


def substitute_generic(type_expr: str, generic_ctx: Dict[str, str]) -> str:
    out = type_expr
    for k, v in generic_ctx.items():
        out = re.sub(rf"\b{re.escape(k)}\b", v, out)
    return out


def parse_javadoc(lines: List[str], start_index: int) -> Tuple[str, int]:
    parts: List[str] = []
    i = start_index
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("/**"):
            line = line[3:]
        if line.endswith("*/"):
            line = line[:-2]
            line = re.sub(r"^\*", "", line).strip()
            if line:
                parts.append(line)
            return " ".join(parts).strip(), i + 1
        line = re.sub(r"^\*", "", line).strip()
        if line:
            parts.append(line)
        i += 1
    return " ".join(parts).strip(), i


def extract_description_from_annotation(line: str) -> Optional[str]:
    if "@ApiModelProperty" not in line and "@Schema" not in line:
        return None
    args = annotation_args(line)
    if not args:
        return None
    m = re.search(r"(?:value|description)\s*=\s*\"([^\"]+)\"", args)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"\"([^\"]+)\"", args)
    if m2:
        return m2.group(1).strip()
    return None


def is_builtin_type(type_name: str) -> bool:
    b, _, is_array = split_base_and_args(type_name)
    s = simple_name(b)
    return is_array or s in JAVA_TYPE_TO_SCHEMA


class JavaClassIndex:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._built = False
        self.by_simple: Dict[str, List[Path]] = {}
        self.by_fqcn: Dict[str, Path] = {}
        self.class_cache: Dict[str, JavaClassDef] = {}

    def _iter_java_files(self):
        seen: Set[str] = set()
        for module in SCHEMA_MODULES:
            module_dir = self.repo_root / module
            if not module_dir.exists():
                continue
            for p in module_dir.rglob("*.java"):
                sp = str(p)
                if sp not in seen:
                    seen.add(sp)
                    yield p

    def _extract_package_and_class(self, text: str) -> Tuple[str, Optional[str]]:
        package_name = ""
        m_pkg = re.search(r"\bpackage\s+([\w\.]+)\s*;", text)
        if m_pkg:
            package_name = m_pkg.group(1)

        m_cls = re.search(r"\bclass\s+(\w+)\b", text)
        if not m_cls:
            m_cls = re.search(r"\binterface\s+(\w+)\b", text)
        if not m_cls:
            m_cls = re.search(r"\benum\s+(\w+)\b", text)
        cls = m_cls.group(1) if m_cls else None
        return package_name, cls

    def _build_if_needed(self):
        if self._built:
            return
        for p in self._iter_java_files():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            package_name, cls = self._extract_package_and_class(text)
            if not cls:
                continue
            self.by_simple.setdefault(cls, []).append(p)
            if package_name:
                self.by_fqcn[f"{package_name}.{cls}"] = p
        self._built = True

    def resolve_class_file(self, base_type: str, imports: Dict[str, str], package_name: str) -> Optional[Path]:
        self._build_if_needed()

        bt = clean_type_expr(base_type)
        sname = simple_name(bt)

        if sname in JAVA_TYPE_TO_SCHEMA:
            return None

        if bt in self.by_fqcn:
            return self.by_fqcn[bt]

        if "." in bt:
            fq = bt
            if fq in self.by_fqcn:
                return self.by_fqcn[fq]
            sname = fq.split(".")[-1]

        if sname in imports:
            fq = imports[sname]
            if fq in self.by_fqcn:
                return self.by_fqcn[fq]

        if package_name:
            fq = f"{package_name}.{sname}"
            if fq in self.by_fqcn:
                return self.by_fqcn[fq]

        candidates = self.by_simple.get(sname) or []
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            # 优先 common，其次 todmanage/edge
            ranked = sorted(candidates, key=lambda x: ("/exam-ms-common/" not in str(x), len(str(x))))
            return ranked[0]
        return None

    def parse_class_def(self, file_path: Path) -> Optional[JavaClassDef]:
        key = str(file_path)
        if key in self.class_cache:
            return self.class_cache[key]

        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            self.class_cache[key] = None  # type: ignore
            return None

        package_name, imports = parse_package_and_imports(lines)

        class_name = file_path.stem
        extends_type: Optional[str] = None
        type_params: List[str] = []

        class_decl_re = re.compile(
            r"\bclass\s+(\w+)\s*(?:<([^>{}]*)>)?(?:\s+extends\s+([^\{]+?))?(?:\s+implements\s+[^\{]+)?\{?"
        )

        found_decl = False
        fields: List[JavaField] = []
        depth = 0
        pending_desc: Optional[str] = None
        pending_required = False

        i = 0
        while i < len(lines):
            raw = lines[i]
            stripped = raw.strip()

            if stripped.startswith("/**"):
                desc, nxt = parse_javadoc(lines, i)
                if desc:
                    pending_desc = desc
                i = nxt
                continue

            if not found_decl:
                m_decl = class_decl_re.search(stripped)
                if m_decl:
                    found_decl = True
                    class_name = m_decl.group(1)
                    if m_decl.group(2):
                        type_params = [x.strip() for x in split_top_level(m_decl.group(2), ",") if x.strip()]
                    ext = (m_decl.group(3) or "").strip()
                    if ext:
                        ext = ext.split("implements", 1)[0].strip()
                        extends_type = ext

            ann_desc = extract_description_from_annotation(stripped)
            if ann_desc:
                pending_desc = ann_desc
            if re.search(r"@NotNull\b", stripped):
                pending_required = True

            if found_decl and depth == 1 and stripped and ";" in stripped and "(" not in stripped:
                m_field = re.search(
                    r"(?:private|protected|public)\s+((?:(?:static|final|transient|volatile)\s+)*)"
                    r"([A-Za-z0-9_\.<>,\[\] ?$]+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=[^;]*)?;",
                    stripped,
                )
                if m_field:
                    mods = m_field.group(1).strip().split() if m_field.group(1) else []
                    type_name = clean_type_expr(m_field.group(2))
                    field_name = m_field.group(3)
                    if "static" not in mods and field_name != "serialVersionUID":
                        fields.append(
                            JavaField(
                                name=field_name,
                                type_name=type_name,
                                description=pending_desc,
                                required=pending_required,
                            )
                        )
                    pending_desc = None
                    pending_required = False

            open_count = raw.count("{")
            close_count = raw.count("}")
            depth += open_count - close_count

            if stripped and not stripped.startswith("@") and not stripped.startswith("*") and not stripped.startswith("/"):
                if ";" not in stripped:
                    pending_required = False

            i += 1

        fqcn = f"{package_name}.{class_name}" if package_name else class_name
        out = JavaClassDef(
            simple_name=class_name,
            fqcn=fqcn,
            file=str(file_path),
            package_name=package_name,
            imports=imports,
            fields=fields,
            extends_type=extends_type,
            type_params=type_params,
        )
        self.class_cache[key] = out
        return out


class SwaggerGenerator:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.class_index = JavaClassIndex(repo_root)
        self.definitions: Dict[str, Dict[str, object]] = {}
        self._building_definitions: Set[str] = set()

    def endpoint_swagger(self, ep: Endpoint) -> Dict[str, object]:
        self.definitions = {}
        self._building_definitions = set()
        paths: Dict[str, object] = {}
        op = self._build_operation(ep)
        paths.setdefault(ep.path, {})[ep.method.lower()] = op
        return {
            "swagger": "2.0",
            "info": {"title": f"{ep.class_name} API", "version": "1.0.0"},
            "paths": paths,
            "produces": ["application/json"],
            "definitions": self.definitions,
        }

    def combined_swagger(self, endpoints: List[Endpoint], title: str = "Auto Parsed APIs") -> Dict[str, object]:
        self.definitions = {}
        self._building_definitions = set()
        paths: Dict[str, Dict[str, object]] = {}
        for ep in sorted(endpoints, key=lambda x: (x.path, x.method, x.file, x.line)):
            op = self._build_operation(ep)
            if ep.path not in paths:
                paths[ep.path] = {}
            paths[ep.path][ep.method.lower()] = op

        return {
            "swagger": "2.0",
            "info": {"title": title, "version": "1.0.0"},
            "paths": paths,
            "produces": ["application/json"],
            "definitions": self.definitions,
        }

    def _build_operation(self, ep: Endpoint) -> Dict[str, object]:
        parameters: List[Dict[str, object]] = []
        has_body = False

        for p in ep.params:
            pin = self._detect_param_in(p.annotations)
            explicit_name = self._extract_annotation_name_override(p.raw)
            pname = explicit_name or p.name

            if pin == "body":
                schema = self._schema_for_type(p.type_name, ep.imports, ep.package_name, {})
                parameters.append(
                    {
                        "name": pname,
                        "in": "body",
                        "required": True,
                        "schema": schema,
                    }
                )
                has_body = True
                continue

            if pin in {"query", "path", "header", "cookie"}:
                schema = self._schema_for_type(p.type_name, ep.imports, ep.package_name, {})
                parameter_item = {
                    "name": pname,
                    "in": pin,
                    "required": pin == "path",
                }
                parameter_item.update(self._schema_to_swagger2_parameter_fields(schema))
                parameters.append(parameter_item)
                continue

            if ep.method in {"POST", "PUT", "PATCH"} and not is_builtin_type(p.type_name):
                schema = self._schema_for_type(p.type_name, ep.imports, ep.package_name, {})
                parameters.append(
                    {
                        "name": pname,
                        "in": "body",
                        "required": True,
                        "schema": schema,
                    }
                )
                has_body = True
                continue

            if is_builtin_type(p.type_name):
                schema = self._schema_for_type(p.type_name, ep.imports, ep.package_name, {})
                parameter_item = {
                    "name": pname,
                    "in": "query",
                    "required": False,
                }
                parameter_item.update(self._schema_to_swagger2_parameter_fields(schema))
                parameters.append(parameter_item)
            else:
                expanded = self._expand_object_query_params(p.type_name, ep.imports, ep.package_name)
                if expanded:
                    parameters.extend(expanded)
                else:
                    parameters.append(
                        {
                            "name": pname,
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    )

        resp_schema = self._schema_for_type(ep.return_type, ep.imports, ep.package_name, {})

        operation = {
            "operationId": f"{ep.class_name}_{ep.method_name}",
            "summary": f"{ep.method} {ep.path}",
            "tags": [ep.class_name],
            "produces": ["application/json"],
            "responses": {
                "200": {
                    "description": "OK",
                    "schema": resp_schema,
                }
            },
        }
        if has_body:
            operation["consumes"] = ["application/json"]
        if parameters:
            operation["parameters"] = parameters
        return operation

    def _detect_param_in(self, annos: List[str]) -> str:
        lower = {a.lower() for a in annos}
        if "requestbody" in lower:
            return "body"
        if "pathvariable" in lower:
            return "path"
        if "requestheader" in lower:
            return "header"
        if "cookievalue" in lower:
            return "cookie"
        if "requestparam" in lower or "modelattribute" in lower:
            return "query"
        return "auto"

    def _extract_annotation_name_override(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        patterns = [
            r"@(?:RequestParam|PathVariable|RequestHeader|CookieValue)\s*\(\s*(?:value|name)\s*=\s*\"([^\"]+)\"",
            r"@(?:RequestParam|PathVariable|RequestHeader|CookieValue)\s*\(\s*\"([^\"]+)\"",
            r"@(?:RequestParam|PathVariable|RequestHeader|CookieValue)\s*\(\s*(?:value|name)\s*=\s*'([^']+)'",
            r"@(?:RequestParam|PathVariable|RequestHeader|CookieValue)\s*\(\s*'([^']+)'",
        ]
        for p in patterns:
            m = re.search(p, raw)
            if m:
                return m.group(1).strip()
        return None

    def _coerce_parameter_schema(self, schema: Dict[str, object]) -> Dict[str, object]:
        if "$ref" in schema:
            return {"type": "string"}
        t = schema.get("type")
        if t in {"object", "array"}:
            return {"type": "string"}
        return schema

    def _schema_to_swagger2_parameter_fields(self, schema: Dict[str, object]) -> Dict[str, object]:
        s = self._coerce_parameter_schema(schema)
        t = str(s.get("type") or "string")
        out: Dict[str, object] = {"type": t}
        fmt = s.get("format")
        if fmt:
            out["format"] = fmt
        if t == "array":
            items = s.get("items")
            if isinstance(items, dict):
                if "$ref" in items:
                    out["items"] = {"type": "string"}
                else:
                    item_type = items.get("type")
                    if item_type:
                        item_out: Dict[str, object] = {"type": item_type}
                        if items.get("format"):
                            item_out["format"] = items["format"]
                        out["items"] = item_out
                    else:
                        out["items"] = {"type": "string"}
            else:
                out["items"] = {"type": "string"}
            out.setdefault("collectionFormat", "multi")
        if "enum" in s:
            out["enum"] = s["enum"]
        return out

    def _expand_object_query_params(
        self,
        type_name: str,
        imports: Dict[str, str],
        package_name: str,
    ) -> List[Dict[str, object]]:
        base, args, is_array = split_base_and_args(type_name)
        if is_array:
            return []

        class_file = self.class_index.resolve_class_file(base, imports, package_name)
        if not class_file:
            return []

        class_def = self.class_index.parse_class_def(class_file)
        if not class_def:
            return []

        generic_ctx = self._build_generic_ctx(class_def, args, imports, package_name, {})
        fields = self._collect_all_fields(class_def, generic_ctx, set())

        out: List[Dict[str, object]] = []
        for f in fields:
            schema = self._schema_for_type(f.type_name, class_def.imports, class_def.package_name, generic_ctx)
            item = {
                "name": f.name,
                "in": "query",
                "required": bool(f.required),
            }
            item.update(self._schema_to_swagger2_parameter_fields(schema))
            if f.description:
                item["description"] = f.description
            out.append(item)
        return out

    def _schema_for_type(
        self,
        type_name: str,
        imports: Dict[str, str],
        package_name: str,
        generic_ctx: Dict[str, str],
    ) -> Dict[str, object]:
        t = clean_type_expr(type_name)
        t = substitute_generic(t, generic_ctx)

        if t == "void":
            return {"type": "object"}

        base, args, is_array = split_base_and_args(t)
        bname = simple_name(base)

        if bname in generic_ctx and not args:
            return self._schema_for_type(generic_ctx[bname], imports, package_name, generic_ctx)

        if bname in WRAPPER_TYPES and args:
            return self._schema_for_type(args[0], imports, package_name, generic_ctx)

        if bname in COLLECTION_TYPES:
            inner = args[0] if args else "Object"
            arr = {"type": "array", "items": self._schema_for_type(inner, imports, package_name, generic_ctx)}
            return arr

        if bname in MAP_TYPES:
            val = args[1] if len(args) > 1 else "Object"
            return {
                "type": "object",
                "additionalProperties": self._schema_for_type(val, imports, package_name, generic_ctx),
            }

        if is_array:
            return {
                "type": "array",
                "items": self._schema_for_type(base, imports, package_name, generic_ctx),
            }

        if bname in JAVA_TYPE_TO_SCHEMA:
            return dict(JAVA_TYPE_TO_SCHEMA[bname])

        comp_name = self._component_name(base, args, generic_ctx)
        self._ensure_definition(comp_name, base, args, imports, package_name, generic_ctx)
        return {"$ref": f"#/definitions/{comp_name}"}

    def _component_name(self, base: str, args: List[str], generic_ctx: Dict[str, str]) -> str:
        b = sanitize_component_name(simple_name(base))
        if not args:
            return b
        arg_names = []
        for a in args:
            aa = substitute_generic(clean_type_expr(a), generic_ctx)
            ab, aargs, _ = split_base_and_args(aa)
            seg = sanitize_component_name(simple_name(ab))
            if aargs:
                seg = seg + "Arg"
            arg_names.append(seg)
        return sanitize_component_name(f"{b}_Of_{'_'.join(arg_names)}")

    def _build_generic_ctx(
        self,
        class_def: JavaClassDef,
        args: List[str],
        imports: Dict[str, str],
        package_name: str,
        parent_ctx: Dict[str, str],
    ) -> Dict[str, str]:
        ctx: Dict[str, str] = dict(parent_ctx)
        if class_def.type_params:
            for i, tp in enumerate(class_def.type_params):
                if i < len(args):
                    ctx[tp] = substitute_generic(clean_type_expr(args[i]), parent_ctx)
                else:
                    ctx[tp] = "Object"
        return ctx

    def _ensure_definition(
        self,
        comp_name: str,
        base: str,
        args: List[str],
        imports: Dict[str, str],
        package_name: str,
        parent_generic_ctx: Dict[str, str],
    ):
        if comp_name in self.definitions:
            return
        if comp_name in self._building_definitions:
            return

        self._building_definitions.add(comp_name)
        try:
            class_file = self.class_index.resolve_class_file(base, imports, package_name)
            if not class_file:
                self.definitions[comp_name] = {
                    "type": "object",
                    "description": f"Unresolved Java type: {base}",
                }
                return

            class_def = self.class_index.parse_class_def(class_file)
            if not class_def:
                self.definitions[comp_name] = {
                    "type": "object",
                    "description": f"Failed to parse Java type: {base}",
                }
                return

            generic_ctx = self._build_generic_ctx(class_def, args, imports, package_name, parent_generic_ctx)
            all_fields = self._collect_all_fields(class_def, generic_ctx, set())

            properties: Dict[str, object] = {}
            required: List[str] = []
            for f in all_fields:
                schema = self._schema_for_type(f.type_name, class_def.imports, class_def.package_name, generic_ctx)
                if f.description:
                    schema = dict(schema)
                    schema["description"] = f.description
                properties[f.name] = schema
                if f.required:
                    required.append(f.name)

            comp: Dict[str, object] = {
                "type": "object",
                "properties": properties,
            }
            if required:
                comp["required"] = sorted(list(dict.fromkeys(required)))
            self.definitions[comp_name] = comp
        finally:
            self._building_definitions.discard(comp_name)

    def _collect_all_fields(
        self,
        class_def: JavaClassDef,
        generic_ctx: Dict[str, str],
        visited: Set[str],
    ) -> List[JavaField]:
        key = class_def.fqcn
        if key in visited:
            return []
        visited.add(key)

        merged: Dict[str, JavaField] = {}

        if class_def.extends_type:
            p_base, p_args, _ = split_base_and_args(substitute_generic(class_def.extends_type, generic_ctx))
            p_file = self.class_index.resolve_class_file(p_base, class_def.imports, class_def.package_name)
            if p_file:
                p_def = self.class_index.parse_class_def(p_file)
                if p_def:
                    p_ctx = self._build_generic_ctx(p_def, p_args, class_def.imports, class_def.package_name, generic_ctx)
                    for pf in self._collect_all_fields(p_def, p_ctx, visited):
                        merged[pf.name] = pf

        for f in class_def.fields:
            resolved_type = substitute_generic(f.type_name, generic_ctx)
            merged[f.name] = JavaField(
                name=f.name,
                type_name=resolved_type,
                description=f.description,
                required=f.required,
            )

        return list(merged.values())


def endpoints_to_locate_output(endpoints: List[Endpoint], swaggers: Dict[Tuple[str, str, str, int], Dict[str, object]]) -> List[Dict[str, object]]:
    out = []
    for ep in sorted(endpoints, key=lambda x: (x.path, x.method, x.file, x.line)):
        key = (ep.method, ep.path, ep.file, ep.line)
        sw = swaggers.get(key) or {}
        out.append(
            {
                "method": ep.method,
                "path": ep.path,
                "file": ep.file,
                "line": ep.line,
                "className": ep.class_name,
                "methodName": ep.method_name,
                "module": ep.module,
                "requestType": [p.type_name for p in ep.params],
                "responseType": ep.return_type,
                "swagger": sw,
                "swaggerJson": json.dumps(sw, ensure_ascii=False),
            }
        )
    return out


def load_config(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def resolve_project(config: Dict[str, object], project_name: Optional[str]) -> Dict[str, object]:
    projects = config.get("projects") or []
    if not isinstance(projects, list):
        raise ValueError("config.projects must be a list")

    if project_name:
        for p in projects:
            if isinstance(p, dict) and p.get("name") == project_name:
                return p
        raise ValueError(f"project '{project_name}' not found in config")

    default_project = config.get("defaultProject")
    if default_project:
        for p in projects:
            if isinstance(p, dict) and p.get("name") == default_project:
                return p

    if len(projects) == 1 and isinstance(projects[0], dict):
        return projects[0]

    available = [p.get("name") for p in projects if isinstance(p, dict)]
    raise ValueError(
        "project not specified. Use --project. available=" + ", ".join([str(x) for x in available if x])
    )


def build_upload_url(project: Dict[str, object]) -> str:
    base = str(project.get("uploadBaseUrl") or "").strip().rstrip("/")
    template = str(project.get("uploadPathTemplate") or "/open/api/autoImport/:groupId").strip()
    group_id = str(project.get("groupId") or "").strip()
    if not base or not group_id:
        raise ValueError("project.uploadBaseUrl/groupId is required")
    merged = template.replace(":groupId", group_id)
    if not merged.startswith("/"):
        merged = "/" + merged
    return base + merged


def pick_import_type(import_type: str, api_exists: Optional[str]) -> int:
    t = import_type.strip().lower()
    if t in {"0", "2"}:
        return int(t)
    if t != "auto":
        raise ValueError("--import-type must be 0|2|auto")

    if api_exists is not None:
        v = api_exists.strip().lower()
        if v in {"1", "true", "yes", "y"}:
            return 2
        if v in {"0", "false", "no", "n"}:
            return 0

    print("[WARN] importType=auto but API existence not resolved by MCP. Please input importType (0 or 2): ", end="")
    user = input().strip()
    if user not in {"0", "2"}:
        raise ValueError("invalid importType input, expected 0 or 2")
    return int(user)


def build_curl_command(url: str, payload_json: str, timeout_sec: int = 30) -> List[str]:
    return [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        url,
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "-H",
        "Accept: application/json",
        "--max-time",
        str(timeout_sec),
        "--data-binary",
        payload_json,
        "--write-out",
        "\n__HTTP_STATUS__:%{http_code}",
    ]


def post_json(url: str, payload: Dict[str, object], timeout_sec: int = 30) -> Dict[str, object]:
    payload_json = json.dumps(payload, ensure_ascii=False)
    cmd = build_curl_command(url, payload_json, timeout_sec=timeout_sec)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 5, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"upload failed: curl timeout after {timeout_sec}s") from exc
    except OSError as exc:
        raise RuntimeError(f"upload failed: {exc}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"curl exited with code {proc.returncode}"
        raise RuntimeError(f"upload failed: {detail}")

    output = proc.stdout or ""
    marker = "__HTTP_STATUS__:"
    body = output
    status_code = 0
    if marker in output:
        body, status_part = output.rsplit(marker, 1)
        body = body.rstrip("\n")
        status_text = status_part.strip().splitlines()[0] if status_part.strip() else ""
        try:
            status_code = int(status_text)
        except ValueError:
            status_code = 0

    if status_code >= 400:
        raise RuntimeError(f"upload failed HTTP {status_code}: {body}")

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def run_locate(repo_root: Path, input_value: str) -> int:
    endpoints, warnings = parse_all_endpoints(repo_root)
    matched = filter_endpoints(endpoints, input_value)
    warn = warning_payload(warnings)

    sw = SwaggerGenerator(repo_root)
    endpoint_swaggers: Dict[Tuple[str, str, str, int], Dict[str, object]] = {}
    for ep in matched:
        key = (ep.method, ep.path, ep.file, ep.line)
        endpoint_swaggers[key] = sw.endpoint_swagger(ep)

    combined = sw.combined_swagger(matched, title="Locate Matched APIs") if matched else {}

    output = {
        "mode": "locate",
        "input": input_value,
        "matchedCount": len(matched),
        "results": endpoints_to_locate_output(matched, endpoint_swaggers),
        "swagger": combined,
        "swaggerJson": json.dumps(combined, ensure_ascii=False),
        "warningCount": warn["warningCount"],
        "warnings": warn["warnings"],
    }
    if warn.get("warningTruncated"):
        output["warningTruncated"] = True
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def run_import(
    repo_root: Path,
    input_value: Optional[str],
    project_name: Optional[str],
    import_type_raw: str,
    api_exists: Optional[str],
    dry_run: bool,
    config_path: Path,
) -> int:
    endpoints, warnings = parse_all_endpoints(repo_root)
    targets = filter_endpoints(endpoints, input_value)
    warn = warning_payload(warnings)

    if not targets:
        print(
            json.dumps(
                {
                    "mode": "import",
                    "matchedCount": 0,
                    "message": "no controller endpoints matched",
                    "warningCount": warn["warningCount"],
                    "warnings": warn["warnings"],
                    "warningTruncated": bool(warn.get("warningTruncated")),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    config = load_config(config_path)
    user_token = str(config.get("userToken") or "").strip()
    if not user_token:
        raise ValueError("config.userToken is empty")

    project = resolve_project(config, project_name)
    project_token = str(project.get("projectToken") or "").strip()
    if not project_token:
        raise ValueError("project.projectToken is required")

    final_import_type = pick_import_type(import_type_raw, api_exists)
    upload_url = build_upload_url(project)

    sw = SwaggerGenerator(repo_root)
    swagger_doc = sw.combined_swagger(targets, title="Auto Import APIs")
    swagger_json = json.dumps(swagger_doc, ensure_ascii=False)

    unique_paths = list(dict.fromkeys([ep.path for ep in targets]))
    apis_value = unique_paths[0] if len(unique_paths) == 1 else ",".join(unique_paths)

    payload = {
        "importType": final_import_type,
        "token": project_token,
        "apis": apis_value,
        "json": swagger_json,
        "isOrigin": False,
        "userToken": user_token,
    }

    preview = {
        "mode": "import",
        "project": project.get("name"),
        "uploadUrl": upload_url,
        "importType": final_import_type,
        "totalParsed": len(endpoints),
        "toUpload": len(targets),
        "apis": apis_value,
        "isOrigin": False,
        "jsonLength": len(swagger_json),
        "swagger": swagger_doc,
        "warningCount": warn["warningCount"],
        "warnings": warn["warnings"],
    }
    if warn.get("warningTruncated"):
        preview["warningTruncated"] = True

    if dry_run:
        preview["dryRun"] = True
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    result = post_json(upload_url, payload)
    preview["dryRun"] = False
    preview["response"] = result
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Locate Java controller APIs and import to api-mocker")
    parser.add_argument("--mode", choices=["locate", "import"], required=True)
    parser.add_argument("--input", help='@GetMapping("/x") or /x')
    parser.add_argument("--project", help="project name in config (for import mode)")
    parser.add_argument("--import-type", default="auto", help="0|2|auto")
    parser.add_argument("--dry-run", action="store_true", help="preview only, do not upload")
    parser.add_argument("--repo-root", default=DEFAULT_REPO_ROOT, help="repo root path")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="config file path")
    parser.add_argument("--api-exists", help="for import-type auto: true/false resolved by MCP")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve()

    if args.mode == "locate":
        if not args.input:
            parser.error("--input is required for --mode locate")
        return run_locate(repo_root, args.input)

    return run_import(
        repo_root=repo_root,
        input_value=args.input,
        project_name=args.project,
        import_type_raw=args.import_type,
        api_exists=args.api_exists,
        dry_run=args.dry_run,
        config_path=config_path,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
