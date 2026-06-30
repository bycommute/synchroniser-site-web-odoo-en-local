"""Client Odoo pour communiquer avec l'API XML-RPC."""
from defusedxml.xmlrpc import monkey_patch

monkey_patch()

# defusedxml.xmlrpc.monkey_patch() is applied above.
import xmlrpc.client  # nosec B411
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from .config import ODOO_CONFIG


REQUIRED_CONFIG_KEYS = ("url", "db", "username", "api_key")
PLACEHOLDER_VALUES = {
    "https://your-company.odoo.com",
    "your-database-name",
    "you@example.com",
    "your-odoo-api-key",
}


def missing_config_keys() -> List[str]:
    """Return missing Odoo connection settings."""
    missing = []
    for key in REQUIRED_CONFIG_KEYS:
        value = str(ODOO_CONFIG.get(key) or "").strip()
        if not value or value in PLACEHOLDER_VALUES:
            missing.append(key)
    url = str(ODOO_CONFIG.get("url") or "").strip()
    if url and url not in PLACEHOLDER_VALUES:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            missing.append("url")
    return missing


def assert_configured() -> None:
    missing = missing_config_keys()
    if not missing:
        return
    env_names = {
        "url": "ODOO_URL",
        "db": "ODOO_DB",
        "username": "ODOO_USERNAME",
        "api_key": "ODOO_API_KEY",
    }
    variables = ", ".join(env_names[key] for key in missing)
    raise RuntimeError(
        "Configuration Odoo incomplète. Lancez `odoo-sync init` ou demandez "
        f"à l'utilisateur les variables suivantes: {variables}."
    )


class OdooClient:
    """Client pour interagir avec l'API Odoo via XML-RPC"""
    
    def __init__(self, url: str = None, db: str = None, username: str = None, api_key: str = None, lang: str = None):
        self.url = url or ODOO_CONFIG["url"]
        self.db = db or ODOO_CONFIG["db"]
        self.username = username or ODOO_CONFIG["username"]
        self.api_key = api_key or ODOO_CONFIG["api_key"]
        self.lang = lang or ODOO_CONFIG.get("lang", "fr_FR")
        
        self._uid = None
        self._common = None
        self._models = None
        
    def _get_common(self):
        """Obtenir le proxy pour les méthodes communes"""
        if self._common is None:
            self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        return self._common
    
    def _get_models(self):
        """Obtenir le proxy pour les méthodes sur les modèles"""
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        return self._models
    
    def authenticate(self) -> int:
        """Authentification et récupération de l'UID"""
        assert_configured()
        if self._uid is None:
            common = self._get_common()
            self._uid = common.authenticate(self.db, self.username, self.api_key, {})
            if not self._uid:
                raise Exception("Échec de l'authentification Odoo. Vérifiez vos credentials.")
        return self._uid
    
    def execute(self, model: str, method: str, *args, **kwargs) -> Any:
        """Exécuter une méthode sur un modèle Odoo"""
        uid = self.authenticate()
        models = self._get_models()
        
        # Ajouter le contexte de langue si non spécifié
        if "context" not in kwargs:
            kwargs["context"] = {"lang": self.lang}
        elif "lang" not in kwargs["context"]:
            kwargs["context"]["lang"] = self.lang
            
        return models.execute_kw(
            self.db, uid, self.api_key,
            model, method,
            list(args),
            kwargs if kwargs else {}
        )
    
    def search_read(
        self,
        model: str,
        domain: List = None,
        fields: List[str] = None,
        limit: int = None,
        offset: int = 0,
        order: str = None,
        context: Optional[Dict] = None,
    ) -> List[Dict]:
        """Rechercher et lire des enregistrements"""
        domain = domain or []
        kwargs = {}
        if fields:
            kwargs["fields"] = fields
        if limit:
            kwargs["limit"] = limit
        if offset:
            kwargs["offset"] = offset
        if order:
            kwargs["order"] = order
            
        if context:
            kwargs["context"] = context
        return self.execute(model, "search_read", domain, **kwargs)
    
    def read(
        self,
        model: str,
        ids: List[int],
        fields: List[str] = None,
        context: Optional[Dict] = None,
    ) -> List[Dict]:
        """Lire des enregistrements par leurs IDs"""
        kwargs = {}
        if fields:
            kwargs["fields"] = fields
        if context:
            kwargs["context"] = context
        return self.execute(model, "read", ids, **kwargs)
    
    def write(
        self,
        model: str,
        ids: List[int],
        values: Dict,
        context: Optional[Dict] = None,
    ) -> bool:
        """Mettre à jour des enregistrements"""
        kwargs = {}
        if context:
            kwargs["context"] = context
        return self.execute(model, "write", ids, values, **kwargs)
    
    def create(self, model: str, values: Dict, context: Optional[Dict] = None) -> int:
        """Créer un nouvel enregistrement"""
        kwargs = {}
        if context:
            kwargs["context"] = context
        return self.execute(model, "create", values, **kwargs)

    def unlink(
        self,
        model: str,
        ids: List[int],
        context: Optional[Dict] = None,
    ) -> bool:
        """Supprimer des enregistrements"""
        kwargs = {}
        if context:
            kwargs["context"] = context
        return self.execute(model, "unlink", ids, **kwargs)
    
    def search(
        self,
        model: str,
        domain: List = None,
        limit: int = None,
        context: Optional[Dict] = None,
    ) -> List[int]:
        """Rechercher des IDs d'enregistrements"""
        domain = domain or []
        kwargs = {}
        if limit:
            kwargs["limit"] = limit
        if context:
            kwargs["context"] = context
        return self.execute(model, "search", domain, **kwargs)
    
    def test_connection(self) -> Dict:
        """Tester la connexion à Odoo"""
        try:
            assert_configured()
            common = self._get_common()
            version = common.version()
            uid = self.authenticate()
            return {
                "success": True,
                "version": version,
                "uid": uid,
                "message": f"Connecté à {self.url} (Odoo {version.get('server_version', 'unknown')})"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": f"Échec de connexion: {e}"
            }


# Instance singleton
_client = None

def get_client() -> OdooClient:
    """Obtenir l'instance du client Odoo"""
    global _client
    if _client is None:
        _client = OdooClient()
    return _client
