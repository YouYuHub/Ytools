from dataclasses import dataclass
@dataclass
class FunctionDefinition:
    name: str
    description: str
    parameters: dict
