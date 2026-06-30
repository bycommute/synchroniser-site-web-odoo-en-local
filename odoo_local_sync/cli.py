#!/usr/bin/env python3
"""
CLI pour la synchronisation Odoo ↔ Fichiers locaux
Usage similaire à Git: odoo-sync pull, odoo-sync push, odoo-sync status
"""
import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Set, Tuple

# Ajouter le répertoire courant au path
sys.path.insert(0, str(Path(__file__).parent))

from .sync_manager import SyncManager
from .odoo_client import get_client, missing_config_keys
from .config import ODOO_MODELS, PROJECT_ROOT, SYNC_DIRS, resolve_project_path
from .redirects_sync import RedirectSyncService, RedirectSyncError
from .image_sync import ImageSyncService
from .odoo_content_validator import collect_paths, validate_paths


LANG_SUFFIX_RE = re.compile(r"\.([a-z]{2}_[A-Z]{2})(?=\.[^.]+$)")
ID_PREFIX_RE = re.compile(r"^(\d+)-")


class Colors:
    """Couleurs ANSI pour le terminal"""
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'


def print_header(text: str):
    """Afficher un header stylisé"""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * 50}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}  {text}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * 50}{Colors.END}\n")


def print_success(text: str):
    print(f"{Colors.GREEN}✓ {text}{Colors.END}")


def print_warning(text: str):
    print(f"{Colors.YELLOW}⚠ {text}{Colors.END}")


def print_error(text: str):
    print(f"{Colors.RED}✗ {text}{Colors.END}")


def print_info(text: str):
    print(f"{Colors.BLUE}ℹ {text}{Colors.END}")


ENV_TEMPLATE = """# Odoo connection
ODOO_URL=https://your-company.odoo.com
ODOO_DB=your-database-name
ODOO_USERNAME=you@example.com
ODOO_API_KEY=your-odoo-api-key

# Website/language defaults
ODOO_WEBSITE_ID=1
ODOO_LANG=fr_FR
ODOO_SOURCE_LANG=fr_FR
ODOO_LANGS=fr_FR,en_US

# Safety defaults
SYNC_BLOCK_SOURCE_WRITES=0
SYNC_HTML_TRANSLATION_DIRECT_WRITE_FALLBACK=0
SYNC_ALLOW_DESCRIPTION_SALE_WRITES=0
SYNC_ALLOW_PRODUCT_STRUCTURAL_WRITES=0
SYNC_ALLOW_METADATA_FALSE_CLEARS=0
SYNC_ENFORCE_FILE_LANG_MATCH=1
"""


def cmd_init(args):
    """Create a local .env template and synchronization folders."""
    print_header("INIT - Configuration locale")
    env_path = PROJECT_ROOT / ".env"
    example_path = PROJECT_ROOT / ".env.example"

    for directory in SYNC_DIRS.values():
        directory.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "images" / "attachments").mkdir(parents=True, exist_ok=True)

    if not example_path.exists() or args.force:
        example_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print_success(f"Exemple créé: {example_path.relative_to(PROJECT_ROOT)}")

    if env_path.exists() and not args.force:
        print_info(".env existe déjà, il n'a pas été écrasé")
    else:
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print_success(".env créé")

    missing = missing_config_keys()
    if missing:
        print_warning("Avant toute commande Odoo, compléter .env avec:")
        for key in missing:
            print(f"  - {key}")
    print_info("Réflexe de travail: `odoo-sync pull <category> --ids <id> --force` avant toute modification.")


def _git_targets_for_push(files, category):
    """Limiter le commit de sauvegarde aux fichiers réellement concernés par le push."""
    if files:
        return [str(_validate_push_file_path(f)) for f in files]
    if category:
        return [str(SYNC_DIRS[category])]
    return [str(SYNC_DIRS[cat]) for cat in ODOO_MODELS]


def _validate_push_file_path(file_path: str) -> Path:
    """Require a pushed file to live inside one configured sync directory."""
    resolved = resolve_project_path(file_path)
    for directory in SYNC_DIRS.values():
        try:
            resolved.relative_to(directory.resolve())
            return resolved
        except ValueError:
            continue
    raise ValueError(f"Push file is outside synchronized folders: {file_path}")


def _validate_push_files(files: List[str]) -> None:
    for file_path in files:
        _validate_push_file_path(file_path)


def _display_path(candidate: Path, prefer_relative: bool) -> str:
    if prefer_relative:
        try:
            return str(candidate.resolve().relative_to(PROJECT_ROOT.resolve()))
        except ValueError:
            pass
    return str(candidate)


def _language_siblings_for_file(file_path: str) -> List[str]:
    """Retourner le fichier ciblé et ses variantes de langue locales pour le même ID Odoo."""
    original = Path(file_path)
    prefer_relative = not original.is_absolute()
    absolute = original if original.is_absolute() else PROJECT_ROOT / original

    if not absolute.exists():
        return [file_path]

    id_match = ID_PREFIX_RE.match(absolute.name)
    if not id_match:
        return [file_path]

    record_id = id_match.group(1)
    candidates = [
        candidate
        for candidate in absolute.parent.glob(f"{record_id}-*{absolute.suffix}")
        if candidate.is_file() and ID_PREFIX_RE.match(candidate.name)
    ]

    def sort_key(candidate: Path) -> Tuple[int, str, str]:
        lang_match = LANG_SUFFIX_RE.search(candidate.name)
        if not lang_match:
            return (0, "", candidate.name)
        return (1, lang_match.group(1), candidate.name)

    return [_display_path(candidate, prefer_relative) for candidate in sorted(candidates, key=sort_key)]


