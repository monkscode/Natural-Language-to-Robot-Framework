from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks

def run_crew(query: str, model_provider: str, model_name: str):
    """
    Initializes and runs the CrewAI crew.
    """
    agents = RobotAgents(model_provider, model_name)
    tasks = RobotTasks()

    # Define Agents
    step_planner_agent = agents.step_planner_agent()
    element_identifier_agent = agents.element_identifier_agent()
    code_assembler_agent = agents.code_assembler_agent()
    code_validator_agent = agents.code_validator_agent()

    # Define Tasks
    plan_steps = tasks.plan_steps_task(step_planner_agent, query)
    identify_elements = tasks.identify_elements_task(element_identifier_agent)
    assemble_code = tasks.assemble_code_task(code_assembler_agent)
    validate_code = tasks.validate_code_task(code_validator_agent)

    # Create and run the crew
    crew = Crew(
        agents=[step_planner_agent, element_identifier_agent, code_assembler_agent, code_validator_agent],
        tasks=[plan_steps, identify_elements, assemble_code, validate_code],
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    return result, crew
