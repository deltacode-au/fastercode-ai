import json
import os
import re

from openai import OpenAI

from .base import LLMProvider

# Models that aren't held to a strict JSON mode routinely wrap their answer in
# a markdown fence, which json.loads() chokes on. Providers configured with
# json_mode=None rely on the prompt alone to ask for {"lines": [...]}, so this
# is their normal path rather than an edge case.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def _parse_lines(content):
    """Parse a {"lines": [...]} payload, tolerating a markdown code fence."""
    if content is None:
        return []
    text = content.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group(1).strip()
    return json.loads(text)["lines"]

_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "refactored_code",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "lines": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["lines"]
        },
        "strict": True
    }
}


class OpenAIProvider(LLMProvider):

    name = "openai"

    def __init__(self, model="gpt-5", base_url=None,
                 api_key_env="OPENAI_API_KEY", use_responses_api=True,
                 json_mode="json_object"):
        # These parameters exist so OpenAI-protocol-compatible backends (see
        # openai_compatible.py) can reuse this whole implementation by
        # subclassing. Every default reproduces today's OpenAI behavior
        # exactly, so OpenAIProvider() is unchanged: base_url=None is the
        # OpenAI SDK's own default (a no-op), the Responses API attempt stays
        # on, and JSON mode stays requested.
        #
        # json_mode=None omits response_format entirely, for backends that
        # don't support it (Gemini's compat shim) or where support depends on
        # which model gets routed to (OpenRouter). Safe because the prompt
        # templates themselves already end with "Return ONLY JSON matching
        # {"lines": [...]}" - response_format is belt-and-braces, not the
        # thing that makes the shape happen.
        self.model = model
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._use_responses_api = use_responses_api
        self._json_mode = json_mode

    def set_credentials(self, key):
        # Set the provider's API key env var (not stored as an instance
        # attribute directly) - call() reads it lazily at call time, so a
        # later set_credentials() always wins, and the env var remains the
        # documented fallback ("if omitted, falls back to whatever
        # OPENAI_API_KEY is already set") for direct/native usage that never
        # calls set_credentials() at all.
        os.environ[self._api_key_env] = key

    def call(self, system, prompt):
        client = OpenAI(
            api_key=os.environ.get(self._api_key_env, "REPLACE_ME"),
            base_url=self._base_url,
        )

        # --- Path A: Responses API with JSON schema (newer SDKs) ---
        # Skipped entirely for backends that only speak Chat Completions -
        # responses.create() is OpenAI-proprietary, so against those it would
        # fail on every single call and burn a wasted round-trip before
        # falling through to Path B.
        if not self._use_responses_api:
            return self._chat_completions_call(system, prompt, client)

        try:
            resp = client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": [{"type": "text", "text": system}]},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                response_format=_JSON_SCHEMA,
                temperature=0.2,
            )
            try:
                content = resp.output[0].content[0]
                if getattr(content, "parsed", None):
                    return content.parsed["lines"]
                return _parse_lines(resp.output_text)
            except Exception:
                text = getattr(resp, "output_text", "").rstrip("\n")
                return text.splitlines() if text else []
        except TypeError:
            pass
        except Exception as e:
            msg = str(e).lower()
            if "unsupported value" in msg and "temperature" in msg:
                try:
                    resp = client.responses.create(
                        model=self.model,
                        input=[
                            {"role": "system", "content": [{"type": "text", "text": system}]},
                            {"role": "user", "content": [{"type": "text", "text": prompt}]},
                        ],
                        response_format=_JSON_SCHEMA,
                    )
                    try:
                        content = resp.output[0].content[0]
                        if getattr(content, "parsed", None):
                            return content.parsed["lines"]
                        data = json.loads(resp.output_text)
                        return data["lines"]
                    except Exception:
                        text = getattr(resp, "output_text", "").rstrip("\n")
                        return text.splitlines() if text else []
                except Exception:
                    pass

        return self._chat_completions_call(system, prompt, client)

    def _chat_completions_call(self, system, prompt, client):
        # --- Path B: Chat Completions JSON mode (older SDKs / fallback) ---
        # Also the only path for Chat-Completions-only backends, which is why
        # this is a method rather than inline in call().
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": self._json_mode}
        try:
            chat = client.chat.completions.create(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "unsupported value" in msg and "temperature" in msg:
                raise
            kwargs["temperature"] = 0.2
            chat = client.chat.completions.create(**kwargs)

        return _parse_lines(chat.choices[0].message.content)
