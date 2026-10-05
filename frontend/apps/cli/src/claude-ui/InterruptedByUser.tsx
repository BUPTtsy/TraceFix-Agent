import * as React from 'react'
import { Text } from './adapter.js'

export function InterruptedByUser(): React.ReactNode {
  return <><Text dimColor>Interrupted </Text><Text dimColor>· What should TraceFix do instead?</Text></>
}
