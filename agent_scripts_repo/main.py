import argparse
import asyncio
import logging
from collections.abc import Sequence


from agent_framework import Agent

from src.clients import create_responses_client
from src.config import AppSettings, load_settings
from src.observability import configure_logging, response_to_lines, get_observability
from src.workflows.level1.supervisor_orchestration import (
    create_supervisor_workflow_agent,
)

# Langfuse integration
logger = logging.getLogger(__name__)

# Provider-agnostic observability (Langfuse, etc.) configured via config.ini.
obs = get_observability()

def _preview_text(value: object, limit: int = 240) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit] + "..." if len(text) > limit else text


def _resolve_target_deployment(settings: AppSettings) -> str:
    target = (settings.target_deployment or "").strip().lower()
    if target not in {"foundry", "aca"}:
        logger.warning(
            "Unsupported deployment target '%s'; defaulting to foundry adapter.",
            target or "<empty>",
        )
        return "foundry"
    return target


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Three-level supervisor/super/utility hierarchy sample"
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run a one-shot CLI interaction instead of starting the HTTP server",
    )
    parser.add_argument(
        "--prompt",
        default="Create an end-to-end implementation plan for a secure release checklist.",
        help="Prompt used in CLI mode",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Python logging level (for example: INFO, DEBUG, WARNING)",
    )
    return parser.parse_args(argv)


async def run_cli(agent: Agent, prompt: str) -> None:
    print("Running hierarchical agent in CLI mode...")
    print(f"\nUser: {prompt}\n")

    try:
        with obs.start_span("MAFAgent.execute", input={"query": prompt}) as span:
            obs.update_trace(input=_preview_text(prompt))
            try:
                response = await agent.run(prompt)
                lines = response_to_lines(response)

                if not lines:
                    result = "No response returned."
                    print("assistant: No response returned.")
                else:
                    result = lines[-1]
                    print("Final output:")
                    print(result)

                span.update(output={"final_response": result})
                obs.update_trace(output={"final_response": result})
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                span.update(level="ERROR", status_message=error_msg, output={"error": error_msg})
                obs.update_trace(output={"error": error_msg})
                raise
    finally:
        obs.flush()


async def run_server(agent: Agent, settings: AppSettings) -> None:
    target_deployment = _resolve_target_deployment(settings)

    if target_deployment == "aca":
        print("Starting MCP -> A2A -> Agent Framework server...")
        from src.hosting import run_mcp_a2a_server

        await run_mcp_a2a_server(agent)
    else:
        print("Starting workflow agent HTTP server (Foundry adapter)...")
        from agent_framework_foundry_hosting import ResponsesHostServer

        await ResponsesHostServer(agent).run_async()


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level)
    settings = load_settings()

    try:
        client = create_responses_client(settings)
        agent = create_supervisor_workflow_agent(client, settings)
        if args.cli:
            try:
                await run_cli(agent, args.prompt)
            except Exception as exc:
                raise SystemExit(f"Run failed: {exc}") from exc
            return

        try:
            await run_server(agent, settings)
        except Exception as exc:
            raise SystemExit(f"Run failed: {exc}") from exc
    except RuntimeError as exc:
        raise SystemExit(f"Startup failed: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
