/**
 * Minimal local types for the OpenClaw plugin SDK.
 *
 * `openclaw` is provided by the host at runtime and is deliberately not a
 * dependency of this package. tsconfig.json maps the type-only import of
 * "openclaw/plugin-sdk/plugin-entry" in index.ts to this file (`paths`), so
 * nothing here exists at runtime and the file is not shipped. Only the
 * surface index.ts touches is declared.
 *
 * A plain `.ts` module rather than a `.d.ts` because the repo's .gitignore
 * excludes `*.d.ts`.
 */

export interface PluginLogger {
  info: (...args: any[]) => void;
  warn: (...args: any[]) => void;
  error?: (...args: any[]) => void;
  debug?: (...args: any[]) => void;
}

export interface ToolResult {
  content: Array<{ type: string; text: string }>;
  details?: unknown;
}

export interface PluginTool {
  name: string;
  label?: string;
  description: string;
  parameters: unknown;
  execute(toolCallId: string, params: unknown): Promise<ToolResult>;
}

/** Commander-style command builder handed to registerCli callbacks. */
export interface CliCommand {
  command(name: string): CliCommand;
  description(text: string): CliCommand;
  argument(name: string, description?: string): CliCommand;
  option(flags: string, description?: string, defaultValue?: string): CliCommand;
  action(handler: (...args: any[]) => unknown): CliCommand;
  outputHelp(): void;
}

export interface OpenClawPluginApi {
  pluginConfig?: unknown;
  logger: PluginLogger;
  runtime: { tools: { registerMemoryCli(program: CliCommand): void } };
  registerTool(tool: PluginTool | ((toolCtx: any) => PluginTool), opts?: { name?: string }): void;
  /** Present on OpenClaw v2026.3.7+ only. */
  registerContextEngine?: (id: string, factory: () => unknown) => void;
  registerCli(register: (ctx: { program: CliCommand }) => void, opts?: { commands?: string[] }): void;
  registerCommand(command: { name: string; description: string; handler: (...args: any[]) => unknown }): void;
  registerService(service: { id: string; start(): unknown; stop(): unknown }): void;
  on(hook: string, handler: (...args: any[]) => unknown): void;
  resolvePath(p: string): string;
}
