"""
ChromaDB vector store for keyword embeddings and semantic search.

This module provides persistent storage and semantic search for Robot Framework
keywords using ChromaDB with sentence-transformers embeddings.
"""

import json
import logging
from typing import List, Dict, Optional
import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)


class KeywordVectorStore:
    """
    ChromaDB-based vector store for Robot Framework keywords.
    
    Provides:
    - Persistent storage of keyword embeddings
    - Semantic search over keywords
    - Library-specific collections
    - Version tracking and auto-rebuild
    """
    
    def __init__(self, persist_directory: str = "./chroma_db"):
        """
        Initialize ChromaDB client with persistence.
        
        Args:
            persist_directory: Path to ChromaDB storage directory
        """
        self.persist_directory = persist_directory
        
        try:
            # Initialize ChromaDB client with persistence
            self.client = chromadb.PersistentClient(
                path=persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
            
            # Initialize embedding function (sentence-transformers)
            # ChromaDB 0.5.x changed the API - now uses default embedding function
            try:
                # Try ChromaDB 0.5.x API first (model_name parameter)
                self.embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )
            except TypeError:
                # Fallback for ChromaDB 0.4.x API (no model_name parameter)
                self.embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction()
            
            logger.info(f"ChromaDB initialized at {persist_directory}")
            
        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            raise
    
    def create_or_get_collection(self, library_name: str):
        """
        Get or create collection for library keywords.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            
        Returns:
            ChromaDB collection for keywords
        """
        collection_name = f"keywords_{library_name.lower()}"
        
        try:
            collection = self.client.get_or_create_collection(
                name=collection_name,
                embedding_function=self.embedding_function,
                metadata={"library": library_name}
            )
            logger.debug(f"Collection '{collection_name}' ready")
            return collection
            
        except Exception as e:
            logger.error(f"Failed to create/get collection '{collection_name}': {e}")
            raise
    
    def get_or_create_pattern_collection(self):
        """
        Get or create collection for query patterns (used by pattern learning).
        
        Returns:
            ChromaDB collection for query patterns
        """
        collection_name = "query_patterns"
        
        try:
            collection = self.client.get_or_create_collection(
                name=collection_name,
                embedding_function=self.embedding_function,
                metadata={"type": "query_patterns"}
            )
            logger.debug(f"Collection '{collection_name}' ready")
            return collection
            
        except Exception as e:
            logger.error(f"Failed to create/get collection '{collection_name}': {e}")
            raise

    
    def add_keywords(self, library_name: str, keywords: List[Dict]) -> None:
        """
        Add keywords to ChromaDB collection.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            keywords: List of keyword dictionaries with name, doc, args
        """
        if not keywords:
            logger.warning(f"No keywords provided for {library_name}")
            return
        
        collection = self.create_or_get_collection(library_name)
        
        try:
            # Prepare documents (keyword name + documentation for embedding)
            documents = []
            ids = []
            metadatas = []
            
            for kw in keywords:
                name = kw.get('name', '')
                doc = kw.get('doc', '')
                args = kw.get('args', [])
                
                if not name:
                    continue
                
                # Create searchable text: keyword name + documentation
                searchable_text = f"{name} {doc}"
                documents.append(searchable_text)
                ids.append(name)
                
                # Store metadata (truncate long docs to 500 chars)
                metadatas.append({
                    "name": name,
                    "args": json.dumps(args),
                    "doc": doc[:500] if doc else ""
                })
            
            if not documents:
                logger.warning(f"No valid keywords to add for {library_name}")
                return
            
            # Add to collection (ChromaDB handles embedding generation)
            collection.add(
                documents=documents,
                ids=ids,
                metadatas=metadatas
            )
            
            logger.info(f"Added {len(documents)} keywords to {library_name} collection")
            
        except Exception as e:
            logger.error(f"Failed to add keywords to {library_name}: {e}")
            raise
    
    def ingest_library_keywords(self, library_name: str) -> None:
        """
        Extract and ingest all keywords from a library using DynamicLibraryDocumentation.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
        """
        try:
            # Import here to avoid circular dependency
            from ..library_context.dynamic_context import DynamicLibraryDocumentation
            
            logger.info(f"Extracting keywords from {library_name}...")
            doc_extractor = DynamicLibraryDocumentation(library_name)
            doc_data = doc_extractor.get_library_documentation()
            
            keywords = doc_data.get('keywords', [])
            
            # Filter out internal/deprecated keywords
            # NOTE: Only filter if keyword ITSELF is deprecated (mentioned in first ~150 chars)
            # Some keywords have deprecated PARAMETERS but are still valid (e.g., Click With Options)
            public_keywords = [
                kw for kw in keywords 
                if not kw['name'].startswith('_') and 
                   'deprecated' not in kw.get('doc', '')[:150].lower()  # Only check first 150 chars
            ]
            
            logger.info(f"Found {len(public_keywords)} public keywords in {library_name}")
            
            # Add to ChromaDB
            self.add_keywords(library_name, public_keywords)
            
        except Exception as e:
            logger.error(f"Failed to ingest keywords from {library_name}: {e}")
            raise

    
    def search(self, library_name: str, query: str, top_k: int = 3) -> List[Dict]:
        """
        Semantic search for keywords using ChromaDB.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            query: Natural language query describing what you want to do
            top_k: Number of results to return (default: 3)
            
        Returns:
            List of matching keywords with metadata and similarity scores
            Format: [{"name": str, "args": list, "description": str, "distance": float}, ...]
        """
        collection = self.create_or_get_collection(library_name)
        
        try:
            # Query ChromaDB (it handles embedding generation automatically)
            results = collection.query(
                query_texts=[query],
                n_results=top_k
            )
            
            # Check if we have results
            if not results['ids'] or not results['ids'][0]:
                logger.warning(f"No results found for query: {query}")
                return []
            
            # Format results
            keywords = []
            for i in range(len(results['ids'][0])):
                metadata = results['metadatas'][0][i]
                distance = results['distances'][0][i] if results.get('distances') else 0.0
                
                keywords.append({
                    "name": metadata['name'],
                    "args": json.loads(metadata['args']),
                    "description": metadata['doc'],
                    "distance": distance,
                    "similarity": 1 / (1 + distance)  # Convert distance to similarity score
                })
            
            logger.debug(f"Found {len(keywords)} keywords for query: {query}")
            return keywords
            
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
            return []

    
    def get_library_version(self, library_name: str) -> Optional[str]:
        """
        Get the version of the installed library.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            
        Returns:
            Version string or None if not found
        """
        try:
            from ..library_context.dynamic_context import DynamicLibraryDocumentation
            
            doc_extractor = DynamicLibraryDocumentation(library_name)
            doc_data = doc_extractor.get_library_documentation()
            version = doc_data.get('version', None)
            
            logger.debug(f"{library_name} version: {version}")
            return version
            
        except Exception as e:
            logger.warning(f"Could not get version for {library_name}: {e}")
            return None
    
    def get_collection_version(self, library_name: str) -> Optional[str]:
        """
        Get the version stored in the ChromaDB collection metadata.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            
        Returns:
            Version string or None if not found
        """
        try:
            collection = self.create_or_get_collection(library_name)
            metadata = collection.metadata
            return metadata.get('version', None)
            
        except Exception as e:
            logger.warning(f"Could not get collection version for {library_name}: {e}")
            return None
    
    def needs_rebuild(self, library_name: str) -> bool:
        """
        Check if collection needs to be rebuilt due to version mismatch.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            
        Returns:
            True if rebuild needed, False otherwise
        """
        try:
            current_version = self.get_library_version(library_name)
            stored_version = self.get_collection_version(library_name)
            
            # If no stored version, need to build
            if stored_version is None:
                logger.info(f"No stored version for {library_name}, rebuild needed")
                return True
            
            # If versions don't match, need to rebuild
            if current_version != stored_version:
                logger.info(f"Version mismatch for {library_name}: {stored_version} -> {current_version}, rebuild needed")
                return True
            
            logger.debug(f"{library_name} version matches ({current_version}), no rebuild needed")
            return False
            
        except Exception as e:
            logger.warning(f"Could not check rebuild status for {library_name}: {e}")
            return False
    
    def rebuild_collection(self, library_name: str) -> None:
        """
        Rebuild collection by deleting and re-ingesting keywords.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
        """
        try:
            collection_name = f"keywords_{library_name.lower()}"
            
            # Delete existing collection
            try:
                self.client.delete_collection(name=collection_name)
                logger.info(f"Deleted existing collection: {collection_name}")
            except Exception as e:
                logger.debug(f"Collection {collection_name} does not exist or could not be deleted: {e}")
            
            # Get current library version
            current_version = self.get_library_version(library_name)
            
            # Create new collection with version metadata
            collection = self.client.create_collection(
                name=collection_name,
                embedding_function=self.embedding_function,
                metadata={
                    "library": library_name,
                    "version": current_version or "unknown"
                }
            )
            
            logger.info(f"Created new collection: {collection_name} (version: {current_version})")
            
            # Ingest keywords
            self.ingest_library_keywords(library_name)
            
            logger.info(f"Successfully rebuilt collection for {library_name}")
            
        except Exception as e:
            logger.error(f"Failed to rebuild collection for {library_name}: {e}")
            raise
    
    def ensure_collection_ready(self, library_name: str) -> None:
        """
        Ensure collection is ready and up-to-date.
        Auto-rebuilds if version mismatch detected.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
        """
        try:
            if self.needs_rebuild(library_name):
                logger.info(f"Rebuilding collection for {library_name}...")
                self.rebuild_collection(library_name)
            else:
                logger.debug(f"Collection for {library_name} is up-to-date")
                
        except Exception as e:
            logger.error(f"Failed to ensure collection ready for {library_name}: {e}")
            raise