def _expand_files_all_langs(files: List[str]) -> List[str]:
    """Étendre une liste de fichiers ciblés à leurs frères traduits, sans doublons."""
    expanded: List[str] = []
    seen: Set[str] = set()
    for file_path in files:
        for candidate in _language_siblings_for_file(file_path):
            key = str((PROJECT_ROOT / candidate).resolve()) if not Path(candidate).is_absolute() else str(Path(candidate).resolve())
            if key in seen:
                continue
            seen.add(key)
            expanded.append(candidate)
    return expanded


def cmd_pull(args):
    """Commande pull: récupérer depuis Odoo"""
    print_header("PULL - Récupération depuis Odoo")
    
    manager = SyncManager()
    
    category = args.category if args.category != "all" else None
    ids = [int(i) for i in args.ids.split(",")] if args.ids else None
    prune = bool(getattr(args, "prune", False))
    
    print_info(f"Catégorie: {args.category}")
    langs = manager.get_active_langs() if getattr(args, "all_langs", False) else [args.lang]
    print_info(f"Langue(s): {', '.join(langs)}")
    if ids:
        print_info(f"IDs spécifiques: {ids}")
    if prune:
        print_warning("Mode --prune activé: suppression locale des fichiers obsolètes (doublons / absents sur Odoo), uniquement si non modifiés localement")
    if args.content_only:
        print_warning("Mode --content-only activé: seules les modifications de contenu seront prises en compte")
    if args.force:
        print_warning("Mode --force activé: les modifications locales seront écrasées")
    
    print()
    
    try:
        # Sécurité: --prune nécessite un pull COMPLET (sinon on ne sait pas ce qui existe encore)
        if prune and ids:
            raise ValueError("--prune ne peut pas être utilisé avec --ids (pull partiel). Lancez un pull complet.")
        if prune and category not in (None, "blog-posts", "products"):
            raise ValueError("--prune est supporté uniquement pour 'blog-posts' et 'products' (ou 'all').")

        results = {"pulled": [], "skipped": [], "errors": []}
        for lang in langs:
            partial = manager.pull(
                category=category,
                force=args.force,
                ids=ids,
                prune=prune and len(langs) == 1,
                content_only=args.content_only,
                lang=lang,
            )
            for key in ("pulled", "skipped", "errors"):
                results[key].extend(partial.get(key, []))
            if partial.get("pruned"):
                results.setdefault("pruned", {"deleted": [], "skipped": [], "errors": []})
                for key in ("deleted", "skipped", "errors"):
                    results["pruned"][key].extend(partial["pruned"].get(key, []))
        
        if results["pulled"]:
            print(f"\n{Colors.GREEN}Fichiers récupérés ({len(results['pulled'])}):{Colors.END}")
            for item in results["pulled"]:
                print(f"  {Colors.GREEN}+{Colors.END} {item['path']}")
                print(f"    {Colors.CYAN}[{item['id']}]{Colors.END} {item['name']}")
        
        if results["skipped"]:
            print(f"\n{Colors.YELLOW}Fichiers ignorés ({len(results['skipped'])}):{Colors.END}")
            for item in results["skipped"]:
                print(f"  {Colors.YELLOW}~{Colors.END} [{item['id']}] {item.get('name', '')}")
                print(f"    {Colors.YELLOW}Raison: {item['reason']}{Colors.END}")
        
        if results["errors"]:
            print(f"\n{Colors.RED}Erreurs ({len(results['errors'])}):{Colors.END}")
            for item in results["errors"]:
                print(f"  {Colors.RED}✗{Colors.END} {item}")

        if results.get("pruned"):
            pruned = results["pruned"]
            deleted = pruned.get("deleted", [])
            skipped = pruned.get("skipped", [])
            if deleted:
                print(f"\n{Colors.GREEN}Fichiers supprimés localement (prune) ({len(deleted)}):{Colors.END}")
                for item in deleted:
                    print(f"  {Colors.GREEN}-{Colors.END} {item['path']}")
                    print(f"    {Colors.CYAN}[{item['id']}]{Colors.END} {item.get('name', '')}")
            if skipped:
                print(f"\n{Colors.YELLOW}Fichiers conservés (prune) ({len(skipped)}):{Colors.END}")
                for item in skipped:
                    print(f"  {Colors.YELLOW}~{Colors.END} {item['path']}")
                    print(f"    {Colors.YELLOW}Raison: {item['reason']}{Colors.END}")
        
        print()
        print_success(f"Pull terminé: {len(results['pulled'])} récupérés, {len(results['skipped'])} ignorés, {len(results['errors'])} erreurs")
        
    except Exception as e:
        print_error(f"Erreur lors du pull: {e}")
        sys.exit(1)


