"""
Qwen LLM client — two operating modes:

  Week 1 (fallback):  diagnose()              -> plain prompt, direct memory context
  Week 2 (MCP):       diagnose_with_mcp()     -> Qwen tool-calling over MCP

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_ascii(value: str, name: str) -> str:
    """
    Strip whitespace from an env-sourced string and verify it is pure ASCII.
    Logs a diagnostic WITHOUT revealing the value if non-ASCII is found.
    Raises ValueError so startup fails fast with a clear message.
    """
    value = value.strip()
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        logger.error(
            "Config error: %s contains non-ASCII characters at byte positions %d-%d "
            "(encoding=%r). Check .env for BOM, invisible Unicode, or copy-paste "
            "artefacts. Key length after strip: %d.",
            name, exc.start, exc.end - 1, exc.encoding, len(value),
        )
        raise ValueError(
            f"{name} has non-ASCII chars at positions {exc.start}-{exc.end - 1}. "
            "Open .env in a hex editor and strip any BOM or invisible characters."
        ) from exc
    return value


def _extract_json(text: str) -> dict:
    """Strip markdown fences and extract the first JSON object."""
    text = text.strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return json.loads(text)


def _safe_json(obj) -> str:
    """Serialize to JSON with all non-ASCII escaped — safe for HTTP headers/bodies."""
    return json.dumps(obj, ensure_ascii=True)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class QwenClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        # Fail fast at startup if the key/URL have encoding issues —
        # better than a cryptic UnicodeEncodeError 60 s into the agent loop.
        api_key  = _sanitize_ascii(api_key,  "QWEN_API_KEY")
        base_url = _sanitize_ascii(base_url, "QWEN_BASE_URL")
        model    = _sanitize_ascii(model,    "QWEN_MODEL")

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)
        self._model  = model
        self.degraded = False  # True when in safe-mode fallback

    # ── Week 1 path ────────────────────────────────────────────────────────

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
                logger.warning("Qwen attempt %d/3 failed (%s), retry in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)
            except UnicodeEncodeError as exc:
                logger.error(
                    "UnicodeEncodeError in Qwen call (encoding=%r, positions %d-%d). "
                    "Possible non-ASCII in API key or request content.",
                    exc.encoding, exc.start, exc.end - 1,
                )
                break  # not transient — don't retry
            except Exception as exc:
                logger.error("Unexpected Qwen error: %s", type(exc).__name__, exc)
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
            f"Current metrics:\n{_safe_json(metrics)}\n\n"
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
        Agentic loop: Qwen calls MCP tools to gather context, then decides.
        Returns (diagnosis_dict, tool_call_log).
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
            except UnicodeEncodeError as exc:
                # Pinpoint WHERE so we don't have to guess next time
                logger.error(
                    "UnicodeEncodeError in Qwen/MCP call: encoding=%r, "
                    "positions %d-%d, reason=%s. "
                    "Likely cause: non-ASCII in QWEN_API_KEY or tool result content.",
                    exc.encoding, exc.start, exc.end - 1, exc.reason,
                )
                break  # not transient — don't retry
            except Exception as exc:
                logger.error("Unexpected Qwen/MCP error: %s: %s", type(exc).__name__, exc)
                break

        logger.error("Qwen/MCP unavailable -- entering safe mode")
        self.degraded = True
        result = self._rule_based_fallback(symptoms, [])
        result["safe_mode"] = True
        return result, []

    def _call_api_with_tools(
        self, metrics: dict, symptoms: list[str], mcp: "IncidentMCPClient"
    ) -> tuple[dict, list[dict]]:
        prompt = (
            "You are an autonomous ops agent. An anomaly has been detected.\n\n"
            f"Current metrics:\n{_safe_json(metrics)}\n\n"
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

            # Execute each tool call via MCP and feed results back
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
                # Use json.dumps (ensure_ascii=True by default) so non-ASCII in
                # stored diagnoses is escaped — safe for log streams and HTTP body
                logger.info("MCP tool call: %s(%s) -> %s",
                            tc.function.name,
                            _safe_json(args)[:80],
                            _safe_json(tool_result)[:120])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    # ensure_ascii=True: diagnoses from DB may contain non-ASCII;
                    # escape them so the HTTP body stays pure ASCII-in-JSON
                    "content": _safe_json(tool_result),
                })

        raise RuntimeError("Agentic loop exceeded max rounds without final answer")

    # ── fallback ──────────────────────────────────────────────────────────

    def _rule_based_fallback(self, symptoms: list[str], similar: list[Incident]) -> dict:
        action = "alert"
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
            "reasoning": "Rule-based fallback -- Qwen API unavailable after 3 retries",
        }
