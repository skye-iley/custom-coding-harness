# Design Document: Custom Coding Harness with Deep Agents

## 1. System Overview & Objectives
*   **Purpose**: Multi-agent orchestration platform for autonomous, sandboxed code generation using the Deep Agents framework.
*   **Execution Target**: Ubuntu-based Docker container with host-persistent workspace and config mounts.
*   **Core Capabilities**:
    *   **Intelligent Routing**: Local FSM-constrained classifier routing to local or cloud expert orchestrators.
    *   **Dynamic Sandboxing**: Per-agent restricted file access via `bubblewrap` and `HarnessProfile` scoping.
    *   **Git Automation**: Deterministic branch lifecycle with mandatory human pre-review for PRs (no auto-merge).
    *   **Token Optimization**: Multi-layer compression pipeline utilizing `Headroom` (CCR), `Caveman` (Terseness), and Prompt Caching.
    *   **Provider Agnostic**: Unified model interface supporting local (Ollama/LMStudio) and cloud (Claude/GPT) providers.
    *   **Observability**: Full telemetry for token usage, financial cost, routing accuracy, and session effectiveness.

---

## 2. Sandboxing Strategy & Container Layout

### Dual-Container Boundary
*   **Orchestrator Container**: Hosts Deep Agents runtime and coordinates agent execution. No mount to host Docker socket (`/var/run/docker.sock`).
*   **Executor Sandbox**: Commands run inside ultra-restrictive nested jail powered by `bubblewrap` (bwrap).

### Bubblewrap Configuration
Shell tool executions wrapped dynamically via host execution template. The bind-mount list is generated per-agent based on the `HarnessProfile`:
```bash
bwrap \
  --ro-bind /usr /usr \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin \
  --ro-bind /sbin /sbin \
  --dir /tmp \
  --proc /proc \
  --dev /dev \
  {DYNAMIC_BIND_MOUNTS} \
  --chdir /workspace \
  --unshare-all \
  --uid 1000 \
  --gid 1000 \
  /bin/bash -c "{agent_command}"
```
*   `{DYNAMIC_BIND_MOUNTS}`: Replaces the global `/workspace` bind.
    *   **Full Access**: `--bind /workspace /workspace`
    *   **Scoped Access**: `--bind /workspace/src/components /workspace/src/components` (only allows access to specific sub-dirs).
    *   **Read-Only Scoping**: `--ro-bind /workspace/docs /workspace/docs`.
*   `--unshare-all`: Isolates net, ipc, uts, pid, user namespaces.
*   `--ro-bind`: Prevents injection or corruption of system paths.

### Path Guard Middleware
Pre-flight check in Python tool execution class prevents symlink escapes and directory traversal:
```python
import os

def validate_path(target_path: str, base_dir: str = "/workspace") -> str:
    abs_target = os.path.realpath(target_path)
    abs_base = os.path.realpath(base_dir)
    if not abs_target.startswith(abs_base):
        raise PermissionError("Path out of sandboxed workspace bounds")
    return abs_target
```

### Workspace Environment Isolation
To prevent dependency conflicts between the orchestrator runtime and the target project:
*   **Decoupled Runtimes**: The Orchestrator runs in a fixed system-level Python virtual environment.
*   **Local Environment Manager**: Utilize Miniforge/Conda within the container to manage workspace-specific dependencies.
*   **Workspace-Local Envs**: Environments are stored within the persistent workspace directory (e.g., `/workspace/.conda/env`), ensuring they survive container restarts.
*   **Execution Wrapper**: All agent-triggered shell commands are routed through a wrapper that automatically activates the workspace-local environment before execution.

### Persistent Storage Strategy

To ensure workspace and configuration persistence across container deployments:
*   **Workspace Mounting**: Start container with `-v /host/path/to/workspace:/workspace`. All agent work inside `/workspace` is automatically persisted on host filesystem.
*   **Config Persistence**: Mount specific host files to container internal paths:
    *   `-v /host/.gitconfig:/root/.gitconfig`
    *   `-v /host/.ssh:/root/.ssh`
    *   `-v /host/.agent_telemetry.json:/workspace/.agent_telemetry.json`

This guarantees agent session state, Git identity, and accumulated metrics survive container teardown.

---

## 3. Git Session Lifecycle & Integration

### Deterministic Branching
*   **Naming Pattern**: `agent/{provider}/{session-id}`
*   **Session ID**: `sha256(user_id + workspace_path + timestamp)[:12]`

### Workflow Hooks

