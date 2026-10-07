"""Compatibility exports for persisted reasoning records.

Transport, structured output and tool execution are owned by PydanticAI and
the model protocol boundary. This module does not implement a second loop.
"""
import copy


def reasoning_records(body):
    records = []
    if not isinstance(body, dict) or not isinstance(body.get('choices'), list):
        return records
    for index, choice in enumerate(body['choices']):
        message = choice.get('message') if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        fields = {field: copy.deepcopy(message[field])
                  for field in ('reasoning_content', 'reasoning')
                  if message.get(field) is not None}
        if fields:
            records.append({'choice_index': choice.get('index', index), **fields})
    return records
