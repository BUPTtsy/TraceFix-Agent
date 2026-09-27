import argparse
import json
import re
import sys


def argument_message(message):
    argument = re.fullmatch(r'argument (.+?): (.+)', message, re.DOTALL)
    if argument:
        return f'参数 {argument[1]}：{argument_message(argument[2])}'
    prefixes = {
        'the following arguments are required: ': '缺少必填参数：',
        'unrecognized arguments: ': '无法识别的参数：',
        'not allowed with argument ': '不能与此参数同时使用：',
        'one of the arguments ': '以下参数必须选择一项：',
    }
    for source, translated in prefixes.items():
        if message.startswith(source):
            rest = message[len(source):]
            if source == 'one of the arguments ':
                rest = rest.removesuffix(' is required')
            return translated + rest
    choice = re.fullmatch(r'invalid choice: (.+) \(choose from (.+)\)', message, re.DOTALL)
    if choice:
        return f'无效选项：{choice[1]}（可选值：{choice[2]}）'
    invalid = re.fullmatch(r'invalid (.+) value: (.+)', message, re.DOTALL)
    if invalid:
        kind = {'int': '整数', 'float': '数字'}.get(invalid[1], invalid[1])
        return f'无效的{kind}值：{invalid[2]}'
    expected = re.fullmatch(r'expected (\d+) arguments?', message)
    if expected:
        return f'需要 {expected[1]} 个参数值'
    return {
        'expected one argument': '需要一个参数值',
        'expected at least one argument': '至少需要一个参数值',
        'expected at most one argument': '最多接受一个参数值',
        'No closing quotation': '引号未闭合，请检查命令中的引号',
        'No escaped character': '转义符后缺少字符',
    }.get(message, f'参数无效：{message}')


class ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix or '用法：')


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        add_help = kwargs.pop('add_help', True)
        kwargs.setdefault('formatter_class', ChineseHelpFormatter)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = '位置参数'
        self._optionals.title = '可选参数'
        if add_help:
            self.add_argument('-h', '--help', action='help', help='显示此帮助信息并退出')

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f'{self.prog}：{argument_message(message)}\n')


def validation_details(error):
    messages = {
        'missing': '缺少必填字段', 'extra_forbidden': '不允许额外字段',
        'string_type': '必须是字符串', 'int_type': '必须是整数',
        'int_parsing': '无法解析为整数', 'int_from_float': '必须是整数，不能带小数',
        'float_type': '必须是数字', 'float_parsing': '无法解析为数字',
        'bool_type': '必须是布尔值', 'bool_parsing': '无法解析为布尔值',
        'list_type': '必须是列表', 'dict_type': '必须是对象',
        'finite_number': '必须是有限数值', 'json_invalid': 'JSON 格式无效',
        'json_type': 'JSON 输入必须是字符串或字节数据',
        'model_type': '必须是有效的对象或模型实例',
        'model_attributes_type': '必须是可读取字段的对象',
        'string_too_short': '字符串长度不能小于 {min_length}',
        'string_too_long': '字符串长度不能大于 {max_length}',
        'too_short': '元素数量不能少于 {min_length}',
        'too_long': '元素数量不能超过 {max_length}',
        'greater_than': '必须大于 {gt}', 'greater_than_equal': '必须大于或等于 {ge}',
        'less_than': '必须小于 {lt}', 'less_than_equal': '必须小于或等于 {le}',
        'literal_error': '必须是以下值之一：{expected}',
        'enum': '必须是以下枚举值之一：{expected}',
        'string_pattern_mismatch': '必须匹配格式：{pattern}',
    }
    result = []
    for detail in error.errors(include_input=False, include_url=False):
        kind, context = detail['type'], detail.get('ctx', {})
        if kind == 'value_error':
            message = str(context.get('error', detail['msg'])).removeprefix('Value error, ')
            if not re.search(r'[\u4e00-\u9fff]', message):
                message = '值无效：' + message
        else:
            template = messages.get(kind)
            message = template.format(**context) if template else f'字段校验失败：{detail["msg"]}'
        result.append({'field': list(detail['loc']), 'type': kind, 'message': message})
    return result


def error_message(error):
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        details = validation_details(error)
        return '数据校验失败：' + json.dumps(details, ensure_ascii=False)
    if isinstance(error, json.JSONDecodeError):
        return f'JSON 格式无效（第 {error.lineno} 行，第 {error.colno} 列）'
    if isinstance(error, KeyError):
        return f'缺少字段或记录：{error}'
    message = str(error)
    if message in {'No closing quotation', 'No escaped character'}:
        return argument_message(message)
    if re.search(r'[\u4e00-\u9fff]', message):
        return message
    for kind, description in (
        (FileNotFoundError, '找不到文件或目录'), (FileExistsError, '文件或目录已存在'),
        (PermissionError, '权限不足或操作被拒绝'), (TimeoutError, '操作超时'),
        (ConnectionError, '连接失败'), (UnicodeError, '文本编码无效'),
        (OSError, '系统操作失败'), (ValueError, '输入值无效'),
        (TypeError, '数据类型无效'), (RuntimeError, '运行失败'),
    ):
        if isinstance(error, kind):
            return description + (f'：{message}' if message else '')
    return f'操作失败（{type(error).__name__}）' + (f'：{message}' if message else '')
