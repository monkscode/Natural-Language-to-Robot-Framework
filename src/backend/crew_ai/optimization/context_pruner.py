"""
Context Pruner for Smart Keyword Filtering

This module classifies user queries into action categories and prunes
keyword context to include only relevant keywords, reducing token usage
while maintaining code generation accuracy.

Uses ChromaDB's default ONNX-based embedding function for lightweight operation.
"""

import hashlib
import logging
from typing import List, Dict
import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)


class ContextPruner:
    """
    Classify queries and prune context to relevant keyword categories.
    
    Uses ChromaDB for semantic similarity to classify queries into action 
    categories (navigation, input, interaction, extraction, assertion, wait) 
    and filters keywords to only those in relevant categories.

    Uses ChromaDB's default embedding function (ONNX-based) which is
    lightweight and doesn't require PyTorch or sentence-transformers.
    """
    
    # Assertion keywords use "Should" auxiliary verb grammar which the embedding
    # model does not map to natural language verification terms ("verify", "check",
    # "confirm"). All other categories use action verbs that encode naturally
    # (Click, Fill Text, Go To, Get Text, Wait For...). This one-line prefix is
    # the minimal targeted fix — it adds verification synonyms to assertion
    # documents without restoring a full manually maintained dictionary.
    _ASSERTION_DOC_PREFIX = "verify check validate assert confirm"

    # Keyword category mappings
    KEYWORD_CATEGORIES = {
        "navigation": [
            "New Browser", "New Page", "Go To", "Go Back", "Go Forward",
            "Close Browser", "Close Page", "Switch Page", "New Context"
        ],
        "input": [
            "Fill Text", "Input Text", "Type Text", "Press Keys", 
            "Upload File", "Type Secret", "Clear Text"
        ],
        "interaction": [
            "Click", "Click Element", "Hover", "Drag And Drop",
            "Select Options By", "Check Checkbox", "Uncheck Checkbox"
        ],
        "extraction": [
            "Get Text", "Get Attribute", "Get Element Count", 
            "Get Property", "Get Style", "Get Url", "Get Title"
        ],
        "assertion": [
            "Should Be Equal", "Should Contain", "Should Be Visible",
            "Should Not Be Visible", "Should Be Enabled", "Should Be Disabled"
        ],
        "wait": [
            "Wait For Elements State", "Wait Until Element Is Visible",
            "Wait For Condition", "Wait For Load State", "Sleep"
        ]
    }
    
    def __init__(
        self, 
        persist_directory: str = "./chroma_db"
    ):
        """
        Initialize with ChromaDB for semantic classification.
        
        Args:
            persist_directory: Path to ChromaDB storage directory
        """
        logger.info(f"Initializing ContextPruner with ChromaDB at {persist_directory}")
        
        try:
            # Initialize ChromaDB client (same pattern as KeywordVectorStore)
            self.client = chromadb.PersistentClient(
                path=persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )

            # Use ChromaDB's default embedding function (ONNX-based)
            # This is lightweight and doesn't require PyTorch
            
            # Create or get category collection (ChromaDB will use default embedding)
            self.collection = self.client.get_or_create_collection(
                name="category_descriptions",
                metadata={"type": "query_categories"}
            )
            
            # Initialize category descriptions in ChromaDB
            self._init_category_collection()
            
            logger.info("ContextPruner initialized successfully with ChromaDB (using default ONNX embedding)")
            
        except Exception as e:
            logger.error(f"Failed to initialize ContextPruner: {e}")
            raise

    @classmethod
    def _build_keyword_index(cls) -> Dict[str, dict]:
        """
        Build a flat index of all keywords for per-keyword semantic indexing.

        Each keyword in KEYWORD_CATEGORIES gets its own entry keyed by
        "category::keyword_name". The keyword name itself is the document —
        the embedding model understands plain English keyword names directly,
        requiring no manually maintained description strings.

        ID format "category::keyword" guarantees uniqueness even if the same
        keyword name were ever listed under two categories.

        Returns:
            Dict[str, dict] mapping document ID to {"document": str, "category": str}
        """
        index = {}
        for cat, keywords in cls.KEYWORD_CATEGORIES.items():
            for kw in keywords:
                # Assertion keywords use "Should" grammar, not action verbs.
                # Prefix with verification synonyms so queries like "verify X"
                # or "check that Y" map to the assertion category correctly.
                if cat == "assertion":
                    document = f"{cls._ASSERTION_DOC_PREFIX} {kw}"
                else:
                    document = kw
                index[f"{cat}::{kw}"] = {"document": document, "category": cat}
        return index

    def _init_category_collection(self):
        """
        Initialize ChromaDB collection with one document per keyword.

        Each keyword in KEYWORD_CATEGORIES gets its own document tagged with
        its category. classify_query() then derives relevant categories from
        whichever keyword documents match the user query — no manually
        maintained description strings needed.

        Uses hash-based versioning. When KEYWORD_CATEGORIES changes the hash
        changes and the collection is fully rebuilt (delete + recreate) to
        prevent stale documents from a previous index format accumulating.
        Upsert alone cannot remove documents whose IDs no longer exist.
        """
        logger.debug("Initializing keyword category index in ChromaDB")

        keyword_index = self._build_keyword_index()

        # Hash over the complete index — any keyword or category change triggers rebuild
        index_hash = hashlib.md5(
            str(sorted(keyword_index.items())).encode()
        ).hexdigest()

        existing_count = self.collection.count()

        if existing_count == len(keyword_index):
            # Count matches — verify content via hash before skipping
            try:
                first_id = sorted(keyword_index.keys())[0]  # deterministic
                existing = self.collection.get(ids=[first_id])
                stored_hash = (
                    existing["metadatas"][0].get("descriptions_hash", "")
                    if existing and existing.get("metadatas")
                    else ""
                )
                if stored_hash == index_hash:
                    logger.debug("Keyword index unchanged (hash match), skipping re-index")
                    return
                logger.info("Keyword index changed (hash mismatch), rebuilding collection...")
            except Exception:
                logger.info("Could not verify index hash, forcing rebuild")

        # Re-index needed: delete and recreate to prevent stale document accumulation.
        # This also handles migration from the old 6-document category-level format.
        try:
            self.client.delete_collection("category_descriptions")
            logger.debug("Deleted stale category_descriptions collection")
        except Exception:
            pass  # Does not exist on first run — safe to ignore

        self.collection = self.client.get_or_create_collection(
            name="category_descriptions",
            embedding_function=self.embedding_function,
            metadata={"type": "query_categories"},
        )

        ids = list(keyword_index.keys())
        documents = [keyword_index[i]["document"] for i in ids]
        metadatas = [
            {"category": keyword_index[i]["category"], "descriptions_hash": index_hash}
            for i in ids
        ]

        self.collection.add(ids=ids, documents=documents, metadatas=metadatas)
        logger.info(
            f"Keyword index built: {len(ids)} keywords across "
            f"{len(self.KEYWORD_CATEGORIES)} categories"
        )
    
    def classify_query(
        self,
        user_query: str,
        confidence_threshold: float = 0.6,
    ) -> List[str]:
        """
        Classify query into action categories by matching against keyword documents.

        Queries the per-keyword ChromaDB index. For each category the highest
        similarity score across all its keywords is used (max aggregation).
        Categories where at least one keyword exceeds the threshold are returned.

        This avoids the need for manually maintained category descriptions —
        the embedding model matches user phrasings directly to keyword names.

        Args:
            user_query: User's natural language query
            confidence_threshold: Minimum similarity for category inclusion (0.0-1.0)

        Returns:
            List of relevant category names, or all categories as fallback.
        """
        logger.debug(f"Classifying query: {user_query[:50]}...")

        try:
            doc_count = self.collection.count()
            if doc_count == 0:
                logger.warning("Keyword index is empty. Falling back to all categories.")
                return list(self.KEYWORD_CATEGORIES.keys())

            # Query all keyword documents — 41 docs is small, querying all is cheap.
            # n_results must be <= doc_count (ChromaDB constraint).
            n_results = min(doc_count, len(self.KEYWORD_CATEGORIES) * 10)

            results = self.collection.query(
                query_texts=[user_query],
                n_results=n_results,
            )

            if not results.get("ids") or not results["ids"][0]:
                logger.warning("ChromaDB returned no results. Falling back to all categories.")
                return list(self.KEYWORD_CATEGORIES.keys())

            # Aggregate: track the best (max) similarity per category.
            # One strong keyword match is sufficient to include a category.
            best_sim_per_category: Dict[str, float] = {}

            for idx, doc_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][idx]
                # ChromaDB normalized cosine distance [0, 2] → similarity [0, 1]
                similarity = 1.0 - (distance / 2.0)
                category = results["metadatas"][0][idx]["category"]

                if category not in best_sim_per_category or similarity > best_sim_per_category[category]:
                    best_sim_per_category[category] = similarity

                logger.debug(
                    f"  keyword='{doc_id}' category='{category}' "
                    f"distance={distance:.4f} similarity={similarity:.4f}"
                )

            relevant_categories = [
                cat for cat, sim in best_sim_per_category.items()
                if sim >= confidence_threshold
            ]

            if relevant_categories:
                logger.info(
                    f"Classified query into {len(relevant_categories)} categories: "
                    f"{relevant_categories} (threshold={confidence_threshold})"
                )
                return relevant_categories

            # Graceful degradation: no category met threshold
            all_categories = list(self.KEYWORD_CATEGORIES.keys())
            max_sim = max(best_sim_per_category.values()) if best_sim_per_category else 0.0
            logger.warning(
                f"No categories met threshold {confidence_threshold}. "
                f"Highest similarity: {max_sim:.4f}. Falling back to all categories."
            )
            return all_categories

        except Exception as e:
            logger.error(f"Classification failed: {e}. Falling back to all categories.")
            return list(self.KEYWORD_CATEGORIES.keys())
    
    def prune_keywords(
        self, 
        all_keywords: List[Dict], 
        categories: List[str]
    ) -> List[Dict]:
        """
        Filter keywords to only those in relevant categories.
        
        Args:
            all_keywords: List of keyword dicts with 'name' field
            categories: List of relevant category names
            
        Returns:
            Filtered list of keyword dicts
        """
        logger.debug(f"Pruning keywords for categories: {categories}")
        
        # Build set of relevant keyword names
        relevant_names = set()
        for category in categories:
            if category in self.KEYWORD_CATEGORIES:
                relevant_names.update(self.KEYWORD_CATEGORIES[category])
        
        # Filter keywords
        pruned = [
            kw for kw in all_keywords 
            if kw.get("name") in relevant_names
        ]
        
        if all_keywords:
            logger.info(f"Pruned {len(all_keywords)} keywords to {len(pruned)} ({len(pruned)/len(all_keywords)*100:.1f}% retained)")
        else:
            logger.info("No keywords to prune (empty input)")
        
        return pruned
    
    def get_pruning_stats(
        self, 
        original_count: int, 
        pruned_count: int
    ) -> Dict[str, float]:
        """
        Calculate pruning statistics.
        
        Args:
            original_count: Number of keywords before pruning
            pruned_count: Number of keywords after pruning
            
        Returns:
            Dict with original_count, pruned_count, retention_rate, reduction_rate, and reduction_percentage
        """
        if original_count == 0:
            return {
                "original_count": 0,
                "pruned_count": 0,
                "retention_rate": 0.0,
                "reduction_rate": 0.0,
                "reduction_percentage": 0.0
            }
        
        retention = pruned_count / original_count
        reduction = 1.0 - retention
        
        return {
            "original_count": original_count,
            "pruned_count": pruned_count,
            "retention_rate": retention,
            "reduction_rate": reduction,
            "reduction_percentage": reduction * 100
        }
