"""
=========================================
:mod:`flytekit.core.python_function_task`
=========================================

.. currentmodule:: flytekit.core.python_function_task

.. autosummary::
   :toctree: generated/

   PythonFunctionTask
   PythonInstanceTask

"""


from abc import ABC
from collections import OrderedDict
from enum import Enum
from typing import Any, Callable, List, Optional, TypeVar, Union

from flytekit.configuration import SerializationSettings
from flytekit.configuration.default_images import DefaultImages
from flytekit.core.base_task import Task, TaskResolverMixin
from flytekit.core.context_manager import ExecutionState, FlyteContext, FlyteContextManager
from flytekit.core.docstring import Docstring
from flytekit.core.interface import transform_function_to_interface
from flytekit.core.python_auto_container import PythonAutoContainerTask, default_task_resolver
from flytekit.core.tracker import extract_task_module, is_functools_wrapped_module_level, isnested, istestfunction
from flytekit.core.workflow import (
    PythonFunctionWorkflow,
    WorkflowFailurePolicy,
    WorkflowMetadata,
    WorkflowMetadataDefaults,
)
from flytekit.exceptions import scopes as exception_scopes
from flytekit.loggers import logger
from flytekit.models import dynamic_job as _dynamic_job
from flytekit.models import literals as _literal_models
from flytekit.models import task as task_models
from flytekit.models.admin import workflow as admin_workflow_models

T = TypeVar("T")


class PythonInstanceTask(PythonAutoContainerTask[T], ABC):
    """
    This class should be used as the base class for all Tasks that do not have a user defined function body, but have
    a platform defined execute method. (Execute needs to be overridden). This base class ensures that the module loader
    will invoke the right class automatically, by capturing the module name and variable in the module name.

    .. code-block: python

        x = MyInstanceTask(name="x", .....)

        # this can be invoked as
        x(a=5) # depending on the interface of the defined task

    """

    def __init__(
        self,
        name: str,
        task_config: T,
        task_type: str = "python-task",
        task_resolver: Optional[TaskResolverMixin] = None,
        **kwargs,
    ):
        """
        Please see class level documentation.
        """
        super().__init__(name=name, task_config=task_config, task_type=task_type, task_resolver=task_resolver, **kwargs)


