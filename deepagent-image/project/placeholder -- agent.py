import os
from typing import Callable
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from deepagents.profiles import HarnessProfile, register_harness_profile
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver

## ------------------------------------------------##
#                        Tools                      #
## ------------------------------------------------##

## callable tool by all agents
#@tool
#def function(param_name: type) -> output_type:
#   """docstring describing the function"""
#   #PUT CODE HERE
#   return [OUTPUT]

## -------------------------------------------------##
#                  Creating The Agent                #
## -------------------------------------------------##

def _read_optional_text(path: Path) -> str:
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


class Agent:
    # look at create_deep_agent commands
    def __init__(self, model: str, agent_tag: str, workspace: Path, defined_path: Path, tools: list[Callable] = [], skills: list[str] = [], middleware = [], profile:HarnessProfile = HarnessProfile()):
        self.model = model+"#"+agent_tag
        self.workspace = workspace
        self.defined_path = defined_path
        self.tools = tools
        self.skills = skills
        self.middleware=middleware
        self.profile = profile
        self.agent = self.build_agent()
    def build_agent(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        backend = LocalShellBackend(
            root_dir=str(self.workspace),
            virtual_mode=True,
            inherit_env=True,
            env={"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin")},
        )

        agents_md = _read_optional_text(Path.cwd() / "AGENTS.md")
        system_prompt = BASE_SYSTEM_PROMPT
        if agents_md:
            system_prompt += "\nAdditional project instructions from AGENTS.md:\n" + agents_md


        register_harness_profile(self.model, self.profile)

        return create_deep_agent(
            model=self.model,
            tools=self.tools,
            skills = self.skills,
            backend=backend,
            system_prompt=system_prompt,
            middleware = self.middleware,
            profile = self.profile, 
            checkpointer=MemorySaver(),
        )