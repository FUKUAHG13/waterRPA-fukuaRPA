"""Guard tests for the modular split of WorkflowEditorController.

WorkflowEditorController is composed of responsibility mixins. The step-edit
responsibility was split further into ``StepClipboardMixin`` and
``StepReorderReferenceMixin`` (phase 6) on top of the earlier ``WorkflowUndoMixin``
/ ``WorkflowStepEditMixin`` / ``WorkflowGroupMixin`` / ``WorkflowContextMixin``
split. These tests assert the split preserves the public surface: MRO order,
no duplicate method definitions across mixins, that the hard-constraint method
names stay resolvable on the controller, that ``WorkflowStepEditMixin`` stays
within its God-Class budget, and that the facade keeps re-exporting shared
symbols.

Run headless with ``QT_QPA_PLATFORM=offscreen`` (the modules import PySide6).
"""
import importlib

MOD_NAME = "fukua_rpa.ui.controllers.workflow_editor"
MIXIN_CLASSES = [
    ("workflow_undo", "WorkflowUndoMixin"),
    ("step_clipboard_mixin", "StepClipboardMixin"),
    ("step_reorder_reference_mixin", "StepReorderReferenceMixin"),
    ("workflow_step_edit", "WorkflowStepEditMixin"),
    ("workflow_group", "WorkflowGroupMixin"),
    ("workflow_search", "WorkflowSearchMixin"),
    ("workflow_context", "WorkflowContextMixin"),
]
REQUIRED_METHODS = [
    # hard-constraint names resolved by string-key dispatch in task_list.py
    "copy_selected_row",
    "cut_selected_row",
    "move_selected_task",
    # called by WorkflowGroupMixin through the controller MRO
    "_restore_structured_tasks",
    # clipboard cluster
    "paste_row_after_selected",
    "has_paste_content",
    # reorder cluster
    "handle_tasks_reordered",
    "_clear_inbound_references",
    # base editing cluster
    "add_row",
    "del_row",
    "delete_selected_rows",
]
SHARED_SYMBOLS = [
    "_snapshot_hash",
    "parse_task_clipboard",
    "MAX_UNDO_DEPTH",
    "MAX_TASK_CLIPBOARD_BYTES",
    "DELETE_LOG_CHUNK_CHARS",
    "_CURRENT_SELECTION",
]
# WorkflowStepEditMixin must stay at/under this after the phase-6 split.
GOD_CLASS_BUDGET = 18


def _controller_class():
    return importlib.import_module(MOD_NAME).WorkflowEditorController


def _mixin_class(mod_name, cls_name):
    return getattr(
        importlib.import_module(f"fukua_rpa.ui.controllers.{mod_name}"), cls_name
    )


def test_mro_order():
    cls = _controller_class()
    assert [c.__name__ for c in cls.__mro__] == [
        "WorkflowEditorController",
        "WorkflowUndoMixin",
        "StepClipboardMixin",
        "StepReorderReferenceMixin",
        "WorkflowStepEditMixin",
        "WorkflowGroupMixin",
        "WorkflowSearchMixin",
        "WorkflowContextMixin",
        "object",
    ]


def test_required_methods_resolvable():
    cls = _controller_class()
    for name in REQUIRED_METHODS:
        assert callable(getattr(cls, name, None)), (
            f"{name} is not resolvable on WorkflowEditorController"
        )


def test_no_duplicate_methods_across_mixins():
    seen = {}
    for mod_name, cls_name in MIXIN_CLASSES:
        mixin = _mixin_class(mod_name, cls_name)
        for name, value in vars(mixin).items():
            if name.startswith("__"):
                continue
            if callable(value) or isinstance(value, property):
                seen.setdefault(name, []).append(cls_name)
    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    assert not duplicates, f"duplicate method definitions: {duplicates}"


def test_step_edit_mixin_within_budget():
    mixin = _mixin_class("workflow_step_edit", "WorkflowStepEditMixin")
    methods = [
        name
        for name, value in vars(mixin).items()
        if not name.startswith("__")
        and (callable(value) or isinstance(value, property))
    ]
    assert len(methods) <= GOD_CLASS_BUDGET, (
        f"WorkflowStepEditMixin has {len(methods)} methods, "
        f"budget is {GOD_CLASS_BUDGET}"
    )


def test_shared_symbols_reexported_by_facade():
    mod = importlib.import_module(MOD_NAME)
    for sym in SHARED_SYMBOLS + ["WorkflowEditorController"]:
        assert hasattr(mod, sym), sym
    for sym in SHARED_SYMBOLS + ["WorkflowEditorController"]:
        assert sym in mod.__all__, f"{sym} missing from __all__"


def test_host_is_property():
    cls = _controller_class()
    assert isinstance(cls.host, property)
