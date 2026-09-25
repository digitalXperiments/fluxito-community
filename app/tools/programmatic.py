"""
Programmatic tool calling — `run_script`.

Lets the agent compose multiple tool calls in a single round-trip by
writing a small async Python snippet. The script runs inside an
AST-restricted sandbox with only two injected callables:

    await call(tool_name, params)   — invoke any other registered tool
    await gather([...])             — run a list of awaitables in parallel

The script assigns its final value to ``RESULT``. The script's stdout
(from ``print(...)``) is captured and returned alongside the result.

Why this exists
---------------
Without this tool, compositions like "audit every GTM container and
return only the broken ones" force the model through N+1 sequential
tool-call round trips. With it, the model emits one script, we fan out
internally, and return only the filtered answer — saving latency and
keeping the model's context focused on the final answer instead of
intermediate payloads.

When NOT to use it
------------------
- Single tool call → call the tool directly. Scripts add overhead.
- Exploration / discovery of unknown shapes → call once, inspect, then
  script. Don't guess at response shapes.

Security notes
--------------
The sandbox uses AST validation + a restricted builtins namespace. It
blocks the common Python escape paths (dunder attribute access, eval,
getattr, imports). This is sufficient for trusted users on the Pro tier.
For untrusted multi-tenant use, migrate to a subprocess worker with
resource limits — the dispatch interface is shaped to make that swap
straightforward.
"""

import ast
import asyncio
import io
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

MAX_SCRIPT_SECONDS_HARD_CAP = 60  # absolute upper bound even if caller asks higher
DEFAULT_SCRIPT_SECONDS = 30
MAX_TOOL_CALLS = 50
MAX_OUTPUT_BYTES = 256 * 1024
MAX_STDOUT_CHARS = 8000


# ---------------------------------------------------------------------------
# AST validation
# ---------------------------------------------------------------------------

# Node types that are never allowed in a script. These either escape the
# sandbox (imports, class metaprogramming) or complicate the error model
# (try/except would let scripts silently swallow tool errors).
_FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.With,
    ast.AsyncWith,
    ast.Global,
    ast.Nonlocal,
    ast.Try,
    ast.Raise,
    ast.ClassDef,
)

# Names that would let a script reach back into the host process.
_FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "__builtins__",
        "breakpoint",
        "help",
        "memoryview",
    }
)


def _validate_ast(code: str) -> None:
    """Parse the script and raise ValueError on any forbidden construct.

    Two layers:
    1. Whole-node bans (imports, classes, try/except, etc.)
    2. Name + attribute bans (dunder access, reflective builtins)
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"Syntax error on line {exc.lineno}: {exc.msg}")

    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise ValueError(
                f"Forbidden construct on line {getattr(node, 'lineno', '?')}: {type(node).__name__}"
            )
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(f"Forbidden name on line {node.lineno}: '{node.id}'")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(
                f"Forbidden attribute on line {node.lineno}: '.{node.attr}' "
                "(underscore-prefixed attributes are blocked to prevent "
                "sandbox escapes via dunder access)"
            )


# ---------------------------------------------------------------------------
# Safe builtins namespace
# ---------------------------------------------------------------------------


# Resolve __builtins__ once — it may be a dict or a module depending on
# how this module is imported.
def _resolve_builtins():
    import builtins as _b

    return _b


def _build_safe_builtins():
    b = _resolve_builtins()
    names = (
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "len",
        "list",
        "map",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "isinstance",
        "issubclass",  # type checks are safe + useful
        "True",
        "False",
        "None",
    )
    return {n: getattr(b, n) for n in names if hasattr(b, n)}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

# Tool names scripts are not allowed to invoke — prevents recursive scripts
# and other self-referential abuse.
_DENYLISTED_TOOLS = frozenset({"run_script"})


async def _dispatch(tool_manager, name: str, params: dict) -> Any:
    """Invoke a publicly registered tool by name.

    Resolution order:
      1. Block the denylist (no recursion into run_script).
      2. Look up in the public tool surface (``_tools``) — the same 18
         unified tools the model sees in its prompt.
      3. Fall back to preserved legacy tools (``_legacy_tools``) so
         scripts can still reach fine-grained internals if needed.
    """
    if name in _DENYLISTED_TOOLS:
        raise ValueError(f"Tool '{name}' cannot be called from run_script")

    tool = getattr(tool_manager, "_tools", {}).get(name)
    if tool is None:
        tool = getattr(tool_manager, "_legacy_tools", {}).get(name)
    if tool is None:
        raise ValueError(f"Unknown tool '{name}'. Use a tool name visible in get_session_context().")
    from app.tools.registry import inner_tool_permitted

    if not await inner_tool_permitted(name, params or {}):
        raise PermissionError(f"Your role does not grant access to '{name}'.")
    return await tool.run(params or {})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_SHORT_DOC = f"""\
