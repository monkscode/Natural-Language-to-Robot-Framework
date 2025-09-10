from crewai import Crew, Process
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks

class RobotCrew:
    def __init__(self, query, model_provider, model_name):
        self.query = query
        self.model_provider = model_provider
        self.model_name = model_name
        self.agents = RobotAgents(model_provider, model_name)
        self.tasks = RobotTasks()

    def run(self):
        # Define Agents
        step_planner_agent = self.agents.step_planner_agent()
        element_identifier_agent = self.agents.element_identifier_agent()
        code_assembler_agent = self.agents.code_assembler_agent()
        code_validator_agent = self.agents.code_validator_agent()

        # Define Tasks
        plan_steps = self.tasks.plan_steps_task(step_planner_agent, self.query)
        identify_elements = self.tasks.identify_elements_task(element_identifier_agent)
        assemble_code = self.tasks.assemble_code_task(code_assembler_agent)
        validate_code = self.tasks.validate_code_task(code_validator_agent)

        # Create and run the crew
        crew = Crew(
            agents=[step_planner_agent, element_identifier_agent, code_assembler_agent, code_validator_agent],
            tasks=[plan_steps, identify_elements, assemble_code, validate_code],
            process=Process.sequential,
            verbose=True,
        )

        result = crew.kickoff()
        return result, crew
