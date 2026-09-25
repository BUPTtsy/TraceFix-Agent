export function errorWithContext(context: string, error: unknown): Error {
  return new Error(`${context}：${error instanceof Error ? error.message : String(error)}`, {cause: error});
}
