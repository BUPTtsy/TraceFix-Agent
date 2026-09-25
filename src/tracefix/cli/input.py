import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style

from tracefix.cli.registry import COMMANDS


class CommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        typed = document.text_before_cursor
        if not typed.startswith('/'):
            return
        parts = typed.split(' ')
        subcommands = {'/projects': ['list', 'show', 'use', 'create', 'configure'], '/scope': ['list', 'show', 'use'],
                       '/remote': ['show', 'set', 'clear'],
                       '/runs': ['list', 'show', 'logs', 'trace', 'sources', 'export', 'remember', 'continue'],
                       '/knowledge': ['list', 'show', 'search', 'import', 'new', 'edit', 'enable', 'disable', 'export'],
                       '/memory': ['search', 'sources'], '/mode': ['test', 'repair', 'chat']}
        if len(parts) == 2 and parts[0] in subcommands:
            for name in subcommands[parts[0]]:
                if name.startswith(parts[1]):
                    yield Completion(name, start_position=-len(parts[1]), display=f'{parts[0]} {name}')
            return
        if any(character.isspace() for character in typed):
            return
        for command in COMMANDS:
            name = '/'+command.name
            if name.startswith(typed):
                yield Completion(name, start_position=-len(typed), display=command.usage,
                                 display_meta=command.help)


def make_prompt(renderer, scope, mode, history_path=None, **options):
    bindings = KeyBindings()

    @bindings.add('enter')
    def submit(event):
        buffer = event.current_buffer
        if buffer.complete_state:
            completion = (buffer.complete_state.current_completion
                          or (buffer.complete_state.completions[0]
                              if buffer.complete_state.completions else None))
            if completion:
                buffer.apply_completion(completion)
                buffer.cancel_completion()
                return
            buffer.cancel_completion()
        buffer.validate_and_handle()

    @bindings.add('escape', 'enter')
    def newline(event):
        event.current_buffer.insert_text('\n')

    @bindings.add('tab')
    def complete(event):
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=True)

    def message():
        if renderer.plain:
            return 'TraceFix > '
        width = max(10, get_app().output.get_size().columns-1)
        return [('class:separator', '─'*width+'\n'), ('class:accent', '❯ ')]

    def toolbar():
        return renderer.toolbar(scope(), mode()) + [
            ('class:hint', '\n / 命令   Enter 发送   Alt+Enter 换行   Ctrl+C 取消   Ctrl+D 退出')]

    if history_path is not None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        message=message, history=FileHistory(str(history_path)) if history_path else InMemoryHistory(),
        completer=CommandCompleter(), key_bindings=bindings, multiline=True,
        prompt_continuation=lambda width, line_number, wrap_count: '  ',
        complete_while_typing=True, reserve_space_for_menu=5,
        bottom_toolbar=None if renderer.plain else toolbar, refresh_interval=0.25,
        style=Style.from_dict({'accent': '#da7756 bold', 'separator': '#666666',
                              'status': '#c0b8b0', 'hint': '#888888',
                              'bottom-toolbar': 'noreverse',
                              'completion-menu.completion': 'bg:#292622 #d7cec4',
                              'completion-menu.completion.current': 'bg:#da7756 #181818',
                              'completion-menu.meta.completion': 'bg:#292622 #b5aca2',
                              'completion-menu.meta.completion.current': 'bg:#504239 #ffffff'}),
        color_depth=ColorDepth.DEPTH_1_BIT if renderer.plain else None,
        **options)


async def read_plain():
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        raise EOFError
    return line.rstrip('\r\n')