def cmd_push(args):
    """Commande push: pousser vers Odoo"""
    print_header("PUSH - Envoi vers Odoo")
    
    manager = SyncManager()
    
    # Déterminer le mode : fichiers spécifiques ou catégorie
    if args.files:
        # Mode fichiers spécifiques
        files = _expand_files_all_langs(args.files) if args.all_langs else args.files
        try:
            _validate_push_files(files)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        category = None
        print_info(f"Mode: Fichiers spécifiques ({len(files)})")
        if args.all_langs and len(files) != len(args.files):
            print_info(f"--all-langs: {len(args.files)} fichier(s) cible(s) étendu(s) à {len(files)} fichier(s)")
        for f in files:
            print(f"  • {f}")
    elif args.category:
        # Mode catégorie
        category = args.category if args.category != "all" else None
        files = None
        print_info(f"Mode: Catégorie '{args.category}'")
    else:
        # Par défaut, demander confirmation
        print_warning("Aucun fichier ni catégorie spécifié.")
        print_info("Usage recommandé:")
        print("  • Push fichier unique: ./odoo-sync push blog-posts/bricolage/564-construire-un-abri-velo-en-bois.html")
        print("  • Push catégorie:      ./odoo-sync push --category blog-posts")
        print("  • Push tout:           ./odoo-sync push --category all")
        sys.exit(1)
    
    if args.force:
        print_warning("Mode --force activé: les modifications remote seront écrasées")
    print_info(f"Langue: {args.lang}")
    if args.create:
        print_warning("Mode --create activé: les nouveaux fichiers pages/products/blog-posts sans ID peuvent créer des objets Odoo non publiés par défaut")
    if args.dry_run:
        print_warning("Mode --dry-run activé: aucune écriture Odoo")
    if args.content_only:
        print_warning("Mode --content-only activé: seul le champ de contenu sera poussé vers Odoo")
    if args.skip_validate:
        print_warning("Préflight local désactivé (--skip-validate)")
    if args.strict_validate:
        print_warning("Préflight strict: les warnings bloquent le push")
    if args.publish and args.unpublish:
        print_error("--publish et --unpublish sont incompatibles")
        sys.exit(1)
    if args.index and args.no_index:
        print_error("--index et --no-index sont incompatibles")
        sys.exit(1)
    publish = True if args.publish else False if args.unpublish else None
    index = True if args.index else False if args.no_index else None
    if publish is not None:
        print_warning(f"Publication explicite demandée: website_published={publish}")
    if index is not None:
        print_warning(f"Indexation explicite demandée: website_indexed={index}")
    
    # Créer un commit Git automatique avant le push (sécurité)
    if not args.no_commit and not args.dry_run:
        print()
        print_info("Création d'un commit Git de sauvegarde...")
        try:
            import subprocess
            from datetime import datetime
            git_targets = _git_targets_for_push(files, category)
            
            # Vérifier uniquement les fichiers concernés par le push.
            # `git add -A` sur tout le dépôt peut embarquer des travaux sans rapport.
            status_result = subprocess.run(
                ["git", "status", "--porcelain", "--", *git_targets],
                capture_output=True,
                text=True,
                check=True
            )
            
            if status_result.stdout.strip():
                subprocess.run(["git", "add", "--", *git_targets], check=True)
                commit_msg = f"Auto-commit avant push Odoo - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                subprocess.run(["git", "commit", "-m", commit_msg], check=True)
                print_success("✓ Commit Git créé (sauvegarde de sécurité)")
            else:
                print_info("Aucun changement à commiter dans le périmètre du push")
        except subprocess.CalledProcessError as e:
            print_warning(f"Impossible de créer le commit Git: {e}")
            print_warning("Continuez quand même ? (y/n)")
            if input().lower() != 'y':
                print_error("Push annulé")
                sys.exit(1)
        except Exception as e:
            print_warning(f"Erreur Git: {e}")
    
    print()
    
    try:
        results = manager.push(
            category=category,
            files=files,
            force=args.force,
            content_only=args.content_only,
            lang=args.lang,
            create_missing=args.create,
            dry_run=args.dry_run,
            publish=publish,
            index=index,
            validate=not args.skip_validate,
            strict_validate=args.strict_validate,
        )
        
        if results["pushed"]:
            print(f"\n{Colors.GREEN}Fichiers poussés ({len(results['pushed'])}):{Colors.END}")
            for item in results["pushed"]:
                print(f"  {Colors.GREEN}↑{Colors.END} {item['file']}")
                print(f"    {Colors.CYAN}[{item['id']}]{Colors.END} {item['name']}")
        
        if results["skipped"]:
            print(f"\n{Colors.YELLOW}Fichiers ignorés ({len(results['skipped'])}):{Colors.END}")
            for item in results["skipped"]:
                print(f"  {Colors.YELLOW}~{Colors.END} {item['file']}")
                print(f"    {Colors.YELLOW}Raison: {item['reason']}{Colors.END}")

        if results.get("warnings"):
            print(f"\n{Colors.YELLOW}Warnings préflight ({len(results['warnings'])}):{Colors.END}")
            for item in results["warnings"][:20]:
                print(f"  {Colors.YELLOW}~{Colors.END} {item['file']}")
                print(f"    {Colors.YELLOW}{item['code']}: {item['message']}{Colors.END}")
            if len(results["warnings"]) > 20:
                print_warning(f"{len(results['warnings']) - 20} warning(s) supplémentaire(s) masqué(s)")
        
        if results["errors"]:
            print(f"\n{Colors.RED}Erreurs ({len(results['errors'])}):{Colors.END}")
            for item in results["errors"]:
                print(f"  {Colors.RED}✗{Colors.END} {item}")
        
        print()
        print_success(f"Push terminé: {len(results['pushed'])} poussés, {len(results['skipped'])} ignorés, {len(results['errors'])} erreurs")
        
    except Exception as e:
        print_error(f"Erreur lors du push: {e}")
        sys.exit(1)