```
[Start Session] ──> Fetch origin/main ──> Create branch agent/cloud/fe89a2
                                                               │
[Agent Edits]   ──> Track tokens/cost ──> Run sandbox command  │
                                                               ▼
[End Session]   ──> Stage mutations ──> Push branch ──> Create GitHub PR
```

1.  **Session Start (Init)**:
    *   Assert workspace is clean: `git status --porcelain` (stash/abort if dirty).
    *   Retrieve remote main: `git fetch origin main`
    *   Checkout target branch: `git checkout -b agent/{provider}/{session-id} origin/main`
    *   Record base commit hash as parent revision pointer.
2.  **Session Close (Teardown)**:
    *   Stage local changes (excluding `.agent_telemetry` and blocklisted configs): `git add .`
    *   Commit changes: `git commit -m "agent(session-{session-id}): automated codebase mutations"`
    *   Push to upstream: `git push origin agent/{provider}/{session-id}`
    *   Invoke GitHub PR API or execute `gh` CLI:
        ```bash
        gh pr create \
          --title "Agent Run: {session-id}" \
          --body "Automated PR generated by Deep Agents. Cost={cost}, Tokens={tokens}. **Note: Manual review required. Auto-merge disabled.**" \
          --base main \
          --head agent/{provider}/{session-id}
        ```
    *   **Merge Policy**: The system must **never** auto-merge PRs. Human pre-review is mandatory before main branch integration.

---

## 4. Model Routing & Provider Abstraction

### Router Architecture
*   **Deep Agents API**: Utilizes `create_deep_agent` from the `deepagents` package to initialize agents with defined backends, profiles, and middleware.
*   **Traffic Classifier (The Gatekeeper)**:
    *   **Model**: Lightweight local LLM (e.g., Qwen2.5-Coder-7B).
    *   **Analysis**: Parses prompt for complexity, intent, and required scope.
    *   **Constraint**: Implements **Constrained Output (FSM)**. The model is forced to output exactly one category token from the set `{TRIVIAL, MODERATE, COMPLEX, CRITICAL}`. No reasoning or prose is permitted.
    *   **Categories**:
        *   `TRIVIAL`: formatting, simple regex, documentation updates $\rightarrow$ Local Orchestrator.
        *   `MODERATE`: Single-file logic changes, bug fixes in isolated functions $\rightarrow$ Local Orchestrator.
        *   `COMPLEX`: Cross-file refactoring, architectural changes, new feature design $\rightarrow$ Cloud Orchestrator.
        *   `CRITICAL`: Security patches, core system migrations $\rightarrow$ Cloud Orchestrator (highest reasoning model).

*   **Expert Orchestrator Roles**:
    *   **Local Orchestrator**: Optimized for speed and cost. Handles `TRIVIAL` and `MODERATE` tasks.
    *   **Cloud Orchestrator**: Optimized for reasoning. Handles `COMPLEX` and `CRITICAL` tasks.
    *   **Specialized Profiles**: `HarnessProfile` defines specific toolsets for roles (e.g., *Architect* has full project read access; *Coder* has limited write access to specific directories).
*   **Routing Pipeline**:
    1.  **Ingestion**: Prompt arrives $\rightarrow$ Classifier analyzes.
    2.  **Decision**: Classifier outputs category $\rightarrow$ Router selects `model` string (e.g., `local-coder#main` vs `claude-3.5#main`).
    3.  **Dispatch**: `create_deep_agent` is called with the selected model, `LocalShellBackend`, and appropriate `HarnessProfile`.
    4.  **Execution**: Selected orchestrator takes control of the session.
*   **Provider Config**:
    *   Model-specific configurations defined via the `model` parameter in `create_deep_agent` (e.g., `model="gpt-4#agent-tag"`).
    *   Backend environments are inherited through `LocalShellBackend` configuration, ensuring consistent execution context.


---

## 5. Agent Architecture & Framework Comparison

### Structural Options
*   **Base Deep Agents Core**:
    *   *Nature*: Pre-configured agent templates provided by the `deepagents` library.
    *   *Pros*: Rapid deployment, stable tool-handling, minimal configuration.
    *   *Cons*: Limited flexibility in reasoning loops; potential token inefficiency for niche tasks.
    *   *Best For*: Generic worker agents (e.g., "File Reader", "Test Runner").

