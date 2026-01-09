"""Client configuration loader with caching and pre-compiled patterns."""
import json
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Pattern

logger = logging.getLogger(__name__)


@dataclass
class ClientConfig:
    """Client-specific configuration."""
    name: str = "Default"
    url_patterns: List[str] = field(default_factory=list)
    
    # Browser-use timing parameters
    minimum_wait_page_load_time: float = 0.5
    wait_for_network_idle_page_load_time: float = 1.0
    wait_between_actions: float = 0.5
    
    # Custom prompts for LLM
    system_prompt_additions: List[str] = field(default_factory=list)
    
    # Pre-compiled patterns (internal)
    _compiled_patterns: List[Pattern] = field(default_factory=list, repr=False)


class FileBasedConfigProvider:
    """Load client configs from JSON files with caching."""
    
    def __init__(self):
        self.clients_dir = Path(__file__).parent
        self._configs: Dict[str, ClientConfig] = {}
        self._default: ClientConfig = ClientConfig()
        self._url_cache: Dict[str, ClientConfig] = {}
        self._load_all()
    
    def _load_all(self):
        """Load and pre-compile all client configs."""
        loaded = 0
        for client_dir in self.clients_dir.iterdir():
            if client_dir.is_dir() and not client_dir.name.startswith(('_', '.', '__')):
                config_file = client_dir / 'config.json'
                if config_file.exists():
                    config = self._parse(config_file)
                    if config:
                        self._configs[client_dir.name] = config
                        loaded += 1
        
        # Load default
        default_file = self.clients_dir / '_default' / 'config.json'
        if default_file.exists():
            self._default = self._parse(default_file) or ClientConfig()
        
        logger.info(f"📋 Loaded {loaded} client configs")
    
    def _parse(self, path: Path) -> Optional[ClientConfig]:
        """Parse JSON and pre-compile regex patterns."""
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            
            timing = data.get('timing', {})
            patterns = data.get('url_patterns', [])
            
            config = ClientConfig(
                name=data.get('name', path.parent.name),
                url_patterns=patterns,
                minimum_wait_page_load_time=timing.get('minimum_wait_page_load_time', 0.5),
                wait_for_network_idle_page_load_time=timing.get('wait_for_network_idle_page_load_time', 1.0),
                wait_between_actions=timing.get('wait_between_actions', 0.5),
                system_prompt_additions=data.get('prompts', {}).get('system_prompt_additions', [])
            )
            
            # Pre-compile regex patterns
            for p in patterns:
                try:
                    config._compiled_patterns.append(re.compile(p, re.IGNORECASE))
                except re.error as e:
                    logger.warning(f"Invalid regex '{p}' in {path}: {e}")
            
            return config
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")
            return None
    
    def get_config(self, url: str) -> ClientConfig:
        """Get config for URL (cached)."""
        if url in self._url_cache:
            return self._url_cache[url]
        
        for config in self._configs.values():
            for pattern in config._compiled_patterns:
                if pattern.search(url):
                    self._url_cache[url] = config
                    return config
        
        return self._default
    
    def clear_cache(self):
        """Clear URL cache."""
        self._url_cache.clear()


# Global instance
_provider: Optional[FileBasedConfigProvider] = None


def get_client_config(url: str) -> ClientConfig:
    """Get client config for URL."""
    global _provider
    if _provider is None:
        _provider = FileBasedConfigProvider()
    return _provider.get_config(url)


def reload_configs():
    """Reload all configs."""
    global _provider
    _provider = FileBasedConfigProvider()