def cmd_validate(args):
    """Commande validate: préflight local avant push Odoo."""
    print_header("VALIDATE - Préflight local Odoo")

    category = None if args.category == "all" else args.category
    paths = collect_paths(files=args.files or None, category=category)
    if not paths:
        print_warning("Aucun fichier à valider")
        return

    issues = validate_paths(paths, category=category)
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]

    print_info(f"Fichiers analysés: {len(paths)}")
    if not issues:
        print_success("Aucun problème détecté")
        return

    shown = issues[: args.max_issues]
    for issue in shown:
        color = Colors.RED if issue.severity == "error" else Colors.YELLOW
        marker = "✗" if issue.severity == "error" else "~"
        print(f"{color}{marker} {issue.path}{Colors.END}")
        print(f"  {issue.severity.upper()} {issue.code}: {issue.message}")
    if len(issues) > len(shown):
        print_warning(f"{len(issues) - len(shown)} problème(s) masqué(s). Augmentez --max-issues pour tout afficher.")

    print()
    if errors:
        print_error(f"Validation échouée: {len(errors)} erreur(s), {len(warnings)} warning(s)")
        sys.exit(1)
    if args.strict and warnings:
        print_error(f"Validation stricte échouée: {len(warnings)} warning(s)")
        sys.exit(1)
    print_warning(f"Validation terminée avec {len(warnings)} warning(s)")


def cmd_status(args):
    """Commande status: afficher l'état de synchronisation"""
    print_header("STATUS - État de synchronisation")
    
    manager = SyncManager()
    category = args.category if args.category != "all" else None
    
    try:
        status = manager.status(category=category, lang=args.lang)
        
        total = sum(len(v) for v in status.values())
        
        if status["modified"]:
            print(f"\n{Colors.YELLOW}Modifiés localement ({len(status['modified'])}):{Colors.END}")
            for item in status["modified"]:
                print(f"  {Colors.YELLOW}M{Colors.END} {item['file']}")
        
        if status["untracked"]:
            print(f"\n{Colors.RED}Non suivis ({len(status['untracked'])}):{Colors.END}")
            for item in status["untracked"]:
                print(f"  {Colors.RED}?{Colors.END} {item['file']}")
        
        if status["synced"]:
            if args.verbose:
                print(f"\n{Colors.GREEN}Synchronisés ({len(status['synced'])}):{Colors.END}")
                for item in status["synced"]:
                    print(f"  {Colors.GREEN}✓{Colors.END} {item['file']}")
            else:
                print(f"\n{Colors.GREEN}✓ {len(status['synced'])} fichiers synchronisés{Colors.END}")
        
        if total == 0:
            print_info("Aucun fichier trouvé. Utilisez 'pull' pour récupérer les données d'Odoo.")
        
    except Exception as e:
        print_error(f"Erreur lors du status: {e}")
        sys.exit(1)


def cmd_diff(args):
    """Commande diff: afficher les différences"""
    print_header(f"DIFF - {args.file}")
    
    manager = SyncManager()
    
    try:
        result = manager.diff(args.file, lang=args.lang)
        
        if "error" in result:
            print_error(result["error"])
            sys.exit(1)
        
        if not result["is_different"]:
            print_success("Les fichiers sont identiques")
            return
        
        print_warning("Les fichiers sont différents")
        print(f"\nHash local:  {result['local_hash']}")
        print(f"Hash remote: {result['remote_hash']}")
        
        if args.show:
            print(f"\n{Colors.CYAN}=== CONTENU LOCAL ==={Colors.END}")
            print(result["local"][:1000] + "..." if len(result["local"]) > 1000 else result["local"])
            print(f"\n{Colors.CYAN}=== CONTENU REMOTE ==={Colors.END}")
            print(result["remote"][:1000] + "..." if len(result["remote"]) > 1000 else result["remote"])
        
    except Exception as e:
        print_error(f"Erreur lors du diff: {e}")
        sys.exit(1)


def cmd_test(args):
    """Commande test: tester la connexion Odoo"""
    print_header("TEST - Connexion Odoo")
    
    client = get_client()
    result = client.test_connection()
    
    if result["success"]:
        print_success(result["message"])
        print(f"  Version: {result['version'].get('server_version', 'N/A')}")
        print(f"  UID: {result['uid']}")
    else:
        print_error(result["message"])
        sys.exit(1)


def cmd_list(args):
    """Commande list: lister les éléments disponibles sur Odoo"""
    print_header(f"LIST - {args.category}")
    
    if args.category not in ODOO_MODELS:
        print_error(f"Catégorie inconnue: {args.category}")
        print_info(f"Catégories disponibles: {', '.join(ODOO_MODELS.keys())}")
        sys.exit(1)
    
    config = ODOO_MODELS[args.category]
    client = get_client()
    context = {"lang": args.lang}
    
    try:
        fields = ["id", config["name_field"], config["url_field"]]
        domain = config.get("domain", [])
        
        records = client.search_read(
            config["model"],
            domain=domain,
            fields=fields,
            limit=args.limit,
            context=context,
        )
        
        print(f"Trouvé {len(records)} enregistrements:\n")
        
        for record in records:
            print(f"  {Colors.CYAN}[{record['id']:4d}]{Colors.END} {record.get(config['name_field'], 'N/A')}")
            if args.verbose:
                print(f"         URL: {record.get(config['url_field'], 'N/A')}")
        
    except Exception as e:
        print_error(f"Erreur: {e}")
        sys.exit(1)


def cmd_langs(args):
    """Lister les langues actives côté Odoo."""
    print_header("LANGS - Langues actives Odoo")
    manager = SyncManager()
    try:
        langs = manager.get_active_langs()
        for lang in langs:
            marker = " (source)" if lang == manager.source_lang else ""
            print(f"  {Colors.CYAN}{lang}{Colors.END}{marker}")
        print_success(f"{len(langs)} langue(s) active(s)")
    except Exception as e:
        print_error(f"Erreur: {e}")
        sys.exit(1)


