"""Catalog extraction retains the original executor facade and immutable templates."""
import copy

from factory.agent_runtime import builtin_tools as bt, builtin_tool_catalog as catalog
from factory.agent_runtime.tool_validation import argument_error


def test_catalog_schemas_and_names_are_reexported_by_original_module():
    names=[key for key in vars(catalog) if key.endswith('TOOL_DEFINITION')]
    assert len(names)==14
    for name in names:
        value=getattr(catalog,name)
        assert getattr(bt,name) is value
        assert bt.is_builtin_tool(value['function']['name'])
    assert bt.COMMAND_TOOL_NAMES is catalog.COMMAND_TOOL_NAMES
    assert bt.SELECTABLE_BUILTIN_TOOL_NAMES is catalog.SELECTABLE_BUILTIN_TOOL_NAMES


def test_injection_never_mutates_catalog_templates():
    before=copy.deepcopy(catalog.RUN_COMMAND_TOOL_DEFINITION)
    tools,_=bt.inject_builtin_tools([],{},include_run_command=True,default_command_shell='cmd')
    names={item['function']['name'] for item in tools}
    assert {'run_command','poll_command','write_stdin','stop_command'}<=names
    tools[0]['function']['description']='client change'
    assert catalog.RUN_COMMAND_TOOL_DEFINITION==before


def test_schema_validation_still_uses_facade_definitions():
    assert argument_error('write_stdin',{'job_id':'bg_x','eof':'yes'})
    assert argument_error('search_files',{'pattern':'x','count_total':1})
    assert argument_error('list_files',{'respect_gitignore':'true'})
