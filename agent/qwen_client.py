"""
Qwen LLM client with three-layer resilience:
  1. Normal call to qwen3.6-flash
  2. Retry up to 3 times with exponential backoff
  3. Safe-mode rule-based fallback when API is unavailable
"""
import json
import logging
import re
import time

from openai import OpenAI, APIError, APITimeoutError, APIConnectionError

from memory import Incident

logger = logging.getLogger(__name__)

# Actions the rule-based fallback will consider "restart-safe" only
# if a past resolved incident used restart for the same symptom set.
_ALWAYS_ALERT = True


def _extract_json(text: str) -> dict:
    """Strip markdown fences and extract the first JSON object."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    # Find first {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return json.loads(text)


class QwenClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)
        self._model = model
        self.degraded = False  # True when in safe-mode fallback

    def diagnose(
        self,
        metrics: dict,
        symptoms: list[str],
        similar: list[Incident],
    ) -> dict:
        """
        Returns:
          {diagnosis: str, action: "alert|restart|halt",
           confidence: float, reasoning: str, safe_mode: bool}
        """
        for attempt in range(3):
            try:
                result = self._call_api(metrics, symptoms, similar)
                if self.degraded:
                    logger.info("Qwen API recovered, leaving safe mode")
                self.degraded = False
                result["safe_mode"] = False
                return result
            except (APIError, APITimeoutError, APIConnectionError, json.JSONDecodeError) as exc:
                wait = 2 ** attempt
                logger.warning("Qwen attempt %d/3 failed (%s), retry in %ds", attempt + 1, exc, wait)
                time.sleep(wait)
            except Exception as exc:
                logger.error("Unexpected Qwen error: %s", exc)
                break

        logger.error("Qwen API unavailable — entering safe mode")
        self.degraded = True
        result = self._rule_based_fallback(symptoms, similar)
        result["safe_mode"] = True
        return result

    def _call_api(self, metrics: dict, symptoms: list[str], similar: list[Incident]) -> dict:
        history_lines: list[str] = []
        for inc in similar[:3]:
            history_lines.append(
                f"  - symptoms={inc.symptoms}, action={inc.action}, "
                f"outcome={inc.outcome!r}, resolved={inc.resolved}"
            )
        history_block = (
            "Similar past incidents (most relevant first):\n" + "\n".join(history_lines)
            if history_lines
            else "No similar past incidents found."
        )

        prompt = (
            "You are an autonomous ops agent. Diagnose the issue and choose one action.\n\n"
            f"Current metrics:\n{json.dumps(metrics, indent=2)}\n\n"
            f"Detected symptoms: {symptoms}\n\n"
            f"{history_block}\n\n"
            "Respond with ONLY a JSON object, no markdown:\n"
            "{\n"
            '  "diagnosis": "brief description",\n'
            '  "action": "alert|restart|halt",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "why this action"\n'
            "}\n\n"
            "action meanings: alert=notify only, restart=restart service, halt=stop service (critical)."
        )

        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        return _extract_json(resp.choices[0].message.content)

    def _rule_based_fallback(self, symptoms: list[str], similar: list[Incident]) -> dict:
        action = "alert"
        # Escalate to restart only if a past incident with same symptoms was resolved by restart
        for inc in similar:
            if (
                inc.resolved
                and inc.action == "restart"
                and set(symptoms) & set(inc.symptoms)
            ):
                action = "restart"
                break

        return {
            "diagnosis": f"[SAFE MODE] Detected symptoms: {', '.join(symptoms)}",
            "action": action,
            "confidence": 0.4,
            "reasoning": "Rule-based fallback — Qwen API unavailable after 3 retries",
        }
