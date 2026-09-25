---
name: gui-reproduce
version: "1.0.0"
description: Reset the complete sandbox and replay the frozen action plan.
phases: ['REPRODUCE']
---

# gui-reproduce

Input: trusted RunState and authorized artifact references.
Output: one typed action or verification result.

1. Revalidate Scope, phase and budget.
2. Reset the complete sandbox and replay the frozen action plan.
3. Persist evidence before proposing any transition.
4. Let the deterministic gate decide whether the phase may end.

Stop when the budget expires, required evidence is missing, the source hash changes, or authorization is revoked.

This file describes a workflow. It grants no tool, path, network or approval permission.
