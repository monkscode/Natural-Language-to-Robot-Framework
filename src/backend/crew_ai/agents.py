import logging
from crewai import Agent

# NOTE: BatchBrowserUseTool is no longer wired into any agent — the
# element-identifier LLM agent was deleted in Task 16 and the tool is now
# called directly by the deterministic element stage
# (src/backend/crew_ai/element_identification.py).

# Import LLM factory function
from .cleaned_llm_wrapper import get_llm
from .tasks import AssemblyOutput, PlanOutput

logger = logging.getLogger(__name__)


class RobotAgents:
    def __init__(self, model_provider, model_name, library_context=None,
                 assembler_context=None):
        """
        Initialize Robot Framework agents.

        Args:
            model_provider: "local", "gemini", or "vertex"
            model_name: Model identifier
            library_context: LibraryContext instance (optional, for dynamic keyword knowledge)
            assembler_context: Optimized context for Code Assembler (optional).
                The planner has NO context slot by design — it runs on the
                static minimal planning context (see step_planner_agent);
                its learning hints arrive via the task description instead
                (RobotTasks hint_context).
        """
        # 24R Stage 3: the assembler's replies are provider-enforced to the
        # AssemblyOutput {"code"} schema through the same gated get_llm path
        # as the planner (Task 22). On providers without schema support
        # (ollama) get_llm silently drops the schema and the guardrail
        # salvage net stays the live output contract — never delete the net
        # while that path exists. The repair crew reuses code_assembler_agent,
        # so dryrun repairs inherit the same enforcement.
        self.llm = get_llm(model_provider, model_name,
                           response_format=AssemblyOutput)
        # Task 22: the planner's replies are provider-enforced to the PlanOutput
        # schema (pure JSON by construction on vertex/gemini; get_llm silently
        # drops the schema for providers without support, e.g. ollama).
        # response_format lives on the LLM instance in CrewAI, so the planner
        # needs its own wrapper — but crew.py treats agents.llm._monitor as the
        # authoritative cleaning/retry counter, so both instances share the ONE
        # monitor object to keep those stats complete.
        self.planner_llm = get_llm(model_provider, model_name,
                                   response_format=PlanOutput)
        self.planner_llm._monitor = self.llm._monitor
        # Usage accounting shares the same way: workflow metrics read token
        # usage ONCE, from the assembler crew's agent.llm (BaseLLM._token_usage,
        # a dict mutated in place and assigned only in __init__). Aliasing it
        # keeps the planner's calls/tokens/cost in that single read — without
        # this line they silently vanish from metrics and pricing.
        self.planner_llm._token_usage = self.llm._token_usage
        self.library_context = library_context

        # NOTE: the keyword-search tool was RETIRED (Task 24R Stage 1,
        # owner-approved 2026-07-11): 0 invocations across 15,987 traced
        # calls, unreachable by construction (the prompts forbid the ReAct
        # syntax CrewAI tool calls require). Retirement unlocks assembler
        # structured output (Stage 3). Do not re-attach without new evidence.

        # Role-specific optimized contexts. Assembler-only: crew.py used to
        # build a planner context every production run (vector search +
        # keyword-doc fetches) that NOTHING ever read — the planner ships
        # static minimal context by design (caught 2026-07-16).
        self.assembler_context = assembler_context

    def _get_agent_context(self, agent_type: str) -> str:
        """
        Unified context retrieval with consistent priority chain.

        Priority:
        1. Optimized context (from SmartKeywordProvider - pattern learning/zero-context+tool)
        2. Library context (static context from library_context)
        3. Empty string (graceful degradation)

        Args:
            agent_type: "assembler" (the planner has no context slot — it
                runs on static minimal context, see step_planner_agent)

        Returns:
            Context string with appropriate formatting
        """
        # Map agent type to optimized context attribute
        optimized_context_map = {
            "assembler": self.assembler_context,
        }

        # Map agent type to library context property
        library_context_map = {
            "assembler": "code_assembly_context",
        }
        
        optimized_context = optimized_context_map.get(agent_type)
        
        # Priority 1: Use optimized context if available
        if optimized_context:
            logger.info(f"🎯 {agent_type.capitalize()} using optimized context")
            return f"\n\n{optimized_context}"
        
        # Priority 2: Fall back to library context
        if self.library_context:
            library_property = library_context_map.get(agent_type)
            if library_property:
                static_context = getattr(self.library_context, library_property)
                logger.info(f"📚 {agent_type.capitalize()} using static library context (optimization not available)")
                return f"\n\n{static_context}"
        
        # Priority 3: Graceful degradation
        logger.warning(f"⚠️ {agent_type.capitalize()} has no context available - using minimal validation")
        return ""

    def step_planner_agent(self) -> Agent:
        # Step Planner needs MINIMAL context - just library name and planning rules
        # It doesn't need keyword details or implementation specifics - that's for the Code Assembler
        library_name = self.library_context.library_name if self.library_context else 'Robot Framework'
        
        # Get library-specific planning rules (timing behavior, capabilities)
        library_guidance = ""
        if self.library_context:
            library_guidance = f"""

**{library_name} PLANNING CONSIDERATIONS:**
{self.library_context.planning_rules}

**Remember:** Create HIGH-LEVEL steps. The Code Assembler handles implementation details.
"""

        return Agent(
            role="Test Automation Planner",
            goal=f"Break down a natural language query into a structured series of high-level test steps for Robot Framework using {library_name}. ONLY include elements and actions explicitly mentioned in the user's query.",
            # The detailed rulebook (explicit-elements-only, no popup steps,
            # etc.) lives ONCE, in the task description's
            # EXPLICIT_ELEMENTS_ONLY_RULES component — the backstory used to
            # duplicate it as an 8-rule list (~400 tokens, finding F9).
            backstory=(
                "You are an expert test automation planner with a strict focus on user "
                "requirements: you convert ONLY the actions explicitly mentioned in the "
                "user's query into structured, HIGH-LEVEL test steps — the Code Assembler "
                "handles keyword and implementation details."
                f"{library_guidance}"
            ),
            llm=self.planner_llm,
            verbose=True,
            allow_delegation=False,
        )

    # NOTE: element_identifier_agent was REMOVED (Task 16). Its entire job —
    # deciding which steps need locators, extracting the URL, building the one
    # batch tool call, and copying the locator contract onto steps — is now
    # deterministic Python in element_identification.py.

    def code_assembler_agent(self) -> Agent:
        # Get context via unified method with consistent priority chain
        library_knowledge = self._get_agent_context("assembler")

        return Agent(
            role="Robot Framework Code Generator",
            goal=f"Generate complete, executable Robot Framework code using {self.library_context.library_name if self.library_context else 'Robot Framework'} and return it as a JSON object with a single 'code' key.",
            # Single output contract. The old backstory demanded raw code
            # ("start IMMEDIATELY with *** Settings ***") while the task
            # description demanded {"code"} JSON — the model chose JSON 30/30
            # on the 2026-07-11 bench and the raw-code manifesto only fed the
            # salvage net (finding F3).
            backstory=(
                "You are a Robot Framework code generator for automated web tests. "
                "Your reply is parsed by a machine, not read by a human.\n\n"
                "**OUTPUT CONTRACT (the ONLY accepted format):**\n"
                "Reply with ONE JSON object: {\"code\": \"<complete .robot file content>\"}\n"
                "- \"code\" holds the entire Robot Framework file as a string, with \\n for newlines\n"
                "- No markdown fences, no explanations, no thinking text — just the JSON object"
                f"{library_knowledge}"
            ),
            llm=self.llm,
            verbose=True,
            # No in-crew delegation: the LLM validator agent was removed and replaced
            # by the deterministic robot --dryrun gate (dryrun_service.py). The repair
            # loop builds a fresh single-agent crew, so this agent never delegates.
            allow_delegation=False,
        )
