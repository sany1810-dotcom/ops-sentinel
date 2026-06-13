"""
Qwen LLM client — two operating modes:

  Week 1 (fallback):  diagnose()              → plain prompt, direct memory context
  Week 2 (MCP):       diagnose_with_mcp()     → Qwen tool-calling over MCP

Resilience layers (both modes):
  1. Normal Qwen call (up to 3 retries with exponential backoff)
  2. Rule-based safe-mode fallback when Qwen API is unavailable
"""
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from openai import OpenAI, APIError, APITimeoutError, APIConnectionError

from memory import Incident

if TYPE_CHECKING:
    from mcp_client import IncidentMCPClient

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

    # ── Week 2: MCP agentic tool-calling ──────────────────────────────────

    def diagnose_with_mcp(
        self,
        metrics: dict,
        symptoms: list[str],
        mcp: "IncidentMCPClient",
    ) -> tuple[dict, list[dict]]:
        """
        Agentic loop: Qwen calls MCP tools (search_similar_incidents, get_stats…)
        to gather context autonomously, then produces a final diagnosis.

        Returns:
          (diagnosis_dict, tool_call_log)
          diagnosis_dict — same shape as diagnose() output
          tool_call_log  — [{tool, args, result}, …] for observability
        """
        for attempt in range(3):
            try:
                result, log = self._call_api_with_tools(metrics, symptoms, mcp)
                if self.degraded:
                    logger.info("Qwen API recovered, leaving safe mode")
                self.degraded = False
                result["safe_mode"] = False
                return result, log
            except (APIError, APITimeoutError, APIConnectionError, json.JSONDecodeError) as exc:
                wait = 2 ** attempt
                logger.warning("Qwen/MCP attempt %d/3 failed (%s), retry in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)
            except Exception as exc:
                logger.error("Unexpected Qwen/MCP error: %s", exc)
                break

        logger.error("Qwen/MCP unavailable — entering safe mode")
        self.degraded = True
        # Fall back to Week 1 rule-based (no MCP memory needed)
        result = self._rule_based_fallback(symptoms, [])
        result["safe_mode"] = True
        return result, []

    def _call_api_with_tools(
        self, metrics: dict, symptoms: list[str], mcp: "IncidentMCPClient"
    ) -> tuple[dict, list[dict]]:
        prompt = (
            "You are an autonomous ops agent. An anomaly has been detected.\n\n"
            f"Current metrics:\n{json.dumps(metrics, indent=2)}\n\n"
            f"Detected symptoms: {symptoms}\n\n"
            "Use the available tools to search for similar past incidents and gather context. "
            "Then respond with ONLY a JSON object (no markdown, no thinking tags):\n"
            "{\n"
            '  "diagnosis": "brief description",\n'
            '  "action": "alert|restart|halt",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "why this action"\n'
            "}\n\n"
            "action meanings: alert=notify only, restart=restart service, "
            "halt=stop service (critical only)."
        )

        tools = mcp.tool_schemas
        messages: list[dict] = [{"role": "user", "content": prompt}]
        tool_call_log: list[dict] = []

        for _round in range(6):  # safety cap on tool-call rounds
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=800,
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                # Final text answer
                return _extract_json(msg.content), tool_call_log

            # Append assistant message (with tool_calls) to history
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # Execute each tool call via MCP and collect results
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                try:
                    tool_result = mcp.call_tool(tc.function.name, args)
                except Exception as exc:
                    tool_result = {"error": str(exc)}

                tool_call_log.append({
                    "tool": tc.function.name,
                    "args": args,
                    "result": tool_result,
                })
                logger.info("MCP tool call: %s(%s) → %s",
                            tc.function.name,
                            json.dumps(args)[:80],
                            str(tool_result)[:120])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result),
                })

        raise RuntimeError("Agentic loop exceeded max rounds without final answer")

    # ── Week 1 / fallback ─────────────────────────────────────────────────

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