def cmd_delete(args):
    """Supprimer des enregistrements Odoo, par défaut en dry-run."""
    print_header("DELETE - Suppression côté Odoo")
    manager = SyncManager()
    ids = [int(i) for i in args.ids.split(",")] if args.ids else None
    dry_run = not args.apply

    print_info(f"Catégorie: {args.category}")
    if args.files:
        print_info(f"Fichiers: {len(args.files)}")
    if ids:
        print_info(f"IDs: {ids}")
    if dry_run:
        print_warning("Dry-run: aucune suppression. Relancez avec --apply pour supprimer réellement.")
    if args.force:
        print_warning("Mode --force: supprime même si Odoo a changé depuis le dernier sync.")

    try:
        result = manager.delete(
            category=args.category,
            files=args.files,
            ids=ids,
            force=args.force,
            dry_run=dry_run,
            lang=args.lang,
        )
        if result["deleted"]:
            label = "À supprimer" if dry_run else "Supprimés"
            print(f"\n{Colors.GREEN}{label} ({len(result['deleted'])}):{Colors.END}")
            for item in result["deleted"]:
                print(f"  {Colors.GREEN}-{Colors.END} [{item['id']}] {item.get('name', '')} {item.get('url', '')}")
                if item.get("file"):
                    print(f"    {item['file']}")
        if result["skipped"]:
            print(f"\n{Colors.YELLOW}Ignorés ({len(result['skipped'])}):{Colors.END}")
            for item in result["skipped"]:
                print(f"  {Colors.YELLOW}~{Colors.END} [{item.get('id')}] {item.get('reason')}")
        if result["errors"]:
            print(f"\n{Colors.RED}Erreurs ({len(result['errors'])}):{Colors.END}")
            for item in result["errors"]:
                print(f"  {Colors.RED}✗{Colors.END} {item}")
        print_success("Delete terminé")
    except Exception as e:
        print_error(f"Erreur lors du delete: {e}")
        sys.exit(1)


def _parse_ids(raw: str | None):
    if not raw:
        return None
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def cmd_images_pull(args):
    """Exporter des images Odoo en local avec leurs métadonnées."""
    print_header("IMAGES PULL - Export ir.attachment")
    service = ImageSyncService()
    try:
        result = service.pull(
            ids=_parse_ids(args.ids),
            res_model=args.res_model,
            res_id=args.res_id,
            limit=None if args.all else args.limit,
            force=args.force,
        )
        if result["pulled"]:
            print(f"\n{Colors.GREEN}Images récupérées ({len(result['pulled'])}):{Colors.END}")
            for item in result["pulled"]:
                print(f"  {Colors.GREEN}+{Colors.END} [{item['id']}] {item['path']}")
                print(f"    meta: {item['sidecar']}")
        if result["skipped"]:
            print(f"\n{Colors.YELLOW}Ignorées ({len(result['skipped'])}):{Colors.END}")
            for item in result["skipped"]:
                print(f"  {Colors.YELLOW}~{Colors.END} {item}")
        if result["errors"]:
            print(f"\n{Colors.RED}Erreurs ({len(result['errors'])}):{Colors.END}")
            for item in result["errors"]:
                print(f"  {Colors.RED}✗{Colors.END} {item}")
        print_success("Images pull terminé")
    except Exception as e:
        print_error(f"Erreur images-pull: {e}")
        sys.exit(1)


def cmd_images_push(args):
    """Pousser les métadonnées/binaries image vers ir.attachment."""
    print_header("IMAGES PUSH - Import ir.attachment")
    service = ImageSyncService()
    try:
        result = service.push(
            files=args.files or None,
            create_missing=args.create,
            dry_run=args.dry_run,
        )
        if result["pushed"]:
            print(f"\n{Colors.GREEN}Images poussées ({len(result['pushed'])}):{Colors.END}")
            for item in result["pushed"]:
                op = item.get("operation", "update")
                prefix = "[DRY-RUN] " if item.get("dry_run") else ""
                print(f"  {Colors.GREEN}↑{Colors.END} {prefix}{op} [{item.get('id')}] {item.get('file')}")
        if result["skipped"]:
            print(f"\n{Colors.YELLOW}Ignorées ({len(result['skipped'])}):{Colors.END}")
            for item in result["skipped"]:
                print(f"  {Colors.YELLOW}~{Colors.END} {item}")
        if result["errors"]:
            print(f"\n{Colors.RED}Erreurs ({len(result['errors'])}):{Colors.END}")
            for item in result["errors"]:
                print(f"  {Colors.RED}✗{Colors.END} {item}")
        print_success("Images push terminé")
    except Exception as e:
        print_error(f"Erreur images-push: {e}")
        sys.exit(1)


def cmd_redirects_pull(args):
    """Exporter les redirections Odoo vers un Excel."""
    print_header("REDIRECTS PULL - Export depuis Odoo")

    service = RedirectSyncService()
    excel_path = resolve_project_path(args.excel)

    print_info(f"Fichier Excel: {excel_path}")
    print_info(f"Onglet: {args.sheet}")
    if args.all:
        print_warning("Mode --all: inclut aussi les redirections inactives")

    try:
        result = service.pull_to_excel(
            excel_path=excel_path,
            sheet_name=args.sheet,
            only_active=not args.all,
        )
        print_success(
            f"Export terminé: {result['count']} redirections dans {result['excel_path']} (onglet '{result['sheet_name']}')"
        )
    except RedirectSyncError as e:
        print_error(str(e))
        sys.exit(1)
    except Exception as e:
        print_error(f"Erreur lors de l'export des redirections: {e}")
        sys.exit(1)


