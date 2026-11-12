import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# The tests write their fixtures as UTF-8 (net names with « », µ). Path's default encoding is the locale's
# (cp1252 / cp932 on Windows), so make UTF-8 the default for the tests only. The package itself names the encoding
# of every file it reads or writes; test_source_names_encodings checks that.
_write_text, _read_text = Path.write_text, Path.read_text


def _utf8_write_text(self, data, encoding=None, errors=None, newline=None):
    return _write_text(self, data, encoding=encoding or "utf-8", errors=errors, newline=newline)


def _utf8_read_text(self, encoding=None, errors=None):
    return _read_text(self, encoding=encoding or "utf-8", errors=errors)


Path.write_text, Path.read_text = _utf8_write_text, _utf8_read_text
