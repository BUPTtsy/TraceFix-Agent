export interface Capabilities {
  rich: boolean;
  color: boolean;
  columns: number;
  rows: number;
}

export function capabilities(env: Record<string, string | undefined> = process.env,
                            stdin: {isTTY?: boolean} = process.stdin,
                            stdout: {isTTY?: boolean; columns?: number; rows?: number} = process.stdout): Capabilities {
  const tty = Boolean(stdin.isTTY && stdout.isTTY);
  const degraded = Boolean(env.NO_COLOR) || env.TERM === 'dumb' || env.TRACEFIX_CLI_PLAIN === '1';
  return {
    rich: tty && !degraded,
    color: tty && !degraded,
    columns: Math.max(20, stdout.columns || 80),
    rows: Math.max(4, stdout.rows || 24),
  };
}
