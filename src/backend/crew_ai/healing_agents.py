"""Healing agents for the test self-healing system."""

import os
from crewai import Agent
from crewai.llm import LLM
# from langchain_community.llms import Ollama
from typing import Dict, Any
from langchain_ollama import OllamaLLM

def get_llm(model_provider: str, model_name: str):
    """Get LLM instance based on provider and model name."""
    if model_provider == "local":
        return OllamaLLM(model=model_name)
    else:
        return LLM(
            api_key=os.getenv("GEMINI_API_KEY"),
            model=f"{model_name}",
            num_retries=5,
        )


class HealingAgents:
    """Collection of specialized agents for test healing workflows."""
    
    def __init__(self, model_provider: str, model_name: str):
        self.llm = get_llm(model_provider, model_name)

    def failure_analysis_agent(self) -> Agent:
        """Agent specialized in analyzing test failures and determining healing feasibility."""
        return Agent(
            role="Test Failure Analysis Specialist",
            goal="Accurately analyze test failures to determine if they are healable locator issues and extract relevant context for healing.",
            backstory=(
                "You are an expert test failure analyst with deep knowledge of Selenium WebDriver exceptions "
                "and Robot Framework test execution patterns. Your expertise lies in quickly identifying "
                "locator-related failures that can be automatically healed versus other types of failures "
                "that require manual intervention. You have extensive experience with web element identification "
                "patterns and can extract precise context needed for successful healing attempts."
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def locator_generation_agent(self) -> Agent:
        """Enhanced agent for generating alternative locators with healing-specific capabilities."""
        return Agent(
            role="Advanced Web Element Locator Specialist",
            goal=(
                "Generate multiple robust alternative locators for failed web elements using comprehensive "
                "element fingerprinting (17+ properties) and intelligent priority-based locator strategies."
            ),
            backstory=(
                "You are an advanced web element locator specialist with expertise in healing broken test automation "
                "and implementing industry best practices from BrowserStack and academic research (Similo algorithm). "
                
                "**Core Expertise:**\n"
                "- Comprehensive element fingerprinting: Extract 17+ properties per element (id, name, type, aria-label, "
                "class, href, alt, src, role, visible_text, placeholder, value, location, dimensions, neighbor_texts, "
                "parent_tag, sibling_tags, is_button, is_clickable, is_input, and all attributes)\n"
                "- Multi-locator strategy: Generate 8 alternative locators per element following BrowserStack hierarchy: "
                "ID (priority 1, 95% stability) → Name (priority 2, 90%) → Aria-label CSS (priority 3, 92%) → "
                "Data-attribute CSS (priority 4, 88%) → Class CSS (priority 5, 70%) → Text-based XPath (priority 6, 85%) → "
                "Relative XPath (priority 7, 65%) → Absolute XPath (priority 8, 50%)\n"
                "- Property stability weighting: Prioritize stable properties (name: 2.90, aria-label: 2.95, visible_text: 2.95) "
                "over volatile ones (class: 1.00, xpath: 0.50)\n"
                "- Similo-based similarity matching: Can re-identify elements when original locators break by computing "
                "weighted similarity scores across all 17+ properties\n"
                
                "**Healing Strategy:**\n"
                "When a locator fails, you:\n"
                "1. Extract comprehensive properties from the target element (17+ attributes)\n"
                "2. Generate 8 alternative locators ranked by stability score\n"
                "3. Use similarity matching to find the element in the updated DOM if direct locators fail\n"
                "4. Return all alternatives to enable intelligent fallback cascade\n"
                
                "Your locators follow Robot Framework syntax and are designed for maximum resilience to UI changes. "
                "You excel at finding elements even when IDs change, classes are reorganized, or DOM structure shifts."
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

    def locator_validation_agent(self) -> Agent:
        """Agent specialized in validating locators against live browser sessions."""
        return Agent(
            role="Live Locator Validation Specialist", 
            goal="Test and validate alternative locators against live browser sessions to ensure they work correctly.",
            backstory=(
                "You are a meticulous locator validation specialist who ensures that generated alternative locators "
                "actually work in real browser environments. You have deep expertise in Selenium WebDriver operations, "
                "element interaction testing, and can assess whether a locator not only finds an element but also "
                "whether that element is the correct one and is interactable. You understand the nuances of element "
                "states, visibility, and interaction patterns. Your validation goes beyond simple element existence "
                "to ensure functional correctness and reliability."
            ),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )