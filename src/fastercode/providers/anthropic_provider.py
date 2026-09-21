import json
import os

from .base import LLMProvider

# Anthropic's structured-outputs schema - the direct analogue of
# openai_provider.py's _JSON_SCHEMA. `additionalProperties: False` and an
# explicit `required` are both mandatory here; the API rejects schemas
# without them.
_JSON_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "lines": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["lines"],
    },
}


class AnthropicProvider(LLMProvider):

    name = "anthropic"

    def __init__(self, model="claude-opus-5", max_tokens=16000):
        self.model = model
        # max_tokens is required by the Messages API, and on current models
        # it caps thinking *plus* response text together - thinking is on by
        # default, so a tight value truncates the refactored function
        # mid-body. 16000 is generous for a single function either way.
        self.max_tokens = max_tokens

    def set_credentials(self, key):
        # Same lazy-env-var contract as OpenAIProvider: call() reads this at
        # call time, so a later set_credentials() wins, and a pre-existing
        # ANTHROPIC_API_KEY keeps working for callers that never set
        # credentials explicitly.
        os.environ["ANTHROPIC_API_KEY"] = key

    def call(self, system, prompt):
        # `anthropic` is a declared dependency, so this import should always
        # succeed. It stays inside call() as a safety net rather than at
        # module scope because providers/__init__.py instantiates every
        # provider at import time - on a partial or vendored install, a
        # module-level import would take down `import fastercode` entirely
        # for users who only ever touch OpenAI or Kimi.
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "the 'anthropic' provider requires the anthropic package, "
                "which should have been installed with fastercode - your "
                "environment looks incomplete, so run `pip install anthropic`"
            ) from exc

        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "REPLACE_ME")
        )

        # No temperature/top_p/top_k: unlike OpenAI, current Claude models
        # reject them outright with a 400. Copying OpenAIProvider's
        # temperature=0.2 across is the one porting mistake that breaks
        # every call.
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": _JSON_SCHEMA},
        )

        # A safety refusal is a *successful* HTTP 200 carrying empty or
        # partial content, so this has to be checked before touching
        # response.content - otherwise it surfaces as an opaque IndexError
        # several lines later. core.optimise() records the raised error as
        # that try's failure and moves on to the next one.
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise RuntimeError(
                f"Anthropic declined this request (refusal category: "
                f"{category}) - no code was returned"
            )

        # output_config guarantees the first text block is valid JSON
        # matching _JSON_SCHEMA.
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise RuntimeError(
                f"Anthropic returned no text content "
                f"(stop_reason: {response.stop_reason})"
            )
        return json.loads(text)["lines"]
