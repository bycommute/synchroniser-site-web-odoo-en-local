"""
Gestionnaire de synchronisation entre Odoo et les fichiers locaux
"""
import json
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

from .config import (
    SYNC_DIRS,
    ODOO_MODELS,
    SYNC_STATE_FILE,
    PROJECT_ROOT,
    ODOO_CONFIG,
    WEBSITE_ID,
    require_child_path,
    resolve_project_path,
)
from .odoo_client import get_client
from .odoo_content_validator import validate_file


class SyncStatus(Enum):
    """États possibles d'un fichier"""
    SYNCED = "synced"           # Identique local et remote
    LOCAL_MODIFIED = "modified"  # Modifié localement
    REMOTE_MODIFIED = "remote"   # Modifié sur Odoo
    CONFLICT = "conflict"        # Modifié des deux côtés
    NEW_LOCAL = "new_local"      # Nouveau fichier local
    NEW_REMOTE = "new_remote"    # Nouveau sur Odoo
    DELETED_LOCAL = "deleted"    # Supprimé localement


@dataclass
class SyncRecord:
    """Enregistrement d'un élément synchronisé"""
    id: int
    model: str
    name: str
    url: str
    key: Optional[str]
    local_path: str
    content_hash: str
    last_sync: str
    write_date: str
    extra_data: Dict = None
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "SyncRecord":
        return cls(**data)


