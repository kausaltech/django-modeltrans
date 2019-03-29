import sys

from django.utils.translation import get_language as _get_language


def get_language():
    """
    Return an active language code that is guaranteed to be in settings.LANGUAGES

    (Django does not seem to guarantee this for us.)
    """
    from .conf import get_available_languages, get_default_language

    lang = _get_language()
    if lang in get_available_languages():
        return lang
    return get_default_language()


def split_translated_fieldname(field_name):
    _pos = field_name.rfind("_")
    return (field_name[0:_pos], field_name[_pos + 1 :])


def build_localized_fieldname(field_name, lang):
    if lang == "id":
        # The 2-letter Indonesian language code is problematic with the
        # current naming scheme as Django foreign keys also add "id" suffix.
        lang = "ind"
    return str("{}_{}".format(field_name, lang.replace("-", "_")))


PY3 = sys.version_info[0] == 3

if PY3:
    string_types = str
    from io import StringIO  # noqa
else:
    string_types = basestring
    from StringIO import StringIO  # noqa