*   **Pi Agent Framework**:
    *   *Nature*: Hyper-lightweight, prompt-driven agents focusing on minimal state and high-speed response.
    *   *Pros*: Extremely low token overhead, high latency performance.
    *   *Cons*: Lower reasoning depth; prone to loop failures on complex logic.
    *   *Best For*: The Traffic Classifier and trivial task handlers.

*   **Custom Agents (via `create_deep_agent`)**:
    *   *Nature*: Bespoke agents utilizing custom `HarnessProfile`, specific `middleware`, and tailored system prompts.
    *   *Pros*: Absolute control over tool access, output formatting, and reasoning steps.
    *   *Cons*: Higher development overhead; requires manual tuning of prompts.
    *   *Best For*: Expert Main Orchestrators.

*   **Hybrid Combination (Recommended)**:
    *   **Classifier (Pi-style)** $\rightarrow$ **Orchestrator (Custom)** $\rightarrow$ **Worker (Base)**.
    *   Ensures a "funnel" of efficiency: fast classification, precise planning, and reliable execution.

### Alternative Lightweight Patterns
*   **Plan-and-Execute**: Decouples the planning phase from the execution phase. Prevents the agent from "forgetting" the goal during long tool-call sequences, reducing redundant tokens.
*   **Finite State Machine (FSM) Agents**: Use deterministic state transitions for specific workflows (e.g., "Branch $\rightarrow$ Edit $\rightarrow$ Test $\rightarrow$ PR"). Eliminates reasoning overhead for predictable paths.
*   **ReAct (Reason+Act)**: The standard iterative loop. Useful for discovery but token-heavy; should be reserved for `COMPLEX` tasks.

---

## 6. Token Usage & Cost Tracker

### Callback Implementation
Deep Agents provides hooks to monitor execution streams:
```python
# Deep Agents tracking implementation utilizing underlying framework callbacks
class TokenCostTracker:
    def on_llm_end(self, response, **kwargs) -> None:
        # Accumulate prompt_tokens, completion_tokens, and query pricing model
        pass
```

### Reference Schema (`prices.json`)
Local dictionary for calculating financial cost of session:
```json
{
  "anthropic/claude-3-5-sonnet-20241022": {
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015
  },
  "openai/gpt-4o": {
    "input_cost_per_token": 0.0000025,
    "output_cost_per_token": 0.000010
  }
}
```

---

## 7. Token Optimization Pipeline (Headroom & Caveman)

### Headroom Context Compression Layer
*   **Tool**: Integrate `chopratejas/headroom` inside the orchestrator container environment.
*   **Integration Vectors**:
    *   **Proxy Mode**: Execute `headroom proxy --port 8787` inside Docker. Route all Deep Agents outbound model traffic through the local proxy endpoint.
    *   **Adapter**: Bind via Deep Agents model wrapping to transparently prune outgoing messages.
    *   **Reversible CCR (Context Cache Retrieval)**: Retain uncompressed raw logs and tool outputs locally. Forward highly compressed schemas (60-95% token savings) to LLM; allow model to retrieve original blocks on-demand via headroom tools.

### Caveman Compression Filter
*   **Dynamic Translation**: Pass structural prompts and systematic templates through Terse Filter (Caveman).
*   **Rule Set**: Automatically drop articles (a, an, the), pronouns, auxiliary verbs, and pleasantries. Translate outputs to minimal structural fragments.

### Pre-Filter Context Buffering
*   **Context Safety Margin**: Protect LLM limits via traditional token-buffer budgeting (retaining 10-20% gap).
*   **Coarse Truncation**: Pre-truncate high-entropy raw terminal dumps (e.g., 2MB build logs) before ingestion by Headroom.
*   **Log-Saliency Sampling**: Prioritize error/warning blocks and critical state transitions; discard repetitive noise.

### Advanced Optimization Techniques
*   **Prompt Caching**: Utilize provider-native caching (e.g., Anthropic Prompt Caching) for static system prompts and large-scale project contexts to reduce cost and latency.
*   **Semantic Deduplication**: Filter out redundant or overlapping information from multiple tool outputs and RAG chunks before prompt assembly.
*   **Relevant Fragment Extraction**: Use Tree-Sitter to extract only the active function and its immediate dependency graph rather than full files or skeletons.

### Deep Agents Integration Specifics

#### Headroom Integration
*   **Proxy-Level (No Code Changes)**:
    Execute Deep Agents in a Docker runtime with `HTTP_PROXY` and `HTTPS_PROXY` pointing to `http://localhost:8787` (Headroom proxy). Outgoing model queries automatically compress redundant logs, JSON tool outputs, and duplicate messages.