class SyncManager:
    """Gestionnaire principal de synchronisation"""
    PROTECTED_DYNAMIC_PAGE_KEYS = {"website_sale.product"}
    PRODUCT_STRUCTURAL_FIELDS = {"default_code", "list_price", "categ_id", "website_published"}
    BOOLEAN_METADATA_FIELDS = {"website_published"}
    DIRECT_TRANSLATION_FALLBACK_FORBIDDEN = {
        ("product.template", "website_description"),
        ("product.template", "description_ecommerce"),
        ("website.page", "arch_db"),
        ("ir.ui.view", "arch_db"),
        ("blog.post", "content"),
    }
    
    def __init__(self):
        self.client = get_client()
        self.state = self._load_state()
        self.default_lang = ODOO_CONFIG.get("lang", "fr_FR")
        self.source_lang = ODOO_CONFIG.get("source_lang", self.default_lang)
        self.active_langs = ODOO_CONFIG.get("active_langs", [self.default_lang])
        self.block_source_writes = self._as_bool(os.getenv("SYNC_BLOCK_SOURCE_WRITES", "0"))
        self.allow_html_direct_write_fallback = self._as_bool(
            os.getenv("SYNC_HTML_TRANSLATION_DIRECT_WRITE_FALLBACK", "0")
        )
        self.allow_description_sale_writes = self._as_bool(os.getenv("SYNC_ALLOW_DESCRIPTION_SALE_WRITES", "0"))
        self.allow_product_structural_writes = self._as_bool(os.getenv("SYNC_ALLOW_PRODUCT_STRUCTURAL_WRITES", "0"))
        self.allow_metadata_false_clears = self._as_bool(os.getenv("SYNC_ALLOW_METADATA_FALSE_CLEARS", "0"))
        self.enforce_file_lang_match = self._as_bool(os.getenv("SYNC_ENFORCE_FILE_LANG_MATCH", "1"))
        self._field_info_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    @staticmethod
    def _as_bool(raw: Optional[str]) -> bool:
        """Convertir une variable d'environnement textuelle en booléen."""
        if raw is None:
            return False
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _normalize_lang(self, lang: Optional[str]) -> str:
        """Normaliser la langue utilisée pour l'opération."""
        return (lang or self.default_lang).strip()

    def _context_for_lang(self, lang: str) -> Dict[str, str]:
        """Construire un contexte Odoo explicite pour une langue donnée."""
        return {"lang": lang}

    @staticmethod
    def _is_false_literal(raw: Any) -> bool:
        """True si une métadonnée locale représente explicitement False."""
        return str(raw).strip().lower() == "false"

    def _file_lang_from_path(self, file_path: Path) -> str:
        """Déduire la langue attendue à partir du suffixe de fichier local."""
        match = re.search(r"\.([a-z]{2}_[A-Z]{2})\.[^.]+$", file_path.name)
        if match:
            return match.group(1)
        return self.source_lang

    def _push_file_lang_mismatch_reason(self, file_path: Path, record_lang: str) -> Optional[str]:
        """Retourner une raison de skip si le fichier et son header n'ont pas la même langue."""
        if not self.enforce_file_lang_match:
            return None
        expected_lang = self._file_lang_from_path(file_path)
        if expected_lang == record_lang:
            return None
        return (
            f"Language mismatch: filename implies {expected_lang}, "
            f"metadata/header says {record_lang}."
        )

    def _push_field_skip_reason(self, model: str, field: str, raw: Any) -> Optional[str]:
        """Décider si une métadonnée locale est autorisée à être écrite côté Odoo."""
        if field == "parent_id":
            return "Structural many2one field parent_id is read-only from local sync."

        if model == "blog.post" and field == "blog_id" and not raw:
            return "Required many2one field blog_id cannot be cleared from local metadata."

        if model == "product.template":
            if field == "description_sale" and not self.allow_description_sale_writes:
                return "description_sale is quote-line text; writes are blocked by default."
            if field in self.PRODUCT_STRUCTURAL_FIELDS and not self.allow_product_structural_writes:
                return f"Product structural field {field} is blocked by default."

        if (
            self._is_false_literal(raw)
            and not self.allow_metadata_false_clears
            and field not in self.BOOLEAN_METADATA_FIELDS
        ):
            return "Metadata False clears are blocked by default."

        return None

    def _record_key(self, category: str, record_id: int, lang: str) -> str:
        """Clé interne de suivi: catégorie + langue + ID."""
        return f"{category}:{lang}:{record_id}"

    def _legacy_record_key(self, category: str, record_id: int) -> str:
        """Ancienne clé de suivi (sans langue), gardée pour rétrocompatibilité."""
        return f"{category}:{record_id}"

    def _get_state_record(self, category: str, record_id: int, lang: str) -> Optional[Dict]:
        """Récupérer un état avec fallback ancien format."""
        key = self._record_key(category, record_id, lang)
        record = self.state["records"].get(key)
        if record:
            return record
        if lang == self.source_lang:
            return self.state["records"].get(self._legacy_record_key(category, record_id))
        return None

    def _is_translatable_lang(self, lang: str) -> bool:
        """Déterminer si la langue cible doit créer un fichier suffixé."""
        return lang != self.source_lang

    def _file_lang_suffix(self, lang: str) -> str:
        """Suffixe stable pour les fichiers multilingues."""
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", lang)
        return f".{safe}"

    def _get_field_info(self, model: str, field_name: str) -> Dict[str, Any]:
        """Récupérer le type/translate d'un champ modèle (cache local)."""
        cache_key = (model, field_name)
        if cache_key in self._field_info_cache:
            return self._field_info_cache[cache_key]
        result = self.client.execute(
            model,
            "fields_get",
            [field_name],
            attributes=["type", "translate"],
            context=self._context_for_lang(self.source_lang),
        )
        info = result.get(field_name, {}) if isinstance(result, dict) else {}
        self._field_info_cache[cache_key] = info
        return info

    def _model_has_field(self, model: str, field_name: str) -> bool:
        """True si le modèle Odoo expose ce champ."""
        return bool(self._get_field_info(model, field_name))

    def _visibility_update_values(
        self,
        model: str,
        publish: Optional[bool] = None,
        index: Optional[bool] = None,
    ) -> Dict[str, bool]:
        """Construire les valeurs de publication/indexation explicitement demandées."""
        values: Dict[str, bool] = {}
        if publish is not None:
            if not self._model_has_field(model, "website_published"):
                raise ValueError(f"{model} ne supporte pas website_published.")
            values["website_published"] = bool(publish)

        if index is not None:
            if not self._model_has_field(model, "website_indexed"):
                raise ValueError(f"{model} ne supporte pas website_indexed.")
            values["website_indexed"] = bool(index)

        return values

    def _apply_visibility_update(
        self,
        model: str,
        record_id: int,
        context: Dict[str, str],
        publish: Optional[bool] = None,
        index: Optional[bool] = None,
    ) -> Dict[str, bool]:
        """Appliquer une publication/indexation explicite."""
        values = self._visibility_update_values(model, publish=publish, index=index)
        if values:
            self.client.write(model, [record_id], values, context=context)
        return values

    @staticmethod
    def _split_validation_issues(issues: List[Any], strict: bool = False) -> Tuple[List[Any], List[Any]]:
        """Séparer les issues bloquantes et non bloquantes pour un push."""
        blocking = [
            issue
            for issue in issues
            if issue.severity == "error" or (strict and issue.severity == "warning")
        ]
        warnings = [issue for issue in issues if issue not in blocking]
        return blocking, warnings

    def _is_html_translation_field(self, model: str, field_name: str) -> bool:
        """True si le champ est un contenu rendu traduisible via termes Odoo."""
        info = self._get_field_info(model, field_name)
        if not info.get("translate"):
            return False
        if info.get("type") == "html":
            return True
        return info.get("type") == "text" and (model, field_name) in {
            ("website.page", "arch_db"),
            ("ir.ui.view", "arch_db"),
        }

    def _read_field_in_lang(self, model: str, record_id: int, field_name: str, lang: str) -> str:
        """Lire une valeur de champ dans une langue explicite."""
        records = self.client.search_read(
            model,
            domain=[["id", "=", record_id]],
            fields=[field_name],
            limit=1,
            context=self._context_for_lang(lang),
        )
        if not records:
            raise ValueError(f"Record introuvable {model}:{record_id}")
        return records[0].get(field_name) or ""
        
    def _load_state(self) -> Dict:
        """Charger l'état de synchronisation"""
        if SYNC_STATE_FILE.exists():
            with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"records": {}, "last_sync": None}
    
    def _save_state(self):
        """Sauvegarder l'état de synchronisation"""
        with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
    
    @staticmethod
    def _compute_hash(content: str) -> str:
        """Calculer le hash MD5 d'un contenu (normalisé)"""
        if content is None:
            content = ""
        # Normaliser le contenu pour éviter les différences de whitespace
        content = content.strip()
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    
    @staticmethod
    def _slugify(text: str) -> str:
        """Convertir un texte en slug pour nom de fichier"""
        text = text.lower()
        text = re.sub(r'[àáâãäå]', 'a', text)
        text = re.sub(r'[èéêë]', 'e', text)
        text = re.sub(r'[ìíîï]', 'i', text)
        text = re.sub(r'[òóôõö]', 'o', text)
        text = re.sub(r'[ùúûü]', 'u', text)
        text = re.sub(r'[ç]', 'c', text)
        text = re.sub(r'[^\w\s-]', '', text)
        text = re.sub(r'[-\s]+', '-', text)
        return text.strip('-')[:60]
    
    def _get_local_path(self, category: str, record: Dict, config: Dict, lang: Optional[str] = None) -> Path:
        """Générer le chemin local pour un enregistrement"""
        lang = self._normalize_lang(lang)
        base_dir = SYNC_DIRS[category]
        
        # Gérer les sous-dossiers si configuré
        subfolder_field = config.get("subfolder_field")
        if subfolder_field and subfolder_field in record:
            subfolder_value = record[subfolder_field]
            # Odoo retourne [id, "name"] pour les many2one
            if isinstance(subfolder_value, (list, tuple)) and len(subfolder_value) > 1:
                subfolder_name = self._slugify(str(subfolder_value[1]))
            elif subfolder_value:
                subfolder_name = self._slugify(str(subfolder_value))
            else:
                subfolder_name = "uncategorized"
            base_dir = base_dir / subfolder_name
        
        # Créer le dossier si nécessaire
        base_dir.mkdir(parents=True, exist_ok=True)
        
        # Créer un nom de fichier basé sur l'ID et le slug du nom
        record_id = record["id"]
        name = record.get(config["name_field"], f"unnamed-{record_id}")
        slug = self._slugify(name)

        # Marquer explicitement les vues sources protégées en lecture seule
        # pour les repérer rapidement en local.
        if category == "dynamic-pages":
            key_field = config.get("key_field")
            record_key = (record.get(key_field) or "").strip() if key_field else ""
            if record_key in self.PROTECTED_DYNAMIC_PAGE_KEYS:
                filename = f"{record_id}-READ-ONLY-SOURCE-{slug}{config['file_extension']}"
                return base_dir / filename

        lang_suffix = self._file_lang_suffix(lang) if self._is_translatable_lang(lang) else ""
        filename = f"{record_id}-{slug}{lang_suffix}{config['file_extension']}"
        return base_dir / filename
    
    def _create_metadata_header(self, record: Dict, config: Dict, lang: Optional[str] = None) -> str:
        """Créer un header de métadonnées pour le fichier"""
        lang = self._normalize_lang(lang)
        meta = {
            "id": record["id"],
            "lang": lang,
            "name": record.get(config["name_field"], ""),
            "url": record.get(config["url_field"], ""),
        }
        
        if config["key_field"] and config["key_field"] in record:
            meta["key"] = record[config["key_field"]]
            
        for field in config.get("extra_fields", []):
            if field in record:
                meta[field] = record[field]
        
        # Format YAML-like dans un commentaire
        lines = ["<!--", "ODOO-SYNC-METADATA"]
        for key, value in meta.items():
            if isinstance(value, (list, tuple)):
                value = value[1] if len(value) > 1 else value[0] if value else ""
            value = "" if value is None else str(value)
            if "\n" in value:
                lines.append(f"{key}: |")
                for block_line in value.split("\n"):
                    lines.append(f"  {block_line}")
            else:
                lines.append(f"{key}: {value}")
        lines.append("-->")
        return "\n".join(lines)

    def get_active_langs(self) -> List[str]:
        """Lire les langues actives d'Odoo, avec fallback sur la configuration locale."""
        try:
            rows = self.client.search_read(
                "res.lang",
                domain=[["active", "=", True]],
                fields=["code"],
                context=self._context_for_lang(self.source_lang),
            )
            langs = [str(row.get("code") or "").strip() for row in rows]
            langs = [lang for lang in langs if lang]
            if langs:
                # Garder la langue source en premier: c'est plus lisible en local.
                ordered = [self.source_lang]
                ordered.extend(lang for lang in langs if lang != self.source_lang)
                return self._ordered_unique(ordered)
        except Exception:
            pass
        return self._ordered_unique([self.source_lang] + list(self.active_langs or []))

    @staticmethod
    def _parse_bool(value: Any, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "published"}

    def _resolve_product_category_id(self, raw: Any) -> Optional[int]:
        """Résoudre une catégorie produit interne depuis une métadonnée locale."""
        if not raw or self._is_false_literal(raw):
            return None
        value = str(raw).strip()
        if value.isdigit():
            return int(value)
        cats = self.client.search_read(
            "product.category",
            domain=[["complete_name", "=", value]],
            fields=["id"],
            limit=2,
            context=self._context_for_lang(self.source_lang),
        )
        if len(cats) == 1:
            return int(cats[0]["id"])
        return None

    def _resolve_blog_id(self, raw: Any) -> Optional[int]:
        """Résoudre un blog depuis une métadonnée locale ou le premier blog disponible."""
        if raw and not self._is_false_literal(raw):
            value = str(raw).strip()
            if value.isdigit():
                return int(value)
            blogs = self.client.search_read(
                "blog.blog",
                domain=[["name", "=", value]],
                fields=["id"],
                limit=2,
                context=self._context_for_lang(self.source_lang),
            )
            if len(blogs) == 1:
                return int(blogs[0]["id"])

        blogs = self.client.search_read(
            "blog.blog",
            domain=[],
            fields=["id"],
            limit=1,
            context=self._context_for_lang(self.source_lang),
        )
        if blogs:
            return int(blogs[0]["id"])
        return None

    def _read_created_record(self, category: str, record_id: int, lang: str) -> Dict[str, Any]:
        """Relire un objet créé avec les champs nécessaires au header/state."""
        config = ODOO_MODELS[category]
        fields = [
            "id",
            "write_date",
            config["name_field"],
            config["url_field"],
            config["content_field"],
        ]
        if config["key_field"]:
            fields.append(config["key_field"])
        fields.extend(field for field in config.get("extra_fields", []) if field not in fields)
        records = self.client.search_read(
            config["model"],
            [["id", "=", record_id]],
            fields,
            limit=1,
            context=self._context_for_lang(lang),
        )
        if not records:
            raise ValueError(f"Objet créé introuvable {config['model']}:{record_id}")
        return records[0]

    def _build_page_view_key(self, metadata: Dict, clean_content: str) -> str:
        """Déduire une clé ir.ui.view stable pour une nouvelle page."""
        raw_key = (metadata.get("key") or "").strip()
        if raw_key and raw_key.lower() != "false":
            return raw_key

        match = re.search(r't-name=["\']([^"\']+)["\']', clean_content or "")
        if match:
            return match.group(1).strip()

        url = (metadata.get("url") or "").strip().strip("/")
        name = (metadata.get("name") or "page").strip()
        base = self._slugify(url or name).replace("-", "_") or "page"
        return f"website.{base}"

    def _unique_view_key(self, base_key: str) -> str:
        """Éviter une collision de clé QWeb lors de la création d'une page."""
        key = base_key
        suffix = 2
        while self.client.search("ir.ui.view", [["key", "=", key]], limit=1):
            key = f"{base_key}_{suffix}"
            suffix += 1
        return key

    @staticmethod
    def _ensure_qweb_template(content: str, key: str) -> str:
        """Créer un template QWeb compatible website si le fichier local est brut."""
        body = content or ""
        stripped = body.strip()
        if re.search(r't-name=["\'][^"\']+["\']', body):
            return body
        if re.search(r't-call=["\']website\.layout["\']', body):
            return f'<t t-name="{key}">\n{stripped}\n</t>'
        return (
            f'<t t-name="{key}">\n'
            f'  <t t-call="website.layout">\n'
            f'    <div id="wrap" class="oe_structure">\n'
            f'{stripped}\n'
            f'    </div>\n'
            f'  </t>\n'
            f'</t>'
        )

    def _page_key_for_push(self, record_id: int, metadata: Dict, clean_content: str) -> str:
        """Trouver la clé QWeb à utiliser pour normaliser une page existante."""
        raw_key = (metadata.get("key") or "").strip()
        if raw_key and raw_key.lower() != "false":
            return raw_key

        records = self.client.search_read(
            "website.page",
            [["id", "=", record_id]],
            ["key"],
            limit=1,
            context=self._context_for_lang(self.source_lang),
        )
        if records and records[0].get("key"):
            return str(records[0]["key"]).strip()

        return self._build_page_view_key(metadata, clean_content)

    def _render_content_for_push(self, category: str, record_id: int, metadata: Dict, clean_content: str) -> str:
        """Normaliser le contenu local avant écriture/traduction Odoo."""
        if category != "pages":
            return clean_content
        key = self._page_key_for_push(record_id, metadata, clean_content)
        return self._ensure_qweb_template(clean_content, key)

    def _create_remote_page_from_file(
        self,
        file_path: Path,
        metadata: Dict,
        clean_content: str,
        lang: str,
        dry_run: bool = False,
    ) -> Dict:
        """Créer une website.page + ir.ui.view depuis un fichier local sans ID."""
        config = ODOO_MODELS["pages"]
        name = (metadata.get("name") or file_path.stem).strip()
        url = (metadata.get("url") or "").strip()
        if not url:
            url = f"/{self._slugify(name)}"
        if not url.startswith("/"):
            url = f"/{url}"

        base_key = self._build_page_view_key(metadata, clean_content)
        key = base_key if dry_run else self._unique_view_key(base_key)
        arch_db = self._ensure_qweb_template(clean_content, key)

        page_values: Dict[str, Any] = {
            "name": name,
            "url": url,
            "website_id": WEBSITE_ID,
            "website_published": self._parse_bool(metadata.get("website_published"), default=False),
        }
        if "website_indexed" in config.get("extra_fields", []):
            page_values["website_indexed"] = self._parse_bool(metadata.get("website_indexed"), default=False)
        for field in config.get("extra_fields", []):
            if field in metadata:
                raw = metadata[field]
                if field in self.BOOLEAN_METADATA_FIELDS or field == "website_indexed":
                    page_values[field] = self._parse_bool(raw, default=False)
                else:
                    page_values[field] = False if str(raw).strip().lower() == "false" else (raw or False)

        if dry_run:
            return {
                "id": None,
                "name": name,
                "url": url,
                "key": key,
                "arch_db": arch_db,
                "write_date": None,
                "created": False,
                "dry_run": True,
            }

        context = self._context_for_lang(lang)
        view_id = self.client.create(
            "ir.ui.view",
            {
                "name": name,
                "type": "qweb",
                "key": key,
                "arch_db": arch_db,
                "website_id": WEBSITE_ID,
            },
            context=context,
        )
        page_values["view_id"] = view_id
        page_id = self.client.create("website.page", page_values, context=context)
        rows = self.client.search_read(
            "website.page",
            domain=[["id", "=", page_id]],
            fields=["id", "name", "url", "key", "write_date", *config.get("extra_fields", [])],
            limit=1,
            context=context,
        )
        record = rows[0] if rows else {"id": page_id, "name": name, "url": url, "key": key, "write_date": None}
        record["arch_db"] = arch_db
        record["created"] = True
        return record

    def _create_remote_product_from_file(
        self,
        file_path: Path,
        metadata: Dict,
        clean_content: str,
        lang: str,
        dry_run: bool = False,
    ) -> Dict:
        """Créer un product.template depuis un fichier local sans ID."""
        if lang != self.source_lang:
            raise ValueError("La création de produit doit se faire depuis la langue source, pas depuis une traduction.")

        config = ODOO_MODELS["products"]
        name = (metadata.get("name") or file_path.stem).strip()
        values: Dict[str, Any] = {
            "name": name,
            "sale_ok": True,
            "active": True,
            "website_published": self._parse_bool(metadata.get("website_published"), default=False),
            config["content_field"]: clean_content,
        }

        for field in config.get("extra_fields", []):
            if field not in metadata:
                continue
            raw = metadata[field]
            if field == "website_published":
                values[field] = self._parse_bool(raw, default=False)
            elif field == "list_price":
                if raw and not self._is_false_literal(raw):
                    values[field] = float(str(raw).replace(",", "."))
            elif field == "categ_id":
                categ_id = self._resolve_product_category_id(raw)
                if categ_id:
                    values[field] = categ_id
            elif self._is_false_literal(raw):
                values[field] = False
            elif raw:
                values[field] = raw

        if dry_run:
            return {
                "id": None,
                "name": name,
                "website_url": metadata.get("url", ""),
                config["content_field"]: clean_content,
                "write_date": None,
                "created": False,
                "dry_run": True,
            }

        product_id = self.client.create(config["model"], values, context=self._context_for_lang(lang))
        record = self._read_created_record("products", int(product_id), lang)
        record["created"] = True
        return record

    def _create_remote_blog_post_from_file(
        self,
        file_path: Path,
        metadata: Dict,
        clean_content: str,
        lang: str,
        dry_run: bool = False,
    ) -> Dict:
        """Créer un blog.post depuis un fichier local sans ID."""
        if lang != self.source_lang:
            raise ValueError("La création d'article doit se faire depuis la langue source, pas depuis une traduction.")

        config = ODOO_MODELS["blog-posts"]
        name = (metadata.get("name") or file_path.stem).strip()
        blog_id = self._resolve_blog_id(metadata.get("blog_id"))
        if not blog_id:
            raise ValueError("Aucun blog.blog disponible pour créer l'article.")

        fields = self.client.execute(
            config["model"],
            "fields_get",
            ["website_published"],
            attributes=["type"],
            context=self._context_for_lang(self.source_lang),
        )
        values: Dict[str, Any] = {
            "name": name,
            "blog_id": blog_id,
            config["content_field"]: clean_content,
        }
        if "website_published" in fields:
            values["website_published"] = self._parse_bool(metadata.get("website_published"), default=False)

        for field in config.get("extra_fields", []):
            if field not in metadata or field in {"blog_id", "website_published"}:
                continue
            raw = metadata[field]
            if self._is_false_literal(raw):
                values[field] = False
            elif raw:
                values[field] = raw

        if dry_run:
            return {
                "id": None,
                "name": name,
                "website_url": metadata.get("url", ""),
                config["content_field"]: clean_content,
                "write_date": None,
                "created": False,
                "dry_run": True,
            }

        post_id = self.client.create(config["model"], values, context=self._context_for_lang(lang))
        record = self._read_created_record("blog-posts", int(post_id), lang)
        record["created"] = True
        return record

    def _parse_metadata_header(self, content: str) -> Tuple[Dict, str]:
        """Parser le header de métadonnées et retourner (metadata, content_sans_header)"""
        pattern = r'<!--\s*ODOO-SYNC-METADATA\s*(.*?)\s*-->\s*'
        match = re.search(pattern, content, re.DOTALL)
        
        if not match:
            return {}, content
            
        meta_str = match.group(1)
        metadata = {}
        
        current_multiline_key: Optional[str] = None
        current_multiline_lines: List[str] = []

        def _flush_multiline() -> None:
            nonlocal current_multiline_key, current_multiline_lines
            if current_multiline_key is None:
                return
            metadata[current_multiline_key] = "\n".join(current_multiline_lines).rstrip("\n")
            current_multiline_key = None
            current_multiline_lines = []

        for raw_line in meta_str.strip().split("\n"):
            if current_multiline_key is not None:
                if raw_line.startswith("  "):
                    current_multiline_lines.append(raw_line[2:])
                    continue
                _flush_multiline()

            if ":" not in raw_line:
                continue

            key, value = raw_line.split(":", 1)
            key = key.strip()
            value = value.strip()

            if value == "|":
                current_multiline_key = key
                current_multiline_lines = []
                continue

            metadata[key] = value

        _flush_multiline()

        # Retirer le header du contenu (inclut les whitespaces après le header)
        clean_content = re.sub(pattern, "", content, flags=re.DOTALL)
        return metadata, clean_content

    @staticmethod
    def _ordered_unique(items: List[str]) -> List[str]:
        """Dédupliquer en conservant l'ordre."""
        out: List[str] = []
        seen = set()
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _extract_html_term_mapping_from_rendered(
        self,
        source_html: str,
        target_html: str,
        source_terms: List[str],
    ) -> Dict[str, str]:
        """
        Déduire une map `source_term -> translated_term` à partir de 2 HTML
        ayant la même structure (source vs cible).
        """
        if source_html is None:
            source_html = ""
        if target_html is None:
            target_html = ""
        if source_html == target_html:
            return {term: term for term in source_terms}

        chunks: List[str] = []
        bound_terms: List[Tuple[str, str]] = []
        cursor = 0
        for term in source_terms:
            if not term:
                continue
            idx = source_html.find(term, cursor)
            if idx < 0:
                continue
            chunks.append(re.escape(source_html[cursor:idx]))
            group_name = f"t{len(bound_terms)}"
            chunks.append(f"(?P<{group_name}>.*?)")
            bound_terms.append((group_name, term))
            cursor = idx + len(term)

        chunks.append(re.escape(source_html[cursor:]))
        pattern = "^" + "".join(chunks) + "$"
        match = re.match(pattern, target_html, flags=re.DOTALL)
        if not match:
            raise ValueError(
                "Impossible de déduire les termes HTML traduits automatiquement. "
                "La structure source/cible semble différente."
            )

        out: Dict[str, str] = {}
        for group_name, term in bound_terms:
            out[term] = match.group(group_name)
        return out

    def _update_html_translation_from_rendered_content(
        self,
        model: str,
        record_id: int,
        field_name: str,
        target_lang: str,
        rendered_target_content: str,
    ) -> None:
        """
        Mettre à jour une traduction HTML via `update_field_translations`
        sans écraser la langue source.
        """
        target_ctx = self._context_for_lang(target_lang)

        translations_raw = self.client.execute(
            model,
            "get_field_translations",
            [record_id],
            field_name,
            context=target_ctx,
        )
        if not translations_raw or not isinstance(translations_raw, list):
            raise ValueError("Impossible de lire les termes de traduction HTML.")

        rows = translations_raw[0] if translations_raw else []
        if not rows:
            raise ValueError("Aucun terme de traduction trouvé pour ce champ HTML.")

        # Langue source prioritaire: toujours celle configurée.
        # Si absente du set de traductions, fallback contrôlé.
        effective_source_lang = self.source_lang
        available_langs = self._ordered_unique([
            r.get("lang", "")
            for r in rows
            if r.get("lang")
        ])

        def _source_terms_for(lang_code: str) -> List[str]:
            return self._ordered_unique([
                str(r.get("source", ""))
                for r in rows
                if r.get("lang") == lang_code and r.get("source")
            ])

        source_terms = _source_terms_for(effective_source_lang)
        if not source_terms:
            # Fallback: une langue avec des termes source non vides,
            # en évitant la langue cible si possible.
            candidates: List[str] = []
            for lang_code in available_langs:
                if _source_terms_for(lang_code):
                    candidates.append(lang_code)

            non_target = [l for l in candidates if l != target_lang]
            if non_target:
                effective_source_lang = non_target[0]
            elif candidates:
                effective_source_lang = candidates[0]

            source_terms = _source_terms_for(effective_source_lang)

        source_ctx = self._context_for_lang(effective_source_lang)
        if target_lang == effective_source_lang:
            # Si on modifie la langue source réelle du champ, il faut écrire la valeur
            # directement (pas via update_field_translations).
            self.client.write(
                model,
                [record_id],
                {field_name: rendered_target_content or ""},
                context=target_ctx,
            )
            return

        canonical_source_html = self._read_field_in_lang(model, record_id, field_name, self.source_lang)
        source_html = self._read_field_in_lang(model, record_id, field_name, effective_source_lang)
        target_record = self.client.search_read(
            model,
            domain=[["id", "=", record_id]],
            fields=[field_name],
            limit=1,
            context=target_ctx,
        )
        current_target_html = target_record[0].get(field_name) if target_record else ""
        if current_target_html is None:
            current_target_html = ""

        if not source_terms:
            raise ValueError("Aucun terme source détecté pour ce champ HTML.")

        # `get_field_translations` peut retourner les termes dans un ordre qui ne
        # suit pas le flux réel du HTML. On les réaligne pour fiabiliser
        # l'extraction source->cible.
        positioned_terms: List[Tuple[int, str]] = []
        for term in source_terms:
            idx = source_html.find(term)
            if idx < 0:
                continue
            positioned_terms.append((idx, term))
        if not positioned_terms:
            # Odoo peut réorganiser les "source terms" après une première
            # traduction: les termes source retournés pour fr_FR peuvent alors
            # correspondre au HTML en_US. On choisit la langue dont les termes
            # correspondent réellement à son HTML courant.
            for candidate_lang in available_langs:
                if candidate_lang == target_lang:
                    continue
                candidate_terms = _source_terms_for(candidate_lang)
                if not candidate_terms:
                    continue
                candidate_html = self._read_field_in_lang(model, record_id, field_name, candidate_lang)
                candidate_positions = [
                    (candidate_html.find(term), term)
                    for term in candidate_terms
                    if candidate_html.find(term) >= 0
                ]
                if candidate_positions:
                    effective_source_lang = candidate_lang
                    source_ctx = self._context_for_lang(effective_source_lang)
                    source_html = candidate_html
                    source_terms = self._ordered_unique([
                        term for _, term in sorted(candidate_positions, key=lambda x: x[0])
                    ])
                    positioned_terms = candidate_positions
                    break
        if positioned_terms:
            source_terms = self._ordered_unique([
                term for _, term in sorted(positioned_terms, key=lambda x: x[0])
            ])

        term_mapping: Optional[Dict[str, str]] = None
        # 1) Tentative principale: alignement source HTML (langue source) -> HTML cible désiré.
        try:
            term_mapping = self._extract_html_term_mapping_from_rendered(
                source_html=source_html,
                target_html=rendered_target_content or "",
                source_terms=source_terms,
            )
        except ValueError:
            # 2) Fallback robuste: alignement HTML cible courant -> HTML cible désiré.
            #    Utile quand le "source term" historique ne matche plus exactement le HTML source.
            try:
                target_term_by_source: Dict[str, str] = {}
                for src in source_terms:
                    target_value = None
                    for r in rows:
                        if r.get("lang") == target_lang and r.get("source") == src:
                            target_value = r.get("value")
                            break
                    target_term_by_source[src] = target_value or src

                target_terms = [target_term_by_source[src] for src in source_terms]
                target_based_map = self._extract_html_term_mapping_from_rendered(
                    source_html=current_target_html,
                    target_html=rendered_target_content or "",
                    source_terms=target_terms,
                )
                term_mapping = {}
                for src in source_terms:
                    current_term = target_term_by_source[src]
                    term_mapping[src] = target_based_map.get(current_term, current_term)
            except ValueError:
                # 3) Fallback ultime: écriture directe dans le contexte de langue cible.
                #    Ce mode est dangereux sur les champs HTML traduisibles Odoo:
                #    selon le modèle, il peut écraser la valeur source au lieu de
                #    seulement renseigner une traduction. Il doit donc rester opt-in.
                if self.allow_html_direct_write_fallback:
                    if (model, field_name) in self.DIRECT_TRANSLATION_FALLBACK_FORBIDDEN:
                        raise ValueError(
                            f"Fallback direct interdit sur {model}.{field_name}: "
                            "risque de contamination entre langues."
                        )
                    self.client.write(
                        model,
                        [record_id],
                        {field_name: rendered_target_content or ""},
                        context=target_ctx,
                    )
                    return
                raise

        safe_term_mapping = {
            str(source): "" if value is None else str(value)
            for source, value in (term_mapping or {}).items()
        }

        self.client.execute(
            model,
            "update_field_translations",
            [record_id],
            field_name,
            {target_lang: safe_term_mapping},
            source_lang=effective_source_lang,
            context=target_ctx,
        )

        source_html_after = self._read_field_in_lang(model, record_id, field_name, effective_source_lang)
        if source_html_after != source_html:
            self.client.write(
                model,
                [record_id],
                {field_name: source_html},
                context=source_ctx,
            )
            raise RuntimeError(
                "Sécurité sync: update_field_translations a modifié la langue source. "
                "La source a été restaurée et l'opération est interrompue."
            )

        canonical_source_html_after = self._read_field_in_lang(model, record_id, field_name, self.source_lang)
        if canonical_source_html_after != canonical_source_html:
            self.client.write(
                model,
                [record_id],
                {field_name: canonical_source_html},
                context=self._context_for_lang(self.source_lang),
            )
            raise RuntimeError(
                "Sécurité sync: update_field_translations a modifié la langue source configurée. "
                "La source a été restaurée et l'opération est interrompue."
            )
    
    def pull(
        self,
        category: str = None,
        force: bool = False,
        ids: List[int] = None,
        prune: bool = False,
        content_only: bool = False,
        lang: Optional[str] = None,
    ) -> Dict:
        """
        Récupérer les données depuis Odoo vers les fichiers locaux
        
        Args:
            category: 'pages', 'blog-posts', 'products' ou None pour tout
            force: Écraser les modifications locales
            ids: Liste d'IDs spécifiques à récupérer
            prune: (blog-posts/products) Supprimer en local les fichiers absents sur Odoo / doublons obsolètes (si non modifiés localement)
            content_only: Synchroniser uniquement le champ de contenu (ignorer les champs metadata/SEO)
            lang: Langue cible de synchronisation (ex: fr_FR, en_US)
            
        Returns:
            Résumé des opérations effectuées
        """
        results = {"pulled": [], "skipped": [], "errors": []}
        lang = self._normalize_lang(lang)
        context = self._context_for_lang(lang)
        # Pour le prune (index remote + chemin attendu)
        blog_posts_remote_ids: Optional[set[int]] = None
        blog_posts_expected_paths: Optional[Dict[int, str]] = None
        products_remote_ids: Optional[set[int]] = None
        products_expected_paths: Optional[Dict[int, str]] = None
        
        categories = [category] if category else list(ODOO_MODELS.keys())
        
        for cat in categories:
            config = ODOO_MODELS[cat]
            base_dir = SYNC_DIRS[cat]
            base_dir.mkdir(parents=True, exist_ok=True)
            extra_fields = [] if content_only else config.get("extra_fields", [])
            
            # Construire les champs à récupérer
            fields = [
                "id", 
                config["name_field"], 
                config["content_field"],
                config["url_field"],
                "write_date"
            ]
            if config["key_field"]:
                fields.append(config["key_field"])
            fields.extend(extra_fields)
            # Ajouter le champ subfolder s'il n'est pas déjà inclus
            subfolder_field = config.get("subfolder_field")
            if subfolder_field and subfolder_field not in fields:
                fields.append(subfolder_field)
            
            # Construire le domaine
            domain = config.get("domain", []).copy()
            if ids:
                domain.append(["id", "in", ids])
            
            try:
                records = self.client.search_read(
                    config["model"],
                    domain=domain,
                    fields=fields,
                    context=context,
                )

                # Index remote pour le prune (uniquement pour un pull complet)
                if prune and ids is None and cat in ("blog-posts", "products"):
                    remote_ids = {int(r["id"]) for r in records}
                    expected_paths: Dict[int, str] = {}
                    for r in records:
                        try:
                            p = self._get_local_path(cat, r, config, lang=lang)
                            expected_paths[int(r["id"])] = str(p.relative_to(PROJECT_ROOT))
                        except Exception:
                            # Si on ne peut pas calculer un path pour un record, on évite d'utiliser ce record pour le prune
                            continue
                    if cat == "blog-posts":
                        blog_posts_remote_ids = remote_ids
                        blog_posts_expected_paths = expected_paths
                    elif cat == "products":
                        products_remote_ids = remote_ids
                        products_expected_paths = expected_paths
                
                for record in records:
                    try:
                        local_path = self._get_local_path(cat, record, config, lang=lang)
                        content = record.get(config["content_field"]) or ""
                        
                        # Vérifier si le fichier local existe et a été modifié
                        record_key = self._record_key(cat, record["id"], lang)
                        state_record = self._get_state_record(cat, record["id"], lang)
                        
                        if local_path.exists() and not force:
                            local_content = local_path.read_text(encoding="utf-8")
                            _, clean_local = self._parse_metadata_header(local_content)
                            local_hash = self._compute_hash(clean_local)
                            
                            if state_record and local_hash != state_record.get("content_hash"):
                                results["skipped"].append({
                                    "id": record["id"],
                                    "name": record.get(config["name_field"]),
                                    "reason": "Local modifications detected. Use --force to overwrite."
                                })
                                continue
                        
                        # Créer le contenu avec header
                        record_for_header = dict(record)
                        if content_only and config.get("extra_fields"):
                            for f in config.get("extra_fields", []):
                                record_for_header.pop(f, None)
                        header = self._create_metadata_header(record_for_header, config, lang=lang)
                        full_content = f"{header}\n\n{content}"
                        
                        # Écrire le fichier
                        local_path.write_text(full_content, encoding="utf-8")
                        
                        # Hash des métadonnées (pour push ultérieur : détecter changement SEO sans toucher au contenu)
                        extra = extra_fields
                        def _val(r, k):
                            v = r.get(k)
                            if v is None: return ""
                            if isinstance(v, (list, tuple)) and len(v) > 1: return str(v[1])
                            return str(v)
                        _meta_str = "|".join(f"{k}={_val(record, k)}" for k in sorted(extra))
                        _meta_hash = hashlib.sha256(_meta_str.encode("utf-8")).hexdigest() if _meta_str else ""
                        # Mettre à jour l'état
                        self.state["records"][record_key] = {
                            "id": record["id"],
                            "lang": lang,
                            "model": config["model"],
                            "name": record.get(config["name_field"]),
                            "url": record.get(config["url_field"]),
                            "key": record.get(config["key_field"]) if config["key_field"] else None,
                            "local_path": str(local_path.relative_to(PROJECT_ROOT)),
                            "content_hash": self._compute_hash(content),
                            "metadata_hash": _meta_hash,
                            "last_sync": datetime.now().isoformat(),
                            "write_date": record.get("write_date"),
                        }
                        
                        results["pulled"].append({
                            "id": record["id"],
                            "name": record.get(config["name_field"]),
                            "path": str(local_path.relative_to(PROJECT_ROOT))
                        })
                        
                    except Exception as e:
                        results["errors"].append({
                            "id": record["id"],
                            "error": str(e)
                        })
                        
            except Exception as e:
                results["errors"].append({
                    "category": cat,
                    "error": str(e)
                })
        
        # Prune local (blog-posts / products)
        if prune:
            if ids is not None:
                # Sécurité: ne jamais prune sur un pull partiel
                results["errors"].append({
                    "error": "Prune demandé mais --ids est utilisé (pull partiel). Prune ignoré."
                })
            else:
                pruned_global = {"deleted": [], "skipped": [], "errors": []}

                if "blog-posts" in categories:
                    if blog_posts_remote_ids is None or blog_posts_expected_paths is None:
                        pruned_global["errors"].append({"category": "blog-posts", "error": "Index remote incomplet, prune ignoré."})
                    else:
                        pruned = self._prune_category_files(
                            category="blog-posts",
                            remote_ids=blog_posts_remote_ids,
                            expected_paths=blog_posts_expected_paths,
                            force=force,
                            lang=lang,
                        )
                        pruned_global["deleted"].extend(pruned.get("deleted", []))
                        pruned_global["skipped"].extend(pruned.get("skipped", []))
                        pruned_global["errors"].extend(pruned.get("errors", []))

                if "products" in categories:
                    if products_remote_ids is None or products_expected_paths is None:
                        pruned_global["errors"].append({"category": "products", "error": "Index remote incomplet, prune ignoré."})
                    else:
                        pruned = self._prune_category_files(
                            category="products",
                            remote_ids=products_remote_ids,
                            expected_paths=products_expected_paths,
                            force=force,
                            lang=lang,
                        )
                        pruned_global["deleted"].extend(pruned.get("deleted", []))
                        pruned_global["skipped"].extend(pruned.get("skipped", []))
                        pruned_global["errors"].extend(pruned.get("errors", []))

                results["pruned"] = pruned_global

        self.state["last_sync"] = datetime.now().isoformat()
        self._save_state()
        
        return results

    def _prune_category_files(
        self,
        category: str,
        remote_ids: set,
        expected_paths: Dict[int, str],
        force: bool = False,
        lang: Optional[str] = None,
    ) -> Dict:
        """
        Supprimer en local les fichiers d'une catégorie qui n'existent plus sur Odoo (ou les doublons obsolètes),
        sans supprimer un fichier modifié localement (sauf force=True).
        """
        results = {"deleted": [], "skipped": [], "errors": []}
        lang = self._normalize_lang(lang)
        if category not in ODOO_MODELS:
            return results

        config = ODOO_MODELS[category]
        base_dir = SYNC_DIRS[category]
        if not base_dir.exists():
            return results

        # 1) Supprimer les fichiers locaux dont l'ID n'existe plus (ou doublons d'ID à l'ancien chemin)
        for file_path in base_dir.glob(f"**/*{config['file_extension']}"):
            try:
                rel_path = str(file_path.relative_to(PROJECT_ROOT))
                content = file_path.read_text(encoding="utf-8")
                metadata, clean_content = self._parse_metadata_header(content)
                if "id" not in metadata:
                    continue

                record_id = int(metadata["id"])
                state_record = self._get_state_record(category, record_id, lang)

                should_delete = False
                delete_reason = ""

                if record_id not in remote_ids:
                    should_delete = True
                    delete_reason = "Record absent sur Odoo"
                else:
                    expected = expected_paths.get(record_id)
                    # Si Odoo existe mais le fichier n'est pas à l'emplacement attendu, c'est un doublon obsolète
                    if expected and rel_path != expected:
                        should_delete = True
                        delete_reason = "Doublon obsolète (slug/chemin changé)"

                if not should_delete:
                    continue

                # Sécurité: ne pas supprimer si modifié localement (sauf --force)
                current_hash = self._compute_hash(clean_content)
                if not force and state_record and current_hash != state_record.get("content_hash"):
                    results["skipped"].append({
                        "category": category,
                        "id": record_id,
                        "name": metadata.get("name", ""),
                        "path": rel_path,
                        "reason": f"{delete_reason} mais modifications locales détectées"
                    })
                    continue

                # Si pas de state_record, on évite de supprimer automatiquement (trop risqué)
                if not force and not state_record:
                    results["skipped"].append({
                        "category": category,
                        "id": record_id,
                        "name": metadata.get("name", ""),
                        "path": rel_path,
                        "reason": f"{delete_reason} mais fichier hors état de sync (non supprimé)"
                    })
                    continue

                # Supprimer le fichier
                file_path.unlink()
                results["deleted"].append({
                    "category": category,
                    "id": record_id,
                    "name": metadata.get("name", ""),
                    "path": rel_path
                })

            except Exception as e:
                results["errors"].append({
                    "category": category,
                    "path": str(file_path),
                    "error": str(e)
                })

        # 2) Nettoyer l'état: enlever les records qui n'existent plus sur Odoo
        keys_to_remove = []
        for key in list(self.state.get("records", {}).keys()):
            parts = key.split(":")
            if len(parts) == 3:
                cat_key, lang_key, rid_str = parts
                if cat_key != category or lang_key != lang:
                    continue
            elif len(parts) == 2:
                cat_key, rid_str = parts
                if cat_key != category:
                    continue
                if lang != self.source_lang:
                    continue
            else:
                continue

            try:
                rid = int(rid_str)
            except Exception:
                continue
            if rid not in remote_ids:
                keys_to_remove.append(key)
        for k in keys_to_remove:
            self.state["records"].pop(k, None)

        # 3) Supprimer les dossiers vides
        try:
            for d in sorted([p for p in base_dir.glob("**/*") if p.is_dir()], key=lambda p: len(str(p)), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        except Exception:
            pass

        return results
    
    def push(
        self,
        category: str = None,
        files: List[str] = None,
        force: bool = False,
        content_only: bool = False,
        lang: Optional[str] = None,
        create_missing: bool = False,
        dry_run: bool = False,
        publish: Optional[bool] = None,
        index: Optional[bool] = None,
        validate: bool = True,
        strict_validate: bool = False,
    ) -> Dict:
        """
        Pousser les modifications locales vers Odoo
        
        Args:
            category: Catégorie spécifique ou None pour tout
            files: Liste de chemins de fichiers spécifiques
            force: Écraser même si le remote a changé
            content_only: Pousser uniquement le champ de contenu (ignorer les métadonnées du header)
            lang: Langue cible (fallback si absente des métadonnées)
            create_missing: Créer une page Odoo si un fichier pages/ n'a pas encore d'ID
            dry_run: Simuler sans écrire sur Odoo
            publish: Publier/dépublier explicitement l'objet poussé
            index: Indexer/désindexer explicitement l'objet poussé si le modèle le supporte
            validate: Valider localement le fichier avant push
            strict_validate: Rendre les warnings de validation bloquants
            
        Returns:
            Résumé des opérations effectuées
        """
        results = {"pushed": [], "skipped": [], "errors": [], "warnings": []}
        default_push_lang = self._normalize_lang(lang)
        visibility_requested = publish is not None or index is not None
        
        categories = [category] if category else list(ODOO_MODELS.keys())
        
        for cat in categories:
            config = ODOO_MODELS[cat]
            base_dir = SYNC_DIRS[cat]
            
            if not base_dir.exists():
                continue

            # Lister les fichiers à traiter (inclut les sous-dossiers)
            if files:
                # Résoudre les chemins relatifs par rapport à PROJECT_ROOT
                file_paths = []
                for f in files:
                    p = resolve_project_path(f)
                    if not p.exists():
                        continue
                    # Accepte le fichier uniquement s'il appartient au dossier de la catégorie
                    try:
                        require_child_path(p, base_dir, f"{cat}/")
                        file_paths.append(p)
                    except ValueError:
                        continue
            else:
                file_paths = list(base_dir.glob(f"**/*{config['file_extension']}"))

            if not file_paths:
                continue

            try:
                visibility_dry_values = self._visibility_update_values(
                    config["model"],
                    publish=publish,
                    index=index,
                ) if visibility_requested else {}
            except ValueError as e:
                results["errors"].append({
                    "category": cat,
                    "error": str(e),
                })
                continue
            
            for file_path in file_paths:
                try:
                    if validate:
                        validation_issues = validate_file(file_path, category=cat)
                        blocking_issues, warning_issues = self._split_validation_issues(
                            validation_issues,
                            strict=strict_validate,
                        )
                        for issue in warning_issues:
                            results["warnings"].append({
                                "file": issue.path,
                                "code": issue.code,
                                "message": issue.message,
                            })
                        if blocking_issues:
                            for issue in blocking_issues:
                                results["errors"].append({
                                    "file": issue.path,
                                    "error": f"Validation {issue.code}: {issue.message}",
                                })
                            continue

                    content = file_path.read_text(encoding="utf-8")
                    metadata, clean_content = self._parse_metadata_header(content)
                    
                    if "id" not in metadata:
                        if cat in {"pages", "products", "blog-posts"} and create_missing:
                            record_lang = self._normalize_lang(metadata.get("lang") or default_push_lang)
                            if cat == "pages":
                                created = self._create_remote_page_from_file(
                                    file_path=file_path,
                                    metadata=metadata,
                                    clean_content=clean_content,
                                    lang=record_lang,
                                    dry_run=dry_run,
                                )
                                operation = "create-page"
                            elif cat == "products":
                                created = self._create_remote_product_from_file(
                                    file_path=file_path,
                                    metadata=metadata,
                                    clean_content=clean_content,
                                    lang=record_lang,
                                    dry_run=dry_run,
                                )
                                operation = "create-product"
                            else:
                                created = self._create_remote_blog_post_from_file(
                                    file_path=file_path,
                                    metadata=metadata,
                                    clean_content=clean_content,
                                    lang=record_lang,
                                    dry_run=dry_run,
                                )
                                operation = "create-blog-post"

                            if dry_run:
                                dry_item = {
                                    "id": created.get("id"),
                                    "file": str(file_path.name),
                                    "name": created.get("name", ""),
                                    "dry_run": True,
                                    "operation": operation,
                                }
                                if visibility_requested:
                                    dry_item["visibility"] = visibility_dry_values
                                results["pushed"].append(dry_item)
                                continue

                            record_id = int(created["id"])
                            visibility_values = self._apply_visibility_update(
                                config["model"],
                                record_id,
                                self._context_for_lang(record_lang),
                                publish=publish,
                                index=index,
                            )
                            if visibility_values:
                                created = self._read_created_record(cat, record_id, record_lang)
                            header = self._create_metadata_header(created, config, lang=record_lang)
                            created_content = created.get(config["content_field"]) or clean_content
                            file_path.write_text(f"{header}\n\n{created_content}", encoding="utf-8")
                            meta_str = "|".join(
                                f"{k}={created.get(k, '')}"
                                for k in sorted(config.get("extra_fields", []))
                                if k in created
                            )
                            meta_hash = hashlib.sha256(meta_str.encode("utf-8")).hexdigest() if meta_str else ""
                            record_key = self._record_key(cat, record_id, record_lang)
                            self.state["records"][record_key] = {
                                "id": record_id,
                                "lang": record_lang,
                                "model": config["model"],
                                "name": created.get(config["name_field"]),
                                "url": created.get(config["url_field"]),
                                "key": created.get(config["key_field"]) if config["key_field"] else None,
                                "local_path": str(file_path.relative_to(PROJECT_ROOT)),
                                "content_hash": self._compute_hash(created_content),
                                "metadata_hash": meta_hash,
                                "last_sync": datetime.now().isoformat(),
                                "write_date": created.get("write_date"),
                            }
                            pushed_item = {
                                "id": record_id,
                                "file": str(file_path.name),
                                "name": created.get("name", ""),
                                "operation": operation,
                            }
                            if visibility_values:
                                pushed_item["visibility"] = visibility_values
                            results["pushed"].append(pushed_item)
                            continue

                        results["errors"].append({
                            "file": str(file_path),
                            "error": "No ID found in metadata header. Use --create for new local pages/products/blog posts."
                        })
                        continue
                    
                    record_id = int(metadata["id"])
                    record_lang = self._normalize_lang(metadata.get("lang") or default_push_lang)
                    lang_mismatch_reason = self._push_file_lang_mismatch_reason(file_path, record_lang)
                    if lang_mismatch_reason:
                        results["skipped"].append({
                            "id": record_id,
                            "file": str(file_path.name),
                            "reason": lang_mismatch_reason,
                        })
                        continue

                    context = self._context_for_lang(record_lang)
                    record_key = self._record_key(cat, record_id, record_lang)
                    state_record = self._get_state_record(cat, record_id, record_lang)

                    # Garde-fou multilingue: empêcher toute écriture sur la langue source
                    # (souvent fr_FR) pendant les pushes de traductions.
                    if self.block_source_writes and record_lang == self.source_lang:
                        results["skipped"].append({
                            "id": record_id,
                            "file": str(file_path.name),
                            "reason": (
                                f"Source language writes are blocked (lang={record_lang}). "
                                "Set SYNC_BLOCK_SOURCE_WRITES=0 to override intentionally."
                            ),
                        })
                        continue

                    # Garde-fou: on ne pousse jamais la vue source principale.
                    # Elle peut être pull pour référence, mais jamais modifiée depuis le local.
                    if cat == "dynamic-pages":
                        meta_key = (metadata.get("key") or "").strip()
                        if meta_key in self.PROTECTED_DYNAMIC_PAGE_KEYS:
                            results["skipped"].append({
                                "id": record_id,
                                "file": str(file_path.name),
                                "reason": "Protected source view: pull allowed, push blocked."
                            })
                            continue
                    
                    # Hash des métadonnées (SEO) pour détecter un changement titre/description/keywords sans toucher au contenu
                    extra = [] if content_only else config.get("extra_fields", [])
                    meta_str = "|".join(f"{k}={metadata.get(k, '')}" for k in sorted(extra) if k in metadata)
                    meta_hash = hashlib.sha256(meta_str.encode("utf-8")).hexdigest() if meta_str else ""
                    current_hash = self._compute_hash(clean_content)
                    rendered_content = self._render_content_for_push(cat, record_id, metadata, clean_content)
                    content_changed = not state_record or current_hash != state_record.get("content_hash")
                    if (
                        state_record
                        and current_hash == state_record.get("content_hash")
                        and meta_hash == state_record.get("metadata_hash", "")
                        and not visibility_requested
                    ):
                        results["skipped"].append({
                            "id": record_id,
                            "file": str(file_path.name),
                            "reason": "No changes detected"
                        })
                        continue
                    
                    # Vérifier si le remote a changé (sauf si force)
                    if not force and state_record:
                        remote_records = self.client.search_read(
                            config["model"],
                            domain=[["id", "=", record_id]],
                            fields=["write_date"],
                            context=context,
                        )
                        if remote_records:
                            remote_write_date = remote_records[0].get("write_date")
                            if remote_write_date != state_record.get("write_date"):
                                results["skipped"].append({
                                    "id": record_id,
                                    "file": str(file_path.name),
                                    "reason": "Remote has been modified. Use --force to overwrite or pull first."
                                })
                                continue
                    
                    # Pousser le contenu + les métadonnées SEO (title, description, keywords, etc.)
                    content_field = config["content_field"]
                    update_data: Dict[str, Any] = {}
                    deferred_html_translations: Dict[str, str] = {}
                    skipped_metadata_fields: Dict[str, str] = {}

                    if content_changed:
                        if (
                            record_lang != self.source_lang
                            and self._is_html_translation_field(config["model"], content_field)
                        ):
                            deferred_html_translations[content_field] = rendered_content
                        else:
                            update_data[content_field] = rendered_content

                    for field in extra:
                        if field in metadata:
                            raw = metadata[field]
                            skip_reason = self._push_field_skip_reason(config["model"], field, raw)
                            if skip_reason:
                                skipped_metadata_fields[field] = skip_reason
                                continue

                            # Champs "False" (au sens Python) dans les métadonnées.
                            if self._is_false_literal(raw):
                                update_data[field] = False
                                continue

                            # Many2one structurel: `parent_id` est stocké localement comme
                            # libellé (ex: "Menu principal du site web 1") ou "False".
                            # Écrire ce champ depuis le local casse le lien côté Odoo.
                            # On ne pousse JAMAIS parent_id (champ structurel, pas du contenu).
                            if field == "parent_id":
                                continue

                            # Many2one: `categ_id` est stocké localement comme libellé (ex: "A / B").
                            # Odoo attend un ID (int). On résout par `complete_name` sinon on n'écrit pas.
                            if field == "categ_id":
                                if not raw:
                                    update_data[field] = False
                                    continue
                                if str(raw).strip().isdigit():
                                    update_data[field] = int(str(raw).strip())
                                    continue

                                # Résolution best-effort (évite de casser les pushes si la catégorie est ambiguë).
                                cats = self.client.search_read(
                                    "product.category",
                                    domain=[["complete_name", "=", raw]],
                                    fields=["id"],
                                    context=self._context_for_lang(self.source_lang),
                                )
                                if len(cats) == 1:
                                    update_data[field] = cats[0]["id"]
                                else:
                                    # Catégorie non trouvée ou ambiguë: ne pas forcer une valeur invalide.
                                    continue
                                continue

                            # Many2one: `blog_id` est stocké localement comme libellé (ex: "ByCommute").
                            # Odoo attend un ID (int). On résout par `name` sinon on n'écrit pas.
                            if field == "blog_id":
                                if not raw:
                                    # `blog_id` est normalement requis sur blog.post: ne pas tenter d'écraser.
                                    continue
                                if str(raw).strip().isdigit():
                                    update_data[field] = int(str(raw).strip())
                                    continue

                                blogs = self.client.search_read(
                                    "blog.blog",
                                    domain=[["name", "=", raw]],
                                    fields=["id"],
                                    context=self._context_for_lang(self.source_lang),
                                )
                                if len(blogs) == 1:
                                    update_data[field] = blogs[0]["id"]
                                else:
                                    continue
                                continue

                            if (
                                record_lang != self.source_lang
                                and self._is_html_translation_field(config["model"], field)
                                and raw
                            ):
                                deferred_html_translations[field] = raw
                                continue

                            # Par défaut: garder la valeur telle quelle (chaîne).
                            update_data[field] = raw or False
                    if dry_run:
                        dry_item = {
                            "id": record_id,
                            "file": str(file_path.name),
                            "name": metadata.get("name", ""),
                            "dry_run": True,
                            "operation": "update",
                        }
                        if skipped_metadata_fields:
                            dry_item["skipped_fields"] = skipped_metadata_fields
                        results["pushed"].append(dry_item)
                        continue

                    if update_data:
                        try:
                            self.client.write(
                                config["model"],
                                [record_id],
                                update_data,
                                context=context,
                            )
                        except Exception as exc:
                            fields = ", ".join(sorted(update_data))
                            raise RuntimeError(
                                f"Échec écriture Odoo {config['model']}:{record_id} "
                                f"lang={record_lang} fields=[{fields}]: {exc}"
                            ) from exc

                    for field_name, rendered_value in deferred_html_translations.items():
                        try:
                            self._update_html_translation_from_rendered_content(
                                model=config["model"],
                                record_id=record_id,
                                field_name=field_name,
                                target_lang=record_lang,
                                rendered_target_content=rendered_value,
                            )
                        except Exception as exc:
                            raise RuntimeError(
                                f"Échec traduction rendue Odoo {config['model']}:{record_id} "
                                f"field={field_name} lang={record_lang}: {exc}"
                            ) from exc

                    visibility_values = self._apply_visibility_update(
                        config["model"],
                        record_id,
                        context,
                        publish=publish,
                        index=index,
                    )
                    
                    # Récupérer les infos à jour depuis Odoo
                    remote_records = self.client.search_read(
                        config["model"],
                        domain=[["id", "=", record_id]],
                        fields=["write_date", config["url_field"]],
                        context=context,
                    )
                    
                    # Mettre à jour ou créer l'état (comme Git après un push)
                    new_state = {
                        "id": record_id,
                        "lang": record_lang,
                        "model": config["model"],
                        "name": metadata.get("name", ""),
                        "url": metadata.get("url", remote_records[0].get(config["url_field"], "") if remote_records else ""),
                        "key": metadata.get("key"),
                        "local_path": str(file_path.relative_to(PROJECT_ROOT)),
                        "content_hash": current_hash,
                        "metadata_hash": meta_hash,
                        "last_sync": datetime.now().isoformat(),
                        "write_date": remote_records[0].get("write_date") if remote_records else None,
                    }
                    self.state["records"][record_key] = new_state
                    
                    pushed_item = {
                        "id": record_id,
                        "file": str(file_path.name),
                        "name": metadata.get("name", "")
                    }
                    if visibility_values:
                        pushed_item["visibility"] = visibility_values
                    if skipped_metadata_fields:
                        pushed_item["skipped_fields"] = skipped_metadata_fields
                    results["pushed"].append(pushed_item)
                    
                except Exception as e:
                    results["errors"].append({
                        "file": str(file_path),
                        "error": str(e)
                    })
        
        self._save_state()
        return results

    def delete(
        self,
        category: str = "pages",
        files: Optional[List[str]] = None,
        ids: Optional[List[int]] = None,
        force: bool = False,
        dry_run: bool = True,
        lang: Optional[str] = None,
    ) -> Dict:
        """
        Supprimer des enregistrements côté Odoo sans supprimer les fichiers locaux.

        Par défaut cette méthode vise `pages`, car c'est l'usage demandé et le plus
        sensible. Elle accepte aussi les autres catégories connues si explicitement
        demandées.
        """
        if category not in ODOO_MODELS:
            raise ValueError(f"Catégorie inconnue: {category}")

        config = ODOO_MODELS[category]
        base_dir = SYNC_DIRS[category]
        record_lang = self._normalize_lang(lang)
        context = self._context_for_lang(record_lang)
        targets: List[Tuple[int, Optional[Path], Dict]] = []
        results = {"deleted": [], "skipped": [], "errors": []}

        if files:
            for raw_file in files:
                try:
                    path = resolve_project_path(raw_file)
                except ValueError as e:
                    results["errors"].append({"file": raw_file, "error": str(e)})
                    continue
                if not path.exists():
                    results["errors"].append({"file": raw_file, "error": "File not found"})
                    continue
                try:
                    require_child_path(path, base_dir, f"{category}/")
                except ValueError:
                    results["errors"].append({"file": raw_file, "error": f"File is not in {category}/"})
                    continue
                content = path.read_text(encoding="utf-8")
                metadata, _ = self._parse_metadata_header(content)
                if not metadata.get("id"):
                    results["errors"].append({"file": raw_file, "error": "No ID found in metadata header"})
                    continue
                targets.append((int(metadata["id"]), path, metadata))

        for record_id in ids or []:
            if not any(t[0] == record_id for t in targets):
                targets.append((int(record_id), None, {}))

        if not targets:
            return results

        seen: set[int] = set()
        for record_id, path, metadata in targets:
            if record_id in seen:
                continue
            seen.add(record_id)
            try:
                fields = ["id", config["name_field"], config["url_field"], "write_date"]
                if config.get("key_field"):
                    fields.append(config["key_field"])
                rows = self.client.search_read(
                    config["model"],
                    domain=[["id", "=", record_id]],
                    fields=fields,
                    limit=1,
                    context=context,
                )
                if not rows:
                    results["skipped"].append({
                        "id": record_id,
                        "file": str(path.relative_to(PROJECT_ROOT)) if path else "",
                        "reason": "Record already absent on Odoo",
                    })
                    continue

                state_record = self._get_state_record(category, record_id, record_lang)
                if not force and state_record:
                    remote_write_date = rows[0].get("write_date")
                    if remote_write_date != state_record.get("write_date"):
                        results["skipped"].append({
                            "id": record_id,
                            "file": str(path.relative_to(PROJECT_ROOT)) if path else "",
                            "reason": "Remote has been modified. Use --force to delete anyway.",
                        })
                        continue

                item = {
                    "id": record_id,
                    "name": rows[0].get(config["name_field"]) or metadata.get("name", ""),
                    "url": rows[0].get(config["url_field"]) or metadata.get("url", ""),
                    "file": str(path.relative_to(PROJECT_ROOT)) if path else "",
                    "dry_run": dry_run,
                }

                if dry_run:
                    results["deleted"].append(item)
                    continue

                ok = self.client.unlink(config["model"], [record_id], context=context)
                if not ok:
                    results["errors"].append({"id": record_id, "error": "Odoo unlink returned false"})
                    continue

                for key in list(self.state.get("records", {}).keys()):
                    parts = key.split(":")
                    if len(parts) == 3 and parts[0] == category and parts[2] == str(record_id):
                        self.state["records"].pop(key, None)
                    elif len(parts) == 2 and parts[0] == category and parts[1] == str(record_id):
                        self.state["records"].pop(key, None)
                results["deleted"].append(item)
            except Exception as e:
                results["errors"].append({"id": record_id, "error": str(e)})

        if not dry_run:
            self._save_state()
        return results
    
    def status(self, category: str = None, lang: Optional[str] = None) -> Dict:
        """
        Afficher le statut de synchronisation
        
        Returns:
            État de chaque fichier
        """
        status = {"synced": [], "modified": [], "remote_modified": [], "untracked": []}
        lang_filter = self._normalize_lang(lang) if lang else None
        
        categories = [category] if category else list(ODOO_MODELS.keys())
        
        for cat in categories:
            config = ODOO_MODELS[cat]
            base_dir = SYNC_DIRS[cat]
            
            if not base_dir.exists():
                continue
            
            # Vérifier les fichiers locaux (inclut les sous-dossiers)
            for file_path in base_dir.glob(f"**/*{config['file_extension']}"):
                content = file_path.read_text(encoding="utf-8")
                metadata, clean_content = self._parse_metadata_header(content)
                
                if "id" not in metadata:
                    status["untracked"].append({
                        "file": str(file_path.relative_to(PROJECT_ROOT)),
                        "category": cat
                    })
                    continue
                
                record_id = int(metadata["id"])
                record_lang = self._normalize_lang(metadata.get("lang") or self.default_lang)
                if lang_filter and record_lang != lang_filter:
                    continue
                state_record = self._get_state_record(cat, record_id, record_lang)
                
                if not state_record:
                    status["untracked"].append({
                        "file": str(file_path.relative_to(PROJECT_ROOT)),
                        "category": cat,
                        "id": record_id,
                        "lang": record_lang,
                    })
                    continue
                
                current_hash = self._compute_hash(clean_content)
                
                if current_hash != state_record.get("content_hash"):
                    status["modified"].append({
                        "file": str(file_path.relative_to(PROJECT_ROOT)),
                        "id": record_id,
                        "name": metadata.get("name", ""),
                        "category": cat,
                        "lang": record_lang,
                    })
                else:
                    status["synced"].append({
                        "file": str(file_path.relative_to(PROJECT_ROOT)),
                        "id": record_id,
                        "name": metadata.get("name", ""),
                        "category": cat,
                        "lang": record_lang,
                    })
        
        return status
    
    def diff(self, file_path: str, lang: Optional[str] = None) -> Dict:
        """
        Afficher les différences entre local et remote
        """
        try:
            path = resolve_project_path(file_path)
        except ValueError as e:
            return {"error": str(e)}
        if not path.exists():
            return {"error": f"File not found: {file_path}"}
        
        content = path.read_text(encoding="utf-8")
        metadata, clean_content = self._parse_metadata_header(content)
        
        if "id" not in metadata:
            return {"error": "No ID in metadata"}
        
        # Trouver la catégorie en parcourant les SYNC_DIRS
        category = None
        for cat_name, cat_dir in SYNC_DIRS.items():
            try:
                require_child_path(path, cat_dir, f"{cat_name}/")
                category = cat_name
                break
            except ValueError:
                continue
        
        if category is None:
            # Fallback sur l'ancienne méthode
            category = path.parent.name
        
        if category not in ODOO_MODELS:
            return {"error": f"Unknown category: {category}"}
        
        config = ODOO_MODELS[category]
        record_id = int(metadata["id"])
        record_lang = self._normalize_lang(metadata.get("lang") or lang)
        context = self._context_for_lang(record_lang)
        
        # Récupérer le contenu remote
        records = self.client.search_read(
            config["model"],
            domain=[["id", "=", record_id]],
            fields=[config["content_field"]],
            context=context,
        )
        
        if not records:
            return {"error": f"Record {record_id} not found on Odoo"}
        
        remote_content = records[0].get(config["content_field"]) or ""
        
        return {
            "local": clean_content,
            "remote": remote_content,
            "local_hash": self._compute_hash(clean_content),
            "remote_hash": self._compute_hash(remote_content),
            "is_different": self._compute_hash(clean_content) != self._compute_hash(remote_content),
            "lang": record_lang,
        }
