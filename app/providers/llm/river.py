from app.providers.llm.openai import OpenAIClient


class RiverClient(OpenAIClient):
    vendor = "river"
    completion_budget_field = "max_tokens"
