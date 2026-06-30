"""
Synchronisation des images Odoo (`ir.attachment`) vers des fichiers locaux.

Chaque image est stockée avec un sidecar JSON éditable:

    images/attachments/123-nom-image.webp
    images/attachments/123-nom-image.webp.odoo.json

Le champ `description` du sidecar correspond à la description/métadonnée de
l'image dans Odoo. Le champ `name` pilote le nom/titre de la pièce jointe.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import IMAGE_SYNC_DIR, PROJECT_ROOT
from .odoo_client import get_client


ATTACHMENT_FIELDS = [
    "id",
    "name",
    "description",
    "mimetype",
    "public",
    "res_model",
    "res_id",
    "url",
    "write_date",
    "file_size",
]

MIMETYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
}


def _compute_bytes_hash(data: bytes) -> str:
    return hashlib.md5(data or b"").hexdigest()


def _slugify(text: str) -> str:
    text = (text or "image").lower()
    text = re.sub(r"[àáâãäå]", "a", text)
    text = re.sub(r"[èéêë]", "e", text)
    text = re.sub(r"[ìíîï]", "i", text)
    text = re.sub(r"[òóôõö]", "o", text)
    text = re.sub(r"[ùúûü]", "u", text)
    text = re.sub(r"[ç]", "c", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80] or "image"


def _extension(name: str, mimetype: str) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".tif", ".tiff"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return MIMETYPE_EXTENSIONS.get((mimetype or "").lower(), mimetypes.guess_extension(mimetype or "") or ".bin")


def _sidecar_for(image_path: Path) -> Path:
    return image_path.with_name(f"{image_path.name}.odoo.json")


def _image_for_sidecar(sidecar_path: Path, meta: Dict[str, Any]) -> Path:
    raw = meta.get("file")
    if raw:
        path = PROJECT_ROOT / raw
        if path.exists():
            return path
    name = sidecar_path.name.removesuffix(".odoo.json")
    return sidecar_path.with_name(name)


class ImageSyncService:
    def __init__(self) -> None:
        self.client = get_client()

    def _local_image_path(self, row: Dict[str, Any]) -> Path:
        name = str(row.get("name") or f"image-{row['id']}")
        ext = _extension(name, str(row.get("mimetype") or ""))
        slug = _slugify(Path(name).stem)
        return IMAGE_SYNC_DIR / f"{row['id']}-{slug}{ext}"

    def pull(
        self,
        ids: Optional[List[int]] = None,
        res_model: Optional[str] = None,
        res_id: Optional[int] = None,
        limit: Optional[int] = 100,
        force: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        results: Dict[str, List[Dict[str, Any]]] = {"pulled": [], "skipped": [], "errors": []}
        IMAGE_SYNC_DIR.mkdir(parents=True, exist_ok=True)

        domain: List[Any] = [["mimetype", "ilike", "image/"]]
        if ids:
            domain.append(["id", "in", ids])
        if res_model:
            domain.append(["res_model", "=", res_model])
        if res_id is not None:
            domain.append(["res_id", "=", int(res_id)])

        rows = self.client.search_read(
            "ir.attachment",
            domain=domain,
            fields=ATTACHMENT_FIELDS,
            limit=limit,
            order="id asc",
        )

        for row in rows:
            try:
                full = self.client.read("ir.attachment", [int(row["id"])], ATTACHMENT_FIELDS + ["datas"])
                if not full:
                    results["errors"].append({"id": row["id"], "error": "Attachment not found"})
                    continue
                full_row = full[0]
                raw_datas = full_row.get("datas")
                if not raw_datas:
                    results["skipped"].append({"id": row["id"], "reason": "No binary datas"})
                    continue
                data = base64.b64decode(raw_datas)
                image_path = self._local_image_path(full_row)
                sidecar_path = _sidecar_for(image_path)

                if image_path.exists() and sidecar_path.exists() and not force:
                    old_meta = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    old_hash = old_meta.get("content_hash")
                    current_hash = _compute_bytes_hash(image_path.read_bytes())
                    if old_hash and current_hash != old_hash:
                        results["skipped"].append({
                            "id": row["id"],
                            "path": str(image_path.relative_to(PROJECT_ROOT)),
                            "reason": "Local image changed. Use --force to overwrite.",
                        })
                        continue

                image_path.write_bytes(data)
                meta = {field: full_row.get(field) for field in ATTACHMENT_FIELDS}
                meta["file"] = str(image_path.relative_to(PROJECT_ROOT))
                meta["content_hash"] = _compute_bytes_hash(data)
                sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                results["pulled"].append({
                    "id": row["id"],
                    "path": str(image_path.relative_to(PROJECT_ROOT)),
                    "sidecar": str(sidecar_path.relative_to(PROJECT_ROOT)),
                })
            except Exception as exc:
                results["errors"].append({"id": row.get("id"), "error": str(exc)})

        return results

    def _iter_sidecars(self, files: Optional[List[str]]) -> List[Path]:
        if files:
            out: List[Path] = []
            for raw in files:
                path = Path(raw)
                if not path.is_absolute():
                    path = PROJECT_ROOT / path
                if path.name.endswith(".odoo.json"):
                    out.append(path)
                else:
                    out.append(_sidecar_for(path))
            return out
        return sorted(IMAGE_SYNC_DIR.glob("**/*.odoo.json"))

    def push(
        self,
        files: Optional[List[str]] = None,
        create_missing: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        results: Dict[str, List[Dict[str, Any]]] = {"pushed": [], "skipped": [], "errors": []}

        for sidecar_path in self._iter_sidecars(files):
            try:
                if not sidecar_path.exists():
                    results["errors"].append({"file": str(sidecar_path), "error": "Sidecar not found"})
                    continue

                meta = json.loads(sidecar_path.read_text(encoding="utf-8"))
                image_path = _image_for_sidecar(sidecar_path, meta)
                if not image_path.exists():
                    results["errors"].append({"file": str(sidecar_path), "error": "Image file not found"})
                    continue

                data = image_path.read_bytes()
                mimetype = meta.get("mimetype") or mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
                values: Dict[str, Any] = {
                    "name": meta.get("name") or image_path.name,
                    "description": meta.get("description") or False,
                    "public": bool(meta.get("public")),
                }
                if meta.get("res_model"):
                    values["res_model"] = meta["res_model"]
                if meta.get("res_id"):
                    values["res_id"] = int(meta["res_id"])

                current_hash = _compute_bytes_hash(data)
                if current_hash != meta.get("content_hash"):
                    values["datas"] = base64.b64encode(data).decode("ascii")

                attachment_id = meta.get("id")
                operation = "update"
                if not attachment_id:
                    if not create_missing:
                        results["errors"].append({
                            "file": str(sidecar_path.relative_to(PROJECT_ROOT)),
                            "error": "No attachment id. Use --create to create it in Odoo.",
                        })
                        continue
                    values["type"] = "binary"
                    values["mimetype"] = mimetype
                    values.setdefault("datas", base64.b64encode(data).decode("ascii"))
                    operation = "create"

                if dry_run:
                    results["pushed"].append({
                        "id": attachment_id,
                        "file": str(image_path.relative_to(PROJECT_ROOT)),
                        "operation": operation,
                        "dry_run": True,
                        "fields": sorted(values.keys()),
                    })
                    continue

                if operation == "create":
                    attachment_id = self.client.create("ir.attachment", values)
                else:
                    self.client.write("ir.attachment", [int(attachment_id)], values)

                updated = self.client.read("ir.attachment", [int(attachment_id)], ATTACHMENT_FIELDS)
                if updated:
                    meta.update({field: updated[0].get(field) for field in ATTACHMENT_FIELDS})
                meta["id"] = int(attachment_id)
                meta["file"] = str(image_path.relative_to(PROJECT_ROOT))
                meta["mimetype"] = mimetype
                meta["content_hash"] = current_hash
                sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

                results["pushed"].append({
                    "id": int(attachment_id),
                    "file": str(image_path.relative_to(PROJECT_ROOT)),
                    "operation": operation,
                })
            except Exception as exc:
                results["errors"].append({"file": str(sidecar_path), "error": str(exc)})

        return results
