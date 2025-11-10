"""
Temporary metrics storage for browser-use executions.
Each workflow stores its metrics in a separate JSON file.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class TempMetricsStorage:
    """Manages temporary metrics files for in-progress workflows."""
    
    def __init__(self, storage_dir: str = "logs/temp_metrics"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Temp metrics storage initialized at {self.storage_dir}")
    
    def _get_file_path(self, workflow_id: str) -> Path:
        """Get file path for a workflow's metrics."""
        return self.storage_dir / f"{workflow_id}.json"
    
    def write_browser_metrics(
        self,
        workflow_id: str,
        metrics: Dict[str, Any]
    ) -> bool:
        """
        Write browser-use metrics to temp file.
        
        Args:
            workflow_id: Unique workflow identifier
            metrics: Browser-use metrics dict
        
        Returns:
            True if successful, False otherwise
        """
        try:
            file_path = self._get_file_path(workflow_id)
            
            data = {
                'workflow_id': workflow_id,
                'timestamp': datetime.now().isoformat(),
                'source': 'browser-use',
                'metrics': metrics
            }
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"✅ Browser-use metrics written to {file_path}")
            return True
        
        except Exception as e:
            logger.error(f"❌ Failed to write browser metrics: {e}")
            return False
    
    def read_browser_metrics(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """
        Read browser-use metrics from temp file.
        
        Args:
            workflow_id: Unique workflow identifier
        
        Returns:
            Metrics dict or None if not found
        """
        try:
            file_path = self._get_file_path(workflow_id)
            
            if not file_path.exists():
                logger.warning(f"⚠️ No temp metrics file found for {workflow_id}")
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            logger.info(f"✅ Browser-use metrics read from {file_path}")
            return data.get('metrics', {})
        
        except Exception as e:
            logger.error(f"❌ Failed to read browser metrics: {e}")
            return None
    
    def delete_temp_file(self, workflow_id: str) -> bool:
        """
        Delete temp metrics file after merging.
        
        Args:
            workflow_id: Unique workflow identifier
        
        Returns:
            True if deleted, False otherwise
        """
        try:
            file_path = self._get_file_path(workflow_id)
            
            if file_path.exists():
                file_path.unlink()
                logger.info(f"🗑️ Deleted temp metrics file: {file_path}")
                return True
            
            return False
        
        except Exception as e:
            logger.error(f"❌ Failed to delete temp file: {e}")
            return False
    
    def cleanup_old_files(self, max_age_hours: int = 24):
        """
        Clean up temp files older than specified hours.
        Useful for handling crashed workflows.
        
        Args:
            max_age_hours: Delete files older than this
        """
        try:
            import time
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            
            deleted_count = 0
            for file_path in self.storage_dir.glob("*.json"):
                file_age = current_time - file_path.stat().st_mtime
                
                if file_age > max_age_seconds:
                    file_path.unlink()
                    deleted_count += 1
            
            if deleted_count > 0:
                logger.info(f"🗑️ Cleaned up {deleted_count} old temp metrics files")
        
        except Exception as e:
            logger.error(f"❌ Failed to cleanup old files: {e}")


# Global instance
_temp_storage: Optional[TempMetricsStorage] = None


def get_temp_metrics_storage() -> TempMetricsStorage:
    """Get the global temp metrics storage instance."""
    global _temp_storage
    if _temp_storage is None:
        _temp_storage = TempMetricsStorage()
    return _temp_storage
