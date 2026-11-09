"""Small, safe parser for the book.yaml subset exported by this module."""

import json


class BookYamlError(ValueError):
    pass


def _scalar(value, line_number):
    value = value.strip()
    if not value:
        raise BookYamlError('Missing value on line %s.' % line_number)
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BookYamlError('Invalid quoted value on line %s.' % line_number) from exc
        if not isinstance(parsed, str):
            raise BookYamlError('Expected text on line %s.' % line_number)
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise BookYamlError('Invalid quoted value on line %s.' % line_number)
        return value[1:-1].replace("''", "'")
    if value[0] in '!&*{[':
        raise BookYamlError('YAML tags, aliases and inline collections are not supported (line %s).' % line_number)
    return value.split(' #', 1)[0].strip()


def _property(content, line_number):
    if ':' not in content:
        raise BookYamlError('Expected a key and value on line %s.' % line_number)
    key, value = content.split(':', 1)
    key = key.strip()
    if not key:
        raise BookYamlError('Missing key on line %s.' % line_number)
    return key, value.strip()


def parse_book_yaml(text):
    """Parse only the documented book configuration structure.

    Deliberately excludes YAML tags, anchors, aliases, nested maps and other
    general YAML features so uploads never instantiate arbitrary objects.
    """
    if not isinstance(text, str):
        raise BookYamlError('The uploaded book must be UTF-8 text.')
    result = {}
    sheets = []
    current = None
    in_instruments = False
    saw_sheets = False
    allowed_top = {'history_days', 'history_keep_days', 'commodity', 'market'}
    allowed_sheet = {'area', 'commodity', 'market'}

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if '\t' in raw_line:
            raise BookYamlError('Tabs are not allowed (line %s).' % line_number)
        if not raw_line.strip() or raw_line.lstrip().startswith('#'):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(' '))
        content = raw_line[indent:].rstrip()

        if indent == 0:
            current = None
            in_instruments = False
            if content == 'sheets:':
                saw_sheets = True
                continue
            if content == 'sheets: []':
                saw_sheets = True
                continue
            key, value = _property(content, line_number)
            if key not in allowed_top:
                raise BookYamlError('Unsupported top-level key %r on line %s.' % (key, line_number))
            if key in result:
                raise BookYamlError('Duplicate key %r on line %s.' % (key, line_number))
            result[key] = _scalar(value, line_number)
            continue

        if indent == 2 and content.startswith('- '):
            if not saw_sheets:
                raise BookYamlError('Define sheets before line %s.' % line_number)
            key, value = _property(content[2:].strip(), line_number)
            if key != 'name':
                raise BookYamlError('Each sheet must start with a name (line %s).' % line_number)
            current = {'name': _scalar(value, line_number), 'instruments': []}
            sheets.append(current)
            in_instruments = False
            continue

        if indent == 4 and current is not None:
            key, value = _property(content, line_number)
            if key == 'instruments':
                if value:
                    raise BookYamlError('Instruments must be a list (line %s).' % line_number)
                in_instruments = True
                continue
            if key not in allowed_sheet:
                raise BookYamlError('Unsupported sheet key %r on line %s.' % (key, line_number))
            if key in current:
                raise BookYamlError('Duplicate sheet key %r on line %s.' % (key, line_number))
            current[key] = _scalar(value, line_number)
            in_instruments = False
            continue

        if indent == 6 and current is not None and in_instruments and content.startswith('- '):
            current['instruments'].append(_scalar(content[2:], line_number))
            continue

        raise BookYamlError('Unsupported YAML structure on line %s.' % line_number)

    if not saw_sheets:
        raise BookYamlError('The file does not contain a sheets list.')
    if len(sheets) > 100:
        raise BookYamlError('A book can contain at most 100 sheets.')
    if sum(len(sheet['instruments']) for sheet in sheets) > 500:
        raise BookYamlError('A book can contain at most 500 instruments.')
    result['sheets'] = sheets
    return result