class PythonFunctionTask(PythonAutoContainerTask[T]):
    """
    A Python Function task should be used as the base for all extensions that have a python function. It will
    automatically detect interface of the python function and when serialized on the hosted Flyte platform handles the
    writing execution command to execute the function

    It is advised this task is used using the @task decorator as follows

    .. code-block: python

        @task
        def my_func(a: int) -> str:
           ...

    In the above code, the name of the function, the module, and the interface (inputs = int and outputs = str) will be
    auto detected.
    """

    class ExecutionBehavior(Enum):
        DEFAULT = 1
        DYNAMIC = 2

    def __init__(
        self,
        task_config: T,
        task_function: Callable,
        task_type="python-task",
        ignore_input_vars: Optional[List[str]] = None,
        execution_mode: ExecutionBehavior = ExecutionBehavior.DEFAULT,
        task_resolver: Optional[TaskResolverMixin] = None,
        **kwargs,
    ):
        """
        :param T task_config: Configuration object for Task. Should be a unique type for that specific Task
        :param Callable task_function: Python function that has type annotations and works for the task
        :param Optional[List[str]] ignore_input_vars: When supplied, these input variables will be removed from the interface. This
                                  can be used to inject some client side variables only. Prefer using ExecutionParams
        :param Optional[ExecutionBehavior] execution_mode: Defines how the execution should behave, for example
            executing normally or specially handling a dynamic case.
        :param str task_type: String task type to be associated with this Task
        """
        if task_function is None:
            raise ValueError("TaskFunction is a required parameter for PythonFunctionTask")
        self._native_interface = transform_function_to_interface(task_function, Docstring(callable_=task_function))
        mutated_interface = self._native_interface.remove_inputs(ignore_input_vars)
        name, _, _, _ = extract_task_module(task_function)
        super().__init__(
            task_type=task_type,
            name=name,
            interface=mutated_interface,
            task_config=task_config,
            task_resolver=task_resolver,
            **kwargs,
        )

        if self._task_resolver is default_task_resolver:
            # The default task resolver can't handle nested functions
            # TODO: Consider moving this to a can_handle function or something inside the resolver itself.
            if (
                not istestfunction(func=task_function)
                and isnested(func=task_function)
                and not is_functools_wrapped_module_level(task_function)
            ):
                raise ValueError(
                    "TaskFunction cannot be a nested/inner or local function. "
                    "It should be accessible at a module level for Flyte to execute it. Test modules with "
                    "names beginning with `test_` are allowed to have nested tasks. "
                    "If you're decorating your task function with custom decorators, use functools.wraps "
                    "or functools.update_wrapper on the function wrapper. "
                    "Alternatively if you want to create your own tasks with custom behavior use the TaskResolverMixin"
                )
        self._task_function = task_function
        self._execution_mode = execution_mode

    @property
    def execution_mode(self) -> ExecutionBehavior:
        return self._execution_mode

    @property
    def task_function(self):
        return self._task_function

    def execute(self, **kwargs) -> Any:
        """
        This method will be invoked to execute the task. If you do decide to override this method you must also
        handle dynamic tasks or you will no longer be able to use the task as a dynamic task generator.
        """
        if self.execution_mode == self.ExecutionBehavior.DEFAULT:
            return exception_scopes.user_entry_point(self._task_function)(**kwargs)
        elif self.execution_mode == self.ExecutionBehavior.DYNAMIC:
            return self.dynamic_execute(self._task_function, **kwargs)

    def compile_into_workflow(
        self, ctx: FlyteContext, task_function: Callable, **kwargs
    ) -> Union[_dynamic_job.DynamicJobSpec, _literal_models.LiteralMap]:
        """
        In the case of dynamic workflows, this function will produce a workflow definition at execution time which will
        then proceed to be executed.
        """
        # TODO: circular import
        from flytekit.core.task import ReferenceTask

        if not ctx.compilation_state:
            cs = ctx.new_compilation_state(prefix="d")
        else:
            cs = ctx.compilation_state.with_params(prefix="d")

        with FlyteContextManager.with_context(ctx.with_compilation_state(cs)):
            # TODO: Resolve circular import
            from flytekit.tools.translator import get_serializable

            workflow_metadata = WorkflowMetadata(on_failure=WorkflowFailurePolicy.FAIL_IMMEDIATELY)
            defaults = WorkflowMetadataDefaults(
                interruptible=self.metadata.interruptible if self.metadata.interruptible is not None else False
            )

            self._wf = PythonFunctionWorkflow(task_function, metadata=workflow_metadata, default_metadata=defaults)
            self._wf.compile(**kwargs)

            wf = self._wf
            model_entities = OrderedDict()
            # See comment on reference entity checking a bit down below in this function.
            # This is the only circular dependency between the translator.py module and the rest of the flytekit
            # authoring experience.
            workflow_spec: admin_workflow_models.WorkflowSpec = get_serializable(
                model_entities, ctx.serialization_settings, wf
            )

            # If no nodes were produced, let's just return the strict outputs
            if len(workflow_spec.template.nodes) == 0:
                return _literal_models.LiteralMap(
                    literals={
                        binding.var: binding.binding.to_literal_model() for binding in workflow_spec.template.outputs
                    }
                )

            # Gather underlying TaskTemplates that get referenced.
            tts = []
            for entity, model in model_entities.items():
                # We only care about gathering tasks here. Launch plans are handled by
                # propeller. Subworkflows should already be in the workflow spec.
                if not isinstance(entity, Task) and not isinstance(entity, task_models.TaskTemplate):
                    continue

                # Handle FlyteTask
                if isinstance(entity, task_models.TaskTemplate):
                    tts.append(entity)
                    continue

                # We are currently not supporting reference tasks since these will
                # require a network call to flyteadmin to populate the TaskTemplate
                # model
                if isinstance(entity, ReferenceTask):
                    raise Exception("Reference tasks are currently unsupported within dynamic tasks")

                if not isinstance(model, task_models.TaskSpec):
                    raise TypeError(
                        f"Unexpected type for serialized form of task. Expected {task_models.TaskSpec}, but got {type(model)}"
                    )

                # Store the valid task template so that we can pass it to the
                # DynamicJobSpec later
                tts.append(model.template)

            dj_spec = _dynamic_job.DynamicJobSpec(
                min_successes=len(workflow_spec.template.nodes),
                tasks=tts,
                nodes=workflow_spec.template.nodes,
                outputs=workflow_spec.template.outputs,
                subworkflows=workflow_spec.sub_workflows,
            )

            return dj_spec

    def dynamic_execute(self, task_function: Callable, **kwargs) -> Any:
        """
        By the time this function is invoked, the local_execute function should have unwrapped the Promises and Flyte
        literal wrappers so that the kwargs we are working with here are now Python native literal values. This
        function is also expected to return Python native literal values.

        Since the user code within a dynamic task constitute a workflow, we have to first compile the workflow, and
        then execute that workflow.

        When running for real in production, the task would stop after the compilation step, and then create a file
        representing that newly generated workflow, instead of executing it.
        """
        ctx = FlyteContextManager.current_context()
        # This is a placeholder SerializationSettings placeholder and is only used to test compilation for dynamic tasks
        # when run locally. The output of the compilation should never actually be used anywhere.
        _LOCAL_ONLY_SS = SerializationSettings.for_image(DefaultImages.default_image(), "v", "p", "d")

        if ctx.execution_state and ctx.execution_state.mode == ExecutionState.Mode.LOCAL_WORKFLOW_EXECUTION:
            updated_exec_state = ctx.execution_state.with_params(mode=ExecutionState.Mode.TASK_EXECUTION)
            with FlyteContextManager.with_context(
                ctx.with_execution_state(updated_exec_state).with_serialization_settings(_LOCAL_ONLY_SS)
            ) as ctx:
                logger.debug(f"Running compilation for {self} as part of local run as check")
                self.compile_into_workflow(ctx, task_function, **kwargs)
                logger.info("Executing Dynamic workflow, using raw inputs")
                return exception_scopes.user_entry_point(task_function)(**kwargs)

        if ctx.execution_state and ctx.execution_state.mode == ExecutionState.Mode.TASK_EXECUTION:
            return self.compile_into_workflow(ctx, task_function, **kwargs)

        if ctx.execution_state and ctx.execution_state.mode == ExecutionState.Mode.LOCAL_TASK_EXECUTION:
            return exception_scopes.user_entry_point(task_function)(**kwargs)

        raise ValueError(f"Invalid execution provided, execution state: {ctx.execution_state}")
