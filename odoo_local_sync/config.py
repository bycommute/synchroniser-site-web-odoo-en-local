"""Configuration runtime for Odoo <-> local synchronization."""
import os
from pathlib import Path

# The synchronized project is the current working directory by default.
# This keeps the package installable globally while storing pulled content in
# the repository where the command is launched.
PROJECT_ROOT = Path(os.getenv("ODOO_SYNC_PROJECT_ROOT", os.getcwd())).resolve()

# Charger le fichier .env s'il existe
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    # Si python-dotenv n'est pas installé, essayer de charger manuellement
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

# Configuration Odoo (à personnaliser dans .env ou variables d'environnement)
def _parse_langs(raw_langs: str, fallback_lang: str) -> list[str]:
    """Parser une liste de langues CSV et garantir un fallback."""
    if not raw_langs:
        return [fallback_lang]
    langs = [x.strip() for x in raw_langs.split(",") if x.strip()]
    return langs or [fallback_lang]


DEFAULT_LANG = os.getenv("ODOO_LANG", "fr_FR")
SOURCE_LANG = os.getenv("ODOO_SOURCE_LANG", DEFAULT_LANG)
ACTIVE_LANGS = _parse_langs(os.getenv("ODOO_LANGS", ""), DEFAULT_LANG)

ODOO_CONFIG = {
    "url": os.getenv("ODOO_URL", "").rstrip("/"),
    "db": os.getenv("ODOO_DB", ""),
    "username": os.getenv("ODOO_USERNAME", ""),
    "api_key": os.getenv("ODOO_API_KEY", ""),  # Utiliser une API key plutôt que le mot de passe
    "lang": DEFAULT_LANG,                      # Langue par défaut des opérations sync
    "source_lang": SOURCE_LANG,                # Langue source de référence (souvent fr_FR)
    "active_langs": ACTIVE_LANGS,              # Langues actives pour le projet
}

# Dossiers de synchronisation
SYNC_DIRS = {
    "pages": PROJECT_ROOT / "pages",
    "blog-posts": PROJECT_ROOT / "blog-posts", 
    "products": PROJECT_ROOT / "products",
    "dynamic-pages": PROJECT_ROOT / os.getenv("ODOO_SYNC_DYNAMIC_PAGES_DIR", "dynamic-pages"),
    "installations-catalog": PROJECT_ROOT / os.getenv("ODOO_SYNC_INSTALLATIONS_CATALOG_DIR", "installations-catalog"),
    "employees": PROJECT_ROOT / "employees",
    "categories": PROJECT_ROOT / "categories",
    "menus": PROJECT_ROOT / "menus",
}

IMAGE_SYNC_DIR = PROJECT_ROOT / "images" / "attachments"

# ID du site web Odoo (pour les menus)
WEBSITE_ID = int(os.getenv("ODOO_WEBSITE_ID", "1"))

# Mapping des modèles Odoo
ODOO_MODELS = {
    "pages": {
        "model": "website.page",
        "content_field": "arch_db",
        "name_field": "name",
        "url_field": "url",
        "key_field": "key",
        "file_extension": ".xml",
        "extra_fields": ["website_meta_title", "website_meta_description", "website_meta_keywords", "website_published", "website_indexed"],
    },
    "blog-posts": {
        "model": "blog.post",
        "content_field": "content",
        "name_field": "name",
        "url_field": "website_url",
        "key_field": None,  # Pas de clé externe, on utilise l'ID
        "file_extension": ".html",
        "extra_fields": ["subtitle", "blog_id", "website_meta_title", "website_meta_description", "website_meta_keywords", "website_published"],
        "subfolder_field": "blog_id",  # Organiser par blog
    },
    "products": {
        "model": "product.template",
        "content_field": "website_description",
        "name_field": "name",
        "url_field": "website_url",
        "key_field": None,
        "file_extension": ".html",
        "extra_fields": ["default_code", "list_price", "description_ecommerce", "description_sale", "categ_id", "website_meta_keywords", "website_meta_title", "website_meta_description", "website_published"],
        "domain": [["active", "=", True]],
        "subfolder_field": "categ_id",  # Organiser par catégorie
    },
    "dynamic-pages": {
        "model": "ir.ui.view",
        "content_field": "arch_db",
        "name_field": "name",
        "url_field": "key",
        "key_field": "key",
        "file_extension": ".xml",
        # On ne pousse que le contenu arch_db pour éviter de casser l'héritage
        # (inherit_id/website_id/type sont gérés côté Odoo).
        "extra_fields": [],
        # Common website product page source + custom inherited qweb views.
        "domain": [
            ["type", "=", "qweb"],
            ["website_id", "=", WEBSITE_ID],
            "|",
            ["key", "=", "website_sale.product"],
            ["key", "ilike", "custom.dynamic_product_"],
        ],
    },
    "installations-catalog": {
        "model": "ir.ui.view",
        "content_field": "arch_db",
        "name_field": "name",
        "url_field": "key",
        "key_field": "key",
        "file_extension": ".xml",
        "extra_fields": [],
        "domain": [
            ["type", "=", "qweb"],
            ["website_id", "=", WEBSITE_ID],
            ["key", "ilike", os.getenv("ODOO_INSTALLATIONS_VIEW_KEY_FILTER", "installations")],
        ],
    },
    "employees": {
        "model": "res.partner",
        "content_field": "website_description",
        "name_field": "name",
        "url_field": "website_url",
        "key_field": None,
        "file_extension": ".html",
        "extra_fields": ["function", "email", "website_short_description", "website_published"],
        "domain": [["is_company", "=", False], ["employee", "=", True]],
    },
    "categories": {
        "model": "product.public.category",
        "content_field": "website_footer",
        "name_field": "name",
        "url_field": "display_name",  # Pas d'URL directe, on utilise display_name
        "key_field": None,
        "file_extension": ".html",
        "extra_fields": ["parent_id", "seo_name", "website_meta_title", "website_meta_description", "website_meta_keywords"],
        "domain": [],  # Toutes les catégories
    },
    "menus": {
        "model": "website.menu",
        "content_field": "mega_menu_content",
        "name_field": "name",
        "url_field": "url",
        "key_field": None,
        "file_extension": ".html",
        "extra_fields": ["mega_menu_classes", "parent_id", "sequence"],
        "domain": [
            ["website_id", "=", WEBSITE_ID],
            ["mega_menu_content", "!=", False],
        ],
    },
}

# Fichier de métadonnées pour tracker l'état de synchronisation
SYNC_STATE_FILE = PROJECT_ROOT / ".odoo-sync-state.json"
