"""The package's front door, which is read far more often than it is run.

``help(mode_sdk)`` and every IDE hover show the module docstring first, and its
example is the first code anyone copies. One that does not compile teaches a shape
the language does not have, and nothing else in the suite would notice.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

import pytest

import mode_sdk
from mode_sdk import resources


def test_the_front_door_example_is_code_a_reader_can_paste():
    example = (mode_sdk.__doc__ or "").split("\n", 2)[-1]
    compile(example, "mode_sdk.__doc__", "exec")


# --- __all__: the list a star-import obeys and a reader trusts ---


@pytest.mark.parametrize("module", [mode_sdk, resources], ids=["mode_sdk", "mode_sdk.resources"])
def test_every_name_in_all_is_actually_importable(module: ModuleType):
    """``__all__`` is the only surface a star-import creates, so a stale entry there is an
    ImportError in someone else's code and a green test suite here.
    """
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert missing == []


@pytest.mark.parametrize("module", [mode_sdk, resources], ids=["mode_sdk", "mode_sdk.resources"])
def test_all_carries_no_duplicate(module: ModuleType):
    """Ordering is ruff's job (RUF022 is selected and enforces the isort-style sort); a
    repeated entry is the failure it does not catch.
    """
    assert len(module.__all__) == len(set(module.__all__))


def test_a_star_import_binds_exactly_what_all_promises():
    namespace: dict[str, object] = {}
    exec("from mode_sdk import *", namespace)  # the behaviour under test
    bound = sorted(name for name in namespace if name != "__builtins__")
    assert bound == sorted(mode_sdk.__all__)


def test_the_types_a_caller_receives_or_constructs_are_all_exported():
    """Step 3 introduced value types that public methods hand back or take -- a caller who
    cannot name ``RunResults`` cannot annotate the variable they just assigned.
    """
    for name in (
        "RunResults",
        "RunState",
        "QuerySpec",
        "PdfExport",
        "PdfExportState",
        "CronSpec",
        "MAX_PER_PAGE",
    ):
        assert name in mode_sdk.__all__


def test_no_resource_module_hides_a_public_value_type_from_the_package():
    """Anything a resource module defines that is not a ``*Resource`` and not private is a
    type a caller can end up holding; it belongs on one of the two ``__all__`` lists.
    """
    exported = set(mode_sdk.__all__) | set(resources.__all__)
    hidden = []
    for info in pkgutil.iter_modules(resources.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{resources.__name__}.{info.name}")
        for name, value in vars(module).items():
            if name.startswith("_") or name.endswith("Ref") or name in exported:
                continue
            if inspect.isclass(value) and value.__module__ == module.__name__:
                hidden.append(f"{info.name}.{name}")
    assert hidden == []


def _public_classes() -> list[tuple[str, type]]:
    """Every class a caller reaches: the two entry points and all resource namespaces.

    Discovered rather than listed. The two hand-written tuples this replaced named four
    resource modules between them and missed ``distribution`` and ``datasets`` entirely --
    a third of the surface, and the third that step 3 had just rewritten. A tuple is
    extended the day someone remembers it; ``iter_modules`` covers a new module the day
    it is added.
    """
    found: list[tuple[str, type]] = [("Mode", mode_sdk.Mode), ("Discovery", mode_sdk.Discovery)]
    for info in pkgutil.iter_modules(resources.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{resources.__name__}.{info.name}")
        found += [
            (f"{info.name}.{name}", value)
            for name, value in vars(module).items()
            if inspect.isclass(value) and value.__module__ == module.__name__
        ]
    return found


def _public_parameters() -> list[tuple[str, inspect.Parameter]]:
    """Every parameter a caller can pass to a public method of one of those classes."""
    found = []
    for class_name, cls in _public_classes():
        for name, attribute in vars(cls).items():
            if name.startswith("_") or not callable(attribute):
                continue
            for parameter in inspect.signature(attribute).parameters.values():
                found.append((f"{class_name}.{name}", parameter))
    return found


def test_the_walk_reaches_every_resource_module_and_both_entry_points():
    """The guard on the two tests below, which are only worth their docstrings if the set
    they walk is the whole surface. A module that imports but contributes no class means
    the discovery silently narrowed.
    """
    reached = {where.split(".")[0] for where, _ in _public_parameters()}
    expected = {info.name for info in pkgutil.iter_modules(resources.__path__)}
    expected = {name for name in expected if not name.startswith("_")}
    assert expected | {"Mode", "Discovery"} == reached


def test_no_public_method_takes_untyped_kwargs():
    """A ``**kwargs`` bag turns a misspelled filter into a query parameter Mode ignores
    instead of a TypeError, and can advertise parameters that are a deterministic 500.

    Annotations read as strings because these modules use ``from __future__ import
    annotations``.
    """
    offenders = [
        f"{where}({parameter})"
        for where, parameter in _public_parameters()
        if parameter.kind in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL)
        or "Any" in str(parameter.annotation)
    ]
    assert offenders == []


def test_only_the_identifiers_on_those_methods_are_positional():
    """Parent identifiers and required attributes first, every option keyword-only. An
    optional positional is where ``update(space, name, description)`` becomes a
    transposition that still type-checks, since all three are strings.
    """
    offenders = [
        f"{where}({parameter})"
        for where, parameter in _public_parameters()
        if parameter.kind is parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is not parameter.empty
    ]
    assert offenders == []
