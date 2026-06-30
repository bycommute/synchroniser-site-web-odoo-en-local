"""
Validation locale légère des fichiers SyncEngine avant push Odoo.

Le but n'est pas de remplacer le rendu Odoo, mais d'attraper tôt les erreurs
qui ont déjà produit des effets de bord: langue incohérente, scripts inline,
pages hors layout/snippets, styles globaux et traductions dont la structure
HTML diverge de la source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import ODOO_CONFIG, PROJECT_ROOT, SYNC_DIRS


SOURCE_LANG = ODOO_CONFIG.get("source_lang", ODOO_CONFIG.get("lang", "fr_FR"))
LANG_SUFFIX_RE = re.compile(r"\.([a-z]{2}_[A-Z]{2})\.[^.]+$")
METADATA_RE = re.compile(r"<!--\s*ODOO-SYNC-METADATA\s*(.*?)\s*-->\s*", re.DOTALL)


@dataclass
class ValidationIssue:
    path: str
    severity: str
    code: str
    message: str


def _parse_metadata_header(content: str) -> Tuple[Dict[str, str], str]:
    match = METADATA_RE.search(content)
    if not match:
        return {}, content

    metadata: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_key, current_lines
        if current_key is not None:
            metadata[current_key] = "\n".join(current_lines).rstrip("\n")
            current_key = None
            current_lines = []

    for raw_line in match.group(1).strip().split("\n"):
        if current_key is not None:
            if raw_line.startswith("  "):
                current_lines.append(raw_line[2:])
                continue
            flush()

        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "|":
            current_key = key
            current_lines = []
            continue
        metadata[key] = value

    flush()
    clean_content = METADATA_RE.sub("", content, count=1)
    return metadata, clean_content


def _filename_lang(path: Path) -> str:
    match = LANG_SUFFIX_RE.search(path.name)
    if match:
        return match.group(1)
    return SOURCE_LANG


def _infer_category(path: Path) -> Optional[str]:
    resolved = path.resolve()
    for category, directory in SYNC_DIRS.items():
        try:
            resolved.relative_to(directory.resolve())
            return category
        except ValueError:
            continue
    return None


def _source_sibling(path: Path) -> Optional[Path]:
    match = LANG_SUFFIX_RE.search(path.name)
    if not match:
        return None
    source_name = path.name[: match.start()] + path.suffix
    direct = path.with_name(source_name)
    if direct.exists():
        return direct

    id_match = re.match(r"^(\d+)-", path.name)
    if not id_match:
        return None
    prefix = id_match.group(1) + "-"
    for candidate in sorted(path.parent.glob(f"{prefix}*{path.suffix}")):
        if candidate == path:
            continue
        if not LANG_SUFFIX_RE.search(candidate.name):
            return candidate
    return None


def _tag_signature(html: str) -> List[str]:
    signature: List[str] = []
    for match in re.finditer(r"<\s*(/)?\s*([a-zA-Z0-9_.:-]+)([^>]*)>", html):
        closing, tag, attrs = match.groups()
        tag = tag.lower()
        if tag.startswith("!--"):
            continue
        if closing:
            signature.append(f"/{tag}")
            continue
        data_snippet = re.search(r'data-snippet=["\']([^"\']+)["\']', attrs or "")
        data_name = re.search(r'data-name=["\']([^"\']+)["\']', attrs or "")
        extra = ""
        if data_snippet:
            extra += f':snippet={data_snippet.group(1)}'
        if data_name:
            extra += f':name={data_name.group(1)}'
        signature.append(f"{tag}{extra}")
    return signature


def _style_targets_global_layout(css: str) -> bool:
    return bool(re.search(r"(^|[{};,\s])(body|html|\.container|\.row|\.col(?:-|[,{:\s]))", css))


def _add(issue_list: List[ValidationIssue], path: Path, severity: str, code: str, message: str) -> None:
    try:
        display_path = str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        display_path = str(path)
    issue_list.append(
        ValidationIssue(
            path=display_path,
            severity=severity,
            code=code,
            message=message,
        )
    )


def validate_file(path: Path, category: Optional[str] = None) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    path = path.resolve()
    category = category or _infer_category(path)

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _add(issues, path, "error", "encoding", "Le fichier doit être en UTF-8.")
        return issues

    metadata, clean_content = _parse_metadata_header(content)
    if not metadata:
        _add(issues, path, "error", "metadata.missing", "Header ODOO-SYNC-METADATA absent.")
    else:
        header_lang = (metadata.get("lang") or SOURCE_LANG).strip()
        expected_lang = _filename_lang(path)
        if header_lang != expected_lang:
            _add(
                issues,
                path,
                "error",
                "lang.mismatch",
                f"Le fichier indique {expected_lang}, mais le header dit {header_lang}.",
            )

    if re.search(r"<\s*script\b", clean_content, flags=re.IGNORECASE):
        _add(issues, path, "warning", "html.script", "Script inline détecté: à éviter dans les contenus poussés par SyncEngine.")
    if re.search(r"\son[a-z]+\s*=", clean_content, flags=re.IGNORECASE):
        _add(issues, path, "warning", "html.inline-handler", "Attribut événement inline détecté (onclick, onload, etc.).")
    if re.search(r"javascript\s*:", clean_content, flags=re.IGNORECASE):
        _add(issues, path, "warning", "html.javascript-url", "URL javascript: détectée.")

    ids = re.findall(r'\sid=["\']([^"\']+)["\']', clean_content)
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        _add(issues, path, "warning", "html.duplicate-id", f"ID HTML dupliqués: {', '.join(duplicates[:5])}.")

    for style_match in re.finditer(r"<\s*style[^>]*>(.*?)<\s*/\s*style\s*>", clean_content, flags=re.IGNORECASE | re.DOTALL):
        if _style_targets_global_layout(style_match.group(1)):
            _add(
                issues,
                path,
                "warning",
                "css.global-layout",
                "Style inline ciblant body/html/.container/.row/.col*: risque de casser le thème Odoo.",
            )

    if category == "pages":
        has_template = bool(re.search(r't-name=["\'][^"\']+["\']', clean_content))
        has_layout = bool(re.search(r't-call=["\']website\.layout["\']', clean_content))
        has_wrap = bool(re.search(r'id=["\']wrap["\']', clean_content))
        if has_template and not has_layout:
            _add(issues, path, "error", "page.layout", "Template QWeb page sans t-call=\"website.layout\".")
        if has_layout and not has_wrap:
            _add(issues, path, "warning", "page.wrap", "Page avec website.layout mais sans div id=\"wrap\".")
        if not re.search(r"data-snippet=", clean_content):
            _add(issues, path, "warning", "page.snippet", "Aucun data-snippet: compatibilité éditeur Odoo moins fiable.")

    source = _source_sibling(path)
    if source:
        try:
            _, source_clean = _parse_metadata_header(source.read_text(encoding="utf-8"))
            if _tag_signature(source_clean) != _tag_signature(clean_content):
                _add(
                    issues,
                    path,
                    "warning",
                    "translation.structure",
                    f"Structure HTML/QWeb différente du fichier source {source.name}. Traduire le texte, pas la structure.",
                )
        except UnicodeDecodeError:
            pass

    return issues


def validate_paths(paths: Iterable[Path], category: Optional[str] = None) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for path in paths:
        issues.extend(validate_file(path, category=category))
    return issues


def collect_paths(files: Optional[List[str]] = None, category: Optional[str] = None) -> List[Path]:
    if files:
        return [Path(f).resolve() if Path(f).is_absolute() else (PROJECT_ROOT / f).resolve() for f in files]

    categories = [category] if category else ["pages", "products", "blog-posts"]
    paths: List[Path] = []
    for cat in categories:
        directory = SYNC_DIRS[cat]
        extension = ".xml" if cat == "pages" else ".html"
        if directory.exists():
            paths.extend(sorted(directory.glob(f"**/*{extension}")))
    return paths
