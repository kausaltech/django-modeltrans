from django.core.exceptions import ImproperlyConfigured
from django.db.models import F, JSONField, fields
from django.db.models.fields.json import KeyTextTransform
from django.db.models.expressions import Case, When
from django.db.models.functions import Cast, Coalesce
from django.utils.translation import gettext

from .conf import get_default_language, get_fallback_chain, get_modeltrans_setting
from .utils import (
    FallbackTransform,
    build_localized_fieldname,
    get_instance_field_value,
    get_language,
)

SUPPORTED_FIELDS = (fields.CharField, fields.TextField)

GLOBAL_DEFAULT_LANGUAGE = get_default_language()


def translated_field_factory(original_field, language=None, default_language_field=None, *args, **kwargs):
    if not isinstance(original_field, SUPPORTED_FIELDS):
        raise ImproperlyConfigured(
            "{} is not supported by django-modeltrans.".format(original_field.__class__.__name__)
        )

    class Specific(TranslatedVirtualField, original_field.__class__):
        pass

    Specific.__name__ = "Translated{}".format(original_field.__class__.__name__)

    return Specific(original_field, language, *args, default_language_field=default_language_field, **kwargs)


class TranslatedVirtualField:
    """
    A field representing a single field translated to a specific language.

    Arguments:
        original_field: The original field to be translated
        language: The language to translate to, or `None` to use the default language (see `default_language_field`)
        default_language_field: Name of the field that contains the default language for this field, or `None` to track
            the current active Django language
    """

    # Implementation inspired by HStoreVirtualMixin from:
    # https://github.com/djangonauts/django-hstore/blob/master/django_hstore/virtual.py

    def __init__(self, original_field, language=None, *args, default_language_field=None, **kwargs):
        # TODO: this feels like a big hack.
        self.__dict__.update(original_field.__dict__)

        self.original_field = original_field
        self.language = language
        self.default_language_field = default_language_field

        self.blank = kwargs["blank"]
        self.null = kwargs["null"]

        self.concrete = False
        self._help_text = kwargs.pop("help_text", None)

    @property
    def original_name(self):
        return self.original_field.name

    @property
    def help_text(self):
        if self._help_text is not None:
            return self._help_text

        if get_modeltrans_setting("MODELTRANS_ADD_FIELD_HELP_TEXT") and self.language is None:
            return gettext("current language: {}").format(get_language())

    def contribute_to_class(self, cls, name):
        self.model = cls

        self.attname = name
        self.name = name
        self.column = None

        # Use a translated verbose name:
        translated_field_name = gettext(self.original_field.verbose_name)
        if self.language is not None:
            translated_field_name += " ({})".format(self.language.upper())
        self.verbose_name = translated_field_name

        setattr(cls, name, self)
        cls._meta.add_field(self, private=True)

    def db_type(self, connection):
        return None

    def get_instance_fallback_chain(self, instance, language):
        """
        Return the fallback chain for the instance.

        Most of the time, it is just the configured fallback chain, but if the per-record-fallback feature
        is used, the value of the field is added (if not None).
        """
        default = get_fallback_chain(language)

        i18n_field = instance._meta.get_field("i18n")
        if i18n_field.fallback_language_field:
            record_fallback_language = get_instance_field_value(
                instance, i18n_field.fallback_language_field
            )

            if record_fallback_language:
                return (record_fallback_language, *default)

        return default

    def __get__(self, instance, instance_type=None):
        # This method is apparently called with instance=None from django.
        # django-hstor raises AttributeError here, but that doesn't solve our problem.
        if instance is None:
            return

        if "i18n" in instance.get_deferred_fields():
            raise ValueError(
                "Getting translated values on a model fetched with defer('i18n') is not supported."
            )

        language = self.get_language()
        original_value = getattr(instance, self.original_name)
        default_language = self.get_default_language(instance)
        if language == default_language and original_value:
            return original_value

        # Make sure we test for containment in a dict, not in None
        if instance.i18n is None:
            instance.i18n = {}

        field_name = build_localized_fieldname(self.original_name, language)

        # Just return the value if this is an explicit field (<name>_<lang>)
        if self.language is not None:
            return instance.i18n.get(field_name)

        # This is the _i18n version of the field, and the current language is not available,
        # so we walk the fallback chain:
        for fallback_language in (language,) + self.get_instance_fallback_chain(instance, language):
            if fallback_language == default_language:
                if original_value:
                    return original_value
                else:
                    continue

            field_name = build_localized_fieldname(self.original_name, fallback_language)
            if field_name in instance.i18n and instance.i18n[field_name]:
                return instance.i18n.get(field_name)

        # finally, return the original field if all else fails.
        return getattr(instance, self.original_name)

    def __set__(self, instance, value):
        if instance.i18n is None:
            instance.i18n = {}

        language = self.get_language()

        default_language = self.get_default_language(instance)
        if language == default_language:
            setattr(instance, self.original_name, value)
        else:
            field_name = build_localized_fieldname(self.original_name, language)

            # if value is None, remove field from `i18n`.
            if value is None:
                instance.i18n.pop(field_name, None)
            else:
                instance.i18n[field_name] = value

    def get_field_name(self):
        """
        Returns the field name for the current virtual field.

        The field name is ``<original_field_name>_<language>`` in case of a specific
        translation or ``<original_field_name>_i18n`` for the currently active language.
        """
        if self.language is None:
            lang = "i18n"
        else:
            lang = self.get_language()

        return build_localized_fieldname(self.original_name, lang)

    def get_language(self):
        """
        Returns the language for this field.

        In case of an explicit language (title_en), it returns "en", in case of
        `title_i18n`, it returns the currently active Django language.
        """
        return self.language if self.language is not None else get_language()

    def output_field(self):
        """
        The type of field used to Cast/Coalesce to.

        Mainly because a max_length argument is required for CharField
        until this PR is merged: https://github.com/django/django/pull/8758
        """
        Field = self.original_field.__class__
        if isinstance(self.original_field, fields.CharField):
            return Field(max_length=self.original_field.max_length)

        return Field()

    def _localized_lookup(self, language, bare_lookup):
        if not self.default_language_field and language == GLOBAL_DEFAULT_LANGUAGE:
            return bare_lookup.replace(self.name, self.original_name)

        # When accessing a table directly, the i18_lookup will be just "i18n", while following relations
        # they are in the lookup first.
        i18n_lookup = bare_lookup.replace(self.name, "i18n")

        # To support per-row fallback languages, an F-expression is passed as language parameter.
        if isinstance(language, F):
            # abuse build_localized_fieldname without language to get "<field>_"
            field_prefix = build_localized_fieldname(self.original_name, "")
            return FallbackTransform(field_prefix, language, i18n_lookup)
        elif self.default_language_field:
            default_value_field = bare_lookup.replace(self.name, self.original_name)
            return Case(
                When(**{self.default_language_field: language}, then=default_value_field),
                default=KeyTextTransform(
                    build_localized_fieldname(self.original_name, language), i18n_lookup
                ),
                output_field=self.output_field(),
            )
        else:
            return KeyTextTransform(
                build_localized_fieldname(self.original_name, language), i18n_lookup
            )

    def as_expression(self, bare_lookup, fallback=True):
        """
        Compose an expression to get the value for this virtual field in a query.
        """
        language = self.get_language()
        if not self.default_language_field and language == GLOBAL_DEFAULT_LANGUAGE:
            return F(self._localized_lookup(language, bare_lookup))

        if not fallback:
            i18n_lookup = self._localized_lookup(language, bare_lookup)
            return Cast(i18n_lookup, self.output_field())

        fallback_chain = get_fallback_chain(language)
        # First, add the current language to the list of lookups
        lookups = [self._localized_lookup(language, bare_lookup)]

        # Optionnally add the lookup for the per-row fallback language
        i18n_field = self.model._meta.get_field("i18n")
        if i18n_field.fallback_language_field:
            lookups.append(
                self._localized_lookup(F(i18n_field.fallback_language_field), bare_lookup)
            )

        # and now, add the list of fallback languages to the lookup list
        for fallback_language in fallback_chain:
            lookups.append(self._localized_lookup(fallback_language, bare_lookup))

        # Add the original field as a fallback (might not be in the fallback chain)
        lookups.append(bare_lookup.replace(self.name, self.original_name))

        return Coalesce(*lookups, output_field=self.output_field())

    def get_default_language(self, instance):
        if not self.default_language_field:
            return GLOBAL_DEFAULT_LANGUAGE
        return get_instance_field_value(instance, self.default_language_field)