Execute a Python snippet that composes multiple tool calls in ONE round-trip.

Use this when answering the user would otherwise need 3+ sequential tool
calls — fan-out over entities, cross-connector composition, or filtering
and aggregation before returning. The script runs server-side, so only
the final value reaches your context.

Script environment:
  await call(tool_name: str, params: dict) -> dict   # invoke any tool
  await gather([awaitable, ...])                      # run in parallel
  RESULT = <final value>                              # what to return
  print(...)                                          # captured, returned as stdout

Safe builtins only (len, sum, min, max, sorted, map, filter, zip, enumerate,
range, dict, list, set, str, int, float, bool, isinstance, etc). No imports,
no file/network I/O, no try/except, no classes, no underscore-prefixed
attribute access.

Do NOT use for:
  - A single tool call — call the tool directly, it's cheaper.
  - Exploring unknown response shapes — call the tool once, inspect,
    then script.

Budgets: {DEFAULT_SCRIPT_SECONDS}s timeout, {MAX_TOOL_CALLS} inner tool calls, {MAX_OUTPUT_BYTES // 1024}KB result.

Call get_session_context(tool_name='run_script') for worked examples.
"""


def register_programmatic_tool(mcp_server) -> None:
    """Register the ``run_script`` MCP tool.

    Must be called BEFORE ``rewire_unified_surface`` so the tool lands
    in ``tm._tools`` and survives the rewire (the rewire only strips
    tools listed in its ``legacy_names`` set, which does not include
    ``run_script``).
    """
    tool_manager = mcp_server._tool_manager

    async def run_script(code: str, timeout_seconds: int | None = None) -> dict:
        # Docstring set below after the def so we can format it with constants.
        if not isinstance(code, str) or not code.strip():
            return {
                "error": True,
                "error_type": "invalid_script",
                "message": "`code` must be a non-empty Python snippet.",
            }

        try:
            _validate_ast(code)
        except ValueError as exc:
            return {
                "error": True,
                "error_type": "invalid_script",
                "message": str(exc),
            }

        timeout = DEFAULT_SCRIPT_SECONDS if timeout_seconds is None else int(timeout_seconds)
        timeout = max(1, min(timeout, MAX_SCRIPT_SECONDS_HARD_CAP))

        call_trace = []
        call_count = {"n": 0}
        stdout_buf = io.StringIO()

        async def _call(tool_name, params=None):
            call_count["n"] += 1
            if call_count["n"] > MAX_TOOL_CALLS:
                raise RuntimeError(
                    f"Exceeded {MAX_TOOL_CALLS}-call budget. Reduce fan-out "
                    "or filter the input list before looping."
                )
            if not isinstance(tool_name, str):
                raise ValueError("call(): tool_name must be a string")
            call_start = time.perf_counter()
            action = (params or {}).get("action") if isinstance(params, dict) else None
            try:
                result = await _dispatch(tool_manager, tool_name, params or {})
                return result
            finally:
                call_trace.append(
                    {
                        "tool": tool_name,
                        "action": action,
                        "duration_ms": int((time.perf_counter() - call_start) * 1000),
                    }
                )

        async def _gather(awaitables):
            if not hasattr(awaitables, "__iter__"):
                raise ValueError("gather(): argument must be an iterable of awaitables")
            return await asyncio.gather(*awaitables)

        def _safe_print(*args, **kwargs) -> None:
            # Redirect print to our buffer. Ignore `file=` kwarg.
            kwargs.pop("file", None)
            print(*args, file=stdout_buf, **kwargs)

        # Wrap the user's code inside an async function so `await` is valid.
        # Each user line gets a 4-space indent; syntax errors caught above.
        wrapped = (
            "async def __script__():\n"
            "    RESULT = None\n"
            + "".join(f"    {line}\n" for line in code.splitlines())
            + "    return RESULT\n"
        )

        safe_builtins = _build_safe_builtins()
        sandbox_globals = {
            "__builtins__": safe_builtins,
            "call": _call,
            "gather": _gather,
            "print": _safe_print,
        }

        start = time.perf_counter()
        try:
            compiled = compile(wrapped, "<run_script>", "exec")
            exec(compiled, sandbox_globals)  # noqa: S102 — intentional, sandboxed
            script_fn = sandbox_globals["__script__"]
            result = await asyncio.wait_for(script_fn(), timeout=timeout)
        except TimeoutError:
            return {
                "error": True,
                "error_type": "timeout",
                "message": (
                    f"Script exceeded {timeout}s and was cancelled. "
                    "Narrow the fan-out, filter earlier, or split into steps."
                ),
                "tool_calls_made": call_count["n"],
                "trace": call_trace,
                "stdout": stdout_buf.getvalue()[:MAX_STDOUT_CHARS] or None,
            }
        except Exception as exc:
            # Any exception in the script body — surface line info to help
            # the model fix it without a debugger.
            import traceback

            tb = traceback.extract_tb(exc.__traceback__)
            # Find the frame from the user's script
            script_frame = next(
                (f for f in reversed(tb) if f.filename == "<run_script>"),
                None,
            )
            line_info = f" at line {script_frame.lineno}" if script_frame else ""
            return {
                "error": True,
                "error_type": type(exc).__name__,
                "message": f"{exc}{line_info}",
                "tool_calls_made": call_count["n"],
                "trace": call_trace,
                "stdout": stdout_buf.getvalue()[:MAX_STDOUT_CHARS] or None,
            }

        duration_ms = int((time.perf_counter() - start) * 1000)

        # Enforce JSON-serializability + output size
        try:
            payload = json.dumps(result, default=str)
        except (TypeError, ValueError) as exc:
            return {
                "error": True,
                "error_type": "non_serializable_result",
                "message": (
                    f"RESULT must be JSON-serializable: {exc}. "
                    "Convert tuples/sets to lists and ensure no raw objects."
                ),
                "tool_calls_made": call_count["n"],
                "trace": call_trace,
            }
        if len(payload) > MAX_OUTPUT_BYTES:
            return {
                "error": True,
                "error_type": "output_too_large",
                "message": (
                    f"Result is {len(payload)} bytes, cap is {MAX_OUTPUT_BYTES}. "
                    "Filter or aggregate more aggressively — return summary "
                    "statistics or top-N instead of raw rows."
                ),
                "preview": payload[:4000],
                "tool_calls_made": call_count["n"],
                "trace": call_trace,
            }

        stdout_text = stdout_buf.getvalue()
        return {
            "result": result,
            "tool_calls_made": call_count["n"],
            "duration_ms": duration_ms,
            "trace": call_trace,
            "stdout": stdout_text[:MAX_STDOUT_CHARS] if stdout_text else None,
        }

    run_script.__doc__ = _SHORT_DOC
    tool_manager.add_tool(run_script, name="run_script")
    logger.info("registered run_script (programmatic tool calling)")