*   **Adapter Wrapper**:
    Configure model clients via Deep Agents configuration to route through Headroom:
    ```python
    # Example approach for binding Headroom proxy within Deep Agents config
    model_config = {
        "model": "gpt-4o",
        "api_base": "http://localhost:8787/v1",
        "api_key": "mock-key-for-proxy"
    }
    ```

#### Caveman Integration
*   **Prompt Pre-processor**:
    Register prompt-slicing middleware within the Deep Agents execution chain:
    ```python
    from caveman_compressor import caveman_compress # Custom prompt trimmer

    def compress_messages_runnable(messages):
        for msg in messages:
            msg['content'] = caveman_compress(msg['content'], level="full")
        return messages

    # Deep Agents internal chain integration point
    # agent_config['pre_processor'] = compress_messages_runnable
    ```

---

## 8. Observability & Metrics

### Logging Architecture
*   **System Logs**: Docker container stdout/stderr for runtime health and harness errors.
*   **Agent Execution Logs**: Detailed trace of tool calls, model inputs, and responses (stored in `.agent-trace.jsonl`).
*   **Optimization Logs**: Record original vs. compressed token counts for Headroom/Caveman to measure actual savings.

### Efficiency Metrics
*   **Token Reduction Ratio**: $\frac{Tokens_{Compressed}}{Tokens_{Original}}$ across all optimized prompts.
*   **Cost per Task**: Total financial cost of a single task/PR, broken down by provider.
*   **Routing Accuracy**: Percentage of tasks correctly routed to local vs. cloud orchestrators (verified by success rate).
*   **Latency Profiling**: Track Time-To-First-Token (TTFT) and total execution time per provider.

### Effectiveness Metrics
*   **Session Success Rate**: Ratio of sessions resulting in a merged PR vs. abandoned/failed sessions.
*   **PR Quality Score**: Manual or automated review of PRs to determine if optimized prompts impacted accuracy.
*   **Iteration Count**: Average number of tool calls per successful task resolution.

### Reporting & Telemetry
*   **Session Telemetry**: Every session generates a `.agent-metrics.json` file in the workspace.
*   **PR Metadata**: Telemetry summaries (Total Cost, Token Savings, Model Mix) are automatically appended to the GitHub PR description.

---

## 9. CLI Frontend & User Interface

### Interface Architecture
*   **Implementation Stack**: Python-based CLI using `Typer` (command structure) and `Rich` (terminal rendering).
*   **Communication Layer**: The CLI acts as a wrapper around the Docker Orchestrator. It communicates via `docker exec` for simple commands or a lightweight FastAPI bridge for real-time telemetry.

### Primary Command Set
*   `harness start [project_path]`: Initializes a new session. Triggers Git branching, mounts volumes, and boots the orchestrator.
*   `harness status`: Displays active agent, current task, and real-time token/cost metrics via a `Rich` live-updating panel.
*   `harness interact`: Opens an interactive loop to send high-level prompts to the orchestrator.
*   `harness finish`: Triggers the session teardown, commits changes, and generates the GitHub PR.
*   `harness metrics`: Prints a detailed session summary (token reduction ratio, cost per task).

### Real-time Observability (TUI Elements)
*   **Agent Status Bar**: Live indicator of current model in use (Local vs. Cloud) and the current level of the FSM classifier.
*   **Cost Ticker**: A rolling update of financial spend per session.
*   **Sandbox Stream**: Tailed view of the `LocalShellBackend` output, color-coded by saliency (e.g., red for errors, yellow for warnings).

### User Control Loop
*   **Pre-Flight Approval**: For `CRITICAL` tasks, the CLI intercepts the orchestrator's plan and requires a `Y/N` confirmation before the agent executes commands in the sandbox.
*   **Manual Override**: Capability to manually trigger a `cloud-fallback` if the local orchestrator is stuck in a loop.

#### HITL Customized Settings
Users can configure their level of autonomy via a `.harness-config.yaml` file:
*   **`autonomy_level`**:
    *   `strict`: Human approval required for *every* tool call (high safety, low speed).
    *   `guided`: Approval required only for `CRITICAL` tasks and filesystem deletions (balanced).
    *   `autonomous`: Human approval only required for the final PR submission (high speed, lower safety).
*   **`review_triggers`**: Customizable list of keywords or file patterns that force a human intervention regardless of the `autonomy_level` (e.g., `*.env`, `auth_logic.py`).
*   **`interruption_policy`**: Define if the agent should pause and wait for input or continue in a "shadow mode" and present a batch of changes for review later.