class TranslationField(JSONField):
    """
    This model field is used to store the translations in the translated model.

    Arguments:
        fields (iterable): List of model field names to make translatable.
        default_language_field (field name):
            Field of the model containing the language stored in the original
            model fields; use Django main language by default. May contain `__`
            as a field name separator to follow foreign keys.
        required_languages (iterable or dict): List of languages required for the model.
            If a dict is supplied, the keys must be translated field names with the value
            containing a list of required languages for that specific field.
        virtual_fields (bool): If `False`, do not add virtual fields to access
            translated values with.
            Set to `True` during migration from django-modeltranslation to prevent
            collisions with it's database fields while having the `i18n` field available.
        fallback_language_field: If not None, this should be the name of the field containing a
            language code to use as the first language in any fallback chain.
            For example: if you have a model instance with 'nl' as language_code, and set
            fallback_language_field='language_code', 'nl' will always be tried after the current
            language before any other language.
    """

    description = "Translation storage for a model"

    def __init__(
        self,
        fields=None,
        default_language_field=None,
        required_languages=None,
        virtual_fields=True,
        fallback_language_field=None,
        *args,
        **kwargs,
    ):
        self.fields = fields or ()
        self.default_language_field = default_language_field
        self.required_languages = required_languages or ()
        self.virtual_fields = virtual_fields
        self.fallback_language_field = fallback_language_field

        kwargs["editable"] = False
        kwargs["null"] = True
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()

        del kwargs["editable"]
        del kwargs["null"]
        kwargs["fields"] = self.fields
        kwargs["required_languages"] = self.required_languages
        kwargs["virtual_fields"] = self.virtual_fields

        return name, path, args, kwargs

    def get_translated_fields(self):
        """Return a generator for all translated fields."""
        for field in self.model._meta.get_fields():
            if isinstance(field, TranslatedVirtualField):
                yield field

    def contribute_to_class(self, cls, name):
        if name != "i18n":
            raise ImproperlyConfigured('{} must have name "i18n"'.format(self.__class__.__name__))

        super().contribute_to_class(cls, name)
