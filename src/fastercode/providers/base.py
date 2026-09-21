from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """
    Everything llm.py needs from a specific LLM backend. Prompt content
    (system text/template) comes from a LanguageAdapter and is provider-
    agnostic - a provider only has to turn (system, prompt) into a list of
    code lines, however its own API/SDK gets there.
    """

    # Overridden by each concrete provider (e.g. "openai") - surfaced in
    # optimise()'s result/report.py so a report shows which provider/model
    # actually produced the refactors it's describing.
    name = "unknown"

    @abstractmethod
    def set_credentials(self, key):
        """Store/apply the given (already resolved) API key for this provider."""

    def set_model(self, model):
        """
        Selects which model this provider's call() should use. Concrete
        (not abstract) - storing the model on self.model is the right
        default for any provider that has a model concept; call()
        implementations read self.model (see OpenAIProvider). Override only
        if a provider needs unusual handling (e.g. validating/normalizing
        the string).
        """
        self.model = model

    @abstractmethod
    def call(self, system, prompt):
        """
        Send system+prompt to the LLM. Must ask the model to return code as
        {"lines": [list of strings]} and return that list back (list[str]).
        """
