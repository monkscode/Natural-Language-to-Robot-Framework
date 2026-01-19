"""Client configuration loader with caching and pre-compiled patterns."""
import json
import re
import logging
from collections import OrderedDict
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
    
    # Maximum number of URL lookups to cache (LRU eviction)
    MAX_CACHE_SIZE = 100
    
    def __init__(self):
        self.clients_dir = Path(__file__).parent
        self._configs: Dict[str, ClientConfig] = {}
        self._default: ClientConfig = ClientConfig()
        # OrderedDict for LRU cache behavior (oldest entries evicted first)
        self._url_cache: OrderedDict[str, ClientConfig] = OrderedDict()
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
            logger.exception(f"Failed to load {path}: {e}")
            return None
    
    def get_config(self, url: str) -> ClientConfig:
        """Get config for URL with LRU caching."""
        # Check cache first
        if url in self._url_cache:
            # Move to end for LRU behavior (most recently used)
            self._url_cache.move_to_end(url)
            return self._url_cache[url]
        
        # Search for matching pattern
        result = self._default  # Default if no pattern matches
        for config in self._configs.values():
            for pattern in config._compiled_patterns:
                if pattern.search(url):
                    result = config
                    break
            if result is not self._default:
                break
        
        # Cache the result (including default matches)
        self._url_cache[url] = result
        
        # Evict oldest entry if cache exceeds max size
        if len(self._url_cache) > self.MAX_CACHE_SIZE:
            self._url_cache.popitem(last=False)  # Remove oldest (first) entry
        
        return result
    
    def clear_cache(self):
        """Clear URL cache."""
        self._url_cache.clear()


# Global instance with thread-safe lazy initialization
_provider: Optional[FileBasedConfigProvider] = None
_provider_lock = __import__('threading').Lock()


def get_client_config(url: str) -> ClientConfig:
    """Get client config for URL (thread-safe)."""
    global _provider
    # Fast path: if already initialized, skip lock
    if _provider is not None:
        return _provider.get_config(url)
    
    # Slow path: acquire lock for initialization
    with _provider_lock:
        # Double-check after acquiring lock (another thread may have initialized)
        if _provider is None:
            _provider = FileBasedConfigProvider()
    
    return _provider.get_config(url)


def reload_configs():
    """Reload all configs (thread-safe)."""
    global _provider
    with _provider_lock:
        _provider = FileBasedConfigProvider()