def cmd_redirects_push(args):
    """Importer/upserter les redirections depuis un Excel vers Odoo."""
    print_header("REDIRECTS PUSH - Import vers Odoo")

    service = RedirectSyncService()
    excel_path = resolve_project_path(args.excel)

    print_info(f"Fichier Excel: {excel_path}")
    print_info(f"Onglet: {args.sheet}")
    if args.dry_run:
        print_warning("Mode --dry-run: aucune écriture Odoo")
    if args.takeover_existing:
        print_warning("Mode --takeover-existing: reprend les redirections existantes pour les sources du fichier et désactive les doublons")
    if args.prune:
        print_warning("Mode --prune: désactive les redirections gérées absentes du fichier")
    if args.prune_unmanaged:
        print_warning("Mode --prune-unmanaged: désactive aussi les redirections non gérées absentes du fichier")

    try:
        result = service.push_from_excel(
            excel_path=excel_path,
            sheet_name=args.sheet,
            dry_run=args.dry_run,
            prune=args.prune,
            takeover_existing=args.takeover_existing,
            prune_unmanaged=args.prune_unmanaged,
        )
        print()
        print_info(f"Lignes lues: {result['total_excel_rows']} ({result['unique_sources']} sources uniques)")
        if result.get("self_redirects_skipped"):
            print_warning(f"Auto-redirections ignorées (source = destination): {result['self_redirects_skipped']}")
        print_info(f"À créer: {result['to_create']}")
        print_info(f"À mettre à jour: {result['to_update']}")
        if args.takeover_existing:
            print_info(f"À reprendre sous gestion sync-engine: {result['to_take_over']}")
        print_info(f"Inchangées: {result['unchanged']}")
        if args.takeover_existing:
            print_info(f"Doublons à désactiver (même source): {result['to_deactivate_duplicates']}")
        if args.prune:
            print_info(f"À désactiver (prune géré): {result['to_deactivate_managed']}")
        if args.prune_unmanaged:
            print_info(f"À désactiver (prune non géré): {result['to_deactivate_unmanaged']}")
        if args.prune or args.prune_unmanaged or args.takeover_existing:
            print_info(f"Total désactivations prévues: {result['to_deactivate']}")
        if result["ambiguous_sources"]:
            print_warning("Sources ambiguës (plusieurs redirections existantes pour le même url_from):")
            for src in result["ambiguous_sources"][:20]:
                print(f"  - {src}")
            if len(result["ambiguous_sources"]) > 20:
                print(f"  ... et {len(result['ambiguous_sources']) - 20} autres")
        if args.dry_run:
            print_success("Dry-run terminé. Relance sans --dry-run pour appliquer.")
        else:
            print_success("Import des redirections terminé.")
    except RedirectSyncError as e:
        print_error(str(e))
        sys.exit(1)
    except Exception as e:
        print_error(f"Erreur lors du push des redirections: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="odoo-sync",
        description="Synchronisation bidirectionnelle Odoo ↔ Fichiers locaux (style Git)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  %(prog)s pull                                           # Récupérer tout depuis Odoo
  %(prog)s pull products --lang en_US                     # Pull des produits en anglais
  %(prog)s pull pages --all-langs                         # Pull des pages dans toutes les langues Odoo actives
  %(prog)s pull pages                                     # Récupérer seulement les pages
  %(prog)s pull dynamic-pages                             # Récupérer les vues dynamiques produit
  %(prog)s pull installations-catalog                     # Récupérer les vues catalogue installations
  %(prog)s pull --ids 4,7,10                              # Récupérer des IDs spécifiques
  
  %(prog)s push blog-posts/bricolage/564-construire.html  # Pousser UN fichier spécifique (RECOMMANDÉ)
  %(prog)s push blog-posts/564.html pages/6-contact.xml   # Pousser plusieurs fichiers
  %(prog)s push --category blog-posts                     # Pousser toute une catégorie
  %(prog)s push --category all                            # Pousser tout (attention !)
  %(prog)s push --category products --lang en_US          # Push en langue anglaise
  %(prog)s push pages/nouvelle-page.xml --create          # Créer une nouvelle page Odoo depuis un fichier local sans ID
  %(prog)s delete pages/123-ma-page.xml                   # Prévisualiser la suppression d'une page Odoo
  %(prog)s delete pages/123-ma-page.xml --apply           # Supprimer réellement la page Odoo
  %(prog)s images-pull --ids 123,456                      # Exporter des images Odoo + sidecars metadata
  %(prog)s images-push images/attachments/123-image.webp  # Pousser description/name/public de l'image
  
  %(prog)s status                                         # Voir l'état de synchronisation
  %(prog)s diff pages/4-home.xml                          # Voir les différences
  %(prog)s validate pages/ma-page.xml                      # Préflight local avant push
  %(prog)s list blog-posts                                # Lister les articles de blog
  %(prog)s init                                           # Créer .env et les dossiers locaux
  %(prog)s redirects-pull --excel redirections.xlsx
  %(prog)s redirects-push --excel redirections.xlsx --dry-run
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commandes disponibles")

    init_parser = subparsers.add_parser("init", help="Créer .env.example, .env et les dossiers de sync")
    init_parser.add_argument("--force", action="store_true", help="Écraser .env/.env.example existants")
    init_parser.set_defaults(func=cmd_init)
    
    # Commande pull
    pull_parser = subparsers.add_parser("pull", help="Récupérer depuis Odoo")
    pull_parser.add_argument("category", nargs="?", default="all",
                            choices=["all", "pages", "blog-posts", "products", "dynamic-pages", "installations-catalog", "employees", "categories", "menus"],
                            help="Catégorie à récupérer (défaut: all)")
    pull_parser.add_argument("--ids", "-i", help="IDs spécifiques (séparés par virgule)")
    pull_parser.add_argument("--prune", action="store_true",
                            help="(blog-posts/products) Supprime en local les fichiers obsolètes (absents sur Odoo / doublons), si non modifiés localement")
    pull_parser.add_argument("--force", "-f", action="store_true",
                            help="Écraser les modifications locales")
    pull_parser.add_argument("--content-only", action="store_true",
                            help="Ne synchroniser que le champ de contenu (sans pousser/tirer les métadonnées SEO)")
    pull_parser.add_argument("--lang", default="fr_FR",
                            help="Langue de sync (ex: fr_FR, en_US)")
    pull_parser.add_argument("--all-langs", action="store_true",
                            help="Récupérer toutes les langues actives d'Odoo (fichiers suffixés .en_US, .de_DE, etc.)")
    pull_parser.set_defaults(func=cmd_pull)
    
    # Commande push
    push_parser = subparsers.add_parser("push", help="Pousser vers Odoo")
    push_parser.add_argument("files", nargs="*", 
                            help="Fichiers spécifiques à pousser (ex: blog-posts/bricolage/564-construire-un-abri-velo-en-bois.html)")
    push_parser.add_argument("--category", "-c",
                            choices=["all", "pages", "blog-posts", "products", "dynamic-pages", "installations-catalog", "employees", "categories", "menus"],
                            help="Pousser toute une catégorie (si aucun fichier spécifié)")
    push_parser.add_argument("--force", "-f", action="store_true",
                            help="Écraser les modifications remote")
    push_parser.add_argument("--no-commit", action="store_true",
                            help="Ne pas créer de commit Git automatique (non recommandé)")
    push_parser.add_argument("--content-only", action="store_true",
                            help="Pousser uniquement le champ de contenu (ignore les métadonnées du header)")
    push_parser.add_argument("--lang", default="fr_FR",
                            help="Langue de sync (ex: fr_FR, en_US)")
    push_parser.add_argument("--create", action="store_true",
                            help="Créer un nouvel objet quand un fichier pages/products/blog-posts n'a pas encore d'ID (non publié par défaut)")
    push_parser.add_argument("--all-langs", action="store_true",
                            help="Avec des fichiers ciblés, pousser aussi les variantes suffixées existantes du même ID Odoo")
    push_parser.add_argument("--publish", action="store_true",
                            help="Publier explicitement les objets poussés")
    push_parser.add_argument("--unpublish", action="store_true",
                            help="Dépublier explicitement les objets poussés")
    push_parser.add_argument("--index", action="store_true",
                            help="Indexer explicitement les pages poussées (website_indexed=True)")
    push_parser.add_argument("--no-index", action="store_true",
                            help="Désindexer explicitement les pages poussées (website_indexed=False)")
    push_parser.add_argument("--skip-validate", action="store_true",
                            help="Désactiver le préflight local automatique avant push")
    push_parser.add_argument("--strict-validate", action="store_true",
                            help="Faire échouer le push aussi sur les warnings de préflight")
    push_parser.add_argument("--dry-run", action="store_true",
                            help="Simuler le push sans écrire sur Odoo")
    push_parser.set_defaults(func=cmd_push)

    # Commande validate
    validate_parser = subparsers.add_parser("validate", help="Valider localement des fichiers avant push")
    validate_parser.add_argument("files", nargs="*",
                                 help="Fichiers spécifiques à valider")
    validate_parser.add_argument("--category", "-c",
                                 choices=["all", "pages", "blog-posts", "products"],
                                 default="all",
                                 help="Catégorie à valider si aucun fichier n'est fourni")
    validate_parser.add_argument("--strict", action="store_true",
                                 help="Échouer aussi sur les warnings")
    validate_parser.add_argument("--max-issues", type=int, default=100,
                                 help="Nombre maximum de problèmes affichés (défaut: 100)")
    validate_parser.set_defaults(func=cmd_validate)
    
    # Commande status
    status_parser = subparsers.add_parser("status", help="État de synchronisation")
    status_parser.add_argument("category", nargs="?", default="all",
                              choices=["all", "pages", "blog-posts", "products", "dynamic-pages", "installations-catalog", "employees", "categories", "menus"],
                              help="Catégorie (défaut: all)")
    status_parser.add_argument("--verbose", "-v", action="store_true",
                              help="Afficher tous les fichiers")
    status_parser.add_argument("--lang", default=None,
                              help="Filtrer sur une langue spécifique (ex: fr_FR, en_US)")
    status_parser.set_defaults(func=cmd_status)
    
    # Commande diff
    diff_parser = subparsers.add_parser("diff", help="Voir les différences")
    diff_parser.add_argument("file", help="Chemin du fichier")
    diff_parser.add_argument("--show", "-s", action="store_true",
                            help="Afficher le contenu")
    diff_parser.add_argument("--lang", default=None,
                            help="Langue à comparer si absente du header metadata")
    diff_parser.set_defaults(func=cmd_diff)
    
    # Commande test
    test_parser = subparsers.add_parser("test", help="Tester la connexion Odoo")
    test_parser.set_defaults(func=cmd_test)
    
    # Commande list
    list_parser = subparsers.add_parser("list", help="Lister les éléments sur Odoo")
    list_parser.add_argument("category", choices=["pages", "blog-posts", "products", "dynamic-pages", "installations-catalog", "employees", "categories", "menus"],
                            help="Catégorie à lister")
    list_parser.add_argument("--limit", "-l", type=int, default=50,
                            help="Nombre maximum d'éléments")
    list_parser.add_argument("--verbose", "-v", action="store_true",
                            help="Afficher plus de détails")
    list_parser.add_argument("--lang", default="fr_FR",
                            help="Langue de lecture (ex: fr_FR, en_US)")
    list_parser.set_defaults(func=cmd_list)

    # Commande langs
    langs_parser = subparsers.add_parser("langs", help="Lister les langues actives Odoo")
    langs_parser.set_defaults(func=cmd_langs)

    # Commande delete
    delete_parser = subparsers.add_parser("delete", help="Supprimer des enregistrements côté Odoo (dry-run par défaut)")
    delete_parser.add_argument("files", nargs="*",
                               help="Fichiers locaux à supprimer côté Odoo (ex: pages/123-ma-page.xml)")
    delete_parser.add_argument("--category", "-c",
                               choices=["pages", "blog-posts", "products", "dynamic-pages", "installations-catalog", "employees", "categories", "menus"],
                               default="pages",
                               help="Catégorie à supprimer (défaut: pages)")
    delete_parser.add_argument("--ids", "-i", help="IDs Odoo spécifiques (séparés par virgule)")
    delete_parser.add_argument("--apply", action="store_true",
                               help="Appliquer réellement la suppression (sinon dry-run)")
    delete_parser.add_argument("--force", "-f", action="store_true",
                               help="Supprimer même si le remote a changé depuis le dernier sync")
    delete_parser.add_argument("--lang", default="fr_FR",
                               help="Langue/contexte de suppression (défaut: fr_FR)")
    delete_parser.set_defaults(func=cmd_delete)

    # Commandes images
    images_pull_parser = subparsers.add_parser("images-pull", help="Exporter des ir.attachment image en local")
    images_pull_parser.add_argument("--ids", "-i", help="IDs d'attachments spécifiques (séparés par virgule)")
    images_pull_parser.add_argument("--res-model", help="Filtrer par res_model (ex: blog.post)")
    images_pull_parser.add_argument("--res-id", type=int, help="Filtrer par res_id")
    images_pull_parser.add_argument("--limit", type=int, default=100,
                                    help="Nombre maximum d'images à tirer (défaut: 100)")
    images_pull_parser.add_argument("--all", action="store_true",
                                    help="Tirer toutes les images correspondant au filtre")
    images_pull_parser.add_argument("--force", "-f", action="store_true",
                                    help="Écraser les images locales modifiées")
    images_pull_parser.set_defaults(func=cmd_images_pull)

    images_push_parser = subparsers.add_parser("images-push", help="Pousser les images/metadonnées ir.attachment")
    images_push_parser.add_argument("files", nargs="*",
                                    help="Images ou sidecars .odoo.json à pousser (défaut: tout images/attachments)")
    images_push_parser.add_argument("--create", action="store_true",
                                    help="Créer l'attachment Odoo si le sidecar n'a pas d'id")
    images_push_parser.add_argument("--dry-run", action="store_true",
                                    help="Simuler sans écrire sur Odoo")
    images_push_parser.set_defaults(func=cmd_images_push)

    # Commande redirects-pull
    redirects_pull_parser = subparsers.add_parser("redirects-pull", help="Exporter les redirections Odoo vers Excel")
    redirects_pull_parser.add_argument(
        "--excel",
        default="redirections.xlsx",
        help="Chemin du fichier Excel cible",
    )
    redirects_pull_parser.add_argument(
        "--sheet",
        default="Redirections Odoo",
        help="Nom de l'onglet de sortie",
    )
    redirects_pull_parser.add_argument(
        "--all",
        action="store_true",
        help="Inclure aussi les redirections inactives",
    )
    redirects_pull_parser.set_defaults(func=cmd_redirects_pull)

    # Commande redirects-push
    redirects_push_parser = subparsers.add_parser("redirects-push", help="Créer/mettre à jour des redirections Odoo depuis Excel")
    redirects_push_parser.add_argument(
        "--excel",
        default="redirections.xlsx",
        help="Chemin du fichier Excel source",
    )
    redirects_push_parser.add_argument(
        "--sheet",
        default="Redirections",
        help="Nom de l'onglet à lire (Ancienne URL / Nouvelle URL)",
    )
    redirects_push_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simuler sans écrire sur Odoo",
    )
    redirects_push_parser.add_argument(
        "--prune",
        action="store_true",
        help="Désactiver les redirections gérées absentes du fichier Excel",
    )
    redirects_push_parser.add_argument(
        "--takeover-existing",
        action="store_true",
        help="Reprendre les redirections existantes pour les sources du fichier sous gestion sync-engine et désactiver les doublons",
    )
    redirects_push_parser.add_argument(
        "--prune-unmanaged",
        action="store_true",
        help="Désactiver aussi les redirections non gérées par sync-engine absentes du fichier Excel (dangereux, à auditer d'abord)",
    )
    redirects_push_parser.set_defaults(func=cmd_redirects_push)

    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
    args.func(args)


if __name__ == "__main__":
    main()
