#!/usr/bin/env python3
"""
Smoke tests réels mais isolés pour le SyncEngine.

Par défaut, le script n'écrit rien dans Odoo. Utiliser --apply pour créer un
produit temporaire non publié, pousser un fichier local via SyncManager, vérifier
les champs, puis nettoyer Odoo et le fichier local.
"""
import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from .config import ODOO_CONFIG, PROJECT_ROOT  # noqa: E402
from .sync_manager import SyncManager  # noqa: E402
from .cli import _expand_files_all_langs  # noqa: E402


PRODUCT_TEST_DIR = PROJECT_ROOT / "products" / "_syncengine_smoke_tests"
PAGE_TEST_DIR = PROJECT_ROOT / "pages" / "_syncengine_smoke_tests"
BLOG_TEST_DIR = PROJECT_ROOT / "blog-posts" / "_syncengine_smoke_tests"


def _now_token() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _parse_langs(raw: str) -> list[str]:
    return [lang.strip() for lang in raw.split(",") if lang.strip()]


def _absolute_url(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    base = str(ODOO_CONFIG.get("url") or "").rstrip("/")
    if not base:
        raise RuntimeError("ODOO_URL est requis pour vérifier le rendu public.")
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return base + path_or_url


def _fetch_public_html(path_or_url: str, marker: str, retries: int = 6) -> str:
    url = _absolute_url(path_or_url)
    last_error = None
    for _ in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "SyncEngineSmokeTest/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                html = response.read().decode("utf-8", errors="replace")
            if marker in html:
                return html
            last_error = f"marker {marker} absent from {url}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"Public rendering check failed for {url}: {last_error}")


def _product_html(record_id: int, name: str, marker: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
id: {record_id}
lang: fr_FR
name: {name}
url: /shop/{name.lower().replace("_", "-")}-{record_id}
default_code: SYNC-SMOKE
list_price: 123.45
description_ecommerce: <p>{marker} description ecommerce FR</p>
description_sale: |
  {marker} description vente FR
  Ligne devis stable.
categ_id: False
website_meta_keywords: syncengine smoke test
website_meta_title: {name}
website_meta_description: {marker} meta description FR
website_published: False
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine smoke test">
  <div class="container">
    <h1>{marker} titre produit FR</h1>
    <p>{marker} contenu website_description FR.</p>
  </div>
</section>
"""


def _cleanup(manager: SyncManager, record_id: int, local_path: Path) -> None:
    if local_path.exists():
        local_path.unlink()
    try:
        PRODUCT_TEST_DIR.rmdir()
    except OSError:
        pass

    try:
        manager.client.execute("product.template", "unlink", [record_id], context={"lang": "fr_FR"})
    except Exception:
        manager.client.write(
            "product.template",
            [record_id],
            {"active": False, "website_published": False},
            context={"lang": "fr_FR"},
        )


def _cleanup_local(local_path: Path, directory: Path) -> None:
    if local_path.exists():
        local_path.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass


def run_product_smoke(apply: bool, keep: bool) -> int:
    marker = f"SYNCENGINE_SMOKE_{_now_token()}"
    name = f"SYNCENGINE TEST {marker}"

    if not apply:
        print("[DRY-RUN] Créerait un product.template non publié, pousserait un fichier local, vérifierait, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    # Ce smoke test vérifie aussi l'écriture volontaire de description_sale.
    os.environ["SYNC_ALLOW_DESCRIPTION_SALE_WRITES"] = "1"
    manager = SyncManager()

    product_id = manager.client.create(
        "product.template",
        {
            "name": name,
            "sale_ok": True,
            "active": True,
            "website_published": False,
            "list_price": 1.0,
            "description_sale": False,
            "description_ecommerce": False,
            "website_description": False,
        },
        context={"lang": "fr_FR"},
    )

    PRODUCT_TEST_DIR.mkdir(parents=True, exist_ok=True)
    local_path = PRODUCT_TEST_DIR / f"{product_id}-syncengine-smoke-product.html"
    local_path.write_text(_product_html(product_id, name, marker), encoding="utf-8")

    try:
        results = manager.push(files=[str(local_path)], force=True, dry_run=False)
        if results.get("errors"):
            raise RuntimeError(f"Push errors: {results['errors']}")
        if not results.get("pushed"):
            raise RuntimeError(f"Nothing pushed: {results}")

        records = manager.client.search_read(
            "product.template",
            [["id", "=", product_id]],
            [
                "id",
                "name",
                "website_description",
                "description_ecommerce",
                "description_sale",
                "website_published",
                "list_price",
            ],
            context={"lang": "fr_FR"},
        )
        if not records:
            raise RuntimeError("Temporary product disappeared before verification")
        record = records[0]

        checks = {
            "website_description": marker in (record.get("website_description") or ""),
            "description_ecommerce": marker in (record.get("description_ecommerce") or ""),
            "description_sale": marker in (record.get("description_sale") or ""),
            "website_published_false": record.get("website_published") is False,
            # Structural writes are blocked, so the smoke metadata list_price must not override creation value.
            "list_price_not_overwritten": float(record.get("list_price") or 0) == 1.0,
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Verification failed: {failed}; record={record}")

        print(f"OK product smoke test: product_id={product_id}, marker={marker}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: product_id={product_id}, local_path={local_path}")
        else:
            _cleanup(manager, product_id, local_path)


def _page_xml(name: str, marker: str, published: bool = False) -> str:
    slug = _slug(name)
    published_value = "True" if published else "False"
    return f"""<!--
ODOO-SYNC-METADATA
lang: fr_FR
name: {name}
url: /{slug}
website_meta_title: {name}
website_meta_description: {marker} meta page FR
website_meta_keywords: syncengine smoke page
website_published: {published_value}
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine smoke page">
  <div class="container">
    <h1>{marker} titre page FR</h1>
    <p>{marker} contenu page compatible éditeur Odoo.</p>
  </div>
</section>
"""


def run_page_smoke(apply: bool, keep: bool, publish: bool = False) -> int:
    marker = f"SYNCENGINE_PAGE_SMOKE_{_now_token()}"
    name = f"SyncEngine smoke page {marker}"

    if not apply:
        state = "publiée temporairement" if publish else "non publiée"
        print(f"[DRY-RUN] Créerait une website.page {state} depuis un fichier local sans ID, vérifierait, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    manager = SyncManager()
    PAGE_TEST_DIR.mkdir(parents=True, exist_ok=True)
    local_path = PAGE_TEST_DIR / f"syncengine-smoke-page-{_now_token()}.xml"
    local_path.write_text(_page_xml(name, marker, published=publish), encoding="utf-8")
    page_id = None

    try:
        results = manager.push(files=[str(local_path)], create_missing=True, force=True, dry_run=False)
        if results.get("errors"):
            raise RuntimeError(f"Push errors: {results['errors']}")
        pushed = results.get("pushed") or []
        if not pushed:
            raise RuntimeError(f"Nothing pushed: {results}")
        page_id = int(pushed[0]["id"])

        records = manager.client.search_read(
            "website.page",
            [["id", "=", page_id]],
            ["id", "name", "url", "arch_db", "website_published", "website_meta_description"],
            context={"lang": "fr_FR"},
        )
        if not records:
            raise RuntimeError("Temporary page disappeared before verification")
        record = records[0]
        checks = {
            "arch_db": marker in (record.get("arch_db") or ""),
            "meta": marker in (record.get("website_meta_description") or ""),
            "website_published": record.get("website_published") is bool(publish),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Verification failed: {failed}; record={record}")

        print(f"OK page smoke test: page_id={page_id}, marker={marker}, url={record.get('url')}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: page_id={page_id}, local_path={local_path}")
        else:
            _cleanup_local(local_path, PAGE_TEST_DIR)
            if page_id:
                try:
                    manager.client.execute("website.page", "unlink", [page_id], context={"lang": "fr_FR"})
                except Exception:
                    manager.client.write("website.page", [page_id], {"website_published": False}, context={"lang": "fr_FR"})


def _blog_html(record_id: int, blog_name: str, name: str, marker: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
id: {record_id}
lang: fr_FR
name: {name}
url: /blog/{name.lower().replace(" ", "-")}
subtitle: {marker} sous-titre FR
blog_id: {blog_name}
website_meta_title: {name}
website_meta_description: {marker} meta article FR
website_meta_keywords: syncengine smoke blog
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine smoke blog">
  <div class="container">
    <h2>{marker} titre article FR</h2>
    <p>{marker} contenu article compatible Odoo.</p>
  </div>
</section>
"""


def _localized_product_html(record_id: int, name: str, marker: str, lang: str, label: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
id: {record_id}
lang: {lang}
name: {name}
url: /shop/{_slug(name)}-{record_id}
description_ecommerce: <p>{marker} description ecommerce {label}</p>
website_meta_keywords: syncengine smoke multilang
website_meta_title: {marker} meta title {label}
website_meta_description: {marker} meta description {label}
website_published: False
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine smoke product multilang">
  <div class="container">
    <h1>{marker} product title {label}</h1>
    <p>{marker} product body {label}.</p>
  </div>
</section>
"""


def _localized_page_xml(record_id: int, name: str, url: str, marker: str, lang: str, label: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
id: {record_id}
lang: {lang}
name: {name}
url: {url}
website_meta_title: {marker} meta title {label}
website_meta_description: {marker} meta description {label}
website_meta_keywords: syncengine smoke multilang
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine smoke page">
  <div class="container">
    <h1>{marker} titre page FR</h1>
    <p>{marker} contenu page compatible éditeur Odoo.</p>
  </div>
</section>
"""


def _localized_blog_html(record_id: int, blog_name: str, name: str, marker: str, lang: str, label: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
id: {record_id}
lang: {lang}
name: {name}
url: /blog/{_slug(name)}
subtitle: {marker} subtitle {label}
blog_id: {blog_name}
website_meta_title: {marker} meta title {label}
website_meta_description: {marker} meta description {label}
website_meta_keywords: syncengine smoke multilang
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine smoke blog multilang">
  <div class="container">
    <h2>{marker} blog title {label}</h2>
    <p>{marker} blog body {label}.</p>
  </div>
</section>
"""


def _assert_lang_markers(record: dict, fields: list[str], expected_marker: str, forbidden_markers: list[str]) -> None:
    failed = []
    for field in fields:
        value = record.get(field) or ""
        if expected_marker not in value:
            failed.append(f"{field}: missing {expected_marker}")
        for forbidden in forbidden_markers:
            if forbidden and forbidden in value:
                failed.append(f"{field}: contains forbidden {forbidden}")
    if failed:
        raise RuntimeError(f"Language verification failed: {failed}; record={record}")


def _ensure_langs_available(manager: SyncManager, langs: list[str]) -> None:
    active = set(manager.get_active_langs())
    missing = [lang for lang in langs if lang not in active]
    if missing:
        raise RuntimeError(f"Langues non actives dans Odoo pour le smoke test: {missing}; actives={sorted(active)}")


def _push_i18n_paths(manager: SyncManager, paths: list[Path], grouped: bool, label: str) -> None:
    if not grouped:
        for path in paths:
            results = manager.push(files=[str(path)], force=True, dry_run=False)
            if results.get("errors") or not results.get("pushed"):
                raise RuntimeError(f"{label} push failed for {path.name}: {results}")
        return

    expanded = _expand_files_all_langs([str(paths[0])])
    expected = [str(path) for path in paths]
    missing = [path for path in expected if path not in expanded]
    if missing:
        raise RuntimeError(f"{label} grouped expansion missed files: missing={missing}; expanded={expanded}")

    results = manager.push(files=expanded, force=True, dry_run=False)
    processed_count = len(results.get("pushed") or []) + len(results.get("skipped") or [])
    if results.get("errors") or processed_count < len(paths):
        raise RuntimeError(f"{label} grouped push failed: {results}; expanded={expanded}")


def run_product_multilang_smoke(apply: bool, keep: bool, langs: list[str], grouped: bool = False) -> int:
    marker_base = f"SYNCENGINE_PRODUCT_I18N_{_now_token()}"
    fr_marker = f"{marker_base}_FR"
    name = f"SYNCENGINE TEST I18N {marker_base}"

    if not apply:
        mode = "en push groupé" if grouped else "une par une"
        print(f"[DRY-RUN] Testerait product.template FR puis traductions {langs} {mode}, vérifierait les langues, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker_base}")
        return 0

    manager = SyncManager()
    _ensure_langs_available(manager, ["fr_FR", *langs])
    product_id = manager.client.create(
        "product.template",
        {
            "name": name,
            "sale_ok": True,
            "active": True,
            "website_published": False,
            "list_price": 1.0,
            "description_ecommerce": False,
            "website_description": False,
        },
        context={"lang": "fr_FR"},
    )
    PRODUCT_TEST_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    try:
        fr_path = PRODUCT_TEST_DIR / f"{product_id}-syncengine-product-i18n.html"
        fr_path.write_text(_localized_product_html(product_id, name, fr_marker, "fr_FR", "FR"), encoding="utf-8")
        paths.append(fr_path)

        target_markers = []
        for lang in langs:
            marker = f"{marker_base}_{lang}"
            target_markers.append(marker)
            path = PRODUCT_TEST_DIR / f"{product_id}-syncengine-product-i18n.{lang}.html"
            path.write_text(_localized_product_html(product_id, name, marker, lang, lang), encoding="utf-8")
            paths.append(path)

        _push_i18n_paths(manager, paths, grouped=grouped, label="product multilang")

        fr_record = manager.client.search_read(
            "product.template",
            [["id", "=", product_id]],
            ["website_description", "description_ecommerce", "website_meta_description"],
            context={"lang": "fr_FR"},
            limit=1,
        )[0]
        _assert_lang_markers(fr_record, ["website_description", "description_ecommerce", "website_meta_description"], fr_marker, target_markers)

        for lang, marker in zip(langs, target_markers):
            record = manager.client.search_read(
                "product.template",
                [["id", "=", product_id]],
                ["website_description", "description_ecommerce", "website_meta_description"],
                context={"lang": lang},
                limit=1,
            )[0]
            _assert_lang_markers(record, ["website_description", "description_ecommerce", "website_meta_description"], marker, [fr_marker])

        suffix = " grouped" if grouped else ""
        print(f"OK product multilang{suffix} smoke test: product_id={product_id}, langs={','.join(langs)}, marker={marker_base}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: product_id={product_id}, local_paths={[str(p) for p in paths]}")
        else:
            for path in paths:
                _cleanup_local(path, PRODUCT_TEST_DIR)
            try:
                manager.client.execute("product.template", "unlink", [product_id], context={"lang": "fr_FR"})
            except Exception:
                manager.client.write("product.template", [product_id], {"active": False, "website_published": False}, context={"lang": "fr_FR"})


def run_page_multilang_smoke(apply: bool, keep: bool, langs: list[str], grouped: bool = False) -> int:
    marker_base = f"SYNCENGINE_PAGE_I18N_{_now_token()}"
    fr_marker = f"{marker_base}_FR"
    name = f"SyncEngine page i18n {marker_base}"

    if not apply:
        mode = "en push groupé" if grouped else "une par une"
        print(f"[DRY-RUN] Testerait website.page FR puis traductions {langs} {mode}, vérifierait les langues, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker_base}")
        return 0

    manager = SyncManager()
    _ensure_langs_available(manager, ["fr_FR", *langs])
    PAGE_TEST_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    page_id = None

    try:
        fr_path = PAGE_TEST_DIR / f"syncengine-page-i18n-{_now_token()}.xml"
        fr_path.write_text(_page_xml(name, fr_marker, published=False), encoding="utf-8")
        paths.append(fr_path)
        results = manager.push(files=[str(fr_path)], create_missing=True, force=True, dry_run=False)
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"FR page push failed: {results}")
        page_id = int(results["pushed"][0]["id"])
        fr_record = manager.client.search_read(
            "website.page",
            [["id", "=", page_id]],
            ["url"],
            context={"lang": "fr_FR"},
            limit=1,
        )[0]
        url = fr_record.get("url") or f"/{_slug(name)}"

        target_markers = []
        for lang in langs:
            marker = f"{marker_base}_{lang}"
            target_markers.append(marker)
            if grouped:
                path = PAGE_TEST_DIR / f"{page_id}-syncengine-page-i18n.{lang}.xml"
            else:
                path = PAGE_TEST_DIR / f"syncengine-page-i18n-{page_id}.{lang}.xml"
            path.write_text(_localized_page_xml(page_id, name, url, marker, lang, lang), encoding="utf-8")
            paths.append(path)

        if grouped:
            source_with_id = PAGE_TEST_DIR / f"{page_id}-syncengine-page-i18n.xml"
            fr_path.rename(source_with_id)
            paths[0] = source_with_id
            _push_i18n_paths(manager, paths, grouped=True, label="page multilang")
        else:
            for path in paths[1:]:
                results = manager.push(files=[str(path)], force=True, dry_run=False)
                if results.get("errors") or not results.get("pushed"):
                    raise RuntimeError(f"{path.name} page push failed: {results}")

        fr_record = manager.client.search_read(
            "website.page",
            [["id", "=", page_id]],
            ["arch_db", "website_meta_description"],
            context={"lang": "fr_FR"},
            limit=1,
        )[0]
        _assert_lang_markers(fr_record, ["arch_db", "website_meta_description"], fr_marker, target_markers)

        for lang, marker in zip(langs, target_markers):
            record = manager.client.search_read(
                "website.page",
                [["id", "=", page_id]],
                ["arch_db", "website_meta_description"],
                context={"lang": lang},
                limit=1,
            )[0]
            _assert_lang_markers(record, ["arch_db", "website_meta_description"], marker, [fr_marker])

        suffix = " grouped" if grouped else ""
        print(f"OK page multilang{suffix} smoke test: page_id={page_id}, langs={','.join(langs)}, marker={marker_base}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: page_id={page_id}, local_paths={[str(p) for p in paths]}")
        else:
            for path in paths:
                _cleanup_local(path, PAGE_TEST_DIR)
            if page_id:
                try:
                    manager.client.execute("website.page", "unlink", [page_id], context={"lang": "fr_FR"})
                except Exception:
                    manager.client.write("website.page", [page_id], {"website_published": False}, context={"lang": "fr_FR"})


def run_blog_multilang_smoke(apply: bool, keep: bool, langs: list[str], grouped: bool = False) -> int:
    marker_base = f"SYNCENGINE_BLOG_I18N_{_now_token()}"
    fr_marker = f"{marker_base}_FR"
    name = f"SyncEngine blog i18n {marker_base}"

    if not apply:
        mode = "en push groupé" if grouped else "une par une"
        print(f"[DRY-RUN] Testerait blog.post FR puis traductions {langs} {mode}, vérifierait les langues, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker_base}")
        return 0

    manager = SyncManager()
    _ensure_langs_available(manager, ["fr_FR", *langs])
    blogs = manager.client.search_read("blog.blog", [], ["id", "name"], limit=1, context={"lang": "fr_FR"})
    if not blogs:
        raise RuntimeError("Aucun blog.blog disponible pour le smoke test")
    blog_id = blogs[0]["id"]
    blog_name = blogs[0]["name"]
    fields = manager.client.execute(
        "blog.post",
        "fields_get",
        ["website_published"],
        attributes=["type"],
        context={"lang": "fr_FR"},
    )
    values = {"name": name, "blog_id": blog_id, "content": ""}
    if "website_published" in fields:
        values["website_published"] = False
    post_id = manager.client.create("blog.post", values, context={"lang": "fr_FR"})
    BLOG_TEST_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    try:
        fr_path = BLOG_TEST_DIR / f"{post_id}-syncengine-blog-i18n.html"
        fr_path.write_text(_localized_blog_html(post_id, blog_name, name, fr_marker, "fr_FR", "FR"), encoding="utf-8")
        paths.append(fr_path)

        target_markers = []
        for lang in langs:
            marker = f"{marker_base}_{lang}"
            target_markers.append(marker)
            path = BLOG_TEST_DIR / f"{post_id}-syncengine-blog-i18n.{lang}.html"
            path.write_text(_localized_blog_html(post_id, blog_name, name, marker, lang, lang), encoding="utf-8")
            paths.append(path)

        _push_i18n_paths(manager, paths, grouped=grouped, label="blog multilang")

        fr_record = manager.client.search_read(
            "blog.post",
            [["id", "=", post_id]],
            ["content", "subtitle", "website_meta_description"],
            context={"lang": "fr_FR"},
            limit=1,
        )[0]
        _assert_lang_markers(fr_record, ["content", "subtitle", "website_meta_description"], fr_marker, target_markers)

        for lang, marker in zip(langs, target_markers):
            record = manager.client.search_read(
                "blog.post",
                [["id", "=", post_id]],
                ["content", "subtitle", "website_meta_description"],
                context={"lang": lang},
                limit=1,
            )[0]
            _assert_lang_markers(record, ["content", "subtitle", "website_meta_description"], marker, [fr_marker])

        suffix = " grouped" if grouped else ""
        print(f"OK blog multilang{suffix} smoke test: post_id={post_id}, langs={','.join(langs)}, marker={marker_base}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: post_id={post_id}, local_paths={[str(p) for p in paths]}")
        else:
            for path in paths:
                _cleanup_local(path, BLOG_TEST_DIR)
            try:
                manager.client.execute("blog.post", "unlink", [post_id], context={"lang": "fr_FR"})
            except Exception:
                if "website_published" in fields:
                    manager.client.write("blog.post", [post_id], {"website_published": False}, context={"lang": "fr_FR"})


def run_multilang_smoke(apply: bool, keep: bool, langs: list[str], grouped: bool = False) -> int:
    for target in (run_product_multilang_smoke, run_page_multilang_smoke, run_blog_multilang_smoke):
        code = target(apply=apply, keep=keep, langs=langs, grouped=grouped)
        if code:
            return code
    return 0


def run_blog_smoke(apply: bool, keep: bool) -> int:
    marker = f"SYNCENGINE_BLOG_SMOKE_{_now_token()}"
    name = f"SyncEngine smoke blog {marker}"

    if not apply:
        print("[DRY-RUN] Créerait un blog.post non publié, pousserait son contenu local, vérifierait, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    manager = SyncManager()
    blogs = manager.client.search_read("blog.blog", [], ["id", "name"], limit=1, context={"lang": "fr_FR"})
    if not blogs:
        raise RuntimeError("Aucun blog.blog disponible pour le smoke test")
    blog_id = blogs[0]["id"]
    blog_name = blogs[0]["name"]

    fields = manager.client.execute(
        "blog.post",
        "fields_get",
        ["website_published"],
        attributes=["type"],
        context={"lang": "fr_FR"},
    )
    values = {"name": name, "blog_id": blog_id, "content": ""}
    if "website_published" in fields:
        values["website_published"] = False
    post_id = manager.client.create("blog.post", values, context={"lang": "fr_FR"})

    BLOG_TEST_DIR.mkdir(parents=True, exist_ok=True)
    local_path = BLOG_TEST_DIR / f"{post_id}-syncengine-smoke-blog.html"
    local_path.write_text(_blog_html(post_id, blog_name, name, marker), encoding="utf-8")

    try:
        results = manager.push(files=[str(local_path)], force=True, dry_run=False)
        if results.get("errors"):
            raise RuntimeError(f"Push errors: {results['errors']}")
        if not results.get("pushed"):
            raise RuntimeError(f"Nothing pushed: {results}")

        records = manager.client.search_read(
            "blog.post",
            [["id", "=", post_id]],
            ["id", "name", "content", "subtitle", "website_meta_description"],
            context={"lang": "fr_FR"},
        )
        if not records:
            raise RuntimeError("Temporary blog post disappeared before verification")
        record = records[0]
        checks = {
            "content": marker in (record.get("content") or ""),
            "subtitle": marker in (record.get("subtitle") or ""),
            "meta": marker in (record.get("website_meta_description") or ""),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Verification failed: {failed}; record={record}")

        print(f"OK blog smoke test: post_id={post_id}, marker={marker}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: post_id={post_id}, local_path={local_path}")
        else:
            _cleanup_local(local_path, BLOG_TEST_DIR)
            try:
                manager.client.execute("blog.post", "unlink", [post_id], context={"lang": "fr_FR"})
            except Exception:
                if "website_published" in fields:
                    manager.client.write("blog.post", [post_id], {"website_published": False}, context={"lang": "fr_FR"})


def _product_create_html(name: str, marker: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
lang: fr_FR
name: {name}
default_code: SYNC-CREATE
list_price: 12.34
description_ecommerce: <p>{marker} description ecommerce création FR</p>
description_sale: |
  {marker} description vente création FR
website_meta_keywords: syncengine create test
website_meta_title: {name}
website_meta_description: {marker} meta création produit FR
website_published: False
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine create product">
  <div class="container">
    <h1>{marker} création produit FR</h1>
    <p>{marker} contenu produit créé depuis local.</p>
  </div>
</section>
"""


def _blog_create_html(blog_name: str, name: str, marker: str) -> str:
    return f"""<!--
ODOO-SYNC-METADATA
lang: fr_FR
name: {name}
subtitle: {marker} sous-titre création FR
blog_id: {blog_name}
website_meta_title: {name}
website_meta_description: {marker} meta création article FR
website_meta_keywords: syncengine create blog
website_published: False
-->

<section class="s_text_block pt16 pb16" data-snippet="s_text_block" data-name="SyncEngine create blog">
  <div class="container">
    <h2>{marker} création article FR</h2>
    <p>{marker} contenu article créé depuis local.</p>
  </div>
</section>
"""


def run_product_create_smoke(apply: bool, keep: bool) -> int:
    marker = f"SYNCENGINE_PRODUCT_CREATE_{_now_token()}"
    name = f"SYNCENGINE CREATE PRODUCT {marker}"

    if not apply:
        print("[DRY-RUN] Créerait un product.template depuis un fichier local sans ID, vérifierait l'ID local, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    manager = SyncManager()
    PRODUCT_TEST_DIR.mkdir(parents=True, exist_ok=True)
    local_path = PRODUCT_TEST_DIR / f"syncengine-create-product-{_now_token()}.html"
    local_path.write_text(_product_create_html(name, marker), encoding="utf-8")
    product_id = None

    try:
        results = manager.push(files=[str(local_path)], create_missing=True, force=True, dry_run=False)
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Create product push failed: {results}")
        product_id = int(results["pushed"][0]["id"])

        metadata, local_content = manager._parse_metadata_header(local_path.read_text(encoding="utf-8"))
        records = manager.client.search_read(
            "product.template",
            [["id", "=", product_id]],
            [
                "name",
                "website_description",
                "description_ecommerce",
                "description_sale",
                "website_meta_description",
                "website_published",
                "list_price",
            ],
            context={"lang": "fr_FR"},
            limit=1,
        )
        if not records:
            raise RuntimeError("Temporary created product disappeared before verification")
        record = records[0]
        checks = {
            "local_id_written": metadata.get("id") == str(product_id),
            "local_content": marker in local_content,
            "website_description": marker in (record.get("website_description") or ""),
            "description_ecommerce": marker in (record.get("description_ecommerce") or ""),
            "description_sale": marker in (record.get("description_sale") or ""),
            "meta": marker in (record.get("website_meta_description") or ""),
            "not_published": record.get("website_published") is False,
            "list_price": abs(float(record.get("list_price") or 0) - 12.34) < 0.001,
        }
        failed = [key for key, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Verification failed: {failed}; record={record}; metadata={metadata}")

        print(f"OK product create smoke test: product_id={product_id}, marker={marker}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: product_id={product_id}, local_path={local_path}")
        else:
            _cleanup_local(local_path, PRODUCT_TEST_DIR)
            if product_id:
                try:
                    manager.client.execute("product.template", "unlink", [product_id], context={"lang": "fr_FR"})
                except Exception:
                    manager.client.write("product.template", [product_id], {"active": False, "website_published": False}, context={"lang": "fr_FR"})


def run_blog_create_smoke(apply: bool, keep: bool) -> int:
    marker = f"SYNCENGINE_BLOG_CREATE_{_now_token()}"
    name = f"SyncEngine create blog {marker}"

    if not apply:
        print("[DRY-RUN] Créerait un blog.post depuis un fichier local sans ID, vérifierait l'ID local, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    manager = SyncManager()
    blogs = manager.client.search_read("blog.blog", [], ["id", "name"], limit=1, context={"lang": "fr_FR"})
    if not blogs:
        raise RuntimeError("Aucun blog.blog disponible pour le smoke test")
    blog_name = blogs[0]["name"]

    BLOG_TEST_DIR.mkdir(parents=True, exist_ok=True)
    local_path = BLOG_TEST_DIR / f"syncengine-create-blog-{_now_token()}.html"
    local_path.write_text(_blog_create_html(blog_name, name, marker), encoding="utf-8")
    post_id = None

    try:
        results = manager.push(files=[str(local_path)], create_missing=True, force=True, dry_run=False)
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Create blog push failed: {results}")
        post_id = int(results["pushed"][0]["id"])

        metadata, local_content = manager._parse_metadata_header(local_path.read_text(encoding="utf-8"))
        records = manager.client.search_read(
            "blog.post",
            [["id", "=", post_id]],
            ["name", "content", "subtitle", "website_meta_description", "website_published"],
            context={"lang": "fr_FR"},
            limit=1,
        )
        if not records:
            raise RuntimeError("Temporary created blog post disappeared before verification")
        record = records[0]
        checks = {
            "local_id_written": metadata.get("id") == str(post_id),
            "local_content": marker in local_content,
            "content": marker in (record.get("content") or ""),
            "subtitle": marker in (record.get("subtitle") or ""),
            "meta": marker in (record.get("website_meta_description") or ""),
            "not_published": record.get("website_published") is False,
        }
        failed = [key for key, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Verification failed: {failed}; record={record}; metadata={metadata}")

        print(f"OK blog create smoke test: post_id={post_id}, marker={marker}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: post_id={post_id}, local_path={local_path}")
        else:
            _cleanup_local(local_path, BLOG_TEST_DIR)
            if post_id:
                try:
                    manager.client.execute("blog.post", "unlink", [post_id], context={"lang": "fr_FR"})
                except Exception:
                    manager.client.write("blog.post", [post_id], {"website_published": False}, context={"lang": "fr_FR"})


def run_create_smoke(apply: bool, keep: bool) -> int:
    for target in (
        lambda apply, keep: run_page_smoke(apply=apply, keep=keep, publish=False),
        run_product_create_smoke,
        run_blog_create_smoke,
    ):
        code = target(apply=apply, keep=keep)
        if code:
            return code
    return 0


def run_visibility_smoke(apply: bool, keep: bool) -> int:
    marker = f"SYNCENGINE_VISIBILITY_{_now_token()}"

    if not apply:
        print("[DRY-RUN] Créerait page/produit/article, appliquerait publish/index explicites, vérifierait, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    manager = SyncManager()
    created: list[tuple[str, int, Path, Path]] = []

    try:
        PAGE_TEST_DIR.mkdir(parents=True, exist_ok=True)
        page_name = f"SyncEngine visibility page {marker}"
        page_path = PAGE_TEST_DIR / f"syncengine-visibility-page-{_now_token()}.xml"
        page_path.write_text(_page_xml(page_name, marker, published=False), encoding="utf-8")
        results = manager.push(
            files=[str(page_path)],
            create_missing=True,
            force=True,
            dry_run=False,
            publish=True,
            index=True,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Visibility page publish failed: {results}")
        page_id = int(results["pushed"][0]["id"])
        created.append(("website.page", page_id, page_path, PAGE_TEST_DIR))
        page = manager.client.search_read(
            "website.page",
            [["id", "=", page_id]],
            ["website_published", "website_indexed"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if page.get("website_published") is not True or page.get("website_indexed") is not True:
            raise RuntimeError(f"Page visibility publish failed: {page}")

        results = manager.push(
            files=[str(page_path)],
            force=True,
            dry_run=False,
            publish=False,
            index=False,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Visibility page unpublish failed: {results}")
        page = manager.client.search_read(
            "website.page",
            [["id", "=", page_id]],
            ["website_published", "website_indexed"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if page.get("website_published") is not False or page.get("website_indexed") is not False:
            raise RuntimeError(f"Page visibility unpublish failed: {page}")

        PRODUCT_TEST_DIR.mkdir(parents=True, exist_ok=True)
        product_name = f"SYNCENGINE VISIBILITY PRODUCT {marker}"
        product_path = PRODUCT_TEST_DIR / f"syncengine-visibility-product-{_now_token()}.html"
        product_path.write_text(_product_create_html(product_name, marker), encoding="utf-8")
        results = manager.push(
            files=[str(product_path)],
            create_missing=True,
            force=True,
            dry_run=False,
            publish=True,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Visibility product publish failed: {results}")
        product_id = int(results["pushed"][0]["id"])
        created.append(("product.template", product_id, product_path, PRODUCT_TEST_DIR))
        product = manager.client.search_read(
            "product.template",
            [["id", "=", product_id]],
            ["website_published"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if product.get("website_published") is not True:
            raise RuntimeError(f"Product visibility publish failed: {product}")

        results = manager.push(files=[str(product_path)], force=True, dry_run=False, publish=False)
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Visibility product unpublish failed: {results}")
        product = manager.client.search_read(
            "product.template",
            [["id", "=", product_id]],
            ["website_published"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if product.get("website_published") is not False:
            raise RuntimeError(f"Product visibility unpublish failed: {product}")

        blogs = manager.client.search_read("blog.blog", [], ["id", "name"], limit=1, context={"lang": "fr_FR"})
        if not blogs:
            raise RuntimeError("Aucun blog.blog disponible pour le smoke test")
        BLOG_TEST_DIR.mkdir(parents=True, exist_ok=True)
        blog_name = f"SyncEngine visibility blog {marker}"
        blog_path = BLOG_TEST_DIR / f"syncengine-visibility-blog-{_now_token()}.html"
        blog_path.write_text(_blog_create_html(blogs[0]["name"], blog_name, marker), encoding="utf-8")
        results = manager.push(
            files=[str(blog_path)],
            create_missing=True,
            force=True,
            dry_run=False,
            publish=True,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Visibility blog publish failed: {results}")
        post_id = int(results["pushed"][0]["id"])
        created.append(("blog.post", post_id, blog_path, BLOG_TEST_DIR))
        post = manager.client.search_read(
            "blog.post",
            [["id", "=", post_id]],
            ["website_published"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if post.get("website_published") is not True:
            raise RuntimeError(f"Blog visibility publish failed: {post}")

        results = manager.push(files=[str(blog_path)], force=True, dry_run=False, publish=False)
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Visibility blog unpublish failed: {results}")
        post = manager.client.search_read(
            "blog.post",
            [["id", "=", post_id]],
            ["website_published"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if post.get("website_published") is not False:
            raise RuntimeError(f"Blog visibility unpublish failed: {post}")

        print(f"OK visibility smoke test: marker={marker}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: created={[(m, i, str(p)) for m, i, p, _ in created]}")
        else:
            for model, record_id, path, directory in reversed(created):
                _cleanup_local(path, directory)
                try:
                    manager.client.execute(model, "unlink", [record_id], context={"lang": "fr_FR"})
                except Exception:
                    values = {"website_published": False}
                    if model == "product.template":
                        values["active"] = False
                    manager.client.write(model, [record_id], values, context={"lang": "fr_FR"})


def run_public_render_smoke(apply: bool, keep: bool) -> int:
    marker = f"SYNCENGINE_PUBLIC_{_now_token()}"

    if not apply:
        print("[DRY-RUN] Créerait page/produit/article publiés, vérifierait leurs URLs publiques, puis nettoierait.")
        print(f"[DRY-RUN] Marker: {marker}")
        return 0

    manager = SyncManager()
    created: list[tuple[str, int, Path, Path]] = []

    try:
        PAGE_TEST_DIR.mkdir(parents=True, exist_ok=True)
        page_name = f"SyncEngine public page {marker}"
        page_path = PAGE_TEST_DIR / f"syncengine-public-page-{_now_token()}.xml"
        page_path.write_text(_page_xml(page_name, marker, published=False), encoding="utf-8")
        results = manager.push(
            files=[str(page_path)],
            create_missing=True,
            force=True,
            dry_run=False,
            publish=True,
            index=False,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Public page create failed: {results}")
        page_id = int(results["pushed"][0]["id"])
        created.append(("website.page", page_id, page_path, PAGE_TEST_DIR))
        page = manager.client.search_read(
            "website.page",
            [["id", "=", page_id]],
            ["url", "website_published", "website_indexed"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if page.get("website_published") is not True or page.get("website_indexed") is not False:
            raise RuntimeError(f"Unexpected page visibility: {page}")
        _fetch_public_html(page["url"], marker)

        PRODUCT_TEST_DIR.mkdir(parents=True, exist_ok=True)
        product_name = f"SYNCENGINE PUBLIC PRODUCT {marker}"
        product_path = PRODUCT_TEST_DIR / f"syncengine-public-product-{_now_token()}.html"
        product_path.write_text(_product_create_html(product_name, marker), encoding="utf-8")
        results = manager.push(
            files=[str(product_path)],
            create_missing=True,
            force=True,
            dry_run=False,
            publish=True,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Public product create failed: {results}")
        product_id = int(results["pushed"][0]["id"])
        created.append(("product.template", product_id, product_path, PRODUCT_TEST_DIR))
        product = manager.client.search_read(
            "product.template",
            [["id", "=", product_id]],
            ["website_url", "website_published"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if product.get("website_published") is not True or not product.get("website_url"):
            raise RuntimeError(f"Unexpected product public fields: {product}")
        _fetch_public_html(product["website_url"], marker)

        blogs = manager.client.search_read("blog.blog", [], ["id", "name"], limit=1, context={"lang": "fr_FR"})
        if not blogs:
            raise RuntimeError("Aucun blog.blog disponible pour le smoke test")
        BLOG_TEST_DIR.mkdir(parents=True, exist_ok=True)
        blog_name = f"SyncEngine public blog {marker}"
        blog_path = BLOG_TEST_DIR / f"syncengine-public-blog-{_now_token()}.html"
        blog_path.write_text(_blog_create_html(blogs[0]["name"], blog_name, marker), encoding="utf-8")
        results = manager.push(
            files=[str(blog_path)],
            create_missing=True,
            force=True,
            dry_run=False,
            publish=True,
        )
        if results.get("errors") or not results.get("pushed"):
            raise RuntimeError(f"Public blog create failed: {results}")
        post_id = int(results["pushed"][0]["id"])
        created.append(("blog.post", post_id, blog_path, BLOG_TEST_DIR))
        post = manager.client.search_read(
            "blog.post",
            [["id", "=", post_id]],
            ["website_url", "website_published"],
            limit=1,
            context={"lang": "fr_FR"},
        )[0]
        if post.get("website_published") is not True or not post.get("website_url"):
            raise RuntimeError(f"Unexpected blog public fields: {post}")
        _fetch_public_html(post["website_url"], marker)

        print(f"OK public render smoke test: marker={marker}")
        return 0
    finally:
        if keep:
            print(f"KEEP enabled: created={[(m, i, str(p)) for m, i, p, _ in created]}")
        else:
            for model, record_id, path, directory in reversed(created):
                _cleanup_local(path, directory)
                try:
                    manager.client.execute(model, "unlink", [record_id], context={"lang": "fr_FR"})
                except Exception:
                    values = {"website_published": False}
                    if model == "product.template":
                        values["active"] = False
                    if model == "website.page":
                        values["website_indexed"] = False
                    manager.client.write(model, [record_id], values, context={"lang": "fr_FR"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke tests isolés du SyncEngine")
    parser.add_argument("--apply", action="store_true", help="Créer/pousser/vérifier/nettoyer réellement dans Odoo")
    parser.add_argument("--keep", action="store_true", help="Conserver l'objet Odoo et le fichier local pour debug")
    parser.add_argument(
        "--target",
        choices=["product", "page", "blog", "create", "visibility", "public", "multilang", "multilang-grouped", "all"],
        default="product",
    )
    parser.add_argument("--langs", default="en_US", help="Langues cibles CSV pour --target multilang, ex: en_US,it_IT")
    parser.add_argument("--publish-page", action="store_true", help="Pour --target page uniquement: publier temporairement la page test")
    args = parser.parse_args()
    langs = _parse_langs(args.langs)

    if args.target == "product":
        return run_product_smoke(apply=args.apply, keep=args.keep)
    if args.target == "page":
        return run_page_smoke(apply=args.apply, keep=args.keep, publish=args.publish_page)
    if args.target == "blog":
        return run_blog_smoke(apply=args.apply, keep=args.keep)
    if args.target == "create":
        return run_create_smoke(apply=args.apply, keep=args.keep)
    if args.target == "visibility":
        return run_visibility_smoke(apply=args.apply, keep=args.keep)
    if args.target == "public":
        return run_public_render_smoke(apply=args.apply, keep=args.keep)
    if args.target == "multilang":
        return run_multilang_smoke(apply=args.apply, keep=args.keep, langs=langs)
    if args.target == "multilang-grouped":
        return run_multilang_smoke(apply=args.apply, keep=args.keep, langs=langs, grouped=True)
    if args.target == "all":
        for target in (run_product_smoke, lambda apply, keep: run_page_smoke(apply, keep, publish=False), run_blog_smoke):
            code = target(apply=args.apply, keep=args.keep)
            if code:
                return code
        code = run_create_smoke(apply=args.apply, keep=args.keep)
        if code:
            return code
        code = run_visibility_smoke(apply=args.apply, keep=args.keep)
        if code:
            return code
        code = run_public_render_smoke(apply=args.apply, keep=args.keep)
        if code:
            return code
        code = run_multilang_smoke(apply=args.apply, keep=args.keep, langs=langs)
        if code:
            return code
        code = run_multilang_smoke(apply=args.apply, keep=args.keep, langs=langs, grouped=True)
        if code:
            return code
        return 0
    raise AssertionError(args.target)


if __name__ == "__main__":
    raise SystemExit(main())
