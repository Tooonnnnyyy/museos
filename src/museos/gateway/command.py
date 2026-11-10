"""Any local command-line model tool as a provider (bring your own stack).

Use this for a vendor's official CLI that you are signed in to with your own subscription, or for a local
model runner. You supply the command in ``providers.toml``; MuseOS sends the prompt on stdin and reads the
reply from stdout. The command runs without a shell, from an empty temporary directory, with other
providers' secrets removed from its environment.

You are responsible for the terms of the tool you configure. A personal subscription is normally for your own
use on your own machine: keep ``max_concurrency = 1`` and the call caps, and never serve other people through it.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile

from museos.gateway.base import CompletionRequest, CompletionResponse, ProviderConfig, ProviderError
from museos.gateway.claude_cli import child_env


class CommandAdapter:
    def __init__(self, config: ProviderConfig):
        if not config.command:
            raise ValueError(f"provider {config.id}: adapter 'command' needs a `command = [...]` list")
        self.config = config
        self.provider_id = config.id

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        binary = shutil.which(self.config.command[0])
        if not binary:
            raise ProviderError(f"command not found: {self.config.command[0]}", retryable=False,
                                kind="missing_binary")
        # {model} in any argument is replaced by the configured model name.
        args = [binary, *[a.replace("{model}", request.model) for a in self.config.command[1:]]]
        prompt = "\n\n".join(m["content"] for m in request.messages)

        with tempfile.TemporaryDirectory(prefix="museos-cmd-") as workdir:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, env=child_env(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                try:
                    proc.stdin.write(prompt.encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError, RuntimeError):
                    pass  # the tool exited before reading its input; the exit code says why
                out, _err = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise

        if proc.returncode != 0:
            raise ProviderError(f"command exited with status {proc.returncode}", retryable=True, kind="cli_error")
        text = out.decode(errors="replace")
        return CompletionResponse(
            text=text, provider=self.provider_id, model=request.model,
            input_tokens=len(prompt) // 4, output_tokens=len(text) // 4,  # estimates: the tool reports none
            cost_usd=self.config.estimate_cost(len(prompt) // 4, len(text) // 4) or 0.0, finish_reason="stop")
