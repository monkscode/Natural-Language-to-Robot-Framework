"""
Healing Orchestrator Service for Test Self-Healing System.

This service coordinates the entire healing workflow, managing session state,
progress tracking, retry logic, and report generation.
"""

import asyncio
import json
import logging
import time
import uuid
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from crewai import Crew

from ..core.models import (
    FailureContext, HealingSession, HealingReport, HealingStatus, 
    LocatorAttempt, LocatorStrategy, HealingConfiguration
)
from ..core.logging_config import get_healing_logger
from ..core.metrics import get_metrics_collector
from ..core.audit_trail import get_audit_trail
from ..core.alerting import get_alerting_system
from ..core.config import settings
from ..crew_ai.healing_agents import HealingAgents
from ..crew_ai.healing_tasks import HealingTasks
from .failure_detection_service import FailureDetectionService
from .chrome_session_manager import ChromeSessionManager
from .test_code_updater import RobotTestCodeUpdater, LocatorReplacement
from .fingerprinting_service import FingerprintingService
from .structural_fallback_system import StructuralFallbackSystem
from .similarity_scorer import SimilarityScorer
from .dom_analyzer import DOMAnalyzer


logger = logging.getLogger(__name__)


class HealingOrchestrator:
    """Main orchestrator for the test self-healing workflow."""
    
    def __init__(
        self, 
        config: HealingConfiguration, 
        model_provider: str = "online", 
        model_name: str = "gemini-1.5-flash"
    ):
        """Initialize the healing orchestrator.
        
        Args:
            config: Healing configuration settings
            model_provider: LLM provider ("online" or "local")
            model_name: Name of the model to use
                       - For online: "gemini-1.5-flash" (fast), "gemini-1.5-pro" (accurate)
                       - For local: "llama3.1", "llama3", etc.
        """
        self.config = config
        
        # Store model configuration
        self.model_provider = model_provider
        self.model_name = model_name
        
        # Initialize services
        self.failure_detection = FailureDetectionService()
        self.chrome_manager = ChromeSessionManager(config)
        self.code_updater = RobotTestCodeUpdater()
        self.fingerprinting = FingerprintingService()
        
        # Initialize vision-first healing components
        self.structural_fallback = StructuralFallbackSystem()
        self.similarity_scorer = SimilarityScorer()
        self.dom_analyzer = DOMAnalyzer()
        
        # BrowserUse service URL (from config or environment)
        self.browser_use_url = getattr(config, 'browser_use_url', settings.BROWSER_USE_SERVICE_URL)
        
        # Initialize AI agents and tasks
        self.agents = HealingAgents(model_provider, model_name)
        self.tasks = HealingTasks()
        
        # Session management
        self.active_sessions: Dict[str, HealingSession] = {}
        self.session_lock = asyncio.Lock()
        
        # Progress tracking
        self.progress_callbacks: Dict[str, List[Callable]] = {}
        
        # Retry and fallback configuration
        self.max_retries = 3
        self.retry_delay = 2.0  # seconds
        
        # Performance tracking
        self.performance_metrics: Dict[str, List[float]] = {
            "total_healing_time": [],
            "analysis_time": [],
            "generation_time": [],
            "validation_time": [],
            "success_rate": []
        }
        
        # Initialize logging and monitoring
        self.metrics_collector = get_metrics_collector()
        self.audit_trail = get_audit_trail()
        self.alerting_system = get_alerting_system()
        
        logger.info(f"Healing orchestrator initialized with {model_provider}/{model_name}")
    
    async def start(self):
        """Start the healing orchestrator and its dependencies."""
        await self.chrome_manager.start()
        logger.info("Healing orchestrator started")
    
    async def stop(self):
        """Stop the healing orchestrator and cleanup resources."""
        # Cancel all active sessions
        async with self.session_lock:
            for session in self.active_sessions.values():
                if session.status == HealingStatus.IN_PROGRESS:
                    session.status = HealingStatus.TIMEOUT
                    session.completed_at = datetime.now()
                    session.error_message = "Orchestrator shutdown"
        
        await self.chrome_manager.stop()
        logger.info("Healing orchestrator stopped")
    
    async def initiate_healing(self, failure_context: FailureContext) -> HealingSession:
        """Initiate a new healing session for a failed test.
        
        Args:
            failure_context: Context information about the test failure
            
        Returns:
            HealingSession object tracking the healing process
        """
        if not self.config.enabled:
            raise RuntimeError("Self-healing is disabled in configuration")
        
        session_id = str(uuid.uuid4())
        session = HealingSession(
            session_id=session_id,
            failure_context=failure_context,
            status=HealingStatus.PENDING,
            started_at=datetime.now()
        )
        
        async with self.session_lock:
            self.active_sessions[session_id] = session
        
        # Get contextual logger
        healing_logger = get_healing_logger("orchestrator", session_id, failure_context.test_case)
        healing_logger.log_operation_start("healing_session", 
                                          test_case=failure_context.test_case,
                                          original_locator=failure_context.original_locator,
                                          failure_type=failure_context.failure_type.value)
        
        # Record metrics
        self.metrics_collector.record_healing_session_start(
            session_id, failure_context.test_case, failure_context.failure_type
        )
        
        # Log to audit trail
        self.audit_trail.log_healing_session_started(session)
        
        logger.info(f"Initiated healing session {session_id} for test {failure_context.test_case}")
        
        # Start healing workflow asynchronously
        asyncio.create_task(self._execute_healing_workflow(session))
        
        return session
    
    async def get_session_status(self, session_id: str) -> Optional[HealingSession]:
        """Get the current status of a healing session.
        
        Args:
            session_id: ID of the healing session
            
        Returns:
            HealingSession object or None if not found
        """
        async with self.session_lock:
            return self.active_sessions.get(session_id)
    
    async def cancel_session(self, session_id: str) -> bool:
        """Cancel an active healing session.
        
        Args:
            session_id: ID of the healing session to cancel
            
        Returns:
            True if session was cancelled, False if not found or already completed
        """
        async with self.session_lock:
            session = self.active_sessions.get(session_id)
            if session and session.status == HealingStatus.IN_PROGRESS:
                session.status = HealingStatus.FAILED
                session.completed_at = datetime.now()
                session.error_message = "Cancelled by user"
                logger.info(f"Cancelled healing session {session_id}")
                return True
        
        return False
    
    def register_progress_callback(self, session_id: str, callback: Callable[[str, Dict[str, Any]], None]):
        """Register a callback for progress updates.
        
        Args:
            session_id: ID of the healing session
            callback: Function to call with progress updates
        """
        if session_id not in self.progress_callbacks:
            self.progress_callbacks[session_id] = []
        self.progress_callbacks[session_id].append(callback)
    
    def _notify_progress(self, session_id: str, progress_data: Dict[str, Any]):
        """Notify registered callbacks about progress updates."""
        # Update session progress and phase
        session = self.active_sessions.get(session_id)
        if session:
            if "progress" in progress_data:
                session.progress = progress_data["progress"]
            if "phase" in progress_data:
                session.current_phase = progress_data["phase"]
        
        # Notify callbacks
        callbacks = self.progress_callbacks.get(session_id, [])
        for callback in callbacks:
            try:
                callback(session_id, progress_data)
            except Exception as e:
                logger.warning(f"Progress callback failed for session {session_id}: {e}")
    
    async def _execute_healing_workflow(self, session: HealingSession):
        """Execute the complete healing workflow for a session.
        
        Args:
            session: HealingSession to process
        """
        start_time = time.time()
        healing_logger = get_healing_logger("orchestrator", session.session_id, session.failure_context.test_case)
        
        try:
            async with self.session_lock:
                session.status = HealingStatus.IN_PROGRESS
            
            healing_logger.log_progress("healing_workflow", 0.0, "Initiating healing workflow")
            
            self._notify_progress(session.session_id, {
                "phase": "starting",
                "message": "Initiating healing workflow",
                "progress": 0.0
            })
            
            # Phase 1: Failure Analysis
            analysis_start = time.time()
            healing_logger.log_progress("healing_workflow", 0.1, "Starting failure analysis")
            
            analysis_result = await self._analyze_failure(session)
            analysis_time = time.time() - analysis_start
            
            # Record analysis phase metrics
            self.metrics_collector.record_healing_session_phase(
                session.session_id, "analysis", analysis_time, analysis_result.get("is_healable", False)
            )
            
            if not analysis_result.get("is_healable", False):
                healing_logger.log_operation_failure("failure_analysis", analysis_time, 
                                                   "Failure is not healable", "NOT_HEALABLE")
                await self._complete_session_with_failure(
                    session, 
                    "Failure is not healable",
                    {"analysis_time": analysis_time}
                )
                return
            
            self._notify_progress(session.session_id, {
                "phase": "analysis_complete",
                "message": "Failure analysis completed",
                "progress": 0.2,
                "analysis_result": analysis_result
            })
            
            # Phase 2: Locator Generation
            generation_start = time.time()
            locator_candidates = await self._generate_alternative_locators(session, analysis_result)
            generation_time = time.time() - generation_start
            
            if not locator_candidates:
                await self._complete_session_with_failure(
                    session,
                    "No alternative locators could be generated",
                    {"analysis_time": analysis_time, "generation_time": generation_time}
                )
                return
            
            self._notify_progress(session.session_id, {
                "phase": "generation_complete",
                "message": f"Generated {len(locator_candidates)} alternative locators",
                "progress": 0.5,
                "candidates": locator_candidates
            })
            
            # Phase 3: Validation
            validation_start = time.time()
            validation_results = await self._validate_locators(session, locator_candidates)
            validation_time = time.time() - validation_start
            
            # Phase 4: Selection and Update
            best_locator = self._select_best_locator(validation_results)
            
            if not best_locator:
                await self._complete_session_with_failure(
                    session,
                    "No valid alternative locators found",
                    {
                        "analysis_time": analysis_time,
                        "generation_time": generation_time,
                        "validation_time": validation_time
                    }
                )
                return
            
            self._notify_progress(session.session_id, {
                "phase": "validation_complete",
                "message": f"Selected best locator: {best_locator['locator']}",
                "progress": 0.8,
                "best_locator": best_locator
            })
            
            # Phase 5: Test Code Update
            update_start = time.time()
            healing_logger.log_progress("healing_workflow", 0.9, "Updating test code")
            
            update_success = await self._update_test_code(session, best_locator)
            update_time = time.time() - update_start
            
            # Record update phase metrics
            self.metrics_collector.record_healing_session_phase(
                session.session_id, "update", update_time, update_success
            )
            
            total_time = time.time() - start_time
            
            if update_success:
                healing_logger.log_operation_success("healing_workflow", total_time,
                                                   healed_locator=best_locator.get("locator"),
                                                   confidence=best_locator.get("confidence_score"))
                await self._complete_session_with_success(
                    session,
                    best_locator,
                    {
                        "analysis_time": analysis_time,
                        "generation_time": generation_time,
                        "validation_time": validation_time,
                        "update_time": update_time,
                        "total_time": total_time
                    }
                )
            else:
                healing_logger.log_operation_failure("healing_workflow", total_time,
                                                   "Failed to update test code", "UPDATE_FAILED")
                await self._complete_session_with_failure(
                    session,
                    "Failed to update test code",
                    {
                        "analysis_time": analysis_time,
                        "generation_time": generation_time,
                        "validation_time": validation_time,
                        "update_time": update_time,
                        "total_time": total_time
                    }
                )
            
        except asyncio.CancelledError:
            logger.info(f"Healing workflow cancelled for session {session.session_id}")
            async with self.session_lock:
                session.status = HealingStatus.FAILED
                session.completed_at = datetime.now()
                session.error_message = "Workflow cancelled"
        
        except Exception as e:
            logger.error(f"Healing workflow failed for session {session.session_id}: {e}")
            await self._complete_session_with_failure(
                session,
                f"Workflow error: {str(e)}",
                {"total_time": time.time() - start_time}
            )
        
        finally:
            # Cleanup progress callbacks
            self.progress_callbacks.pop(session.session_id, None)
    
    async def _analyze_failure(self, session: HealingSession) -> Dict[str, Any]:
        """Analyze the failure to determine healing feasibility.
        
        Args:
            session: HealingSession containing failure context
            
        Returns:
            Dictionary with analysis results
        """
        failure_context = asdict(session.failure_context)
        
        # Create failure analysis agent and task
        analysis_agent = self.agents.failure_analysis_agent()
        analysis_task = self.tasks.analyze_failure_task(analysis_agent, failure_context)
        
        # Execute analysis with retry logic
        for attempt in range(self.max_retries):
            try:
                crew = Crew(
                    agents=[analysis_agent],
                    tasks=[analysis_task],
                    verbose=False
                )
                
                result = crew.kickoff()
                
                # Parse JSON response
                if hasattr(result, 'raw'):
                    result_text = result.raw
                else:
                    result_text = str(result)
                
                analysis_result = json.loads(result_text)
                
                logger.info(f"Failure analysis completed for session {session.session_id}")
                return analysis_result
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse analysis result (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    # Fallback to basic analysis
                    return self._fallback_failure_analysis(session.failure_context)
            
            except Exception as e:
                logger.error(f"Analysis failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    return self._fallback_failure_analysis(session.failure_context)
        
        return self._fallback_failure_analysis(session.failure_context)
    
    def _fallback_failure_analysis(self, failure_context: FailureContext) -> Dict[str, Any]:
        """Fallback failure analysis using rule-based logic."""
        is_healable, failure_type = self.failure_detection.is_locator_failure(
            failure_context.exception_message
        )
        
        return {
            "is_healable": is_healable,
            "failure_type": failure_type.value,
            "confidence": 0.6,
            "element_type": "unknown",
            "action_intent": "unknown",
            "locator_strategy": "unknown",
            "failure_reason": failure_context.exception_message,
            "element_context": f"Element from test step: {failure_context.failing_step}",
            "healing_priority": "medium",
            "recommendations": ["Use AI-based locator generation", "Validate against live session"]
        }
    
    async def _generate_alternative_locators(self, session: HealingSession, analysis_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate alternative locators using vision-first approach with comprehensive fallbacks.
        
        This implements the vision-first architecture:
        1. PRIMARY: Vision-based locator generation (BrowserUse with 4 fallback strategies)
        2. SECONDARY: Structural similarity fallback (Similo algorithm)
        3. TERTIARY: AI-based generation (CrewAI agents)
        4. FINAL: Rule-based fallback
        
        Args:
            session: HealingSession containing failure context
            analysis_result: Results from failure analysis
            
        Returns:
            List of alternative locator candidates with confidence scores
        """
        healing_logger = get_healing_logger("orchestrator", session.session_id, session.failure_context.test_case)
        all_candidates = []
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1: VISION-FIRST APPROACH (Primary - 93% success rate)
        # ═══════════════════════════════════════════════════════════════════
        healing_logger.log_progress("locator_generation", 0.1, "🎯 Trying vision-based healing (Primary)")
        
        vision_result = await self._try_vision_healing(session)
        
        if vision_result and vision_result.get("success"):
            validated_locators = vision_result.get("validated_locators", [])
            fallback_used = vision_result.get("fallback_strategy", "primary_coordinates")
            
            healing_logger.log_operation_success(
                "vision_healing",
                vision_result.get("execution_time", 0),
                f"✅ Vision succeeded with {fallback_used} strategy",
                validated_count=len(validated_locators)
            )
            
            # Convert BrowserUse format to our internal format
            for idx, loc_data in enumerate(validated_locators):
                all_candidates.append({
                    "locator": loc_data.get("locator"),
                    "strategy": loc_data.get("type", "unknown"),
                    "confidence": loc_data.get("confidence", 0.8),
                    "reasoning": f"Vision-based locator (F12-validated, {fallback_used})",
                    "stability_score": 1.0 if loc_data.get("unique") else 0.7,
                    "fallback_level": "vision_primary",
                    "unique": loc_data.get("unique", False),
                    "validation_count": loc_data.get("count", 1),
                    "method": f"vision_{fallback_used}"
                })
            
            # If we have good candidates from vision, return them
            if len(all_candidates) >= 3:
                healing_logger.log_progress(
                    "locator_generation", 
                    1.0, 
                    f"✅ Vision generated {len(all_candidates)} validated locators"
                )
                return all_candidates[:self.config.max_alternatives]
        
        else:
            reason = vision_result.get("reason", "unknown") if vision_result else "service_unavailable"
            healing_logger.log_operation_failure(
                "vision_healing",
                vision_result.get("execution_time", 0) if vision_result else 0,
                f"⚠️ Vision failed: {reason}",
                attempted_strategies=vision_result.get("attempted_strategies", []) if vision_result else []
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 2: STRUCTURAL FALLBACK (Secondary - +4% success rate)
        # ═══════════════════════════════════════════════════════════════════
        healing_logger.log_progress("locator_generation", 0.4, "🔄 Trying structural similarity fallback (Secondary)")
        
        structural_result = await self._try_structural_fallback(session)
        
        if structural_result and structural_result.get("success"):
            matches = structural_result.get("matches", [])
            
            healing_logger.log_operation_success(
                "structural_fallback",
                structural_result.get("execution_time", 0),
                f"✅ Structural fallback found {len(matches)} similar elements",
                top_similarity=structural_result.get("top_similarity_score", 0)
            )
            
            # Add structural matches to candidates
            for match in matches:
                for locator_data in match.get("generated_locators", []):
                    all_candidates.append({
                        "locator": locator_data.get("locator"),
                        "strategy": locator_data.get("type", "unknown"),
                        "confidence": match.get("similarity_score", 0.7) * locator_data.get("stability", 0.8),
                        "reasoning": f"Structural similarity match (Similo score: {match.get('similarity_score', 0):.2f})",
                        "stability_score": locator_data.get("stability", 0.8),
                        "fallback_level": "structural_similarity",
                        "similarity_score": match.get("similarity_score", 0),
                        "method": "structural_similo"
                    })
            
            # If we have candidates now, return them
            if len(all_candidates) >= 2:
                healing_logger.log_progress(
                    "locator_generation",
                    1.0,
                    f"✅ Structural fallback generated {len(all_candidates)} candidates"
                )
                return all_candidates[:self.config.max_alternatives]
        
        else:
            reason = structural_result.get("reason", "unknown") if structural_result else "no_dom_available"
            healing_logger.log_operation_failure(
                "structural_fallback",
                structural_result.get("execution_time", 0) if structural_result else 0,
                f"⚠️ Structural fallback failed: {reason}",
                {}
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 3: AI-BASED GENERATION (Tertiary - CrewAI agents)
        # ═══════════════════════════════════════════════════════════════════
        healing_logger.log_progress("locator_generation", 0.7, "🤖 Trying AI-based generation (Tertiary)")
        
        ai_candidates = await self._try_ai_generation(session, analysis_result)
        
        if ai_candidates:
            healing_logger.log_operation_success(
                "ai_generation",
                0,
                f"✅ AI generated {len(ai_candidates)} candidates"
            )
            all_candidates.extend(ai_candidates)
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: RULE-BASED FALLBACK (Final - Last resort)
        # ═══════════════════════════════════════════════════════════════════
        if len(all_candidates) < 2:
            healing_logger.log_progress("locator_generation", 0.9, "⚙️ Using rule-based fallback (Final)")
            rule_based = self._fallback_locator_generation(session.failure_context, analysis_result)
            all_candidates.extend(rule_based)
        
        # Remove duplicates and rank by confidence
        unique_candidates = self._deduplicate_and_rank_candidates(all_candidates)
        
        healing_logger.log_progress(
            "locator_generation",
            1.0,
            f"✅ Generated {len(unique_candidates)} total candidates from all strategies"
        )
        
        return unique_candidates[:self.config.max_alternatives]
    
    async def _try_vision_healing(self, session: HealingSession) -> Optional[Dict[str, Any]]:
        """Try vision-based healing using BrowserUse service.
        
        Returns:
            Dict with success status, validated locators, and metadata
        """
        start_time = time.time()
        
        try:
            # Prepare vision task description
            element_description = self._create_element_description(session.failure_context)
            target_url = session.failure_context.target_url
            
            if not target_url:
                return {
                    "success": False,
                    "reason": "no_target_url",
                    "execution_time": time.time() - start_time
                }
            
            # Call BrowserUse service
            response = requests.post(
                f"{self.browser_use_url}/submit",
                json={
                    "browser_use_objective": f"Navigate to {target_url} and find element: {element_description}. Use vision AI to locate the element and extract all possible locators."
                },
                timeout=60  # 60 second timeout
            )
            
            if response.status_code not in [200, 202]:  # Accept both 200 and 202
                return {
                    "success": False,
                    "reason": f"service_error_{response.status_code}",
                    "execution_time": time.time() - start_time
                }
            
            data = response.json()
            task_id = data.get("task_id")
            
            # Poll for results
            max_polls = 60  # 60 polls = 5 minutes max
            poll_interval = 5  # 5 seconds between polls
            
            for _ in range(max_polls):
                await asyncio.sleep(poll_interval)
                
                status_response = requests.get(f"{self.browser_use_url}/query/{task_id}")
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    
                    if status_data.get("status") == "completed":
                        result = status_data.get("result", {})
                        locator_data = result.get("locator_data")
                        
                        if locator_data and locator_data.get("success"):
                            return {
                                "success": True,
                                "validated_locators": locator_data.get("all_locators", []),
                                "best_locator": locator_data.get("best_locator"),
                                "fallback_strategy": locator_data.get("fallback_strategy", "primary_coordinates"),
                                "attempted_strategies": locator_data.get("attempted_strategies", []),
                                "validation_summary": locator_data.get("validation_summary", {}),
                                "execution_time": time.time() - start_time
                            }
                        else:
                            return {
                                "success": False,
                                "reason": "no_valid_locators",
                                "attempted_strategies": locator_data.get("attempted_strategies", []) if locator_data else [],
                                "execution_time": time.time() - start_time
                            }
                    
                    elif status_data.get("status") == "failed":
                        return {
                            "success": False,
                            "reason": status_data.get("error", "unknown_error"),
                            "execution_time": time.time() - start_time
                        }
            
            # Timeout
            return {
                "success": False,
                "reason": "timeout",
                "execution_time": time.time() - start_time
            }
        
        except requests.exceptions.ConnectionError:
            logger.warning("BrowserUse service not available, skipping vision healing")
            return {
                "success": False,
                "reason": "service_unavailable",
                "execution_time": time.time() - start_time
            }
        
        except Exception as e:
            logger.error(f"Vision healing error: {e}")
            return {
                "success": False,
                "reason": f"exception_{type(e).__name__}",
                "error": str(e),
                "execution_time": time.time() - start_time
            }
    
    async def _try_structural_fallback(self, session: HealingSession) -> Optional[Dict[str, Any]]:
        """Try structural similarity fallback using Similo algorithm.
        
        Returns:
            Dict with success status, matched elements, and generated locators
        """
        start_time = time.time()
        
        try:
            failure_context = session.failure_context
            
            # Need both old and current DOM for structural matching
            if not failure_context.original_locator:
                return {
                    "success": False,
                    "reason": "no_original_locator",
                    "execution_time": time.time() - start_time
                }
            
            # Get current DOM from target URL
            current_dom = await self._get_current_dom(failure_context.target_url)
            if not current_dom:
                return {
                    "success": False,
                    "reason": "cannot_fetch_current_dom",
                    "execution_time": time.time() - start_time
                }
            
            # Use structural fallback system
            result = await self.structural_fallback.find_similar_element(
                old_locator=failure_context.original_locator,
                old_dom=failure_context.page_source or "",  # Original DOM if available
                current_dom=current_dom,
                threshold=0.65  # 65% similarity threshold
            )
            
            if result.success and result.matches:
                return {
                    "success": True,
                    "matches": [
                        {
                            "element": match.element_html,
                            "similarity_score": match.similarity_score,
                            "generated_locators": match.generated_locators,
                            "properties": match.properties
                        }
                        for match in result.matches[:3]  # Top 3 matches
                    ],
                    "top_similarity_score": result.matches[0].similarity_score,
                    "execution_time": time.time() - start_time
                }
            else:
                return {
                    "success": False,
                    "reason": result.failure_reason or "no_similar_elements",
                    "execution_time": time.time() - start_time
                }
        
        except Exception as e:
            logger.error(f"Structural fallback error: {e}")
            return {
                "success": False,
                "reason": f"exception_{type(e).__name__}",
                "error": str(e),
                "execution_time": time.time() - start_time
            }
    
    async def _try_ai_generation(self, session: HealingSession, analysis_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Try AI-based locator generation using CrewAI agents.
        
        Returns:
            List of locator candidates from AI
        """
        # Get DOM context if available
        dom_context = ""
        if session.failure_context.target_url:
            try:
                dom_context = await self._get_dom_context(session.failure_context.target_url)
            except Exception as e:
                logger.warning(f"Failed to get DOM context: {e}")
        
        # Create locator generation agent and task
        generation_agent = self.agents.locator_generation_agent()
        generation_task = self.tasks.generate_alternative_locators_task(
            generation_agent, analysis_result, dom_context
        )
        
        # Execute generation with retry logic
        for attempt in range(self.max_retries):
            try:
                crew = Crew(
                    agents=[generation_agent],
                    tasks=[generation_task],
                    verbose=False
                )
                
                result = crew.kickoff()
                
                # Parse JSON response
                if hasattr(result, 'raw'):
                    result_text = result.raw
                else:
                    result_text = str(result)
                
                generation_result = json.loads(result_text)
                alternatives = generation_result.get("alternatives", [])
                
                logger.info(f"AI generated {len(alternatives)} alternative locators")
                return alternatives
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse AI generation result (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    return []
            
            except Exception as e:
                logger.error(f"AI generation failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    return []
        
        return []
    
    def _create_element_description(self, failure_context: FailureContext) -> str:
        """Create a detailed element description for vision AI.
        
        Args:
            failure_context: Context about the failure
            
        Returns:
            Human-readable description for vision AI
        """
        # Extract element type and attributes from locator
        locator = failure_context.original_locator
        action = failure_context.original_action or "interact with"
        
        description_parts = []
        
        # Add action context
        if "Click" in action:
            description_parts.append("clickable element")
        elif "Input" in action:
            description_parts.append("input field")
        elif "Select" in action:
            description_parts.append("dropdown or select element")
        else:
            description_parts.append("element")
        
        # Parse locator for hints
        if "login" in locator.lower():
            description_parts.append("related to login")
        elif "search" in locator.lower():
            description_parts.append("search functionality")
        elif "submit" in locator.lower() or "button" in locator.lower():
            description_parts.append("button")
        
        # Add original locator as hint
        description_parts.append(f"(originally: {locator})")
        
        return " ".join(description_parts)
    
    async def _get_current_dom(self, url: str) -> Optional[str]:
        """Get current DOM from URL using Chrome session.
        
        Args:
            url: Target URL
            
        Returns:
            HTML source or None
        """
        try:
            async with self.chrome_manager.session_context(url) as session:
                page_source = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: session.driver.page_source
                )
                return page_source
        except Exception as e:
            logger.warning(f"Failed to get current DOM from {url}: {e}")
            return None
    
    def _deduplicate_and_rank_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate locators and rank by confidence.
        
        Args:
            candidates: List of locator candidates
            
        Returns:
            Deduplicated and ranked list
        """
        seen_locators = set()
        unique_candidates = []
        
        # Sort by confidence first
        sorted_candidates = sorted(
            candidates,
            key=lambda x: (x.get("confidence", 0), x.get("stability_score", 0)),
            reverse=True
        )
        
        for candidate in sorted_candidates:
            locator = candidate.get("locator")
            if locator and locator not in seen_locators:
                seen_locators.add(locator)
                unique_candidates.append(candidate)
        
        return unique_candidates
    
    def _fallback_locator_generation(self, failure_context: FailureContext, analysis_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fallback locator generation using rule-based logic."""
        original_locator = failure_context.original_locator
        alternatives = []
        
        # Generate basic alternatives based on common patterns
        if "id=" in original_locator:
            element_id = original_locator.replace("id=", "")
            alternatives.extend([
                {
                    "locator": f"css=#{element_id}",
                    "strategy": "css",
                    "confidence": 0.8,
                    "reasoning": "CSS selector equivalent of ID",
                    "stability_score": 0.9,
                    "fallback_level": "primary"
                },
                {
                    "locator": f"xpath=//*[@id='{element_id}']",
                    "strategy": "xpath",
                    "confidence": 0.7,
                    "reasoning": "XPath equivalent of ID",
                    "stability_score": 0.8,
                    "fallback_level": "secondary"
                }
            ])
        
        elif "css=" in original_locator:
            css_selector = original_locator.replace("css=", "")
            # Try to convert CSS to XPath
            if css_selector.startswith("#"):
                element_id = css_selector[1:]
                alternatives.append({
                    "locator": f"id={element_id}",
                    "strategy": "id",
                    "confidence": 0.9,
                    "reasoning": "ID equivalent of CSS selector",
                    "stability_score": 0.95,
                    "fallback_level": "primary"
                })
        
        elif "xpath=" in original_locator:
            # Generate CSS alternatives for XPath
            xpath = original_locator.replace("xpath=", "")
            if "@id=" in xpath:
                # Extract ID from XPath
                import re
                id_match = re.search(r"@id=['\"]([^'\"]+)['\"]", xpath)
                if id_match:
                    element_id = id_match.group(1)
                    alternatives.extend([
                        {
                            "locator": f"id={element_id}",
                            "strategy": "id",
                            "confidence": 0.9,
                            "reasoning": "ID extracted from XPath",
                            "stability_score": 0.95,
                            "fallback_level": "primary"
                        },
                        {
                            "locator": f"css=#{element_id}",
                            "strategy": "css",
                            "confidence": 0.8,
                            "reasoning": "CSS selector from XPath ID",
                            "stability_score": 0.9,
                            "fallback_level": "secondary"
                        }
                    ])
        
        # Add generic alternatives if none generated
        if not alternatives:
            alternatives = [
                {
                    "locator": f"css=*[contains(text(), 'submit')]",
                    "strategy": "css",
                    "confidence": 0.5,
                    "reasoning": "Generic text-based CSS selector",
                    "stability_score": 0.4,
                    "fallback_level": "tertiary"
                },
                {
                    "locator": f"xpath=//*[contains(text(), 'submit')]",
                    "strategy": "xpath",
                    "confidence": 0.5,
                    "reasoning": "Generic text-based XPath",
                    "stability_score": 0.4,
                    "fallback_level": "tertiary"
                }
            ]
        
        return alternatives[:self.config.max_alternatives]
    
    async def _get_dom_context(self, url: str) -> str:
        """Get DOM context for locator generation."""
        try:
            async with self.chrome_manager.session_context(url) as session:
                # Get page source for DOM analysis
                page_source = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: session.driver.page_source
                )
                
                # Return truncated page source (first 5000 chars for context)
                return page_source[:5000] if page_source else ""
        except Exception as e:
            logger.warning(f"Failed to get DOM context from {url}: {e}")
            return ""
    
    async def _validate_locators(self, session: HealingSession, locator_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate alternative locators against live browser session.
        
        Args:
            session: HealingSession containing failure context
            locator_candidates: List of locator candidates to validate
            
        Returns:
            List of validation results
        """
        if not session.failure_context.target_url:
            logger.warning(f"No target URL for session {session.session_id}, skipping validation")
            return []
        
        validation_results = []
        
        try:
            async with self.chrome_manager.session_context(session.failure_context.target_url) as chrome_session:
                session.chrome_session_id = chrome_session.session_id
                
                for candidate in locator_candidates:
                    try:
                        locator = candidate["locator"]
                        strategy_str = candidate["strategy"]
                        
                        # Convert strategy string to enum
                        try:
                            strategy = LocatorStrategy(strategy_str)
                        except ValueError:
                            logger.warning(f"Unknown strategy {strategy_str}, skipping")
                            continue
                        
                        # Validate locator
                        validation_result = await self.chrome_manager.validate_locator(
                            chrome_session, locator, strategy
                        )
                        
                        # Create attempt record
                        attempt = LocatorAttempt(
                            locator=locator,
                            strategy=strategy,
                            success=validation_result.is_valid,
                            error_message=validation_result.error_message,
                            confidence_score=validation_result.confidence_score,
                            execution_time=0.0,  # Will be updated by validation
                            timestamp=datetime.now()
                        )
                        
                        session.attempts.append(attempt)
                        
                        # Add to validation results
                        validation_results.append({
                            "locator": locator,
                            "strategy": strategy_str,
                            "is_valid": validation_result.is_valid,
                            "element_found": validation_result.element_found,
                            "is_interactable": validation_result.is_interactable,
                            "matches_expected_type": validation_result.matches_expected_type,
                            "confidence_score": validation_result.confidence_score,
                            "error_message": validation_result.error_message,
                            "element_properties": validation_result.element_properties,
                            "original_candidate": candidate
                        })
                        
                        logger.debug(f"Validated locator {locator}: {validation_result.is_valid}")
                        
                    except Exception as e:
                        logger.error(f"Failed to validate locator {candidate.get('locator', 'unknown')}: {e}")
                        
                        # Record failed attempt
                        attempt = LocatorAttempt(
                            locator=candidate.get("locator", "unknown"),
                            strategy=LocatorStrategy.CSS,  # Default
                            success=False,
                            error_message=str(e),
                            confidence_score=0.0,
                            timestamp=datetime.now()
                        )
                        session.attempts.append(attempt)
        
        except Exception as e:
            logger.error(f"Failed to validate locators for session {session.session_id}: {e}")
        
        return validation_results
    
    def _select_best_locator(self, validation_results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Select the best locator from validation results.
        
        Args:
            validation_results: List of validation results
            
        Returns:
            Best locator candidate or None if no valid locators found
        """
        valid_locators = [
            result for result in validation_results 
            if result["is_valid"] and result["confidence_score"] >= self.config.confidence_threshold
        ]
        
        if not valid_locators:
            logger.warning("No valid locators found meeting confidence threshold")
            return None
        
        # Sort by confidence score and stability
        def locator_score(result):
            confidence = result["confidence_score"]
            stability = result["original_candidate"].get("stability_score", 0.5)
            strategy_bonus = self._get_strategy_bonus(result["strategy"])
            return confidence * 0.5 + stability * 0.3 + strategy_bonus * 0.2
        
        best_locator = max(valid_locators, key=locator_score)
        
        logger.info(f"Selected best locator: {best_locator['locator']} "
                   f"(confidence: {best_locator['confidence_score']:.2f})")
        
        return best_locator
    
    def _get_strategy_bonus(self, strategy: str) -> float:
        """Get stability bonus for locator strategy."""
        strategy_scores = {
            "id": 1.0,
            "name": 0.8,
            "css": 0.6,
            "xpath": 0.4,
            "link_text": 0.5,
            "partial_link_text": 0.3,
            "tag_name": 0.2,
            "class_name": 0.4
        }
        return strategy_scores.get(strategy, 0.0)
    
    async def _update_test_code(self, session: HealingSession, best_locator: Dict[str, Any]) -> bool:
        """Update the test code with the healed locator.
        
        Args:
            session: HealingSession containing failure context
            best_locator: Best locator candidate to use for replacement
            
        Returns:
            True if update was successful, False otherwise
        """
        try:
            failure_context = session.failure_context
            old_locator = failure_context.original_locator
            new_locator = best_locator["locator"]
            
            # Create backup and update test file
            update_result = self.code_updater.update_locator(
                failure_context.test_file,
                old_locator,
                new_locator,
                create_backup=True
            )
            
            if update_result.success:
                session.successful_locator = new_locator
                session.backup_file_path = update_result.backup_path
                
                logger.info(f"Successfully updated test file {failure_context.test_file}: "
                           f"{old_locator} -> {new_locator}")
                return True
            else:
                logger.error(f"Failed to update test file: {update_result.error_message}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating test code for session {session.session_id}: {e}")
            return False
    
    async def _complete_session_with_success(self, session: HealingSession, 
                                           best_locator: Dict[str, Any],
                                           performance_metrics: Dict[str, float]):
        """Complete a healing session with success."""
        async with self.session_lock:
            session.status = HealingStatus.SUCCESS
            session.completed_at = datetime.now()
            session.successful_locator = best_locator["locator"]
            session.confidence_score = best_locator.get("confidence_score", 0.0)
            session.progress = 1.0
            session.current_phase = "completed"
        
        # Update performance metrics
        self._update_performance_metrics(performance_metrics, success=True)
        
        # Record completion in metrics collector
        total_time = performance_metrics.get("total_time", 0.0)
        self.metrics_collector.record_healing_session_complete(
            session.session_id, HealingStatus.SUCCESS, total_time
        )
        
        # Log to audit trail
        self.audit_trail.log_healing_session_completed(session, total_time)
        
        # Record performance metrics for alerting
        await self.alerting_system.check_performance_metrics(total_time)
        
        self._notify_progress(session.session_id, {
            "phase": "completed",
            "message": "Healing completed successfully",
            "progress": 1.0,
            "success": True,
            "healed_locator": best_locator["locator"]
        })
        
        logger.info(f"Healing session {session.session_id} completed successfully")
    
    async def _complete_session_with_failure(self, session: HealingSession, 
                                           error_message: str,
                                           performance_metrics: Dict[str, float]):
        """Complete a healing session with failure."""
        async with self.session_lock:
            session.status = HealingStatus.FAILED
            session.completed_at = datetime.now()
            session.error_message = error_message
            session.progress = 1.0
            session.current_phase = "failed"
        
        # Update performance metrics
        self._update_performance_metrics(performance_metrics, success=False)
        
        # Record completion in metrics collector
        total_time = performance_metrics.get("total_time", 0.0)
        self.metrics_collector.record_healing_session_complete(
            session.session_id, HealingStatus.FAILED, total_time, error_message
        )
        
        # Log to audit trail
        self.audit_trail.log_healing_session_completed(session, total_time)
        
        # Check for repeated failures and trigger alerts
        await self.alerting_system.check_healing_failure(
            session.failure_context.test_case, 
            session.failure_context.failure_type,
            session.session_id
        )
        
        self._notify_progress(session.session_id, {
            "phase": "failed",
            "message": error_message,
            "progress": 1.0,
            "success": False
        })
        
        logger.warning(f"Healing session {session.session_id} failed: {error_message}")
    
    def _update_performance_metrics(self, metrics: Dict[str, float], success: bool):
        """Update performance tracking metrics."""
        for key, value in metrics.items():
            if key in self.performance_metrics:
                self.performance_metrics[key].append(value)
        
        # Track success rate
        self.performance_metrics["success_rate"].append(1.0 if success else 0.0)
        
        # Keep only last 100 measurements
        for key in self.performance_metrics:
            if len(self.performance_metrics[key]) > 100:
                self.performance_metrics[key] = self.performance_metrics[key][-100:]
    
    async def generate_healing_report(self, session_id: str) -> Optional[HealingReport]:
        """Generate a comprehensive healing report for a session.
        
        Args:
            session_id: ID of the healing session
            
        Returns:
            HealingReport object or None if session not found
        """
        async with self.session_lock:
            session = self.active_sessions.get(session_id)
        
        if not session:
            return None
        
        # Calculate performance metrics
        performance_metrics = {}
        if session.duration:
            performance_metrics["total_duration"] = session.duration
        
        performance_metrics.update({
            "attempts_count": len(session.attempts),
            "success_rate": session.success_rate,
            "confidence_score": max((a.confidence_score for a in session.attempts), default=0.0)
        })
        
        # Generate healing summary
        healing_summary = {
            "original_locator": session.failure_context.original_locator,
            "healed_locator": session.successful_locator,
            "failure_type": session.failure_context.failure_type.value,
            "healing_status": session.status.value,
            "attempts_made": len(session.attempts),
            "successful_attempts": sum(1 for a in session.attempts if a.success),
            "chrome_session_used": session.chrome_session_id is not None,
            "backup_created": session.backup_file_path is not None
        }
        
        # Generate recommendations
        recommendations = self._generate_recommendations(session)
        
        report = HealingReport(
            session=session,
            original_failure=session.failure_context,
            healing_summary=healing_summary,
            performance_metrics=performance_metrics,
            recommendations=recommendations,
            generated_at=datetime.now()
        )
        
        logger.info(f"Generated healing report for session {session_id}")
        return report
    
    def _generate_recommendations(self, session: HealingSession) -> List[str]:
        """Generate recommendations based on healing session results."""
        recommendations = []
        
        if session.status == HealingStatus.SUCCESS:
            recommendations.extend([
                "Consider updating test maintenance practices to prevent similar failures",
                "Review the stability of the original locator strategy",
                "Monitor the healed locator for future stability"
            ])
            
            # Strategy-specific recommendations
            if session.successful_locator:
                if "id=" in session.successful_locator:
                    recommendations.append("ID-based locators are generally more stable - consider using them consistently")
                elif "xpath=" in session.successful_locator:
                    recommendations.append("XPath locators can be fragile - consider CSS alternatives when possible")
        
        else:
            recommendations.extend([
                "Manual intervention may be required for this test failure",
                "Consider reviewing the application's element identification patterns",
                "Check if the target URL is accessible and the page loads correctly"
            ])
            
            if not session.attempts:
                recommendations.append("No healing attempts were made - check failure detection logic")
            elif all(not a.success for a in session.attempts):
                recommendations.append("All locator alternatives failed - the element may have been removed or significantly changed")
        
        # Performance-based recommendations
        if session.duration and session.duration > 60:  # More than 1 minute
            recommendations.append("Healing took longer than expected - consider optimizing locator generation strategies")
        
        return recommendations
    
    async def get_healing_statistics(self) -> Dict[str, Any]:
        """Get comprehensive healing statistics.
        
        Returns:
            Dictionary with healing statistics and performance metrics
        """
        async with self.session_lock:
            total_sessions = len(self.active_sessions)
            successful_sessions = sum(1 for s in self.active_sessions.values() 
                                    if s.status == HealingStatus.SUCCESS)
            failed_sessions = sum(1 for s in self.active_sessions.values() 
                                if s.status == HealingStatus.FAILED)
            in_progress_sessions = sum(1 for s in self.active_sessions.values() 
                                     if s.status == HealingStatus.IN_PROGRESS)
        
        # Calculate average metrics
        avg_metrics = {}
        for key, values in self.performance_metrics.items():
            if values:
                avg_metrics[f"avg_{key}"] = sum(values) / len(values)
                avg_metrics[f"max_{key}"] = max(values)
                avg_metrics[f"min_{key}"] = min(values)
        
        return {
            "session_counts": {
                "total": total_sessions,
                "successful": successful_sessions,
                "failed": failed_sessions,
                "in_progress": in_progress_sessions
            },
            "success_rate": successful_sessions / total_sessions if total_sessions > 0 else 0.0,
            "performance_metrics": avg_metrics,
            "chrome_session_stats": self.chrome_manager.get_session_stats(),
            "configuration": self.config.to_dict()
        }
    
    async def cleanup_completed_sessions(self, retention_hours: int = 24):
        """Clean up completed sessions older than retention period.
        
        Args:
            retention_hours: Hours to retain completed sessions
        """
        cutoff_time = datetime.now() - timedelta(hours=retention_hours)
        sessions_to_remove = []
        
        async with self.session_lock:
            for session_id, session in self.active_sessions.items():
                if (session.status in [HealingStatus.SUCCESS, HealingStatus.FAILED, HealingStatus.TIMEOUT] and
                    session.completed_at and session.completed_at < cutoff_time):
                    sessions_to_remove.append(session_id)
            
            for session_id in sessions_to_remove:
                del self.active_sessions[session_id]
        
        logger.info(f"Cleaned up {len(sessions_to_remove)} completed healing sessions")
        return len(sessions_to_remove)
    
    async def health_check(self) -> Dict[str, Any]:
        """Perform a health check of the healing orchestrator.
        
        Returns:
            Dictionary with health status information
        """
        try:
            # Check Chrome session manager health
            chrome_health = await self.chrome_manager.health_check()
            
            # Check active sessions
            async with self.session_lock:
                active_count = len(self.active_sessions)
                in_progress_count = sum(1 for s in self.active_sessions.values() 
                                      if s.status == HealingStatus.IN_PROGRESS)
            
            # Overall health assessment
            is_healthy = (
                chrome_health.get("status") == "healthy" and
                in_progress_count < self.config.max_concurrent_sessions
            )
            
            return {
                "status": "healthy" if is_healthy else "degraded",
                "orchestrator_enabled": self.config.enabled,
                "active_sessions": active_count,
                "in_progress_sessions": in_progress_count,
                "max_concurrent_sessions": self.config.max_concurrent_sessions,
                "chrome_manager_health": chrome_health,
                "performance_metrics": {
                    key: len(values) for key, values in self.performance_metrics.items()
                },
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }