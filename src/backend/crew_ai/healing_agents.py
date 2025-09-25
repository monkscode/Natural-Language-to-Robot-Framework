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
            goal="Generate multiple robust alternative locators for failed web elements using DOM analysis and element fingerprinting.",
            backstory=(
                "You are an advanced web element locator specialist with expertise in healing broken test automation. "
                "Unlike basic locator generation, you specialize in analyzing failed locators and generating "
                "intelligent alternatives that are more resilient to UI changes. You understand element fingerprinting, "
                "DOM structure analysis, and can create diverse locator strategies that target the same element "
                "through different approaches. Your locators follow Robot Framework syntax and prioritize stability "
                "over brevity. You excel at finding elements even when their original identifiers have changed."
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