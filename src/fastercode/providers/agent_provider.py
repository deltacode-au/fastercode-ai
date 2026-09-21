from .base import LLMProvider


class AgentProvider(LLMProvider):
    """
    Stand-in provider for agent mode, where the caller supplies the code via
    optimise(propose=...) and no LLM is contacted at all.

    It exists purely so optimise()'s result - and the report built from it -
    names what actually produced the candidates. Without it the run inherits
    whatever provider llm.py happens to have active (OpenAI by default), and
    a report claims a model that was never called.

    Deliberately not registered in providers.PROVIDERS: it is not something a
    job.yaml's `api.provider` should ever be able to select, since it cannot
    produce code.
    """

    name = "agent"

    # No model concept - report.py only prints the model when it's truthy, so
    # an agent run reads "Provider: agent" with nothing implied after it.
    model = None

    def set_credentials(self, key):
        raise RuntimeError(
            "the agent provider takes no credentials - it never contacts an LLM"
        )

    def set_model(self, model):
        raise RuntimeError("the agent provider has no model to select")

    def call(self, system, prompt):
        # Reaching here means something tried to route a real refactor request
        # through agent mode, which would silently turn a "no LLM was called"
        # run into one that did. Fail loudly instead.
        raise RuntimeError(
            "agent mode makes no LLM calls - optimise() was expected to take "
            "its candidates from propose(), not from a provider"
        )