---

## 10. Security, Verification, & Testing Plan

### Risk Analysis & Mitigation
*   **Classifier Misrouting**:
    *   *Risk*: A `CRITICAL` security task is misclassified as `TRIVIAL`, routing it to a local model that fails to identify a vulnerability.
    *   *Mitigation*: Implement a "Keyword Override" list (e.g., "CVE", "exploit", "auth") that forces an automatic upgrade to Cloud Orchestrator regardless of the local model's decision.
*   **Telemetry Leakage**:
    *   *Risk*: Sensitive environment variables or API keys are captured in `.agent-trace.jsonl` or `.agent-metrics.json`.
    *   *Mitigation*: Implement a PII/Secret scrubbing middleware that masks strings matching common key patterns before writing to disk.
*   **CLI Input Injection**:
    *   *Risk*: Malicious input via `harness interact` is passed unvalidated to the orchestrator or shell.
    *   *Mitigation*: Strict input sanitization and avoidance of `shell=True` in any Python `subprocess` calls.
*   **Sandbox Escape (Dynamic Binds)**:
    *   *Risk*: An incorrectly configured `HarnessProfile` creates a bind mount that allows access to the host's `/root` or `/etc`.
    *   *Mitigation*: Enforce a strict whitelist of allowed base directories for all dynamic binds; any path outside `/workspace` is rejected at the `create_deep_agent` level.
*   **Distillation Bias**:
    *   *Risk*: The "Teacher" model generates skewed synthetic labels, causing the local classifier to consistently under-route complex tasks.
    *   *Mitigation*: Use a "Gold Set" of human-verified routing labels to validate the student model's accuracy before deployment.

### Security Verification Suite
*   **Traversal Escape Block Test**: Execution of `bwrap ... cat ../../etc/passwd` must fail.
*   **Root Isolation Test**: Execution of `sudo apt update` or `su root` inside runtime must throw permission errors.
*   **Data Exfiltration Test**: Disable virtual NIC inside sandbox; verify curl/wget payloads to external domains abort instantly.

### Automation Validation Tests
*   **Git Lifecycle Mocking**: Verify git checkout, commit generation, and `gh` client payload formats match specs.
*   **Cost Accumulation Test**: Assert calculator arithmetic computes correctly across multi-step mixed local/cloud runs.
*   **Headroom Proxy Assertions**: Verify mock 20KB tool outputs intercept, compress by >70% via local proxy, and preserve core semantic content under eval.
*   **Routing Accuracy Test**: Pass 100 mixed-complexity prompts; verify classifier matches ground truth with >95% accuracy.
*   **CLI Command Validation**: Test all `harness` commands for proper error handling and response formatting.


---

## 11. Future Extensions & Roadmap

### Agentic Evolutions
*   **Multi-Agent Peer Review**: Introduce a secondary "Reviewer Agent" that must approve changes in the sandbox before they are committed to the Git branch.
*   **Agent Swarms**: Allow the orchestrator to spawn multiple specialized worker agents (e.g., one for tests, one for docs, one for logic) to work in parallel on separate files.
*   **Long-Term Project Memory**: Integrate a vector database (e.g., Qdrant or ChromaDB) to store a persistent, compressed index of the entire project history and decision logs.

### Framework Enhancements
*   **Automated Benchmarking Suite**: Build a "Gold Set" of coding tasks with known correct outcomes to quantitatively measure the impact of new compression algorithms or routing logic.
*   **Self-Tuning Classifier**: Implement a feedback loop where the orchestrator reports the "correctness" of the initial routing decision, used to fine-tune the local classifier model via DPO or RLHF.
*   **Classifier Knowledge Distillation**: Use a high-reasoning "Teacher" model (e.g., Claude 3.5 Opus or GPT-4o) to label a large dataset of prompt-category pairs. Fine-tune the lightweight local classifier (the "Student") using this high-quality synthetic data to align its routing decisions with the teacher's reasoning.
*   **IDE Integration**: Develop a VS Code extension to allow the harness to be controlled directly from the editor, providing inline "agent-suggested" diffs.

### Advanced Optimization
*   **Speculative Execution**: Run the local orchestrator and cloud orchestrator in parallel for `MODERATE` tasks; use the cloud result to verify the local one, optimizing for both speed and accuracy.
*   **Dynamic Prompt Compression**: Adjust Caveman/Headroom intensity levels in real-time based on the current token window usage and the importance of the current task.


