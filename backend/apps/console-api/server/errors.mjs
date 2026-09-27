export function errorWithContext(context, error) {
  return Object.assign(new Error(`${context}：${error instanceof Error ? error.message : String(error)}`, {cause: error}),
    {code: error?.code, status: error?.status});
}
